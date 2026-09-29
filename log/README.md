# 运行日志目录（内容不入库，见 .gitignore）

`auto_coach.py` / `hud.py` / `coach.py` 的运行日志都写在这里，按天切分：

```
log/auto_coach-2026-09-29.log     识别 + 引擎主循环的日志
log/hud-2026-09-29.log            浮窗进程的日志
log/coach-YYYY-MM-DD.log          命令行 coach.py 的日志
log/coach.out.log                 主循环的 stdout/stderr（start.ps1 重定向）
```

默认保留 14 天，超过的会在下次打开日志文件时自动清掉。
想看更啰嗦的内容，启动前设 `JJCHESS_LOG_LEVEL=debug`。
