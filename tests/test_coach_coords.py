# -*- coding: utf-8 -*-
"""坐标与中文记谱转换的回归测试。

直接跑：python tests/test_coach_coords.py

这两处转换一旦错位，浮窗上就会显示一个"看起来像那么回事、照着走却不对"的着法，
比直接报错更难发现，所以用固定用例钉死。
"""
import io
import os
import sys

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 运行时核心在 app/ 下（测试在 tests/，两者都挂在项目根下）
sys.path.insert(0, os.path.join(ROOT, "app"))

import coach
import rules

FAIL = []


def check(name, got, want):
    ok = got == want
    print("  {} {:<38} -> {!r}".format("PASS" if ok else "FAIL", name, got))
    if not ok:
        FAIL.append("{}: 期望 {!r} 实际 {!r}".format(name, want, got))


print("=== UCCI <-> (行,列) 必须互逆 ===")
for mv in ["a0a1", "h2e2", "e9e8", "c3c4", "a9a8", "i0i9", "b0c2"]:
    frm, to = coach.ucci_to_rc(mv)
    check(mv, coach.move_to_ucci(frm, to), mv)

print("\n=== 中文记谱 ===")
board = {(r, c): (v, 1.0) for (r, c), v in rules.SETUP.items()}

check("红炮 (7,7)->(7,4)", coach.move_to_chinese(board, "h2e2"), "炮二平五")
check("红马 (9,1)->(7,2)", coach.move_to_chinese(board, "b0c2"), "马八进七")
check("红车 (9,0)->(9,1)", coach.move_to_chinese(board, "a0b0"), "车九平八")
check("红兵 (6,0)->(5,0)", coach.move_to_chinese(board, "a3a4"), "兵九进一")
check("黑炮 (2,1)->(2,4)", coach.move_to_chinese(board, "b7e7"), "炮2平5")
check("黑卒 (3,0)->(4,0)", coach.move_to_chinese(board, "a6a5"), "卒1进1")
check("黑车 (0,0)->(1,0)", coach.move_to_chinese(board, "a9a8"), "车1进1")

print()
if FAIL:
    print("有 {} 个用例未通过：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部用例通过")
