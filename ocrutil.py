# -*- coding: utf-8 -*-
"""RapidOCR 封装，针对「小程序界面上的小字号中文」做了预处理。

这一类画面的难点在于字小、背景杂、还有选中高亮和半透明遮罩。
默认处理策略：灰度 -> 放大 -> 提对比，再交给 OCR。
"""
import numpy as np
from PIL import Image, ImageOps

_ENGINE = None


def engine(verbose=False):
    """惰性加载，模型只在第一次用时载入。"""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
        if verbose:
            print("  [ocr] RapidOCR 已加载")
    return _ENGINE


def preprocess(img, scale=2, autocontrast=True):
    """灰度 + 放大 + 提对比。返回 PIL 灰度图。"""
    if img.mode != "L":
        img = img.convert("L")
    if scale and scale > 1:
        w, h = img.size
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
    if autocontrast:
        img = ImageOps.autocontrast(img, cutoff=1)
    return img


def recognize(img, min_score=0.5, scale=2, autocontrast=True, verbose=False):
    """返回 [(text, score, box)]，box 是四点坐标（放大后坐标系）。

    坐标是预处理图的坐标系，调用方要换算回原图就除以 scale。
    """
    gray = preprocess(img, scale=scale, autocontrast=autocontrast)
    arr = np.asarray(gray)
    result, _ = engine(verbose)(arr)
    out = []
    if not result:
        return out
    for item in result:
        box, text, raw_score = item[0], item[1], item[2]
        # 某些版本返回的置信度是字符串，必须先转
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if not text or score < min_score:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({
            "text": text.strip(),
            "score": float(score),
            "x": float(min(xs)) / (scale or 1),
            "y": float(min(ys)) / (scale or 1),
            "w": float(max(xs) - min(xs)) / (scale or 1),
            "h": float(max(ys) - min(ys)) / (scale or 1),
        })
    out.sort(key=lambda r: (r["y"], r["x"]))
    return out


def dump_lines(img, **kw):
    """把一张图上所有识别到的文字按纵向顺序列出来，用于摸界面布局。"""
    return recognize(img, **kw)
