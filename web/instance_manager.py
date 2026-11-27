# -*- coding: utf-8 -*-
"""隔离刷课任务进程管理器（单容器 / 本地通用的轻量隔离进程架构）。"""

from collections import deque
import configparser
from io import StringIO
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, urlparse
import uuid
from typing import Any

import psutil

from api.base import _build_isolated_session, fetch_courses_with_cookies
from api.cookies import parse_cookie_string, save_cookies_dict, use_cookies
from api.decode import decode_course_catalog
from web.api_store import api_store
from web.session_store import session_store
from web.system_monitor import check_safety_guard

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCES_DIR = os.path.join(ROOT_DIR, "instances")


class SafetyLimitError(RuntimeError):
    """当服务器硬件负载或并发数达到安全阈值时抛出。"""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("；".join(reasons))


def mask_sensitive_log(text: str) -> str:
    """脱敏日志中的 API Key 与长 Cookie 值。"""
    if not text:
        return ""
    masked = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", text)
    masked = re.sub(
        r"(?i)\b(vc3|uf|_uid|UID|fid|JSESSIONID)=([^;\s,'\"]+)",
        lambda m: f"{m.group(1)}=***",
        masked,
    )
    return masked


def _build_chaoxing_course_url(course_id: str, clazz_id: str, cpi: str) -> str:
    return (
        f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
        f"?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ut=s"
    )


def _inject_html_base(html: str, final_url: str, banner_text: str = "") -> str:
    parsed = urlparse(final_url or "https://mooc1.chaoxing.com/mooc-ans/")
    if parsed.scheme and parsed.netloc:
        dir_path = (parsed.path.rsplit("/", 1)[0] + "/") if "/" in parsed.path else "/"
        base_href = f"{parsed.scheme}://{parsed.netloc}{dir_path}"
    else:
        base_href = "https://mooc1.chaoxing.com/mooc-ans/"
    safe_base = base_href.replace('"', "&quot;")
    base_tag = f'<base href="{safe_base}" target="_blank">'
    if re.search(r"<head[^>]*>", html, flags=re.I):
        out = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + base_tag, html, count=1, flags=re.I)
    else:
        out = base_tag + html
    if banner_text:
        banner_html = (
            '<div style="background:#e8f0fe;color:#1a73e8;padding:8px 14px;'
            'font-family:sans-serif;font-size:13px;border-bottom:1px solid #d2e3fc;">'
            f"{banner_text}</div>"
        )
        if re.search(r"<body[^>]*>", out, flags=re.I):
            out = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner_html, out, count=1, flags=re.I)
        else:
            out = banner_html + out
    return out


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="meta_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _read_json(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


_LOGURU_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+(?:\s+[+-]\d{2}:\d{2})?\s+\|\s+([A-Z]+)\s+\|"
)


def _filter_key_log_lines(text: str, tail: int) -> str:
    """过滤掉 TRACE / DEBUG 刷屏行，保留 INFO / WARNING / ERROR 及多行堆栈。"""
    lines = text.splitlines()
    has_loguru = any(_LOGURU_LINE_RE.match(line) for line in lines)
    if not has_loguru:
        return "\n".join(lines[-tail:])

    kept: list[str] = []
    # 默认丢弃首个时间戳前被截断出来的多行 TRACE/DEBUG 碎片（如 HTML 片段）
    keep_continuation = False
    for line in lines:
        m = _LOGURU_LINE_RE.match(line)
        if m:
            level = m.group(1).upper()
            keep_continuation = level not in {"TRACE", "DEBUG"}
        if keep_continuation:
            kept.append(line)
    return "\n".join(kept[-tail:])


def _tail_file(path: str, lines: int = 200) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf8", errors="replace") as f:
            return "".join(deque(f, maxlen=lines))
    except OSError:
        return ""


class InstanceManager:
    """管理完全隔离的刷课子进程实例（每实例独立目录、Cookie、配置与日志）。"""

    def __init__(self, instances_dir: str = INSTANCES_DIR) -> None:
        self.instances_dir = instances_dir
        self._lock = threading.RLock()
        self._local_procs: dict[str, subprocess.Popen] = {}
        self._local_ps: dict[str, psutil.Process] = {}
        os.makedirs(self.instances_dir, exist_ok=True)

    def _inst_dir(self, instance_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", instance_id or ""):
            raise ValueError("非法的实例 ID")
        return os.path.join(self.instances_dir, instance_id)

    @staticmethod
    def _build_instance_config(
        courses: list[str],
        speed: float,
        jobs: int,
        notopen_action: str,
        api_profile: dict[str, Any] | None,
    ) -> str:
        parser = configparser.ConfigParser(interpolation=None)
        parser["common"] = {
            "use_cookies": "true",
            "username": "",
            "password": "",
            "course_list": ",".join(courses),
            "speed": str(speed),
            "jobs": str(jobs),
            "notopen_action": notopen_action,
        }
        if api_profile:
            parser["tiku"] = {
                "provider": "AI",
                "check_llm_connection": "true",
                "submit": "true" if api_profile.get("submit", True) else "false",
                "cover_rate": str(api_profile.get("cover_rate", 0.9)),
                "delay": str(api_profile.get("delay", 1.0)),
                "use_cache": "true" if api_profile.get("use_cache", True) else "false",
                "endpoint": str(api_profile.get("endpoint", "")),
                "key": str(api_profile.get("key", "")),
                "model": str(api_profile.get("model", "")),
                "min_interval_seconds": str(api_profile.get("min_interval_seconds", 3)),
                "http_proxy": str(api_profile.get("http_proxy", "")),
                "true_list": "正确,对,√,是",
                "false_list": "错误,错,×,否,不对,不正确",
            }
        else:
            parser["tiku"] = {
                "provider": "",
                "check_llm_connection": "false",
                "submit": "false",
                "cover_rate": "0.9",
                "delay": "1.0",
                "use_cache": "true",
                "true_list": "正确,对,√,是",
                "false_list": "错误,错,×,否,不对,不正确",
            }
        parser["notification"] = {
            "provider": "ServerChan",
            "url": "",
            "tg_chat_id": "",
        }
        buf = StringIO()
        parser.write(buf)
        return buf.getvalue()

    def _is_proc_running(self, instance_id: str, meta: dict[str, Any]) -> bool:
        proc = self._local_procs.get(instance_id)
        if proc is not None:
            if proc.poll() is None:
                return True
            self._local_procs.pop(instance_id, None)
            self._local_ps.pop(instance_id, None)
            return False

        pid = meta.get("pid")
        if pid:
            try:
                p = psutil.Process(int(pid))
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    cmdline = " ".join(p.cmdline())
                    if "main.py" in cmdline and instance_id in cmdline:
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                pass
        return False

    def count_running(self) -> int:
        return sum(1 for item in self.list_instances() if item["runtime_status"] == "running")

    def list_instances(self) -> list[dict[str, Any]]:
        with self._lock:
            if not os.path.isdir(self.instances_dir):
                return []
            items: list[dict[str, Any]] = []
            for name in os.listdir(self.instances_dir):
                inst_dir = os.path.join(self.instances_dir, name)
                if not os.path.isdir(inst_dir):
                    continue
                meta = _read_json(os.path.join(inst_dir, "meta.json"))
                if not meta:
                    continue
                items.append(self._build_status_view(inst_dir, meta))

            items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            return items

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        inst_dir = self._inst_dir(instance_id)
        meta = _read_json(os.path.join(inst_dir, "meta.json"))
        if not meta:
            return None
        return self._build_status_view(inst_dir, meta)

    def _build_status_view(self, inst_dir: str, meta: dict[str, Any]) -> dict[str, Any]:
        instance_id = meta["id"]
        progress = _read_json(os.path.join(inst_dir, "progress.json")) or {
            "status": "idle",
            "message": "尚未启动",
            "overall_percent": 0.0,
            "courses_total": len(meta.get("courses") or []),
            "courses_completed": 0,
            "courses": [],
            "current_course": None,
            "chapters_total": 0,
            "chapters_completed": 0,
            "chapters_failed": 0,
            "current_chapter": "",
            "current_job": None,
            "active_jobs": [],
        }

        cpu_percent = 0.0
        mem_mb = 0.0
        if self._is_proc_running(instance_id, meta):
            runtime_status = "running"
            pid = meta.get("pid")
            if pid:
                try:
                    p = self._local_ps.get(instance_id) or psutil.Process(int(pid))
                    self._local_ps[instance_id] = p
                    cpu_percent = round(float(p.cpu_percent(interval=None)), 1)
                    mem_mb = round(p.memory_info().rss / (1024 * 1024), 1)
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    pass
        else:
            p_status = progress.get("status")
            runtime_status = p_status if p_status in ("completed", "error", "stopped") else "stopped"

        if runtime_status != "running" and progress.get("status") in ("starting", "running"):
            progress["status"] = runtime_status
            if runtime_status == "stopped":
                progress["message"] = "任务已停止"

        if runtime_status != "running":
            progress["active_jobs"] = []
            progress["current_job"] = None
            for c_item in progress.get("courses") or []:
                if c_item.get("status") == "running":
                    c_item["status"] = runtime_status

        course_ids = meta.get("courses") or []
        course_titles = meta.get("course_titles") or {}
        if not progress.get("courses") and course_ids:
            default_status = "completed" if runtime_status == "completed" else "pending"
            default_pct = 100.0 if default_status == "completed" else 0.0
            progress["courses"] = [
                {
                    "id": str(cid),
                    "title": str(course_titles.get(str(cid), f"课程 {cid}")),
                    "index": idx,
                    "status": default_status,
                    "chapters_total": 0,
                    "chapters_completed": 0,
                    "chapters_failed": 0,
                    "percent": default_pct,
                }
                for idx, cid in enumerate(course_ids, 1)
            ]
        if "active_jobs" not in progress or progress["active_jobs"] is None:
            progress["active_jobs"] = []

        return {
            "id": instance_id,
            "name": meta.get("name", instance_id),
            "mode": "process",
            "pid": meta.get("pid"),
            "runtime_status": runtime_status,
            "cpu_percent": cpu_percent,
            "mem_mb": mem_mb,
            "courses": meta.get("courses") or [],
            "course_titles": meta.get("course_titles") or {},
            "speed": meta.get("speed", 2.0),
            "jobs": meta.get("jobs", 4),
            "notopen_action": meta.get("notopen_action", "retry"),
            "redo_finished": bool(meta.get("redo_finished", False)),
            "no_cache": bool(meta.get("no_cache", False)),
            "api_profile_id": meta.get("api_profile_id"),
            "api_profile_name": meta.get("api_profile_name") or "仅刷视频",
            "progress": progress,
            "created_at": meta.get("created_at", 0),
        }

    def create_instance(self, payload: dict[str, Any], auto_start: bool = True) -> dict[str, Any]:
        with self._lock:
            if auto_start:
                guard = check_safety_guard(running_containers=self.count_running())
                if not guard["allowed"]:
                    raise SafetyLimitError(guard["reasons"])

            name = str(payload.get("name") or "").strip() or f"task-{uuid.uuid4().hex[:4]}"
            courses_raw = payload.get("courses") or []
            if isinstance(courses_raw, str):
                courses = [c.strip() for c in re.split(r"[,\s]+", courses_raw) if c.strip()]
            else:
                courses = [str(c).strip() for c in courses_raw if str(c).strip()]

            courses = list(dict.fromkeys(courses))
            if not courses:
                raise ValueError("请至少选择或输入 1 个课程 ID")

            try:
                speed = float(payload.get("speed", 2.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("倍速参数无效") from exc
            if not (1.0 <= speed <= 2.0):
                raise ValueError("视频倍速安全范围为 1.0 ~ 2.0")

            try:
                jobs = int(payload.get("jobs", 4))
            except (TypeError, ValueError) as exc:
                raise ValueError("章节并发数无效") from exc
            if not (1 <= jobs <= 4):
                raise ValueError("章节并发数安全范围为 1 ~ 4")

            notopen_action = str(payload.get("notopen_action") or "retry").strip()
            if notopen_action not in ("retry", "continue"):
                notopen_action = "retry"

            api_profile_id = str(payload.get("api_profile_id") or "").strip() or None
            api_profile = None
            if api_profile_id:
                api_profile = api_store.get_profile(api_profile_id, masked=False)
                if not api_profile:
                    raise ValueError("所选 AI API 配置不存在")

            cookie_source = str(payload.get("cookie_source") or "session").strip()
            if cookie_source == "session":
                cookie_session_id = str(payload.get("cookie_session_id") or "").strip()
                if not cookie_session_id:
                    raise ValueError("请先扫码登录或选择已保存账号")
                cookies = session_store.get_session_cookies(cookie_session_id)
                if not cookies:
                    raise ValueError("所选账号会话不存在或 Cookie 为空")
            elif cookie_source == "custom":
                cookies = parse_cookie_string(str(payload.get("custom_cookie") or ""))
                if not cookies:
                    raise ValueError("自定义 Cookie 格式无效")
            else:
                cookies = use_cookies()
                if not cookies:
                    raise ValueError("当前无可用 Cookie")

            instance_id = uuid.uuid4().hex[:8]
            inst_dir = self._inst_dir(instance_id)
            os.makedirs(inst_dir, exist_ok=True)

            save_cookies_dict(cookies, path=os.path.join(inst_dir, "cookies.txt"))
            cfg_content = self._build_instance_config(courses, speed, jobs, notopen_action, api_profile)
            with open(os.path.join(inst_dir, "config.ini"), "w", encoding="utf8") as f:
                f.write(cfg_content)

            course_titles = payload.get("course_titles") if isinstance(payload.get("course_titles"), dict) else {}
            raw_links = payload.get("course_links") if isinstance(payload.get("course_links"), dict) else {}
            course_links: dict[str, dict[str, str]] = {}
            for cid_key, link_info in raw_links.items():
                if not isinstance(link_info, dict):
                    continue
                cid_str = str(cid_key).strip()
                clazz_id = str(link_info.get("clazzId") or "").strip()
                cpi = str(link_info.get("cpi") or "").strip()
                if cid_str and clazz_id and cpi:
                    course_links[cid_str] = {
                        "clazzId": clazz_id,
                        "cpi": cpi,
                        "title": str(link_info.get("title") or course_titles.get(cid_str, "")),
                        "teacher": str(link_info.get("teacher") or ""),
                        "url": _build_chaoxing_course_url(cid_str, clazz_id, cpi),
                    }

            meta: dict[str, Any] = {
                "id": instance_id,
                "name": name,
                "mode": "process",
                "pid": None,
                "courses": courses,
                "course_titles": {str(k): str(v) for k, v in course_titles.items()},
                "course_links": course_links,
                "speed": speed,
                "jobs": jobs,
                "notopen_action": notopen_action,
                "redo_finished": bool(payload.get("redo_finished", False)),
                "no_cache": bool(payload.get("no_cache", False)),
                "api_profile_id": api_profile_id,
                "api_profile_name": api_profile["name"] if api_profile else "仅刷视频",
                "created_at": int(time.time()),
            }
            _atomic_write_json(os.path.join(inst_dir, "meta.json"), meta)

        if auto_start:
            return self.start_instance(instance_id)
        return self.get_instance(instance_id)  # type: ignore[return-value]

    def start_instance(self, instance_id: str) -> dict[str, Any]:
        with self._lock:
            inst_dir = self._inst_dir(instance_id)
            meta = _read_json(os.path.join(inst_dir, "meta.json"))
            if not meta:
                raise KeyError("未找到指定任务")

            current = self.get_instance(instance_id)
            if current and current["runtime_status"] == "running":
                return current

            guard = check_safety_guard(running_containers=self.count_running())
            if not guard["allowed"]:
                raise SafetyLimitError(guard["reasons"])

            proc = self._start_process(inst_dir, meta)
            meta["pid"] = proc.pid
            _atomic_write_json(os.path.join(inst_dir, "meta.json"), meta)
            return self.get_instance(instance_id)  # type: ignore[return-value]

    def _start_process(self, inst_dir: str, meta: dict[str, Any]) -> subprocess.Popen:
        cfg_path = os.path.join(inst_dir, "config.ini")
        cookies_path = os.path.join(inst_dir, "cookies.txt")
        progress_path = os.path.join(inst_dir, "progress.json")
        log_path = os.path.join(inst_dir, "runner.log")

        cmd = [
            sys.executable,
            os.path.join(ROOT_DIR, "main.py"),
            "-c",
            cfg_path,
            "--use-cookies",
            "--cookies-path",
            cookies_path,
            "--non-interactive",
            "--status-file",
            progress_path,
        ]
        if meta.get("redo_finished"):
            cmd.append("--redo-finished")
        if meta.get("no_cache"):
            cmd.append("--no-cache")

        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "CHAOXING_BANK_PATH": os.path.join(ROOT_DIR, "data", "question_bank.sqlite3"),
            "PYTHONPATH": ROOT_DIR + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        }
        log_fp = open(log_path, "a", encoding="utf8")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=inst_dir,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                env=env,
            )
        finally:
            log_fp.close()

        self._local_procs[meta["id"]] = proc
        try:
            p = psutil.Process(proc.pid)
            p.cpu_percent(interval=None)
            self._local_ps[meta["id"]] = p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return proc

    def stop_instance(self, instance_id: str) -> dict[str, Any]:
        with self._lock:
            inst_dir = self._inst_dir(instance_id)
            meta = _read_json(os.path.join(inst_dir, "meta.json"))
            if not meta:
                raise KeyError("未找到指定任务")

            pid = meta.get("pid")
            if pid:
                try:
                    p = psutil.Process(int(pid))
                    children = p.children(recursive=True)
                    for ch in children:
                        ch.terminate()
                    p.terminate()
                    _, alive = psutil.wait_procs([p, *children], timeout=4)
                    for a in alive:
                        a.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    pass
            self._local_procs.pop(instance_id, None)
            self._local_ps.pop(instance_id, None)

            prog_path = os.path.join(inst_dir, "progress.json")
            prog = _read_json(prog_path)
            if prog and prog.get("status") in ("starting", "running"):
                prog["status"] = "stopped"
                prog["message"] = "已手动停止"
                prog["updated_at"] = int(time.time())
                _atomic_write_json(prog_path, prog)

            return self.get_instance(instance_id)  # type: ignore[return-value]

    def delete_instance(self, instance_id: str) -> None:
        with self._lock:
            inst_dir = self._inst_dir(instance_id)
            meta = _read_json(os.path.join(inst_dir, "meta.json"))
            if not meta:
                raise KeyError("未找到指定任务")
            self.stop_instance(instance_id)
            shutil.rmtree(inst_dir, ignore_errors=True)

    def clear_completed(self) -> int:
        with self._lock:
            completed_ids = [
                item["id"]
                for item in self.list_instances()
                if item.get("runtime_status") == "completed"
            ]
            for instance_id in completed_ids:
                self.delete_instance(instance_id)
            return len(completed_ids)

    def get_logs(self, instance_id: str, tail: int = 200, include_trace: bool = False) -> str:
        inst_dir = self._inst_dir(instance_id)
        meta = _read_json(os.path.join(inst_dir, "meta.json"))
        if not meta:
            raise KeyError("未找到指定任务")

        tail = max(10, min(1000, int(tail)))
        raw_tail = tail if include_trace else max(tail * 8, 2000)
        chaoxing_log = os.path.join(inst_dir, "chaoxing.log")
        runner_log = os.path.join(inst_dir, "runner.log")
        text = _tail_file(chaoxing_log, raw_tail) or _tail_file(runner_log, raw_tail)
        if not include_trace and text:
            text = _filter_key_log_lines(text, tail)
        return mask_sensitive_log(text)

    def update_instance_cookies(self, instance_id: str, cookies: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            inst_dir = self._inst_dir(instance_id)
            meta = _read_json(os.path.join(inst_dir, "meta.json"))
            if not meta:
                raise KeyError("未找到指定任务")
            save_cookies_dict(cookies, path=os.path.join(inst_dir, "cookies.txt"))
            return self.get_instance(instance_id)  # type: ignore[return-value]

    def update_instance_options(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            inst_dir = self._inst_dir(instance_id)
            meta_path = os.path.join(inst_dir, "meta.json")
            meta = _read_json(meta_path)
            if not meta:
                raise KeyError("未找到指定任务")
            if "redo_finished" in payload:
                meta["redo_finished"] = bool(payload["redo_finished"])
            if "no_cache" in payload:
                meta["no_cache"] = bool(payload["no_cache"])
            _atomic_write_json(meta_path, meta)
            return self.get_instance(instance_id)  # type: ignore[return-value]

    def get_instance_portal(self, instance_id: str, refresh: bool = False) -> dict[str, Any]:
        inst_dir = self._inst_dir(instance_id)
        meta_path = os.path.join(inst_dir, "meta.json")
        meta = _read_json(meta_path)
        if not meta:
            raise KeyError("未找到指定任务")

        course_ids = [str(c) for c in (meta.get("courses") or [])]
        course_titles = {str(k): str(v) for k, v in (meta.get("course_titles") or {}).items()}
        course_links = {
            str(k): dict(v)
            for k, v in (meta.get("course_links") or {}).items()
            if isinstance(v, dict)
        }

        warning = ""
        need_fetch = refresh or any(
            not (course_links.get(cid) or {}).get("clazzId") or not (course_links.get(cid) or {}).get("cpi")
            for cid in course_ids
        )
        if need_fetch and course_ids:
            cookies = use_cookies(os.path.join(inst_dir, "cookies.txt"))
            if not cookies:
                warning = "当前任务未找到有效 Cookie，请先通过卡片「登录」重新扫码后再查看章节与测验。"
            else:
                try:
                    fetched = fetch_courses_with_cookies(cookies)
                    fetched_map = {str(item.get("courseId", "")): item for item in fetched if item.get("courseId")}
                    changed = False
                    for cid in course_ids:
                        item = fetched_map.get(cid)
                        if not item:
                            continue
                        clazz_id = str(item.get("clazzId") or "").strip()
                        cpi = str(item.get("cpi") or "").strip()
                        title = str(item.get("title") or course_titles.get(cid) or "").strip()
                        teacher = str(item.get("teacher") or "").strip()
                        if title and not course_titles.get(cid):
                            course_titles[cid] = title
                            changed = True
                        if clazz_id and cpi:
                            new_entry = {
                                "clazzId": clazz_id,
                                "cpi": cpi,
                                "title": title,
                                "teacher": teacher,
                                "url": _build_chaoxing_course_url(cid, clazz_id, cpi),
                            }
                            if course_links.get(cid) != new_entry:
                                course_links[cid] = new_entry
                                changed = True
                    if changed:
                        with self._lock:
                            latest_meta = _read_json(meta_path) or meta
                            latest_meta["course_titles"] = course_titles
                            latest_meta["course_links"] = course_links
                            _atomic_write_json(meta_path, latest_meta)
                            meta = latest_meta
                except Exception as exc:
                    warning = f"自动获取课程直达链接失败（可能 Cookie 已过期，可先通过「登录」重新扫码）：{exc}"

        courses_view: list[dict[str, Any]] = []
        for cid in course_ids:
            info = course_links.get(cid) or {}
            clazz_id = str(info.get("clazzId") or "")
            cpi = str(info.get("cpi") or "")
            direct = bool(clazz_id and cpi)
            courses_view.append(
                {
                    "id": cid,
                    "title": str(course_titles.get(cid) or info.get("title") or f"课程 {cid}"),
                    "teacher": str(info.get("teacher") or ""),
                    "clazzId": clazz_id,
                    "cpi": cpi,
                    "direct": direct,
                    "url": _build_chaoxing_course_url(cid, clazz_id, cpi) if direct else "https://i.chaoxing.com/base",
                }
            )

        failed_works_md = ""
        failed_path = os.path.join(inst_dir, "未通过测验记录.md")
        if os.path.exists(failed_path):
            try:
                with open(failed_path, "r", encoding="utf8", errors="replace") as fp:
                    content = fp.read()
                failed_works_md = content[-30000:] if len(content) > 30000 else content
            except OSError:
                failed_works_md = ""

        status_view = self._build_status_view(inst_dir, meta)
        return {
            "id": instance_id,
            "name": meta.get("name", instance_id),
            "portal_url": "https://i.chaoxing.com/base",
            "courses": courses_view,
            "redo_finished": bool(meta.get("redo_finished", False)),
            "no_cache": bool(meta.get("no_cache", False)),
            "runtime_status": status_view.get("runtime_status", "stopped"),
            "failed_works_md": failed_works_md,
            "warning": warning,
        }

    def _resolve_course_context(self, instance_id: str, course_id: str) -> tuple[dict[str, Any], dict[str, str], str, str]:
        inst_dir = self._inst_dir(instance_id)
        meta_path = os.path.join(inst_dir, "meta.json")
        meta = _read_json(meta_path)
        if not meta:
            raise KeyError("未找到指定任务")

        cid = str(course_id or "").strip()
        if not re.fullmatch(r"\d{1,30}", cid):
            raise ValueError("非法的课程 ID")

        info = (meta.get("course_links") or {}).get(cid) or {}
        clazz_id = str(info.get("clazzId") or "").strip()
        cpi = str(info.get("cpi") or "").strip()
        if not (clazz_id and cpi):
            self.get_instance_portal(instance_id, refresh=False)
            meta = _read_json(meta_path) or meta
            info = (meta.get("course_links") or {}).get(cid) or {}
            clazz_id = str(info.get("clazzId") or "").strip()
            cpi = str(info.get("cpi") or "").strip()
        if not (clazz_id and cpi):
            raise ValueError("未能获取该课程的 clazzId/cpi 参数，请先点击「刷新课程链接」或重新扫码登录")

        cookies = use_cookies(os.path.join(inst_dir, "cookies.txt"))
        if not cookies:
            raise ValueError("当前任务暂无有效 Cookie，请先点击卡片「登录」重新扫码")
        return meta, cookies, clazz_id, cpi

    def get_course_chapters(self, instance_id: str, course_id: str) -> dict[str, Any]:
        meta, cookies, clazz_id, cpi = self._resolve_course_context(instance_id, course_id)
        cid = str(course_id).strip()
        session = _build_isolated_session(cookies)
        url = _build_chaoxing_course_url(cid, clazz_id, cpi)
        resp = session.get(url, timeout=12)
        if resp.status_code != 200:
            raise ValueError(f"学习通返回异常状态码: HTTP {resp.status_code}")

        final_url = str(getattr(resp, "url", "")).lower()
        if "passport2.chaoxing.com" in final_url or "<title>用户登录" in resp.text:
            raise ValueError("该任务的 Cookie 已过期，请先点击卡片「登录」重新扫码")

        catalog = decode_course_catalog(resp.text)
        titles = meta.get("course_titles") or {}
        info = (meta.get("course_links") or {}).get(cid) or {}
        unfinished_cnt = int(catalog["pending_chapters"]) + int(catalog["locked_chapters"])
        return {
            "course_id": cid,
            "title": str(titles.get(cid) or info.get("title") or f"课程 {cid}"),
            "clazzId": clazz_id,
            "cpi": cpi,
            "job_done": catalog["job_done"],
            "job_total": catalog["job_total"],
            "job_percent": catalog["job_percent"],
            "total": catalog["total_chapters"],
            "completed": catalog["completed_chapters"],
            "pending": catalog["pending_chapters"],
            "locked": catalog["locked_chapters"],
            "no_task": catalog["no_task_chapters"],
            "unfinished": unfinished_cnt,
            "pending_jobs_total": catalog["pending_jobs_total"],
            "has_locked": catalog["has_locked"],
            "units": catalog["units"],
            "points": catalog["points"],
        }

    def get_chapter_cards(self, instance_id: str, course_id: str, knowledge_id: str) -> dict[str, Any]:
        _, cookies, clazz_id, cpi = self._resolve_course_context(instance_id, course_id)
        cid = str(course_id).strip()
        kid = str(knowledge_id or "").strip()
        if not re.fullmatch(r"\d{1,30}", kid):
            raise ValueError("非法的章节 ID")

        session = _build_isolated_session(cookies)
        cards: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        not_open = False

        for num in ("0", "1", "2", "3", "4"):
            resp = session.get(
                "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards",
                params={
                    "clazzid": clazz_id,
                    "courseid": cid,
                    "knowledgeid": kid,
                    "ut": "s",
                    "cpi": cpi,
                    "num": num,
                    "mooc2": 1,
                },
                timeout=12,
            )
            if resp.status_code != 200:
                break
            final_url = str(getattr(resp, "url", "")).lower()
            if "passport2.chaoxing.com" in final_url or "<title>用户登录" in resp.text:
                raise ValueError("该任务的 Cookie 已过期，请先点击卡片「登录」重新扫码")
            if "章节未开放" in resp.text:
                not_open = True
                break

            match = re.search(r"mArg\s*=\s*(\{.*?\});\s*(?:\r?\n|$|<)", resp.text, re.S)
            if not match:
                if num != "0":
                    break
                continue
            try:
                data = json.loads(match.group(1))
            except ValueError:
                if num != "0":
                    break
                continue

            attachments = data.get("attachments") or []
            if not attachments and num != "0":
                break
            ktoken = str((data.get("defaults") or {}).get("ktoken") or "")

            for card in attachments:
                card_type = str(card.get("type") or "").lower()
                prop = card.get("property") or {}
                if not card_type and not card.get("jobid"):
                    continue
                jobid = str(card.get("jobid") or "")
                name = str(
                    prop.get("title")
                    or prop.get("name")
                    or prop.get("bookname")
                    or jobid
                    or card_type
                    or "未命名任务点"
                )
                dedup_key = (card_type, jobid, name)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                is_passed_raw = card.get("isPassed")
                is_passed = bool(is_passed_raw) if is_passed_raw is not None else None
                enc = str(card.get("enc") or "")
                view_url = ""
                if card_type == "workid" and jobid:
                    qs = urlencode(
                        {
                            "courseId": cid,
                            "clazzId": clazz_id,
                            "cpi": cpi,
                            "knowledgeId": kid,
                            "jobid": jobid,
                            "ktoken": ktoken,
                            "enc": enc,
                        }
                    )
                    view_url = f"/api/instances/{instance_id}/chaoxing-work?{qs}"

                cards.append(
                    {
                        "type": card_type or "other",
                        "name": name,
                        "jobid": jobid,
                        "is_passed": is_passed,
                        "view_url": view_url,
                    }
                )

        return {
            "course_id": cid,
            "knowledge_id": kid,
            "not_open": not_open,
            "cards": cards,
        }

    def fetch_chaoxing_work_html(self, instance_id: str, params: dict[str, str]) -> tuple[str, str]:
        inst_dir = self._inst_dir(instance_id)
        meta = _read_json(os.path.join(inst_dir, "meta.json"))
        if not meta:
            raise KeyError("未找到指定任务")

        cookies = use_cookies(os.path.join(inst_dir, "cookies.txt"))
        if not cookies:
            raise ValueError("当前任务暂无有效 Cookie，请先点击卡片「登录」重新扫码")

        course_id = str(params.get("courseId") or "").strip()
        clazz_id = str(params.get("clazzId") or "").strip()
        knowledge_id = str(params.get("knowledgeId") or "").strip()
        jobid = str(params.get("jobid") or "").strip()
        ktoken = str(params.get("ktoken") or "").strip()
        enc = str(params.get("enc") or "").strip()
        cpi = str(params.get("cpi") or "").strip()
        if not cpi:
            cpi = str(((meta.get("course_links") or {}).get(course_id) or {}).get("cpi") or "").strip()

        if not (
            re.fullmatch(r"\d{1,30}", course_id)
            and re.fullmatch(r"\d{1,30}", clazz_id)
            and re.fullmatch(r"\d{1,30}", knowledge_id)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jobid)
        ):
            raise ValueError("缺少或包含非法的测验查询参数")

        session = _build_isolated_session(cookies)
        work_id = jobid[5:] if jobid.startswith("work-") else jobid
        resp = session.get(
            "https://mooc1.chaoxing.com/mooc-ans/api/work",
            params={
                "api": "1",
                "workId": work_id,
                "jobid": jobid,
                "originJobId": jobid,
                "needRedirect": "true",
                "skipHeader": "true",
                "knowledgeid": knowledge_id,
                "ktoken": ktoken,
                "cpi": cpi,
                "ut": "s",
                "clazzId": clazz_id,
                "type": "",
                "enc": enc,
                "mooc2": "1",
                "courseid": course_id,
            },
            timeout=12,
        )
        final_url = str(getattr(resp, "url", ""))
        if "passport2.chaoxing.com" in final_url.lower() or "<title>用户登录" in resp.text:
            expired_html = (
                "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'>"
                "<body style='font-family:sans-serif;padding:32px;color:#d93025;'>"
                "<h3>该任务的 Cookie 已过期</h3>"
                "<p>请回到 SuperStar 面板点击该任务卡片上的「登录」重新扫码后再试。</p>"
                "</body></html>"
            )
            return expired_html, final_url

        html = _inject_html_base(
            resp.text,
            final_url,
            banner_text="已通过任务保存的 Cookie 免登录加载学习通测验 / 成绩原页（只读核查视图）",
        )
        return html, final_url


instance_manager = InstanceManager()
