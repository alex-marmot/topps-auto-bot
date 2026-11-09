from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

from .runner import SingleInstanceLock
from .settings import AppPaths, default_paths


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Topps.com 限量直购桌面辅助工具")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--check-runtime", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="开发/测试用数据目录；默认使用当前用户的 ToppsAutoBot 数据目录",
    )
    return parser.parse_args(argv)


def _windows_app_mutex():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, "Local\\ToppsAutoBot.Application")
    if not handle:
        return None
    installer = kernel.OpenMutexW(0x00100000, False, "Local\\ToppsAutoBot.Setup")
    if installer:
        kernel.CloseHandle(installer)
        kernel.CloseHandle(handle)
        return None
    return lambda: kernel.CloseHandle(handle)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.check_runtime:
        import tkinter
        import undetected_chromedriver

        return 0 if tkinter.Tcl().eval("info patchlevel") and undetected_chromedriver.Chrome else 2
    release_mutex = _windows_app_mutex() if sys.platform == "win32" else lambda: None
    if release_mutex is None:
        return 1
    try:
        return _run(args)
    finally:
        release_mutex()


def _run(args: argparse.Namespace) -> int:
    paths = AppPaths.under(args.data_dir) if args.data_dir is not None else default_paths()
    paths.ensure_directories()
    lock = SingleInstanceLock(paths.root / "app.lock")
    if not lock.acquire():
        print("同一用户数据目录已有程序实例在运行。", file=sys.stderr)
        return 1
    try:
        try:
            from .ui import run_ui
        except ImportError as exc:
            print(f"无法启动图形界面：{exc}", file=sys.stderr)
            return 2
        run_ui(paths)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
