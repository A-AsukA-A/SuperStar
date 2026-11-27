##
# @Author: SocialSisterYi
# @Edit: Samueli924
# @Reference: https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy
#

import base64
import hashlib
import json
import os
import sys
import threading
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, IO, Optional, Union

from api.exceptions import FontDecodeError
from api.logger import logger

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 康熙部首替换表
KX_RADICALS_TAB = str.maketrans(
    "⼀⼁⼂⼃⼄⼅⼆⼇⼈⼉⼊⼋⼌⼍⼎⼏⼐⼑⼒⼓⼔⼕⼖⼗⼘⼙⼚⼛⼜⼝⼞⼟⼠⼡⼢⼣⼤⼥⼦⼧⼨⼩⼪⼫⼬⼭⼮⼯⼰⼱⼲⼳⼴⼵⼶⼷⼸⼹⼺⼻⼼⼽⼾⼿⽀⽁⽂⽃⽄⽅⽆⽇⽈⽉⽊⽋⽌⽍⽎⽏⽐⽑⽒⽓⽔⽕⽖⽗⽘⽙⽚⽛⽜⽝⽞⽟⽠⽡⽢⽣⽤⽥⽦⽧⽨⽩⽪⽫⽬⽭⽮⽯⽰⽱⽲⽳⽴⽵⽶⽷⽸⽹⽺⽻⽼⽽⽾⽿⾀⾁⾂⾃⾄⾅⾆⾇⾈⾉⾊⾋⾌⾍⾎⾏⾐⾑⾒⾓⾔⾕⾖⾗⾘⾙⾚⾛⾜⾝⾞⾟⾠⾡⾢⾣⾤⾥⾦⾧⾨⾩⾪⾫⾬⾭⾮⾯⾰⾱⾲⾳⾴⾵⾶⾷⾸⾹⾺⾻⾼髙⾽⾾⾿⿀⿁⿂⿃⿄⿅⿆⿇⿈⿉⿊⿋⿌⿍⿎⿏⿐⿑⿒⿓⿔⿕⺠⻬⻩⻢⻜⻅⺟⻓",
    "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又口囗土士夂夊夕大女子宀寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳心戈戶手支攴文斗斤方无日曰月木欠止歹殳毋比毛氏气水火爪父爻爿片牙牛犬玄玉瓜瓦甘生用田疋疒癶白皮皿目矛矢石示禸禾穴立竹米糸缶网羊羽老而耒耳聿肉臣自至臼舌舛舟艮色艸虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛辰辵邑酉采里金長門阜隶隹雨青非面革韋韭音頁風飛食首香馬骨高高髟鬥鬯鬲鬼魚鳥鹵鹿麥麻黃黍黑黹黽鼎鼓鼠鼻齊齒龍龜龠民齐黄马飞见母长",
)


def resource_path(relative_path: str) -> str:
    """获取资源文件绝对路径，兼容子进程 cwd 与 PyInstaller 打包。"""
    base_path = getattr(sys, "_MEIPASS", ROOT_DIR)
    return os.path.join(base_path, relative_path)


class FontHashDAO:
    """按需懒加载字体哈希映射表，仅保留单向 hash -> unicode 字典以节省内存。"""

    def __init__(self, file_path: str = "resource/font_map_table.json"):
        self.file_path = file_path
        self._lock = threading.Lock()
        self._loaded = False
        self.hash_map: Dict[str, str] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            full_path = resource_path(self.file_path)
            try:
                with open(full_path, "r", encoding="utf-8") as fp:
                    raw = json.load(fp)
                self.hash_map = {hash_val: char for char, hash_val in raw.items()}
                del raw
            except (FileNotFoundError, json.JSONDecodeError) as e:
                logger.warning(f"加载字体映射表失败: {full_path} - {e}")
                self.hash_map = {}
            finally:
                self._loaded = True

    def find_char(self, font_hash: str) -> Optional[str]:
        self._ensure_loaded()
        return self.hash_map.get(font_hash)


fonthash_dao = FontHashDAO()


def hash_glyph(glyph: Any) -> str:
    """计算 TTF 字体字形的哈希值。"""
    if glyph.numberOfContours <= 0:
        return ""

    pos_data = []
    last_index = 0

    for i in range(glyph.numberOfContours):
        end_point = glyph.endPtsOfContours[i]
        for j in range(last_index, end_point + 1):
            x, y = glyph.coordinates[j]
            flag = glyph.flags[j] & 0x01
            pos_data.append(f"{x}{y}{flag}")
        last_index = end_point + 1

    pos_bin = "".join(pos_data)
    return hashlib.md5(pos_bin.encode()).hexdigest()


def font2map(font_data: Union[IO, Path, str]) -> Dict[str, str]:
    """从字体文件或 Base64 编码字体数据提取字形哈希映射表（按需导入 fontTools）。"""
    from fontTools.ttLib.ttFont import TTFont

    font_hashmap: Dict[str, str] = {}

    if isinstance(font_data, str) and font_data.startswith("data:application/font-ttf;charset=utf-8;base64,"):
        try:
            font_data = BytesIO(base64.b64decode(font_data[47:]))
        except Exception as e:
            raise FontDecodeError(f"无法解码Base64字体数据: {e}") from e

    try:
        with TTFont(font_data, lazy=False) as font_file:
            table = font_file["glyf"]
            for name in table.glyphOrder:
                if name.startswith("uni"):
                    glyph_hash = hash_glyph(table.glyphs[name])
                    if glyph_hash:
                        font_hashmap[name] = glyph_hash
    except Exception as e:
        raise FontDecodeError(f"无法解析字体文件: {e}") from e

    return font_hashmap


def decrypt(dst_fontmap: Dict[str, str], encrypted_text: str) -> str:
    """解密超星学习通加密字体的文本。"""
    result = []

    for char in encrypted_text:
        char_code = f"uni{ord(char):X}"
        if char_code in dst_fontmap:
            dst_hash = dst_fontmap[char_code]
            original_char_code = fonthash_dao.find_char(dst_hash)
            if original_char_code:
                try:
                    original_char = chr(int(original_char_code[3:], 16))
                    result.append(original_char)
                    continue
                except (ValueError, IndexError):
                    pass
        result.append(char)

    return "".join(result).translate(KX_RADICALS_TAB)
