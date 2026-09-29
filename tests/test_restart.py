# -*- coding: utf-8 -*-
"""「重开一局」开关 + 子力骤降基线的回归测试。

直接跑：python tests/test_restart.py

power_drop        —— 子力骤降判定（只判降，不判升）
restart_requested —— 一次性重置信号（读到就消费掉）

这两块都是为了修同一个现场问题：一局下完之后助手再也不出招。
见 docs/PROGRESS.md「重置开关与子力骤降基线」。
"""
import io
import os
import sys

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 运行时核心在 app/ 下（测试在 tests/，两者都挂在项目根下）
sys.path.insert(0, os.path.join(ROOT, "app"))

import auto_coach as A

FAIL = []


def check(name, got, want):
    ok = got == want
    print("  {} {:<46} -> {!r}".format("PASS" if ok else "FAIL", name, got))
    if not ok:
        FAIL.append("{}: 期望 {!r} 实际 {!r}".format(name, want, got))


print("=== power_drop：子力骤降判定 ===")
check("没有基线时不报", A.power_drop(None, 32), "")
check("子力不变不报", A.power_drop(32, 32), "")
check("少 1 个不报（抖动量内）", A.power_drop(32, 31), "")
check("少 2 个不报（容差边界）", A.power_drop(32, 30), "")
check("少 3 个才报", A.power_drop(32, 29) != "", True)
check("掉一大截要报", A.power_drop(32, 22) != "", True)

# 新开一局 / 加载完成时子力是变多的，绝不能当成异常（旧代码在这里
# 顺手把基线改掉了，坏帧就是从这个口子把基线带偏的）
check("子力变多不报（新开一局）", A.power_drop(20, 32), "")
check("子力暴涨不报", A.power_drop(1, 32), "")

# 线上就是这么卡死的：识别崩了读出 42 子（棋盘上限 32），基线被顶成 42，
# 之后每一帧正常盘面都被判「42 -> 32 骤降」，守门员永久不放行。
# 这条钉的是"基线一旦被污染就会持续误报"这个事实——真正的修复是
# auto_coach 主循环里"基线只由过了守门员的帧来立"，不是改这里的判定。
check("基线被污染后会持续误报（修复点在主循环，不在判定里）",
      A.power_drop(42, 32) != "", True)
check("污染后的误报文案会带上两个数字",
      A.power_drop(42, 32), "子力 42 -> 32 骤降，疑似漏检")

print("\n=== restart_requested：一次性重置信号 ===")
flag = os.path.join(ROOT, "out", "_restart_test.flag")
if os.path.exists(flag):
    os.remove(flag)

check("没有信号时返回 False", A.restart_requested(flag), False)
with open(flag, "w", encoding="utf-8") as f:
    f.write("1\n")
check("有信号时返回 True", A.restart_requested(flag), True)
check("信号被消费掉（文件已删除）", os.path.exists(flag), False)
check("再问一次不会重复触发", A.restart_requested(flag), False)

# 目录不存在时不该炸，只当"没有信号"（out/ 被清掉的情况）
check("路径不存在也当没有信号",
      A.restart_requested(os.path.join(ROOT, "out", "_no_such_dir", "x.flag")), False)

# 默认参数指向的确实是 out/restart.flag（浮窗写的就是这个路径）
check("默认信号路径在 out/ 下",
      (os.path.relpath(A.RESTART_FLAG, ROOT), A.restart_requested.__defaults__[0]),
      (os.path.join("out", "restart.flag"), A.RESTART_FLAG))

print()
if FAIL:
    print("有 {} 个用例未通过：".format(len(FAIL)))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部用例通过")
