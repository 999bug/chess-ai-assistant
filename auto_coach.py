# -*- coding: utf-8 -*-
"""自动教练后台进程：盯着棋盘，你走完一步就自动算出下一手。

和全局架构一致：
- 每轮重新抓全窗、重新识别整盘，不靠上一帧推算，漏一帧不会错位
- 局面连续多帧一致才认账，滤掉走子动画和加载中的中间态
- 引擎常驻，不每步重启

它只负责算，结果写进 out/suggestion.json；浮窗由 hud.py 读这个文件显示
（之所以拆两个进程：隔离环境没带 tkinter，而系统 Python 有，
 让系统 Python 只画 UI 就不用往它身上装任何包）。

用法：
    python auto_coach.py                       # 默认红方，深度 14
    python auto_coach.py --side black
    python auto_coach.py --depth 18 --interval 1.0
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

import grid_classify as G
import rules
from coach import Engine, board_to_fen, move_to_chinese, parse_score

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "out", "suggestion.json")


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="red", choices=["red", "black"])
    ap.add_argument("--depth", type=int, default=14)
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--stable", type=int, default=3)
    ap.add_argument("--backend", default="onnx", choices=["onnx", "template"],
                    help="识别后端：onnx=整板分类（默认），template=老的逐格模板匹配")
    ap.add_argument("--no-learn", dest="learn", action="store_false",
                    help="关闭在线样本积累（默认开启，仅 template 后端有效）")
    args = ap.parse_args()

    if not os.path.exists(os.path.join(HERE, "engine", "pikafish.exe")):
        print("!! 引擎不存在：engine/pikafish.exe")
        return 1
    if args.backend == "onnx":
        model = os.path.join(HERE, "models", "layout_nano.onnx")
        if not os.path.exists(model):
            print("!! 整板识别模型不存在：models/layout_nano.onnx")
            print("   运行 python download_models.py 下载（需要能访问 HuggingFace）")
            return 1
        # 先把模型加载好。一是加载失败要立刻失败而不是等第一帧，
        # 二是别让第一帧多等一秒多的首次加载时间
        try:
            import board_onnx
            board_onnx.session()
            print("整板识别模型已加载")
        except Exception as e:
            print(f"!! 整板识别模型加载失败: {type(e).__name__}: {e}")
            return 1
    else:
        if not os.path.exists(os.path.join(HERE, "out", "templates.npz")):
            print("!! 模板不存在：先跑 python grid_classify.py --build（画面须为开局）")
            return 1

    try:
        eng = Engine()
        eng.init()
    except Exception as e:
        print(f"!! 引擎启动失败: {e}")
        return 1
    print(f"引擎就绪，识别后端 {args.backend}，"
          f"开始监测（{'红' if args.side=='red' else '黑'}方，深度 {args.depth}）")

    cfg, pts0, cell0 = G.load_layout()
    last_key, pending_key, pending_n = None, None, 0
    last_move = None
    last_count = None      # 上一次通过校验时的子力数，用于连续性检查
    last_board = None      # 上一次通过校验的盘面，用于着法差分
    bad_n = 0          # 连续校验失败次数
    write({"ts": time.time(), "status": "启动中…", "move": None})

    try:
        while True:
            w = G.find_window()
            if not w:
                write({"ts": time.time(),
                       "status": "找不到 JJ象棋 窗口（应用宝失焦会隐藏，把窗口点出来）",
                       "move": last_move})
                time.sleep(args.interval)
                continue

            x, y, ww, hh = w[1]
            sx, sy = ww / cfg["image"]["w"], hh / cfg["image"]["h"]
            pts = [[(p[0] * sx, p[1] * sy) for p in row] for row in pts0]
            cell = cell0 * ((sx + sy) / 2)

            try:
                img = G.grab((x, y, ww, hh))
                board = recognize(args.backend, img, pts, cell)
                occ = {k: v[0] for k, v in board.items() if v[0] != "EMPTY"}
            except Exception as e:
                write({"ts": time.time(), "status": f"抓图/识别出错: {type(e).__name__}",
                       "move": last_move})
                time.sleep(args.interval)
                continue

            if not occ:
                write({"ts": time.time(), "status": "画面里没读到棋子（可能还在加载）",
                       "move": last_move})
                time.sleep(args.interval)
                continue

            key = tuple(sorted(occ.items()))
            if key == last_key:
                pending_key, pending_n = None, 0
                time.sleep(args.interval)
                continue

            if key == pending_key:
                pending_n += 1
            else:
                pending_key, pending_n = key, 1

            if pending_n < args.stable:
                write({"ts": time.time(),
                       "status": f"局面变化，确认中 {pending_n}/{args.stable}",
                       "move": last_move})
                time.sleep(args.interval)
                continue

            last_key = key
            pending_key, pending_n = None, 0

            # 守门员：局面说不通就不许出招。宁可显示"不确定"，
            # 也不能拿错局面去问引擎——那会给出比不给更糟的建议
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

            # 着法差分：两帧之间必须只差一个合法着法。
            # 这条比"连续 N 帧一致"更强的点在于——它能识别出"稳定但不可能"的盘面：
            # 连续 3 帧都一致，也可能是一致的错。而一个子凭空消失/多出、
            # 或者一次动了 9 处，都说明中途出了问题。
            warn = ""
            if last_board is not None:
                kind, why = rules.diff(last_board, board)
                if kind == "reset":
                    print("  检测到新的一局（回到标准开局）")
                elif kind == "one_move":
                    print(f"  {why}")
                elif kind == "noisy":
                    warn = "盘面与上一手对不上（{}），请核对".format(why)
                    print(f"  !! 差分异常: {why}")

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
                time.sleep(args.interval)
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
                    print(f"  !! {warn}")
            last_board = board

            # 在线积累：只有过了校验的局面才配进模板库。
            # 这样下得越久，见过的棋形越多，识别越稳。
            # 仅 template 后端需要——onnx 后端的模型是预训练的，不在这里积累样本。
            if args.learn and args.backend == "template":
                try:
                    G.add_samples(img, pts, cell, board)
                except Exception as e:
                    print("  样本积累失败:", e)

            fen = board_to_fen(board, args.side)
            write({"ts": time.time(),
                   "status": warn or f"识别 {len(occ)} 子，引擎计算中…",
                   "move": last_move})
            mv, info = eng.bestmove(fen, depth=args.depth)
            if not mv:
                write({"ts": time.time(), "status": "引擎没给着法（局面可能不合法）",
                       "move": last_move})
                continue

            sc, d = parse_score(info)
            cn = move_to_chinese(board, mv)
            last_move = {"cn": cn, "mv": mv, "score": sc, "depth": d,
                         "fen": fen, "n": len(occ)}
            write({"ts": time.time(), "status": warn, "move": last_move})
            print(f"[{time.strftime('%H:%M:%S')}] {cn}  [{mv}]  "
                  f"评分 {sc/100 if sc is not None else 0:+.2f}  深度 {d}  ({len(occ)} 子)")
            if warn:
                print(f"  !! {warn}")

            time.sleep(args.interval)
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
