# -*- coding: utf-8 -*-
"""引擎失联处理的回归测试。

直接跑：python tests/test_engine_recover.py

钉住的是实测出来的四种失联（2026-09-29，把 pikafish 单独拉起来问出来的）：
  · 无着可走   —— 引擎活着回了 bestmove (none)。它没坏，不该重试。
  · 拒收局面   —— pikafish 打印 CRITICAL ERROR 之后**自己退出**。
                  不该重问同一局面（等于再打死一次），但**必须重启进程**——
                  否则从这一手往后每一问都失败，日志上只看到一串"没给着法"。
  · 超时       —— 还活着但没在预算内回话。要能把它随后吐出的残留 bestmove 丢掉，
                  否则下一次提问会把**上一个局面**的答案当成当前局面的，
                  照错的建议走棋比不给建议更糟。
  · 崩溃       —— 进程没了（退出码非零、没有输出）。重启后重问一次应该能拿到答案。

引擎用一个假的可执行文件顶替（tests/fake_engine.py），不依赖真引擎和模型。
"""
import io
import os
import sys
import tempfile

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 运行时核心在 app/ 下（测试在 tests/，两者都挂在项目根下）
sys.path.insert(0, os.path.join(ROOT, "app"))

# 测试产生的日志丢到临时目录，别污染项目的 log/
TMP = tempfile.mkdtemp(prefix="jjchess_engine_test_")
os.environ["JJCHESS_LOG_DIR"] = TMP

import coach                      # noqa: E402
from applog import get_logger     # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_engine.py")
START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
CLEAN_FEN = "4k4/9/9/9/9/9/9/9/9/3RK4 w - - 0 1"

FAIL = []


def check(name, got, want):
    ok = got == want
    print("  {} {:<46} -> {!r}".format("PASS" if ok else "FAIL", name, got))
    if not ok:
        FAIL.append("{}: 期望 {!r} 实际 {!r}".format(name, want, got))


def check_true(name, got):
    check(name, bool(got), True)


def mk_engine(mode, tag):
    """一个假引擎。tag 用来区分计数文件（问了几次 go 都记在里面）。"""
    tally = os.path.join(TMP, "go_{}.txt".format(tag))
    eng = coach.Engine(path=sys.executable,
                       extra_args=[FAKE, mode, tally],
                       hash_mb=16, threads=1,
                       log=get_logger("engine_test"),
                       name="engine_test")
    return eng, tally


def ask_count(tally):
    """问了几次。数的是 position 命令——拒收局面的引擎在解析 position 时
    就已经自杀了，根本读不到后面的 go，所以数 go 会数成 0。"""
    try:
        with open(tally, encoding="utf-8") as f:
            return len([l for l in f if l.strip() == "position"])
    except FileNotFoundError:
        return 0


# ---------------------------------------------------------------- 正常
print("=== 正常出招（对照组）===")
eng, tally = mk_engine("ok", "ok")
eng.init()
mv, info = eng.bestmove(CLEAN_FEN, movetime=100)
check("着法", mv, "h2e2")
check("reason", eng.reason, None)
check("进程还在", eng.alive(), True)
check("只问了 1 次", ask_count(tally), 1)
eng.quit()

# ---------------------------------------------------------------- 无着可走
print("\n=== 无着可走：引擎活着，只是没招 ===")
eng, tally = mk_engine("none", "none")
eng.init()
mv, info = eng.bestmove(CLEAN_FEN, movetime=100)
check("着法", mv, None)
check("reason", eng.reason, coach.Engine.NO_MOVE)
check("进程还活着", eng.alive(), True)
check("引擎没崩", eng.exit_code(), None)
eng.quit()

# ---------------------------------------------------------------- 拒收局面
print("\n=== 拒收局面：引擎打印 CRITICAL ERROR 后自杀 ===")
# 用 invalid_once：第一个进程拒收并退出，重启起来的进程恢复正常出招，
# 这样才能验证"重启之后管道真的能用了"，而不是只重启了一个同样坏的进程
eng, tally = mk_engine("invalid_once", "invalid")
eng.init()
mv, info = eng.bestmove(CLEAN_FEN, movetime=100)
check("着法", mv, None)
check("reason", eng.reason, coach.Engine.INVALID)
check("进程已经退出", eng.alive(), False)
check("退出码", eng.exit_code(wait=5.0), 1)
check_true("留下了引擎的抱怨", "King can be captured" in eng.critical())
# 引擎都死了，这时候"不问"是错的：管道永久废掉，后面每一问都失败
check_true("重启成功", eng.restart("测试：拒收之后必须把引擎拉起来"))
check("重启后进程还在", eng.alive(), True)
check("没白重问同一局面（还是 1 次）", ask_count(tally), 1)
mv2, _ = eng.bestmove(CLEAN_FEN, movetime=100)
check("重启后能正常出招", mv2, "h2e2")
check("这一问确实是重启后的新进程接的", ask_count(tally), 2)
eng.quit()

# ---------------------------------------------------------------- 超时 + 残留答案
print("\n=== 超时：引擎随后吐出的旧答案不能漏到下一问 ===")
eng, tally = mk_engine("stale", "stale")
eng.init()
mv, info = eng.bestmove(CLEAN_FEN, movetime=100, budget=0.6)
check("第一次着法", mv, None)
check("reason", eng.reason, coach.Engine.TIMEOUT)
check("管道已拉平", eng.resync("测试"), True)
mv2, _ = eng.bestmove("4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1", movetime=100,
                      budget=5.0)
check("第二次拿到的是新局面的着法（不是残留的 a0a1）", mv2, "h2e2")
eng.quit()

# ---------------------------------------------------------------- 崩溃 + 重试
print("\n=== 崩溃：进程没了，重启后重问一次 ===")
eng, tally = mk_engine("crash_once", "crash")
eng.init()
mv, info = eng.bestmove(CLEAN_FEN, movetime=100, budget=5.0)
check("第一次着法", mv, None)
check("reason", eng.reason, coach.Engine.DEAD)
check("退出码", eng.exit_code(wait=5.0), 3)   # poll() 有一两秒滞后

try:
    import auto_coach
except Exception as e:                      # 缺 cv2/onnxruntime 就跳过这一节
    print("  SKIP 导入 auto_coach 失败，跳过 ask_engine 的重试检查: {}".format(e))
else:
    class Tune(dict):
        def __missing__(self, k):
            return None
    tune = Tune(depth=None, movetime=100)
    mv, info = auto_coach.ask_engine(eng, CLEAN_FEN, [], tune,
                                     get_logger("engine_test"))
    check("重试后拿到着法", mv, "h2e2")
    check("一共问了 2 次（第一问崩 + 重试 1 次）", ask_count(tally), 2)
    check("引擎被拉起来了", eng.alive(), True)

    # 拒收局面那一路不能重试：重问同一个局面只会再打死一次引擎
    eng2, tally2 = mk_engine("invalid_once", "invalid2")
    eng2.init()
    mv, info = auto_coach.ask_engine(eng2, CLEAN_FEN, [], tune,
                                     get_logger("engine_test"))
    check("拒收局面不重试（只问 1 次）", ask_count(tally2), 1)
    check("但没有把死引擎留在那里", eng2.alive(), True)
    eng2.quit()
eng.quit()

# ---------------------------------------------------------------- 位置门槛
print("\n=== 守门员：轮到我走而我方能吃对方将 ===")
import rules                               # noqa: E402

# 我方车能吃到对方将：红车在 d 列（col3），黑将跑进这一列且中间无子
board = {(r, c): ("EMPTY", 1.0) for r in range(10) for c in range(9)}
board[(9, 3)] = ("R車", 0.99)
board[(9, 4)] = ("R帥", 0.99)              # 帅在 e 列，和黑将不同列，不会误触发照面
board[(5, 3)] = ("B將", 0.99)
bad, why = rules.king_capturable(rules.pieces(board), "R")
check("判出局面与轮次不符", bad, True)
check_true("说清了是谁能吃将: {}".format(why), "R車" in why)

# 正常开局：我方吃不到对方的将
board = {(r, c): ("EMPTY", 1.0) for r in range(10) for c in range(9)}
for k, v in rules.SETUP.items():
    board[k] = (v, 1.0)
bad, why = rules.king_capturable(rules.pieces(board), "R")
check("标准开局不误报", bad, False)
bad, why = rules.king_capturable(rules.pieces(board), "B")
check("标准开局不误报（黑方视角）", bad, False)

# 正常对攻局面：红车和黑将同列但中间隔着子 → 不算能吃
board = {(r, c): ("EMPTY", 1.0) for r in range(10) for c in range(9)}
board[(9, 3)] = ("R車", 0.99)
board[(5, 3)] = ("B卒", 0.99)              # 挡在红车和黑将之间
board[(9, 4)] = ("R帥", 0.99)
board[(4, 3)] = ("B將", 0.99)              # 在九宫里，且和红车之间有一子
bad, why = rules.king_capturable(rules.pieces(board), "R")
check("被挡住就不算能吃", bad, False)

# 炮吃将需要炮架：正好一个炮架时算能吃（引擎同样会拒收这种局面）
board = {(r, c): ("EMPTY", 1.0) for r in range(10) for c in range(9)}
board[(9, 3)] = ("R炮", 0.99)
board[(5, 3)] = ("R卒", 0.99)              # 炮架
board[(9, 4)] = ("R帥", 0.99)
board[(4, 3)] = ("B將", 0.99)
bad, why = rules.king_capturable(rules.pieces(board), "R")
check("炮隔一个架子能吃将", bad, True)

print()
if FAIL:
    print("失败 {} 项：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
