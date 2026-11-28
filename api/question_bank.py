# -*- coding: utf-8 -*-
"""按课程划分的轻量本地 SQLite 自建题库（低内存、亚毫秒检索、支持多进程 WAL 并发）。"""

from contextlib import closing
import hashlib
import json
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


SUBJECTIVE_Q_TYPES = {"shortanswer", "completion"}

# 答案来源等级：AI(未验证) < verified(批阅确认正确) / manual(人工)；wrong 表示已知错误、不再命中
SOURCE_AI = "AI"
SOURCE_VERIFIED = "verified"
SOURCE_MANUAL = "manual"
SOURCE_WRONG = "wrong"
TRUSTED_SOURCES = (SOURCE_VERIFIED, SOURCE_MANUAL)

_TRUE_WORDS = {"正确", "对", "√", "✓", "是", "true", "t", "yes", "y", "1", "right"}
_FALSE_WORDS = {"错误", "错", "×", "✗", "x", "否", "false", "f", "no", "n", "0", "wrong", "不对", "不正确"}


def normalize_answer(answer: Any, q_type: str = "") -> str:
    """把答案统一成可比较的规范形式：选择题→排序后的大写字母，判断题→正确/错误，其他→压缩空白后的文本。"""
    text = str(answer or "").strip()
    if not text:
        return ""
    clean_type = str(q_type or "").strip()
    if clean_type in ("single", "multiple"):
        compact = re.sub(r"[\s,，、;；|/]+", "", text)
        if re.fullmatch(r"[A-Ha-h]{1,8}", compact):
            return "".join(sorted(set(compact.upper())))
        return re.sub(r"\s+", " ", text)
    if clean_type == "judgement":
        low = re.sub(r"\s+", "", text).lower()
        if low in _TRUE_WORDS:
            return "正确"
        if low in _FALSE_WORDS:
            return "错误"
        return text
    return re.sub(r"\s+", " ", text)


def _load_wrong(raw: Any) -> list[str]:
    try:
        data = json.loads(raw) if raw else []
        return [str(x) for x in data if str(x).strip()] if isinstance(data, list) else []
    except Exception:
        return []


def compute_question_hash(title: str, options: str, q_type: str) -> str:
    clean_type = str(q_type or "").strip()
    norm_title = normalize_title(title)
    norm_options = "" if clean_type in SUBJECTIVE_Q_TYPES else normalize_options(options)
    raw = f"{norm_title}||{norm_options}||{clean_type}"
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
            cols = {r[1] for r in conn.execute("PRAGMA table_info(questions)").fetchall()}
            if "wrong_answers" not in cols:
                conn.execute("ALTER TABLE questions ADD COLUMN wrong_answers TEXT NOT NULL DEFAULT ''")
            self._cleanup_dirty_subjective_rows(conn)
            conn.commit()
            self._initialized = True

    @staticmethod
    def _cleanup_dirty_subjective_rows(conn: sqlite3.Connection) -> None:
        dirty_rows = conn.execute(
            "SELECT id, course_id, q_type, title, hit_count FROM questions "
            "WHERE q_type IN ('shortanswer', 'completion') AND options != ''"
        ).fetchall()
        for row in dirty_rows:
            new_hash = compute_question_hash(row["title"], "", row["q_type"])
            dup = conn.execute(
                "SELECT id FROM questions WHERE course_id = ? AND q_hash = ? AND id != ? LIMIT 1",
                (row["course_id"], new_hash, row["id"]),
            ).fetchone()
            if dup:
                conn.execute(
                    "UPDATE questions SET hit_count = hit_count + ? WHERE id = ?",
                    (int(row["hit_count"] or 0), dup["id"]),
                )
                conn.execute("DELETE FROM questions WHERE id = ?", (row["id"],))
            else:
                conn.execute(
                    "UPDATE questions SET options = '', q_hash = ? WHERE id = ?",
                    (new_hash, row["id"]),
                )

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
                "SELECT id, answer, source FROM questions WHERE course_id = ? AND q_hash = ? LIMIT 1",
                (cid, q_hash),
            ).fetchone()
            if not row or row["source"] == SOURCE_WRONG or not str(row["answer"] or "").strip():
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
        clean_type = str(q_type or "").strip()
        clean_title = normalize_title(title)
        eff_options = "" if clean_type in SUBJECTIVE_Q_TYPES else str(options or "")
        q_hash = compute_question_hash(title, eff_options, clean_type)
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
                    answer = CASE
                        WHEN questions.source IN ('verified', 'manual') AND excluded.source = 'AI'
                            THEN questions.answer
                        ELSE excluded.answer
                    END,
                    course_title = CASE
                        WHEN excluded.course_title != '' THEN excluded.course_title
                        ELSE questions.course_title
                    END,
                    source = CASE
                        WHEN questions.source IN ('verified', 'manual') AND excluded.source = 'AI'
                            THEN questions.source
                        ELSE excluded.source
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    cid,
                    ctitle,
                    q_hash,
                    clean_type,
                    clean_title,
                    eff_options,
                    str(answer).strip(),
                    source,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _get_row(self, conn: sqlite3.Connection, cid: str, q_hash: str):
        return conn.execute(
            "SELECT id, answer, source, wrong_answers FROM questions WHERE course_id = ? AND q_hash = ? LIMIT 1",
            (cid, q_hash),
        ).fetchone()

    def get_wrong_answers(self, course_id: str, title: str, options: str = "", q_type: str = "") -> list[str]:
        """返回该题已被批阅判定为错误的答案列表（规范化形式）。"""
        self._ensure_schema()
        cid = str(course_id or "default").strip() or "default"
        clean_type = str(q_type or "").strip()
        eff_options = "" if clean_type in SUBJECTIVE_Q_TYPES else str(options or "")
        q_hash = compute_question_hash(title, eff_options, clean_type)
        with self._lock:
            row = self._get_row(self._get_conn(), cid, q_hash)
        return _load_wrong(row["wrong_answers"]) if row else []

    def record_review(
        self,
        course_id: str,
        course_title: str,
        title: str,
        options: str,
        q_type: str,
        correct_answer: str = "",
        wrong_answer: str = "",
    ) -> str:
        """根据批阅结果更新题库。

        - correct_answer 非空：写入为 verified（覆盖 AI / manual / wrong）。
        - 只有 wrong_answer：把它加入错误列表；若当前存储答案就是它，则标记为 wrong（不再命中）。
        返回动作：verified / corrected / wrong / noop。
        """
        self._ensure_schema()
        cid = str(course_id or "default").strip() or "default"
        ctitle = str(course_title or "").strip()
        clean_type = str(q_type or "").strip()
        clean_title = normalize_title(title)
        eff_options = "" if clean_type in SUBJECTIVE_Q_TYPES else str(options or "")
        q_hash = compute_question_hash(title, eff_options, clean_type)
        correct = normalize_answer(correct_answer, clean_type)
        wrong = normalize_answer(wrong_answer, clean_type)
        if correct and wrong and correct == wrong:
            wrong = ""
        if not correct and not wrong:
            return "noop"
        now = int(time.time())
        with self._lock:
            conn = self._get_conn()
            row = self._get_row(conn, cid, q_hash)
            wrong_list = _load_wrong(row["wrong_answers"]) if row else []
            if wrong and wrong not in wrong_list:
                wrong_list.append(wrong)
            if correct:
                wrong_list = [w for w in wrong_list if w != correct]
            wrong_json = json.dumps(wrong_list[-20:], ensure_ascii=False)

            if correct:
                old_norm = normalize_answer(row["answer"], clean_type) if row else ""
                action = "verified" if (row and old_norm == correct) else "corrected"
                new_answer, new_source = correct, SOURCE_VERIFIED
            else:
                old_answer = str(row["answer"]) if row else ""
                old_source = str(row["source"]) if row else ""
                if row and normalize_answer(old_answer, clean_type) == wrong:
                    new_answer, new_source, action = old_answer, SOURCE_WRONG, "wrong"
                elif row:
                    # 存储答案与这次错误答案不同（比如这次是随机作答），保留原答案，仅记录错误项
                    new_answer, new_source, action = old_answer, old_source, "wrong"
                else:
                    new_answer, new_source, action = wrong, SOURCE_WRONG, "wrong"

            if row:
                conn.execute(
                    "UPDATE questions SET answer = ?, source = ?, wrong_answers = ?, updated_at = ?, "
                    "course_title = CASE WHEN ? != '' THEN ? ELSE course_title END WHERE id = ?",
                    (new_answer, new_source, wrong_json, now, ctitle, ctitle, row["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO questions (
                        course_id, course_title, q_hash, q_type, title, options,
                        answer, source, hit_count, created_at, updated_at, wrong_answers
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (cid, ctitle, q_hash, clean_type, clean_title, eff_options,
                     new_answer, new_source, now, now, wrong_json),
                )
            conn.commit()
            return action

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
                    SUM(CASE WHEN source IN ('verified', 'manual') THEN 1 ELSE 0 END) AS verified_count,
                    SUM(CASE WHEN source = 'wrong' THEN 1 ELSE 0 END) AS wrong_count,
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
                    "verified_count": int(r["verified_count"] or 0),
                    "wrong_count": int(r["wrong_count"] or 0),
                    "total_hits": int(r["total_hits"]),
                    "updated_at": int(r["updated_at"]),
                }
                for r in rows
            ]

    def list_questions(
        self,
        course_id: str | None = None,
        query: str = "",
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._ensure_schema()
        clauses: list[str] = []
        params: list[Any] = []
        if course_id:
            clauses.append("course_id = ?")
            params.append(str(course_id).strip())
        if source and str(source).strip():
            clauses.append("source = ?")
            params.append(str(source).strip())
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
                       source, hit_count, created_at, updated_at, wrong_answers
                FROM questions
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            items = []
            for r in rows:
                item = dict(r)
                item["wrong_answers"] = _load_wrong(item.get("wrong_answers"))
                items.append(item)
        return {"items": items, "total": total}

    def update_answer(self, question_id: int, answer: str) -> bool:
        self._ensure_schema()
        clean = str(answer or "").strip()
        if not clean:
            raise ValueError("答案不能为空")
        now = int(time.time())
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT id, q_type, wrong_answers FROM questions WHERE id = ? LIMIT 1",
                (int(question_id),),
            ).fetchone()
            if not row:
                return False
            q_type = str(row["q_type"] or "")
            norm_clean = normalize_answer(clean, q_type)
            wrong_list = [w for w in _load_wrong(row["wrong_answers"]) if w != norm_clean]
            wrong_json = json.dumps(wrong_list, ensure_ascii=False)
            cur = conn.execute(
                "UPDATE questions SET answer = ?, source = 'manual', wrong_answers = ?, updated_at = ? WHERE id = ?",
                (clean, wrong_json, now, int(question_id)),
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
