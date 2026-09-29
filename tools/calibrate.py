# -*- coding: utf-8 -*-
"""一次性标定：模糊的记忆到此为止，脚本直接从当前屏幕重算坐标。

流程：
  1. 枚举所有窗口（含隐藏/最小化），找 JJ象棋，还原并置前
  2. 用多种方式各抓一张，自动挑出不是黑屏的那张
  3. 棋盘：优先用直线检测找格线，格线不可靠时用棋子圆心反推网格
  4. 棋谱：OCR 全图，命中记谱格式的文本聚成一簇，取外接矩形
  5. 结果写进 config/layout.json，坐标全部相对窗口左上角

用法：
    python calibrate.py
    python calibrate.py --title JJ象棋 --show
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
import win32con
import win32gui
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 tools/ 下，上一级才是项目根
OUT_DIR = os.path.join(ROOT, "out")
CFG_DIR = os.path.join(ROOT, "config")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(CFG_DIR, exist_ok=True)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def find_window(title="JJ象棋", prefer_proc="Androws.exe"):
    """返回最匹配的一个顶层窗口 dict；找不到返回 None。"""
    rows = []

    def cb(h, _):
        try:
            t = win32gui.GetWindowText(h) or ""
            cls = win32gui.GetClassName(h) or ""
            l, tp, r, b = win32gui.GetWindowRect(h)
            w, hh = r - l, b - tp
            _, pid = __import__("win32process").GetWindowThreadProcessId(h)
            rows.append({
                "hwnd": h, "title": t, "cls": cls, "rect": (l, tp, w, hh),
                "pid": pid,
                "visible": bool(win32gui.IsWindowVisible(h)),
                "iconic": bool(ctypes.windll.user32.IsIconic(h)),
            })
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)

    cand = [r for r in rows if title.lower() in (r["title"] or "").lower() and r["rect"][2] > 80]
    if not cand:
        return None
    cand.sort(key=lambda r: (not r["visible"], -(r["rect"][2] * r["rect"][3])))
    return cand[0]


def activate(hwnd):
    u = ctypes.windll.user32
    if u.IsIconic(hwnd):
        _safe(lambda: win32gui.ShowWindow(hwnd, win32con.SW_RESTORE))
        time.sleep(0.35)
    try:
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.2)
    except Exception:
        pass
    fg = _safe(win32gui.GetForegroundWindow)
    return fg == hwnd


def guess_snapshot(hwnd, rect):
    """多种方式各抓一张，自动挑最亮的（黑屏的一律丢弃）。"""
    x, y, w, h = rect
    results = []

    def try_mss():
        import mss
        with mss.MSS() as sct:
            raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def try_printwindow():
        u = ctypes.windll.user32
        import win32ui
        dc = win32gui.GetWindowDC(hwnd)
        mfc = win32ui.CreateDCFromHandle(dc)
        save = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        try:
            bmp.CreateCompatibleBitmap(mfc, w, h)
            save.SelectObject(bmp)
            u.PrintWindow(hwnd, save.GetSafeHdc(), 2)
            info = bmp.GetInfo()
            return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                    bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1).copy()
        finally:
            for step in (lambda: win32gui.DeleteObject(bmp.GetHandle()), save.DeleteDC,
                         mfc.DeleteDC, lambda: win32gui.ReleaseDC(hwnd, dc)):
                _safe(step)

    def try_dxcom():
        import dxcom
        cam = dxcom.create(output_color="RGB")
        frame = None
        for _ in range(20):
            frame = cam.grab(region=(x, y, x + w, y + h))
            if frame is not None:
                break
            time.sleep(0.05)
        if frame is None:
            raise RuntimeError("no frame")
        return Image.fromarray(frame)

    for name, fn in [("mss", try_mss), ("printwindow", try_printwindow), ("dxcom", try_dxcom)]:
        img = _safe(fn)
        if img is None:
            print(f"  [{name}] 失败")
            continue
        arr = np.asarray(img.convert("RGB"))
        score = float(arr.std())
        ok = score > 12
        print(f"  [{name}] {img.width}x{img.height} 标准差={score:.1f} -> {'可用' if ok else '内容不足'}")
        results.append((score, name, img))
    if not results:
        return None, None
    results.sort(key=lambda t: -t[0])
    score, name, img = results[0]
    return img, name


def cluster_1d(vals, tol):
    vals = sorted(vals)
    groups = []
    for v in vals:
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [float(np.mean(g)) for g in groups]


def detect_grid_by_lines(arr):
    """用直线检测找棋盘格线。返回 (x0,y0,x1,y1,cell_w,cell_h) 或 None。"""
    import cv2
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=int(min(w, h) * 0.35),
                            minLineLength=int(min(w, h) * 0.35), maxLineGap=6)
    if lines is None:
        return None
    horiz, vert = [], []
    for ln in lines.reshape(-1, 4):
        x1, y1, x2, y2 = [float(v) for v in ln]
        if abs(y2 - y1) < 3 and abs(x2 - x1) > w * 0.45:
            horiz.append((y1 + y2) / 2)
        elif abs(x2 - x1) < 3 and abs(y2 - y1) > h * 0.3:
            vert.append((x1 + x2) / 2)
    if len(horiz) < 6 or len(vert) < 6:
        return None
    hy = cluster_1d(horiz, tol=max(4.0, h * 0.02))
    vx = cluster_1d(vert, tol=max(4.0, w * 0.02))
    if len(hy) < 6 or len(vx) < 6:
        return None
    dy = np.median(np.diff(sorted(hy))) if len(hy) > 1 else 0
    dx = np.median(np.diff(sorted(vx))) if len(vx) > 1 else 0
    if dx <= 0 or dy <= 0:
        return None
    x0, x1 = float(min(vx)), float(max(vx))
    y0, y1 = float(min(hy)), float(max(hy))
    return x0, y0, x1, y1, float(dx), float(dy)


def detect_grid_by_circles(arr):
    """棋子是圆形。用圆心反推网格，兜直线检测失败的情况。"""
    import cv2
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    min_r = max(6, int(min(w, h) * 0.020))
    max_r = max(min_r + 4, int(min(w, h) * 0.055))
    gray_blur = cv2.medianBlur(gray, 5)
    circles = None
    for p2 in (28, 22, 18, 14):
        circles = cv2.HoughCircles(gray_blur, cv2.HOUGH_GRADIENT, dp=1.0,
                                   minDist=min_r * 1.6, param1=100, param2=p2,
                                   minRadius=min_r, maxRadius=max_r)
        if circles is not None and len(circles[0]) >= 8:
            break
        circles = None
    if circles is None:
        return None, []
    pts = [(float(c[0]), float(c[1]), float(c[2])) for c in circles[0]]
    if len(pts) < 8:
        return None, pts
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    tol_x = max(6.0, (max(xs) - min(xs)) / 20.0)
    tol_y = max(6.0, (max(ys) - min(ys)) / 22.0)
    vx = cluster_1d(xs, tol_x)
    vy = cluster_1d(ys, tol_y)
    if len(vx) < 5 or len(vy) < 5:
        return None, pts
    dx = float(np.median(np.diff(sorted(vx)))) if len(vx) > 1 else 0
    dy = float(np.median(np.diff(sorted(vy)))) if len(vy) > 1 else 0
    if dx <= 0 or dy <= 0:
        return None, pts
    return (float(min(vx)), float(min(vy)), float(max(vx)), float(max(vy)), dx, dy), pts


def fit_square_grid(pts, tol_ratio=0.35):
    """象棋棋盘是正方形格。若行/列间距不相等，说明聚类漏了行。

    改为固定 cell = 列间距，然后在候选原点里搜索，使尽量多的棋子圆心落在网格交点上。
    这既修正了漏行，又天然做了一次一致性校验：命中率低就说明这个网格不可信。
    """
    if len(pts) < 6:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    vx = cluster_1d(xs, max(6.0, (max(xs) - min(xs)) / 20.0))
    dx = float(np.median(np.diff(sorted(vx)))) if len(vx) > 1 else 0.0
    if dx <= 0:
        return None
    dy = dx
    tol = tol_ratio
    x0, y0 = min(xs), min(ys)
    best = None
    i = 0.0
    while i <= dx:
        cx = x0 + i
        j = 0.0
        while j <= dy:
            cy = y0 + j
            hit = 0
            for px, py, _r in pts:
                c = (px - cx) / dx
                r = (py - cy) / dy
                if abs(c - round(c)) <= tol and abs(r - round(r)) <= tol \
                        and -0.5 <= round(c) <= 8.5 and -0.5 <= round(r) <= 9.5:
                    hit += 1
            if best is None or hit > best[0]:
                best = (hit, cx, cy)
            j += 0.5
        i += 0.5
    hit, cx, cy = best
    return (cx, cy, cx + 8 * dx, cy + 9 * dy, dx, dy), hit, len(pts)


def detect_notation(arr, ocr_items, board_box):
    """在 OCR 结果里找棋谱区：优先命中记谱格式的文本，否则取棋盘下方最密的一簇。"""
    hits = []
    try:
        import notation
        for it in ocr_items:
            if notation.parse_move(it["text"]):
                hits.append(it)
    except Exception:
        pass
    if not hits:
        return None, []
    xs1 = min(i["x"] for i in hits)
    ys1 = min(i["y"] for i in hits)
    xs2 = max(i["x"] + i["w"] for i in hits)
    ys2 = max(i["y"] + i["h"] for i in hits)
    pad = 6
    return (max(0, xs1 - pad), max(0, ys1 - pad),
            xs2 - xs1 + pad * 2, ys2 - ys1 + pad * 2), hits


def build_points(x0, y0, dx, dy, cols=9, rows=10):
    pts = []
    for r in range(rows):
        row = []
        for c in range(cols):
            row.append([round(x0 + c * dx, 1), round(y0 + r * dy, 1)])
        pts.append(row)
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="JJ象棋")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    print("== 1. 定位窗口 ==")
    w = find_window(args.title)
    if not w:
        print(f"!! 找不到标题含 {args.title!r} 的窗口")
        return 1
    x, y, ww, hh = w["rect"]
    print(f'  hwnd={w["hwnd"]} rect=({x},{y}) {ww}x{hh} visible={w["visible"]} iconic={w["iconic"]}')

    locked = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
    if locked:
        ctypes.windll.user32.CloseDesktop(locked)
    else:
        print("  !! 屏幕锁定中，截图只会是壁纸")

    print("== 2. 置前并抓图 ==")
    ok = activate(w["hwnd"])
    if not ok:
        print("  (未能抢占前台，改用 PrintWindow 离屏渲染)")
    time.sleep(0.5)
    img, method = guess_snapshot(w["hwnd"], (x, y, ww, hh))
    if img is None:
        print("!! 所有抓图方式都失败")
        return 1
    ts = time.strftime("%H%M%S")
    snap = os.path.join(OUT_DIR, f"snapshot_{ts}.png")
    img.save(snap)
    print(f"  采用 [{method}] -> {snap}")

    arr = np.asarray(img.convert("RGB"))
    W, H = img.size

    print("== 3. 棋盘：直线检测 ==")
    grid = detect_grid_by_lines(arr)
    note = "lines"
    circles = []
    if grid is None:
        print("  直线检测未找到足够格线，改用棋子圆心反推")
        grid, circles = detect_grid_by_circles(arr)
        note = "circles"
        if grid and circles:
            gx0, gy0, gx1, gy1, gdx, gdy = grid
            # 棋盘必须是正方形格，行列间距不相等就说明聚类漏行了
            if abs(gdx - gdy) > 0.15 * max(gdx, gdy):
                fitted = fit_square_grid(circles)
                if fitted:
                    grid, hit, total = fitted
                    note = "circles+square-fit"
                    print(f"  行/列间距不一致({gdx:.1f} vs {gdy:.1f})，改用正方形网格拟合")
                    print(f"  {hit}/{total} 个棋子圆心落在拟合网格交点上")
    if grid is None:
        print("!! 棋盘未能自动定位")
        return 1
    x0, y0, x1, y1, dx, dy = grid
    print(f"  方式={note} 网格左上角=({x0:.1f},{y0:.1f}) 右下=({x1:.1f},{y1:.1f}) "
          f"格距=({dx:.1f},{dy:.1f})")
    pts = build_points(x0, y0, dx, dy)

    print("== 4. 棋谱：OCR ==")
    try:
        import ocrutil
        items = ocrutil.recognize(img, min_score=0.55, scale=2)
    except Exception as e:
        print(f"  OCR 失败: {e}")
        items = []
    print(f"  OCR 得到 {len(items)} 条文本")
    notation_box, notation_hits = detect_notation(arr, items, None)
    if notation_box:
        print(f"  棋谱区=({notation_box[0]:.0f},{notation_box[1]:.0f}) "
              f"{notation_box[2]:.0f}x{notation_box[3]:.0f}，命中 {len(notation_hits)} 条记谱")
    else:
        print("  未在画面中命中记谱格式文本（可能还没开局，或需要 --title 确认窗口）")

    cfg = {
        "window": {"hwnd": w["hwnd"], "title": w["title"], "cls": w["cls"],
                   "pid": w["pid"], "rect": [x, y, ww, hh]},
        "capture": {"method": method, "note": "坐标为相对于 GetWindowRect 左上角的像素"},
        "image": {"w": W, "h": H, "snapshot": os.path.basename(snap)},
        "board": {
            "detect": note,
            "origin": [round(x0, 1), round(y0, 1)],
            "end": [round(x1, 1), round(y1, 1)],
            "cell": [round(dx, 2), round(dy, 2)],
            "cols": 9, "rows": 10,
            "rect": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)],
            "points": pts,
        },
        "notation": (
            None if not notation_box else
            {"rect": [round(v, 1) for v in notation_box],
             "hits": [h["text"] for h in notation_hits][:40]}
        ),
        "calibrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    cfg_path = os.path.join(CFG_DIR, "layout.json")
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"\n== 完成 ==\n配置文件: {cfg_path}")

    if args.show:
        try:
            import cv2
            vis = arr.copy()
            for r_i, row in enumerate(pts):
                for c_i, (px, py) in enumerate(row):
                    cv2.circle(vis, (int(px), int(py)), 3, (0, 0, 255), -1)
            if notation_box:
                bx, by, bw, bh = [int(v) for v in notation_box]
                cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
            outp = os.path.join(OUT_DIR, f"calib_vis_{ts}.png")
            cv2.imwrite(outp, cv2.cvtColor(vis, cv2.COLOR_RGB2BGR))
            print(f"可视化: {outp}")
        except Exception as e:
            print(f"  可视化失败: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
