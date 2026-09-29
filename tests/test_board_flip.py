# -*- coding: utf-8 -*-
"""棋盘朝向自适应的回归测试。

直接跑：python tests/test_board_flip.py

looks_flipped —— 判断识别出的盘面是不是相对项目约定上下颠倒了
flip_rows     —— 把盘面镜像回来

背景（2026-09-29 实测踩到）：JJ象棋 在执黑视角、或点了「翻转棋盘」之后会把
整盘翻过来显示，红帅落到 row 0。这时 rules.validate 会一直报
「R帥 在 (0,4) 位置不合法」，守门员永久不放行，助手一局都出不了招。
"""
import io
import os
import sys

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import board_onnx as B
import rules

FAIL = []


def check(name, got, want):
    ok = got == want
    print("  {} {:<48} -> {!r}".format("PASS" if ok else "FAIL", name, got))
    if not ok:
        FAIL.append("{}: 期望 {!r} 实际 {!r}".format(name, want, got))


def as_board(pos):
    """{(行,列): 标签} -> {(行,列): (标签, 分)}，与 classify 的返回结构一致。"""
    return {k: (v, 1.0) for k, v in pos.items()}


print("=== looks_flipped：标准开局 ===")
setup = as_board(rules.SETUP)
check("标准开局（黑方在上）不判翻转", B.looks_flipped(setup), False)
check("标准开局镜像后判翻转", B.looks_flipped(as_board(B.flip_rows(rules.SETUP))), True)

print("\n=== looks_flipped：将帅硬约束优先 ===")
check("红帅在 row 0 -> 翻转",
      B.looks_flipped({(0, 4): ("R帥", 1.0), (9, 4): ("B將", 1.0)}), True)
check("黑将跑到 row 9 -> 翻转",
      B.looks_flipped({(9, 4): ("R帥", 1.0), (0, 4): ("B士", 1.0), (9, 3): ("B將", 1.0)}), True)
check("将帅各在自己半场 -> 不翻转",
      B.looks_flipped({(9, 4): ("R帥", 1.0), (0, 4): ("B將", 1.0)}), False)

print("\n=== looks_flipped：将帅都没认到时退回子力重心 ===")
check("红方在下、黑方在上 -> 不翻转",
      B.looks_flipped({(9, 4): ("R車", 1.0), (8, 0): ("R兵", 1.0),
                       (0, 4): ("B車", 1.0), (1, 0): ("B卒", 1.0)}), False)
check("红方在上、黑方在下 -> 翻转",
      B.looks_flipped({(9, 4): ("B車", 1.0), (8, 0): ("B卒", 1.0),
                       (0, 4): ("R車", 1.0), (1, 0): ("R兵", 1.0)}), True)

print("\n=== looks_flipped：认不全时保守不翻转 ===")
check("只有红子 -> 不翻转", B.looks_flipped({(9, 4): ("R帥", 1.0)}), False)
check("只有黑子 -> 不翻转", B.looks_flipped({(0, 4): ("B將", 1.0)}), False)
check("空盘 -> 不翻转", B.looks_flipped({}), False)
check("全空位 -> 不翻转",
      B.looks_flipped({(0, 0): ("EMPTY", 1.0), (9, 8): ("EMPTY", 1.0)}), False)
check("UNKNOWN 不参与统计 -> 不翻转",
      B.looks_flipped({(0, 0): ("UNKNOWN", 1.0), (9, 8): ("UNKNOWN", 1.0)}), False)

print("\n=== flip_rows：镜像 ===")
check("row 0 的将镜像到 row 9",
      B.flip_rows({(0, 4): ("B將", 1.0)}), {(9, 4): ("B將", 1.0)})
check("列号不变", B.flip_rows({(0, 8): ("x", 1.0)}), {(9, 8): ("x", 1.0)})
check("镜像两次回到原样", B.flip_rows(B.flip_rows(setup)), setup)
# 这条正是现场看到的样子：翻转后红帅落到 row 0，于是被守门员拦下
check("标准开局镜像后红帅落到 row 0",
      B.flip_rows(rules.SETUP)[(0, 4)], "R帥")

print("\n=== 和 rules.validate 串起来看（现场卡死就是这个形状）===")
ok_flipped, probs = rules.validate(as_board(B.flip_rows(rules.SETUP)))
check("翻转的盘面过不了守门员", ok_flipped, False)
check("原因里点到红帅位置", any("R帥" in p for p in probs), True)
ok_fixed, _ = rules.validate(as_board(B.flip_rows(B.flip_rows(rules.SETUP))))
check("镜像回来就能过守门员", ok_fixed, True)

print()
if FAIL:
    print("有 {} 个用例未通过：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部用例通过")
