from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from topps_auto_bot.browser import verify_blank_browser
from topps_auto_bot.driver_setup import (
    CfTClient,
    DriverOperation,
    current_platform_tag,
    detect_chrome,
    download_driver_to_staging,
    validate_staged_driver,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "topps-auto-bot-test")
    cache_dir = parser.parse_args().cache_dir
    chrome = detect_chrome()
    if chrome is None:
        print("未检测到 Chrome，无法准备测试驱动。", file=sys.stderr)
        return 1
    platform_tag = current_platform_tag()
    client = CfTClient(timeout=60.0)
    metadata = client.fetch_metadata()
    download = client.find_compatible(metadata, chrome.version, platform_tag)
    print(f"Chrome {chrome.version}; chromedriver {download.version}; platform {platform_tag}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "chromedriver.exe" if platform_tag.startswith("win") else "chromedriver"
    destination = cache_dir / f"{platform_tag}-{binary_name}"
    with TemporaryDirectory(prefix="topps-test-driver-") as raw_tmp:
        staging_root = Path(raw_tmp)
        staged = download_driver_to_staging(
            download,
            staging_root=staging_root,
            operation=DriverOperation("test-driver"),
        )
        result = validate_staged_driver(
            staged,
            chrome_major=chrome.major_version,
            platform_tag=platform_tag,
            browser_verifier=lambda **kwargs: verify_blank_browser(**kwargs, use_uc=False),
            chrome_path=str(chrome.path),
            chrome_version=chrome.version,
        )
        if not result.ok:
            print(f"驱动验证失败：{result.error}", file=sys.stderr)
            return 2
        shutil.copy2(staged.path, destination)
        destination.chmod(destination.stat().st_mode | 0o100)
    print(f"测试驱动已缓存：{destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
