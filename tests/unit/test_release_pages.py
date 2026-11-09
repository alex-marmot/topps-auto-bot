from __future__ import annotations

import hashlib
import io
import json
from urllib.error import HTTPError

import pytest

from scripts.prepare_release_pages import prepare_pages
from topps_auto_bot.updates import check_for_update


SITE = "https://example.github.io/test-project"
SOURCE = SITE + "/latest.json"
REPO = "example/test-project"
TAG = "v0.2.0"
INSTALLER = "ToppsAutoBot-Setup-0.2.0.exe"


class FeedResponse(io.BytesIO):
    def geturl(self):
        return SOURCE


def missing(request, *, timeout):
    raise HTTPError(request.full_url, 404, "Not found", {}, None)


def test_bootstrap_is_a_valid_feed_without_an_installer_link(tmp_path):
    prepare_pages(tmp_path, SOURCE, SITE, REPO, "bootstrap", "", probe=missing)
    feed = check_for_update(
        SOURCE, opener=lambda *_args, **_kwargs: FeedResponse((tmp_path / "latest.json").read_bytes()),
    )
    assert feed.version == "0.0.0" and feed.comparison == -1
    assert feed.download_url == SITE + "/download.html"
    assert "<a " not in (tmp_path / "download.html").read_text(encoding="utf-8")


def test_bootstrap_cannot_replace_any_existing_feed(tmp_path):
    with pytest.raises(ValueError, match="已存在"):
        prepare_pages(tmp_path, SOURCE, SITE, REPO, "bootstrap", "", probe=lambda *_args, **_kwargs: io.BytesIO())
    assert not (tmp_path / "latest.json").exists()


def release_fixture(*, draft=False, missing_checksum=False, data=b"fixture installer"):
    name = INSTALLER
    prefix = f"https://github.com/{REPO}/releases/download/{TAG}/"
    metadata = {
        "tag_name": TAG, "draft": draft, "prerelease": False, "body": "Update <test>",
        "html_url": f"https://github.com/{REPO}/releases/tag/{TAG}",
        "assets": [
            {"name": filename, "size": 40, "state": "uploaded", "browser_download_url": prefix + filename}
            for filename in ([name] if missing_checksum else [name, name + ".sha256"])
        ],
    }
    checksum = hashlib.sha256(b"fixture installer").hexdigest()
    responses = {
        f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}": json.dumps(metadata).encode(),
        prefix + name + ".sha256": f"{checksum}  {name}\n".encode(),
        prefix + name: data,
    }

    def open_public(request, *, timeout):
        assert not request.has_header("Authorization") and not request.has_header("Cookie")
        return io.BytesIO(responses[request.full_url])

    return open_public


def test_pages_switch_only_after_anonymous_release_asset_verification(tmp_path):
    prepare_pages(tmp_path, SOURCE, SITE, REPO, "pages", TAG, opener=release_fixture())
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8")) == {
        "version": "0.2.0", "notes": "Update <test>", "download_url": SITE + "/download.html",
    }
    page = (tmp_path / "download.html").read_text(encoding="utf-8")
    assert "Update &lt;test&gt;" in page
    assert f'href="https://github.com/{REPO}/releases/tag/{TAG}"' in page


@pytest.mark.parametrize("opener, error", [
    (release_fixture(draft=True), ValueError),
    (release_fixture(missing_checksum=True), ValueError),
    (release_fixture(data=b"mismatching installer"), ValueError),
    (missing, HTTPError),
])
def test_failed_release_verification_preserves_existing_manifest(tmp_path, opener, error):
    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b"previous manifest")
    with pytest.raises(error):
        prepare_pages(tmp_path, SOURCE, SITE, REPO, "pages", TAG, opener=opener)
    assert manifest.read_bytes() == b"previous manifest"


@pytest.mark.parametrize("tag", ["0.2.0", "v0.0.0", "v00.2.0", "v0.2.0-beta"])
def test_release_pages_requires_canonical_version_tag(tmp_path, tag):
    with pytest.raises(ValueError):
        prepare_pages(tmp_path, SOURCE, SITE, REPO, "pages", tag, opener=release_fixture())


def test_publisher_must_match_configured_pages_path(tmp_path):
    with pytest.raises(ValueError):
        prepare_pages(tmp_path, "https://elsewhere.test/latest.json", SITE, REPO, "bootstrap", "", probe=missing)
