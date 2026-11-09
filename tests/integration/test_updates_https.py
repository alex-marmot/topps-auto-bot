from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from topps_auto_bot.updates import UpdateError, check_for_update


def test_fixed_publisher_over_verified_https(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("受控 HTTPS 证书生成需要 openssl")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    config = tmp_path / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
        "[dn]\nCN=127.0.0.1\n[ext]\nsubjectAltName=IP:127.0.0.1\n",
        encoding="ascii",
    )
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-config", str(config), "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True, timeout=15,
    )

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Cookie"), self.headers.get("Authorization")))
            if self.path == "/unsafe":
                self.send_response(302)
                self.send_header("Location", "https://127.0.0.1:1/other")
                self.end_headers()
                return
            payload = json.dumps({
                "version": "1.0.0", "notes": "Controlled HTTPS release", "download_url": origin + "/download",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        origin = f"https://127.0.0.1:{server.server_port}"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        trusted = ssl.create_default_context(cafile=str(cert))
        monkeypatch.setattr(ssl, "_create_default_https_context", lambda: trusted)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            release = check_for_update(origin + "/latest")
            assert release.comparison == 1
            assert release.download_url == origin + "/download"
            with pytest.raises(UpdateError, match="重定向"):
                check_for_update(origin + "/unsafe")
            assert requests == [("/latest", None, None), ("/unsafe", None, None)]
        finally:
            server.shutdown()
            worker.join()
