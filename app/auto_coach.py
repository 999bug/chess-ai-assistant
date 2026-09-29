# -*- coding: utf-8 -*-
"""自动教练后台进程：盯着棋盘，你走完一步就自动算出下一手。

和全局架构一致：
- 每轮重新抓全窗、重新识别整盘，不靠上一帧推算，漏一帧不会错位
- 局面连续多帧一致才认账，滤掉走子动画和加载中的中间态
- 引擎常驻，不每步重启

它只负责算，结果写进 out/suggestion.json；浮窗由 hud.py 读这个文件显示
（之所以拆两个进程：隔离环境没带 tkinter，而系统 Python 有，
 让系统 Python 只画 UI 就不用往它身上装任何包）。

速度是怎么来的（2026-09-29 重构）
---------------------------------
纯推理其实只要 256ms（board_onnx.py --snapshot 显示的 1.2~1.8s 里，
绝大部分是每个新进程一次的模型加载，不是帧耗时）。
老版本一轮 = interval 1.5s 纯 sleep + 推理，再要求连续 3 帧一致，
确认一次盘面要 5.4 秒；而对局里对方一两秒就回招，
于是两次确认之间必然跨两步，差分天天报异常。现在改成两级：

  ① 每轮只算一张整板缩略图（~1ms）判断"画面变没变"，没变就完全不碰 ONNX；
  ② 画面确实稳定了，才跑一次 ONNX 定案（256ms）。

正确性不变（仍然是"稳定才认账"），确认延迟从 5.4 秒降到 1 秒左右。

运行期参数在 out/tune.json，改了保存下一轮就生效，不用重启——见 Tune 的说明。

用法：
    python auto_coach.py                       # 默认红方
    python auto_coach.py --side black
    python auto_coach.py --movetime 1500
    python auto_coach.py --interval 0.4 --stable 2
"""
import argparse
import ctypes
import io
import json
import os
import sys
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAwareness()
    except Exception:
        pass

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import cv2
import numpy as np

import grid_classify as G
import rules
from applog import get_logger, install_excepthook
from coach import (Engine, board_to_fen, engine_reason_text, move_to_chinese,
                   move_to_ucci, parse_score, set_process_priority, ucci_to_rc)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 app/ 下，上一级才是项目根
OUT_DIR = os.path.join(ROOT, "out")
OUT_JSON = os.path.join(OUT_DIR, "suggestion.json")
# 配置文件放 config/ 而不是 out/：out/ 整个目录是运行期产物（截图、建议），
# 被 .gitignore 挡着，配置放里面换台机器就丢了。
TUNE_PATH = os.path.join(ROOT, "config", "tune.json")
TUNE_LEGACY = os.path.join(OUT_DIR, "tune.json")   # 老位置，仅用于一次性迁移
# 手工重置信号：浮窗上的「重开一局」按钮（或直接建这个文件）写了它就重置。
# 放 out/ 而不是 config/：这是一次性的运行期产物，不是要入库的配置。
RESTART_FLAG = os.path.join(OUT_DIR, "restart.flag")

# 整板缩略图：45x50，每格约 5px。够看出棋子动没动，算起来又便宜（0.9ms/帧）。
SIG_SIZE = (45, 50)
# 单个像素算"变了"的灰度差；再统计"变了"的像素占比，比只看均值稳得多
SIG_PIXEL_THR = 20
# 阈值是实测标定出来的（拿 out/ 里的历史快照两两比对，看差异像素占比）：
#     同一局面的连续帧      0.00000     <- 噪声下限
#     只走掉一个子          0.00578     <- 必须能检出的最小真实变化
#     两个不同局面之间      0.108 ~ 0.176
# 所以取 0.002：离噪声有足够余量，又能逮住"一个子挪了个窝"。


def recognize(backend, img, pts, cell):
    """按后端取一帧的识别结果。返回 {(行,列): (标签, 分)}。

    onnx     —— 整板 ONNX 分类（默认）。把拉正后的棋盘一次喂给网络，
                90 格一起判，结构上不会"某一格塌掉"。
    template —— 老的逐格模板匹配。保留是为了万一 ONNX 模型不可用时还能跑，
                并且能拿同一帧做新旧对比（见 eval_recognition.py）。
    """
    if backend == "onnx":
        import board_onnx
        return board_onnx.classify(img, pts, cell)
    return G.classify(img, pts, cell)


def write(payload):
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_JSON)  # 原子替换，读的一方不会读到半个文件


# 子力数允许的抖动量：识别偶尔多认/少认一两个子，不该当成走了棋。
POWER_DROP_TOLERANCE = 2


def power_drop(last_count, n_now):
    """子力骤降的告警文案，没有就返回空串。

    为什么要这条：一步最多吃掉一个子，采样间隔又只有几百毫秒，子力不该掉一大截。
    实测过识别从 32 子掉到 22 子（马炮整批漏检）却仍然"结构合法"，
    引擎基于残缺局面给了荒唐建议。这一条专门堵这个洞。

    **只判"降"，不判"升"**（2026-09-29 修）：子力变多只可能是新开一局，
    或者识别把空位认成了子——前者无害，后者由静态校验的子力上限兜住。
    旧实现是在这里顺手把基线改成当前值的（"子力变多当作新开一局"），
    于是坏帧反而能污染基线：识别崩掉时会读出 42 子（棋盘上限 32），
    基线一变成 42，之后每一帧正常盘面都被判「42 -> 32 骤降，疑似漏检」，
    守门员永久不放行——助手连一局都跑不完。日志见 log/auto_coach-*.log。
    所以基线只由**过了守门员的帧**来立（见主循环），这个函数纯粹只读。
    """
    if last_count is None or last_count - n_now <= POWER_DROP_TOLERANCE:
        return ""
    return "子力 {} -> {} 骤降，疑似漏检".format(last_count, n_now)


def restart_requested(path=RESTART_FLAG):
    """有人请求「重开一局」吗？有就把信号消费掉（删文件）并返回 True。

    为什么要跨进程走文件：浮窗借的是系统 Python、识别跑在隔离环境，
    两个进程不共享内存，只能靠文件通信（out/suggestion.json 也是这么传的）。

    为什么要删掉而不是一直读：这是一次性动作。文件留着的话每一轮都会重置，
    等于永远停在"刚重新开始"，反而再也不会出招了。
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        # 删不掉（被占用之类）就当没收到：留着下一轮再试，总比反复重置强
        print("  重置信号处理失败: {}".format(e))
        return False
    return True


class Reporter:
    """写 out/suggestion.json（浮窗读它），同时把同一份状态记进日志。

    两件事绑在一起是有意的：浮窗上那行字和日志必须是同一个事实，
    分成两处写迟早会对不上（"界面明明写着 A，日志里只有 B"最难查）。
    状态没变就不重复记——日志要能一眼看出"什么时候发生过什么"，
    被同一句话刷满就白记了。
    """

    def __init__(self, log):
        self.log = log
        self.last = None

    def status(self, status, move=None, level="info", detail=None, exc=None):
        write({"ts": time.time(), "status": status, "move": move})
        if status == self.last:
            return
        self.last = status
        if exc is not None:
            self.log.exception(status, exc)     # 带栈，只在第一次出现时记
        else:
            getattr(self.log, level)(status)
        if detail:
            getattr(self.log, level)("  " + detail)


def ask_engine(eng, start_fen, moves, tune, log, retries=1):
    """问引擎要一手，失联时先恢复再重问。返回 (着法, info)。

    **同步版本**：调用方会被阻塞到出结果为止。主循环已经不用它了——改走
    Engine.start/step/take 的异步三件套，好在等待期间继续盯画面（见主循环里
    `pending` 那一段）。留着它是因为"失败后立刻重问一次"这个行为本身仍然对，
    测试也在钉它（tests/test_engine_recover.py）。

    为什么要分情况重试：只有"进程死了 / 超时没回话"重试才有意义。
    另外两种重试一百次也是同一个结果，而且都有副作用：

      · 无着可走（bestmove (none)）：引擎好着呢，重问纯属浪费时间
      · 引擎拒收局面：pikafish 会打印 CRITICAL ERROR 然后**自己退出**，
        重问等于再打死一次。但这时进程已经没了，**必须重启**——
        不然从这一手往后每一问都失败，日志上只看到一串"引擎没给着法"。
    """
    def ask():
        if tune["depth"]:
            return eng.bestmove(start_fen, depth=tune["depth"], moves=moves)
        return eng.bestmove(start_fen, movetime=tune["movetime"], moves=moves)

    mv, info = ask()
    if mv or eng.reason in (Engine.NO_MOVE, Engine.INVALID, Engine.EMPTY):
        if eng.reason == Engine.INVALID:
            eng.restart("上一问被引擎拒收局面，进程已退出")
        return mv, info

    for i in range(retries):
        reason = eng.reason
        if reason == Engine.TIMEOUT:
            # bestmove 里已经 stop + isready 拉平过管道，直接重问
            log.warn("引擎超时，重问一次（FEN {}）".format(start_fen))
        else:
            if not eng.restart("提问失败: {}".format(reason)):
                return None, info
            log.warn("引擎已重启，重问一次（FEN {}）".format(start_fen))
        mv, info = ask()
        if mv:
            log.info("恢复后拿到着法 {}（第 {} 次重试）".format(mv, i + 1))
            return mv, info
        if eng.reason in (Engine.NO_MOVE, Engine.INVALID, Engine.EMPTY):
            if eng.reason == Engine.INVALID:
                eng.restart("重试时又被拒收局面")
            break
    return None, info


def recover_engine(eng, log):
    """一次提问失败后，把引擎收拾到"下一问还能用"的状态。

    和 ask_engine 的分工：ask_engine 是"收拾完立刻重问一次"，这里只收拾，
    重不重问交给调用方——异步路径上等到下一轮采样时局面可能已经变了，
    拿着旧局面重问没有意义。

    只有这几种值得动手：
      · 拒收局面 / 进程没了 → 进程已经退出，**必须重启**，否则从这一手往后
                              每一问都失败，日志上只看到一串"引擎没给着法"
      · 超时               → 引擎其实还在搜，要拉平管道；它稍后吐出的
                              bestmove 会被下一问当成新局面的答案，那更糟
      · 无着可走 / 空 bestmove → 引擎好着呢，什么都不用做
    返回一句能给用户看的话。
    """
    reason = eng.reason
    if reason == Engine.INVALID:
        eng.restart("上一问被引擎拒收局面，进程已退出")
    elif reason == Engine.DEAD:
        eng.restart("引擎进程没了")
    elif reason == Engine.TIMEOUT:
        eng.resync("上一问超时，管道里可能还留着它的输出")
    return engine_reason_text(eng)


def emit_suggestion(pending, info, mv, rep, log):
    """把一手建议写进浮窗和日志，返回挂到浮窗上的那条记录。

    参数全从 pending 里取，**不读当前循环的 board / track**：搜索是异步的，
    等到结果的那一轮盘面可能已经翻篇，那时再去读当前状态就张冠李戴了。
    （也正是因为这样，pending 里必须存着出建议所需的全部上下文。）
    """
    sc, d = parse_score(info)
    cn = move_to_chinese(pending["board"], mv)
    last_move = {"cn": cn, "mv": mv, "score": sc, "depth": d,
                 "fen": pending["fen_now"], "n": pending["n"],
                 "history": pending["history"], "movetime": pending["movetime"]}
    rep.status(pending["warn"], last_move)
    # 每一手都留痕（含完整 FEN）。这是事后复盘"当时它到底看到了什么"的
    # 唯一凭据——out/suggestion.json 会被下一手覆盖，日志不会。
    log.info("出招 {} [{}]｜评分 {}｜深度 {}｜{} 子｜历史 {} 步｜{:.2f}s｜FEN {}".format(
        cn, mv, "无" if sc is None else "{:+.2f}".format(sc / 100), d,
        pending["n"], pending["history"], time.time() - pending["t0"],
        pending["fen_now"]))
    print("[{}] {}  [{}]  评分 {:+.2f}  深度 {}  "
          "({} 子, 历史 {} 步, 本轮 {:.2f}s)".format(
              time.strftime("%H:%M:%S"), cn, mv,
              sc / 100 if sc is not None else 0, d, pending["n"],
              pending["history"], time.time() - pending["t0"]))
    if pending["warn"]:
        print("  !! {}".format(pending["warn"]))
    return last_move


def window_rect(hwnd):
    """按缓存的句柄直接取窗口位置，省掉每轮 EnumWindows。

    窗口没了或被隐藏（应用宝失焦会隐藏）就返回 None，由调用方重新枚举。
    """
    try:
        import win32gui
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if (r - l) <= 80 or (b - t) <= 80:
            return None
        return (l, t, r - l, b - t)
    except Exception:
        return None


def board_signature(img, pts, cell):
    """整板缩略图，用来判断"画面变没变"。

    取棋盘四角略外扩的区域（保证边缘棋子也在画面里），透视缩到 SIG_SIZE。
    代价约 1ms，比整板 ONNX 推理（256ms）便宜两个数量级——
    画面没变就不用跑识别，这是把确认延迟压到 1 秒的关键。
    """
    arr = np.asarray(img.convert("RGB") if hasattr(img, "convert") else img)
    k = cell * 0.6                       # 外扩半格，覆盖边缘棋子的圆盘
    src = np.float32([
        [pts[0][0][0] - k, pts[0][0][1] - k],
        [pts[0][8][0] + k, pts[0][8][1] - k],
        [pts[9][0][0] - k, pts[9][0][1] + k],
        [pts[9][8][0] + k, pts[9][8][1] + k],
    ])
    dst = np.float32([[0, 0], [SIG_SIZE[0], 0],
                      [0, SIG_SIZE[1]], [SIG_SIZE[0], SIG_SIZE[1]]])
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(arr, m, SIG_SIZE, flags=cv2.INTER_AREA)
    return cv2.cvtColor(warped, cv2.COLOR_RGB2GRAY)


def sig_diff(a, b):
    """两张签名的差异占比（0~1）。任一为空视为完全不同。"""
    if a is None or b is None:
        return 1.0
    return float((cv2.absdiff(a, b) > SIG_PIXEL_THR).mean())


class Tune:
    """运行期参数：读 out/tune.json，改了保存就生效，不用重启。

    为什么用文件而不是命令行参数：引擎和识别跑在同一个长驻进程里，
    为了调个思考时间还要 Ctrl+C 重开一遍，谁都不乐意调。
    放文件里，对局中途改一行保存，下一轮就应用上了。

    清单（括号里是在 i7-11700 / 16 线程这台机器上实测挑出来的值）：
      movetime  每步思考毫秒数（3000：中局约到深度 22；1000 只有 18）
      depth     给了就改用固定深度。注意 pikafish 的 depth 很浅，一般别用
      hash_mb   引擎哈希表 MB（1024。实测 512 已到饱和点，再大不涨；但引擎默认只有 16，必须设）
      threads   引擎线程数，null = 按 CPU 核数自动
      interval  采样间隔秒（0.25）
      stable    画面连续稳定几帧才认定局面（2）
      sig_thr   判定"画面变了"的差异像素占比阈值
      force_after  画面持续变化多久后不再等稳定、强行按当前帧定案（秒）
      max_steps 差分最多用几步合法着法解释
    """

    DEFAULTS = {
        "movetime": 3000,
        "depth": None,
        "hash_mb": 1024,
        "threads": None,
        "interval": 0.25,
        "stable": 2,
        "sig_thr": 0.002,
        "force_after": 6.0,
        "max_steps": 3,
    }

    def __init__(self, path=TUNE_PATH, legacy=TUNE_LEGACY):
        self.path = path
        self.legacy = legacy
        self.mtime = None
        self.data = dict(self.DEFAULTS)
        if not os.path.exists(self.path):
            self._migrate_legacy()
        if not os.path.exists(self.path):
            self.save()
        self.reload(force=True)

    def _migrate_legacy(self):
        """把老位置（out/tune.json）的配置搬过来。

        老版本把 tune.json 生成在 out/ 下，而 out/ 整个目录不入库，
        辛苦调好的参数换台机器就没了。这里一次性迁移，老文件留着不管。
        """
        if not self.legacy or not os.path.exists(self.legacy):
            return
        try:
            with open(self.legacy, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in self.DEFAULTS:
                        self.data[k] = v
            print("  已把 {} 迁移到 {}".format(
                os.path.relpath(self.legacy, ROOT),
                os.path.relpath(self.path, ROOT)))
        except Exception as e:
            print("  老 tune.json 迁移失败（按默认值继续）:", e)
            return
        try:
            os.remove(self.legacy)      # 老位置在 out/ 下，留着只会让人改错文件
            print("  已把 {} 迁移到 {}，并删除老文件".format(
                os.path.relpath(self.legacy, ROOT),
                os.path.relpath(self.path, ROOT)))
        except OSError:
            print("  已把 {} 迁移到 {}".format(
                os.path.relpath(self.legacy, ROOT),
                os.path.relpath(self.path, ROOT)))

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)
        try:
            self.mtime = os.path.getmtime(self.path)
        except OSError:
            self.mtime = None

    def reload(self, force=False):
        """文件变了就重读。返回参数是否发生了变化。"""
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            return False
        if not force and mt == self.mtime:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print("  tune.json 读取失败，沿用上次的值:", e)
            self.mtime = mt
            return False
        data = dict(self.DEFAULTS)
        if isinstance(raw, dict):
            # 只认已知的键，写错名字不会把参数带歪
            data.update({k: v for k, v in raw.items() if k in self.DEFAULTS})
        changed = data != self.data
        self.data, self.mtime = data, mt
        return changed

    def __getitem__(self, key):
        return self.data[key]


SIDE_LETTER = {"red": "R", "black": "B"}   # 我方颜色 -> 棋盘标签里的字头


def other_letter(letter):
    return "B" if letter == "R" else "R"


def side_word(letter):
    return "红方" if letter == "R" else "黑方"


def fen_of(board, letter):
    """盘面 + 轮次 -> FEN。轮次传字头（"R"/"B"）。"""
    return board_to_fen(board, "red" if letter == "R" else "black")


def resolve_turn(explicit, pos, me):
    """定下"进场那一刻轮到谁走"，返回 (字头, 说明, 是不是猜的)。

    为什么这是个真问题：对局打到一半才打开引擎时没有"上一帧"可比，起点 FEN
    里那个"轮谁走"只能定出来。定错了不会打死引擎（FEN 标注和着法历史同源，
    永远自洽），但会把对方该走的着法当建议给出来——比不给更糟。
    所以：命令行显式指定 > 从局面推断（被将军的局面能判出来）> 默认我方。
    """
    if explicit in ("red", "black"):
        return SIDE_LETTER[explicit], "命令行 --turn 指定", False
    turn, why = rules.guess_turn(pos, me)
    if turn:
        return turn, why, False
    return me, why + "，先按「我方走」处理", True


class MoveTrack:
    """维护"起点局面（含轮次）+ 之后走过的着法"，让引擎看得到对局历史。

    为什么需要：只丢一个孤立 FEN，引擎不知道之前下过什么，就判断不了
    重复局面（三次重复算和棋）。结果该求和的盘它当优势继续磨，
    能逼和的时候又看不出来。

    为什么要连轮次一起存：起点 FEN 里的"轮谁走"过去是**写死成我方**的，
    于是"历史第一手归谁"也只能跟着写死。一旦差分把顺序排反（见
    rules.explain_change 里那段说明），引擎就收到"标着红先、却先给黑着法"
    的命令，判 Illegal move 之后**自己退出**。现在轮次是一个真实状态：
    进场时推断或指定，之后每一手翻一次，FEN 标注和着法历史同源、不可能矛盾。

    安全第一：每次追加着法都会在本地重放一遍，只有重放结果与当前识别盘面
    完全一致、**且每一步都归该走的那一方**，才接受；一旦对不上就地重置为当前局面。
    丢掉历史顶多少一个信息，把错的历史喂给引擎才是真出事。
    """

    def __init__(self, pos, turn="R", side="red"):
        self.side = side                    # 我方颜色（"red"/"black"）
        self.letter = SIDE_LETTER.get(side, "R")
        self.turn = turn                    # 起点局面轮到谁走（"R"/"B"）
        self.start_pos = dict(pos)
        self.pos = dict(pos)
        self.moves = []

    def reset(self, pos, turn=None):
        """以给定局面重新起头（turn 省略时沿用当前轮次）。"""
        self.start_pos = dict(pos)
        self.pos = dict(pos)
        self.moves = []
        if turn:
            self.turn = turn

    def expect_first(self):
        """接下来该由谁走——即"当前"该谁走（历史偶数步 = 起点那一方）。"""
        if len(self.moves) % 2 == 0:
            return self.turn
        return other_letter(self.turn)

    def start_fen(self):
        """起点 FEN。轮次字段跟着 self.turn 走，不写死。"""
        return fen_of(self.start_pos, self.turn)

    def push(self, seq, cur_pos):
        """追加若干步着法。轮次或盘面对不上就重置，返回是否保留历史。"""
        work = dict(self.pos)
        want = self.expect_first()
        for frm, to in seq:
            legal, _why = rules.move_legal(work, frm, to)
            if not legal:
                self.reset(cur_pos)
                return False
            if work[frm][0] != want:     # 这一步不归它走 -> 轮次或识别有问题
                self.reset(cur_pos)
                return False
            work[to] = work.pop(frm)
            want = other_letter(want)
        if work != cur_pos:
            self.reset(cur_pos)
            return False
        self.moves.extend(move_to_ucci(f, t) for f, t in seq)
        self.pos = work
        return True

    def history_turn_ok(self):
        """重放整段历史，确认走子方是"起点那一方先、之后逐手交替"。

        提问引擎之前的最后一道保险：就算真出问题，也只是把历史丢掉、退化成
        孤立局面，绝不能把轮次错的历史送进去——引擎会拒收并把进程打死。
        """
        work = dict(self.start_pos)
        for i, mv in enumerate(self.moves):
            frm, to = ucci_to_rc(mv)
            lab = work.get(frm)
            want = self.turn if i % 2 == 0 else other_letter(self.turn)
            if not lab or lab[0] != want:
                return False
            work[to] = work.pop(frm)
        return True

    def position(self):
        """返回交给引擎的 (起点 FEN, 着法列表)。没历史时退化成孤立局面。"""
        return self.start_fen(), list(self.moves)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="red", choices=["red", "black"])
    ap.add_argument("--turn", default="auto", choices=["auto", "red", "black"],
                    help="进场那一刻轮到谁走。默认 auto：从局面推断（被将军的"
                         "局面判得出来），判不出就按我方走并提示。中途接手且"
                         "局面平静时，用这个参数直接告诉它更准。")
    ap.add_argument("--movetime", type=int, default=None,
                    help="引擎每步思考毫秒数（默认 1000）")
    ap.add_argument("--depth", type=int, default=None,
                    help="固定搜索深度。pikafish 的 depth 很浅（14 只要 0.05 秒），一般别用")
    ap.add_argument("--interval", type=float, default=None, help="采样间隔秒（默认 0.25）")
    ap.add_argument("--stable", type=int, default=None,
                    help="画面连续稳定几帧才认定局面（默认 2）")
    ap.add_argument("--hash-mb", dest="hash_mb", type=int, default=None,
                    help="引擎哈希表 MB（默认 512；引擎自带的默认只有 16，会限制搜索质量）")
    ap.add_argument("--threads", type=int, default=None,
                    help="引擎线程数，默认按 CPU 核数自动")
    ap.add_argument("--backend", default="onnx", choices=["onnx", "template"],
                    help="识别后端：onnx=整板分类（默认），template=老的逐格模板匹配")
    ap.add_argument("--no-learn", dest="learn", action="store_false",
                    help="关闭在线样本积累（默认开启，仅 template 后端有效）")
    args = ap.parse_args()

    # 日志先立起来：后面所有失败（缺引擎、缺模型、握手不上）都要有地方说话。
    log = get_logger("auto_coach")
    install_excepthook(log)
    log.info("=" * 60)
    log.info("启动 auto_coach pid={}｜{}".format(os.getpid(), " ".join(sys.argv)))

    # 识别（ONNX）、引擎、JJ象棋 抢的是同一台机器。默认优先级下游戏一忙，
    # 识别就可能被排到队尾——表现是"这一手莫名其妙慢了很多"，而且难复现。
    # 提到 AboveNormal 只是略微优先，不会拖累系统；失败也无所谓，这是优化不是功能。
    if set_process_priority():
        log.info("进程优先级已提到 AboveNormal")
    else:
        log.info("进程优先级没提上去（非 Windows 或调用失败），不影响功能")

    if not os.path.exists(os.path.join(ROOT, "engine", "pikafish.exe")):
        print("!! 引擎不存在：engine/pikafish.exe")
        log.error("引擎不存在：engine/pikafish.exe")
        return 1
    if args.backend == "onnx":
        model = os.path.join(ROOT, "models", "layout_nano.onnx")
        if not os.path.exists(model):
            print("!! 整板识别模型不存在：models/layout_nano.onnx")
            print("   运行 python download_models.py 下载（需要能访问 HuggingFace）")
            log.error("整板识别模型不存在：models/layout_nano.onnx")
            return 1
        # 先把模型加载好。一是加载失败要立刻失败而不是等第一帧，
        # 二是别让第一帧多扛一次一秒多的首次加载
        try:
            import board_onnx
            board_onnx.session()
            print("整板识别模型已加载")
        except Exception as e:
            print(f"!! 整板识别模型加载失败: {type(e).__name__}: {e}")
            log.exception("整板识别模型加载失败", e)
            return 1
    else:
        if not os.path.exists(os.path.join(OUT_DIR, "templates.npz")):
            print("!! 模板不存在：先跑 python grid_classify.py --build（画面须为开局）")
            log.error("模板不存在：out/templates.npz")
            return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    tune = Tune(TUNE_PATH)
    overridden = []
    for key in ("movetime", "depth", "interval", "stable", "hash_mb", "threads"):
        val = getattr(args, key)
        if val is not None and tune[key] != val:
            tune.data[key] = val
            overridden.append("{}={}".format(key, val))
    if overridden:
        tune.save()

    try:
        eng = Engine(hash_mb=tune["hash_mb"], threads=tune["threads"])
        eng.init()
    except Exception as e:
        print(f"!! 引擎启动失败: {e}")
        log.exception("引擎启动失败", e)
        return 1

    print("引擎就绪：Hash {}MB / Threads {}".format(eng.hash_mb, eng.threads))
    print("识别后端 {}；执{}方；{}".format(
        args.backend, "红" if args.side == "red" else "黑",
        "固定深度 {}".format(tune["depth"]) if tune["depth"]
        else "每步思考 {} ms".format(tune["movetime"])))
    if overridden:
        print("命令行覆盖：" + "，".join(overridden))
    print("运行期参数见 {}（改了保存下一轮生效，不用重启）".format(
        os.path.relpath(TUNE_PATH, ROOT)))
    log.info("引擎就绪 Hash {}MB / Threads {}｜识别后端 {}｜执{}方｜{}".format(
        eng.hash_mb, eng.threads, args.backend,
        "红" if args.side == "red" else "黑",
        "固定深度 {}".format(tune["depth"]) if tune["depth"]
        else "每步思考 {} ms".format(tune["movetime"])))
    log.info("调参: {}".format(json.dumps(tune.data, ensure_ascii=False)))
    if overridden:
        log.info("命令行覆盖: " + "，".join(overridden))

    cfg, pts0, cell0 = G.load_layout()

    hwnd = None            # 缓存的窗口句柄，避免每轮 EnumWindows
    last_board = None      # 上一次通过校验的盘面（结构版，差分用）
    last_key = None        # 上一次通过校验的盘面（标签快照，去重用）
    last_count = None      # 上一次的子力数
    last_move = None
    bad_n = 0
    track = None           # 着法历史
    sig_prev = None        # 上一帧签名
    sig_done = None        # 已经定过案的签名
    still_n = 0            # 画面连续稳定帧数
    last_ts = time.time()  # 上次定案（或启动）的时间，给强行定案兜底用
    pending = None         # 正在进行的一轮搜索（异步）；None = 引擎不在忙

    rep = Reporter(log)
    rep.status("启动中…")

    try:
        while True:
            # 运行期调参：文件变了就热更新（引擎选项不需要重启进程）
            if tune.reload():
                eng.apply_options(hash_mb=tune["hash_mb"], threads=tune["threads"])
                print("  运行参数已更新：movetime={} depth={} hash={}MB threads={} "
                      "interval={} stable={}".format(
                          tune["movetime"], tune["depth"], eng.hash_mb, eng.threads,
                          tune["interval"], tune["stable"]))
                log.info("tune.json 已热更新: {}".format(
                    json.dumps(tune.data, ensure_ascii=False)))

            # ---- 手工重置：浮窗「重开一局」按钮，或直接建 out/restart.flag ----
            # 一局下完之后，管道里攒着上一局的着法历史、轮次和子力基线。
            # 靠差分自己走出来只有一条路——盘面正好回到标准 32 子开局
            # （rules.explain_change 返回 reset）。可 JJ象棋 有「棋力评测」
            # 这类非标准开局模式，认不出 reset，于是轮次会一直继承上一局
            # （常见症状：卡在"轮对方走"再也不出招）。
            # 所以给一个不看盘面、直接把状态清干净的入口。
            #
            # 注意 sig_prev 是**有意不清**的：留着它，画面没变时下一轮就不会
            # 走进"画面变化中…"分支，"已重置"这句状态才能在浮窗上多停一会儿。
            # sig_done 必须清，否则重置后这张画面会被当成"已经定过案"而跳过。
            if restart_requested():
                if pending is not None:
                    # 有搜索在跑就先停掉。不停的话它稍后吐出的 bestmove 会留在
                    # 管道里，被下一问当成新局面的答案——照着错的建议走棋，
                    # 比不给建议更糟。
                    eng.resync("手工重置，放弃进行中的搜索")
                    pending = None
                track = None          # 着法历史 + 轮次，会重新走"首帧"那套推断
                last_board = None     # 差分基准
                last_key = None       # 去重用的标签快照
                last_count = None     # 子力基线
                last_move = None      # 浮窗上还挂着上一局的建议，一起撤掉
                bad_n = 0
                sig_done = None
                still_n = 0
                last_ts = time.time()
                text = "已重置：清空上一局的记忆，按当前局面重新识别"
                rep.status(text)     # 写 out/suggestion.json 给浮窗，顺带记一条日志
                print("  " + text)

            interval = max(0.02, float(tune["interval"]))
            stable = max(1, int(tune["stable"]))

            rect = window_rect(hwnd) if hwnd else None
            if not rect:
                w = G.find_window()
                if not w:
                    hwnd = None
                    rep.status("找不到 JJ象棋 窗口（应用宝失焦会隐藏，把窗口点出来）",
                               last_move, level="warn")
                    time.sleep(interval)
                    continue
                hwnd, rect = w[0], w[1]

            sx, sy = rect[2] / cfg["image"]["w"], rect[3] / cfg["image"]["h"]
            pts = [[(p[0] * sx, p[1] * sy) for p in row] for row in pts0]
            cell = cell0 * ((sx + sy) / 2)

            try:
                img = G.grab(rect)
                sig = board_signature(img, pts, cell)
            except Exception as e:
                rep.status("抓图出错: {}".format(type(e).__name__), last_move,
                           level="error", exc=e)
                time.sleep(interval)
                continue

            # ---- 一级：只看画面变没变。没变就连 ONNX 都不碰 ----
            changed = sig_diff(sig, sig_prev) > tune["sig_thr"]
            sig_prev = sig
            # 兜底：万一面画一直在动（落子高亮闪烁、动画循环），
            # 不能让管道永远出不了结果，超过 force_after 就按当前帧强行定案。
            stale = (time.time() - last_ts) > float(tune["force_after"])

            if changed:
                still_n = 0
                if last_move is None and not stale:
                    rep.status("画面变化中…")
            else:
                still_n += 1

            # ---- 异步搜索进行中：一边等引擎，一边继续盯画面 ----
            # 必须放在稳定判定之前：画面没变时下面那个"已经定过案"的分支会
            # 直接 continue，搜索就永远推进不到头。
            if pending is not None:
                if changed:
                    # 画面变了 = 这一问问的是已经不存在的局面，停掉重来。
                    # 这正是异步的全部意义：串行时得等这次搜索跑完，才会发现
                    # 局面早就变了，那几秒纯属白搜。
                    eng.resync("画面变化，放弃进行中的搜索")
                    log.info("画面变化，放弃进行中的搜索｜FEN {}｜已搜 {:.1f}s".format(
                        pending["start_fen"], time.time() - pending["go_ts"]))
                    pending = None
                    rep.status("画面变化，重新识别…", last_move)
                    time.sleep(interval)
                    continue

                if eng.step():                 # 非阻塞：拿到 bestmove 才算完
                    mv, info = eng.take()
                    if mv:
                        last_move = emit_suggestion(pending, info, mv, rep, log)
                    else:
                        text = recover_engine(eng, log)
                        log.warn("出招失败｜{}｜FEN {}｜着法历史 {}".format(
                            text, pending["start_fen"],
                            " ".join(pending["moves"]) or "（无）"))
                        rep.status(text, last_move, level="warn")
                    pending = None
                    time.sleep(interval)
                    continue

                if eng.expired():
                    # 兜底：引擎一直不回话。按原因把它收拾回可用的状态，
                    # 别把一个坏进程留在那儿，让后面每一问都失败。
                    eng.take()
                    text = recover_engine(eng, log)
                    log.warn("出招失败｜{}｜FEN {}".format(
                        text, pending["start_fen"]))
                    rep.status(text, last_move, level="warn")
                    pending = None
                    time.sleep(interval)
                    continue

                time.sleep(interval)
                continue

            if still_n < stable and not stale:
                time.sleep(interval)
                continue

            # 这个画面已经定过案了，而且不是在兜底——没必要重复跑识别
            if not stale and sig_diff(sig, sig_done) <= tune["sig_thr"]:
                time.sleep(interval)
                continue
            if stale and changed:
                print("  画面持续变化超过 {:.0f}s，按当前帧强行定案".format(
                    float(tune["force_after"])))

            t0 = time.time()
            try:
                board = recognize(args.backend, img, pts, cell)
            except Exception as e:
                rep.status("识别出错: {}".format(type(e).__name__), last_move,
                           level="error", exc=e)
                time.sleep(interval)
                continue
            sig_done = sig
            still_n = 0
            last_ts = time.time()
            occ = {k: v[0] for k, v in board.items() if v[0] != "EMPTY"}

            if not occ:
                rep.status("画面里没读到棋子（可能还在加载）", last_move)
                time.sleep(interval)
                continue

            key = tuple(sorted(occ.items()))
            if key == last_key:
                time.sleep(interval)
                continue
            last_key = key

            # ---- 守门员：局面说不通就不许出招。宁可显示"不确定"，
            #      也不能拿错局面去问引擎——那会给出比不给更糟的建议 ----
            ok, problems = rules.validate(board)

            # 时序连续性：一步最多吃掉一个子，子力数不该骤降。
            # 判定本身在 power_drop() 里（只判降不判升，理由见那里的注释），
            # 关键是**基线只在下面"守门员过了"之后才更新**——绝不能在验之前改，
            # 否则识别崩掉时读出的坏帧（42 子）会把基线顶上去，之后每一帧
            # 正常盘面都被判「骤降」而永久卡住。
            drop = power_drop(last_count, len(occ))
            if drop:
                problems.append(drop)
            if problems:
                ok = False

            # ---- 守门员没过：这一帧不认账，连差分都不做 ----
            # （过去是让差分流先跑完再 continue，于是一个说不通的盘面照样能
            #   改动着法历史，甚至把轮次带偏。）
            fen_now = board_to_fen(board, args.side)
            if not ok:
                bad_n += 1
                rep.status("识别不确定（第 {} 次）：{}".format(
                    bad_n, "；".join(problems[:2])), last_move, level="warn",
                    detail="全部问题: {}｜FEN {}".format(
                        "；".join(problems), fen_now))
                print("  校验未过: " + "；".join(problems[:3]))
                if bad_n >= 3:
                    # 连着几帧都说不通，就别再拿旧基线卡着新画面不放了：
                    # last_count 也一起清掉，让下一帧重新立基线。
                    # 只清 last_key 的话，识别崩过一阵又恢复时，
                    # 恢复后的第一帧仍会被「骤降」判死，白等一轮。
                    last_key = None
                    last_count = None
                    bad_n = 0
                time.sleep(interval)
                continue
            bad_n = 0
            last_count = len(occ)      # 子力基线只由过了守门员的帧来立

            # ---- 差分：解释得了就是正常推进，顺带维护着法历史 ----
            my_letter = SIDE_LETTER.get(args.side, "R")
            warn = ""
            if track is None:
                # 首帧。中途进场时没有"上一帧"可比，"轮谁走"只能现定：
                # 命令行 --turn > 从局面推断（被将军的局面判得出来）> 默认我方。
                turn, why_turn, guessed = resolve_turn(
                    args.turn, rules.pieces(board), my_letter)
                track = MoveTrack(occ, turn, args.side)
                print("  起始轮次：{}（{}）".format(side_word(turn), why_turn))
                log.info("起始轮次 {}｜{}".format(side_word(turn), why_turn))
                if guessed:
                    # 猜的就得说清楚：轮次错了，浮窗给的是对方该走的着法，
                    # 用户至少要能看出来，而不是被蒙在鼓里。
                    warn = "轮次判不出来，先按「我方走」算；不对请加 --turn 指定"
                    print("  !! {}".format(warn))
            else:
                prev_turn = track.expect_first()
                kind, why, seq = rules.explain_change(
                    last_board, board, tune["max_steps"], first_side=prev_turn)
                if kind == "reset":
                    print("  检测到新的一局（回到标准开局）")
                    track = MoveTrack(occ, "R", args.side)   # 标准开局红先
                elif kind in ("one_move", "multi_move"):
                    if not track.push(seq, occ):
                        print("  着法历史与当前盘面对不上，已重置为当前局面")
                    if kind == "multi_move":
                        print("  {}（两次确认之间走了 {} 步，正常）".format(
                            why, len(seq)))
                elif kind == "same":
                    # 两帧一模一样。这不是异常——"识别连续不确定后放手重来"
                    # 会重新确认同一帧——更不能因此把历史清掉。
                    pass
                else:
                    # 差分解释不通。最可能的是**进场时轮次猜反了**：换一种轮次
                    # 再试一次，成立就地纠正；否则才当识别抖动处理。
                    alt_turn = other_letter(prev_turn)
                    alt_kind, alt_why, alt_seq = rules.explain_change(
                        last_board, board, tune["max_steps"], first_side=alt_turn)
                    if alt_kind in ("one_move", "multi_move"):
                        text = "起始轮次判错了，已按「{}先走」重新对齐".format(
                            side_word(alt_turn))
                        print("  !! {}".format(text))
                        log.warn(text + "｜" + alt_why)
                        track = MoveTrack(rules.pieces(last_board), alt_turn, args.side)
                        track.push(alt_seq, occ)
                    else:
                        warn = "识别可能不稳（{}），这一手请自行核对".format(why)
                        print("  !! 差分异常: {}".format(why))
                        log.warn("差分异常: {}｜本帧 {}｜上一帧 {}".format(
                            why, fen_of(board, prev_turn),
                            fen_of(last_board, prev_turn)))
                        track = MoveTrack(occ, prev_turn, args.side)

            fen_now = fen_of(board, track.expect_first())

            # 第一次拿到稳定盘面时，严格比一次标准开局。
            # 静态校验只能查必要条件——一个 22 子的残盘（红方缺 2 车 2 马 2 炮）
            # 物理上完全可达，静态查不出来；唯一能拦住这类错误的时机就是开局帧。
            # 注意 JJ象棋 有「棋力评测」等非标准开局模式，所以只告警不拦。
            if last_board is None:
                ok_start, start_probs = rules.check_start(board)
                if ok_start:
                    print("  起始盘面：标准开局 32 子")
                else:
                    # 接在中途进场的"轮次是猜的"后面，别把它顶掉——
                    # 中途接手正好两条会同时出现，而轮次那条更要紧。
                    text = "起始盘面不是标准开局（{}），若是中途接手请忽略".format(
                        "；".join(start_probs[:1]))
                    warn = warn + "；" + text if warn else text
                    print("  !! {}".format(text))
            last_board = board

            # 在线积累：只有过了校验的局面才配进模板库（仅 template 后端需要，
            # onnx 的模型是预训练的，不在这里积累样本）
            if args.learn and args.backend == "template":
                try:
                    G.add_samples(img, pts, cell, board)
                except Exception as e:
                    print("  样本积累失败:", e)

            start_fen, moves = track.position()

            # 轮次检查：只有"轮到我方走"才问引擎。轮到对方时问出来的着法
            # 是对方该走的，我方用不上。过去这里靠"着法历史步数的奇偶"来判，
            # 现在轮次是真实状态（中途进场也定得出来），直接看它更准。
            if track.expect_first() != my_letter:
                rep.status("轮对方走（我方已落子），等对方回招…", last_move)
                time.sleep(interval)
                continue

            # ---- 最后一道保险：FEN 的轮次标注必须和历史首手同源 ----
            # 只要两者一致，引擎就不可能判 Illegal move（它拒收的正是
            # "标着红先、却先给黑着法"这种命令）。所以这里真出问题，代价
            # 也只是白丢一段历史，绝不会再把 pikafish 打死一次。
            if not track.history_turn_ok():
                log.warn("着法历史轮次对不上，已丢弃历史｜起点 {}｜着法 {}".format(
                    start_fen, " ".join(moves) or "（无）"))
                rep.status("着法历史轮次异常，本轮按孤立局面求解",
                           last_move, level="warn")
                track = MoveTrack(occ, my_letter, args.side)
                start_fen, moves = track.position()

            # ---- 轮次和盘面对不上：这种局面不给引擎 ----
            # 合法对局里"轮到我走"时对方不可能正被将军（对方被将军就必然
            # 轮到对方走）。所以只要我方现在就能吃到对方的将，就说明轮次或
            # 识别有误。实测这种局面喂给 pikafish，它会打印一行 CRITICAL ERROR
            # 之后**自己退出**，于是后面每一问都失败，日志上只剩一串
            # "引擎没给着法"——根因被埋掉。先说清楚，别把引擎搞死。
            bad, why = rules.king_capturable(rules.pieces(board), my_letter)
            if bad:
                text = "局面与轮次不符（{}），已跳过".format(why)
                log.warn(text + "｜FEN {}｜着法历史 {} 步".format(start_fen, len(moves)))
                rep.status(text, last_move, level="warn")
                time.sleep(interval)
                continue

            # 引擎也可能在两问之间就没了（上一问被拒收、被系统杀了、内存不够）。
            # 不查一下的话，"进程没了"会一路伪装成"局面可能不合法"。
            if not eng.alive() and not eng.restart("提问前发现引擎已退出"):
                log.error("引擎重启失败，停止运行")
                rep.status("引擎起不来（详见 log/ 里的日志）", last_move, level="error")
                return 1

            rep.status(warn or "识别 {} 子，引擎计算中…".format(len(occ)), last_move)
            # 只发起、不等待，然后立刻回到循环顶部。这一手要算几秒，而这几秒
            # 主循环会继续抓图：对手要是抢先落子，下一轮就能把这次搜索取消掉，
            # 而不是像以前那样傻等它跑完，才发现局面早就变了。
            # pending 里把"出建议时要用到的东西"全部带上——等到结果的那一轮
            # 盘面可能已经翻篇，那时再回头读 board / track 就晚了。
            if not eng.start(start_fen, depth=tune["depth"],
                             movetime=tune["movetime"], moves=moves):
                # 命令没送进引擎（进程刚死 / 管道断了）。**不能**把它挂成
                # pending——那样要白等 20 秒的兜底超时才有人收拾，这一手还出不来。
                text = recover_engine(eng, log)
                log.warn("发起搜索失败｜{}｜FEN {}".format(text, start_fen))
                rep.status(text, last_move, level="warn")
                time.sleep(interval)
                continue
            pending = {"t0": t0, "go_ts": time.time(),
                       "start_fen": start_fen, "fen_now": fen_now,
                       "moves": list(moves), "board": board, "n": len(occ),
                       "warn": warn, "history": len(track.moves),
                       "movetime": tune["movetime"]}
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n停止")
        log.info("收到中断，停止")
    except Exception as e:
        # 主循环里没预料到的异常，过去会让进程静默退出：浮窗停在上一条状态上，
        # 日志里干干净净。最难查的就是这种"什么都没留下"的退出。
        log.exception("主循环异常退出", e)
        rep.status("内部异常，已退出: {}（详见 log/）".format(type(e).__name__),
                   last_move, level="error")
        return 1
    finally:
        try:
            eng.quit()
        except Exception:
            pass
        log.info("auto_coach 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
