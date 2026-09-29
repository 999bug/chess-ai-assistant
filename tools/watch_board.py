# -*- coding: utf-8 -*-
"""实时检测整个棋盘。

和全局架构一致：每一轮都重新抓全窗、重新识别全部棋子，不靠上一帧推算，
所以漏一帧也不会错位；连续多帧结果一致才确认，滤掉走子动画和选中高亮。

用法：
    python watch_board.py                # 一直跑，Ctrl+C 停止
    python watch_board.py --seconds 8    # 只跑 8 秒，用于验证
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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 tools/ 下，上一级才是项目根
CFG_PATH = os.path.join(ROOT, "config", "layout.json")


def load_cfg():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def find_window(title_kw="JJ象棋"):
    import win32gui
    hits = []

    def cb(h, _):
        try:
            t = win32gui.GetWindowText(h) or ""
            if title_kw.lower() in t.lower():
                l, tp, r, b = win32gui.GetWindowRect(h)
                if (r - l) > 80 and (b - tp) > 80:
                    hits.append({"hwnd": h, "title": t, "rect": (l, tp, r - l, b - tp)})
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    if not hits:
        return None
    hits.sort(key=lambda x: -(x["rect"][2] * x["rect"][3]))
    return hits[0]


def grab(rect):
    import mss
    x, y, w, h = rect
    with mss.MSS() as sct:
        raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def read_board(img, pts, cell):
    """返回 {(行,列): 'R'/'B'}。只定占位和阵营，兵种识别另做。"""
    import cv2

    arr = np.asarray(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    blur = cv2.medianBlur(gray, 5)
    circles = None
    for p2 in (30, 24, 18, 14):
        c = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, 1.0, minDist=cell * 0.6,
                             param1=100, param2=p2,
                             minRadius=int(cell * 0.26), maxRadius=int(cell * 0.56))
        if c is not None and len(c[0]) >= 12:
            circles = c
            break
    if circles is None:
        return {}

    out = {}
    for cx, cy, _r in [(float(a[0]), float(a[1]), float(a[2])) for a in circles[0]]:
        best, bd = None, 1e9
        for ri, row in enumerate(pts):
            for ci, (px, py) in enumerate(row):
                d = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
                if d < bd:
                    bd, best = d, (ri, ci)
        if best is None or bd > cell * 0.5:
            continue
        px, py = pts[best[0]][best[1]]
        rad = int(cell * 0.30)
        patch = arr[max(0, int(py - rad)):int(py + rad),
                    max(0, int(px - rad)):int(px + rad)].astype(np.int16)
        red = int(((patch[..., 0] > patch[..., 1] + 25) &
                   (patch[..., 0] > patch[..., 2] + 25)).sum())
        dark = int((patch[..., 0] < 100).sum())
        out[best] = "R" if red > dark else "B"
    return out


def render(board, rows=10, cols=9):
    lines = []
    for r in range(rows):
        lines.append("  " + " ".join(board.get((r, c), ".") for c in range(cols)))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--stable", type=int, default=2)
    args = ap.parse_args()

    cfg = load_cfg()
    b = cfg["board"]
    pts = b.get("points") or b.get("points_px")
    cell = b.get("cell") or b.get("cell_px")
    if isinstance(cell, (list, tuple)):
        cell = float(cell[0])

    w = find_window()
    if not w:
        print("!! 找不到 JJ象棋 窗口")
        return 1
    x, y, ww, hh = w["rect"]
    print(f"窗口 hwnd={w['hwnd']} ({x},{y}) {ww}x{hh}")
    # 网格按标定时的尺寸归一化，窗口缩放后仍适用
    sx, sy = ww / cfg["image"]["w"], hh / cfg["image"]["h"]
    pts = [[(p[0] * sx, p[1] * sy) for p in row] for row in pts]
    cell = cell * ((sx + sy) / 2)
    print(f"缩放系数 {sx:.3f}/{sy:.3f}，格距 {cell:.1f}\n")

    prev, pending, pending_n = {}, {}, 0
    t0 = time.time()
    interval = 1.0 / max(args.hz, 0.2)
    print("开始监测（R=红 B=黑 .=空），Ctrl+C 停止\n")
    try:
        while True:
            img = grab((x, y, ww, hh))
            board = read_board(img, pts, cell)
            cur = {k: v for k, v in sorted(board.items())}

            if cur == prev:
                pending, pending_n = {}, 0
            else:
                if cur == pending:
                    pending_n += 1
                else:
                    pending, pending_n = cur, 1
                if pending_n >= args.stable:
                    if prev:
                        moved = [k for k in set(cur) | set(prev)
                                 if cur.get(k) != prev.get(k)]
                        print(f'[{time.strftime("%H:%M:%S")}] 变化 {len(moved)} 处: '
                              f'{sorted(moved)}')
                    else:
                        print(f'[{time.strftime("%H:%M:%S")}] 初始局面，共 {len(cur)} 子')
                    print(render(cur))
                    print()
                    prev, pending, pending_n = cur, {}, 0

            if args.seconds and (time.time() - t0) > args.seconds:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    print("停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
