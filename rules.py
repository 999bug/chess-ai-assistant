# -*- coding: utf-8 -*-
"""象棋规则：局面合法性校验 + 相邻两帧的着法差分。

为什么单独抽一层：识别和规则是两件事，但过去它们混在一起——
grid_classify.validate_board 只会查「子力数量上限 + 士象将的位置」，
结果一个 22 子的荒唐残盘（红方缺 2 车 2 马 2 炮）照样判「通过」，
引擎拿着错盘面出招，给出的建议比不给更糟。

这一层的定位是**守门员**：宁可承认"我不确定"，也不能把说不通的局面交给引擎。
但它只做"必要条件"校验，不做完整可达性搜索——后者代价高且容易误杀真实局面。

校验分两类：
  静态（单帧自己就该说得通）：将帅各一、子力不超编、士象将帅兵卒的位置合法、
      将帅不能照面。
  时序（相邻帧之间必须说得通）：一局棋每走一步只动一个子，所以两帧的差异
      必须能用一个合法着法解释（含吃子）；或者是重开一局回到标准开局。

用法：
    python rules.py --snapshot out/xxx.png
"""
import argparse
import io
import json
import os
import sys

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config", "layout.json")

# 开局编制：每种子力的数量上限。识别出的数量超过它，必定是认错了。
LIMIT = {
    ("R", "車"): 2, ("R", "馬"): 2, ("R", "炮"): 2, ("R", "相"): 2,
    ("R", "仕"): 2, ("R", "兵"): 5, ("R", "帥"): 1,
    ("B", "車"): 2, ("B", "馬"): 2, ("B", "砲"): 2, ("B", "象"): 2,
    ("B", "士"): 2, ("B", "卒"): 5, ("B", "將"): 1,
}

# 标准开局摆法，用于识别"重开一局"
SETUP = {}
for _c, _ch in enumerate("車馬象士將士象馬車"):
    SETUP[(0, _c)] = "B" + _ch
for _c, _ch in enumerate("車馬相仕帥仕相馬車"):
    SETUP[(9, _c)] = "R" + _ch
for _c in (1, 7):
    SETUP[(2, _c)] = "B砲"
    SETUP[(7, _c)] = "R炮"
for _c in (0, 2, 4, 6, 8):
    SETUP[(3, _c)] = "B卒"
    SETUP[(6, _c)] = "R兵"

# 直走子：车/炮/兵/卒/帅/将。用来判断一个着法在几何上是否说得通。
STRAIGHT = {"車", "车", "炮", "砲", "兵", "卒", "帥", "帅", "將", "将"}


def pieces(board):
    """从 {(行,列): (标签, 分)} 里取 {(行,列): 标签}，丢掉空位与未知。"""
    out = {}
    for k, v in board.items():
        lab = v[0] if isinstance(v, (tuple, list)) else v
        if lab and lab != "EMPTY":
            out[k] = lab
    return out


def legal_square(side, piece, r, c):
    """该兵种出现在 (r,c) 是否可能。只做宽松的位置约束。

    用处在挡明显的误判：识别把 (7,6) 判成"仕"（仕只能在九宫 col3~5），
    出来的 FEN 会有两个帅，引擎直接拒招。
    """
    palace_b = 0 <= r <= 2 and 3 <= c <= 5
    palace_r = 7 <= r <= 9 and 3 <= c <= 5
    if side == "B":
        if piece in ("將", "士"):
            return palace_b
        if piece == "象":
            return r <= 4          # 象不过河
        if piece == "卒":
            return r >= 3          # 卒只进不退
    else:
        if piece in ("帥", "仕"):
            return palace_r
        if piece == "相":
            return r >= 5          # 相不过河
        if piece == "兵":
            return r <= 6
    return True                     # 車馬炮anywhere


def kings_face(pos):
    """将帅照面（同一纵线且中间无子）。

    象棋里这是非法局面；识别一旦把某个子漏掉，就很容易凑出照面，
    所以这条检查能顺带抓出一批漏检。
    """
    rk = next((k for k, v in pos.items() if v == "R帥"), None)
    bk = next((k for k, v in pos.items() if v == "B將"), None)
    if not rk or not bk:
        return False
    if rk[1] != bk[1]:
        return False
    col = rk[1]
    lo, hi = min(rk[0], bk[0]), max(rk[0], bk[0])
    return not any(1 for (r, c) in pos if c == col and lo < r < hi)


def check_start(board):
    """这一局是不是从标准开局开始的。严格比对 32 子摆法。

    为什么需要它：静态校验只能查"必要条件"，一个 22 子的残盘（红方缺 2 车 2 马 2 炮）
    在物理上是完全可达的——那些子被吃了而已——所以静态校验必须放行。
    这类错误唯一能拦住的时机就是**开局帧**：开局必须是标准的 32 子摆法，不是就说明认错了。

    注意：JJ象棋 有「棋力评测」等非标准开局的模式，所以这里只给告警，
    由上层决定是拦住还是提示用户。
    """
    pos = pieces(board)
    if pos == SETUP:
        return True, []
    problems = []
    if len(pos) != 32:
        problems.append(f"开局应有 32 子，实际 {len(pos)}")
    missing = sorted(k for k in SETUP if pos.get(k) != SETUP[k])
    extra = sorted(k for k in pos if k not in SETUP)
    if missing:
        problems.append(f"{len(missing)} 处与标准开局不符: "
                        + " ".join(f"{k}应{SETUP[k]}实{pos.get(k, '空')}" for k in missing[:5]))
    if extra:
        problems.append(f"{len(extra)} 处多出棋子: "
                        + " ".join(f"{k}={pos[k]}" for k in extra[:5]))
    return False, problems


def validate(board):
    """单帧静态校验。返回 (是否说得通, 问题列表)。"""
    pos = pieces(board)
    problems = []

    if not pos:
        return False, ["一格棋子都没读到"]

    from collections import Counter
    cnt = Counter()
    for lab in pos.values():
        if lab == "UNKNOWN":
            problems.append("存在无法识别的格子（模型给了 other 类）")
            continue
        if len(lab) < 2:
            problems.append(f"标签格式异常: {lab!r}")
            continue
        cnt[(lab[0], lab[1:])] += 1

    # 将帅各且仅一个。多了少了都说明识别错了。
    if cnt[("R", "帥")] != 1:
        problems.append(f"红帅 {cnt[('R', '帥')]} 个（应为 1）")
    if cnt[("B", "將")] != 1:
        problems.append(f"黑将 {cnt[('B', '將')]} 个（应为 1）")

    # 子力不得超编
    for key, cap in LIMIT.items():
        if cnt[key] > cap:
            problems.append(f"{key[0]}{key[1]} {cnt[key]} 个（上限 {cap}）")

    # 双方各自不超过 16 子，总计不超过 32
    n_red = sum(n for (s, _), n in cnt.items() if s == "R")
    n_black = sum(n for (s, _), n in cnt.items() if s == "B")
    if n_red > 16:
        problems.append(f"红方 {n_red} 子（上限 16）")
    if n_black > 16:
        problems.append(f"黑方 {n_black} 子（上限 16）")
    if n_red + n_black > 32:
        problems.append(f"棋子总数 {n_red + n_black}（上限 32）")

    # 位置合法性
    for (r, c), lab in sorted(pos.items()):
        if lab == "UNKNOWN":
            continue
        if not legal_square(lab[0], lab[1:], r, c):
            problems.append(f"{lab} 在 ({r},{c}) 位置不合法")

    # 将帅照面
    if kings_face(pos):
        problems.append("将帅照面（同一纵线中间无子），局面不合法")

    return (not problems), problems


def _path_clear(pos, frm, to):
    """格点之间（不含两端）是否全空。"""
    (r1, c1), (r2, c2) = frm, to
    if r1 == r2:
        lo, hi = sorted((c1, c2))
        return all((r1, c) not in pos for c in range(lo + 1, hi))
    if c1 == c2:
        lo, hi = sorted((r1, r2))
        return all((r, c1) not in pos for r in range(lo + 1, hi))
    return False


def _between(pos, frm, to):
    """格点之间（不含两端）有几个子，用于炮的翻山吃子。"""
    (r1, c1), (r2, c2) = frm, to
    n = 0
    if r1 == r2:
        lo, hi = sorted((c1, c2))
        n = sum(1 for c in range(lo + 1, hi) if (r1, c) in pos)
    elif c1 == c2:
        lo, hi = sorted((r1, r2))
        n = sum(1 for r in range(lo + 1, hi) if (r, c1) in pos)
    return n


def move_legal(pos, frm, to):
    """在没有完整盘面的情况下，判断「从 frm 走到 to」在几何上是否可能。

    只做必要的形状检查，不查将军/自将（那需要完整搜索）。
    返回 (是否可能, 原因)。
    """
    if frm == to:
        return False, "原地不动"
    lab = pos.get(frm)
    if not lab:
        return False, "起点没有棋子"
    side, piece = lab[0], lab[1:]
    (r1, c1), (r2, c2) = frm, to
    dr, dc = r2 - r1, c2 - c1
    target = pos.get(to)

    if target and target[0] == side:
        return False, "目标是己方棋子"

    # 红方向 r 减小走，黑方向 r 增大走
    forward = (-1 if side == "R" else 1)

    if piece in ("車", "车"):
        if dr != 0 and dc != 0:
            return False, "车只能直线走"
        return (_path_clear(pos, frm, to), "路径被挡" if not _path_clear(pos, frm, to) else "")

    if piece in ("炮", "砲"):
        if dr != 0 and dc != 0:
            return False, "炮只能直线走"
        mid = _between(pos, frm, to)
        if target:
            ok = mid == 1
            return ok, "" if ok else f"炮吃子需要恰好一个炮架，实际 {mid} 个"
        ok = mid == 0
        return ok, "" if ok else f"炮平移不能有子挡路，实际 {mid} 个"

    if piece == "馬":
        if sorted((abs(dr), abs(dc))) != [1, 2]:
            return False, "马走日"
        # 蹩马腿
        if abs(dr) == 2:
            leg = (r1 + dr // 2, c1)
        else:
            leg = (r1, c1 + dc // 2)
        if leg in pos:
            return False, "蹩马腿"
        return True, ""

    if piece == "象":
        if abs(dr) != 2 or abs(dc) != 2:
            return False, "象走田"
        if (r1 + dr // 2, c1 + dc // 2) in pos:
            return False, "塞象眼"
        return True, ""

    if piece == "相":
        if abs(dr) != 2 or abs(dc) != 2:
            return False, "相走田"
        if (r1 + dr // 2, c1 + dc // 2) in pos:
            return False, "塞相眼"
        return True, ""

    if piece in ("士", "仕"):
        if abs(dr) != 1 or abs(dc) != 1:
            return False, "士走斜一格"
        return True, ""

    if piece in ("將", "帥"):
        if abs(dr) + abs(dc) != 1:
            return False, "将帅走一格直线"
        return True, ""

    if piece in ("兵", "卒"):
        crossed = (r1 < 5) if side == "B" else (r1 > 4)
        if dr == forward and dc == 0:
            return True, ""
        if crossed and dr == 0 and abs(dc) == 1:
            return True, ""
        return False, "兵卒只能向前，过河后可横走"

    return True, ""


def diff(prev, cur, red_thr=None, black_thr=None):
    """比较相邻两帧的盘面，判断变化能否用一个合法着法解释。

    返回 (类型, 说明)。类型：
        'same'      没有变化
        'reset'     回到了标准开局（重开一局）
        'one_move'  恰好一个合法着法（含吃子）
        'noisy'     变化无法用一个着法解释——多半是识别抖动或走子动画中间态
    """
    p, q = pieces(prev), pieces(cur)
    if p == q:
        return "same", ""

    if dict(q) == SETUP:
        return "reset", "回到标准开局"

    gone = [k for k in p if p.get(k) != q.get(k)]
    added = [k for k in q if q.get(k) != p.get(k)]

    # 正常走子：起点消失、终点出现
    if len(gone) == 1 and len(added) == 1:
        frm, to = gone[0], added[0]
        ok, why = move_legal(p, frm, to)
        if ok:
            return "one_move", f"{p[frm]} {frm}->{to}"
        return "noisy", f"{p[frm]} {frm}->{to} 不合法（{why}）"

    # 退化情况：只是某格"消失"或"出现"，都是识别抖动
    if len(gone) == 1 and not added:
        return "noisy", f"{gone[0]} 的 {p[gone[0]]} 凭空消失"
    if len(added) == 1 and not gone:
        return "noisy", f"{added[0]} 凭空多出 {q[added[0]]}"
    if len(gone) > 1 or len(added) > 1:
        return "noisy", f"一次动了 {len(gone)} 处（应为 1 处）"
    return "noisy", "变化无法解释"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", nargs="+", required=True,
                    help="一张或多张快照；多张时按顺序做时序校验")
    args = ap.parse_args()

    import board_onnx

    prev = None
    for path in args.snapshot:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        _cfg, pts, cell = board_onnx.load_layout()
        board = board_onnx.classify(img, pts, cell)
        pos = pieces(board)

        ok, probs = validate(board)
        print(f"== {os.path.basename(path)} ==")
        print(f"  子数 {len(pos)}   静态校验: {'通过' if ok else '不通过'}")
        for p in probs:
            print(f"    - {p}")
        if prev is not None:
            kind, why = diff(prev, board)
            print(f"  与上一帧的差分: {kind}  {why}")
        prev = board
    return 0


if __name__ == "__main__":
    sys.exit(main())
