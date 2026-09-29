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
import itertools
import json
import os
import sys

if getattr(sys.stdout, "encoding", None) != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 app/ 下，上一级才是项目根
CFG = os.path.join(ROOT, "config", "layout.json")

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


def king_capturable(pos, side):
    """轮到我方(side)走，但我方已经能吃到对方的将。返回 (是/否, 说明)。

    为什么要单列这一条（2026-09-29 实测）：pikafish 遇到这种局面不是"拒绝出招"，
    而是打印一行
        info string CRITICAL ERROR: ... Reason: Unsupported position. King can be captured.
    然后**自己退出**。引擎一死，后面每一问都失败，而日志上只看得到一串
    "引擎没给着法"，真正的原因被埋在中间。

    它什么时候会真出现：
      · 轮次判错——差分异常后着法历史被重置，于是"该对方走"被当成"该我方走"；
      · 或者识别把某个子放错了位置，凭空多出一个将军。
    合法对局里它不可能出现：对方被将军，就必然轮到对方走。

    判据和引擎是同一个（都看"对方将是否被我这方攻击"），所以拦它不会误杀
    任何引擎本来愿意接受的局面——引擎面对它只会自杀，这里只是提前把话说清楚。
    """
    rk = next((k for k, v in pos.items() if v == "R帥"), None)
    bk = next((k for k, v in pos.items() if v == "B將"), None)
    if rk is None or bk is None:
        return False, ""                      # 缺将帅的事归静态校验管
    target, foe = (rk, "R帥") if side == "B" else (bk, "B將")
    for sq in sorted(k for k, v in pos.items() if v[0] == side and k != target):
        ok, _ = move_legal(pos, sq, target)
        if ok:
            return True, "{}（{}）能吃到 {}".format(pos[sq], sq, foe)
    if kings_face(pos):
        # 帅沿纵线"攻击"对方将（飞将），引擎同样判非法
        return True, "将帅照面（帅沿纵线攻击对方将）"
    return False, ""


def guess_turn(pos, me):
    """中途进场时，从局面本身推断"轮到谁走"。返回 (轮次 / None, 说明)。

    为什么需要：对局打到一半才把引擎打开时，没有"上一帧"可比，起点 FEN 里
    那个"轮谁走"就是猜的。猜错了不会打死引擎（FEN 标注和历史首手同源，永远
    自洽），但会拿对方该走的着法当建议给出来，比不给更糟。

    依据是合法局面留下的痕迹（判据和 king_capturable 一致）：
      · 我方能吃到对方的将 -> 不可能轮到我方（对局里没人会走一步送将），
                              所以现在轮**对方**走；
      · 对方能吃到我方的将 -> 不可能轮到对方走（否则他上一手直接吃了），
                              所以现在轮**我方**走（我方正被将军）；
      · 两个都不成立       -> 平静局面，看不出轮次，返回 None 交给上层。
    """
    foe = "B" if me == "R" else "R"
    bad, why = king_capturable(pos, me)
    if bad:
        return foe, "我方能吃到对方的将（{}），说明不是轮到我方走".format(why)
    bad, why = king_capturable(pos, foe)
    if bad:
        return me, "对方能吃到我方的将（{}），说明轮到我方走".format(why)
    return None, "将帅都不受攻击，从局面看不出轮次"


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


def explain_change(prev, cur, max_steps=3, first_side=None):
    """比较相邻两帧的盘面，返回 (类型, 说明, 着法序列)。

    着法序列是 [((fx行,fy列), (tx行,ty列)), ...]，可以用它把对局历史喂给引擎
    （引擎要看到着法才判得了重复局面）。没有变化或解释不了时返回空列表。

    类型：
        'same'       没有变化
        'reset'      回到了标准开局（重开一局）
        'one_move'   恰好一个合法着法（含吃子）
        'multi_move' 能用 2~max_steps 步合法着法解释——这是**正常情况**，不是异常
        'noisy'      解释不了——才真的可疑（识别抖动或走子动画中间态）

    为什么允许"多步"：管道确认一次盘面要好几秒（采样间隔 + 推理 + 连续帧确认），
    而正常对局里双方各走一步只要 2~3 秒。也就是说管道眼里的"相邻两帧"，
    实际上经常跨了 2 步棋（我方一步 + 对方一步）。老版本硬要求"恰好一步"，
    结果只要棋局在正常推进就必然误报，把正常对局一直说成"对不上"。

    为什么要单独处理吃子：被吃的那个子在两帧里"同位置但换了主人"，
    会**同时**被算进两侧的差异，于是最普通的一步吃子会变成 2:1 而判成异常。
    所以先把这类格子摘出来——它属于"被吃"，不是"走子"。

    为什么还要 first_side（2026-09-29 实测的坑）：`move_legal` 只判几何形状，
    不看轮到谁走。两步**互不相干**的走子（不互吃、路径不冲突）正反两种顺序
    在几何上都成立，于是下面那句 `permutations` 会取到遍历顺序决定的那个，
    也就是行号小的那个——黑方子力普遍靠上，于是"黑先红后"。
    实测这样喂给 pikafish 是
        info string CRITICAL ERROR: ... Reason: Illegal move: h7f7
    然后进程**自己退出**。所以给了 first_side（"R"/"B"）就只接受
    "首手归它、之后逐手交替"的排列；一条都排不出来时返回 noisy，
    让上层去决定是纠正轮次还是丢掉历史——绝不能就这么送进引擎。
    """
    p, q = pieces(prev), pieces(cur)
    if p == q:
        return "same", "", []

    if dict(q) == SETUP:
        return "reset", "回到标准开局", []

    gone = [k for k in p if p.get(k) != q.get(k)]
    came = [k for k in q if q.get(k) != p.get(k)]

    # 被吃的子：同位置在两侧都出现、且颜色不同
    eaten = [k for k in gone if k in came and p[k][0] != q[k][0]]
    movers = [k for k in gone if k not in eaten]

    if not movers and not came:
        return "noisy", "只有子力被吃、没有任何子移动", []
    if len(movers) != len(came):
        return "noisy", "消失 {} 处、出现 {} 处，对不上".format(
            len(movers), len(came)), []
    if len(movers) > max_steps:
        return "noisy", "一次动了 {} 处（上限 {} 步）".format(
            len(movers), max_steps), []

    # 枚举"谁走到哪里"和"谁先谁后"，两者都要穷举。
    #
    # 顺序为什么也得枚举（2026-09-29 的坑）：原先只重排"目的地"，走子的先后
    # 等于 `movers` 的遍历顺序，也就是盘面的行优先顺序。两步互不相干的走子
    # 谁先谁后都合法，于是"先后"就由行号决定了——黑方子力普遍靠上，
    # 结果**黑方那一手永远排在前面**，标着红先的 FEN 配黑先的着法，
    # 引擎判 Illegal move 后自杀退出。
    #
    # 步数很少（≤3），配对 × 顺序最多 3!×3! = 36 种，直接穷举最可靠。
    # 顺序的第一个排列就是原来的遍历顺序，所以不给 first_side 时结果与旧版一致。
    n = len(movers)
    for perm in itertools.permutations(came):
        for order in itertools.permutations(range(n)):
            work = dict(p)
            seq, ok = [], True
            want = first_side
            for i in order:
                frm, to = movers[i], perm[i]
                legal, _why = move_legal(work, frm, to)
                if not legal:
                    ok = False
                    break
                # 轮次也要对上：起点 FEN 标的是哪一方先走，第一手就得归那一方
                if want and work[frm][0] != want:
                    ok = False
                    break
                work[to] = work.pop(frm)   # 逐走着法，后一步要在前一步之后的盘面上判定
                seq.append((frm, to))
                if want:
                    want = "B" if want == "R" else "R"
            if not ok or work != q:        # 重放完必须与目标盘面完全一致
                continue
            if len(seq) == 1:
                frm, to = seq[0]
                return "one_move", "{} {}->{}".format(p[frm], frm, to), seq
            return "multi_move", "{} 步：{}".format(
                len(seq), "，".join("{} {}->{}".format(p[f], f, t)
                                    for f, t in seq)), seq

    if first_side is not None:
        return "noisy", "变化能用 ≤{} 步解释，但轮次对不上（该{}先走）".format(
            max_steps, "红方" if first_side == "R" else "黑方"), []
    if len(movers) == 1:
        frm, to = movers[0], came[0]
        _legal, why = move_legal(p, frm, to)
        return "noisy", "{} {}->{} 不合法（{}）".format(p[frm], frm, to, why), []
    return "noisy", "{} 处变化无法用 ≤{} 步合法着法解释".format(
        len(movers) + len(eaten), max_steps), []


def diff(prev, cur, max_steps=3, first_side=None):
    """只要 (类型, 说明) 的便捷入口，保持旧调用方式可用。"""
    kind, why, _seq = explain_change(prev, cur, max_steps, first_side)
    return kind, why


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
