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
import coach
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
tr = A.MoveTrack(pos1, "R", "red")

check("无历史时退化成孤立局面", tr.position(), (FEN, []))

pos2 = dict(pos1)
pos2[(7, 4)] = pos2.pop((7, 7))                  # 红炮 7,7 -> 7,4
check("正常着法被接受", tr.push([((7, 7), (7, 4))], pos2), True)
check("记录了一步", len(tr.moves), 1)
check("坐标转换正确", tr.moves[0], "h2e2")
check("起点仍是最初局面", tr.start_fen(), FEN)
check("起点 FEN 的轮次跟着我方走（红 -> w）", tr.start_fen().split()[1], "w")

pos3 = dict(pos2)
pos3[(2, 6)] = pos3.pop((0, 7))                  # 黑马 0,7 -> 2,6
tr.push([((0, 7), (2, 6))], pos3)
check("累计两步", len(tr.moves), 2)
check("两步之后又轮到我方", tr.expect_first(), "R")

check("有历史时给出 (起点, 着法表)",
      tr.position(), (FEN, ["h2e2", "h9g7"]))

# 走子方不对：起点是红先，第一手却让黑子走。
# 线上就是这么翻车的——旧实现不看轮次，把错序的历史原样喂给引擎，
# pikafish 判 Illegal move 之后**自己退出**。
tr_bad = A.MoveTrack(pos1, "R", "red")
bpos = dict(pos1)
bpos[(2, 6)] = bpos.pop((0, 7))                  # 第一手就是黑马
check("轮次不对的着法被拒绝", tr_bad.push([((0, 7), (2, 6))], bpos), False)
check("拒绝后历史为空", len(tr_bad.moves), 0)

# 我方执黑：起点该标 b，首手该归黑
tr_black = A.MoveTrack(pos1, "B", "black")
check("我方执黑时起点 FEN 标 b", tr_black.start_fen().split()[1], "b")
check("我方执黑时首手归黑", tr_black.expect_first(), "B")

# 提问引擎前的最后一道保险：FEN 标注和历史首手必须同源
tr_fake = A.MoveTrack(pos1, "R", "red")
tr_fake.moves = ["h9g7"]                         # 伪造：标着红先却记了黑方一手
check("伪造的历史自检不过", tr_fake.history_turn_ok(), False)
tr_ok = A.MoveTrack(pos1, "R", "red")
tr_ok.moves = ["h2e2"]
check("正常的历史自检通过", tr_ok.history_turn_ok(), True)

cur = dict(pos3)
cur[(9, 5)] = cur.pop((9, 0))                    # 车 9,0 -> 9,5 中间被自家子挡着，不合法
check("对不上的着法被拒绝", tr.push([((9, 0), (9, 5))], cur), False)
check("拒绝后历史被清空", len(tr.moves), 0)
check("起点改为当前局面（降级但不喂错数据）",
      tr.position(), (A.fen_of(cur, "R"), []))
check("降级后 position 退回孤立局面", tr.position()[1], [])

# 盘面对不上（着法本身合法，但结果和识别结果不一致）
tr2 = A.MoveTrack(pos1, "R", "red")
wrong = dict(pos1)
wrong[(7, 4)] = wrong.pop((7, 7))
wrong[(5, 5)] = "R兵"                            # 凭空多一个兵
check("重放结果与识别盘面不符时也拒绝", tr2.push([((7, 7), (7, 4))], wrong), False)
check("同样被清空", len(tr2.moves), 0)

print("\n=== 中途进场：轮次推断 ===")
# 平静局面判不出轮次 -> 交给上层（默认按我方走并提示）
check("平静局面判不出轮次", rules.guess_turn(dict(rules.SETUP), "R")[0], None)
check("判不出时 resolve_turn 回退到我方", A.resolve_turn("auto", dict(rules.SETUP), "R")[0], "R")
check("判不出时标记为「猜的」", A.resolve_turn("auto", dict(rules.SETUP), "R")[2], True)
check("显式 --turn 优先，且不算猜", A.resolve_turn("black", dict(rules.SETUP), "R"), ("B", "命令行 --turn 指定", False))

# 我方被将军 -> 只可能轮我方走（否则对方上一手就直接吃了）
check("我方被将军 -> 判出轮我方走",
      rules.guess_turn({(9, 4): "R帥", (0, 4): "B車", (0, 0): "B將"}, "R")[0], "R")
# 我方能吃到对方的将 -> 不可能轮我方（对局里没人走一步送将）
check("对方被将军 -> 判出轮对方走",
      rules.guess_turn({(9, 4): "R帥", (5, 4): "R車", (0, 4): "B將"}, "R")[0], "B")

print("\n=== push：走完不能让自己的将被吃（引擎的 pos.legal 也查这个）===")
# 线上就是这么翻车的：rules.move_legal 只查形状，放行了"送将"的着法，
# 拼进 position fen … moves … 之后被 pikafish 拒收
#   CRITICAL ERROR: … Reason: Illegal move: h0h7
# 然后引擎自己退出；就算不退出，历史也会被它静默截断，
# 于是引擎停在很早的局面上，给出的着法起点和当前帧对不上，
# 中文记谱翻不出来、原样露出 h0h7 这种 UCCI 坐标。
exposed = {
    (9, 4): "R帥",
    (8, 4): "R仕",
    (5, 4): "B車",      # 黑车就在同一列，全靠 (8,4) 的仕挡着
    (0, 4): "B將",
}
tr_unsafe = A.MoveTrack(dict(exposed), "R", "red")
after = dict(exposed)
after[(7, 5)] = after.pop((8, 4))        # 仕斜走一步，把帅暴露给黑车
check("送将的着法被拒", tr_unsafe.push([((8, 4), (7, 5))], after), False)
check("拒绝后历史为空（退化成孤立局面，不会喂错）", len(tr_unsafe.moves), 0)

# 反过来：正常着法不能被误杀
tr_safe = A.MoveTrack(dict(rules.SETUP), "R", "red")
p_ok = dict(rules.SETUP)
p_ok[(7, 4)] = p_ok.pop((7, 7))           # 红炮 7,7 -> 7,4
check("正常的着法仍然被接受（别收紧过头）", tr_safe.push([((7, 7), (7, 4))], p_ok), True)
check("历史正常累积", tr_safe.moves, ["h2e2"])

print("\n=== move_to_chinese：起点没棋子时要明确标出来 ===")
tiny = {(9, 4): ("R帥", 1.0), (0, 4): ("B將", 1.0)}
check("起点有子 -> 正常中文记谱", coach.move_to_chinese(tiny, "e0e1"), "帅五进一")
# 原样返回 UCCI 会让人以为这是一条正常建议，实际根本对不上棋盘
check("起点没子 -> 标注成「？（坐标）」", coach.move_to_chinese(tiny, "h0h7"), "？（h0h7）")

print()
if FAIL:
    print("有 {} 个用例未通过：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部用例通过")
