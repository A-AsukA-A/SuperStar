# -*- coding: utf-8 -*-
"""超星学习通网页端二维码扫码登录管理。"""

import base64
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from api.base import validate_cookies_dict
from api.config import GlobalConst as gc
from api.cookies import get_cookies_path, save_cookies_dict
from api.logger import logger


@dataclass
class _QRSession:
    session_id: str
    http_session: requests.Session
    qr_uuid: str
    enc: str
    created_at: float = field(default_factory=time.time)
    expires_in: int = 180


class QRLoginManager:
    """线程安全的超星扫码登录会话管理器。"""

    LOGIN_URL = "https://passport2.chaoxing.com/login?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com"
    FALLBACK_LOGIN_URL = "https://passport2.chaoxing.com/cloudscanlogin"
    CREATE_QR_URL = "https://passport2.chaoxing.com/createqr"
    AUTH_STATUS_URLS = (
        "https://passport2.chaoxing.com/getauthstatus",
        "https://passport2.chaoxing.com/getauthstatus/v2",
    )
    TTL_SECONDS = 300

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _QRSession] = {}

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [sid for sid, item in self._sessions.items() if now - item.created_at > self.TTL_SECONDS]
        for sid in expired:
            self._sessions.pop(sid, None)

    @staticmethod
    def _extract_uuid_enc(html_text: str) -> tuple[str, str]:
        soup = BeautifulSoup(html_text, "lxml")
        uuid_el = soup.find("input", {"id": "uuid"}) or soup.find("input", {"name": "uuid"})
        enc_el = soup.find("input", {"id": "enc"}) or soup.find("input", {"name": "enc"})
        qr_uuid = (uuid_el.get("value") or "").strip() if uuid_el else ""
        enc = (enc_el.get("value") or "").strip() if enc_el else ""

        if not qr_uuid:
            m = re.search(r'id=["\']uuid["\'][^>]*value=["\']([^"\']+)["\']', html_text)
            if m:
                qr_uuid = m.group(1).strip()
        if not enc:
            m = re.search(r'id=["\']enc["\'][^>]*value=["\']([^"\']+)["\']', html_text)
            if m:
                enc = m.group(1).strip()
        return qr_uuid, enc

    @staticmethod
    def _is_image_bytes(raw: bytes, content_type: str = "") -> bool:
        if content_type.lower().startswith("image/"):
            return True
        return (
            raw.startswith(b"\x89PNG")
            or raw.startswith(b"\xff\xd8\xff")
            or raw.startswith(b"GIF87a")
            or raw.startswith(b"GIF89a")
        )

    def create_qr_session(self) -> dict:
        """向超星申请二维码登录会话，返回 base64 PNG 与 session_id。"""
        with self._lock:
            self._cleanup_expired()

        http_session = requests.Session()
        http_session.headers.update(gc.HEADERS)
        http_session.headers.update({"Referer": "https://passport2.chaoxing.com/login"})

        qr_uuid, enc = "", ""
        last_err = None
        for url in (self.LOGIN_URL, self.FALLBACK_LOGIN_URL):
            try:
                resp = http_session.get(url, timeout=10)
                if resp.status_code == 200:
                    qr_uuid, enc = self._extract_uuid_enc(resp.text)
                    if qr_uuid and enc:
                        break
            except requests.RequestException as exc:
                last_err = exc

        if not qr_uuid or not enc:
            detail = f" ({last_err})" if last_err else ""
            raise RuntimeError(f"未能从超星登录页获取二维码参数 uuid/enc{detail}")

        qr_resp = http_session.get(
            self.CREATE_QR_URL,
            params={"uuid": qr_uuid, "fid": "-1"},
            timeout=10,
        )
        if qr_resp.status_code != 200 or not self._is_image_bytes(
            qr_resp.content, qr_resp.headers.get("Content-Type", "")
        ):
            raise RuntimeError(f"获取超星二维码图片失败: HTTP {qr_resp.status_code}")

        mime = (qr_resp.headers.get("Content-Type") or "image/png").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/png"
        qr_b64 = base64.b64encode(qr_resp.content).decode("ascii")

        session_id = uuid.uuid4().hex[:16]
        item = _QRSession(
            session_id=session_id,
            http_session=http_session,
            qr_uuid=qr_uuid,
            enc=enc,
        )
        with self._lock:
            self._sessions[session_id] = item

        return {
            "session_id": session_id,
            "qr_image": f"data:{mime};base64,{qr_b64}",
            "status": "waiting",
            "expires_in": item.expires_in,
        }

    def poll_qr_session(
        self,
        session_id: str,
        save_path: str | None = None,
        return_cookies: bool = False,
    ) -> dict:
        """轮询扫码状态；扫码确认后可写入指定 cookies.txt 或将 cookies 交由调用方保存，并验证会话有效性。"""
        with self._lock:
            self._cleanup_expired()
            item = self._sessions.get(session_id)

        if item is None:
            return {"status": "expired", "msg": "二维码会话不存在或已过期，请刷新"}

        payload = {"enc": item.enc, "uuid": item.qr_uuid, "doubleFactorLogin": "0"}
        res_json = None
        last_err = None
        for status_url in self.AUTH_STATUS_URLS:
            try:
                resp = item.http_session.post(status_url, data=payload, timeout=10)
                if resp.status_code == 200:
                    res_json = resp.json()
                    break
            except (requests.RequestException, ValueError) as exc:
                last_err = exc

        if not isinstance(res_json, dict):
            return {
                "status": "error",
                "msg": f"轮询超星扫码状态失败: {last_err or '响应格式异常'}",
            }

        status_flag = res_json.get("status")
        mes = str(res_json.get("mes") or "").strip()
        code_type = str(res_json.get("type") or "").strip()
        uid = str(res_json.get("uid") or "").strip() or None
        nickname = str(res_json.get("nickname") or "").strip() or None

        if status_flag is True:
            try:
                item.http_session.get("https://i.chaoxing.com/base", timeout=8)
            except requests.RequestException as exc:
                logger.debug("访问 i.chaoxing.com/base 补全 Cookie 时出错（可忽略）: {}", exc)

            cookies = item.http_session.cookies.get_dict()
            saved_path = None
            if save_path or not return_cookies:
                saved_path = save_cookies_dict(cookies, path=save_path or get_cookies_path())
            validation = validate_cookies_dict(cookies)

            with self._lock:
                self._sessions.pop(session_id, None)

            logger.info("二维码登录成功 (uid={})", validation.get("uid") or uid)
            result = {
                "status": "confirmed",
                "msg": "扫码登录成功，已保存并校验会话",
                "uid": validation.get("uid") or uid,
                "nickname": nickname,
                "cookie_valid": validation["valid"],
                "validation_msg": validation["msg"],
                "saved_path": saved_path,
            }
            if return_cookies:
                result["cookies"] = cookies
            return result

        if code_type == "4" or "已扫描" in mes:
            return {
                "status": "scanned",
                "msg": mes or "已扫描，请在学习通 App 点击确认登录",
                "uid": uid,
                "nickname": nickname,
            }

        if code_type == "2" or "失效" in mes or "过期" in mes:
            with self._lock:
                self._sessions.pop(session_id, None)
            return {"status": "expired", "msg": mes or "二维码已失效，请刷新"}

        if code_type == "6" or "取消" in mes:
            with self._lock:
                self._sessions.pop(session_id, None)
            return {"status": "cancelled", "msg": mes or "已在手机端取消登录"}

        return {
            "status": "waiting",
            "msg": mes or "等待手机学习通扫码",
            "uid": uid,
            "nickname": nickname,
        }


qr_login_manager = QRLoginManager()
