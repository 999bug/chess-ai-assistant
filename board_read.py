# -*- coding: utf-8 -*-
"""直接从左下角...不，从整张截图读出整个棋盘局面。

流程完全按架构走：
  1. 读 config/layout.json 的棋盘网格（按比例换算，窗口缩放也不失效）
  2. 霍夫圆检测找出全部棋子
  3. 每个圆心吸附到最近的交叉点 -> 得到占位图（这步天然滤掉了不在格点上的UI圆）
  4. 对每个被占的点：OCR 棋子上的汉字 -> 定兵种；统计红/黑像素 -> 定阵营
  5. 一致性校验：双方各且仅一个将帅、总数、以及兵种数量上限
     校验不通过就明确报错，绝不含糊地把错局面交给上层

用法：
    python board_read.py --snapshot out/snapshot_120857.png
    python board_read.py            # 不传则用配置里记录的快照
"""
import argparse
import io
import json
import os
import sys

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config", "layout.json")

RED_LIMIT = {("帅", 1), ("仕", 2), ("相", 2), ("马", 2), ("车", 2), ("炮", 2), ("兵", 5)}
BLACK_LIMIT = {("将", 1), ("士", 2), ("象", 2), ("马", 2), ("车", 2), ("炮", 2), ("卒", 5)}

GLYPH_MAP = {
    "車": "车", "马": "马", "馬": "马", "砲": "炮", "炮": "炮",
    "帥": "帅", "帅": "帅", "將": "将", "将": "将",
    "仕": "仕", "士": "士", "相": "相", "象": "象",
    "兵": "兵", "卒": "卒",
}


def load_layout(size=None):
    with open(CFG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    img = cfg["image"]
    sx = (size[0] / img["w"]) if size else 1.0
    sy = (size[1] / img["h"]) if size else 1.0
    pts = [[(p[0] * sx, p[1] * sy) for p in row] for row in cfg["board"]["points_px"]]
    cell = cfg["board"]["cell_px"] * ((sx + sy) / 2)
    return cfg, pts, cell


def find_colored(piece_img):
    """用画面里的红/黑像素占比判断阵营。红字红多为红方，黑字多为黑方。"""
    arr = np.asarray(piece_img.convert("RGB")).astype(np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    redish = (r > 120) & (r > g + 35) & (r > b + 35)
    dark = (r < 90) & (g < 90) & (b < 90)
    return int(redish.sum()), int(dark.sum())


def read_glyph(img, pt, cell):
    """裁一个格子，OCR 上面的汉字。取置信度最高的一条。"""
    from PIL import Image

    rad = int(cell * 0.34)
    x, y = int(pt[0]), int(pt[1])
    box = (max(0, x - rad), max(0, y - rad), x + rad, y + rad)
    crop = img.crop(box)
    crop = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
    try:
        import ocrutil
        items = ocrutil.recognize(crop, min_score=0.35, scale=2)
    except Exception:
        return None, 0.0
    if not items:
        return None, 0.0
    items = sorted(items, key=lambda t: -t["score"])
    for it in items:
        for ch in it["text"]:
            if ch in GLYPH_MAP:
                return GLYPH_MAP[ch], it["score"]
    return None, 0.0


def detect_pieces(img, cell):
    """霍夫圆：棋子都是等大的圆。"""
    import cv2

    arr = np.asarray(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    min_r = max(6, int(min(w, h) * 0.020))
    max_r = max(min_r + 4, int(min(w, h) * 0.055))
    blur = cv2.medianBlur(gray, 5)
    for p2 in (28, 22, 18, 14, 11):
        circles = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.0,
                                   minDist=min_r * 1.5, param1=100, param2=p2,
                                   minRadius=min_r, maxRadius=max_r)
        if circles is not None and len(circles[0]) >= 12:
            return [(float(c[0]), float(c[1]), float(c[2])) for c in circles[0]]
    return []


def assign_to_grid(centers, pts, cell):
    """圆心吸附到最近交叉点。距离超过半格就丢弃（滤掉非棋子的圆）。"""
    occ = {}
    dropped = []
    for cx, cy, rr in centers:
        best, bd = None, 1e9
        for r_i, row in enumerate(pts):
            for c_i, (px, py) in enumerate(row):
                d = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
                if d < bd:
                    bd, best = d, (r_i, c_i)
        if best and bd <= cell * 0.48:
            occ[best] = (cx, cy, rr)
        else:
            dropped.append((round(cx), round(cy)))
    return occ, dropped


def consistency(board):
    """规则校验。返回 (是否通过, 问题列表)。"""
    problems = []
    reds = [v for v in board.values() if v["side"] == "red"]
    blacks = [v for v in board.values() if v["side"] == "black"]
    kings = [v for v in reds if v["piece"] == "帅"]
    generals = [v for v in blacks if v["piece"] == "将"]
    if len(kings) != 1:
        problems.append(f"红帅数量应为 1，实际 {len(kings)}")
    if len(generals) != 1:
        problems.append(f"黑将数量应为 1，实际 {len(generals)}")
    total = len(reds) + len(blacks)
    if total > 32:
        problems.append(f"棋子总数上限 32，实际 {total}")
    for side, limit, group in (("red", RED_LIMIT, reds), ("black", BLACK_LIMIT, blacks)):
        for piece, cap in limit:
            n = sum(1 for v in group if v["piece"] == piece)
            if n > cap:
                problems.append(f"{side} {piece} 上限 {cap}，实际 {n}")
    return (not problems), problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--no-ocr", action="store_true", help="跳过 OCR，只定占位和阵营")
    args = ap.parse_args()

    from PIL import Image

    with open(CFG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    snap = args.snapshot or os.path.join(HERE, "out", cfg["image"]["snapshot"])
    if not os.path.exists(snap):
        print(f"!! 快照不存在: {snap}")
        return 1
    img = Image.open(snap).convert("RGB")
    print(f"快照 {img.width}x{img.height}  {os.path.basename(snap)}")

    _cfg, pts, cell = load_layout(img.size)
    centers = detect_pieces(img, cell)
    print(f"霍夫圆检测到 {len(centers)} 个圆")

    occ, dropped = assign_to_grid(centers, pts, cell)
    if dropped:
        print(f"丢弃 {len(dropped)} 个不在格点上的圆（UI 装饰）: {dropped[:5]}")
    print(f"吸附后有子的交叉点: {len(occ)} 个\n")

    board = {}
    for (r_i, c_i), (cx, cy, rr) in sorted(occ.items()):
        pt = pts[r_i][c_i]
        red_n, dark_n = find_colored(img.crop((
            max(0, int(pt[0] - cell * 0.3)), max(0, int(pt[1] - cell * 0.3)),
            int(pt[0] + cell * 0.3), int(pt[1] + cell * 0.3))))
        side = "red" if red_n > dark_n else "black"
        glyph, score = (None, 0.0)
        if not args.no_ocr:
            glyph, score = read_glyph(img, pt, cell)
        board[(r_i, c_i)] = {"piece": glyph or "?", "side": side,
                             "red_px": red_n, "dark_px": dark_n,
                             "conf": round(float(score), 2)}

    print("==" * 4, "读出的棋盘", "==" * 4)
    for r_i in range(10):
        line = []
        for c_i in range(9):
            v = board.get((r_i, c_i))
            line.append(f"{v['piece']}" if v else ".")
        print(f"{r_i}  " + " ".join(line))
    print("\n明细：")
    for (r_i, c_i), v in sorted(board.items()):
        print(f"  ({r_i},{c_i}) {v['side']:<5} {v['piece']}  红像素={v['red_px']:<5} "
              f"黑像素={v['dark_px']:<5} 置信={v['conf']}")

    ok, problems = consistency(board)
    print("\n== 一致性校验 ==")
    if ok:
        n_unknown = sum(1 for v in board.values() if v["piece"] == "?")
        print(f"  通过。共 {len(board)} 子"
              + (f"，其中 {n_unknown} 子兵种未识别" if n_unknown else ""))
    else:
        print("  未通过：")
        for p in problems:
            print(f"   - {p}")

    out = os.path.join(HERE, "out", "board.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"snapshot": os.path.basename(snap),
                   "cells": [[r, c, v["piece"], v["side"], v["conf"]]
                             for (r, c), v in board.items()],
                   "consistent": ok, "problems": problems}, f,
                  ensure_ascii=False, indent=2)
    print(f"\n局面已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
