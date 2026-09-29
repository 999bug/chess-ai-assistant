# -*- coding: utf-8 -*-
"""下载整板识别所需的 ONNX 模型。

模型体积大（31MB），按 .gitignore 的约定不入库，克隆后跑这个脚本拉取。

模型来源：TheOne1006/chinese-chess-recognition 与 cheese2020/Chinese-Chess-Recognition
共用的预训练权重，托管在 HuggingFace Space（Apache-2.0）。
    https://huggingface.co/spaces/yolo12138/Chinese_Chess_Recognition

国内直连 HuggingFace 可能不通，脚本会依次尝试官方域名与 hf-mirror 镜像。

用法：
    python download_models.py
    python download_models.py --force      # 已存在也重新下载
"""
import argparse
import io
import os
import sys

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")

SPACE = "yolo12138/Chinese_Chess_Recognition"
HOSTS = ["https://huggingface.co", "https://hf-mirror.com"]

# 本地文件名 -> (Space 内路径, 期望字节数)
FILES = {
    "layout_nano.onnx": ("onnx/layout_recognition/nano_v3-0319.onnx", 31101356),
    "pose_4_v6.onnx": ("onnx/pose/4_v6-0301.onnx", 10704184),
}
REQUIRED = {"layout_nano.onnx"}


def fetch(rel_path, dest, expect_size):
    import urllib.request

    last = None
    for host in HOSTS:
        url = f"{host}/spaces/{SPACE}/resolve/main/{rel_path}"
        for attempt in range(1, 4):
            try:
                print(f"  尝试 {url}  (第 {attempt} 次)")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    total = int(r.headers.get("Content-Length") or 0)
                    if total and total != expect_size:
                        # HF 返回的是 LFS 指针而不是真文件时会出现这种情况
                        raise RuntimeError(f"返回体积 {total} != 期望 {expect_size}（可能是 LFS 指针）")
                    chunks, got = [], 0
                    while True:
                        buf = r.read(1 << 20)
                        if not buf:
                            break
                        chunks.append(buf)
                        got += len(buf)
                        print(f"\r    已下载 {got/1048576:.1f} MB", end="")
                    data = b"".join(chunks)
                if len(data) != expect_size:
                    raise RuntimeError(f"下载完成但体积 {len(data)} != 期望 {expect_size}")
                print(f"\r    完成 {len(data)/1048576:.1f} MB      ")
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            except Exception as e:
                last = e
                print(f"\r    失败: {type(e).__name__}: {e}")
    print(f"  !! {rel_path} 下载失败: {last}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all", action="store_true", help="连关键点模型一起下（当前识别链路用不到）")
    args = ap.parse_args()

    os.makedirs(MODELS, exist_ok=True)
    want = dict(FILES)
    if not args.all:
        want = {k: v for k, v in FILES.items() if k in REQUIRED}

    print(f"模型目录: {MODELS}")
    failed = []
    for name, (rel, size) in want.items():
        dest = os.path.join(MODELS, name)
        if os.path.exists(dest) and os.path.getsize(dest) == size and not args.force:
            print(f"已存在且体积正确，跳过: {name} ({size/1048576:.1f} MB)")
            continue
        print(f"下载 {name}  <- {rel}  ({size/1048576:.1f} MB)")
        if not fetch(rel, dest, size):
            failed.append(name)

    missing = [n for n in REQUIRED if not os.path.exists(os.path.join(MODELS, n))]
    if missing:
        print(f"\n!! 必需模型缺失: {missing}")
        print("   开了代理/梯子后重试：python download_models.py")
        return 1
    if failed:
        print(f"\n以下可选模型没下成功：{failed}（不影响识别）")
    print("\n完成。可以跑了：python board_onnx.py --snapshot out/xxx.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
