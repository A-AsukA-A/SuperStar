import base64
import configparser
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from re import sub
from typing import Optional

import requests
from urllib3 import disable_warnings, exceptions

from api.answer_check import check_answer
from api.logger import logger
from api.question_bank import QuestionBankDAO

# 关闭警告
disable_warnings(exceptions.InsecureRequestWarning)

__all__ = ["CacheDAO", "Tiku", "AI"]


class CacheDAO:
    """保留兼容的旧 JSON 缓存类。"""

    DEFAULT_CACHE_FILE = "cache.json"

    def __init__(self, file: str = DEFAULT_CACHE_FILE):
        self.cache_file = Path(file)
        self._lock = threading.RLock()
        if not self.cache_file.is_file():
            self._write_cache({})

    def _read_cache(self) -> dict:
        try:
            with self._lock:
                if not self.cache_file.is_file():
                    return {}
                try:
                    with self.cache_file.open("r", encoding="utf8") as fp:
                        return json.load(fp)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.error(f"缓存文件读取/解析失败: {e}, 尝试恢复...")
                    try:
                        raw = self.cache_file.read_bytes()
                        text = raw.decode("utf-8", errors="ignore")
                        start = text.find("{")
                        end = text.rfind("}")
                        if start != -1 and end != -1 and start < end:
                            try:
                                return json.loads(text[start : end + 1])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        bak_name = f"{self.cache_file.name}.bak.{int(time.time())}"
                        bak_path = self.cache_file.with_name(bak_name)
                        shutil.copy2(self.cache_file, bak_path)
                    except Exception:
                        pass
                    return {}
        except Exception as e:
            logger.error(f"读取缓存异常: {e}")
            return {}

    def _write_cache(self, data: dict) -> None:
        try:
            with self._lock:
                parent = self.cache_file.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(prefix=self.cache_file.name, dir=str(parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf8") as fp:
                        json.dump(data, fp, ensure_ascii=False, indent=4)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(tmp_path, str(self.cache_file))
                except Exception as e:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                    logger.error(f"Failed to write cache atomically: {e}")
        except IOError as e:
            logger.error(f"Failed to write cache: {e}")

    def get_cache(self, question: str) -> Optional[str]:
        return self._read_cache().get(question)

    def add_cache(self, question: str, answer: str) -> None:
        with self._lock:
            data = self._read_cache()
            data[question] = answer
            self._write_cache(data)


class Tiku:
    CONFIG_PATH = os.path.join(os.getcwd(), "config.ini")
    USE_CACHE = True
    DISABLE = False
    SUBMIT = False
    COVER_RATE = 0.8
    true_list = []
    false_list = []

    def __init__(self) -> None:
        self._name = None
        self._api = None
        self._conf = None
        self._token = None
        self._bank: Optional[QuestionBankDAO] = None

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def api(self):
        return self._api

    @api.setter
    def api(self, value):
        self._api = value

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        self._token = value

    def _get_bank(self) -> QuestionBankDAO:
        if self._bank is None:
            self._bank = QuestionBankDAO()
        return self._bank

    def init_tiku(self):
        if not self._conf:
            self.config_set(self._get_conf())
        if not self.DISABLE and self._conf is not None:
            _uc = str(self._conf.get("use_cache", "true")).strip().lower()
            self.USE_CACHE = _uc not in ("false", "0", "no", "off")
            self.SUBMIT = str(self._conf.get("submit", "false")).strip().lower() == "true"
            try:
                self.COVER_RATE = float(self._conf.get("cover_rate", 0.9) or 0.9)
            except (TypeError, ValueError):
                self.COVER_RATE = 0.9
            true_raw = str(self._conf.get("true_list", "正确,对,√,是"))
            false_raw = str(self._conf.get("false_list", "错误,错,×,否,不对,不正确"))
            self.true_list = [x.strip() for x in true_raw.split(",") if x.strip()]
            self.false_list = [x.strip() for x in false_raw.split(",") if x.strip()]
            self._init_tiku()

    def _init_tiku(self):
        pass

    def config_set(self, config):
        self._conf = config

    def _get_conf(self):
        """从配置文件加载 [tiku] 节，关闭 % 插值。"""
        config_path = os.environ.get("CHAOXING_CONFIG_PATH") or self.CONFIG_PATH
        try:
            config = configparser.ConfigParser(interpolation=None)
            read_files = config.read(config_path, encoding="utf8")
            if not read_files:
                raise FileNotFoundError(config_path)
            return dict(config.items("tiku"))
        except (KeyError, configparser.NoSectionError, FileNotFoundError):
            logger.info("未找到tiku配置, 已忽略AI答题功能")
            self.DISABLE = True
            return None

    def query(self, q_info: dict, course_id: str = "", course_title: str = "") -> Optional[str]:
        if self.DISABLE:
            return None

        logger.debug(f"原始标题：{q_info['title']}")
        q_info["title"] = sub(r"^\d+", "", q_info["title"])
        q_info["title"] = sub(r"（\d+\.\d+分）$", "", q_info["title"])
        logger.debug(f"处理后标题：{q_info['title']}")

        cid = str(course_id or q_info.get("course_id") or "default").strip() or "default"
        ctitle = str(course_title or q_info.get("course_title") or "").strip()
        options = q_info.get("options", "")
        qtype = q_info.get("type", "")
        use_cache = getattr(self, "USE_CACHE", True)
        bank = self._get_bank() if use_cache else None

        # 1. 优先查询按课程划分的自建本地 SQLite 题库
        if use_cache and bank is not None:
            answer = bank.lookup(cid, q_info["title"], options, qtype)
            if answer:
                logger.info(f"命中自建题库[课程 {cid}]：{q_info['title']} -> {answer}")
                return answer.strip()

        # 2. 自建题库未命中时调用 AI API 作答，校验通过后入库
        answer = self._query(q_info)
        if answer:
            answer = answer.strip()
            logger.info(f"从{self.name}获取答案：{q_info['title']} -> {answer}")
            if check_answer(answer, qtype, self):
                if use_cache and bank is not None:
                    bank.save(cid, ctitle, q_info["title"], options, qtype, answer, source="AI")
                else:
                    logger.info("已启用 --no-cache，本次结果不写入自建题库")
                return answer
            else:
                logger.info(f"从{self.name}获取到的答案类型与题目类型不符，已舍弃")
                return None

        logger.error(f"从{self.name}获取答案失败：{q_info['title']}")
        return None

    def _query(self, q_info: dict) -> Optional[str]:
        pass

    def get_tiku_from_config(self):
        """从配置文件加载 AI 答题实例。"""
        if not self._conf:
            self.config_set(self._get_conf())
        if self.DISABLE or not self._conf:
            return self
        try:
            cls_name = str(self._conf.get("provider", "")).strip()
            if not cls_name:
                raise KeyError("provider")
        except KeyError:
            self.DISABLE = True
            logger.error("未找到题库配置, 已忽略AI答题功能")
            return self

        providers = [name.strip() for name in cls_name.split(",") if name.strip()]
        if not providers:
            self.DISABLE = True
            logger.error("provider配置为空, 已忽略AI答题功能")
            return self

        valid_providers = [name for name in providers if name in PROVIDER_REGISTRY]
        if not valid_providers:
            self.DISABLE = True
            logger.warning(f"仅支持 AI 大模型答题，当前配置 provider={cls_name} 已停用")
            return self

        provider_cls = PROVIDER_REGISTRY[valid_providers[0]]
        new_cls = provider_cls()
        new_cls.config_set(self._conf)
        return new_cls

    def judgement_select(self, answer: str) -> bool:
        if self.DISABLE:
            return False
        answer = answer.strip()
        if answer in self.true_list:
            return True
        if answer in self.false_list:
            return False
        logger.error(
            f"无法判断答案 -> {answer} 对应的是正确还是错误, 请自行判断并加入配置文件重启脚本, 本次将会随机选择选项"
        )
        return random.choice([True, False])

    def get_submit_params(self):
        return "" if self.SUBMIT else "1"

    def check_llm_connection(self) -> bool:
        return True


class AI(Tiku):
    """基于 requests 直连的 OpenAI 兼容大模型答题实现（支持 DeepSeek 与图文混合题目）。"""

    IMG_RE = re.compile(r'<img[^>]*?src="([^"]+)"[^>]*>')

    def __init__(self) -> None:
        super().__init__()
        self.name = "AI大模型答题"
        self.last_request_time = None
        self.endpoint = ""
        self.key = ""
        self.model = ""
        self.http_proxy = ""
        self.min_interval_seconds = 3

    def _is_deepseek_v4(self) -> bool:
        return (
            "api.deepseek.com" in (self.endpoint or "").lower()
            and (self.model or "").lower().startswith("deepseek-v4")
        )

    def _wait_for_interval(self):
        if self.last_request_time:
            interval_time = time.time() - self.last_request_time
            if interval_time < self.min_interval_seconds:
                sleep_time = self.min_interval_seconds - interval_time
                logger.debug(f"API请求间隔过短, 等待 {sleep_time} 秒")
                time.sleep(sleep_time)

    @staticmethod
    def _sniff_image_mime(raw: bytes) -> Optional[str]:
        if raw[:4] == b"\x89PNG":
            return "image/png"
        if raw[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        if raw[:2] == b"BM":
            return "image/bmp"
        return None

    def _image_to_data_uri(self, url: str) -> Optional[str]:
        try:
            resp = requests.get(
                url,
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://mooc1.chaoxing.com/",
                },
            )
            if resp.status_code != 200 or not resp.content:
                logger.debug(f"图片下载失败({resp.status_code}): {url}")
                return None
            if len(resp.content) > 4 * 1024 * 1024:
                logger.debug(f"图片过大已跳过: {url}")
                return None
            raw = resp.content
            mime = self._sniff_image_mime(raw)
            if not mime:
                logger.warning(f"下载到的不是图片（可能是 403 页面）: {url}")
                return None
            return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        except Exception as e:
            logger.debug(f"图片下载异常: {url} {e}")
            return None

    def _build_user_content(self, text: str):
        parts = []
        last = 0
        index = 0
        for match in self.IMG_RE.finditer(text):
            parts.append({"type": "text", "text": text[last : match.start()]})
            index += 1
            parts.append({"type": "text", "text": f"[图{index}]"})
            data_uri = self._image_to_data_uri(match.group(1))
            if data_uri:
                parts.append({"type": "image_url", "image_url": {"url": data_uri}})
            else:
                parts.append({"type": "text", "text": "（该图加载失败）"})
            last = match.end()
        parts.append({"type": "text", "text": text[last:]})
        if index == 0:
            return text
        logger.info(f"本题包含 {index} 张图片，已转为多模态请求")
        return parts

    @staticmethod
    def _extract_json_answer(text: str):
        if not text:
            return None
        cleaned = str(text).strip()
        candidates = []
        for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.S):
            candidates.append(m.group(1))
        candidates.append(cleaned)
        for c in candidates:
            try:
                obj = json.loads(c)
                if isinstance(obj, dict) and ("Answer" in obj or "answer" in obj):
                    return obj
            except Exception:
                pass
        for m in reversed(list(re.finditer(r'\{[^{}]*"[Aa]nswer"\s*:[^{}]*\}', cleaned, re.S))):
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict) and ("Answer" in obj or "answer" in obj):
                    return obj
            except Exception:
                continue
        return None

    def _chat_completions_url(self) -> str:
        base = self.endpoint.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _post_chat_completions(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int = 60,
    ) -> str:
        payload: dict = {
            "model": self.model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if self._is_deepseek_v4():
            payload["thinking"] = {"type": "disabled"}

        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        proxies = {"http": self.http_proxy, "https": self.http_proxy} if self.http_proxy else None
        resp = requests.post(
            self._chat_completions_url(),
            headers=headers,
            json=payload,
            proxies=proxies,
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:240]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"响应缺少 choices: {str(data)[:200]}")
        return str((choices[0].get("message") or {}).get("content") or "")

    def _query(self, q_info: dict):
        options = "\n".join(q_info["options"].split("\n"))

        if q_info["type"] == "single":
            system_prompt = (
                "本题为单选题，选项带有 A/B/C/D 编号，其中部分选项可能是图片（消息中用 [图N] 标记）。"
                "请先逐一转写/描述每个选项的内容，再逐条判断正误，最后另起一行输出 JSON。"
                '最终答案必须形如 {"Answer": ["B"]}，只包含选项字母。'
            )
            user_text = f"题目：{q_info['title']}\n选项：\n{options}"
        elif q_info["type"] == "multiple":
            system_prompt = (
                "本题为多选题，选项带有 A/B/C/D 编号，其中部分选项可能是图片（消息中用 [图N] 标记）。"
                "请先逐一转写/描述每个选项的内容，再逐条判断正误（特别注意公式是否有笔误），"
                '最后另起一行输出 JSON。最终答案必须形如 {"Answer": ["A","C"]}，只包含选项字母。'
            )
            user_text = f"题目：{q_info['title']}\n选项：\n{options}"
        elif q_info["type"] == "completion":
            system_prompt = (
                "本题为填空题，你必须根据语境和相关知识填入合适的内容，以json格式输出正确的答案，"
                '示例回答：{"Answer": ["答案"]}。不要使用MD语法。'
            )
            user_text = f"题目：{q_info['title']}"
        elif q_info["type"] == "judgement":
            system_prompt = (
                "本题为判断题，你只能回答正确或者错误，以json格式输出，"
                '示例回答：{"Answer": ["正确"]}。不要使用MD语法。'
            )
            user_text = f"题目：{q_info['title']}"
        else:
            system_prompt = (
                "本题为简答题，请根据题目回答问题，以json格式输出，"
                '示例回答：{"Answer": ["这是我的答案"]}。不要使用MD语法。'
            )
            user_text = f"题目：{q_info['title']}"

        user_content = self._build_user_content(user_text)

        self._wait_for_interval()
        self.last_request_time = time.time()
        try:
            raw_content = self._post_chat_completions(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0,
            )
        except Exception as e:
            logger.error(f"大模型请求失败: {e}")
            return None

        try:
            response = self._extract_json_answer(raw_content)
            if response is None:
                raise ValueError("no json answer found")
            answers = response.get("Answer") or response.get("answer") or []
            if not isinstance(answers, list) or not answers:
                logger.warning(f"模型返回空答案，原始输出: {str(raw_content)[:200]}")
                return None
            if all(isinstance(a, str) and re.fullmatch(r"[A-Ha-h]", a.strip()) for a in answers):
                return "".join(sorted({a.strip().upper() for a in answers}))
            return "\n".join(str(a) for a in answers).strip()
        except Exception:
            logger.error(f"无法解析大模型输出内容，原始输出: {str(raw_content)[:200]}")
            return None

    def _init_tiku(self):
        self.endpoint = str(self._conf.get("endpoint", "")).strip()
        self.key = str(self._conf.get("key", "")).strip()
        self.model = str(self._conf.get("model", "")).strip()
        self.http_proxy = str(self._conf.get("http_proxy", "")).strip()
        try:
            self.min_interval_seconds = int(float(self._conf.get("min_interval_seconds", 3) or 3))
        except (TypeError, ValueError):
            self.min_interval_seconds = 3

        if not self.endpoint or not self.key or not self.model:
            self.DISABLE = True
            logger.warning("AI 题库缺少 endpoint / key / model 配置，已停用 AI 答题功能")

    def check_llm_connection(self) -> bool:
        """检查大模型连接是否可用。"""
        if self.DISABLE or not self.endpoint or not self.key or not self.model:
            logger.warning("AI 答题未配置完整的 endpoint/key/model")
            return False

        logger.info(f"正在检查 {self.name} 连接...")
        try:
            self._wait_for_interval()
            self.last_request_time = time.time()
            content = self._post_chat_completions(
                messages=[{"role": "user", "content": "你好，请回答：1+1 等于几？只回答数字。"}],
                max_tokens=64,
                timeout=30,
            )
            if content:
                logger.info(f"{self.name} 连接检查成功")
                return True
            logger.error(f"{self.name} 连接检查失败：未收到响应")
            return False
        except Exception as e:
            logger.error(f"{self.name} 连接检查失败：{e}")
            return False


PROVIDER_REGISTRY = {
    "AI": AI,
}
