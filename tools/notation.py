# -*- coding: utf-8 -*-
"""中文记谱法解析。

标准格式（以红方视角）：
    前/后 + 棋子 + 纵线 + 平/进/退 + 目标
    红：帅仕相马车炮兵，纵线用汉字数字（从右往左 一~九）
    黑：将士象马车炮卒，纵线用阿拉伯数字（从左往右 1~9）
车/炮/兵/王/帅 这类直走棋子，进退后面跟的是步数；马/象相/士仕跟的是目标纵线。

这一层的作用不只是"读出字"，而是给整个方案兜底：
见 Too Long 的字串一律判为识别噪声，不进棋局。
"""
import re

RED_PIECES = "帅仕相马车炮兵"
BLACK_PIECES = "将士象马车炮卒"
ALL_PIECES = RED_PIECES + BLACK_PIECES
CN_NUM = "一二三四五六七八九"
CN_STEPS = CN_NUM + "十"          # 步数也可能写成 十
ACTION = "平进退"

# 繁体/异体字统一到简体canonical，OCR 常把字认成这些
CANON = {
    "車": "车", "馬": "马", "砲": "炮", "炮": "炮", "傌": "马",
    "將": "将", "帥": "帅", "帥": "帅",
    "仕": "仕", "士": "士", "象": "象", "相": "相",
    "兵": "兵", "卒": "卒",
    "卒": "卒", "兵": "兵",
}

TX = "".join(CANON.keys())

_MOVE_RE = re.compile(
    r"^(?P<prefix>[前后])?"
    r"(?P<piece>[" + ALL_PIECES + TX + r"])"
    r"(?P<file>[" + CN_NUM + r"0-9])"
    r"(?P<action>[" + ACTION + r"])"
    r"(?P<target>[" + CN_STEPS + r"0-9]{1,2})$"
)


def canonical_char(ch):
    return CANON.get(ch, ch)


def normalize_token(s):
    """清洗 OCR 出来的原始字串：去序号、去空白、全角转半角、异体转简体。"""
    if not s:
        return ""
    s = s.strip()
    # 去掉行首的回合序号： "12." "12、" "12:" "(12)" 等
    s = re.sub(r"^[\(\[]?\s*\d{1,3}\s*[\.\、\:\)\]]\s*", "", s)
    s = s.replace(" ", "").replace("\u3000", "")
    # 全角数字转半角
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    # 简单粘连修复：仅在长度超标时尝试去掉尾随杂符
    return "".join(canonical_char(c) for c in s)


def parse_move(token):
    """把 '炮二平五' / '马8进7' 解析成结构化着法，失败返回 None。

    返回 dict：side / piece / file / action / target / text
    """
    raw = normalize_token(token)
    if not raw:
        return None
    m = _MOVE_RE.match(raw)
    if not m:
        return None

    piece = canonical_char(m.group("piece"))
    file_ch = m.group("file")
    action = m.group("action")
    target = m.group("target")
    prefix = m.group("prefix") or ""

    # 判定阵营：车马炮三子靠纵线数字的类型区分
    if piece in "帅仕相兵":
        side = "red"
    elif piece in "将士象卒":
        side = "black"
    else:  # 车 马 炮
        side = "red" if file_ch in CN_NUM else "black"

    if file_ch in CN_NUM:
        file_no = CN_NUM.index(file_ch) + 1
    else:
        file_no = int(file_ch)

    return {
        "side": side,
        "piece": piece,
        "file": file_no,
        "file_ch": file_ch,
        "action": action,
        "target": target,
        "prefix": prefix,
        "text": prefix + piece + file_ch + action + target,
    }


def extract_moves(lines, min_score=0.6):
    """从 OCR 结果列表里挑出合法着法。

    lines: ocrutil.recognize 的输出（含 text/score/y/x）
    返回 [(move, 原始条目)]，按纵向位置排序。
    """
    hits = []
    for item in lines:
        if item.get("score", 0) < min_score:
            continue
        mv = parse_move(item["text"])
        if mv:
            hits.append((mv, item))
    hits.sort(key=lambda p: (p[1]["y"], p[1]["x"]))
    return hits


def is_prefix_of(a, b):
    """判断 move 序列 a 是否为 b 的前缀，用于识别结果比对。"""
    if len(a) > len(b):
        return False
    return a == b[: len(a)]
