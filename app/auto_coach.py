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
from coach import Engine, board_to_fen, move_to_chinese, move_to_ucci, parse_score

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 app/ 下，上一级才是项目根
OUT_DIR = os.path.join(ROOT, "out")
OUT_JSON = os.path.join(OUT_DIR, "suggestion.json")
# 配置文件放 config/ 而不是 out/：out/ 整个目录是运行期产物（截图、建议），
# 被 .gitignore 挡着，配置放里面换台机器就丢了。
TUNE_PATH = os.path.join(ROOT, "config", "tune.json")
TUNE_LEGACY = os.path.join(OUT_DIR, "tune.json")   # 老位置，仅用于一次性迁移

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


class MoveTrack:
    """维护"起点局面 + 之后走过的着法"，让引擎看得到对局历史。

    为什么需要：只丢一个孤立 FEN，引擎不知道之前下过什么，就判断不了
    重复局面（三次重复算和棋）。结果该求和的盘它当优势继续磨，
    能逼和的时候又看不出来。

    安全第一：每次追加着法都会在本地重放一遍，只有重放结果与当前识别盘面
    完全一致才接受；一旦对不上（识别抖动、差分解释错），就地重置为当前局面。
    丢掉历史顶多少一个信息，把错的历史喂给引擎才是真出事——
    引擎会基于一个不存在的局面给出建议。
    """

    def __init__(self, start_fen, start_pos):
        self.start_fen = start_fen
        self.moves = []
        self.pos = dict(start_pos)

    def reset(self, fen, pos):
        self.start_fen = fen
        self.moves = []
        self.pos = dict(pos)

    def push(self, seq, cur_pos, cur_fen):
        """追加若干步着法。对不上就重置，返回是否成功保留历史。"""
        work = dict(self.pos)
        for frm, to in seq:
            legal, _why = rules.move_legal(work, frm, to)
            if not legal:
                self.reset(cur_fen, cur_pos)
                return False
            work[to] = work.pop(frm)
        if work != cur_pos:
            self.reset(cur_fen, cur_pos)
            return False
        self.moves.extend(move_to_ucci(f, t) for f, t in seq)
        self.pos = work
        return True

    def position(self, cur_fen):
        """返回交给引擎的 (起点 fen, 着法列表)。没历史时退化成孤立局面。"""
        if not self.moves:
            return cur_fen, []
        return self.start_fen, self.moves


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="red", choices=["red", "black"])
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

    if not os.path.exists(os.path.join(ROOT, "engine", "pikafish.exe")):
        print("!! 引擎不存在：engine/pikafish.exe")
        return 1
    if args.backend == "onnx":
        model = os.path.join(ROOT, "models", "layout_nano.onnx")
        if not os.path.exists(model):
            print("!! 整板识别模型不存在：models/layout_nano.onnx")
            print("   运行 python download_models.py 下载（需要能访问 HuggingFace）")
            return 1
        # 先把模型加载好。一是加载失败要立刻失败而不是等第一帧，
        # 二是别让第一帧多扛一次一秒多的首次加载
        try:
            import board_onnx
            board_onnx.session()
            print("整板识别模型已加载")
        except Exception as e:
            print(f"!! 整板识别模型加载失败: {type(e).__name__}: {e}")
            return 1
    else:
        if not os.path.exists(os.path.join(OUT_DIR, "templates.npz")):
            print("!! 模板不存在：先跑 python grid_classify.py --build（画面须为开局）")
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

    write({"ts": time.time(), "status": "启动中…", "move": None})

    try:
        while True:
            # 运行期调参：文件变了就热更新（引擎选项不需要重启进程）
            if tune.reload():
                eng.apply_options(hash_mb=tune["hash_mb"], threads=tune["threads"])
                print("  运行参数已更新：movetime={} depth={} hash={}MB threads={} "
                      "interval={} stable={}".format(
                          tune["movetime"], tune["depth"], eng.hash_mb, eng.threads,
                          tune["interval"], tune["stable"]))

            interval = max(0.02, float(tune["interval"]))
            stable = max(1, int(tune["stable"]))

            rect = window_rect(hwnd) if hwnd else None
            if not rect:
                w = G.find_window()
                if not w:
                    hwnd = None
                    write({"ts": time.time(),
                           "status": "找不到 JJ象棋 窗口（应用宝失焦会隐藏，把窗口点出来）",
                           "move": last_move})
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
                write({"ts": time.time(),
                       "status": "抓图出错: {}".format(type(e).__name__),
                       "move": last_move})
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
                    write({"ts": time.time(), "status": "画面变化中…", "move": None})
            else:
                still_n += 1

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
                write({"ts": time.time(),
                       "status": "识别出错: {}".format(type(e).__name__),
                       "move": last_move})
                time.sleep(interval)
                continue
            sig_done = sig
            still_n = 0
            last_ts = time.time()
            occ = {k: v[0] for k, v in board.items() if v[0] != "EMPTY"}

            if not occ:
                write({"ts": time.time(),
                       "status": "画面里没读到棋子（可能还在加载）", "move": last_move})
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
            # 实测过识别从 32 子掉到 22 子（马炮整批漏检）却仍然"结构合法"，
            # 于是引擎基于残缺局面给了荒唐建议。这一条专门堵这个洞。
            n_now = len(occ)
            if last_count is not None:
                if n_now > last_count + 2:
                    last_count = n_now          # 子力变多，当作新开一局
                elif last_count - n_now > 2:
                    problems.append(
                        "子力 {} -> {} 骤降，疑似漏检".format(last_count, n_now))
            if problems:
                ok = False

            fen_now = board_to_fen(board, args.side)

            # ---- 差分：解释得了就是正常推进，顺带维护着法历史 ----
            warn = ""
            if last_board is None:
                track = MoveTrack(fen_now, occ)
            else:
                kind, why, seq = rules.explain_change(
                    last_board, board, tune["max_steps"])
                if kind == "reset":
                    print("  检测到新的一局（回到标准开局）")
                    track = MoveTrack(fen_now, occ)
                elif kind in ("one_move", "multi_move"):
                    if track is None:
                        track = MoveTrack(fen_now, occ)
                    elif not track.push(seq, occ, fen_now):
                        print("  着法历史与当前盘面对不上，已重置为当前局面")
                    if kind == "multi_move":
                        print("  {}（两次确认之间走了 {} 步，正常）".format(
                            why, len(seq)))
                else:
                    warn = "识别可能不稳（{}），这一手请自行核对".format(why)
                    print("  !! 差分异常: {}".format(why))
                    track = MoveTrack(fen_now, occ)

            if not ok:
                bad_n += 1
                write({"ts": time.time(),
                       "status": "识别不确定（第 {} 次）：{}".format(
                           bad_n, "；".join(problems[:2])),
                       "move": last_move})
                print("  校验未过: " + "；".join(problems[:3]))
                if bad_n >= 3:
                    last_key = None   # 放手，等下一轮重新确认
                    bad_n = 0
                time.sleep(interval)
                continue
            bad_n = 0
            last_count = len(occ)

            # 第一次拿到稳定盘面时，严格比一次标准开局。
            # 静态校验只能查必要条件——一个 22 子的残盘（红方缺 2 车 2 马 2 炮）
            # 物理上完全可达，静态查不出来；唯一能拦住这类错误的时机就是开局帧。
            # 注意 JJ象棋 有「棋力评测」等非标准开局模式，所以只告警不拦。
            if last_board is None:
                ok_start, start_probs = rules.check_start(board)
                if ok_start:
                    print("  起始盘面：标准开局 32 子")
                else:
                    warn = "起始盘面不是标准开局（{}），若是中途接手请忽略".format(
                        "；".join(start_probs[:1]))
                    print("  !! {}".format(warn))
            last_board = board

            # 在线积累：只有过了校验的局面才配进模板库（仅 template 后端需要，
            # onnx 的模型是预训练的，不在这里积累样本）
            if args.learn and args.backend == "template":
                try:
                    G.add_samples(img, pts, cell, board)
                except Exception as e:
                    print("  样本积累失败:", e)

            start_fen, moves = track.position(fen_now)

            # 轮次检查：起点 FEN 标的是"我方走"，所以着法数是奇数就意味着
            # 我方刚落子、现在轮到对方（AI 正在思考）。这时问引擎没有意义——
            # 引擎会正确推断出该对方走，给出来的建议我方根本用不上。
            # （老版本把 FEN 一律标成"我方走"，这种时刻就会给出一份
            #   基于错误轮次的建议，比不给更糟。）
            if len(moves) % 2 == 1:
                write({"ts": time.time(),
                       "status": "轮对方走（我方已落子），等对方回招…",
                       "move": last_move})
                time.sleep(interval)
                continue

            write({"ts": time.time(),
                   "status": warn or "识别 {} 子，引擎计算中…".format(len(occ)),
                   "move": last_move})

            if tune["depth"]:
                mv, info = eng.bestmove(start_fen, depth=tune["depth"], moves=moves)
            else:
                mv, info = eng.bestmove(start_fen, movetime=tune["movetime"],
                                        moves=moves)
            if not mv:
                write({"ts": time.time(), "status": "引擎没给着法（局面可能不合法）",
                       "move": last_move})
                continue

            sc, d = parse_score(info)
            cn = move_to_chinese(board, mv)
            last_move = {"cn": cn, "mv": mv, "score": sc, "depth": d,
                         "fen": fen_now, "n": len(occ),
                         "history": len(track.moves), "movetime": tune["movetime"]}
            write({"ts": time.time(), "status": warn, "move": last_move})
            print("[{}] {}  [{}]  评分 {:+.2f}  深度 {}  "
                  "({} 子, 历史 {} 步, 本轮 {:.2f}s)".format(
                      time.strftime("%H:%M:%S"), cn, mv,
                      sc / 100 if sc is not None else 0, d, len(occ),
                      len(track.moves), time.time() - t0))
            if warn:
                print("  !! {}".format(warn))

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n停止")
    finally:
        try:
            eng.quit()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
