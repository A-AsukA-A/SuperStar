# -*- coding: utf-8 -*-
"""多账号 Cookie 会话存储（每个账号独立目录与 cookies.txt，不向前端返回明文 Cookie）。"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from typing import Any

from api.cookies import save_cookies_dict, use_cookies

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_DIR = os.path.join(ROOT_DIR, "data", "sessions")


class SessionStore:
    """线程安全的多 Cookie 会话管理器。"""

    def __init__(self, sessions_dir: str = SESSIONS_DIR) -> None:
        self.sessions_dir = sessions_dir
        self._lock = threading.RLock()
        os.makedirs(self.sessions_dir, exist_ok=True)

    def _session_dir(self, session_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", session_id or ""):
            raise ValueError("非法的会话 ID")
        return os.path.join(self.sessions_dir, session_id)

    @staticmethod
    def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="sess_", suffix=".tmp", dir=parent)
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

    @staticmethod
    def _read_json(path: str) -> dict[str, Any] | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def save_session(
        self,
        cookies: dict[str, str],
        uid: str | None = None,
        nickname: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        if not cookies:
            raise ValueError("Cookie 不能为空")

        clean_uid = re.sub(r"[^A-Za-z0-9_-]", "", str(uid or "").strip())
        session_id = f"u{clean_uid}" if clean_uid else uuid.uuid4().hex[:8]
        s_dir = self._session_dir(session_id)
        os.makedirs(s_dir, exist_ok=True)

        cookie_path = os.path.join(s_dir, "cookies.txt")
        save_cookies_dict(cookies, path=cookie_path)

        with self._lock:
            meta_path = os.path.join(s_dir, "meta.json")
            existing = self._read_json(meta_path) or {}
            default_uid_label = f"UID {clean_uid}" if clean_uid else session_id
            clean_label = str(label or "").strip()
            clean_nick = str(nickname or existing.get("nickname") or "").strip() or None

            if clean_label:
                display_label = clean_label
                has_custom_label = clean_label != default_uid_label
            elif existing.get("has_custom_label") or (
                existing.get("label") and str(existing.get("label")).strip() != default_uid_label
            ):
                display_label = str(existing["label"]).strip()
                has_custom_label = True
            elif clean_nick and clean_nick != default_uid_label:
                display_label = clean_nick
                has_custom_label = True
            else:
                display_label = default_uid_label
                has_custom_label = False

            meta: dict[str, Any] = {
                "id": session_id,
                "uid": clean_uid or None,
                "nickname": clean_nick,
                "label": display_label,
                "has_custom_label": has_custom_label,
                "cookie_count": len(cookies),
                "updated_at": int(time.time()),
            }
            self._atomic_write_json(meta_path, meta)
        return dict(meta)

    def update_session_label(self, session_id: str, label: str) -> dict[str, Any]:
        clean_label = str(label or "").strip()
        if not clean_label:
            raise ValueError("账户备注不能为空")
        if len(clean_label) > 40:
            raise ValueError("账户备注长度不能超过 40 个字符")

        with self._lock:
            s_dir = self._session_dir(session_id)
            meta_path = os.path.join(s_dir, "meta.json")
            meta = self._read_json(meta_path)
            if not meta:
                raise KeyError("未找到该账号会话")
            meta["label"] = clean_label
            meta["has_custom_label"] = True
            meta["updated_at"] = int(time.time())
            self._atomic_write_json(meta_path, meta)
            return dict(meta)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            if not os.path.isdir(self.sessions_dir):
                return []
            items: list[dict[str, Any]] = []
            for name in os.listdir(self.sessions_dir):
                s_dir = os.path.join(self.sessions_dir, name)
                if not os.path.isdir(s_dir):
                    continue
                meta = self._read_json(os.path.join(s_dir, "meta.json"))
                cookie_file = os.path.join(s_dir, "cookies.txt")
                if meta and os.path.exists(cookie_file):
                    items.append(meta)
            items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
            return items

    def get_session_meta(self, session_id: str) -> dict[str, Any] | None:
        s_dir = self._session_dir(session_id)
        return self._read_json(os.path.join(s_dir, "meta.json"))

    def get_session_cookies(self, session_id: str) -> dict[str, str]:
        s_dir = self._session_dir(session_id)
        cookie_file = os.path.join(s_dir, "cookies.txt")
        if not os.path.exists(cookie_file):
            return {}
        return use_cookies(cookie_file)

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            s_dir = self._session_dir(session_id)
            if not os.path.isdir(s_dir):
                return False
            shutil.rmtree(s_dir, ignore_errors=True)
            return True


session_store = SessionStore()
