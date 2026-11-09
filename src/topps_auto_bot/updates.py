"""User-initiated version checks against a publisher URL shipped with the build."""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__

_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_MAX_RESPONSE = 64 * 1024


class UpdateError(ValueError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    download_url: str
    comparison: int


def publisher_origin(url: str) -> str:
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url) or "\\" in url:
        raise UpdateError("发布链接格式无效")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.port == 0
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or "." not in parsed.hostname or parsed.hostname.endswith(".")
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)
        ):
            raise UpdateError("发布链接必须使用可信的 HTTPS 来源")
        return parsed.hostname if parsed.port in (None, 443) else f"{parsed.hostname}:{parsed.port}"
    except ValueError as exc:
        raise UpdateError("发布链接必须使用可信的 HTTPS 来源") from exc


class _PublisherRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if publisher_origin(newurl) != self.origin:
            raise UpdateError("发布源重定向至未许可来源")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def packaged_source() -> str | None:
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        return None
    try:
        return (Path(base) / "publisher-update-source.txt").read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise UpdateError("无法读取随包发布源") from exc


def _version(text: str) -> tuple[int, ...]:
    if not isinstance(text, str) or not _VERSION.fullmatch(text):
        raise UpdateError("发布版本格式无效")
    return tuple(map(int, text.split(".")))


def check_for_update(source: str | None, *, opener=None) -> Release:
    if not source:
        raise UpdateError("未配置发布源")
    origin = publisher_origin(source)
    if opener is None:
        opener = urllib.request.build_opener(_PublisherRedirects(origin)).open
    request = urllib.request.Request(source, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=8) as response:
            if publisher_origin(response.geturl()) != origin:
                raise UpdateError("发布响应来源不可信")
            raw = response.read(_MAX_RESPONSE + 1)
    except OSError as exc:
        raise UpdateError("无法连接发布源或读取响应") from exc
    if len(raw) > _MAX_RESPONSE:
        raise UpdateError("发布响应超出允许大小")
    try:
        payload = json.loads(raw.decode("utf-8"))
        version, notes, download = (payload[key] for key in ("version", "notes", "download_url"))
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise UpdateError("发布响应格式无效") from exc
    if not isinstance(notes, str) or not notes.strip() or len(notes) > 12000:
        raise UpdateError("发行说明格式无效")
    if publisher_origin(download) != origin:
        raise UpdateError("下载链接来源不可信")
    latest, current = _version(version), _version(__version__)
    comparison = (latest > current) - (latest < current)
    return Release(version, notes, download, comparison)
