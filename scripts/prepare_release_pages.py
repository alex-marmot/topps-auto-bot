"""Prepare a Pages deployment from an anonymously downloadable GitHub Release."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from topps_auto_bot.updates import _version, publisher_origin

_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if publisher_origin(newurl) not in _DOWNLOAD_HOSTS:
            raise ValueError("GitHub 资产重定向至未许可来源")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def release_version(tag: str) -> str:
    if not tag.startswith("v"):
        raise ValueError("发布 tag 必须为 vX.Y.Z")
    version = tag[1:]
    if _version(version) == (0, 0, 0):
        raise ValueError("0.0.0 保留用于首次发布前的 Pages 清单")
    return version


def public_release(repository: str, tag: str, *, opener=None) -> tuple[str, str]:
    version = release_version(tag)
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub 仓库名无效")
    if opener is None:
        opener = build_opener(ReleaseRedirects()).open

    request = Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ToppsAutoBot-release-workflow"},
    )
    with opener(request, timeout=20) as response:
        release = json.load(response)
    url = f"https://github.com/{repository}/releases/tag/{tag}"
    if release["tag_name"] != tag or release["draft"] or release["prerelease"] or release["html_url"] != url:
        raise ValueError("找不到与 tag 匹配的公开正式 Release")
    notes = release["body"]
    if not isinstance(notes, str) or not notes.strip() or len(notes) > 12000:
        raise ValueError("发行说明必须包含 1–12000 字符")
    assets = {asset["name"]: asset for asset in release["assets"] if asset["state"] == "uploaded"}
    name = f"ToppsAutoBot-Setup-{version}.exe"
    prefix = f"https://github.com/{repository}/releases/download/{tag}/"
    for filename in (name, name + ".sha256"):
        asset = assets.get(filename)
        if asset is None or asset["size"] <= 0 or asset["browser_download_url"] != prefix + filename:
            raise ValueError("Release 缺少可公开下载的安装包或校验文件")
    with opener(Request(prefix + name + ".sha256"), timeout=20) as response:
        checksum = response.read(512).decode("ascii").strip().split()
    if len(checksum) != 2 or not re.fullmatch(r"[a-f0-9]{64}", checksum[0]) or checksum[1] != name:
        raise ValueError("安装包校验文件格式无效")
    with opener(Request(prefix + name), timeout=30) as response:
        digest = hashlib.file_digest(response, "sha256").hexdigest()
    if digest != checksum[0]:
        raise ValueError("公开下载的安装包与 SHA-256 不一致")
    return notes, url


def prepare_pages(
    output: Path, source: str, base_url: str, repository: str, mode: str, tag: str,
    *, opener=None, probe=None,
) -> None:
    base_url = base_url.rstrip("/")
    if publisher_origin(source) != publisher_origin(base_url) or source != base_url + "/latest.json":
        raise ValueError("PUBLISHER_SOURCE 必须是 GitHub Pages 实际站点根目录的 latest.json")
    if urlsplit(base_url).query:
        raise ValueError("Pages 站点地址不能包含查询参数")
    download = None
    if mode == "bootstrap":
        if tag:
            raise ValueError("初始化 Pages 时请留空 tag")
        probe = probe or build_opener(NoRedirects()).open
        try:
            with probe(Request(source), timeout=15):
                raise ValueError("已存在更新清单，拒绝用首次发布占位清单覆盖")
        except HTTPError as exc:
            if exc.code != 404:
                raise
        version = "0.0.0"
        notes = "尚未发布 Windows 安装包。"
    else:
        version = release_version(tag)
        notes, download = public_release(repository, tag, opener=opener)

    manifest = json.dumps({
        "version": version, "notes": notes, "download_url": base_url + "/download.html",
    }, ensure_ascii=False, indent=2) + "\n"
    if len(manifest.encode("utf-8")) > 64 * 1024:
        raise ValueError("更新清单超出客户端允许大小")
    link = f'<p><a href="{html.escape(download, quote=True)}">前往 GitHub Release 下载安装包</a></p>' if download else ""
    heading = f"Topps Auto Bot {version}" if download else "Topps Auto Bot"
    output.mkdir(parents=True, exist_ok=True)
    (output / "latest.json").write_text(manifest, encoding="utf-8")
    (output / "download.html").write_text(
        f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f'<title>{heading}</title><main><h1>{heading}</h1><pre>{html.escape(notes)}</pre>{link}</main></html>\n',
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("bootstrap", "pages"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare_pages(args.output, args.source, args.base_url, args.repo, args.mode, args.tag)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Pages 准备失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
