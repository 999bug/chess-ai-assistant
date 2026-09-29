# -*- coding: utf-8 -*-
"""跑完 tests/ 下的所有测试。

用法：python tests/run_all.py
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))

bad = []
for path in files:
    print("=" * 62)
    print(">> " + os.path.basename(path))
    if subprocess.run([sys.executable, path]).returncode != 0:
        bad.append(os.path.basename(path))

print("=" * 62)
if bad:
    print("失败：" + "，".join(bad))
    sys.exit(1)
print("全部 {} 个测试文件通过".format(len(files)))
