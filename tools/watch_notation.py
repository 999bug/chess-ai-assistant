# -*- coding: utf-8 -*-
"""JJ象棋 棋谱实时检测。

思路：不认棋子，只读界面左下角的中文记谱列表。
每一轮重新识别整个棋谱区（全量重建，不依赖上一帧的结果），
只有连续多帧完全一致的确认识别结果，才会产出新的着法。

用法：
    python watch_notation.py dump  --title 微信              # 把窗口文字全量 OCR 出来，用于摸布局
    python watch_notation.py dump  --title 微信 --region bottomleft
    python watch_notation.py watch --title 微信 --hz 4       # 实时监测，新增着法即时输出
"""
import argparse
import io
import json
import os
import sys
import time

import ocrutil
import notation
from probe import is_desktop_locked, list_windows, bring_to_front, grab_mss, grab_dxcam

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tools/ 的上一级
OUT_DIR = os.path.join(ROOT, "out")
LOG_PATH = os.path.join(OUT_DIR, "notation_log.jsonl")


def setup_stdout():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def region_presets(x, y, w, h):
    return {
        "whole": (x, y, w, h),
        "bottomleft": (x, y + int(h * 0.58), int(w * 0.36), int(h * 0.42)),
        "bottomleft_wide": (x, y + int(h * 0.42), int(w * 0.50), int(h * 0.58)),
        "bottom": (x, y + int(h * 0.70), w, int(h * 0.30)),
        "right": (x + int(w * 0.62), y, int(w * 0.38), h),
    }


def resolve_target(title):
    rows, errors = list_windows()
    if errors:
        print(f"!! 枚举出错 {len(errors)} 个：{errors[0][1]}: {errors[0][2]}")
    if not rows:
        print("!! 枚举不到任何窗口")
        return None
    if title:
        hit = [r for r in rows
               if title.lower() in ((r["title"] or "") + (r["cls"] or "") + (r["proc"] or "")).lower()]
    else:
        key = ["象棋", "jj", "wv_", "微信", "wechat"]
        hit = [r for r in rows
               if any(k in ((r["title"] or "") + (r["cls"] or "")) for k in key)]
    if not hit:
        print("!! 没有匹配窗口。用 `python probe.py list` 看一眼真实标题")
        return None
    # 多个候选时全部列出，默认取面积最大的
    if len(hit) > 1:
        print(f"匹配到 {len(hit)} 个窗口：")
        for r in hit:
            print(f'  hwnd={r["hwnd"]:<10} [{r["rect"][2]}x{r["rect"][3]}] '
                  f'proc={r["proc"]:<22} title={r["title"]!r}')
    return sorted(hit, key=lambda r: r["rect"][2] * r["rect"][3], reverse=True)[0]


def capture(rect, method="mss"):
    return grab_mss(rect) if method == "mss" else grab_dxcam(rect)


def cmd_dump(args):
    target = resolve_target(args.title)
    if not target:
        return 1
    x, y, w, h = target["rect"]
    print(f'目标 hwnd={target["hwnd"]} {target["proc"]} {w}x{h} title={target["title"]!r}')
    ok, how = bring_to_front(target["hwnd"])
    print(f"唤醒前台: {'成功' if ok else '失败'} ({how})")
    time.sleep(0.6)

    presets = region_presets(x, y, w, h)
    rect = presets.get(args.region, presets["whole"])
    print(f"区域 [{args.region}] -> ({rect[0]},{rect[1]}) {rect[2]}x{rect[3]}")

    img = capture(rect, args.method)
    os.makedirs(OUT_DIR, exist_ok=True)
    snap = os.path.join(OUT_DIR, f"dump_{args.region}_{time.strftime('%H%M%S')}.png")
    img.save(snap)
    print(f"原图已存: {snap}\n")

    lines = ocrutil.recognize(img, min_score=args.min_score, scale=args.scale)
    print(f"OCR 共 {len(lines)} 条：")
    print(f'{"x":>5} {"y":>5} {"score":>6}  text')
    for it in lines:
        print(f'{it["x"]:>5.0f} {it["y"]:>5.0f} {it["score"]:>6.2f}  {it["text"]!r}')

    hits = notation.extract_moves(lines, min_score=args.min_score)
    print(f"\n其中符合记谱格式的 {len(hits)} 条：")
    for mv, it in hits:
        print(f"  y={it['y']:.0f}  {mv['text']}  ->  {mv}")

    if not hits:
        print("\n!! 没解析出任何着法。可能原因：")
        print("   1) 这个区域不是棋谱区（换 --region 试试，或先看 whole 的输出定位）")
        print("   2) 程序还没进到对局界面")
        print("   3) 字号太小，加大 --scale")
    return 0


def cmd_watch(args):
    target = resolve_target(args.title)
    if not target:
        return 1
    x, y, w, h = target["rect"]
    print(f'目标 hwnd={target["hwnd"]} {target["proc"]} {w}x{h} title={target["title"]!r}')
    ok, how = bring_to_front(target["hwnd"])
    print(f"唤醒前台: {'成功' if ok else '失败'} ({how})")
    time.sleep(0.5)

    presets = region_presets(x, y, w, h)
    rect = presets.get(args.region, presets["bottomleft"])
    print(f"监测区域 [{args.region}] ({rect[0]},{rect[1]}) {rect[2]}x{rect[3]}")

    os.makedirs(OUT_DIR, exist_ok=True)
    known = []            # 已确认的着法文本序列
    pending, pending_n = None, 0
    interval = 1.0 / max(args.hz, 0.2)
    print(f"开始监测 @ {args.hz}Hz，Ctrl+C 停止\n")

    try:
        while True:
            t0 = time.time()
            img = capture(rect, args.method)
            lines = ocrutil.recognize(img, min_score=args.min_score, scale=args.scale)
            hits = notation.extract_moves(lines, min_score=args.min_score)
            current = [mv["text"] for mv, _ in hits]

            if current != known:
                if current == pending:
                    pending_n += 1
                else:
                    pending, pending_n = current, 1
                # 连续 N 帧一致才确认，滤掉动画和高亮中间态
                if pending_n >= args.stable:
                    old = known
                    known = current
                    pending, pending_n = None, 0
                    mark = "新增" if notation.is_prefix_of(old, current) and len(current) > len(old) else "变更"
                    if len(current) != len(old) or current != old:
                        new = current[len(old):] if notation.is_prefix_of(old, current) else []
                        if new:
                            for i, mv_text in enumerate(new):
                                idx = len(old) + i + 1
                                print(f'[{time.strftime("%H:%M:%S")}] {mark} 第{idx}手  {mv_text}')
                                with open(LOG_PATH, "a", encoding="utf-8") as f:
                                    f.write(json.dumps({"ts": time.time(), "idx": idx,
                                                        "move": mv_text, "all": current},
                                                       ensure_ascii=False) + "\n")
                        else:
                            print(f'[{time.strftime("%H:%M:%S")}] {mark} 棋谱变化，当前 {len(current)} 手: '
                                  f'{" ".join(current[-6:])}')
                            with open(LOG_PATH, "a", encoding="utf-8") as f:
                                f.write(json.dumps({"ts": time.time(), "event": "replaced",
                                                    "all": current}, ensure_ascii=False) + "\n")
                else:
                    pass
            else:
                pending, pending_n = None, 0

            dt = time.time() - t0
            time.sleep(max(0, interval - dt))
    except KeyboardInterrupt:
        print(f"\n停止。当前共 {len(known)} 手，日志: {LOG_PATH}")
    return 0


def main():
    setup_stdout()
    if is_desktop_locked():
        print("!! 屏幕已锁定，截到的只会是壁纸。请解锁后再跑。")
        return 1

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    for name in ("dump", "watch"):
        s = sub.add_parser(name)
        s.add_argument("--title", default=None)
        s.add_argument("--region", default="bottomleft" if name == "watch" else "whole",
                       choices=["whole", "bottomleft", "bottomleft_wide", "bottom", "right"])
        s.add_argument("--method", default="mss", choices=["mss", "dxcam"])
        s.add_argument("--scale", type=int, default=2)
        s.add_argument("--min-score", type=float, default=0.6)
        if name == "watch":
            s.add_argument("--hz", type=float, default=3.0)
            s.add_argument("--stable", type=int, default=3, help="连续多少帧一致才确认")
    args = ap.parse_args()

    if args.cmd == "dump":
        return cmd_dump(args)
    if args.cmd == "watch":
        return cmd_watch(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
