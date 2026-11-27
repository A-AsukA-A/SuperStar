# -*- coding: utf-8 -*-
import os
import tempfile

import requests

from api.config import GlobalConst as gc


def get_cookies_path(path: str | None = None) -> str:
    """解析 Cookie 文件路径，优先顺序：显式传参 > 环境变量 > GlobalConst.COOKIES_PATH。"""
    return path or os.environ.get("CHAOXING_COOKIES_PATH") or gc.COOKIES_PATH


def parse_cookie_string(raw: str) -> dict[str, str]:
    """解析分号分隔的 Cookie 字符串，保留值中的 '%' 和 '='。"""
    cookies: dict[str, str] = {}
    if not raw:
        return cookies
    for item in str(raw).strip().split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            continue
        cookies[k] = v.strip()
    return cookies


def save_cookies_dict(cookies: dict[str, str], path: str | None = None) -> str:
    """将 Cookie 字典原子写入指定 cookies.txt 文件，返回实际写入路径。"""
    target = get_cookies_path(path)
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    buffer = ";".join(f"{k}={v}" for k, v in cookies.items() if k)
    fd, tmp_path = tempfile.mkstemp(prefix="cookies_", suffix=".tmp", dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(buffer)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    return target


def save_cookies(session: requests.Session, path: str | None = None) -> str:
    return save_cookies_dict(session.cookies.get_dict(), path=path)


def use_cookies(path: str | None = None) -> dict[str, str]:
    target = get_cookies_path(path)
    if not os.path.exists(target):
        return {}
    with open(target, "r", encoding="utf8") as f:
        return parse_cookie_string(f.read())


def clear_cookies_file(path: str | None = None) -> bool:
    target = get_cookies_path(path)
    if os.path.exists(target):
        os.remove(target)
        return True
    return False
