from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from tests.support import SYNTHETIC_CARD  # noqa: F401 - keeps synthetic data in one place
from topps_auto_bot.driver_setup import (
    CfTClient,
    ChromeInstallation,
    DriverDownloadError,
    DriverDownloadInfo,
    DriverMetadataError,
    DriverOperation,
    DriverOperationCancelled,
    DriverValidationError,
    DriverDownloadError as DownloadError,
    activate_driver,
    check_enabled_driver,
    current_platform_tag,
    detect_chrome,
    download_driver_to_staging,
    inspect_binary_platform,
    prepare_automatic_driver,
    prepare_manual_driver,
    read_driver_version,
    safe_extract_zip,
    stage_manual_driver,
    validate_staged_driver,
)
from topps_auto_bot.models import DriverSettings
from topps_auto_bot.settings import AppPaths

DRIVER_URL = "https://storage.googleapis.com/chrome-for-testing-public/156.0.8078.80/mac-arm64/chromedriver-mac-arm64.zip"


def fake_runner(command) -> str:
    return "ChromeDriver 156.0.8078.80"
METADATA_URL = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"


def macho_binary() -> bytes:
    header = bytearray(64)
    header[0:4] = (0xFEEDFACF).to_bytes(4, "little")
    header[4:8] = (0x0100000C).to_bytes(4, "little")
    return bytes(header)


def driver_zip(binary_name: str = "chromedriver", member: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member or f"chromedriver-mac-arm64/{binary_name}", macho_binary())
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, payload: bytes, url: str, *, content_length: bool = True, chunk_size: int = 7) -> None:
        self._stream = io.BytesIO(payload)
        self._url = url
        self.headers = {"Content-Length": str(len(payload))} if content_length else {}
        self._chunk_size = chunk_size

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._stream.read()
        return self._stream.read(min(size, self._chunk_size))

    def close(self) -> None:
        self._stream.close()


def test_platform_tags() -> None:
    assert current_platform_tag(system="Windows", machine="AMD64") == "win64"
    assert current_platform_tag(system="Darwin", machine="arm64") == "mac-arm64"
    assert current_platform_tag(system="Darwin", machine="x86_64") == "mac-x64"
    assert current_platform_tag(system="Linux", machine="aarch64") == "linux-arm64"


def test_detect_chrome_uses_runner_and_candidates(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"fake")
    installation = detect_chrome(
        candidates=[chrome],
        runner=lambda command: "Google Chrome 156.0.8078.80",
    )
    assert installation is not None
    assert installation.version == "156.0.8078.80"
    assert installation.major_version == 156
    assert detect_chrome(candidates=[tmp_path / "missing"], runner=lambda command: "x") is None


def test_windows_chrome_detection_reads_the_selected_executable_version(tmp_path, monkeypatch):
    from topps_auto_bot import driver_setup

    chrome = tmp_path / "Browser [custom]" / "chrome.exe"
    chrome.parent.mkdir()
    chrome.write_bytes(b"synthetic exe")
    monkeypatch.setattr(driver_setup.platform, "system", lambda: "Windows")

    def version_reader(command, *, env):
        assert command[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        assert "-LiteralPath $env:TOPPS_CHROME_VERSION_FILE" in command[4]
        assert Path(env["TOPPS_CHROME_VERSION_FILE"]) == chrome.resolve()
        return "167.0.5000.12"

    monkeypatch.setattr(driver_setup, "_run_version", version_reader)
    assert driver_setup.detect_chrome(candidates=[chrome]).major_version == 167


def test_inspect_binary_platform(tmp_path: Path) -> None:
    path = tmp_path / "chromedriver"
    path.write_bytes(macho_binary())
    assert inspect_binary_platform(path) == "mac-arm64"
    win = bytearray(128)
    win[0:2] = b"MZ"
    win[0x3C:0x40] = (64).to_bytes(4, "little")
    win[64:68] = b"PE\0\0"
    win[68:70] = (0x8664).to_bytes(2, "little")
    path.write_bytes(bytes(win))
    assert inspect_binary_platform(path) == "win64"


def test_metadata_matching_and_no_fallback() -> None:
    metadata = {
        "versions": [
            {
                "version": "155.0.1.0",
                "downloads": {"chromedriver": [{"platform": "mac-arm64", "url": "https://storage.googleapis.com/chrome-for-testing-public/155.0.1.0/mac-arm64/chromedriver-mac-arm64.zip"}]},
            },
            {
                "version": "156.0.8078.80",
                "downloads": {"chromedriver": [{"platform": "mac-arm64", "url": DRIVER_URL}]},
            },
        ]
    }
    client = CfTClient()
    match = client.find_compatible(metadata, "156.0.8078.80", "mac-arm64")
    assert match.version == "156.0.8078.80"
    with pytest.raises(DriverMetadataError):
        client.find_compatible(metadata, "157.0.0.0", "mac-arm64")
    with pytest.raises(DriverMetadataError):
        client.find_compatible(metadata, "156.0.8078.80", "win64")


def test_matches_exact_patch_first_and_never_other_build():
    versions = ["167.0.5000.1", "167.0.5000.2", "167.0.5001.1"]
    metadata = {"versions": [
        {"version": version, "downloads": {"chromedriver": [{
            "platform": "win64",
            "url": f"https://storage.googleapis.com/chrome-for-testing-public/{version}/win64/chromedriver-win64.zip",
        }]}}
        for version in versions
    ]}
    client = CfTClient()
    assert client.find_compatible(metadata, "167.0.5000.1", "win64").version == versions[0]
    assert client.find_compatible(metadata, "167.0.5000.9", "win64").version == versions[1]
    with pytest.raises(DriverMetadataError):
        client.find_compatible(metadata, "167.0.5002.1", "win64")


def test_metadata_rejects_non_official_url() -> None:
    metadata = {
        "versions": [
            {
                "version": "156.0.1.0",
                "downloads": {"chromedriver": [{"platform": "mac-arm64", "url": "https://mirror.invalid/chromedriver.zip"}]},
            }
        ]
    }
    with pytest.raises(DriverMetadataError):
        CfTClient().find_compatible(metadata, "156.0.1.0", "mac-arm64")


def test_metadata_fetch_tls_failure() -> None:
    def opener(url, timeout=30):
        raise OSError("TLS handshake failed")

    with pytest.raises(DriverMetadataError):
        CfTClient(opener=opener).fetch_metadata()


def test_metadata_fetch_rejects_redirect_to_other_host() -> None:
    metadata = json.dumps({"versions": []}).encode()

    def opener(url, timeout=30):
        return FakeResponse(metadata, "https://evil.invalid/metadata.json")

    with pytest.raises(DriverMetadataError):
        CfTClient(opener=opener).fetch_metadata()


def test_download_extracts_only_staged_copy(tmp_path: Path) -> None:
    operation = DriverOperation("op-1")
    progress = []
    download = DriverDownloadInfo("156.0.8078.80", "mac-arm64", DRIVER_URL)
    staged = download_driver_to_staging(
        download,
        staging_root=tmp_path,
        operation=operation,
        progress=lambda update: progress.append(update),
        opener=lambda url, timeout=30: FakeResponse(driver_zip(), DRIVER_URL),
    )
    assert staged.path.is_file()
    assert tmp_path / "op-1" in staged.path.parents
    assert staged.path.read_bytes() == macho_binary()
    assert progress
    assert progress[-1].stage == "extracting"


def test_download_without_content_length_reports_bytes(tmp_path: Path) -> None:
    progress = []
    staged = download_driver_to_staging(
        DriverDownloadInfo("156.0.8078.80", "mac-arm64", DRIVER_URL),
        staging_root=tmp_path,
        operation=DriverOperation("op-2"),
        progress=lambda update: progress.append(update),
        opener=lambda url, timeout=30: FakeResponse(driver_zip(), DRIVER_URL, content_length=False),
    )
    assert staged.path.exists()
    assert progress[-1].total_bytes is None
    assert progress[-1].downloaded_bytes > 0


def test_download_cancel_cleans_staging(tmp_path: Path) -> None:
    operation = DriverOperation("op-3")

    def progress(update) -> None:
        operation.cancel()

    with pytest.raises(DriverOperationCancelled):
        download_driver_to_staging(
            DriverDownloadInfo("156.0.8078.80", "mac-arm64", DRIVER_URL),
            staging_root=tmp_path,
            operation=operation,
            progress=progress,
            opener=lambda url, timeout=30: FakeResponse(driver_zip(), DRIVER_URL),
        )
    assert not (tmp_path / "op-3").exists()


def test_download_rejects_path_traversal_and_symlink(tmp_path: Path) -> None:
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape", "x")
    with pytest.raises(DriverDownloadError):
        safe_extract_zip_bytes(traversal.getvalue(), tmp_path / "out")

    symlink = io.BytesIO()
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(DriverDownloadError):
        safe_extract_zip_bytes(symlink.getvalue(), tmp_path / "out2")


def safe_extract_zip_bytes(payload: bytes, destination: Path) -> None:
    zip_path = destination.parent / "payload.zip"
    zip_path.write_bytes(payload)
    safe_extract_zip(zip_path, destination)


def test_manual_staging_copies_without_modifying_source(tmp_path: Path) -> None:
    source = tmp_path / "chromedriver"
    source.write_bytes(macho_binary())
    original = source.read_bytes()
    staged = stage_manual_driver(
        source,
        staging_root=tmp_path / "staging",
        operation=DriverOperation("manual-1"),
        platform_tag="mac-arm64",
        runner=fake_runner,
    )
    assert tmp_path / "staging" / "manual-1" in staged.path.parents
    assert source.read_bytes() == original
    assert staged.path.stat().st_mode & stat.S_IXUSR
    assert staged.path != source


def test_manual_staging_rejects_wrong_name(tmp_path: Path) -> None:
    source = tmp_path / "other.exe"
    source.write_bytes(macho_binary())
    with pytest.raises(DriverValidationError):
        stage_manual_driver(
            source,
            staging_root=tmp_path / "staging",
            operation=DriverOperation("manual-2"),
            platform_tag="mac-arm64",
            runner=fake_runner,
        )


def test_validation_checks_platform_version_and_browser(tmp_path: Path) -> None:
    source = tmp_path / "chromedriver"
    source.write_bytes(macho_binary())
    staged = stage_manual_driver(
        source,
        staging_root=tmp_path / "staging",
        operation=DriverOperation("manual-3"),
        platform_tag="mac-arm64",
        runner=fake_runner,
    )
    ok = validate_staged_driver(
        staged,
        chrome_major=156,
        platform_tag="mac-arm64",
        runner=lambda command: "ChromeDriver 156.0.8078.80",
        browser_verifier=lambda chrome_path, driver_path: None,
        chrome_path="/fake/chrome",
    )
    assert ok.ok
    wrong_major = validate_staged_driver(
        staged,
        chrome_major=157,
        platform_tag="mac-arm64",
        runner=lambda command: "ChromeDriver 156.0.8078.80",
    )
    assert not wrong_major.ok
    wrong_platform = validate_staged_driver(
        staged,
        chrome_major=156,
        platform_tag="win64",
        runner=lambda command: "ChromeDriver 156.0.8078.80",
    )
    assert not wrong_platform.ok
    browser_failed = validate_staged_driver(
        staged,
        chrome_major=156,
        platform_tag="mac-arm64",
        runner=lambda command: "ChromeDriver 156.0.8078.80",
        browser_verifier=lambda chrome_path, driver_path: (_ for _ in ()).throw(RuntimeError("no launch")),
        chrome_path="/fake/chrome",
    )
    assert not browser_failed.ok


def test_activation_requires_operation_and_rolls_back_store_failure(tmp_path: Path) -> None:
    source = tmp_path / "chromedriver"
    source.write_bytes(macho_binary())
    staged = stage_manual_driver(
        source,
        staging_root=tmp_path / "staging",
        operation=DriverOperation("manual-4"),
        platform_tag="mac-arm64",
        runner=fake_runner,
    )
    operation = DriverOperation("act-1")
    with pytest.raises(Exception):
        activate_driver(
            staged,
            drivers_root=tmp_path / "drivers",
            operation=operation,
            store_selection=lambda settings: (_ for _ in ()).throw(RuntimeError("save failed")),
            mode="manual",
            platform_tag="mac-arm64",
        )
    assert not list((tmp_path / "drivers").glob("156*"))

    good_operation = DriverOperation("act-2")
    with pytest.raises(DriverOperationCancelled):
        bad = DriverOperation("act-3")
        bad.cancel()
        activate_driver(
            staged,
            drivers_root=tmp_path / "drivers",
            operation=bad,
            store_selection=lambda settings: None,
            mode="manual",
            platform_tag="mac-arm64",
        )

    committed = DriverOperation("act-4")

    def store_and_cancel(settings) -> None:
        committed.cancel()

    activated = activate_driver(
        staged,
        drivers_root=tmp_path / "drivers",
        operation=committed,
        store_selection=store_and_cancel,
        mode="manual",
        platform_tag="mac-arm64",
    )
    assert activated.path.exists()
    assert activated.settings.mode == "manual"


def test_check_enabled_driver(tmp_path: Path) -> None:
    chrome = ChromeInstallation(path=tmp_path / "chrome", version="156.0.8078.80", architecture="mac-arm64")
    missing = check_enabled_driver(DriverSettings(mode="manual", path=None), chrome=chrome)
    assert not missing.ok
    driver = tmp_path / "chromedriver"
    driver.write_bytes(macho_binary())
    valid = check_enabled_driver(
        DriverSettings(mode="manual", path=str(driver), version="156.0.8078.80", platform="mac-arm64"),
        chrome=chrome,
        platform_tag="mac-arm64",
        runner=lambda command: "ChromeDriver 156.0.8078.80",
    )
    assert valid.ok


def test_prepare_automatic_full_flow(tmp_path: Path) -> None:
    paths = AppPaths.under(tmp_path / "data")
    paths.ensure_directories()
    chrome = ChromeInstallation(path=tmp_path / "chrome", version="156.0.8078.80", architecture="mac-arm64")
    metadata = {
        "versions": [
            {
                "version": "156.0.8078.80",
                "downloads": {"chromedriver": [{"platform": "mac-arm64", "url": DRIVER_URL}]},
            }
        ]
    }

    def opener(url, timeout=30):
        if url == METADATA_URL:
            return FakeResponse(json.dumps(metadata).encode(), METADATA_URL)
        return FakeResponse(driver_zip(), DRIVER_URL)

    stored = []
    activated = prepare_automatic_driver(
        chrome=chrome,
        paths=paths,
        operation=DriverOperation("auto-1"),
        client=CfTClient(opener=opener),
        platform_tag="mac-arm64",
        browser_verifier=lambda chrome_path, driver_path: None,
        store_selection=stored.append,
        opener=opener,
        runner=lambda command: "ChromeDriver 156.0.8078.80",
    )
    assert activated.path.exists()
    assert stored
    assert stored[0].mode == "automatic"
    assert not list(paths.driver_staging.iterdir())
