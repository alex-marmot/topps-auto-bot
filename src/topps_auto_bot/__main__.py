from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import SingleInstanceLock
from .settings import AppPaths, default_paths


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Topps.com 限量直购桌面辅助工具")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="开发/测试用数据目录；默认使用当前用户的 ToppsAutoBot 数据目录",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
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
