# -*- coding: utf-8 -*-
"""SuperStar Web 控制面板后端（多 Cookie 会话、扫码一体选课、实时监控 SSE、Docker/进程调度）。"""

import hmac
import json
import os
import time

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from api.base import fetch_courses_with_cookies, validate_cookies_dict
from api.cookies import (
    clear_cookies_file,
    get_cookies_path,
    parse_cookie_string,
    save_cookies_dict,
    use_cookies,
)
from api.progress import ProgressReporter  # noqa: F401
from api.qr_login import qr_login_manager
from api.question_bank import QuestionBankDAO
from web.api_store import api_store
from web.instance_manager import SafetyLimitError, instance_manager
from web.session_store import session_store
from web.system_monitor import get_system_overview, settings_store, system_monitor

question_bank = QuestionBankDAO()


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    system_monitor.ensure_started()

    @app.before_request
    def _require_web_password():
        expected_pw = os.environ.get("WEB_PASSWORD", "")
        if not expected_pw or request.path == "/api/health":
            return None
        auth = request.authorization
        expected_user = os.environ.get("WEB_USERNAME")
        pw_ok = auth is not None and hmac.compare_digest(auth.password or "", expected_pw)
        user_ok = True
        if expected_user:
            user_ok = auth is not None and hmac.compare_digest(auth.username or "", expected_user)
        if not (pw_ok and user_ok):
            return Response(
                "Unauthorized",
                401,
                {"WWW-Authenticate": 'Basic realm="SuperStar Control Panel"'},
            )
        return None

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    # ---------------- 宿主机硬件 & 安全阈值 & 实时监控 SSE ----------------

    @app.get("/api/system/overview")
    def system_overview():
        running = instance_manager.count_running()
        return jsonify(get_system_overview(running_containers=running))

    @app.put("/api/system/thresholds")
    def update_thresholds():
        payload = request.get_json(silent=True) or {}
        try:
            updated = settings_store.update_thresholds(payload)
            running = instance_manager.count_running()
            return jsonify({"ok": True, "thresholds": updated, "overview": get_system_overview(running)})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/stream/monitor")
    def stream_monitor():
        def generate():
            while True:
                instances = instance_manager.list_instances()
                running = sum(1 for item in instances if item.get("runtime_status") == "running")
                payload = {
                    "overview": get_system_overview(running_containers=running),
                    "instances": instances,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                time.sleep(2)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ---------------- 多账号 Cookie 会话 & 二维码登录 ----------------

    @app.get("/api/sessions")
    def list_sessions():
        return jsonify({"items": session_store.list_sessions()})

    @app.post("/api/sessions/import")
    def import_session():
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("cookie") or "").strip()
        label = str(payload.get("label") or "").strip() or None
        cookies = parse_cookie_string(raw)
        if not cookies:
            return jsonify({"ok": False, "error": "Cookie 格式无效，应为 key=value; ..."}), 400
        validation = validate_cookies_dict(cookies)
        meta = session_store.save_session(cookies, uid=validation.get("uid"), label=label)
        return jsonify(
            {
                "ok": True,
                "session": meta,
                "valid": validation["valid"],
                "msg": validation["msg"],
            }
        )

    @app.delete("/api/sessions/<session_id>")
    def delete_session(session_id: str):
        try:
            removed = session_store.delete_session(session_id)
            if not removed:
                return jsonify({"ok": False, "error": "未找到该会话"}), 404
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/sessions/<session_id>/courses")
    def get_session_courses(session_id: str):
        try:
            cookies = session_store.get_session_cookies(session_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not cookies:
            return jsonify({"ok": False, "error": "会话不存在或 Cookie 为空"}), 404

        validation = validate_cookies_dict(cookies)
        if not validation["valid"]:
            return jsonify({"ok": False, "error": validation["msg"]}), 401

        try:
            courses = fetch_courses_with_cookies(cookies)
            return jsonify({"ok": True, "courses": courses})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"拉取课程列表失败: {exc}"}), 502

    @app.post("/api/auth/qr/create")
    def auth_qr_create():
        try:
            data = qr_login_manager.create_qr_session()
            return jsonify({"ok": True, **data})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.get("/api/auth/qr/poll/<session_id>")
    def auth_qr_poll(session_id: str):
        instance_id = request.args.get("instance_id", "").strip()
        result = qr_login_manager.poll_qr_session(session_id, return_cookies=True)
        if result.get("status") == "confirmed":
            cookies = result.pop("cookies", {})
            if cookies:
                saved_meta = session_store.save_session(
                    cookies,
                    uid=result.get("uid"),
                    nickname=result.get("nickname"),
                )
                result["saved_session"] = saved_meta
                if instance_id:
                    try:
                        instance_manager.update_instance_cookies(instance_id, cookies)
                        result["instance_updated"] = True
                    except (KeyError, ValueError) as exc:
                        result["instance_updated"] = False
                        result["instance_error"] = str(exc)
        return jsonify({"ok": result.get("status") != "error", **result})

    @app.get("/api/auth/cookie/status")
    def auth_cookie_status():
        cookies_path = get_cookies_path()
        exists = os.path.exists(cookies_path)
        if not exists:
            return jsonify(
                {
                    "exists": False,
                    "valid": False,
                    "uid": None,
                    "cookie_count": 0,
                    "path": cookies_path,
                    "msg": "未检测到 cookies.txt",
                }
            )
        cookies = use_cookies(cookies_path)
        validation = validate_cookies_dict(cookies)
        return jsonify(
            {
                "exists": True,
                "valid": validation["valid"],
                "uid": validation.get("uid"),
                "cookie_count": len(cookies),
                "path": cookies_path,
                "msg": validation["msg"],
            }
        )

    @app.post("/api/auth/cookie/import")
    def auth_cookie_import():
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("cookie") or "").strip()
        cookies = parse_cookie_string(raw)
        if not cookies:
            return jsonify({"ok": False, "error": "Cookie 格式无效，应为 key=value; ..."}), 400
        saved_path = save_cookies_dict(cookies)
        validation = validate_cookies_dict(cookies)
        return jsonify(
            {
                "ok": True,
                "valid": validation["valid"],
                "uid": validation.get("uid"),
                "cookie_count": len(cookies),
                "path": saved_path,
                "msg": validation["msg"],
            }
        )

    @app.delete("/api/auth/cookie")
    def auth_cookie_delete():
        removed = clear_cookies_file()
        return jsonify({"ok": True, "removed": removed})

    @app.get("/api/courses")
    def list_courses():
        session_id = request.args.get("session_id", "").strip()
        instance_id = request.args.get("instance_id", "").strip()
        if session_id:
            try:
                cookies = session_store.get_session_cookies(session_id)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        elif instance_id:
            try:
                inst_cookies = os.path.join(instance_manager._inst_dir(instance_id), "cookies.txt")
                cookies = use_cookies(inst_cookies)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        else:
            cookies = use_cookies()

        if not cookies:
            return jsonify({"ok": False, "error": "当前无可用 Cookie，请先扫码登录或选择账号会话"}), 400

        validation = validate_cookies_dict(cookies)
        if not validation["valid"]:
            return jsonify({"ok": False, "error": validation["msg"]}), 401

        try:
            courses = fetch_courses_with_cookies(cookies)
            return jsonify({"ok": True, "courses": courses})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"拉取课程列表失败: {exc}"}), 502

    # ---------------- AI 答题 API 管理 ----------------

    @app.get("/api/ai-apis")
    def list_ai_apis():
        return jsonify({"items": api_store.list_profiles(masked=True)})

    @app.post("/api/ai-apis")
    def add_ai_api():
        payload = request.get_json(silent=True) or {}
        try:
            item = api_store.add_profile(payload)
            return jsonify({"ok": True, "item": item}), 201
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.put("/api/ai-apis/<profile_id>")
    def update_ai_api(profile_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item = api_store.update_profile(profile_id, payload)
            return jsonify({"ok": True, "item": item})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/ai-apis/<profile_id>")
    def delete_ai_api(profile_id: str):
        try:
            api_store.delete_profile(profile_id)
            return jsonify({"ok": True})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/ai-apis/<profile_id>/default")
    def set_default_ai_api(profile_id: str):
        try:
            item = api_store.set_default(profile_id)
            return jsonify({"ok": True, "item": item})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/ai-apis/<profile_id>/test")
    def test_ai_api(profile_id: str):
        try:
            res = api_store.test_profile(profile_id)
            return jsonify(res)
        except KeyError as exc:
            return jsonify({"ok": False, "msg": str(exc)}), 404

    # ---------------- Docker / 本地实例调度与监控 ----------------

    @app.get("/api/instances")
    def get_instances():
        return jsonify({"items": instance_manager.list_instances()})

    @app.post("/api/instances")
    def create_instance():
        payload = request.get_json(silent=True) or {}
        auto_start = bool(payload.get("auto_start", True))
        try:
            item = instance_manager.create_instance(payload, auto_start=auto_start)
            return jsonify({"ok": True, "item": item}), 201
        except SafetyLimitError as exc:
            return jsonify({"ok": False, "error": str(exc), "reasons": exc.reasons}), 409
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"创建或启动实例失败: {exc}"}), 500

    @app.post("/api/instances/<instance_id>/start")
    def start_instance(instance_id: str):
        try:
            item = instance_manager.start_instance(instance_id)
            return jsonify({"ok": True, "item": item})
        except SafetyLimitError as exc:
            return jsonify({"ok": False, "error": str(exc), "reasons": exc.reasons}), 409
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"启动实例失败: {exc}"}), 500

    @app.post("/api/instances/<instance_id>/stop")
    def stop_instance(instance_id: str):
        try:
            item = instance_manager.stop_instance(instance_id)
            return jsonify({"ok": True, "item": item})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/instances/<instance_id>")
    def delete_instance(instance_id: str):
        try:
            instance_manager.delete_instance(instance_id)
            return jsonify({"ok": True})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/instances/clear-completed")
    def clear_completed_instances():
        removed = instance_manager.clear_completed()
        return jsonify({"ok": True, "removed": removed})

    @app.get("/api/instances/<instance_id>/logs")
    def get_instance_logs(instance_id: str):
        try:
            tail = int(request.args.get("tail", 200))
        except ValueError:
            tail = 200
        include_trace = request.args.get("trace", "").lower() in {"1", "true", "yes"}
        try:
            logs = instance_manager.get_logs(instance_id, tail=tail, include_trace=include_trace)
            return jsonify({"ok": True, "logs": logs})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/instances/<instance_id>/portal")
    def get_instance_portal(instance_id: str):
        refresh = request.args.get("refresh", "").lower() in {"1", "true", "yes"}
        try:
            data = instance_manager.get_instance_portal(instance_id, refresh=refresh)
            return jsonify({"ok": True, **data})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.put("/api/instances/<instance_id>/options")
    def update_instance_options(instance_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item = instance_manager.update_instance_options(instance_id, payload)
            return jsonify({"ok": True, "item": item})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/instances/<instance_id>/courses/<course_id>/chapters")
    def get_instance_course_chapters(instance_id: str, course_id: str):
        try:
            data = instance_manager.get_course_chapters(instance_id, course_id)
            return jsonify({"ok": True, **data})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"拉取章节列表失败: {exc}"}), 502

    @app.get("/api/instances/<instance_id>/courses/<course_id>/chapters/<knowledge_id>/cards")
    def get_instance_chapter_cards(instance_id: str, course_id: str, knowledge_id: str):
        try:
            data = instance_manager.get_chapter_cards(instance_id, course_id, knowledge_id)
            return jsonify({"ok": True, **data})
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"拉取章节任务点失败: {exc}"}), 502

    @app.get("/api/instances/<instance_id>/chaoxing-work")
    def proxy_instance_chaoxing_work(instance_id: str):
        try:
            html, _ = instance_manager.fetch_chaoxing_work_html(
                instance_id,
                {
                    "courseId": request.args.get("courseId", ""),
                    "clazzId": request.args.get("clazzId", ""),
                    "cpi": request.args.get("cpi", ""),
                    "knowledgeId": request.args.get("knowledgeId", ""),
                    "jobid": request.args.get("jobid", ""),
                    "ktoken": request.args.get("ktoken", ""),
                    "enc": request.args.get("enc", ""),
                },
            )
            return Response(html, mimetype="text/html; charset=utf-8")
        except KeyError as exc:
            return Response(f"<h3>{exc}</h3>", status=404, mimetype="text/html; charset=utf-8")
        except ValueError as exc:
            return Response(f"<h3>{exc}</h3>", status=400, mimetype="text/html; charset=utf-8")
        except Exception as exc:
            return Response(f"<h3>加载测验页面失败: {exc}</h3>", status=502, mimetype="text/html; charset=utf-8")

    # ---------------- 按课程划分的自建题库 ----------------

    @app.get("/api/bank/courses")
    def list_bank_courses():
        return jsonify({"items": question_bank.list_courses()})

    @app.get("/api/bank/questions")
    def list_bank_questions():
        course_id = request.args.get("course_id", "").strip() or None
        q = request.args.get("q", "").strip()
        try:
            limit = int(request.args.get("limit", 100))
            offset = int(request.args.get("offset", 0))
        except ValueError:
            limit, offset = 100, 0
        return jsonify(question_bank.list_questions(course_id=course_id, query=q, limit=limit, offset=offset))

    @app.put("/api/bank/questions/<int:qid>")
    def update_bank_question(qid: int):
        payload = request.get_json(silent=True) or {}
        try:
            updated = question_bank.update_answer(qid, str(payload.get("answer") or ""))
            if not updated:
                return jsonify({"ok": False, "error": "未找到该题目"}), 404
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/bank/questions/<int:qid>")
    def delete_bank_question(qid: int):
        if not question_bank.delete_question(qid):
            return jsonify({"ok": False, "error": "未找到该题目"}), 404
        return jsonify({"ok": True})

    @app.delete("/api/bank/courses/<course_id>")
    def delete_bank_course(course_id: str):
        removed = question_bank.delete_course(course_id)
        return jsonify({"ok": True, "removed": removed})

    return app


app = create_app()
