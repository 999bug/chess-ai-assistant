# -*- coding: utf-8 -*-
"""自动教练：读棋盘 -> 出 FEN -> 问引擎 -> 翻译成中文记谱。

用法：
    python coach.py                    # 抓当前窗口，给红方出建议
    python coach.py --side black       # 给黑方出建议
    python coach.py --depth 12         # 引擎搜索深度
    python coach.py --snapshot out/x.png
"""
import argparse
import ctypes
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAwareness()
    except Exception:
        pass

# 只在还不是 utf-8 时包装。重复包装会让上一层被回收时关掉底层 buffer，
# 表现为 "I/O operation on closed file"
if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import grid_classify as G
import rules
from applog import get_logger

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 app/ 下，上一级才是项目根
ENGINE = os.path.join(ROOT, "engine", "pikafish.exe")


def default_threads():
    """引擎默认线程数：留一半核给别的进程，上限 8。

    识别（ONNX）和引擎是同机跑的，把核全占满反而互相拖慢。
    实测这台 16 核机器上引擎单线程 nps 约 90 万、8 线程约 420 万，
    再往上收益就很小了。
    """
    return max(1, min((os.cpu_count() or 4) // 2, 8))

# 识别标签 -> FEN 字母（大写红、小写黑）
FEN_MAP = {
    ("B", "車"): "r", ("B", "馬"): "n", ("B", "象"): "b", ("B", "士"): "a",
    ("B", "將"): "k", ("B", "砲"): "c", ("B", "卒"): "p",
    ("R", "車"): "R", ("R", "馬"): "N", ("R", "相"): "B", ("R", "仕"): "A",
    ("R", "帥"): "K", ("R", "炮"): "C", ("R", "兵"): "P",
}
# 识别标签 -> 中文记谱用字
NAME_MAP = {
    ("R", "帥"): "帅", ("R", "仕"): "仕", ("R", "相"): "相", ("R", "車"): "车",
    ("R", "馬"): "马", ("R", "炮"): "炮", ("R", "兵"): "兵",
    ("B", "將"): "将", ("B", "士"): "士", ("B", "象"): "象", ("B", "車"): "车",
    ("B", "馬"): "马", ("B", "砲"): "炮", ("B", "卒"): "卒",
}
CN_NUM = "一二三四五六七八九"
STRAIGHT = {"车", "炮", "兵", "卒", "帅", "将"}  # 直走：进退记步数


def board_to_fen(board, turn="red"):
    """{(行,列): 标签} 或 {(行,列): (标签, 分)} -> FEN 串。

    两种取值都收得下：识别结果给的是 (标签, 分)，而着法历史里存的是纯标签
    （历史要按"当时的轮次"重新拼 FEN，拿不到分数也不该卡在这）。
    """
    rows = []
    for r in range(10):
        s, empty = "", 0
        for c in range(9):
            v = board.get((r, c), "EMPTY")
            lab = v[0] if isinstance(v, (tuple, list)) else v
            if not lab or lab == "EMPTY":
                empty += 1
            else:
                if empty:
                    s += str(empty)
                    empty = 0
                s += FEN_MAP.get((lab[0], lab[1:]), "?")
        if empty:
            s += str(empty)
        rows.append(s)
    return "/".join(rows) + (" w " if turn == "red" else " b ") + "- - 0 1"


def ucci_to_rc(mv):
    """'h2e2' -> ((行,列),(行,列))。UCCI 行号是红方视角，0=红底线。"""
    f = ord(mv[0]) - ord('a')
    fr = int(mv[1])
    t = ord(mv[2]) - ord('a')
    tr = int(mv[3])
    return (9 - fr, f), (9 - tr, t)


def rc_to_ucci(r, c):
    """(行,列) -> UCCI 坐标，如 (9,0) -> 'a0'（UCCI 行号 0 = 红方底线）。

    注意引擎也是这个口径（排在第 0 行的是红方底线），所以
    move_to_ucci / ucci_to_rc 和 pikafish 的着法能直接互换。
    与 ucci_to_rc 互逆。
    """
    return chr(ord('a') + int(c)) + str(9 - int(r))


def move_to_ucci(frm, to):
    """((行,列), (行,列)) -> 'a9a8'。给引擎喂着法历史时用。"""
    return rc_to_ucci(*frm) + rc_to_ucci(*to)


def move_to_chinese(board, mv):
    """UCCI 着法 -> 中文记谱，如 '炮二平五'。"""
    (fr, fc), (tr, tc) = ucci_to_rc(mv)
    lab = board.get((fr, fc), ("EMPTY", 0))[0]
    if lab == "EMPTY":
        return mv
    side, piece_ch = lab[0], lab[1:]
    name = NAME_MAP.get((side, piece_ch), piece_ch)

    # 纵线号：红从右往左 一~九，黑从左往右 1~9
    if side == "R":
        file_no = 9 - fc          # col8->1 ... col0->9
        num = lambda n: CN_NUM[n - 1]
    else:
        file_no = fc + 1
        num = lambda n: str(n)

    # 同一纵线上有同色同种子的，加"前/后"
    prefix = ""
    same = [(rr, cc) for (rr, cc) in board
            if cc == fc and board[(rr, cc)][0] == lab and rr != fr]
    if same:
        if side == "R":
            front = min([fr] + [r for r, _ in same]) == fr
        else:
            front = max([fr] + [r for r, _ in same]) == fr
        prefix = "前" if front else "后"

    if tr == fr:                                   # 平
        act = "平" + num(9 - tc if side == "R" else tc + 1)
    else:
        forward = (tr < fr) if side == "R" else (tr > fr)
        act_dir = "进" if forward else "退"
        if name in STRAIGHT:
            act = act_dir + num(abs(tr - fr))      # 直走子记步数
        else:
            act = act_dir + num(9 - tc if side == "R" else tc + 1)
    return prefix + name + num(file_no) + act


class Engine:
    """极简 UCI 客户端（pikafish 走的是 UCI，不是 UCCI）。

    2026-09-29 重写了两处，都是实测踩出来的：

    1) 读线程 + 队列。原来直接在 bestmove() 里 readline，引擎一旦不吭声，
       读操作就把整个进程按死在那一行——"预算超时"的判断根本没有执行的
       机会（它只在两行之间轮得到）。现在输出统一由读线程收进队列，
       所有等待都带真实超时，最坏情况也只是等我们设定的秒数。

    2) 区分失联原因。原来只有一句"引擎没给着法（局面可能不合法）"，
       把三种完全不同的事混成一种。实测它们的性质差别很大：

     - 无着可走（bestmove (none)）：引擎活得好好的，重试一百次也一样
     - 引擎拒收局面：pikafish 打印 "CRITICAL ERROR: ... King can be captured"
       然后**自己退出**。这时重试不是"再问一遍"，是"再打死一次",
       而且进程已经没了，不重启的话后面每一问都会失败
     - 进程崩了/管道断了：这才是重试（重启）能救的

       分不清就只能瞎重试，所以现在 bestmove() 一定写下 self.reason，
       由上层决定是重试、重启，还是老实承认这局面喂不进去。
    """

    # self.reason 的取值（None = 正常拿到着法）
    NO_MOVE = "no_legal_move"      # 引擎回 bestmove (none)：绝杀/困毙，无着可走
    INVALID = "invalid_position"   # 引擎拒收该局面，打印 CRITICAL ERROR 后自杀
    DEAD = "dead"                  # 进程没了 / 管道断了
    TIMEOUT = "timeout"            # 进程还活着，但预算内没回话
    EMPTY = "empty_bestmove"       # 回了 bestmove 行却没带着法（罕见）

    def __init__(self, path=ENGINE, hash_mb=512, threads=None, extra_args=None,
                 log=None, name="engine"):
        if not os.path.exists(path):
            raise FileNotFoundError(f"引擎不存在: {path}")
        self.path = path
        self.extra_args = list(extra_args or [])

        # 引擎自带的默认值完全不够用：Hash 只有 16MB、Threads 只有 1。
        # 实测改成 512MB + 8 线程后 nps 从 90 万涨到 420 万。
        self.hash_mb = int(hash_mb)
        self.threads = int(threads) if threads else default_threads()
        self._sent_hash = None      # 已下发的值，避免重复发（改 Hash 会清空哈希表）
        self._sent_threads = None

        self.log = log if log is not None else get_logger(name)
        self.reason = None          # 上一次提问的结果（None = 正常）
        self.diag = []              # 引擎自己吐的 info string（抱怨都在这儿）
        self.q = None
        self.p = None
        self.eof = False            # 输出流已经读到结尾 = 引擎没了（见 alive()）
        self.spawn()

    # ---------- 进程与管道 ----------
    def spawn(self):
        """起引擎进程 + 读线程。"""
        self.eof = False
        self.p = subprocess.Popen([self.path] + self.extra_args,
                                  stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1,
                                  encoding="utf-8", errors="replace")
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.log.info("引擎进程已启动 pid={}".format(self.p.pid))
        return self.p

    def _pump(self):
        """把引擎的输出搬进队列。EOF 时放一个 None 当哨兵。

        注意顺序：先立 eof 再放哨兵，否则读的一方可能先消费掉哨兵、
        再去问 alive()，那时 eof 还没置上，就会把"引擎已经没了"
        错判成"引擎不理人"。
        """
        try:
            for raw in self.p.stdout:
                self.q.put(raw.strip())
        except Exception:
            pass
        self.eof = True
        self.q.put(None)

    def alive(self):
        """引擎还能用吗。

        为什么把 eof 也算进来：**实测 Windows 上 poll() 有一两秒的滞后**——
        引擎已经退出、stdout 都读到 EOF 了，poll() 还回 None。
        只信 poll() 的话，"进程死了"会被误判成"超时不理人"，
        于是走错恢复分支（该重启的只是干等）。输出流结束是更早也更可信的信号。
        """
        return (self.p is not None and not self.eof and self.p.poll() is None)

    def exit_code(self, wait=0.0):
        """退出码。进程刚死时 poll() 可能还查不到，可以等一小会儿。"""
        if self.p is None:
            return None
        code = self.p.poll()
        if code is None and wait > 0:
            try:
                code = self.p.wait(timeout=wait)
            except Exception:
                code = None
        return code

    def send(self, cmd):
        """下发一条命令。引擎已经死了就返回 False，不抛异常。

        这一点很关键：抛异常的话，一个已经退出的引擎会让识别进程带着栈
        整条退出——"引擎死了"是可恢复的，不该拖垮整条管道。
        （实测往已退出的进程 stdin 写会抛 OSError [Errno 22]。）
        """
        if not self.alive():
            self.log.warn("引擎进程已退出（退出码 {}），不再下发: {}".format(
                self.exit_code(), cmd))
            return False
        try:
            self.p.stdin.write(cmd + "\n")
            self.p.stdin.flush()
            return True
        except (OSError, ValueError) as e:
            self.log.warn("引擎管道已断（{}），不再下发: {}".format(
                type(e).__name__, cmd))
            return False

    def read_until(self, token, timeout):
        """读到含 token 的行为止。返回 (是否读到, 本次收到的所有行)。

        队列超时是真实超时——这是和 "直接 readline" 最重要的区别。
        """
        lines = []
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                return False, lines
            try:
                line = self.q.get(timeout=min(left, 0.25))
            except queue.Empty:
                continue
            if line is None:        # EOF：进程没了
                self.q.put(None)    # 哨兵放回去，后面的调用立刻就知道
                return False, lines
            lines.append(line)
            if token in line:
                return True, lines

    def critical(self):
        """引擎抱怨的原因（CRITICAL ERROR 那行），没有就返回空串。"""
        for line in self.diag:
            if "CRITICAL ERROR" in line:
                return line.split("CRITICAL ERROR:", 1)[-1].strip()
        return ""

    def resync(self, why=""):
        """把管道拉回干净状态：停掉可能还在跑的搜索，等引擎真正空闲。

        为什么必须做：超时之后引擎其实还在搜，它稍后会吐出一行 bestmove。
        下一次提问如果先读到那行，就会把**上一个局面**的着法当成当前局面的
        答案——那比不给答案更糟（会照着错的建议走棋）。
        isready 只在引擎空闲时才回 readyok，所以"读到 readyok 为止、
        中间所有输出全丢掉"正好能把残留的 bestmove 清干净。
        """
        if not self.alive():
            return False
        if not self.send("stop") or not self.send("isready"):
            return False
        found, _ = self.read_until("readyok", 8.0)
        if found:
            self.log.debug("管道已拉平（{}）".format(why or "未说明原因"))
        else:
            self.log.warn("拉平管道时没等到 readyok（{}）".format(why or "未说明原因"))
        return found

    def restart(self, why=""):
        """重启引擎进程并重新下发选项。返回是否可用。

        踩过的坑：局面被引擎拒收后（CRITICAL ERROR）进程就没了，
        这时候"不重试同一局面"是对的，但**必须把引擎拉起来**——
        否则后面每一个局面都会失败，而日志上只看到一串"引擎没给着法"。
        """
        old = self.exit_code()
        self.log.warn("重启引擎（{}）".format(why or "未说明原因"))
        try:
            if self.p is not None:
                if self.p.poll() is None:
                    self.p.kill()
                self.p.wait(timeout=5)
        except Exception:
            pass
        try:
            self.p.stdin.close()
        except Exception:
            pass
        self.spawn()
        try:
            self.init()
        except Exception as e:
            self.log.exception("重启后握手失败", e)
            return False
        self.log.info("引擎已重启（旧进程退出码 {}），Hash {}MB / Threads {}".format(
            old, self.hash_mb, self.threads))
        return True

    def quit(self):
        """让它自己走；不肯走就强杀，别留孤儿进程占内存。"""
        try:
            if self.alive():
                self.send("quit")
                try:
                    self.p.wait(timeout=3)
                except Exception:
                    self.p.kill()
        except Exception:
            pass

    # ---------- UCI 对话 ----------
    def init(self):
        # Pikafish 说的是 UCI 不是 UCCI：发 ucci 会被当成未知命令，
        # 实测就是这样，别再写错
        if not self.send("uci"):
            raise RuntimeError("引擎进程起不来（下发 uci 失败）")
        found, lines = self.read_until("uciok", 10.0)
        if not found:
            self.log.error("引擎没回 uciok，握手失败")
            raise RuntimeError("引擎握手失败（没回 uciok）")
        for line in lines:
            if line.startswith("id "):
                self.log.info("引擎自报: {}".format(line))
        self.apply_options()

    def apply_options(self, hash_mb=None, threads=None):
        """下发引擎选项。运行期随时可调，不需要重启进程。

        Hash 改大小会连带清空哈希表，所以只在数值真的变了时才发。
        """
        if hash_mb is not None:
            self.hash_mb = int(hash_mb)
        if threads is not None:
            self.threads = int(threads)
        if self.threads != self._sent_threads:
            self.send("setoption name Threads value {}".format(self.threads))
            self._sent_threads = self.threads
        if self.hash_mb != self._sent_hash:
            self.send("setoption name Hash value {}".format(self.hash_mb))
            self._sent_hash = self.hash_mb
        if not self.send("isready"):
            return False
        found, _ = self.read_until("readyok", 10.0)
        if not found:
            self.log.warn("下发引擎选项后没等到 readyok")
        return found

    def bestmove(self, fen, depth=None, movetime=None, moves=None, budget=None):
        """问引擎要一手。返回 (着法, info 行列表)。

        depth / movetime：movetime 优先。**推荐用 movetime**——
        pikafish 的 `go depth N` 是"搜到 N 层就收工"，实测 depth 14 只要
        0.05 秒、depth 20 要 1.05 秒，而同样 1 秒走 movetime 能到 23 层。
        把 depth 当强度旋钮会让引擎刚热完身就交卷。

        moves：起点局面之后的着法序列（UCCI）。带上它引擎才知道实际下过
        哪些棋，从而识别重复局面；只丢一个孤立 FEN 的话，引擎判不了
        长将/循环，可能把必和盘面算成优势。

        budget：等回话的秒数上限，默认按 movetime/depth 推算
        （movetime 的 10 倍、最少 20 秒；depth 固定 60 秒）。测试要压短超时
        就只能靠它——movetime 再小，下限也还是 20 秒。

        无论成败都会写下 self.reason，并把引擎吐的 info string 收进
        self.diag（它拒收局面的原因就在那里）。
        """
        self.reason, self.diag = None, []

        if not self.alive():
            self.reason = self.DEAD
            self.log.warn("提问时引擎已经不在了（退出码 {}）｜FEN {}".format(
                self.exit_code(), fen))
            return None, []

        if moves:
            pos_cmd = "position fen {} moves {}".format(fen, " ".join(moves))
        else:
            pos_cmd = "position fen {}".format(fen)

        if movetime:
            cmd, wait = "go movetime {}".format(int(movetime)), \
                max(20.0, int(movetime) / 1000.0 * 10)
        elif depth:
            cmd, wait = "go depth {}".format(int(depth)), 60.0
        else:
            cmd, wait = "go movetime 1000", 20.0
        if budget is not None:
            wait = float(budget)

        # 引擎可能在后一条命令上就已经死了（拒收局面的引擎是在解析 position
        # 的时候就自杀了）。所以即使 go 发不出去，也要把剩下的话读完——
        # 它那行 CRITICAL ERROR 里有"为什么拒收"，丢了就只能瞎猜。
        sent = self.send(pos_cmd) and self.send(cmd)

        t0 = time.time()
        found, lines = self.read_until("bestmove", wait if sent else 0.6)
        info = [l for l in lines if l.startswith("info")]
        self.diag = [l for l in info if l.startswith("info string")]
        self.log.debug("提问 {}｜着法历史 {} 步｜{:.2f}s｜{}行输出".format(
            fen, len(moves or []), time.time() - t0, len(lines)))

        if not found:
            why = self.critical()
            if why:
                # 引擎自己说了为什么（一般是拒收局面），这比我们的推断可靠
                self.reason = self.INVALID
                self.log.error("引擎拒收该局面并退出：{}｜FEN {}".format(why, fen))
            elif not self.alive():
                self.reason = self.DEAD
                self.log.error("引擎在回答前退出（退出码 {}）｜FEN {}｜末尾输出: {}".format(
                    self.exit_code(wait=1.0), fen, " / ".join(lines[-3:]) or "无"))
            elif not sent:
                # 进程还在，但命令已经写不进去了：管道状态不可信，别硬等
                self.reason = self.DEAD
                self.log.warn("命令没能送进引擎（管道异常）｜FEN {}".format(fen))
            else:
                self.reason = self.TIMEOUT
                self.log.warn("引擎 {:.0f}s 内没回话（还活着），拉平管道防错位｜FEN {}".format(
                    wait, fen))
                self.resync("提问超时")
            return None, info

        mv = None
        for line in lines:
            if line.startswith("bestmove"):
                parts = line.split()
                mv = parts[1] if len(parts) > 1 else None
                break

        if mv in ("(none)", "0000"):
            # 引擎明确告诉你"这盘没棋可走了"（绝杀/困毙），不是它出故障
            self.reason = self.NO_MOVE
            self.log.info("引擎判无着可走（{}）｜FEN {}".format(mv, fen))
            return None, info
        if not mv:
            self.reason = self.EMPTY
            self.log.warn("引擎回了空的 bestmove 行｜FEN {}".format(fen))
            return None, info

        if not self.alive():
            self.log.warn("引擎给出着法后立刻退出（退出码 {}）".format(self.exit_code()))
        return mv, info


def parse_score(info):
    """从 info 里取最后一个评分（厘兵，正数红优）。"""
    sc, d = None, None
    for line in info:
        if "score cp" in line:
            try:
                sc = int(line.split("score cp")[1].split()[0])
            except Exception:
                pass
        if " depth " in line:
            try:
                d = int(line.split(" depth ")[1].split()[0])
            except Exception:
                pass
    return sc, d


def engine_reason_text(eng):
    """把引擎这次的失败原因翻成一句人话。

    UI 状态和日志共用一份，别在两处各写一套——否则同一次故障
    在界面和日志里长得不一样，对不上的时候更费劲。
    """
    r = eng.reason
    if r == Engine.NO_MOVE:
        return "无着可走（可能已被绝杀或困毙）"
    if r == Engine.INVALID:
        return "引擎拒收这个局面（{}）".format(eng.critical() or "原因不明")
    if r == Engine.DEAD:
        return "引擎进程已退出（退出码 {}）".format(eng.exit_code())
    if r == Engine.TIMEOUT:
        return "引擎超时没回话（管道已拉平）"
    if r == Engine.EMPTY:
        return "引擎回了空的 bestmove 行"
    return "引擎没给着法（原因不明）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="red", choices=["red", "black"])
    ap.add_argument("--movetime", type=int, default=3000,
                    help="每步思考毫秒数（默认 3000，实测中局约到深度 22）")
    ap.add_argument("--depth", type=int, default=None,
                    help="搜到该深度就停。pikafish 的 depth 很浅（14 只要 0.05 秒），一般别用")
    ap.add_argument("--hash", type=int, default=1024, help="哈希表大小 MB")
    ap.add_argument("--threads", type=int, default=None, help="引擎线程数，默认按 CPU 核数自动")
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--no-engine", action="store_true", help="只出 FEN，不问引擎")
    ap.add_argument("--backend", default="onnx", choices=["onnx", "template"],
                    help="识别后端：onnx=整板分类（默认），template=老的逐格模板匹配")
    args = ap.parse_args()

    cfg, pts, cell = G.load_layout()

    if args.snapshot:
        img_path = args.snapshot
        from PIL import Image
        img = Image.open(img_path).convert("RGB")
    else:
        w = G.find_window()
        if not w:
            print("!! 找不到 JJ象棋 窗口（应用宝容器失焦后会隐藏，先把窗口点出来）")
            return 1
        x, y, ww, hh = w[1]
        sx, sy = ww / cfg["image"]["w"], hh / cfg["image"]["h"]
        pts = [[(p[0] * sx, p[1] * sy) for p in row] for row in pts]
        cell = cell * ((sx + sy) / 2)
        img = G.grab((x, y, ww, hh))
        img_path = os.path.join(ROOT, "out", f"coach_{time.strftime('%H%M%S')}.png")
        img.save(img_path)

    if args.backend == "onnx":
        import board_onnx
        board = board_onnx.classify(img, pts, cell)
    else:
        board = G.classify(img, pts, cell)
    occ = {k: v for k, v in board.items() if v[0] != "EMPTY"}
    print(f"画面 {img.width}x{img.height}  识别后端 {args.backend}  "
          f"识别 {len(occ)} 子  ({os.path.basename(img_path)})")
    print("\n" + G.render(board))

    ok, problems = rules.validate(board)
    fen = board_to_fen(board, args.side)
    print(f"\nFEN: {fen}")
    if not ok:
        print("\n!! 局面校验未通过，不向引擎提问（避免基于错局面出招）：")
        for p in problems[:6]:
            print(f"   - {p}")
        return 2

    # 轮次对不上：这块盘面里我方已经能吃对方的将，说明"该我方走"是判错的
    # （或者识别把某个子放错了）。这种局面喂给 pikafish 会让它打印
    # CRITICAL ERROR 之后自己退出——先说清楚，别把引擎搞死。
    bad, why = rules.king_capturable(rules.pieces(board), args.side)
    if bad:
        print("\n!! 局面与轮次不符：{}。".format(why))
        print("   合法对局里对方被将军就该轮到对方走，所以这里多半是轮次或识别有误，")
        print("   引擎会拒收这个局面（并自行退出），已跳过。")
        return 2

    if args.no_engine:
        return 0

    try:
        eng = Engine(hash_mb=args.hash, threads=args.threads)
        eng.init()
    except FileNotFoundError as e:
        print(f"\n!! {e}")
        print("   引擎还没就位，先用 --no-engine 看 FEN")
        return 1

    print(f"引擎: Hash {eng.hash_mb}MB / Threads {eng.threads}")
    if args.depth:
        mv, info = eng.bestmove(fen, depth=args.depth)
    else:
        mv, info = eng.bestmove(fen, movetime=args.movetime)
    eng.quit()
    if not mv:
        print("!! 引擎没给着法: {}".format(engine_reason_text(eng)))
        return 1

    sc, d = parse_score(info)
    cn = move_to_chinese(board, mv)
    print(f"\n{'='*40}")
    print(f"建议（{'红' if args.side=='red' else '黑'}方）：{cn}    [{mv}]")
    if sc is not None:
        # UCI 的 score cp 以"轮到走的一方"为视角；我们的 FEN 始终标成自己执的一方，
        # 所以正数就是自己占优，不需要再按执方翻转（原来那句 red_view 算了没用上）。
        print("引擎评分：{:+.2f}（正数{}方优）  深度 {}".format(
            sc / 100, "红" if args.side == "red" else "黑", d))
    print("=" * 40)
    return 0


if __name__ == "__main__":
    sys.exit(main())
