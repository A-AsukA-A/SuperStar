# -*- coding: utf-8 -*-
"""独立轻量补救/重做排队答题子进程：按队列顺序处理指定章节或测验，跑完即退出释放内存。"""

import argparse
import configparser
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
from typing import Any

from api.answer import Tiku
from api.base import Account, Chaoxing, SessionManager, StudyResult
from api.decode import _extract_job_info
from api.logger import logger


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="remedy_", suffix=".tmp", dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.flush()
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _load_tiku_section(config_path: str) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    if config_path and os.path.exists(config_path):
        parser.read(config_path, encoding="utf8")
    if parser.has_section("tiku"):
        return dict(parser.items("tiku"))
    return {}


def _extract_work_cards_from_chapter(
    course_id: str,
    clazz_id: str,
    cpi: str,
    knowledge_id: str,
) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    """拉取章节全部测验卡片（不跳过 isPassed=True，以便支持已通过/未通过重做检测）。"""
    session = SessionManager.get_session()
    work_jobs: list[dict[str, Any]] = []
    seen_jobids: set[str] = set()
    job_info: dict[str, Any] = {}
    not_open = False

    for num in ("0", "1", "2", "3", "4"):
        resp = session.get(
            "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards",
            params={
                "clazzid": clazz_id,
                "courseid": course_id,
                "knowledgeid": knowledge_id,
                "ut": "s",
                "cpi": cpi,
                "num": num,
                "mooc2": 1,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            break
        if "章节未开放" in resp.text:
            not_open = True
            break

        match = re.search(r"mArg\s*=\s*(\{.*?\});\s*(?:\r?\n|$|<)", resp.text, re.S)
        if not match:
            if num != "0":
                break
            continue
        try:
            cards_data = json.loads(match.group(1))
        except ValueError:
            if num != "0":
                break
            continue

        extracted = _extract_job_info(cards_data)
        if extracted:
            job_info.update(extracted)

        attachments = cards_data.get("attachments") or []
        if not attachments and num != "0":
            break

        for card in attachments:
            card_type = str(card.get("type") or "").lower()
            if card_type != "workid":
                continue
            jobid = str(card.get("jobid") or "").strip()
            if not jobid or jobid in seen_jobids:
                continue
            seen_jobids.add(jobid)
            other_raw = str(card.get("otherInfo") or "")
            prop = card.get("property") or {}
            work_jobs.append(
                {
                    "type": "workid",
                    "jobid": jobid,
                    "name": str(prop.get("title") or prop.get("name") or jobid),
                    "otherinfo": other_raw.split("&")[0],
                    "mid": card.get("mid", ""),
                    "enc": card.get("enc", ""),
                    "aid": card.get("aid", ""),
                }
            )

    if not job_info.get("knowledgeid"):
        job_info["knowledgeid"] = knowledge_id
    if not job_info.get("cpi"):
        job_info["cpi"] = cpi
    return not_open, work_jobs, job_info


class _RemedyProgressBridge:
    """将 study_work 的逐题回调实时同步到当前队列项的进度显示。"""

    def __init__(self, queue_file: str, queue_data: dict[str, Any], lock: threading.Lock) -> None:
        self.queue_file = queue_file
        self.queue_data = queue_data
        self.lock = lock
        self.current_item: dict[str, Any] | None = None
        self._last_flush = 0.0

    def update_job(
        self,
        job_type: str,
        name: str,
        current: int | float,
        total: int | float,
        detail: str | None = None,
        force: bool = False,
        job_key: str | None = None,
        status_text: str | None = None,
    ) -> None:
        if self.current_item is None or job_type != "work":
            return
        now = time.time()
        if not force and (now - self._last_flush) < 1.0:
            return
        with self.lock:
            status_word = status_text or "答题中"
            self.current_item["message"] = f"{status_word}：{name} ({detail or f'{current}/{total}'})"
            self.queue_data["updated_at"] = int(now)
            _atomic_write_json(self.queue_file, self.queue_data)
            self._last_flush = now

    def finish_job(self, job_key: str) -> None:
        return


def run_remedy_queue(
    queue_file: str,
    cookies_path: str,
    config_path: str,
    no_cache: bool = False,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    with open(queue_file, "r", encoding="utf8") as fp:
        queue_data: dict[str, Any] = json.load(fp)

    os.environ["CHAOXING_COOKIES_PATH"] = cookies_path
    os.environ["CHAOXING_CONFIG_PATH"] = config_path

    if stop_event is None:
        stop_event = threading.Event()

    tiku_config = _load_tiku_section(config_path)
    tiku = Tiku()
    tiku.config_set(tiku_config)
    tiku = tiku.get_tiku_from_config()
    tiku.init_tiku()
    if no_cache or queue_data.get("no_cache"):
        tiku.USE_CACHE = False
        logger.warning("补救队列已启用 --no-cache：本次忽略缓存并重新请求 AI 答题")

    SessionManager.update_cookies()

    delay_raw = tiku_config.get("delay", 0)
    try:
        query_delay = float(delay_raw or 0)
    except (TypeError, ValueError):
        query_delay = 0.0

    write_lock = threading.Lock()
    bridge = _RemedyProgressBridge(queue_file, queue_data, write_lock)

    chaoxing = Chaoxing(account=Account("", ""), tiku=tiku, query_delay=query_delay)
    chaoxing.force_redo = True
    chaoxing.progress_reporter = bridge

    items: list[dict[str, Any]] = queue_data.get("items") or []
    queue_data["status"] = "running"
    queue_data["total"] = len(items)
    queue_data["updated_at"] = int(time.time())
    _atomic_write_json(queue_file, queue_data)

    for idx, item in enumerate(items):
        if stop_event.is_set():
            break
        if item.get("status") in ("done", "skipped"):
            continue

        item["status"] = "running"
        item["message"] = "正在拉取章节测验..."
        item["started_at"] = int(time.time())
        queue_data["current_index"] = idx
        queue_data["updated_at"] = int(time.time())
        with write_lock:
            bridge.current_item = item
            _atomic_write_json(queue_file, queue_data)

        cid = str(item.get("course_id") or "")
        clazz_id = str(item.get("clazz_id") or "")
        cpi = str(item.get("cpi") or "")
        kid = str(item.get("knowledge_id") or "")
        target_jobid = str(item.get("jobid") or "").strip()

        try:
            not_open, work_jobs, job_info = _extract_work_cards_from_chapter(cid, clazz_id, cpi, kid)
            if not_open:
                item["status"] = "skipped"
                item["message"] = "该章节尚未开放"
            else:
                if target_jobid:
                    work_jobs = [w for w in work_jobs if w["jobid"] == target_jobid]
                if not work_jobs:
                    item["status"] = "skipped"
                    item["message"] = "未找到匹配的测验任务点" if target_jobid else "该章节无测验任务点"
                else:
                    course_obj = {
                        "courseId": cid,
                        "clazzId": clazz_id,
                        "cpi": cpi,
                        "title": str(item.get("course_title") or ""),
                    }
                    messages: list[str] = []
                    had_error = False
                    had_done = False
                    for w_job in work_jobs:
                        if stop_event.is_set():
                            break
                        chaoxing.last_work_notes.pop(w_job["jobid"], None)
                        res = chaoxing.study_work(course_obj, w_job, job_info)
                        note = chaoxing.last_work_notes.get(w_job["jobid"]) or {}
                        code = note.get("code", "")
                        msg = note.get("message") or ("处理完成" if res == StudyResult.SUCCESS else "答题失败")
                        messages.append(f"{w_job['name']}：{msg}")
                        if res != StudyResult.SUCCESS or code in ("error",):
                            had_error = True
                        elif code in ("submitted", "saved", "failed_grade"):
                            had_done = True

                    if stop_event.is_set():
                        item["status"] = "stopped"
                        item["message"] = "已被用户停止"
                    elif had_error and not had_done:
                        item["status"] = "failed"
                        item["message"] = "；".join(messages)
                    elif not had_done and not had_error:
                        item["status"] = "skipped"
                        item["message"] = "；".join(messages)
                    else:
                        item["status"] = "done"
                        item["message"] = "；".join(messages)
        except Exception as exc:
            logger.error(f"补救队列处理章节 {kid} 异常: {exc}")
            item["status"] = "failed"
            item["message"] = f"执行异常：{exc}"
        finally:
            item["finished_at"] = int(time.time())
            with write_lock:
                bridge.current_item = None
                queue_data["done_count"] = sum(1 for x in items if x.get("status") == "done")
                queue_data["skipped_count"] = sum(1 for x in items if x.get("status") == "skipped")
                queue_data["failed_count"] = sum(1 for x in items if x.get("status") == "failed")
                queue_data["updated_at"] = int(time.time())
                _atomic_write_json(queue_file, queue_data)

    if stop_event.is_set():
        for item in items:
            if item.get("status") in ("pending", "running"):
                item["status"] = "stopped"
                item["message"] = "已手动停止"
        queue_data["status"] = "stopped"
    else:
        queue_data["status"] = "completed"
    queue_data["updated_at"] = int(time.time())
    with write_lock:
        _atomic_write_json(queue_file, queue_data)
    return queue_data


def main() -> None:
    parser = argparse.ArgumentParser(description="SuperStar 手动补救/重做测验队列执行器")
    parser.add_argument("--queue-file", required=True)
    parser.add_argument("--cookies-path", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    stop_event = threading.Event()

    def _on_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    run_remedy_queue(
        queue_file=args.queue_file,
        cookies_path=args.cookies_path,
        config_path=args.config_path,
        no_cache=bool(args.no_cache),
        stop_event=stop_event,
    )


if __name__ == "__main__":
    main()
