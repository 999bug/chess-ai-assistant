# -*- coding: utf-8 -*-
"""auto_coach 里两块新增逻辑的回归测试。

直接跑：python tests/test_tune_track.py

Tune       —— 运行时改参数能不能真的生效（方式：改文件 -> 下一轮 reload）
MoveTrack  —— 着法历史与实际盘面对不上时，能不能安全降级而不是把错历史喂给引擎
"""
import io
import json
import os
import sys
import time

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 运行时核心在 app/ 下（测试在 tests/，两者都挂在项目根下）
sys.path.insert(0, os.path.join(ROOT, "app"))

import auto_coach as A
import rules

FAIL = []


def check(name, got, want):
    ok = got == want
    print("  {} {:<40} -> {!r}".format("PASS" if ok else "FAIL", name, got))
    if not ok:
        FAIL.append("{}: 期望 {!r} 实际 {!r}".format(name, want, got))


print("=== Tune：运行期改参数 ===")
path = os.path.join(ROOT, "out", "_tune_test.json")
if os.path.exists(path):
    os.remove(path)

t = A.Tune(path)
# 3000 是 7c1310f 之后实机标定出来的默认值（那次只改了 DEFAULTS，这里漏掉了）
check("首次启动写出默认值", t["movetime"], 3000)
check("文件已生成", os.path.exists(path), True)
check("没改文件时 reload 返回 False", t.reload(), False)

with open(path, "r", encoding="utf-8") as f:
    raw = json.load(f)
raw["movetime"] = 2000
raw["interval"] = 0.4
raw["这个键不存在"] = 12345
time.sleep(0.05)
with open(path, "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False)

check("改了文件 reload 返回 True", t.reload(), True)
check("新值已生效", t["movetime"], 2000)
check("间隔也生效", t["interval"], 0.4)
check("未知键被忽略（不会带歪参数）", "这个键不存在" in t.data, False)
check("reload 之后不重复触发", t.reload(), False)
os.remove(path)

print("\n=== MoveTrack：着法历史 ===")
FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
pos1 = dict(rules.SETUP)
tr = A.MoveTrack(FEN, pos1)

check("无历史时退化成孤立局面", tr.position("CUR_FEN"), ("CUR_FEN", []))

pos2 = dict(pos1)
pos2[(7, 4)] = pos2.pop((7, 7))                  # 红炮 7,7 -> 7,4
check("正常着法被接受", tr.push([((7, 7), (7, 4))], pos2, FEN), True)
check("记录了一步", len(tr.moves), 1)
check("坐标转换正确", tr.moves[0], "h2e2")
check("起点仍是最初局面", tr.start_fen, FEN)

pos3 = dict(pos2)
pos3[(2, 6)] = pos3.pop((0, 7))                  # 黑马 0,7 -> 2,6
tr.push([((0, 7), (2, 6))], pos3, FEN)
check("累计两步", len(tr.moves), 2)

check("有历史时给出 (起点, 着法表)",
      tr.position("ignored"), (FEN, ["h2e2", "h9g7"]))

NEW_FEN = "9/9/9/9/9/9/9/9/9/9 w - - 0 1"
bad = dict(pos3)
bad[(9, 5)] = bad.pop((9, 0))                    # 车 9,0 -> 9,5 中间被自家子挡着，不合法
check("对不上的着法被拒绝", tr.push([((9, 0), (9, 5))], bad, NEW_FEN), False)
check("拒绝后历史被清空", len(tr.moves), 0)
check("起点改为当前局面（降级但不喂错数据）", tr.start_fen, NEW_FEN)
check("降级后 position 退回孤立局面", tr.position(NEW_FEN), (NEW_FEN, []))

# 盘面对不上（着法本身合法，但结果和识别结果不一致）
tr2 = A.MoveTrack(FEN, pos1)
wrong = dict(pos1)
wrong[(7, 4)] = wrong.pop((7, 7))
wrong[(5, 5)] = "R兵"                            # 凭空多一个兵
check("重放结果与识别盘面不符时也拒绝", tr2.push([((7, 7), (7, 4))], wrong, FEN), False)
check("同样被清空", len(tr2.moves), 0)

print()
if FAIL:
    print("有 {} 个用例未通过：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部用例通过")
