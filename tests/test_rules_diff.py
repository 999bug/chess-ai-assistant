# -*- coding: utf-8 -*-
"""rules.diff 的回归测试。

直接跑：python tests/test_rules_diff.py

重点盯住两类历史上会**误报**的正常情况：
  · 单步吃子（被吃的子会同时进两侧差异，老实现判成"一次动了 2 处"）
  · 双方各走一步（管道确认一次要好几秒，实际"相邻两帧"经常跨 2 步）
"""
import io
import os
import sys

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import rules


def mk(pos):
    """{(行,列): 标签} -> diff 期望的 {(行,列): (标签, 分)} 结构。"""
    board = {(r, c): ("EMPTY", 1.0) for r in range(10) for c in range(9)}
    for k, v in pos.items():
        board[k] = (v, 0.95)
    return board


def setup(**over):
    """标准开局，再用 over 覆盖：key 形如 '7,7'，值 None 表示清空。"""
    pos = dict(rules.SETUP)
    for k, v in over.items():
        r, c = (int(x) for x in k.split(","))
        if v is None:
            pos.pop((r, c), None)
        else:
            pos[(r, c)] = v
    return pos


FAIL = []


def case(name, expect, prev, cur):
    kind, why = rules.diff(mk(prev), mk(cur))
    ok = kind == expect
    print("  {} {:<44} -> {:<11} {}".format(
        "PASS" if ok else "FAIL", name, kind, why))
    if not ok:
        FAIL.append("{}: 期望 {} 实际 {}（{}）".format(name, expect, kind, why))


print("=== 正常情况（必须不告警）===")

case("无变化", "same", setup(), setup())

case("单步走子：红炮 7,7 -> 7,4", "one_move",
     setup(),
     setup(**{"7,7": None, "7,4": "R炮"}))

case("单步吃子：红兵 6,0 吃黑卒 5,0", "one_move",
     setup(**{"3,0": None, "5,0": "B卒"}),
     setup(**{"3,0": None, "6,0": None, "5,0": "R兵"}))

case("双方各走一步（我方+对方，最典型）", "multi_move",
     setup(),
     setup(**{"7,7": None, "7,4": "R炮", "0,7": None, "2,6": "B馬"}))

case("双方各走一步，其中一步吃子", "multi_move",
     setup(**{"3,0": None, "5,0": "B卒"}),
     setup(**{"3,0": None, "6,0": None, "5,0": "R兵",
              "0,7": None, "2,6": "B馬"}))

case("我方一步 + 对方一步（黑方平车，长距离直线）", "multi_move",
     setup(),
     setup(**{"7,7": None, "7,4": "R炮", "0,0": None, "1,0": "B車"}))

case("重开一局（回到标准开局）", "reset",
     setup(**{"7,7": None, "7,4": "R炮", "0,7": None, "2,6": "B馬"}),
     setup())

print("\n=== 真正异常（才该告警）===")

case("某格棋子凭空消失", "noisy",
     setup(),
     setup(**{"0,7": None}))

case("某格凭空多出一个子", "noisy",
     setup(),
     setup(**{"2,6": "B馬"}))

case("一步跨越 6 格的车（A 到 B 不共线，单步不可达）", "noisy",
     setup(),
     setup(**{"9,0": None, "7,3": "R車"}))

case("一次动了 4 处（超过上限）", "noisy",
     setup(),
     setup(**{"6,0": None, "5,0": "R兵", "6,2": None, "5,2": "R兵",
              "3,0": None, "4,0": "B卒", "3,2": None, "4,2": "B卒"}))

case("凭空消失两处", "noisy",
     setup(),
     setup(**{"0,7": None, "0,1": None}))

print()
if FAIL:
    print("有 {} 个用例未通过：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部用例通过")
