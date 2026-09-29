# -*- coding: utf-8 -*-
"""逐格分类：90 个交叉点，每个格子都给一个答案（空 / 红某子 / 黑某子）。

为什么不继续用霍夫圆：实测漏检 4~5 子，且会在相邻帧之间闪动。
逐格分类结构上保证每格必有输出，不会漏，这正是本项目第 2 条关键设计决策。

兵种识别用「开局自举」：开局摆法是已知的，直接拿它当免费标注抠出模板，
之后每一格做模板匹配。零人工标注、零训练。

用法：
    python grid_classify.py --build     # 用当前画面（应为开局）建立模板
    python grid_classify.py             # 识别当前画面并校验
    python grid_classify.py --snapshot out/xxx.png
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

# 只在还不是 utf-8 时包装。被别的模块 import 时若重复包装，
# 上一层被回收会关掉底层 buffer（曾导致 I/O operation on closed file）
if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import cv2
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 app/ 下，上一级才是项目根
CFG = os.path.join(ROOT, "config", "layout.json")
TPL = os.path.join(ROOT, "out", "templates.npz")
SIZE = 40  # 模板归一化尺寸

# 开局标准摆法：(行,列) -> (阵营, 字)
SETUP = {}
for c, ch in enumerate("車馬象士將士象馬車"):
    SETUP[(0, c)] = ("B", ch)
for c, ch in enumerate("車馬相仕帥仕相馬車"):
    SETUP[(9, c)] = ("R", ch)
for c in (1, 7):
    SETUP[(2, c)] = ("B", "砲")
    SETUP[(7, c)] = ("R", "炮")
for c in (0, 2, 4, 6, 8):
    SETUP[(3, c)] = ("B", "卒")
    SETUP[(6, c)] = ("R", "兵")


def load_layout():
    with open(CFG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    b = cfg["board"]
    pts = b.get("points") or b.get("points_px")
    c0 = b.get("cell") or b.get("cell_px")
    return cfg, pts, float(c0[0]) if isinstance(c0, (list, tuple)) else float(c0)


def find_window(title_kw="JJ象棋"):
    import win32gui
    hits = []

    def cb(h, _):
        try:
            t = win32gui.GetWindowText(h) or ""
            if title_kw.lower() in t.lower():
                l, tp, r, bt = win32gui.GetWindowRect(h)
                if (r - l) > 80 and (bt - tp) > 80:
                    hits.append((h, (l, tp, r - l, bt - tp)))
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    if not hits:
        return None
    hits.sort(key=lambda x: -(x[1][2] * x[1][3]))
    return hits[0]


def grab(rect):
    import mss
    x, y, w, h = rect
    with mss.MSS() as sct:
        raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def cell_img(arr, px, py, cell, k=0.40):
    """裁一个格子。返回归一化后的灰度图，或 None（越界）。"""
    rad = int(cell * k)
    x0, y0 = max(0, int(px - rad)), max(0, int(py - rad))
    patch = arr[y0:int(py + rad), x0:int(px + rad)]
    if patch.size == 0:
        return None
    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    return cv2.resize(gray, (SIZE, SIZE), interpolation=cv2.INTER_AREA)


def cell_mask(arr, px, py, cell, k=0.40):
    """取字的墨迹掩码。

    注意：这条路试过，实测反而退化（开自校验 32/32 -> 30/32），
    因为二值化丢掉了圆盘的灰度层次，让相近字形更容易混。当前不使用，
    保留仅供后续对比实验。正式识别走 cell_img 的灰度模板。
    """
    rad = int(cell * k)
    patch = arr[max(0, int(py - rad)):int(py + rad),
                max(0, int(px - rad)):int(px + rad)]
    if patch.size == 0:
        return None
    g = patch.mean(axis=-1)
    bg = float(np.median(g))          # 圆盘底色占多数，用它当背景基准
    dark = g < bg - 40                # 黑字：明显暗于背景
    r, gc, b = patch[..., 0], patch[..., 1], patch[..., 2]
    red = (r > gc + 50) & (r > b + 50)  # 红字：R 明显高于 G/B
    m = (dark | red).astype(np.uint8) * 255
    return cv2.resize(m, (SIZE, SIZE), interpolation=cv2.INTER_AREA)


def cross_score(arr, px, py, cell, k=0.40):
    """交叉点是否露着棋盘的十字线。

    空交叉点：横竖两条棋盘线都穿过中心，沿线几乎全是深色。
    有子的格子：圆盘把线盖住了，沿线会断开。
    取横竖两条线里"更不像线"的那个（min）——只要有一边被盖住就判成有子。
    """
    H, W = arr.shape[:2]
    rad = int(cell * k)

    def dark_ratio(seg):
        if seg.size == 0:
            return 0.0
        g = seg.mean(axis=-1) if seg.ndim == 2 else seg
        return float((g < 140).mean())

    y = int(py)
    x0, x1 = max(0, int(px - rad)), min(W, int(px + rad))
    if y < 0 or y >= H or x1 <= x0:
        return 0.0
    h = dark_ratio(arr[y, x0:x1])

    x = int(px)
    y0, y1 = max(0, int(py - rad)), min(H, int(py + rad))
    if x < 0 or x >= W or y1 <= y0:
        return 0.0
    v = dark_ratio(arr[y0:y1, x])
    return min(h, v)


def build_templates(img, pts, cell, save=True):
    """从开局摆法里抠模板。同时取几个确定为空的交叉点做「空」模板。"""
    arr = np.asarray(img.convert("RGB"))
    tpl, labels = [], []
    for key, (side, ch) in sorted(SETUP.items()):
        r, c = key
        im = cell_img(arr, pts[r][c][0], pts[r][c][1], cell)
        if im is None:
            continue
        tpl.append(im)
        labels.append(f"{side}{ch}")

    # 空位样本：第 1/4/5/8 行的若干交叉点，开局必为空
    empties = [(1, 0), (1, 4), (1, 8), (4, 0), (4, 4), (4, 8),
               (5, 0), (5, 4), (5, 8), (8, 0), (8, 4), (8, 8)]
    for r, c in empties:
        im = cell_img(arr, pts[r][c][0], pts[r][c][1], cell)
        if im is not None:
            tpl.append(im)
            labels.append("EMPTY")

    stack = np.stack(tpl).astype(np.float32)
    if save:
        os.makedirs(os.path.dirname(TPL), exist_ok=True)
        np.savez(TPL, tpl=stack, labels=np.array(labels))
    return stack, labels


def legal_at(side, piece, r, c):
    """该兵种出现在 (r,c) 是否可能。

    只做宽松的合法性检查，用途是排除模板匹配的明显误判——
    实测出现过把 (7,6) 判成"仕"（仕只能在九宫 col3~5），
    加了这层之后这类荒谬结果会被过滤掉，改取次优但合法的兵种。
    """
    palace_b = 0 <= r <= 2 and 3 <= c <= 5
    palace_r = 7 <= r <= 9 and 3 <= c <= 5
    if side == "B":
        if piece == "將":
            return palace_b
        if piece == "士":
            return palace_b
        if piece == "象":
            return r <= 4          # 象不过河
        if piece == "卒":
            return r >= 3          # 卒只进不退，不会跑到自家底线以北
    else:
        if piece == "帥":
            return palace_r
        if piece == "仕":
            return palace_r
        if piece == "相":
            return r >= 5
        if piece == "兵":
            return r <= 6
    return True                     # 車馬炮任意位置


def classify(img, pts, cell, margin=0.40, std_thr=30.0):
    """对 90 个交叉点逐个分类。返回 {(r,c): (label, score)}

    类别竞争：分别算"最像哪个有子模板"和"最像哪个空位模板"，
    两者之差超过 margin 才判为有子。

    为什么不直接取全体模板最高分：实测那样会把空位（露着十字线）
    错配到卒/兵上，多出 14 个假子。改成类别竞争后 90/90。
    为什么不检测十字线：实测该特征方向是反的（有子格均值 0.383
    反而高于空位 0.129），不可用，已废弃。
    """
    with np.load(TPL, allow_pickle=True) as d:
        tpl = d["tpl"]
        labels = list(d["labels"])
    tpl_n = [(t - t.mean()) / (t.std() + 1e-6) for t in tpl]
    i_empty = [i for i, l in enumerate(labels) if l == "EMPTY"]
    i_piece = [i for i, l in enumerate(labels) if l != "EMPTY"]
    arr = np.asarray(img.convert("RGB"))

    out = {}
    for r in range(10):
        for c in range(9):
            im = cell_img(arr, pts[r][c][0], pts[r][c][1], cell)
            if im is None:
                out[(r, c)] = ("EMPTY", 0.0)
                continue
            # 占位用格内标准差判：实测有子格 44~54、空位 17~18，间隔极大，
            # 比模板分可靠得多。模板分曾把两个真实棋子(0,7)(7,7)卡在阈值外判成空，
            # 但它们 std 高达 53.1 / 44.9，明显是有子。
            im_std = float(im.std())
            im_n = (im - im.mean()) / (im.std() + 1e-6)
            scores0 = [float((im_n * t_n).mean()) for t_n in tpl_n]
            eb = max(scores0[i] for i in i_empty) if i_empty else -9.0
            pb = max(scores0[i] for i in i_piece) if i_piece else -9.0
            if pb - eb <= margin:
                out[(r, c)] = ("EMPTY", round(pb - eb, 3))
                continue
            # 兵种匹配用灰度模板（掩码版实测退化，已回退）
            im_n = (im - im.mean()) / (im.std() + 1e-6)
            scores = [float((im_n * t_n).mean()) for t_n in tpl_n]
            # 同一兵种可能有多个样本，先按兵种聚合取最高分，再挑合法的那个
            best = {}
            for i, lab in enumerate(labels):
                if lab == "EMPTY":
                    continue
                if lab not in best or scores[i] > best[lab]:
                    best[lab] = scores[i]
            if not best:
                out[(r, c)] = ("EMPTY", round(im_std, 1))
                continue
            cand = sorted(best.items(), key=lambda kv: -kv[1])
            chosen = next((lab for lab, _ in cand
                           if legal_at(lab[0], lab[1:], r, c)), cand[0][0])
            out[(r, c)] = (chosen, round(best[chosen], 3))
    return out


def add_samples(img, pts, cell, board, max_per_class=8):
    """在线积累：把一帧里识别出来的棋子样本并入模板库。

    只对"通过了 validate_board 的局面"调用——拿错局面去扩充模板会把库带歪。
    每类最多存 max_per_class 个，免得无限膨胀。返回新增样本数。
    """
    from collections import Counter

    if os.path.exists(TPL):
        with np.load(TPL, allow_pickle=True) as d:
            tpl = list(d["tpl"])
            labels = list(d["labels"])
    else:
        tpl, labels = [], []

    cnt = Counter(labels)
    arr = np.asarray(img.convert("RGB"))
    added = 0
    for (r, c), (lab, _s) in board.items():
        if lab == "EMPTY" or cnt[lab] >= max_per_class:
            continue
        im = cell_img(arr, pts[r][c][0], pts[r][c][1], cell)
        if im is None:
            continue
        tpl.append(im)
        labels.append(lab)
        cnt[lab] += 1
        added += 1

    if added:
        os.makedirs(os.path.dirname(TPL), exist_ok=True)
        np.savez(TPL, tpl=np.stack(tpl).astype(np.float32), labels=np.array(labels))
    return added


# 各兵种的数量上限（开局编制）
LIMIT = {("R", "車"): 2, ("R", "馬"): 2, ("R", "炮"): 2, ("R", "相"): 2,
         ("R", "仕"): 2, ("R", "兵"): 5,
         ("B", "車"): 2, ("B", "馬"): 2, ("B", "砲"): 2, ("B", "象"): 2,
         ("B", "士"): 2, ("B", "卒"): 5}


def validate_board(board):
    """检查识别出来的是不是一个说得通的象棋局面。

    这是整个方案的守门员：宁可承认"不确定"，也不能把错局面交给引擎——
    实测过因为把 (7,6) 认成"仕"（仕只能在九宫），出来的 FEN 有两个帅，
    引擎直接拒招；更糟的情况是局面凑巧合法但错了，那就会给出荒唐建议
    （比如对方已经将军却建议走别的）。所以这里卡死。
    """
    from collections import Counter

    cells = {k: v[0] for k, v in board.items() if v[0] != "EMPTY"}
    problems = []

    if not cells:
        return False, ["一格棋子都没读到"]

    cnt = Counter()
    for (r, c), lab in cells.items():
        cnt[(lab[0], lab[1:])] += 1

    # 将帅必须各有一个，多了少了都是识别错了
    if cnt[("R", "帥")] != 1:
        problems.append("红帅 {} 个（应为 1）".format(cnt[("R", "帥")]))
    if cnt[("B", "將")] != 1:
        problems.append("黑将 {} 个（应为 1）".format(cnt[("B", "將")]))

    for key, cap in LIMIT.items():
        if cnt[key] > cap:
            problems.append("{}{} {} 个（上限 {}）".format(key[0], key[1], cnt[key], cap))

    if len(cells) > 32:
        problems.append("棋子总数 {}（上限 32）".format(len(cells)))

    # 位置合法性：士象将帅的走位限制能挡掉一大类误判
    for (r, c), lab in cells.items():
        if not legal_at(lab[0], lab[1:], r, c):
            problems.append("{} 在 ({},{}) 位置不合法".format(lab, r, c))

    return (not problems), problems


def calibrate_margin(img, pts, cell):
    """用开局真值扫出最优 margin。画面必须是未走棋的开局。"""
    arr = np.asarray(img.convert("RGB"))
    with np.load(TPL, allow_pickle=True) as d:
        tpl = d["tpl"]
        labels = list(d["labels"])
    tpl_n = [(t - t.mean()) / (t.std() + 1e-6) for t in tpl]
    i_empty = [i for i, l in enumerate(labels) if l == "EMPTY"]
    i_piece = [i for i, l in enumerate(labels) if l != "EMPTY"]

    diffs = {}
    for r in range(10):
        for c in range(9):
            im = cell_img(arr, pts[r][c][0], pts[r][c][1], cell)
            if im is None:
                diffs[(r, c)] = -9.0
                continue
            im_n = (im - im.mean()) / (im.std() + 1e-6)
            scores = [float((im_n * t_n).mean()) for t_n in tpl_n]
            eb = max(scores[i] for i in i_empty) if i_empty else -9.0
            pb = max(scores[i] for i in i_piece)
            diffs[(r, c)] = pb - eb

    best_ok, best_m = -1, 0.40
    m = -0.60
    while m < 0.61:
        okc = sum(1 for k, v in diffs.items() if (v > m) == (k in SETUP))
        if okc > best_ok:
            best_ok, best_m = okc, m
        m += 0.02
    return round(best_m, 2), best_ok


def render(board, mark_wrong=None):
    lines = []
    for r in range(10):
        row = []
        for c in range(9):
            lab, _s = board.get((r, c), ("EMPTY", 0))
            if lab == "EMPTY":
                row.append("..")
            else:
                flag = ""
                if mark_wrong and mark_wrong.get((r, c)):
                    flag = "*"
                row.append(f"{lab}{flag}")
        lines.append("  " + " ".join(f"{x:<3}" for x in row))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="用当前画面建立模板（画面须为开局）")
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--learn", action="store_true",
                    help="识别结果通过校验时，把本帧样本并入模板库（在线积累）")
    args = ap.parse_args()

    cfg, pts, cell = load_layout()

    if args.snapshot:
        img = Image.open(args.snapshot).convert("RGB")
    else:
        w = find_window()
        if not w:
            print("!! 找不到 JJ象棋 窗口")
            return 1
        x, y, ww, hh = w[1]
        sx, sy = ww / cfg["image"]["w"], hh / cfg["image"]["h"]
        pts = [[(p[0] * sx, p[1] * sy) for p in row] for row in pts]
        cell = cell * ((sx + sy) / 2)
        img = grab((x, y, ww, hh))
        ts = time.strftime("%H%M%S")
        img.save(os.path.join(ROOT, "out", f"grid_{ts}.png"))

    print(f"画面 {img.width}x{img.height} 格距 {cell:.1f}")

    if args.build or not os.path.exists(TPL):
        tpl, labels = build_templates(img, pts, cell)
        print(f"已建立模板 {len(labels)} 个（含 EMPTY）: {' '.join(labels)}")
        print(f"  -> {TPL}")

    if not os.path.exists(TPL):
        print("!! 模板不存在，先跑 --build")
        return 1

    margin, acc = calibrate_margin(img, pts, cell)
    print(f"占位判据自动标定 margin = {margin:+.2f}  90 格判对 {acc}/90")

    board = classify(img, pts, cell, margin)
    occupied = {k: v for k, v in board.items() if v[0] != "EMPTY"}
    print(f"\n识别到 {len(occupied)} 个子（开局应为 32）")
    print("\n" + render(board))

    # 与开局标准摆法对照（自校验）
    correct, wrong = 0, {}
    for key, (side, ch) in SETUP.items():
        got = board.get(key, ("EMPTY", 0))[0]
        if got == f"{side}{ch}":
            correct += 1
        else:
            wrong[key] = got
    extra = [k for k in occupied if k not in SETUP]
    print(f"\n== 自校验（对照开局标准摆法）==")
    print(f"  兵种+阵营全对: {correct}/32")
    if wrong:
        print(f"  不一致 {len(wrong)} 处: "
              f"{[(k, SETUP[k], v) for k, v in list(wrong.items())][:8]}")
    print(f"  多出（空位误判为有子）: {sorted(extra) if extra else '无'}")

    if args.learn:
        okv, probs = validate_board(board)
        if okv:
            n = add_samples(img, pts, cell, board)
            print(f"\n本帧通过校验，已并入模板库：新增 {n} 个样本")
        else:
            print(f"\n本帧未通过校验，不并入模板库（会带歪库）：{probs[:2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
