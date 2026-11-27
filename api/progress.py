# -*- coding: utf-8 -*-
"""结构化任务进度记录器，供 Web 控制台实时读取百分比、当前课程/章节/任务点与状态。"""

import json
import os
import tempfile
import threading
import time
from typing import Any


def format_sec(seconds: int | float) -> str:
    sec_int = max(0, int(seconds))
    mins, secs = divmod(sec_int, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


class ProgressReporter:
    """线程安全的节流原子进度写入器。当 status_file 为空时为无开销空操作。"""

    def __init__(self, status_file: str | None = None, write_interval: float = 2.0) -> None:
        self.status_file = status_file
        self.write_interval = write_interval
        self._lock = threading.RLock()
        self._last_write_ts = 0.0
        now = int(time.time())
        self._state: dict[str, Any] = {
            "status": "starting",
            "message": "正在初始化任务...",
            "overall_percent": 0.0,
            "courses_total": 0,
            "courses_completed": 0,
            "current_course": None,
            "chapters_total": 0,
            "chapters_completed": 0,
            "chapters_failed": 0,
            "current_chapter": "",
            "current_job": None,
            "started_at": now,
            "updated_at": now,
        }
        if self.status_file:
            self._flush(force=True)

    @property
    def enabled(self) -> bool:
        return bool(self.status_file)

    def _recompute_percent(self) -> None:
        status = self._state["status"]
        if status == "completed":
            self._state["overall_percent"] = 100.0
            return

        c_total = int(self._state.get("courses_total") or 0)
        c_done = int(self._state.get("courses_completed") or 0)
        if c_total <= 0:
            self._state["overall_percent"] = 0.0
            return

        ch_total = int(self._state.get("chapters_total") or 0)
        ch_done = int(self._state.get("chapters_completed") or 0)
        ch_frac = 0.0
        if ch_total > 0:
            job = self._state.get("current_job") or {}
            job_pct = float(job.get("percent") or 0.0) / 100.0
            job_contrib = (min(1.0, max(0.0, job_pct)) / ch_total) if ch_done < ch_total else 0.0
            ch_frac = min(1.0, (ch_done / ch_total) + job_contrib)

        raw_pct = ((c_done + ch_frac) / c_total) * 100.0
        if status == "running":
            raw_pct = min(99.9, raw_pct)
        self._state["overall_percent"] = round(max(0.0, min(100.0, raw_pct)), 1)

    def _flush(self, force: bool = False) -> None:
        if not self.status_file:
            return
        now = time.time()
        if not force and (now - self._last_write_ts) < self.write_interval:
            return

        self._state["updated_at"] = int(now)
        self._recompute_percent()
        payload = dict(self._state)

        parent = os.path.dirname(os.path.abspath(self.status_file))
        if parent:
            os.makedirs(parent, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(prefix="progress_", suffix=".tmp", dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
                fp.flush()
            os.replace(tmp_path, self.status_file)
            self._last_write_ts = now
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def set_status(self, status: str, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state["status"] = status
            self._state["message"] = message
            self._flush(force=True)

    def set_courses(self, courses: list[dict]) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state["status"] = "running"
            self._state["courses_total"] = len(courses)
            self._state["courses_completed"] = 0
            self._state["message"] = f"已加载 {len(courses)} 门待学习课程"
            self._flush(force=True)

    def start_course(
        self,
        course: dict,
        index: int,
        total_chapters: int,
        initial_completed: int = 0,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            total = max(0, int(total_chapters))
            done = max(0, min(int(initial_completed), total))
            self._state["status"] = "running"
            self._state["current_course"] = {
                "id": str(course.get("courseId", "")),
                "title": str(course.get("title", "")),
                "index": index,
            }
            self._state["chapters_total"] = total
            self._state["chapters_completed"] = done
            self._state["chapters_failed"] = 0
            self._state["current_chapter"] = ""
            self._state["current_job"] = None
            pending = total - done
            if done > 0:
                self._state["message"] = (
                    f"课程 {index}/{self._state['courses_total']} ({course.get('title', '')})："
                    f"已完成 {done} 章，待处理 {pending} 章"
                )
            else:
                self._state["message"] = f"正在学习课程 {index}/{self._state['courses_total']}: {course.get('title', '')}"
            self._flush(force=True)

    def chapter_started(self, title: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state["current_chapter"] = str(title or "")
            self._state["current_job"] = None
            self._state["message"] = f"正在处理章节: {title}"
            self._flush(force=True)

    def chapter_finished(self, title: str, success: bool = True, skipped: bool = False) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state["chapters_completed"] = int(self._state.get("chapters_completed") or 0) + 1
            if not success and not skipped:
                self._state["chapters_failed"] = int(self._state.get("chapters_failed") or 0) + 1
            self._state["current_job"] = None
            status_word = "已跳过" if skipped else ("已完成" if success else "失败")
            self._state["message"] = f"章节{status_word}: {title}"
            self._flush(force=True)

    def update_job(
        self,
        job_type: str,
        name: str,
        current: int | float,
        total: int | float,
        detail: str | None = None,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            pct = 0.0
            if total and total > 0:
                pct = round(max(0.0, min(100.0, (float(current) / float(total)) * 100.0)), 1)
            self._state["current_job"] = {
                "type": job_type,
                "name": str(name or ""),
                "percent": pct,
                "detail": detail or f"{current}/{total}",
            }
            self._flush(force=force)

    def finish_course(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state["courses_completed"] = int(self._state.get("courses_completed") or 0) + 1
            self._state["current_job"] = None
            self._flush(force=True)

    def mark_completed(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            c_total = int(self._state.get("courses_total") or 0)
            self._state["courses_completed"] = c_total
            self._state["status"] = "completed"
            self._state["current_job"] = None
            self._state["message"] = "所有课程学习任务已完成"
            self._flush(force=True)

    def mark_stopped(self, message: str = "任务已停止") -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._state.get("status") == "completed":
                return
            self._state["status"] = "stopped"
            self._state["message"] = message
            self._flush(force=True)

    def mark_error(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state["status"] = "error"
            self._state["message"] = str(message)
            self._flush(force=True)
