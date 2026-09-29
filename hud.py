# -*- coding: utf-8 -*-
"""置顶浮窗：显示自动教练给出的下一手。

只用标准库，用带 tkinter 的 Python 跑（start.ps1 会自动挑）：
    python hud.py

读 out/suggestion.json（由 auto_coach.py 写），所以要两个一起开。
可拖动，Esc 退出。
"""
import json
import os
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "out", "suggestion.json")

BG = "#16181a"
FG = "#d8dee3"
MUTED = "#98a1a9"
ACCENT = "#c9a227"
WARN = "#d05f45"


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

        tk.Label(frm, text="拖动移动 · Esc 退出", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 8)).pack(anchor="w", pady=(8, 0))

        self.lbl_move.bind("<Button-1>", self.start_move)
        self.lbl_move.bind("<B1-Motion>", self.on_move)
        self.lbl_score.bind("<Button-1>", self.start_move)
        self.lbl_score.bind("<B1-Motion>", self.on_move)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self.shown_move = None

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
            self.lbl_status.config(text="等 auto_coach.py 写出结果…", fg=MUTED)
        except Exception as e:
            self.lbl_status.config(text=f"读取失败: {e}", fg=WARN)
        else:
            mv = data.get("move")
            if mv:
                sig = (mv.get("cn"), mv.get("score"), mv.get("depth"))
                if sig != self.shown_move:
                    self.shown_move = sig
                    self.lbl_move.config(text=mv.get("cn", "?"))
                    sc = mv.get("score")
                    if sc is not None:
                        self.lbl_score.config(
                            text="评分 {:+.2f}    深度 {}".format(sc / 100, mv.get("depth")))
                    else:
                        self.lbl_score.config(text="")
            st = data.get("status", "")
            if st:
                self.lbl_status.config(text=st, fg=MUTED)
            elif mv:
                self.lbl_status.config(
                    text="[{}]   盘面 {} 子".format(mv.get("mv", ""), mv.get("n", "")),
                    fg=MUTED)
        self.root.after(400, self.tick)

    def run(self):
        self.tick()
        self.root.mainloop()


if __name__ == "__main__":
    Hud().run()
