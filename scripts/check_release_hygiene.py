from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED_SAMPLES = (
    "config.json",
    "config.json.tmp",
    "run-state.json",
    "browser-profile/Default/Cookies",
    "drivers/156/chromedriver.exe",
    "driver-staging/op/chromedriver.zip",
    "logs/app.log",
    "screenshots/checkout.png",
)
ALLOWED_EXAMPLE = "config.example.json"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)


def main() -> int:
    problems: list[str] = []
    for sample in IGNORED_SAMPLES:
        result = git("check-ignore", "-q", "--no-index", sample)
        if result.returncode != 0:
            problems.append(f"未被忽略：{sample}")
    example = git("check-ignore", "-q", "--no-index", ALLOWED_EXAMPLE)
    if example.returncode == 0:
        problems.append(f"示例文件不应被忽略：{ALLOWED_EXAMPLE}")

    tracked = git("ls-files", "-z")
    if tracked.returncode == 0:
        for path in tracked.stdout.split("\0"):
            if not path or path == ALLOWED_EXAMPLE:
                continue
            base = Path(path).name
            if base in {"config.json", "run-state.json"}:
                problems.append(f"真实数据被跟踪：{path}")
            if any(part in {"browser-profile", "drivers", "driver-staging", "logs", "screenshots"} for part in Path(path).parts):
                problems.append(f"用户数据目录被跟踪：{path}")

    packages = (ROOT / "dist" / "ToppsAutoBot", ROOT / "dist" / "macos-arm64" / "ToppsAutoBot.app")
    forbidden_names = {"config.json", "run-state.json", "browser-profile", "drivers", "driver-staging", "logs", "screenshots"}
    for package in packages:
        for path in package.rglob("*"):
            if path.name in forbidden_names:
                problems.append(f"构建产物包含禁止项：{path.relative_to(ROOT)}")

    if problems:
        print("发布卫生检查失败：", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("发布卫生检查通过：忽略规则、跟踪清单和现有构建目录未发现真实用户数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
