# -*- coding: utf-8 -*-
"""按课程划分的轻量本地 SQLite 自建题库（低内存、亚毫秒检索、支持多进程 WAL 并发）。"""

from contextlib import closing
import hashlib
import os
import re
import sqlite3
import threading
import time
from typing import Any, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BANK_PATH = os.path.join(ROOT_DIR, "data", "question_bank.sqlite3")


def get_bank_path(path: str | None = None) -> str:
    return path or os.environ.get("CHAOXING_BANK_PATH") or DEFAULT_BANK_PATH


def normalize_title(title: str) -> str:
    text = str(title or "").strip()
    text = re.sub(r"^\d+", "", text)
    text = re.sub(r"（\d+(?:\.\d+)?分）$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_options(options: str) -> str:
    text = str(options or "").strip()
    return re.sub(r"\s+", " ", text)


def compute_question_hash(title: str, options: str, q_type: str) -> str:
    norm_title = normalize_title(title)
    norm_options = normalize_options(options)
    raw = f"{norm_title}||{norm_options}||{str(q_type or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class QuestionBankDAO:
    """按课程 ID 分区的高速本地 SQLite 题库（复用连接，亚毫秒检索）。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = get_bank_path(db_path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA cache_size=-2000;")
        self._local.conn = conn
        with self._lock:
            self._conns.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
        self._local.conn = None

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            conn = self._get_conn()
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id TEXT NOT NULL,
                    course_title TEXT NOT NULL DEFAULT '',
                    q_hash TEXT NOT NULL,
                    q_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    options TEXT NOT NULL DEFAULT '',
                    answer TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'AI',
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(course_id, q_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_questions_course_updated
                    ON questions(course_id, updated_at DESC);
                """
            )
            conn.commit()
            self._initialized = True

    def lookup(
        self,
        course_id: str,
        title: str,
        options: str = "",
        q_type: str = "",
    ) -> Optional[str]:
        self._ensure_schema()
        cid = str(course_id or "default").strip() or "default"
        q_hash = compute_question_hash(title, options, q_type)
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT id, answer FROM questions WHERE course_id = ? AND q_hash = ? LIMIT 1",
                (cid, q_hash),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE questions SET hit_count = hit_count + 1 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            return str(row["answer"])

    def save(
        self,
        course_id: str,
        course_title: str,
        title: str,
        options: str,
        q_type: str,
        answer: str,
        source: str = "AI",
    ) -> None:
        self._ensure_schema()
        cid = str(course_id or "default").strip() or "default"
        ctitle = str(course_title or "").strip()
        clean_title = normalize_title(title)
        q_hash = compute_question_hash(title, options, q_type)
        now = int(time.time())
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO questions (
                    course_id, course_title, q_hash, q_type, title, options,
                    answer, source, hit_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(course_id, q_hash) DO UPDATE SET
                    answer = excluded.answer,
                    course_title = CASE
                        WHEN excluded.course_title != '' THEN excluded.course_title
                        ELSE questions.course_title
                    END,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    cid,
                    ctitle,
                    q_hash,
                    str(q_type or ""),
                    clean_title,
                    str(options or ""),
                    str(answer).strip(),
                    source,
                    now,
                    now,
                ),
            )
            conn.commit()

    def list_courses(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """
                SELECT
                    course_id,
                    MAX(course_title) AS course_title,
                    COUNT(*) AS question_count,
                    COALESCE(SUM(hit_count), 0) AS total_hits,
                    MAX(updated_at) AS updated_at
                FROM questions
                GROUP BY course_id
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [
                {
                    "course_id": r["course_id"],
                    "course_title": r["course_title"] or f"课程 {r['course_id']}",
                    "question_count": int(r["question_count"]),
                    "total_hits": int(r["total_hits"]),
                    "updated_at": int(r["updated_at"]),
                }
                for r in rows
            ]

    def list_questions(
        self,
        course_id: str | None = None,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._ensure_schema()
        clauses: list[str] = []
        params: list[Any] = []
        if course_id:
            clauses.append("course_id = ?")
            params.append(str(course_id).strip())
        if query and query.strip():
            like = f"%{query.strip()}%"
            clauses.append("(title LIKE ? OR answer LIKE ?)")
            params.extend([like, like])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))

        with self._lock:
            conn = self._get_conn()
            total = int(
                conn.execute(f"SELECT COUNT(*) AS cnt FROM questions {where}", params).fetchone()["cnt"]
            )
            rows = conn.execute(
                f"""
                SELECT id, course_id, course_title, q_type, title, options, answer,
                       source, hit_count, created_at, updated_at
                FROM questions
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            items = [dict(r) for r in rows]
        return {"items": items, "total": total}

    def update_answer(self, question_id: int, answer: str) -> bool:
        self._ensure_schema()
        clean = str(answer or "").strip()
        if not clean:
            raise ValueError("答案不能为空")
        now = int(time.time())
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE questions SET answer = ?, source = 'manual', updated_at = ? WHERE id = ?",
                (clean, now, int(question_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_question(self, question_id: int) -> bool:
        self._ensure_schema()
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM questions WHERE id = ?", (int(question_id),))
            conn.commit()
            return cur.rowcount > 0

    def delete_course(self, course_id: str) -> int:
        self._ensure_schema()
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM questions WHERE course_id = ?", (str(course_id).strip(),))
            conn.commit()
            return int(cur.rowcount)
