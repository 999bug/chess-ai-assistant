# -*- coding: utf-8 -*-
"""异步搜索（Engine.start / step / take / expired）的回归测试。

直接跑：python tests/test_engine_async.py

主循环现在**不等引擎**了：发起搜索后继续抓图，画面一变就把这次搜索取消掉重来。
这里钉住这条路径上最容易出错的三件事：

  · start/step/take 三件套拿到的结果，必须和同步 bestmove() 完全一致
  · step() 默认（timeout=0）真的不阻塞——主循环每轮就靠它转一圈
  · **取消之后，上一次搜索残留的输出不能漏到下一问**
    这是整条异步路径上最危险的地方：串了的话，浮窗会给出属于**上一个局面**
    的着法，而用户在界面上完全看不出来。串行时代用 resync() 防的就是它。

引擎用 tests/fake_engine.py 顶替，不依赖真引擎和模型。
"""
import io
import os
import sys
import tempfile
import time

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

# 测试产生的日志丢到临时目录，别污染项目的 log/
TMP = tempfile.mkdtemp(prefix="jjchess_async_test_")
os.environ["JJCHESS_LOG_DIR"] = TMP

import coach                      # noqa: E402
from applog import get_logger     # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_engine.py")
FEN_A = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
FEN_B = "4k4/9/9/9/9/9/9/9/9/3RK4 w - - 0 1"

FAIL = []


def check(name, got, want=True):
    ok = got == want
    print("  {} {:<52} -> {!r}".format("PASS" if ok else "FAIL", name, got))
    if not ok:
        FAIL.append("{}: 期望 {!r} 实际 {!r}".format(name, want, got))


def mk_engine(mode, tag):
    """一个假引擎。tag 用来区分计数文件（问了几次都记在里面）。"""
    tally = os.path.join(TMP, "go_{}.txt".format(tag))
    return coach.Engine(path=sys.executable,
                        extra_args=[FAKE, mode, tally],
                        hash_mb=16, threads=1,
                        log=get_logger("async_test"),
                        name="async_test")


def run(eng, limit=10.0):
    """按主循环的写法推进搜索：非阻塞轮询，直到拿到 bestmove 或进程没了。"""
    end = time.time() + limit
    while time.time() < end:
        if eng.step(0.05):
            return True
        if not eng.alive():
            return False          # 进程没了就别干等满
    return False


# ---------------------------------------------------------------- 三件套 == 同步
print("=== 三件套拿到的结果和同步 bestmove() 一致 ===")
eng = mk_engine("ok", "same")
eng.init()
check("发起成功", eng.start(FEN_A, movetime=100), True)
check("收到 bestmove", run(eng), True)
mv, info = eng.take()
check("着法", mv, "h2e2")
check("reason", eng.reason, None)
check("info 行也被收下了", len(info) > 0, True)
check("进程还在", eng.alive(), True)
eng.quit()

# ---------------------------------------------------------------- 非阻塞
print("\n=== step(0) 不阻塞：主循环每轮就靠它转一圈 ===")
# stale 模式第一次 go 会睡 2 秒，正好用来确认"客户端没有在这里等"
eng = mk_engine("stale", "nonblock")
eng.init()
eng.start(FEN_B, movetime=5000)
t0 = time.time()
got = eng.step()                 # 默认 timeout=0
dt = time.time() - t0
check("没拿到 bestmove（引擎还在搜）", got, False)
check("而且几乎没花时间（{:.3f}s < 0.3s）".format(dt), dt < 0.3, True)
check("expired() 此时不该是已超时", eng.expired(), False)
eng.quit()

# ---------------------------------------------------------------- 取消不串答案
print("\n=== 取消之后，上一次搜索的残留输出不能漏到下一问 ===")
# stale 模式：第一次 go 拖 2 秒才吐 bestmove a0a1。
# 这里的 resync() 就是主循环"画面变了"时做的那一下。
eng = mk_engine("stale", "cancel")
eng.init()
check("第一次发起成功", eng.start(FEN_A, movetime=5000), True)
check("取消（拉平管道）成功", eng.resync("模拟：画面变了"), True)
# 残留的 a0a1 应该已经被 resync 连带丢掉了；下一问必须拿到自己的答案
eng.start(FEN_B, movetime=100)
check("新的一问拿到结果", run(eng), True)
mv, _ = eng.take()
check("是新局面的着法，不是残留的 a0a1", mv, "h2e2")
eng.quit()
# 注：这里不测"取消后多久能出结果"。真 pikafish 收到 stop 是毫秒级返回，
# 但 tests/fake_engine.py 是单线程顺序处理的，"睡得正香"时读不到 stop，
# 测出来的耗时只反映假引擎，没有意义。

# ---------------------------------------------------------------- 失联的几种
print("\n=== 异步路径下的失联判定与恢复 ===")
try:
    import auto_coach
except Exception as e:                      # 缺 cv2/onnxruntime 就跳过这一节
    print("  SKIP 导入 auto_coach 失败，跳过 recover_engine 检查: {}".format(e))
else:
    log = get_logger("async_test")

    # 无着可走：引擎活着，只是没招。不该动它。
    eng = mk_engine("none", "rec_none")
    eng.init()
    pid_before = eng.p.pid
    eng.start(FEN_B, movetime=100)
    run(eng)
    eng.take()
    check("reason", eng.reason, coach.Engine.NO_MOVE)
    auto_coach.recover_engine(eng, log)
    check("无着可走不该重启（还是同一个进程）", eng.p.pid, pid_before)
    eng.quit()

    # 拒收局面：引擎已经自己退出了，**必须**重启，否则后面每问都失败
    eng = mk_engine("invalid_once", "rec_invalid")
    eng.init()
    eng.start(FEN_B, movetime=100)
    run(eng)
    eng.take()
    check("reason", eng.reason, coach.Engine.INVALID)
    check("进程确实已经退出", eng.alive(), False)
    auto_coach.recover_engine(eng, log)
    check("拒收之后被拉起来了", eng.alive(), True)
    mv, _ = eng.bestmove(FEN_B, movetime=100)
    check("拉起来之后能正常出招", mv, "h2e2")
    eng.quit()

    # 崩溃：进程没了，同样要重启
    eng = mk_engine("crash_once", "rec_crash")
    eng.init()
    eng.start(FEN_B, movetime=100)
    run(eng)
    eng.take()
    check("reason", eng.reason, coach.Engine.DEAD)
    auto_coach.recover_engine(eng, log)
    check("崩溃之后被拉起来了", eng.alive(), True)
    eng.quit()

# ---------------------------------------------------------------- 进程优先级
print("\n=== 进程优先级：引擎子进程 + 本身（Windows）===")
if os.name != "nt":
    print("  SKIP 非 Windows")
else:
    import ctypes
    from ctypes import wintypes

    # 这份 argtypes 声明就是坑本身：不声明的话 GetCurrentProcess() 的伪句柄
    # 会被当 c_int 截断，SetPriorityClass 静悄悄地失败（详见 coach 里的注释）。
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k.GetPriorityClass.restype = wintypes.DWORD
    k.GetCurrentProcess.argtypes = []
    k.GetCurrentProcess.restype = wintypes.HANDLE
    ABOVE = coach.ABOVE_NORMAL_PRIORITY_CLASS

    # 引擎是子进程，靠 Popen 的 creationflags 提上去
    eng = mk_engine("ok", "prio")
    try:
        h = k.OpenProcess(0x1000, False, eng.p.pid)   # QUERY_LIMITED_INFORMATION
        check("引擎子进程是 AboveNormal", k.GetPriorityClass(h), ABOVE)
    finally:
        eng.quit()

    # 识别跑在本进程里，只能自己调 SetPriorityClass
    check("set_process_priority() 返回成功", coach.set_process_priority(), True)
    check("本进程也提到 AboveNormal",
          k.GetPriorityClass(k.GetCurrentProcess()), ABOVE)

print()
if FAIL:
    print("失败 {} 项：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
