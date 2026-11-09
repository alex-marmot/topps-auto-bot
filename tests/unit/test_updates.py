from __future__ import annotations

import io
import json
from urllib.error import URLError
from urllib.request import Request

import pytest

from topps_auto_bot.updates import UpdateError, _PublisherRedirects, check_for_update

SOURCE = "https://publisher.example.test/latest.json"
DOWNLOAD = "https://publisher.example.test/download"


class Response(io.BytesIO):
    def geturl(self):
        return SOURCE


@pytest.mark.parametrize(("version", "comparison"), [("0.10.0", 1), ("0.2.0", 0), ("0.1.0", -1)])
def test_publisher_versions_and_request_contains_no_local_data(version, comparison):
    def open_manifest(request, *, timeout):
        assert request.full_url == SOURCE
        assert request.get_method() == "GET"
        assert request.data is None
        assert request.header_items() == [("Accept", "application/json")]
        assert timeout == 8
        return Response(json.dumps({"version": version, "notes": "Controlled release", "download_url": DOWNLOAD}).encode())

    release = check_for_update(SOURCE, opener=open_manifest)
    assert (release.version, release.notes, release.download_url, release.comparison) == (
        version, "Controlled release", DOWNLOAD, comparison,
    )


@pytest.mark.parametrize("payload", [
    b"invalid json", b"null", b"{}",
    pytest.param(b" " * (64 * 1024 + 1), id="oversized-response"),
    b'{"version":"next","notes":"Release","download_url":"https://publisher.example.test/download"}',
    b'{"version":"1.0.0","notes":false,"download_url":"https://publisher.example.test/download"}',
    b'{"version":"1.0.0","notes":"Release","download_url":"https://other.example.test/download"}',
    b'{"version":"1.0.0","notes":"Release","download_url":"https://publisher.example.test@other.example.test/download"}',
])
def test_illegal_response_has_no_release(payload):
    with pytest.raises(UpdateError):
        check_for_update(SOURCE, opener=lambda *_args, **_kwargs: Response(payload))


def test_offline_and_missing_source_are_distinct():
    def offline(*_args, **_kwargs):
        raise URLError("offline")

    with pytest.raises(UpdateError, match="无法连接"):
        check_for_update(SOURCE, opener=offline)

    def unexpected(*_args, **_kwargs):
        pytest.fail("unconfigured source must not send a request")

    with pytest.raises(UpdateError, match="未配置发布源"):
        check_for_update(None, opener=unexpected)


@pytest.mark.parametrize("url", ["https://another.example.test/latest", "http://publisher.example.test/latest"])
def test_redirect_handler_rejects_redirect_before_following(url):
    handler = _PublisherRedirects("publisher.example.test")
    with pytest.raises(UpdateError):
        handler.redirect_request(Request(SOURCE), None, 302, "", {}, url)
