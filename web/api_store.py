# -*- coding: utf-8 -*-
"""AI 答题 API 配置持久化与连通性检测。"""

import json
import os
import tempfile
import threading
import time
import uuid
from typing import Any

from api.answer import AI

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
PROFILES_PATH = os.path.join(DATA_DIR, "api_profiles.json")


def mask_key(key: str) -> str:
    """对 API Key 脱敏，前端仅展示前后缀。"""
    raw = str(key or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{raw[:4]}...{raw[-4:]}"


def _is_masked_placeholder(val: str) -> bool:
    s = str(val or "").strip()
    return "..." in s and len(s) >= 7


def _to_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


class AIProfileStore:
    """管理多个 OpenAI 兼容（如 DeepSeek）答题 API 配置。"""

    def __init__(self, file_path: str = PROFILES_PATH) -> None:
        self.file_path = file_path
        self._lock = threading.RLock()

    def _read_all(self) -> list[dict]:
        with self._lock:
            if not os.path.exists(self.file_path):
                return []
            try:
                with open(self.file_path, "r", encoding="utf8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
            except (OSError, ValueError):
                return []

    def _write_all(self, profiles: list[dict]) -> None:
        with self._lock:
            parent = os.path.dirname(os.path.abspath(self.file_path))
            os.makedirs(parent, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix="api_profiles_", suffix=".tmp", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf8") as f:
                    json.dump(profiles, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.file_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise

    @staticmethod
    def _public_view(item: dict) -> dict:
        out = dict(item)
        raw_key = out.pop("key", "")
        out["key_masked"] = mask_key(raw_key)
        out["has_key"] = bool(raw_key)
        return out

    @staticmethod
    def _normalize_payload(payload: dict, existing: dict | None = None) -> dict:
        name = str(payload.get("name") or (existing or {}).get("name") or "").strip()
        endpoint = str(payload.get("endpoint") or (existing or {}).get("endpoint") or "").strip()
        model = str(payload.get("model") or (existing or {}).get("model") or "").strip()
        submitted_key = str(payload.get("key") or "").strip()

        if existing and (not submitted_key or _is_masked_placeholder(submitted_key)):
            key = str(existing.get("key") or "")
        else:
            key = submitted_key

        if not name:
            raise ValueError("API 配置名称不能为空")
        if not endpoint or not (endpoint.startswith("http://") or endpoint.startswith("https://")):
            raise ValueError("Endpoint 必须以 http:// 或 https:// 开头")
        if not model:
            raise ValueError("模型名称（model）不能为空")
        if not key:
            raise ValueError("API Key 不能为空")

        try:
            min_interval = max(0, int(payload.get("min_interval_seconds", (existing or {}).get("min_interval_seconds", 3))))
        except (TypeError, ValueError) as exc:
            raise ValueError("min_interval_seconds 必须是整数") from exc

        try:
            delay = max(0.0, float(payload.get("delay", (existing or {}).get("delay", 1.0))))
        except (TypeError, ValueError) as exc:
            raise ValueError("delay 必须是数字") from exc

        try:
            cover_rate = float(payload.get("cover_rate", (existing or {}).get("cover_rate", 0.9)))
            cover_rate = min(1.0, max(0.0, cover_rate))
        except (TypeError, ValueError) as exc:
            raise ValueError("cover_rate 必须是 0~1 之间的数字") from exc

        return {
            "id": existing["id"] if existing else uuid.uuid4().hex[:8],
            "name": name,
            "endpoint": endpoint,
            "key": key,
            "model": model,
            "min_interval_seconds": min_interval,
            "delay": delay,
            "submit": _to_bool(payload.get("submit"), (existing or {}).get("submit", True)),
            "cover_rate": cover_rate,
            "use_cache": _to_bool(payload.get("use_cache"), (existing or {}).get("use_cache", True)),
            "http_proxy": str(payload.get("http_proxy", (existing or {}).get("http_proxy", "")) or "").strip(),
            "is_default": bool(payload.get("is_default", (existing or {}).get("is_default", False))),
            "created_at": existing["created_at"] if existing else int(time.time()),
        }

    def list_profiles(self, masked: bool = True) -> list[dict]:
        items = self._read_all()
        if masked:
            return [self._public_view(item) for item in items]
        return items

    def get_profile(self, profile_id: str, masked: bool = True) -> dict | None:
        for item in self._read_all():
            if item.get("id") == profile_id:
                return self._public_view(item) if masked else item
        return None

    def add_profile(self, payload: dict) -> dict:
        with self._lock:
            items = self._read_all()
            entry = self._normalize_payload(payload)
            if not items or entry["is_default"]:
                for item in items:
                    item["is_default"] = False
                entry["is_default"] = True
            items.append(entry)
            self._write_all(items)
            return self._public_view(entry)

    def update_profile(self, profile_id: str, payload: dict) -> dict:
        with self._lock:
            items = self._read_all()
            idx = next((i for i, x in enumerate(items) if x.get("id") == profile_id), None)
            if idx is None:
                raise KeyError("未找到指定 API 配置")
            updated = self._normalize_payload(payload, existing=items[idx])
            if updated["is_default"]:
                for item in items:
                    item["is_default"] = False
            items[idx] = updated
            if not any(x.get("is_default") for x in items):
                items[0]["is_default"] = True
            self._write_all(items)
            return self._public_view(updated)

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            items = self._read_all()
            new_items = [x for x in items if x.get("id") != profile_id]
            if len(new_items) == len(items):
                raise KeyError("未找到指定 API 配置")
            if new_items and not any(x.get("is_default") for x in new_items):
                new_items[0]["is_default"] = True
            self._write_all(new_items)

    def set_default(self, profile_id: str) -> dict:
        with self._lock:
            items = self._read_all()
            target = None
            for item in items:
                is_target = item.get("id") == profile_id
                item["is_default"] = is_target
                if is_target:
                    target = item
            if target is None:
                raise KeyError("未找到指定 API 配置")
            self._write_all(items)
            return self._public_view(target)

    def test_profile(self, profile_id: str) -> dict:
        item = self.get_profile(profile_id, masked=False)
        if item is None:
            raise KeyError("未找到指定 API 配置")

        ai = AI()
        ai.config_set(
            {
                "provider": "AI",
                "endpoint": item["endpoint"],
                "key": item["key"],
                "model": item["model"],
                "http_proxy": item.get("http_proxy", ""),
                "min_interval_seconds": str(item.get("min_interval_seconds", 3)),
                "submit": "true" if item.get("submit", True) else "false",
                "cover_rate": str(item.get("cover_rate", 0.9)),
                "use_cache": "true" if item.get("use_cache", True) else "false",
                "true_list": "正确,对,√,是",
                "false_list": "错误,错,×,否,不对,不正确",
            }
        )
        ai.init_tiku()
        t0 = time.time()
        ok = ai.check_llm_connection()
        latency_ms = int((time.time() - t0) * 1000)
        return {
            "ok": ok,
            "latency_ms": latency_ms,
            "msg": f"连接成功 ({latency_ms} ms)" if ok else "连接失败，请检查 Endpoint / Key / Model 或代理设置",
        }


api_store = AIProfileStore()
