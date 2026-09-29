# -*- coding: utf-8 -*-
"""置顶浮窗：显示自动教练给出的下一手。

只用标准库，用带 tkinter 的 Python 跑（start.ps1 会自动挑）：
    python hud.py

读 out/suggestion.json（由 auto_coach.py 写），所以要两个一起开。
可拖动，Esc 退出。「重开一局」按钮写 out/restart.flag。
"""
import json
import os
import sys
import time
import tkinter as tk

from applog import get_logger, install_excepthook

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 代码在 app/ 下，上一级才是项目根
SRC = os.path.join(ROOT, "out", "suggestion.json")
# 重置信号：写这个文件，auto_coach 下一轮就会清空上一局的记忆重来。
# 两个进程不共享内存（浮窗借系统 Python、识别在隔离环境），只能靠文件通信。
FLAG = os.path.join(ROOT, "out", "restart.flag")

log = get_logger("hud")

BG = "#16181a"
FG = "#d8dee3"
MUTED = "#98a1a9"
ACCENT = "#c9a227"
WARN = "#d05f45"
BTN = "#23282c"
BTN_HI = "#31383d"


class Hud:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("象棋教练")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.geometry("+{}+{}".format(self.root.winfo_screenwidth() - 300, 60))

        frm = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        frm.pack()

        tk.Label(frm, text="下一手", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 10)).pack(anchor="w")

        self.lbl_move = tk.Label(frm, text="等待中…", bg=BG, fg=ACCENT,
                                 font=("Microsoft YaHei", 24, "bold"))
        self.lbl_move.pack(anchor="w", pady=(2, 0))

        self.lbl_score = tk.Label(frm, text="", bg=BG, fg=FG,
                                  font=("Microsoft YaHei", 11))
        self.lbl_score.pack(anchor="w")

        self.lbl_status = tk.Label(frm, text="", bg=BG, fg=MUTED,
                                   font=("Microsoft YaHei", 9), wraplength=260,
                                   justify="left")
        self.lbl_status.pack(anchor="w", pady=(6, 0))

        # 一局下完后按它：清掉上一局的着法历史、轮次和子力基线，按当前局面重来。
        # 这里做的动作很小（只留个信号文件），真正的重置在 auto_coach 下一轮里发生。
        self.btn_restart = tk.Button(
            frm, text="重开一局", command=self.restart,
            bg=BTN, fg=FG, activebackground=BTN_HI, activeforeground=ACCENT,
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            font=("Microsoft YaHei", 9), padx=10, pady=3)
        self.btn_restart.pack(anchor="w", pady=(10, 0))

        tk.Label(frm, text="拖动移动 · Esc 退出", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 8)).pack(anchor="w", pady=(8, 0))

        self.lbl_move.bind("<Button-1>", self.start_move)
        self.lbl_move.bind("<B1-Motion>", self.on_move)
        self.lbl_score.bind("<Button-1>", self.start_move)
        self.lbl_score.bind("<B1-Motion>", self.on_move)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self.shown_move = None
        self.last_err = None        # 上一次的读文件错误，用来去重日志
        self.no_file = False        # 只在第一次"读不到文件"时记一条

    def restart(self):
        """请 auto_coach 清空上一局的记忆，按当前局面重新开始。

        浮窗只是个读 suggestion.json 的旁观者，能做的只有留个信号文件；
        真正的重置发生在识别进程的下一轮循环里，所以这里成功与否
        只看文件有没有写下去，不需要等它回应。
        """
        try:
            os.makedirs(os.path.dirname(FLAG), exist_ok=True)
            with open(FLAG, "w", encoding="utf-8") as f:
                f.write("{:.3f}\n".format(time.time()))
        except OSError as e:
            self.lbl_status.config(text="写重置信号失败: {}".format(e), fg=WARN)
            log.exception("写重置信号失败（{}）".format(os.path.relpath(FLAG, ROOT)), e)
            return
        log.info("已请求重开一局（写入 {}）".format(os.path.relpath(FLAG, ROOT)))
        # 这行提示最多停 0.4 秒（下一次 tick 就把它顶掉），
        # 真正让人看到回执的是 auto_coach 写回的"已重置…"那句状态
        self.lbl_status.config(text="已请求重开一局，等 auto_coach 响应…", fg=ACCENT)

    def start_move(self, e):
        self.dx, self.dy = e.x, e.y

    def on_move(self, e):
        self.root.geometry("+{}+{}".format(
            self.root.winfo_rootx() + e.x - self.dx,
            self.root.winfo_rooty() + e.y - self.dy))

    def tick(self):
        try:
            with open(SRC, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            if not self.no_file:
                self.no_file = True
                log.info("还没等到 {}（auto_coach 没在跑？）".format(
                    os.path.relpath(SRC, ROOT)))
            self.lbl_status.config(text="等 auto_coach.py 写出结果…", fg=MUTED)
        except Exception as e:
            self.lbl_status.config(text=f"读取失败: {e}", fg=WARN)
            if self.last_err != str(e):     # 同一句错误别刷屏
                self.last_err = str(e)
                log.exception("读 suggestion.json 失败（{}）".format(
                    os.path.relpath(SRC, ROOT)), e)
        else:
            mv = data.get("move")
            if mv:
                sig = (mv.get("cn"), mv.get("score"), mv.get("depth"))
                if sig != self.shown_move:
                    self.shown_move = sig
                    log.info("显示: {}  评分 {}  深度 {}".format(
                        mv.get("cn"), mv.get("score"), mv.get("depth")))
                    self.lbl_move.config(text=mv.get("cn", "?"))
                    sc = mv.get("score")
                    if sc is not None:
                        # 引擎评分以"我方"为视角（FEN 一直标成我方走）
                        self.lbl_score.config(
                            text="我方 {:+.2f}    深度 {}".format(
                                sc / 100, mv.get("depth")))
                    else:
                        self.lbl_score.config(text="")
            st = data.get("status", "")
            if st:
                self.lbl_status.config(text=st, fg=MUTED)
            elif mv:
                self.lbl_status.config(
                    text="[{}]  深度 {}  ·  {} 子  ·  历史 {} 步".format(
                        mv.get("mv", ""), mv.get("depth", "?"), mv.get("n", ""),
                        mv.get("history", 0)),
                    fg=MUTED)
        self.root.after(400, self.tick)

    def run(self):
        self.tick()
        self.root.mainloop()


if __name__ == "__main__":
    install_excepthook(log)
    log.info("浮窗启动 pid={}（Python {}）".format(os.getpid(), sys.version.split()[0]))
    Hud().run()
