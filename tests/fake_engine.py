# -*- coding: utf-8 -*-
"""测试用的假引擎：模仿 pikafish 的那几种反应，用来验证客户端的失联处理。

文件名不匹配 `test_*.py`，所以 run_all.py 不会把它当测试跑。

用法：python fake_engine.py <模式> [计数文件]
模式：
    ok          正常出招（bestmove h2e2）
    none        无着可走（bestmove (none)），进程活着
    invalid     拒收局面：打印 CRITICAL ERROR 后 exit(1)——pikafish 的实测行为
    invalid_once  同上，但只拒第一个进程；记下标记后重起的进程正常出招
                  （用来验证"重启之后管道真的恢复了"，不然重启只是重启一个坏进程）
    stale       第一次 go 拖到 2 秒才吐 bestmove（模拟超时后残留的旧答案），之后正常
    crash_once  第一次 go 直接 exit(3)（无声崩溃）；计数文件的 .restarted 存在后正常出招
计数文件每收到一条命令就追加首单词，用来断言"到底问了几次"。

一个坑：这里的"死"必须用 os._exit，不能用 sys.exit / return。
实测 sys.exit 之后（stdout 已经 EOF）进程还挂在 poll()==None 上，
于是父进程会把"引擎死了"误判成"引擎不理人"。
"""
import io
import os
import sys
import time

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
TALLY = sys.argv[2] if len(sys.argv) > 2 else None
DIE_INVALID = 1
DIE_CRASH = 3
STALE_DELAY = 2.0


def out(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def tally(cmd):
    """记一条收到的命令（只记首单词），用来数"问了几次"。"""
    if not TALLY:
        return
    try:
        with open(TALLY, "a", encoding="utf-8") as f:
            f.write("{}\n".format(cmd.split()[0] if cmd.split() else cmd))
    except OSError:
        pass


def main():
    go_n = 0
    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue
        tally(cmd)
        if cmd == "uci":
            out("id name fake-pikafish")
            out("uciok")
        elif cmd == "isready":
            out("readyok")
        elif cmd == "quit":
            os._exit(0)
        elif cmd.startswith("setoption") or cmd == "stop":
            pass                       # 假引擎不需要真的做什么
        elif cmd.startswith("position"):
            first_run = not (TALLY and os.path.exists(TALLY + ".restarted"))
            if MODE == "invalid" or (MODE == "invalid_once" and first_run):
                if MODE == "invalid_once" and TALLY:
                    open(TALLY + ".restarted", "w").close()
                out("info string CRITICAL ERROR: Command `{}` failed. "
                    "Reason: Unsupported position. King can be captured.".format(cmd))
                os._exit(DIE_INVALID)  # 和 pikafish 一样：拒收之后自己退出
        elif cmd.startswith("go"):
            go_n += 1
            if MODE == "none":
                out("info depth 0 score mate 0")
                out("bestmove (none)")
            elif MODE == "stale" and go_n == 1:
                time.sleep(STALE_DELAY)     # 让客户端先超时（它就还在搜）
                out("info depth 5 score cp 0")
                out("bestmove a0a1")        # 上一问的答案，此刻才吐出来
            elif MODE == "crash_once" and TALLY and not os.path.exists(TALLY + ".restarted"):
                open(TALLY + ".restarted", "w").close()
                os._exit(DIE_CRASH)        # 一声不吭地崩掉
            else:
                out("info depth 8 score cp 12")
                out("bestmove h2e2")
        # 其它命令一律忽略（真引擎也不会为未知命令报错）
    return 0


sys.exit(main())   # 正常读完 stdin 就退出
