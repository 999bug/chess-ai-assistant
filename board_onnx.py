# -*- coding: utf-8 -*-
"""整板识别：把棋盘拉正，再用一个 ONNX 模型一次性判出 10x9 全部格子。

为什么换掉逐格模板匹配（grid_classify.py 那套）：
  模板匹配每格各自为政，一格的成败不依赖别处，看上去很"独立"，实际很脆——
  画面里棋子一旦被选中高亮、被走子动画覆盖、被落子提示挡住，该格形状就变了，
  匹配直接给错答案甚至给"空"。而棋盘是一个整体：90 个格子共享同一套棋子外观。
  把整张拉正后的棋盘一次性喂给分类网络，网络能同时看到全部格子，用"盘面长什么样"
  这个全局信息来判每一格，结构上就不会出现"某一格塌掉"。

  这正是开源界的共识做法（见 README/文档里的方案对比）：
  先定位棋盘四点、透视拉正，再做 10x9x16 的多区域分类。

棋盘四点从哪来：不需要额外的关键点模型。layout.json 里已经存了 90 个交叉点坐标，
  取四个角（左上/右上/左下/右下）就是棋盘四点。少跑一个模型，少一处出错点。

模型输入输出（已实测确认）：
  输入  input   (batch, 3, H, W)   float32，RGB，ImageNet 归一化
  输出  output  (batch, 90, 16)    90 = 行*9+列，16 = 类别

用法：
    python board_onnx.py --snapshot out/xxx.png
    python board_onnx.py                 # 抓当前窗口
"""
import argparse
import io
import json
import os
import sys

import numpy as np

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config", "layout.json")
MODEL = os.path.join(HERE, "models", "layout_nano.onnx")

# 模型的 16 个类别，顺序必须与训练时一致，不能改
CLASSES = [
    "point", "other",
    "red_king", "red_advisor", "red_bishop", "red_knight",
    "red_rook", "red_cannon", "red_pawn",
    "black_king", "black_advisor", "black_bishop", "black_knight",
    "black_rook", "black_cannon", "black_pawn",
]

# 模型类别 -> 本项目内部标签。沿用 coach.py 的约定：车馬用繁体（APP 就是这么渲染的），
# 红方是 帅仕相炮兵、黑方是 將士象砲卒。
LABEL = {
    "point": "EMPTY",
    "other": "UNKNOWN",
    "red_king": "R帥", "red_advisor": "R仕", "red_bishop": "R相",
    "red_knight": "R馬", "red_rook": "R車", "red_cannon": "R炮", "red_pawn": "R兵",
    "black_king": "B將", "black_advisor": "B士", "black_bishop": "B象",
    "black_knight": "B馬", "black_rook": "B車", "black_cannon": "B砲", "black_pawn": "B卒",
}

# 拉正后的画布：棋盘四点映射到边距 padding 的内框上。
# 这套尺寸必须与模型训练时一致，改了就相当于给网络喂了不同分布的图，精度会掉。
DST_SIZE = (450, 500)      # (宽, 高)
PADDING = 50
CROP_SIZE = (400, 450)     # 从拉正图中心裁出棋盘格区域 (宽, 高)
INPUT_SIZE = (280, 315)    # 网络输入 (宽, 高)，正好 35px 一格
MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
STD = np.array([58.395, 57.12, 57.375], dtype=np.float32)

_SESSION = None


def session():
    """惰性加载 ONNX 会话，只在第一次用到时载入。"""
    global _SESSION
    if _SESSION is None:
        if not os.path.exists(MODEL):
            raise FileNotFoundError(
                "整板识别模型不存在: {}\n"
                "  运行 python download_models.py 下载".format(MODEL))
        import onnxruntime as ort
        _SESSION = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    return _SESSION


def load_layout():
    with open(CFG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    b = cfg["board"]
    pts = b.get("points") or b.get("points_px")
    c0 = b.get("cell") or b.get("cell_px")
    cell = float(c0[0]) if isinstance(c0, (list, tuple)) else float(c0)
    return cfg, pts, cell


def board_corners(pts):
    """从 90 个交叉点里取棋盘四角，顺序为 左上/右上/左下/右下。

    左上对应黑方视角的左角（(0,0)），右下对应红方右角（(9,8)）。
    """
    return np.float32([
        pts[0][0], pts[0][8], pts[9][0], pts[9][8],
    ])


def warp_board(arr_rgb, pts):
    """把棋盘拉正成 DST_SIZE 的正视图。返回 (拉正图, 4x2 目标角点)。"""
    import cv2

    src = board_corners(pts)
    dst = np.float32([
        [PADDING, PADDING],
        [DST_SIZE[0] - PADDING, PADDING],
        [PADDING, DST_SIZE[1] - PADDING],
        [DST_SIZE[0] - PADDING, DST_SIZE[1] - PADDING],
    ])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(arr_rgb, m, DST_SIZE), dst


def center_crop(img, size):
    """按中心裁到 size=(宽,高)，尺寸不足时给黑边补齐而不是直接失败。"""
    import cv2

    h, w = img.shape[:2]
    tw, th = size
    x0 = (w - tw) // 2
    y0 = (h - th) // 2
    if x0 >= 0 and y0 >= 0:
        return img[y0:y0 + th, x0:x0 + tw]

    # 拉正图比期望的小（棋盘很小时会发生），补边保证形状固定
    canvas = np.zeros((th, tw, img.shape[2]), dtype=img.dtype)
    sx0, sy0 = max(0, -x0), max(0, -y0)
    src = img[sy0:sy0 + th, sx0:sx0 + tw]
    canvas[(th - src.shape[0]) // 2:(th - src.shape[0]) // 2 + src.shape[0],
           (tw - src.shape[1]) // 2:(tw - src.shape[1]) // 2 + src.shape[1]] = src
    return canvas


def preprocess(warped):
    """拉正图 -> 网络输入张量。步骤必须与模型训练时完全一致。"""
    import cv2

    crop = center_crop(warped, CROP_SIZE)
    resized = cv2.resize(crop, INPUT_SIZE, interpolation=cv2.INTER_LINEAR)
    x = (resized.astype(np.float32) - MEAN) / STD
    x = np.transpose(x, (2, 0, 1))[None]     # HWC -> NCHW
    return np.ascontiguousarray(x, dtype=np.float32)


def classify(img, pts, cell=None, return_scores=False):
    """识别整盘。返回 {(行,列): (标签, 置信度)}。

    与 grid_classify.classify 的返回结构一致，可直接替换。
    标签取值：'EMPTY' / 'R帥' / 'B車' / 'UNKNOWN' …
    """
    arr = np.asarray(img.convert("RGB") if hasattr(img, "convert") else img)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)

    warped, _ = warp_board(arr, pts)
    x = preprocess(warped)
    out = session().run(None, {"input": x})[0]        # (1, 90, 16)

    logits = out[0].astype(np.float32)                # (90, 16)
    # 模型输出的量纲不确定（可能是 logits 也可能是概率），统一过一遍 softmax
    # 让它变成可解释的置信度；argmax 不受影响
    e = np.exp(logits - logits.max(axis=-1, keepdims=True))
    prob = e / e.sum(axis=-1, keepdims=True)

    idx = prob.argmax(axis=-1)
    board = {}
    for i in range(90):
        r, c = divmod(i, 9)
        name = CLASSES[idx[i]]
        board[(r, c)] = (LABEL[name], round(float(prob[i, idx[i]]), 4))
    if return_scores:
        return board, prob, warped
    return board


def render(board):
    """把局面打成 10 行 9 列的文本表。"""
    lines = []
    for r in range(10):
        row = []
        for c in range(9):
            lab = board.get((r, c), ("EMPTY", 0))[0]
            row.append(".." if lab == "EMPTY" else lab)
        lines.append("  " + " ".join(f"{s:<3}" for s in row))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--show-warp", action="store_true", help="把拉正后的棋盘存到 out/")
    args = ap.parse_args()

    from PIL import Image

    cfg, pts, cell = load_layout()

    if args.snapshot:
        img = Image.open(args.snapshot).convert("RGB")
    else:
        import grid_classify as G
        w = G.find_window()
        if not w:
            print("!! 找不到 JJ象棋 窗口")
            return 1
        x, y, ww, hh = w[1]
        sx, sy = ww / cfg["image"]["w"], hh / cfg["image"]["h"]
        pts = [[(p[0] * sx, p[1] * sy) for p in row] for row in pts]
        img = G.grab((x, y, ww, hh))

    print(f"画面 {img.width}x{img.height}")

    import time
    t0 = time.time()
    board, prob, warped = classify(img, pts, cell, return_scores=True)
    dt = (time.time() - t0) * 1000

    occ = {k: v for k, v in board.items() if v[0] != "EMPTY"}
    print(f"识别到 {len(occ)} 子   耗时 {dt:.0f} ms")
    print("\n" + render(board))

    low = sorted(((v[1], k, v[0]) for k, v in board.items()), key=lambda t: t[0])[:6]
    print("\n置信度最低的 6 格：")
    for conf, (r, c), lab in low:
        print(f"  ({r},{c}) {lab:<8} {conf:.3f}")

    if args.show_warp:
        import cv2
        os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
        p = os.path.join(HERE, "out", "warped_board.png")
        cv2.imwrite(p, cv2.cvtColor(warped, cv2.COLOR_RGB2BGR))
        print(f"\n拉正后的棋盘: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
