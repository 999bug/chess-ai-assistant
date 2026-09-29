# -*- coding: utf-8 -*-
"""
JJ 象棋窗口探测工具（只读）

用法：
    python probe.py list                 枚举所有可见窗口，标出可疑目标
    python probe.py grab  --auto         对命中的窗口用 4 种方式各截一张
    python probe.py grab  --title 象棋    按标题关键词指定窗口
    python probe.py crop  --title 象棋 --preset moves   只截左下角棋谱区并放大 2 倍

输出写在 out/ 目录下，并对每张图自动判断是否为黑屏。
"""
import argparse
import ctypes

user32 = ctypes.windll.user32  # ctypes.windll 是 ctypes 的属性，不能 import
import io
import os
import sys
import time

# 必须在调用任何窗口 API 之前设置 DPI 感知，否则 GetWindowRect 返回的坐标会和截图对不上
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAwareness()
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tools/ 的上一级
OUT_DIR = os.path.join(ROOT, "out")

KEYWORDS = ["象棋", "JJ", "jj", "WeChat", "微信", "应用宝", "MyApp", "Androws",
            "MuMu", "雷电", "LDPlayer", "夜神", "BlueStacks", "手游助手", "模拟器"]


def setup_stdout():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _ts():
    return time.strftime("%H%M%S")


def is_desktop_locked():
    """输入桌面打不开 = 会话被锁定。锁屏状态下截到的只会是壁纸，且无法唤醒窗口。"""
    DESKTOP_SWITCHDESKTOP = 0x0100
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if h:
        user32.CloseDesktop(h)
        return False
    return True


def proc_of(hwnd):
    """返回 (pid, 进程可执行文件名)。小程序/模拟器这类套壳窗口，光看标题认不出来，必须看进程。"""
    try:
        import os
        import win32api
        import win32con
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            full = win32process.GetModuleFileNameEx(h, 0)
        finally:
            win32api.CloseHandle(h)
        return pid, os.path.basename(full)
    except Exception as e:
        try:
            return pid, f"<{type(e).__name__}>"
        except Exception:
            return -1, "?"


def list_windows(include_hidden=False):
    """枚举可见窗口。捕获到的异常会记进 errors 并返回，绝不静默吞掉——
    上次就是 GetClassName 拼错被 pass 掉，导致结果全空还查不出原因。"""
    import win32gui
    rows = []
    errors = []

    def cb(h, _):
        try:
            visible = bool(win32gui.IsWindowVisible(h))
            if not visible and not include_hidden:
                return
            title = win32gui.GetWindowText(h) or ""
            cls = win32gui.GetClassName(h) or ""  # 是 GetClassName，没有 GetWindowClassName
            l, t, r, b = win32gui.GetWindowRect(h)
            w, hh = r - l, b - t
            if w < 80 or hh < 80:
                return
            # 应用宝(Androws.exe)的窗口失焦后会变成不可见状态，只按可见性筛选会漏
            # 所以这里把 visible/iconic 记下来，交给调用方决定是否还原
            iconic = bool(user32.IsIconic(h))
            pid, proc = proc_of(h)
            rows.append({"hwnd": h, "title": title, "cls": cls, "rect": (l, t, w, hh),
                         "pid": pid, "proc": proc, "visible": visible, "iconic": iconic})
        except Exception as e:
            errors.append((h, type(e).__name__, str(e)))

    win32gui.EnumWindows(cb, None)
    return rows, errors


def is_candidate(row):
    blob = (row["title"] or "") + " " + (row["cls"] or "") + " " + (row.get("proc") or "")
    return any(k in blob for k in KEYWORDS)


def pick_window(title_kw, include_hidden=True):
    rows, errors = list_windows(include_hidden=include_hidden)
    if title_kw:
        hit = [r for r in rows if title_kw.lower() in
               ((r["title"] or "") + (r["cls"] or "") + (r["proc"] or "")).lower()]
    else:
        hit = [r for r in rows if is_candidate(r)]
    return hit, rows, errors


def bring_to_front(hwnd):
    """把窗口唤起到前台。

    Windows 有前台锁：不是前台进程时，SetForegroundWindow 常常静默失败。
    所以按可靠性从高到低串一串。
    返回 (是否成功, 用了哪一步)
    """
    import win32gui
    import win32con
    import win32process
    import win32api

    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002

    def ok():
        return win32gui.GetForegroundWindow() == hwnd

    if ok():
        return True, "already-foreground"

    # 1) 最小化的话先还原
    try:
        if user32.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.35)
            if ok():
                return True, "SW_RESTORE"
    except Exception:
        pass

    # 2) 直给
    try:
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        if ok():
            return True, "SetForegroundWindow"
    except Exception:
        pass

    # 3) AttachThreadInput：把当前线程挂到前台线程上，骗过前台锁
    try:
        fg = win32gui.GetForegroundWindow()
        if fg:
            fg_tid, _ = win32process.GetWindowThreadProcessId(fg)
            cur_tid = win32api.GetCurrentThreadId()
            if fg_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, fg_tid, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.25)
                finally:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
                if ok():
                    return True, "AttachThreadInput"
    except Exception:
        pass

    # 4) Alt 键 trick：按一下 Alt 让系统允许本次抢前台
    try:
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        if ok():
            return True, "Alt-trick"
    except Exception:
        pass

    # 5) SwitchToThisWindow（已废弃但仍可用）
    try:
        user32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.3)
        if ok():
            return True, "SwitchToThisWindow"
    except Exception:
        pass

    return False, "全部失败"


def stats_from_pil(img):
    import numpy as np
    a = np.asarray(img.convert("RGB"))
    try:
        colors = len(__import__("numpy").unique(a.reshape(-1, 3), axis=0))
    except Exception:
        colors = -1
    return float(a.mean()), float(a.std()), colors


def verdict(mean, std):
    if mean < 6 or std < 2:
        return "黑屏 / 纯色"
    if std < 12:
        return "可疑（内容极少）"
    return "正常"


def save_and_report(img, name):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{name}_{_ts()}.png")
    img.save(path)
    mean, std, colors = stats_from_pil(img)
    v = verdict(mean, std)
    print(f"  {name:<14} {img.width}x{img.height}  亮度均值={mean:6.2f} 标准差={std:6.2f} "
          f"颜色数={colors:<7} -> {v}")
    print(f"    saved: {path}")
    return path, v


def grab_mss(rect):
    from PIL import Image
    import mss
    x, y, w, h = rect
    with mss.MSS() as sct:
        raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def grab_dxcam(rect):
    from PIL import Image
    import dxcam
    cam = dxcam.create(output_color="RGB")
    if cam is None:
        raise RuntimeError("dxcam.create() 返回 None（显卡/输出口不兼容）")
    x, y, w, h = rect
    frame = None
    # dxcam 只在有新帧时返回，轮询几次
    for _ in range(20):
        frame = cam.grab(region=(x, y, x + w, y + h))
        if frame is not None:
            break
        time.sleep(0.05)
    if frame is None:
        raise RuntimeError("dxcam 取帧失败（ region 超出输出范围或无新帧）")
    return Image.fromarray(frame)


def grab_printwindow(hwnd, rect):
    import win32gui
    import win32ui
    from PIL import Image

    x, y, w, h = rect
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        # PW_RENDERFULLCONTENT(2) 才是能抓 GPU 合成窗口的那个 flag
        ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               bits, "raw", "BGRX", 0, 1)
        img.load()  # 在释放位图前把像素读实
        if not ok:
            pass  # 返回 0 也可能有内容，交给黑屏检测判断
        return img
    finally:
        # 清理顺序必须是 位图 -> 内存DC -> 源DC，顺序反了会 DeleteDC failed 丢图
        for step in (lambda: win32gui.DeleteObject(bmp.GetHandle()),
                     save_dc.DeleteDC,
                     mfc_dc.DeleteDC,
                     lambda: win32gui.ReleaseDC(hwnd, hwnd_dc)):
            try:
                step()
            except Exception:
                pass


def grab_bitblt(hwnd, rect):
    import win32gui
    import win32ui
    from PIL import Image

    x, y, w, h = rect
    desktop = win32gui.GetDesktopWindow()
    dc = win32gui.GetWindowDC(desktop)
    mfc_dc = win32ui.CreateDCFromHandle(dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        save_dc.BitBlt((0, 0), (w, h), mfc_dc, (x, y), 0x00CC0020)  # SRCCOPY
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               bits, "raw", "BGRX", 0, 1)
        img.load()
        return img
    finally:
        for step in (lambda: win32gui.DeleteObject(bmp.GetHandle()),
                     save_dc.DeleteDC,
                     mfc_dc.DeleteDC,
                     lambda: win32gui.ReleaseDC(desktop, dc)):
            try:
                step()
            except Exception:
                pass


def ensure_foreground(hwnd):
    """把窗口弄到可截图状态：最小化就还原，然后抢前台。

    应用宝(Androws.exe)这类容器窗口失焦后会隐藏/最小化，直接截图会拿到空白，
    所以这一步在实际流程里是必须的。返回是否成功。
    """
    import win32con
    import win32gui

    if user32.IsIconic(hwnd):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.35)
        except Exception:
            pass
    ok, how = bring_to_front(hwnd)
    time.sleep(0.35)
    return ok, how


def cmd_list(args):
    rows, errors = list_windows(include_hidden=args.all)
    print(f"可见窗口共 {len(rows)} 个（宽高均 >= 80）")
    if errors:
        print(f"!! 枚举过程中 {len(errors)} 个窗口出错，首个错误：hwnd={errors[0][0]} "
              f"{errors[0][1]}: {errors[0][2]}")
    print()
    hits = [r for r in rows if is_candidate(r)]
    if hits:
        print("== 命中的可疑目标 ==")
        for r in hits:
            print(f'  hwnd={r["hwnd"]:<10} [{r["rect"][2]}x{r["rect"][3]}] '
                  f'({r["rect"][0]},{r["rect"][1]})  cls={r["cls"]}  title={r["title"]!r}')
    else:
        print("!! 没有命中任何关键词。JJ 象棋还没打开？")
    print("\n== 全部窗口 ==")
    for r in rows:
        mark = "*" if is_candidate(r) else " "
        print(f'{mark} hwnd={r["hwnd"]:<10} [{r["rect"][2]}x{r["rect"][3]}] '
              f'cls={r["cls"]:<26} proc={r["proc"]:<26} pid={r["pid"]:<8} title={r["title"]!r}')


def cmd_grab(args):
    import win32gui
    hits, all_rows, errors = pick_window(args.title)
    if errors:
        print(f"!! 枚举有 {len(errors)} 个错误（首个 {errors[0][1]}: {errors[0][2]}）")
    if not hits:
        print("!! 没找到目标窗口。先跑 `python probe.py list` 看看，"
              "或者用 --title 指定标题关键词。")
        return 1
    # 命中的可能有多层窗口（容器 + 渲染子窗口），取面积最大的那个
    target = sorted(hits, key=lambda r: r["rect"][2] * r["rect"][3], reverse=True)[0]
    hwnd = target["hwnd"]
    x, y, w, h = target["rect"]
    print(f'目标窗口 hwnd={hwnd} title={target["title"]!r} cls={target["cls"]}')
    print(f"位置 ({x},{y}) 尺寸 {w}x{h}\n")

    if is_desktop_locked():
        print("!! 屏幕当前处于锁定状态。锁屏下截到的只会是壁纸，且无法唤醒窗口。")
        print("   请解锁屏幕、把 JJ象棋 棋盘界面打开后再跑一次。\n")

    # 窗口可能被任务栏遮挡或超出屏幕，做一次裁剪
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    print(f"主屏分辨率 {screen_w}x{screen_h}\n")

    # 用户要求：截图时把 JJ 唤起到前台，否则截图可能被别的窗口盖住
    import win32gui
    if not args.no_focus:
        success, how = bring_to_front(hwnd)
        print(f"唤醒前台: {'成功' if success else '失败'}  ({how})")
        if not success:
            print("  !! 没能抢到前台。截图可能抓到别的窗口，结果不可信。")
        time.sleep(0.6)  # 等渲染稳定
    else:
        try:
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.4)
        except Exception as e:
            print(f"  置前失败（不影响截图）: {e}")

    rect = (x, y, w, h)
    results = []
    for name, fn in [
        ("mss", lambda: grab_mss(rect)),
        ("dxcam", lambda: grab_dxcam(rect)),
        ("printwindow", lambda: grab_printwindow(hwnd, (0, 0, w, h))),
        ("bitblt", lambda: grab_bitblt(hwnd, rect)),
    ]:
        print(f"[{name}]")
        try:
            img = fn()
            path, v = save_and_report(img, name)
            results.append((name, v, path))
        except Exception as e:
            print(f"  失败: {type(e).__name__}: {e}")
            results.append((name, "失败", None))
        print()

    print("== 结论 ==")
    good = [r for r in results if r[1] == "正常"]
    if good:
        print(f"可用方式 {len(good)}/{len(results)}：" + "、".join(r[0] for r in good))
        print("把对应 PNG 发我，我看一下左下角棋谱区在哪。")
    else:
        print("全部失败或黑屏。下一步：尝试关闭显卡硬件加速 / 让窗口不要全屏独占。")
    return 0


def cmd_crop(args):
    from PIL import Image
    hits, _ = pick_window(args.title)
    if not hits:
        print("!! 没找到目标窗口")
        return 1
    target = sorted(hits, key=lambda r: r["rect"][2] * r["rect"][3], reverse=True)[0]
    x, y, w, h = target["rect"]
    hwnd = target["hwnd"]

    success, how = bring_to_front(hwnd)
    print(f'目标窗口 hwnd={hwnd} title={target["title"]!r}')
    print(f"唤醒前台: {'成功' if success else '失败'}  ({how})")
    time.sleep(0.6)

    # 左下角棋谱区：默认取宽度 34%、高度 40%，贴着左边界和下边界
    presets = {
        "moves": (x, y + int(h * 0.60), int(w * 0.34), int(h * 0.40)),
        "moves_wide": (x, y + int(h * 0.45), int(w * 0.45), int(h * 0.55)),
    }
    cw, ch = int(w * 0.34), int(h * 0.40)
    if args.preset in presets:
        rect = presets[args.preset]
    else:
        rect = (args.left if args.left is not None else x,
                args.top if args.top is not None else y + h - ch,
                args.width or cw, args.height or ch)

    print(f"窗口 {w}x{h} @({x},{y}) -> 裁剪区 {rect[2]}x{rect[3]} @({rect[0]},{rect[1]})")
    img = None
    try:
        img = grab_dxcam(rect)
        used = "dxcam"
    except Exception as e:
        print(f"  dxcam 失败({e})，回退 mss")
        img = grab_mss(rect)
        used = "mss"
    print(f"  抓图方式: {used}")

    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"crop_{args.preset}_{_ts()}.png")
    img.save(path)
    print(f"  saved: {path}  ({img.width}x{img.height}, 已放大 2 倍)")
    print("  把这张图发我。")
    return 0


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="枚举窗口").add_argument(
        "--all", action="store_true", help="连同不可见/最小化的窗口一起列出")

    g = sub.add_parser("grab", help="四种方式各截一张")
    g.add_argument("--title", default=None, help="窗口标题关键词")
    g.add_argument("--auto", action="store_true", help="自动命中")
    g.add_argument("--no-focus", action="store_true", help="不要试图唤醒窗口到前台")

    c = sub.add_parser("crop", help="裁剪子区域")
    c.add_argument("--title", default=None)
    c.add_argument("--preset", default="moves", choices=["moves", "moves_wide"])
    c.add_argument("--left", type=int)
    c.add_argument("--top", type=int)
    c.add_argument("--width", type=int)
    c.add_argument("--height", type=int)

    args = ap.parse_args()
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "grab":
        return cmd_grab(args)
    if args.cmd == "crop":
        return cmd_crop(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
