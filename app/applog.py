# -*- coding: utf-8 -*-
"""统一日志：写到项目根目录的 log/ 下，按天切分。

为什么要有这么个东西
--------------------
auto_coach 是被 start.ps1 用 -WindowStyle Hidden 拉起来的，print 出去的东西
在终端里看不到；等出问题再回头查时，往往只剩一句 UI 上的提示，什么现场都没有。
把关键现场（完整 FEN、校验没过的问题列表、引擎原始输出、异常栈）落到文件里，
事后才查得动。

为什么不放 out/
--------------
out/ 是运行期产物目录，截图跑一轮就一堆，被当成"可以随时清掉"的地方；
日志要留一段时间用来回溯，而且 log/ 是独立的一等目录（已加入 .gitignore）。
放 config/ 更不行——那是入库的配置。

刻意保持"笨"
------------
零依赖、不做异步、每行写完就 flush。我们每秒最多写几行，这点开销无所谓，
换来的是进程被强杀（引擎把父进程拖死、任务管理器结束进程）时日志不丢——
缓冲写在这种情况下会连着最后几行一起丢掉，而那几行恰恰是最想看的。

用法
----
    from applog import get_logger
    log = get_logger("auto_coach")
    log.info("帧 {} 校验未过: {}".format(n, "；".join(problems)))

环境变量：
    JJCHESS_LOG_LEVEL  debug|info|warn|error，默认 info
    JJCHESS_LOG_DIR    改日志目录（测试用），默认 <项目根>/log
"""
import datetime
import glob
import os
import sys
import threading
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_TAG = {10: "DEBUG", 20: "INFO ", 30: "WARN ", 40: "ERROR"}


def log_dir():
    """日志目录。每次都读环境变量，测起来方便。"""
    return os.environ.get("JJCHESS_LOG_DIR") or os.path.join(ROOT, "log")


def default_level():
    name = (os.environ.get("JJCHESS_LOG_LEVEL") or "info").strip().lower()
    return LEVELS.get(name, LEVELS["info"])


class Logger:
    """一个名字对应一天一个文件。写入失败绝不影响主流程。"""

    def __init__(self, name, level=None, keep_days=14):
        self.name = name
        self.level = default_level() if level is None else int(level)
        self.keep_days = keep_days
        self._fh = None
        self._day = None
        self._lock = threading.Lock()
        self._broken = False        # 磁盘满了 / 权限不对：只报一次，之后静默

    # ---- 文件管理 ----
    def path(self, day=None):
        day = day or time_day()
        return os.path.join(log_dir(), "{}-{}.log".format(self.name, day))

    def _handle(self):
        day = time_day()
        if self._fh is not None and self._day == day:
            return self._fh
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        os.makedirs(log_dir(), exist_ok=True)
        # 换天了顺手清一次过期日志（长驻进程跨天也能清到）
        self._prune()
        self._fh = open(self.path(day), "a", encoding="utf-8", newline="\n")
        self._day = day
        return self._fh

    def _prune(self):
        """删掉超过保留期的日志。只认自己写的文件名格式，别误伤别的文件。"""
        if self.keep_days <= 0:
            return
        cutoff = datetime.date.today() - datetime.timedelta(days=self.keep_days)
        for p in glob.glob(os.path.join(log_dir(), "*-*.log")):
            base = os.path.basename(p)[:-4]           # 去掉 .log
            try:                                      # 文件名末尾就是 YYYY-MM-DD
                day = datetime.date.fromisoformat(base[-10:])
            except ValueError:
                continue                              # 不是我们写的格式，别碰
            if day < cutoff:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def close(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    # ---- 写入 ----
    def log(self, level, msg):
        if level < self.level or self._broken:
            return
        head = "{} [{}] ".format(time_stamp(), _TAG.get(level, "INFO "))
        body = str(msg)
        # 多行（异常栈）只在第一行带前缀，后面按原样贴，读起来更像终端
        text = head + body + "\n"
        with self._lock:
            try:
                fh = self._handle()
                fh.write(text)
                fh.flush()
            except Exception:
                self._broken = True
                try:
                    sys.stderr.write("[applog] 日志写入失败，已停用文件日志: {}\n"
                                     .format(log_dir()))
                except Exception:
                    pass

    def debug(self, msg):
        self.log(LEVELS["debug"], msg)

    def info(self, msg):
        self.log(LEVELS["info"], msg)

    def warn(self, msg):
        self.log(LEVELS["warn"], msg)

    def error(self, msg):
        self.log(LEVELS["error"], msg)

    def exception(self, msg, exc=None):
        """记一条错误，带上异常栈。不传 exc 就取当前正在处理的异常。"""
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            self.error(msg)
            return
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.error("{}｜{}: {}\n{}".format(
            msg, type(exc).__name__, exc, tb.rstrip()))


def time_day():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def time_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def get_logger(name):
    """按名字取 logger（同名复用）。"""
    with _CACHE_LOCK:
        lg = _CACHE.get(name)
        if lg is None:
            lg = Logger(name)
            _CACHE[name] = lg
        return lg


def install_excepthook(logger, tag="未捕获异常"):
    """把没人接的异常也写进日志。

    没有它的时候，一个 OSError 就能让整个识别进程静默退出，
    而日志里干干净净——最难查的就是这种"什么都没留下"的退出。
    """
    prev = sys.excepthook

    def hook(tp, val, tb):
        try:
            text = "".join(traceback.format_exception(tp, val, tb)).rstrip()
            logger.error("{}｜{}: {}\n{}".format(tag, tp.__name__, val, text))
            logger.info("进程退出（上面那条异常没人接）")
        except Exception:
            pass
        prev(tp, val, tb)

    sys.excepthook = hook
    return hook
