"""日志初始化：轮转文件 + stdout，跨重启保留，未捕获异常带完整 traceback。"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import threading

from . import config

log = logging.getLogger("proxy")

_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_file_handler: logging.handlers.RotatingFileHandler | None = None


def setup() -> None:
    """按配置装配 root logger。可重复调用（热重载时只改级别，不重建 handler）。"""
    global _file_handler
    lcfg = config.cfg().get("logging", {})
    level = getattr(logging, str(lcfg.get("level", "INFO")).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if not any(getattr(h, "_cproxy_stdout", False) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(_FMT))
        sh._cproxy_stdout = True  # type: ignore[attr-defined]
        root.addHandler(sh)

    path = lcfg.get("file")
    if path and _file_handler is None:
        path = config.resolve_path(str(path))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # mode="a"：进程重启不截断，崩溃现场保留
        fh = logging.handlers.RotatingFileHandler(
            path, mode="a", maxBytes=int(lcfg.get("max_bytes", 20 * 1024 * 1024)),
            backupCount=int(lcfg.get("backup_count", 5)), encoding="utf-8", delay=False)
        fh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fh)
        _file_handler = fh

    for h in root.handlers:
        h.setLevel(logging.NOTSET)

    # uvicorn 自带的 logger 交回 root，统一进同一个文件
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)


def install_crash_handlers() -> None:
    """把线程/事件循环/解释器级别的未捕获异常也写进日志，避免崩溃无迹可寻。"""

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("未捕获异常导致进程退出", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook

    def _thread_hook(args):
        log.critical("线程 %s 未捕获异常", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_hook

    if hasattr(sys, "unraisablehook"):
        def _unraisable(args):
            log.error("unraisable 异常: %r", args.object,
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        sys.unraisablehook = _unraisable


def install_loop_handler(loop: asyncio.AbstractEventLoop) -> None:
    def _handler(_loop, context):
        exc = context.get("exception")
        msg = context.get("message", "")
        if exc is not None:
            log.error("事件循环未捕获异常: %s", msg, exc_info=exc)
        else:
            log.error("事件循环异常: %s (%r)", msg, context)

    loop.set_exception_handler(_handler)
