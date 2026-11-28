# -*- coding: utf-8 -*-
"""测验提交后的批阅结果解析与题库自动纠错。

流程：提交成功 → 重新打开测验页（此时会跳转到批阅/查看页）→ 逐题解析
「对/错」标记与「正确答案」→ 写回自建题库：
  - 有正确答案：写入为 verified（已验证），并把错误作答加入错误列表；
  - 判对：把本次作答写入为 verified；
  - 判错且无正确答案：判断题直接取相反答案为 verified，其余题型记录为已知错误答案。
课程若未公开对错/答案，则解析不到任何结果，直接跳过，不影响原流程。
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

from api.logger import logger
from api.question_bank import QuestionBankDAO, normalize_answer

_CORRECT_CLASSES = {"dui", "marking_dui", "icon-dui", "answerRight"}
_WRONG_CLASSES = {
    "cuo", "marking_cuo", "icon-cuo", "bandui", "marking_bandui",
    "halfRight", "half_right", "answerWrong",
}
_CORRECT_ANSWER_RE = re.compile(
    r"(?:正确答案|参考答案|标准答案)[ \t]*[：:][ \t]*\n?[ \t]*"
    r"(?!(?:我的答案|正确答案|参考答案|标准答案|答案解析|解析|得分|批语|评语))"
    r"(.+?)(?=\n|我的答案|答案解析|解析|得分|批语|评语|$)"
)
_MY_ANSWER_RE = re.compile(
    r"我的答案[ \t]*[：:][ \t]*\n?[ \t]*"
    r"(?!(?:我的答案|正确答案|参考答案|标准答案|答案解析|解析|得分|批语|评语))"
    r"(.+?)(?=\n|正确答案|参考答案|标准答案|答案解析|解析|得分|批语|评语|$)"
)
_SCORE_RE = re.compile(r"得分\s*[：:]?\s*(\d+(?:\.\d+)?)\s*分?")
_FULL_SCORE_RE = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[)）]")
_UNGRADED_RE = re.compile(r"(?:待批阅|未批阅|待教师批阅)")
_REDO_URL_RE = re.compile(
    r"""['"]((?:https?:)?//mooc1\.chaoxing\.com)?(/mooc-ans/work/doHomeWorkNew\?[^'"]*(?:isredo|redo)[^'"]*)['"]""",
    re.I,
)


def extract_redo_url(html_content: str) -> Optional[str]:
    """从测验/批阅页 HTML 中提取超星重做入口 URL。"""
    if not html_content:
        return None
    m = _REDO_URL_RE.search(html_content)
    if not m:
        return None
    return "https://mooc1.chaoxing.com" + _html.unescape(m.group(2))


def _block_id(block) -> str:
    for attr in ("data", "data-id", "questionid", "qid"):
        val = str(block.attrs.get(attr, "") or "").strip()
        if re.fullmatch(r"\d{4,}", val):
            return val
    m = re.search(r"(\d{6,})", str(block.attrs.get("id", "") or ""))
    return m.group(1) if m else ""


def _classes_in(block) -> set[str]:
    found: set[str] = set()
    for tag in block.find_all(True):
        for c in tag.get("class") or []:
            found.add(str(c))
    return found


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" ;；,，")


def _extract_dom_answer(block, selectors: tuple[str, ...], font_decoder) -> str:
    for sel in selectors:
        node = block.select_one(sel)
        if node is None:
            continue
        raw = node.get_text(" ")
        if font_decoder is not None:
            try:
                raw = font_decoder.decode(raw)
            except Exception:
                pass
        cleaned = _clean(re.sub(r"^(?:正确答案|参考答案|标准答案|我的答案)[ \t]*[：:]?", "", raw))
        if cleaned:
            return cleaned
    return ""


def parse_review_html(html: str) -> dict[str, Any]:
    """解析批阅页。返回 {"by_id": {qid: info}, "ordered": [info, ...]}，info 含 correct / correct_answer / my_answer。"""
    result: dict[str, Any] = {"by_id": {}, "ordered": []}
    if not html:
        return result
    soup = BeautifulSoup(html, "lxml")
    font_decoder = None
    if soup.find("style", id="cxSecretStyle"):
        try:
            from api.decode import FontDecoder

            font_decoder = FontDecoder(html)
        except Exception as exc:  # 字体解密失败时只使用对错标记与字母答案
            logger.debug(f"批阅页字体解密初始化失败: {exc}")

    blocks = soup.select("div.questionLi") or soup.select("div.singleQuesId") or soup.select("div.TiMu")
    for block in blocks:
        for bad in block.find_all(["script", "style", "noscript"]):
            bad.decompose()
        dom_correct_answer = _extract_dom_answer(block, (".rightAnswerContent", ".correctAnswer"), font_decoder)
        dom_my_answer = _extract_dom_answer(block, (".myAnswerContent", ".stuAnswerContent"), font_decoder)

        text = block.get_text("\n")
        if font_decoder is not None:
            try:
                text = font_decoder.decode(text)
            except Exception:
                pass

        classes = _classes_in(block)
        correct: Optional[bool] = None
        if classes & _WRONG_CLASSES:
            correct = False
        elif classes & _CORRECT_CLASSES:
            correct = True

        if correct is None and not _UNGRADED_RE.search(text):
            score_m = _SCORE_RE.search(text)
            full_m = _FULL_SCORE_RE.search(text)
            if score_m and full_m:
                try:
                    score, full = float(score_m.group(1)), float(full_m.group(1))
                    if full > 0:
                        correct = score >= full
                except ValueError:
                    pass

        ca_m = _CORRECT_ANSWER_RE.search(text)
        my_m = _MY_ANSWER_RE.search(text)
        info = {
            "id": _block_id(block),
            "correct": correct,
            "correct_answer": dom_correct_answer or (_clean(ca_m.group(1)) if ca_m else ""),
            "my_answer": dom_my_answer or (_clean(my_m.group(1)) if my_m else ""),
        }
        if info["correct"] is None and not info["correct_answer"]:
            info["empty"] = True
        result["ordered"].append(info)
        if info["id"]:
            result["by_id"][info["id"]] = info
    return result


def submitted_to_bank_answer(q: dict) -> str:
    """把提交表单里的答案（字母 / true,false / 文本）转换成题库的答案形式。"""
    raw = str((q.get("answerField") or {}).get(f'answer{q.get("id")}', "") or "").strip()
    q_type = str(q.get("type") or "")
    if q_type == "judgement":
        return {"true": "正确", "false": "错误"}.get(raw.lower(), normalize_answer(raw, q_type))
    return raw


def _extract_correct(answer: str, q_type: str) -> str:
    text = str(answer or "").strip()
    if not text:
        return ""
    if q_type in ("single", "multiple"):
        compact = re.sub(r"[\s,，、;；|/]+", "", text)
        if re.fullmatch(r"[A-Ha-h]{1,8}", compact):
            return "".join(sorted(set(compact.upper())))
        if q_type == "single":
            m = re.match(r"^([A-Ha-h])(?:\s*[.、:：)）]|\s|$)", text)
            if m:
                return m.group(1).upper()
        return ""
    if q_type == "judgement":
        norm = normalize_answer(text, q_type)
        return norm if norm in ("正确", "错误") else ""
    return text


def apply_review(
    bank: QuestionBankDAO,
    course_id: str,
    course_title: str,
    questions: list[dict],
    review: dict[str, Any],
) -> dict[str, int]:
    stats = {"verified": 0, "corrected": 0, "wrong": 0, "unknown": 0}
    ordered = review.get("ordered") or []
    by_id = review.get("by_id") or {}
    use_order = len(ordered) == len(questions)
    for idx, q in enumerate(questions):
        info = by_id.get(str(q.get("id") or "")) or (ordered[idx] if use_order else None)
        q_type = str(q.get("type") or "")
        if not info or info.get("empty") or q_type in ("shortanswer", "unknown", ""):
            stats["unknown"] += 1
            continue
        submitted = submitted_to_bank_answer(q)
        correct_answer = _extract_correct(info.get("correct_answer", ""), q_type)
        is_correct = info.get("correct")
        wrong_answer = ""

        if correct_answer:
            if submitted and normalize_answer(submitted, q_type) != normalize_answer(correct_answer, q_type):
                wrong_answer = submitted
        elif is_correct is True and submitted:
            correct_answer = submitted
        elif is_correct is False and submitted:
            if q_type == "judgement":
                correct_answer = "错误" if normalize_answer(submitted, q_type) == "正确" else "正确"
            wrong_answer = submitted
        else:
            stats["unknown"] += 1
            continue

        try:
            action = bank.record_review(
                course_id, course_title, q.get("title", ""), q.get("options", ""), q_type,
                correct_answer=correct_answer, wrong_answer=wrong_answer,
            )
        except Exception as exc:
            logger.warning(f"写回批阅结果失败: {exc}")
            stats["unknown"] += 1
            continue
        if action in stats:
            stats[action] += 1
        else:
            stats["unknown"] += 1
    return stats
