from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .models import DomainError, parse_utc_iso
from .settings import AppPaths, ConfigSaveError, save_config

CURRENT_CHROME_PLATFORM_HOST = "googlechromelabs.github.io"
KNOWN_GOOD_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "known-good-versions-with-downloads.json"
)
DOWNLOAD_HOST = "storage.googleapis.com"
DOWNLOAD_PATH_PREFIX = "/chrome-for-testing-public/"
MAX_ZIP_MEMBERS = 200
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
CHROME_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")


class DriverSetupError(RuntimeError):
    pass


class DriverOperationCancelled(DriverSetupError):
    pass


class DriverMetadataError(DriverSetupError):
    pass


class DriverDownloadError(DriverSetupError):
    pass


class DriverValidationError(DriverSetupError):
    pass


class DriverActivationError(DriverSetupError):
    pass


class DriverNotReady(DriverSetupError):
    pass


@dataclass(frozen=True)
class ChromeInstallation:
    path: Path
    version: str
    architecture: str

    @property
    def major_version(self) -> int:
        return int(self.version.split(".", 1)[0])


@dataclass(frozen=True)
class DriverDownloadInfo:
    version: str
    platform: str
    url: str


@dataclass
class DriverOperation:
    operation_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    activation_allowed: bool = True
    staging_dir: Path | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set() or not self.activation_allowed

    def cancel(self) -> None:
        self.activation_allowed = False
        self.cancel_event.set()


@dataclass(frozen=True)
class StagedDriver:
    operation_id: str
    path: Path
    version: str
    platform: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    version: str | None
    platform: str | None
    error: str | None = None


@dataclass(frozen=True)
class DownloadProgress:
    downloaded_bytes: int
    total_bytes: int | None
    stage: str


@dataclass(frozen=True)
class ActivatedDriver:
    settings: object
    path: Path
    version: str


def current_platform_tag(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    system = (system or platform.system()).casefold()
    machine = (machine or platform.machine()).casefold()
    if system.startswith("win"):
        return "win64" if machine in ("amd64", "x86_64", "arm64") else "win32"
    if system == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if machine in ("aarch64", "arm64"):
        return "linux-arm64"
    return "linux64"


def driver_binary_name(platform_tag: str) -> str:
    return "chromedriver.exe" if platform_tag.startswith("win") else "chromedriver"


def _run_version(command: list[str], *, timeout: float = 15.0) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DriverValidationError(f"无法执行版本检查：{exc}") from exc
    return f"{completed.stdout}\n{completed.stderr}".strip()


def _extract_version(output: str) -> str:
    match = CHROME_VERSION_RE.search(output)
    if not match:
        raise DriverValidationError("无法读取版本号")
    return match.group(1)


def inspect_chrome(path: Path, *, runner: Callable[..., str] = _run_version) -> ChromeInstallation:
    path = Path(path)
    if not path.is_file():
        raise DriverValidationError(f"Chrome 文件不存在：{path}")
    version = _extract_version(runner([str(path), "--version"]))
    return ChromeInstallation(
        path=path,
        version=version,
        architecture=current_platform_tag(),
    )


def chrome_candidate_paths(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    system = (system or platform.system()).casefold()
    environ = environ or os.environ
    candidates: list[Path] = []
    if system.startswith("win"):
        for base in (
            environ.get("PROGRAMFILES"),
            environ.get("PROGRAMFILES(X86)"),
            environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif system == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        candidates.append(Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            resolved = shutil.which(name)
            if resolved:
                candidates.append(Path(resolved))
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def detect_chrome(
    *,
    candidates: list[Path] | None = None,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] = _run_version,
) -> ChromeInstallation | None:
    for candidate in candidates if candidates is not None else chrome_candidate_paths(system=system, environ=environ):
        try:
            return inspect_chrome(candidate, runner=runner)
        except DriverValidationError:
            continue
    return None


def _check_https_source(url: str, *, allowed_host: str, path_prefix: str | None = None) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https":
        raise DriverMetadataError("官方来源必须使用 HTTPS")
    if (parsed.hostname or "").casefold() != allowed_host:
        raise DriverMetadataError(f"来源主机不是官方允许主机：{allowed_host}")
    if parsed.username is not None or parsed.password is not None:
        raise DriverMetadataError("来源 URL 不能包含用户信息")
    if parsed.port not in (None, 443):
        raise DriverMetadataError("来源 URL 不能使用非默认端口")
    if path_prefix is not None and not parsed.path.startswith(path_prefix):
        raise DriverMetadataError("来源路径不在官方下载目录内")


class CfTClient:
    def __init__(
        self,
        *,
        opener: Callable[..., object] = urllib.request.urlopen,
        timeout: float = 30.0,
    ) -> None:
        self._opener = opener
        self._timeout = timeout

    def fetch_metadata(self) -> dict:
        try:
            response = self._opener(KNOWN_GOOD_URL, timeout=self._timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DriverMetadataError(f"无法读取官方版本元数据：{exc}") from exc
        try:
            final_url = response.geturl()
            _check_https_source(final_url, allowed_host=CURRENT_CHROME_PLATFORM_HOST)
            payload = response.read()
            return json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriverMetadataError(f"官方版本元数据无效：{exc}") from exc
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()

    def find_compatible(
        self,
        metadata: dict,
        chrome_version: str,
        platform_tag: str,
    ) -> DriverDownloadInfo:
        try:
            known_versions = metadata["versions"]
        except (KeyError, TypeError) as exc:
            raise DriverMetadataError("官方元数据缺少 versions") from exc
        major = chrome_version.split(".", 1)[0]
        matches: list[DriverDownloadInfo] = []
        for item in known_versions:
            if not isinstance(item, dict):
                continue
            version = item.get("version")
            if not isinstance(version, str) or not version.startswith(f"{major}."):
                continue
            downloads = item.get("downloads") or {}
            candidates = downloads.get("chromedriver") or []
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("platform") != platform_tag:
                    continue
                url = candidate.get("url")
                if not isinstance(url, str):
                    continue
                try:
                    _check_https_source(
                        url,
                        allowed_host=DOWNLOAD_HOST,
                        path_prefix=DOWNLOAD_PATH_PREFIX,
                    )
                except DriverMetadataError:
                    continue
                matches.append(DriverDownloadInfo(version=version, platform=platform_tag, url=url))
        if not matches:
            raise DriverMetadataError(
                f"没有找到与 Chrome {chrome_version} 和平台 {platform_tag} 匹配的官方驱动"
            )

        def _version_key(info: DriverDownloadInfo) -> tuple[int, ...]:
            return tuple(int(part) for part in info.version.split("."))

        matches.sort(key=_version_key)
        return matches[-1]


def inspect_binary_platform(path: Path) -> str | None:
    try:
        header = Path(path).read_bytes()[:64]
    except OSError:
        return None
    if header.startswith(b"MZ") and len(header) >= 0x40:
        try:
            pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
            with open(path, "rb") as handle:
                handle.seek(pe_offset)
                pe_header = handle.read(6)
        except OSError:
            return None
        if pe_header[:4] != b"PE\0\0":
            return None
        machine = struct.unpack_from("<H", pe_header, 4)[0]
        return {0x8664: "win64", 0x014C: "win32", 0xAA64: "win64"}.get(machine)
    if header.startswith(b"\x7fELF") and len(header) >= 20:
        machine = struct.unpack_from("<H", header, 18)[0]
        return {0x3E: "linux64", 0xB7: "linux-arm64"}.get(machine)
    if len(header) >= 8:
        magic_le = struct.unpack_from("<I", header, 0)[0]
        if magic_le in (0xFEEDFACF, 0xFEEDFACE):
            cpu = struct.unpack_from("<I", header, 4)[0]
            if cpu == 0x01000007:
                return "mac-x64"
            if cpu == 0x0100000C:
                return "mac-arm64"
        magic_be = struct.unpack_from(">I", header, 0)[0]
        if magic_be in (0xFEEDFACF, 0xFEEDFACE):
            cpu = struct.unpack_from(">I", header, 4)[0]
            if cpu == 0x01000007:
                return "mac-x64"
            if cpu == 0x0100000C:
                return "mac-arm64"
    return None


def safe_extract_zip(zip_path: Path, destination: Path) -> list[Path]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise DriverDownloadError("压缩包条目过多，已拒绝解压")
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > MAX_EXTRACTED_BYTES:
            raise DriverDownloadError("压缩包解压后过大，已拒绝解压")
        destination_resolved = destination.resolve()
        for info in infos:
            name = info.filename
            if not name or name.startswith("/") or "\\" in name:
                raise DriverDownloadError("压缩包包含不安全的绝对路径或反斜杠路径")
            pure = Path(name)
            if ".." in pure.parts:
                raise DriverDownloadError("压缩包包含路径穿越条目")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise DriverDownloadError("压缩包包含符号链接，已拒绝解压")
            target = destination / pure
            if not target.resolve().is_relative_to(destination_resolved):
                raise DriverDownloadError("压缩包条目写出暂存目录，已拒绝解压")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            extracted.append(target)
    return extracted


def _check_operation(operation: DriverOperation) -> None:
    if operation.cancelled:
        raise DriverOperationCancelled("驱动操作已取消")


def _trusted_download_response(response) -> None:
    final_url = response.geturl()
    _check_https_source(final_url, allowed_host=DOWNLOAD_HOST, path_prefix=DOWNLOAD_PATH_PREFIX)


def download_driver_to_staging(
    download: DriverDownloadInfo,
    *,
    staging_root: Path,
    operation: DriverOperation,
    progress: Callable[[DownloadProgress], None] | None = None,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 30.0,
    chunk_size: int = 64 * 1024,
) -> StagedDriver:
    _check_operation(operation)
    _check_https_source(download.url, allowed_host=DOWNLOAD_HOST, path_prefix=DOWNLOAD_PATH_PREFIX)
    stage_dir = Path(staging_root) / operation.operation_id
    stage_dir.mkdir(parents=True, exist_ok=False)
    try:
        stage_dir.chmod(0o700)
    except OSError:
        pass
    zip_path = stage_dir / "chromedriver.zip"
    downloaded = 0
    try:
        try:
            response = opener(download.url, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DriverDownloadError(f"驱动下载失败：{exc}") from exc
        try:
            _trusted_download_response(response)
            length_header = response.headers.get("Content-Length")
            total = int(length_header) if length_header and length_header.isdigit() else None
            with open(zip_path, "wb") as handle:
                while True:
                    _check_operation(operation)
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(DownloadProgress(downloaded, total, "downloading"))
                handle.flush()
                os.fsync(handle.fileno())
            if total is not None and downloaded != total:
                raise DriverDownloadError("驱动下载不完整，总长度校验失败")
            if progress is not None:
                progress(DownloadProgress(downloaded, total, "extracting"))
            extracted = safe_extract_zip(zip_path, stage_dir / "extracted")
            binary_name = driver_binary_name(download.platform)
            candidates = [path for path in extracted if path.name == binary_name]
            if len(candidates) != 1:
                raise DriverDownloadError(f"压缩包中没有唯一的 {binary_name}")
            binary = candidates[0]
            try:
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            except OSError:
                pass
            return StagedDriver(
                operation_id=operation.operation_id,
                path=binary,
                version=download.version,
                platform=download.platform,
            )
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def stage_manual_driver(
    source: Path,
    *,
    staging_root: Path,
    operation: DriverOperation,
    platform_tag: str,
    runner: Callable[..., str] = _run_version,
) -> StagedDriver:
    _check_operation(operation)
    source = Path(source)
    binary_name = driver_binary_name(platform_tag)
    if source.name != binary_name:
        raise DriverValidationError(f"请选择解压后的 {binary_name}")
    if not source.is_file():
        raise DriverValidationError(f"驱动文件不存在：{source}")
    stage_dir = Path(staging_root) / operation.operation_id
    stage_dir.mkdir(parents=True, exist_ok=False)
    try:
        stage_dir.chmod(0o700)
    except OSError:
        pass
    destination = stage_dir / binary_name
    try:
        shutil.copy2(source, destination)
        try:
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        except OSError:
            pass
        version = read_driver_version(destination, runner=runner)
        return StagedDriver(
            operation_id=operation.operation_id,
            path=destination,
            version=version,
            platform=platform_tag,
        )
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def read_driver_version(path: Path, *, runner: Callable[..., str] = _run_version) -> str:
    return _extract_version(runner([str(path), "--version"]))


def patch_driver_for_uc(
    path: Path,
    *,
    chrome_major: int,
) -> None:
    try:
        from undetected_chromedriver.patcher import Patcher

        patcher = Patcher(executable_path=str(path), version_main=chrome_major)
        patcher.auto()
        if not patcher.is_binary_patched(str(path)):
            raise DriverValidationError("UC 未确认驱动已处理")
        if platform.system() == "Darwin":
            completed = subprocess.run(
                ["codesign", "--force", "--sign", "-", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise DriverValidationError("macOS 无法重新签署 UC 处理后的驱动")
            verified = subprocess.run(
                ["codesign", "--verify", "--verbose=2", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if verified.returncode != 0:
                raise DriverValidationError("macOS UC 驱动签名验证失败")
    except DriverValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DriverValidationError(f"UC 驱动处理失败：{exc}") from exc


def validate_staged_driver(
    staged: StagedDriver,
    *,
    chrome_major: int,
    platform_tag: str,
    runner: Callable[..., str] = _run_version,
    browser_verifier: Callable[..., object] | None = None,
    chrome_path: str | None = None,
    driver_preparer: Callable[..., object] | None = None,
) -> ValidationResult:
    if not staged.path.is_file():
        return ValidationResult(False, None, None, "暂存驱动文件不存在")
    if staged.path.name != driver_binary_name(platform_tag):
        return ValidationResult(False, None, None, f"暂存文件不是 {driver_binary_name(platform_tag)}")
    detected_platform = inspect_binary_platform(staged.path)
    if detected_platform is None:
        return ValidationResult(False, None, None, "无法确认驱动运行平台")
    if detected_platform != platform_tag:
        return ValidationResult(False, None, None, f"驱动平台 {detected_platform} 与当前平台 {platform_tag} 不匹配")
    try:
        version = read_driver_version(staged.path, runner=runner)
    except DriverValidationError as exc:
        return ValidationResult(False, None, None, str(exc))
    try:
        major = int(version.split(".", 1)[0])
    except ValueError:
        return ValidationResult(False, version, detected_platform, "驱动版本无法解析")
    if major != chrome_major:
        return ValidationResult(
            False,
            version,
            detected_platform,
            f"驱动主版本 {major} 与 Chrome 主版本 {chrome_major} 不匹配",
        )
    if driver_preparer is not None:
        try:
            driver_preparer(staged.path, chrome_major=chrome_major)
        except Exception as exc:  # noqa: BLE001
            return ValidationResult(False, version, detected_platform, f"驱动处理失败：{exc}")
    if browser_verifier is not None:
        if chrome_path is None:
            return ValidationResult(False, version, detected_platform, "缺少空白浏览器验证所需的 Chrome 路径")
        before = hashlib.sha256(staged.path.read_bytes()).hexdigest()
        try:
            browser_verifier(chrome_path=chrome_path, driver_path=str(staged.path))
        except Exception as exc:  # noqa: BLE001 - any launch failure means not verified
            return ValidationResult(False, version, detected_platform, f"空白浏览器验证失败：{exc}")
        after = hashlib.sha256(staged.path.read_bytes()).hexdigest()
        if before != after:
            return ValidationResult(
                False,
                version,
                detected_platform,
                "UC 启动时修改了已处理驱动，组合不能启用",
            )
    return ValidationResult(True, version, detected_platform)


def activate_driver(
    staged: StagedDriver,
    *,
    drivers_root: Path,
    operation: DriverOperation,
    store_selection: Callable[[object], None],
    mode: str,
    platform_tag: str,
) -> ActivatedDriver:
    _check_operation(operation)
    drivers_root = Path(drivers_root)
    destination = drivers_root / f"{staged.version}-{operation.operation_id}"
    try:
        drivers_root.mkdir(parents=True, exist_ok=True)
        staging = drivers_root / f".activating-{operation.operation_id}"
        shutil.copytree(staged.path.parent, staging)
        copied = staging / staged.path.name
        try:
            copied.chmod(copied.stat().st_mode | stat.S_IXUSR)
        except OSError:
            pass
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
    except OSError as exc:
        raise DriverActivationError(f"驱动晋升失败：{exc}") from exc
    try:
        _check_operation(operation)
    except DriverOperationCancelled:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    from .models import DriverSettings

    settings = DriverSettings(
        mode=mode,
        path=str(destination / staged.path.name),
        version=staged.version,
        platform=platform_tag,
    )
    try:
        store_selection(settings)
    except Exception as exc:  # noqa: BLE001 - keep old selection
        shutil.rmtree(destination, ignore_errors=True)
        raise DriverActivationError(f"保存有效驱动选择失败：{exc}") from exc
    return ActivatedDriver(settings=settings, path=destination / staged.path.name, version=staged.version)


def check_enabled_driver(
    driver_settings,
    *,
    chrome: ChromeInstallation,
    platform_tag: str | None = None,
    runner: Callable[..., str] = _run_version,
) -> ValidationResult:
    if driver_settings.mode == "manual" and not driver_settings.path:
        return ValidationResult(False, None, None, "手动模式尚未选择有效驱动")
    if not driver_settings.path:
        return ValidationResult(False, None, None, "尚未启用驱动")
    path = Path(driver_settings.path)
    if not path.is_file():
        return ValidationResult(False, None, None, "已启用驱动文件不存在")
    staged = StagedDriver(
        operation_id="installed",
        path=path,
        version=driver_settings.version or "",
        platform=platform_tag or current_platform_tag(),
    )
    result = validate_staged_driver(
        staged,
        chrome_major=chrome.major_version,
        platform_tag=platform_tag or current_platform_tag(),
        runner=runner,
    )
    return result


def prepare_automatic_driver(
    *,
    chrome: ChromeInstallation,
    paths: AppPaths,
    operation: DriverOperation,
    client: CfTClient | None = None,
    platform_tag: str | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
    browser_verifier: Callable[..., object] | None = None,
    driver_preparer: Callable[..., object] | None = None,
    store_selection: Callable[[object], None],
    opener: Callable[..., object] = urllib.request.urlopen,
    runner: Callable[..., str] = _run_version,
) -> ActivatedDriver:
    client = client or CfTClient()
    platform_tag = platform_tag or current_platform_tag()
    _check_operation(operation)
    metadata = client.fetch_metadata()
    download = client.find_compatible(metadata, chrome.version, platform_tag)
    staged = download_driver_to_staging(
        download,
        staging_root=paths.driver_staging,
        operation=operation,
        progress=progress,
        opener=opener,
    )
    try:
        _check_operation(operation)
        result = validate_staged_driver(
            staged,
            chrome_major=chrome.major_version,
            platform_tag=platform_tag,
            runner=runner,
            browser_verifier=browser_verifier,
            chrome_path=str(chrome.path),
            driver_preparer=driver_preparer,
        )
        if not result.ok:
            raise DriverValidationError(result.error or "驱动验证失败")
        activated = activate_driver(
            staged,
            drivers_root=paths.drivers,
            operation=operation,
            store_selection=store_selection,
            mode="automatic",
            platform_tag=platform_tag,
        )
        shutil.rmtree(Path(paths.driver_staging) / operation.operation_id, ignore_errors=True)
        return activated
    except Exception:
        shutil.rmtree(Path(paths.driver_staging) / operation.operation_id, ignore_errors=True)
        raise


def prepare_manual_driver(
    *,
    source: Path,
    chrome: ChromeInstallation,
    paths: AppPaths,
    operation: DriverOperation,
    platform_tag: str | None = None,
    browser_verifier: Callable[..., object] | None = None,
    driver_preparer: Callable[..., object] | None = None,
    store_selection: Callable[[object], None],
    runner: Callable[..., str] = _run_version,
) -> ActivatedDriver:
    platform_tag = platform_tag or current_platform_tag()
    staged = stage_manual_driver(
        source,
        staging_root=paths.driver_staging,
        operation=operation,
        platform_tag=platform_tag,
        runner=runner,
    )
    try:
        _check_operation(operation)
        result = validate_staged_driver(
            staged,
            chrome_major=chrome.major_version,
            platform_tag=platform_tag,
            runner=runner,
            browser_verifier=browser_verifier,
            chrome_path=str(chrome.path),
            driver_preparer=driver_preparer,
        )
        if not result.ok:
            raise DriverValidationError(result.error or "驱动验证失败")
        activated = activate_driver(
            staged,
            drivers_root=paths.drivers,
            operation=operation,
            store_selection=store_selection,
            mode="manual",
            platform_tag=platform_tag,
        )
        shutil.rmtree(Path(paths.driver_staging) / operation.operation_id, ignore_errors=True)
        return activated
    except Exception:
        shutil.rmtree(Path(paths.driver_staging) / operation.operation_id, ignore_errors=True)
        raise
