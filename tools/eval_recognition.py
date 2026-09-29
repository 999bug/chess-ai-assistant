# -*- coding: utf-8 -*-
"""识别准确率评测：拿人工核对过的真值，量出识别到底错多少。

为什么要有这个脚本：改识别之前先得说清"现在错多少"。项目原来只有一个
「对照开局标准摆法」的自校验，那个只在标准开局帧上成立——
  一是它把真值写死成开局，中局帧全被判为"不一致"，数字没有意义；
  二是它给人错觉：逐格错 4 个看着不多，但那 4 个错足以凑出一个不可能的盘面。

指标口径（都在 90 个交叉点上算）：
    逐格准确率   90 格里判对的格子比例。空位也算一格，所以这个数天然偏高，
                 不要只看它。
    子力召回     真实存在的棋子里，被正确认出来的比例。这个才是关键指标。
    幻影数       空交叉点被凭空判成有子的个数。这个最危险——
                 它会让引擎基于一个物理上不可能的局面出招。
    整盘全对     一帧 90 格全对才算。引擎要的是整盘正确，不是 98% 正确。

用法：
    python eval_recognition.py                  # 新旧都跑，做对比
    python eval_recognition.py --backend new    # 只跑整板 ONNX
    python eval_recognition.py --backend old    # 只跑逐格模板匹配
"""
import argparse
import io
import json
import os
import sys
import time

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 tools/ 下，上一级才是项目根
GT_PATH = os.path.join(ROOT, "tests", "gt", "recognition_gt.json")
SNAP_DIR = os.path.join(ROOT, "out")
# 运行时核心在 app/ 下，这里要用到 board_onnx / grid_classify
sys.path.insert(0, os.path.join(ROOT, "app"))


def load_gt():
    with open(GT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["frames"]


def parse_rows(rows):
    """10 行文本 -> {(行,列): 标签}，'..' 归一成 EMPTY。"""
    out = {}
    for r, line in enumerate(rows):
        toks = line.split()
        if len(toks) != 9:
            raise ValueError(f"真值第 {r} 行不是 9 列: {line!r}")
        for c, tok in enumerate(toks):
            out[(r, c)] = "EMPTY" if tok in ("..", ".") else tok
    return out


def run_old(img, pts, cell):
    import grid_classify as G
    return {k: v[0] for k, v in G.classify(img, pts, cell).items()}


def run_new(img, pts, cell):
    import board_onnx
    return {k: v[0] for k, v in board_onnx.classify(img, pts, cell).items()}


BACKENDS = {"old": run_old, "new": run_new}


def score(gt, got):
    """返回该帧的各项指标。"""
    cells = len(gt)                                   # 90
    n_true = sum(1 for v in gt.values() if v != "EMPTY")
    hit = sum(1 for k in gt if got.get(k) == gt[k])
    # 子力召回：真实棋子被认对
    recall = sum(1 for k, v in gt.items() if v != "EMPTY" and got.get(k) == v)
    # 幻影：真空位被判成有子
    phantom = sum(1 for k, v in gt.items() if v == "EMPTY" and got.get(k, "EMPTY") != "EMPTY")
    # 漏子：真子被判成空
    missing = sum(1 for k, v in gt.items() if v != "EMPTY" and got.get(k, "EMPTY") == "EMPTY")
    # 错种：位置对了但兵种/阵营错
    wrongtype = sum(1 for k, v in gt.items()
                    if v != "EMPTY" and got.get(k) not in (v, "EMPTY"))
    return {
        "cells": cells, "cell_ok": hit, "n_true": n_true,
        "recall": recall, "phantom": phantom, "missing": missing,
        "wrongtype": wrongtype, "all_correct": hit == cells,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="both", choices=["both", "old", "new"])
    ap.add_argument("--verbose", action="store_true", help="打印每一处错格")
    args = ap.parse_args()

    from PIL import Image
    import grid_classify as G

    _cfg, pts, cell = G.load_layout()
    gt_all = load_gt()

    names = ["old", "new"] if args.backend == "both" else [args.backend]
    summary = {b: {"cell_ok": 0, "cells": 0, "recall": 0, "n_true": 0,
                   "phantom": 0, "frames_ok": 0, "ms": 0.0}
               for b in names}

    for fname, spec in gt_all.items():
        path = os.path.join(SNAP_DIR, fname)
        if not os.path.exists(path):
            print(f"!! 缺快照，跳过: {path}")
            continue
        gt = parse_rows(spec["rows"])
        img = Image.open(path).convert("RGB")
        print("=" * 68)
        print(f"{fname}   （{spec.get('note','')[:52]}）")
        print("=" * 68)

        for b in names:
            t0 = time.time()
            got = BACKENDS[b](img, pts, cell)
            ms = (time.time() - t0) * 1000
            m = score(gt, got)
            s = summary[b]
            s["cell_ok"] += m["cell_ok"]; s["cells"] += m["cells"]
            s["recall"] += m["recall"]; s["n_true"] += m["n_true"]
            s["phantom"] += m["phantom"]; s["frames_ok"] += int(m["all_correct"])
            s["ms"] += ms

            flag = "整盘全对 ✅" if m["all_correct"] else "整盘有错 ❌"
            print(f"  [{b}] {flag}   逐格 {m['cell_ok']}/{m['cells']}"
                  f"   子力召回 {m['recall']}/{m['n_true']}"
                  f"   幻影 {m['phantom']}  漏子 {m['missing']}  错种 {m['wrongtype']}"
                  f"   {ms:.0f}ms")
            if args.verbose and not m["all_correct"]:
                bad = [(k, gt[k], got.get(k, "?")) for k in sorted(gt) if got.get(k) != gt[k]]
                for (r, c), want, have in bad:
                    print(f"        ({r},{c}) 真值={want:<8} 认出={have}")

    if not summary:
        return 1

    print()
    print("=" * 68)
    print("汇总")
    print("=" * 68)
    n_frames = len(gt_all)
    print(f"{'方案':<6}{'逐格准确率':>12}{'子力召回':>12}{'幻影':>6}{'整盘全对':>10}{'平均耗时':>11}")
    for b in names:
        s = summary[b]
        print(f"{b:<6}{s['cell_ok']/max(1,s['cells']):>11.1%}"
              f"{s['recall']/max(1,s['n_true']):>11.1%}"
              f"{s['phantom']:>6}"
              f"{f'{s["frames_ok"]}/{n_frames}':>10}"
              f"{s['ms']/n_frames:>9.0f}ms")

    if args.backend == "both":
        good = summary["new"]["recall"] - summary["old"]["recall"]
        print(f"\n子力召回提升: {good:+d} 个棋子"
              f"（旧 {summary['old']['recall']}/{summary['old']['n_true']}"
              f" -> 新 {summary['new']['recall']}/{summary['new']['n_true']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
