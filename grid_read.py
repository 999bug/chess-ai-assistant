# -*- coding: utf-8 -*-
"""逐格分类：90 个交叉点，每格必定给一个答案（空/红某子/黑某子）。

为什么不用目标检测：检测会漏，漏一个子整个局面就错了；
逐格分类结构上保证每格都有输出，这正是本项目的核心取舍。

兵种识别用「开局自举」：开局摆法是已知的，从已知格子上取字模当模板，
之后每一帧用模板匹配认字。不需要人工标注，也不需要训练模型。

用法：
    python grid_read.py                       # 读配置里记录的那张快照
    python grid_read.py --snapshot out/xx.png
"""
import argparse
import io
import json
import os
import sys

import numpy as np
from PIL import Image

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config", "layout.json")

# 开局标准摆法：行 -> {列: (阵营, 字)}
SETUP = {}
for c, ch in enumerate("車馬象士將士象馬車"):
    SETUP[(0, c)] = ("B", ch)
for c, ch in enumerate("車馬相仕帥仕相馬車"):
    SETUP[(9, c)] = ("R", ch)
for c in (1, 7):
    SETUP[(2, c)] = ("B", "砲")
    SETUP[(7, c)] = ("R", "炮")
for c in (0, 2, 4, 6, 8):
    SETUP[(3, c)] = ("B", "卒")
    SETUP[(6, c)] = ("R", "兵")


def load():
    with open(CFG, "r", encoding="utf-8") as f:
        return json.load(f)


def ink_masks(arr, px, py, cell):
    """取格心区域，返回 (墨迹掩码, 红墨数, 黑墨数, 像素总数)。

    只统计笔画墨迹，避免把偏红的木纹底盘当成红方棋子。
    """
    rad = int(cell * 0.30)
    patch = arr[max(0, int(py - rad)):int(py + rad),
                max(0, int(px - rad)):int(px + rad)].astype(np.int16)
    if patch.size == 0:
        return None, 0, 0, 0
    r, g, b = patch[..., 0], patch[..., 1], patch[..., 2]
    red = (r > 110) & (r > g + 45) & (r > b + 45)
    dark = (r < 105) & (g < 105) & (b < 105)
    mask = red | dark
    return mask, int(red.sum()), int(dark.sum()), int(mask.size)


def norm_mask(mask, size=32):
    """把墨迹掩码缩放到固定尺寸，用于模板匹配。"""
    im = Image.fromarray((mask * 255).astype(np.uint8)).resize((size, size), Image.BILINEAR)
    a = np.asarray(im).astype(np.float32) / 255.0
    a -= a.mean()
    n = np.sqrt((a ** 2).sum())
    return a / n if n > 1e-6 else a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--ink-ratio", type=float, default=0.045,
                    help="墨迹占比超过该阈值判定为有子")
    args = ap.parse_args()

    cfg = load()
    b = cfg["board"]
    pts = b.get("points") or b.get("points_px")
    c0 = b.get("cell") or b.get("cell_px")
    cell = float(c0[0]) if isinstance(c0, (list, tuple)) else float(c0)
    snap = args.snapshot or os.path.join(HERE, "out", cfg["image"]["snapshot"])
    img = Image.open(snap).convert("RGB")
    arr = np.asarray(img)
    print(f"快照 {img.width}x{img.height} 格距 {cell:.1f}  {os.path.basename(snap)}")

    # 1) 逐格判定占位与阵营
    cells = {}
    for ri, row in enumerate(pts):
        for ci, (px, py) in enumerate(row):
            mask, rn, dn, total = ink_masks(arr, px, py, cell)
            if mask is None or total == 0:
                continue
            ratio = (rn + dn) / total
            if ratio < args.ink_ratio:
                continue
            cells[(ri, ci)] = {
                "side": "R" if rn > dn else "B",
                "red": rn, "dark": dn, "ratio": round(ratio, 3),
                "vec": norm_mask(mask),
            }

    print(f"逐格判定有子 {len(cells)} 格（开局应为 32）")
    miss = sorted(set(SETUP) - set(cells))
    extra = sorted(set(cells) - set(SETUP))
    print(f"  漏检: {miss or '无'}")
    print(f"  误检: {extra or '无'}")
    side_ok = sum(1 for k, v in cells.items() if k in SETUP and v["side"] == SETUP[k][0])
    print(f"  红黑判定正确: {side_ok}/{len(cells)}")

    # 2) 开局自举建模板
    templates = {}
    for key, (side, ch) in SETUP.items():
        if key in cells:
            templates[(side, ch)] = cells[key]["vec"]
    print(f"\n从开局摆法提取模板 {len(templates)} 个："
          f"{' '.join(f'{s}{c}' for s, c in templates)}")

    # 3) 模板匹配认兵种
    print("\n识别结果：")
    ok_type = 0
    for ri in range(10):
        line = []
        for ci in range(9):
            v = cells.get((ri, ci))
            if not v:
                line.append("..")
                continue
            best, bs = None, -9
            for (s, ch), tv in templates.items():
                score = float((v["vec"] * tv).sum())
                if score > bs:
                    bs, best = score, (s, ch)
            v["type"] = best
            v["score"] = round(bs, 2)
            exp = SETUP.get((ri, ci))
            hit = exp and best[0] == exp[0] and best[1] == exp[1]
            ok_type += 1 if hit else 0
            mark = "" if hit else "*"
            line.append(f"{best[0]}{best[1]}{mark}")
        print(f"  {ri}  " + " ".join(line))
    print(f"\n兵种识别与开局一致: {ok_type}/{len(cells)}（* 为不一致）")

    out = os.path.join(HERE, "out", "board_grid.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"snapshot": os.path.basename(snap),
                   "cells": [[r, c, v.get("type", [None, None])[0],
                              v.get("type", [None, None])[1], v["side"], v["score"]]
                             for (r, c), v in cells.items()]},
                  f, ensure_ascii=False, indent=2)
    print(f"局面已存: {out}")


if __name__ == "__main__":
    main()
