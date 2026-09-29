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
import subprocess
import sys
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

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "engine", "pikafish.exe")

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
    rows = []
    for r in range(10):
        s, empty = "", 0
        for c in range(9):
            lab = board.get((r, c), ("EMPTY", 0))[0]
            if lab == "EMPTY":
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
    """极简 UCCI 客户端。"""

    def __init__(self, path=ENGINE):
        if not os.path.exists(path):
            raise FileNotFoundError(f"引擎不存在: {path}")
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1,
                                  encoding="utf-8", errors="replace")

    def send(self, cmd):
        self.p.stdin.write(cmd + "\n")
        self.p.stdin.flush()

    def wait_for(self, token, timeout=30):
        end = time.time() + timeout
        lines = []
        while time.time() < end:
            line = self.p.stdout.readline()
            if not line:
                break
            line = line.strip()
            lines.append(line)
            if token in line:
                return lines
        return lines

    def init(self):
        # Pikafish 说的是 UCI 不是 UCCI：发 ucci 会被当成未知命令，
        # 实测就是这样，别再写错
        self.send("uci")
        self.wait_for("uciok")
        self.send("setoption name Threads value 4")
        self.send("isready")
        self.wait_for("readyok")

    def bestmove(self, fen, depth=12, movetime=None):
        self.send(f"position fen {fen}")
        cmd = f"go depth {depth}" if movetime is None else f"go movetime {movetime}"
        self.send(cmd)
        info = []
        end = time.time() + 60
        while time.time() < end:
            line = self.p.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("bestmove"):
                parts = line.split()
                mv = parts[1] if len(parts) > 1 else None
                return mv, info
            if line.startswith("info"):
                info.append(line)
        return None, info

    def quit(self):
        try:
            self.send("quit")
        except Exception:
            pass


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="red", choices=["red", "black"])
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--movetime", type=int, default=None)
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
        img_path = os.path.join(HERE, "out", f"coach_{time.strftime('%H%M%S')}.png")
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

    if args.no_engine:
        return 0

    try:
        eng = Engine()
        eng.init()
    except FileNotFoundError as e:
        print(f"\n!! {e}")
        print("   引擎还没就位，先用 --no-engine 看 FEN")
        return 1

    mv, info = eng.bestmove(fen, depth=args.depth, movetime=args.movetime)
    eng.quit()
    if not mv:
        print("!! 引擎没给着法")
        return 1

    sc, d = parse_score(info)
    cn = move_to_chinese(board, mv)
    print(f"\n{'='*40}")
    print(f"建议（{'红' if args.side=='red' else '黑'}方）：{cn}    [{mv}]")
    if sc is not None:
        red_view = sc if args.side == "red" else -sc
        print(f"引擎评分：{sc/100:+.2f}（正数{'红' if args.side=='red' else '黑'}优）  深度 {d}")
    print("=" * 40)
    return 0


if __name__ == "__main__":
    sys.exit(main())
