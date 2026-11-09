from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from topps_auto_bot.driver_setup import current_platform_tag, detect_chrome, driver_binary_name


def main() -> int:
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    try:
        from topps_auto_bot.ui import configure_tcl_environment

        configure_tcl_environment()
        root = tk.Tk()
        root.withdraw()
        root.update()
        root.destroy()
        print(f"Tk: {tk.TkVersion}")
    except Exception as exc:  # noqa: BLE001
        print(f"Tk: 不可用：{exc}")
        return 1
    try:
        import selenium

        print(f"Selenium: {selenium.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"Selenium: 不可用：{exc}")
        return 1
    chrome = detect_chrome()
    if chrome is None:
        print("Chrome: 未找到")
        return 1
    print(f"Chrome: {chrome.version} ({chrome.path})")
    print(f"平台: {current_platform_tag()}；驱动文件名: {driver_binary_name(current_platform_tag())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
