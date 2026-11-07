"""Run with:  PYTHONPATH=. pytest tests/unit"""

import asyncio
import errno
import json
import os
import socket
import stat
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from gateway import link as gateway_link
from gateway.access import AllowList, RateLimiter, real_client
from gateway.app import create_app
from gateway.config import ConfigError, Settings, memory_problem
from gateway.jobs import new_job_id
from gateway.responses import content_disposition, output_name, sanitize_filename
from shared import link as wire

GATEWAY_IP = "172.31.90.1"
TRUSTED = frozenset({GATEWAY_IP})
PAGE = Path(__file__).resolve().parents[2] / "ui" / "page.html"


# ---- the allow-list ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["", "   ", ",", None])
def test_empty_allow_list_is_refused(raw):
    with pytest.raises(ValueError):
        AllowList.parse(raw)


@pytest.mark.parametrize("raw", ["999.1.1.1", "not-an-ip", "10.0.0.0/99", "10.0.0.1,garbage", "all,10.0.0.1"])
def test_bad_entries_stop_startup(raw):
    with pytest.raises(ValueError):
        AllowList.parse(raw)


def test_allow_list_matching():
    allow = AllowList.parse("192.168.1.0/24, 203.0.113.7,2001:db8::/32")
    assert allow.allows("192.168.1.55")
    assert allow.allows("203.0.113.7")
    assert allow.allows("2001:db8::1")
    assert not allow.allows("192.168.2.1")
    assert not allow.allows("203.0.113.8")
    assert not allow.allows("2001:db9::1")
    assert not allow.allows(None)
    assert not allow.allows("")
    assert not allow.allows("garbage")


def test_ipv4_mapped_ipv6_is_compared_as_ipv4():
    allow = AllowList.parse("192.168.1.0/24")
    assert allow.allows("::ffff:192.168.1.5")
    assert not allow.allows("::ffff:10.0.0.1")


def test_all_must_be_explicit():
    assert AllowList.parse("all").allows("8.8.8.8")
    assert AllowList.parse("ALL").allows(None)


# ---- who the client really is ------------------------------------------------------------------
def test_header_is_only_believed_from_the_proxy():
    assert real_client(GATEWAY_IP, "203.0.113.7", TRUSTED) == "203.0.113.7"
    assert real_client("198.51.100.9", "203.0.113.7", TRUSTED) == "198.51.100.9"


def test_missing_or_malformed_header_never_becomes_a_trusted_address():
    assert real_client(GATEWAY_IP, None, TRUSTED) == GATEWAY_IP
    assert real_client(GATEWAY_IP, "1.2.3.4, 5.6.7.8", TRUSTED) is None
    assert real_client(GATEWAY_IP, "garbage", TRUSTED) is None


def test_rate_limiter_refills():
    now = [0.0]
    limiter = RateLimiter(60, clock=lambda: now[0])
    assert all(limiter.allow("a") for _ in range(60))
    assert not limiter.allow("a")
    assert limiter.allow("b")
    now[0] = 1.1
    assert limiter.allow("a")


# ---- file names ----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("report.pdf", "report.pdf"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
    ("/etc/shadow", "shadow"),
    ("a\x00b\nc\r.txt", "abc.txt"),
    ("...", "file"),
    ("", "file"),
    (None, "file"),
    (".bashrc", "bashrc"),
    ("Τιμολόγιο.pdf", "Τιμολόγιο.pdf"),
    ("evil‮gnp.exe", "evilgnp.exe"),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_long_names_are_cut_and_keep_their_extension():
    name = sanitize_filename("α" * 500 + ".pdf")
    assert len(name.encode()) <= 200
    assert name.endswith(".pdf")


def test_output_name():
    assert output_name("report.docx", "pdf", False) == "report.pdf"
    assert output_name("backup.tar.gz", "zip", False) == "backup.zip"
    assert output_name("slides.pdf", "png", True) == "slides.png.zip"
    assert output_name("mail.pst", "zip", True) == "mail.zip"
    assert output_name("noext", "pdf", False) == "noext.pdf"


@pytest.mark.parametrize("name", ['a"; filename="evil.html', "x\r\nSet-Cookie: a=b", "Τιμολόγιο.pdf", "résumé final.pdf"])
def test_content_disposition_cannot_be_broken_out_of(name):
    header = content_disposition(sanitize_filename(name))
    assert "\r" not in header and "\n" not in header
    assert header.startswith('attachment; filename="')
    plain = header.split('filename="')[1].split('"')[0]
    assert all(c.isalnum() or c in "._-" for c in plain)


# ---- settings -----------------------------------------------------------------------------------
def base_env(tmp_path, **extra):
    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    return {
        "ALLOWED_IPS": "203.0.113.0/24",
        "TRUSTED_PROXIES": GATEWAY_IP,
        "JOB_DIR": str(jobs),
        "LINK_SOCKET": str(tmp_path / "link.sock"),
        "WORKER_UID": str(os.getuid()),
        "PAGE_PATH": str(PAGE),
        "MAX_UPLOAD_BYTES": "1000",
        "MAX_OUTPUT_BYTES": "1000000",
        **extra,
    }


@pytest.mark.parametrize("extra", [
    {"ALLOWED_IPS": ""}, {"ALLOWED_IPS": "nonsense"}, {"TRUSTED_PROXIES": "nonsense"},
    {"BASE_PATH": "convert"}, {"BASE_PATH": "/a b"}, {"API_TOKEN": "short"}, {"MAX_UPLOAD_BYTES": "lots"},
])
def test_bad_settings_stop_startup(tmp_path, extra):
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(tmp_path, **extra))


def test_files_may_not_outgrow_the_memory_limit(tmp_path):
    settings = Settings.from_env(base_env(tmp_path))
    gib = 1024**3
    assert memory_problem(settings, 2 * gib, 2 * gib) is not None
    assert memory_problem(settings, 2 * gib, 3 * gib) is None
    assert memory_problem(settings, 2 * gib, None) is None


def test_limits_must_fit_the_store(tmp_path):
    big_upload = Settings.from_env(base_env(tmp_path, MAX_UPLOAD_BYTES=str(3 * 1024**3)))
    big_output = Settings.from_env(base_env(tmp_path, MAX_OUTPUT_BYTES=str(3 * 1024**3)))
    assert "MAX_UPLOAD_BYTES" in memory_problem(big_upload, 2 * 1024**3, None)
    assert "MAX_OUTPUT_BYTES" in memory_problem(big_output, 2 * 1024**3, None)


# ---- the app, with a pretend worker on the real socket ---------------------------------------------
CATALOG = {
    "families": {"document": "Documents", "archive": "Archives"},
    "formats": [
        {"id": "docx", "name": "Word", "family": "document", "exts": ["docx"]},
        {"id": "pdf", "name": "PDF", "family": "document", "exts": ["pdf"]},
        {"id": "zip", "name": "ZIP", "family": "archive", "exts": ["zip"]},
    ],
    "groups": [
        {"engine": "LibreOffice", "reads": ["docx"], "writes": ["pdf"], "grade": "lossy"},
        {"engine": "Package", "reads": ["*"], "writes": ["zip"], "grade": "package", "terminal": True},
    ],
}
ROUTE = {"grade": "lossy", "steps": [{"engine": "LibreOffice", "from": "docx", "to": "pdf"}]}


def wait_until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return False


class FakeWorker:
    """Speaks the real protocol over the real socket, so it can also misbehave on it."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(path)

    def _read(self, size):
        data = b""
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise EOFError
            data += part
        return data

    def recv(self):
        kind, length = wire.HEADER.unpack(self._read(wire.HEADER.size))
        return kind, self._read(length) if length else b""

    def hello(self, catalog=None):
        assert self.recv() == (wire.WELCOME, b"")
        self.send(wire.HELLO, json.dumps({"isolation": "test", "catalog": catalog or CATALOG}).encode())

    def send(self, kind, payload=b""):
        self.sock.sendall(wire.frame(kind, payload))

    def say(self, kind, data):
        self.send(kind, json.dumps(data).encode())

    def ready(self):
        self.send(wire.READY)

    def read_job(self):
        """Returns (what it was told, the file, how the input ended)."""
        kind, payload = self.recv()
        assert kind == wire.JOB
        meta = json.loads(payload)
        body = b""
        while True:
            kind, payload = self.recv()
            if kind == wire.DATA:
                body += payload
            else:
                return meta, body, kind

    def finish(self, body=b"PDFDATA", ready=True, **extra):
        self.say(wire.STATUS, {"step": 0, "route": ROUTE, "detected": "docx"})
        self.say(wire.OUTPUT, {"size": len(body), "bundle": False, "out_ext": "pdf", "step": 1, "route": ROUTE, "detected": "docx", **extra})
        self.send(wire.DATA, body)
        self.send(wire.END)
        if ready:
            self.ready()

    def fail(self, error="It didn't work.", ready=True):
        self.say(wire.FAILED, {"error": error, "route": ROUTE})
        if ready:
            self.ready()

    def closed(self):
        """Has the gateway hung up on it? Anything sent before hanging up doesn't count."""
        try:
            while self.sock.recv(65536):
                pass
        except TimeoutError:
            return False
        except OSError:
            return True
        return True

    def close(self):
        self.sock.close()


class Rig:
    def __init__(self, tmp_path, **env):
        self.settings = Settings.from_env(base_env(tmp_path, **env))
        self.app = create_app(self.settings)
        self.store = self.app.app.state.store
        self.link = self.app.app.state.link
        self.link.cooldown = 0
        self.worker = None
        # Keep requests looking like they come through the host nginx from an allowed client.
        self.client = TestClient(self.app, client=(GATEWAY_IP, 50000), headers={"X-Real-IP": "203.0.113.7"})

    def connect(self):
        self.worker = FakeWorker(self.settings.socket_path)
        self.worker.hello()
        self.worker.ready()
        assert wait_until(lambda: self.link.online)
        return self.worker

    def upload(self, body=b"hello", to="pdf", filename="a.docx", **params):
        return self.client.put("/api/jobs", params={"to": to, **params}, content=body, headers={"X-Filename": quote(filename, safe="")})

    def finished(self, body=b"PDFDATA"):
        """A job that has been through the worker, with the worker free again."""
        job_id = self.upload().json()["id"]
        self.worker.read_job()
        self.worker.finish(body)
        assert wait_until(lambda: self.store.jobs[job_id].state == "done" and self.link.ready.is_set())
        return job_id

    def occupied(self):
        """Keeps the worker busy, so whatever is uploaded next has to wait in line."""
        job_id = self.upload().json()["id"]
        self.worker.read_job()
        return job_id


@pytest.fixture
def rig(tmp_path):
    with_rig = Rig(tmp_path)
    with with_rig.client:
        with_rig.connect()
        yield with_rig
        with_rig.worker.close()


def test_refuses_outside_the_allow_list(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(rig.app, client=(GATEWAY_IP, 1), headers={"X-Real-IP": "198.51.100.1"}) as client:
        assert client.get("/").status_code == 403
        assert client.get("/api/formats").status_code == 403
        assert client.put("/api/jobs", params={"to": "pdf"}, content=b"x").status_code == 403


def test_a_spoofed_header_from_a_stranger_changes_nothing(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(rig.app, client=("198.51.100.1", 1), headers={"X-Real-IP": "203.0.113.7"}) as client:
        assert client.get("/").status_code == 403


def test_x_forwarded_for_is_never_believed(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(rig.app, client=(GATEWAY_IP, 1), headers={"X-Forwarded-For": "203.0.113.7"}) as client:
        assert client.get("/").status_code == 403


def test_a_request_with_no_header_is_refused(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(rig.app, client=(GATEWAY_IP, 1)) as client:
        assert client.get("/").status_code == 403


def test_loopback_gets_no_special_treatment(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(rig.app, client=("127.0.0.1", 1)) as client:
        assert client.get("/").status_code == 403
        assert client.get("/healthz").status_code == 403


def test_page_carries_the_live_marker_and_a_strict_policy(rig):
    res = rig.client.get("/")
    assert res.status_code == 200
    assert '<meta name="omniconvert-live"' in res.text
    assert res.text.startswith("<!doctype html>")
    csp = res.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'sha256-" in csp
    assert "unsafe-inline" not in csp
    assert "connect-src 'self'" in csp
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["cross-origin-opener-policy"] == "same-origin"
    assert "server" not in res.headers


def test_no_api_docs(rig):
    for path in ("/docs", "/redoc", "/openapi.json", "/healthz"):
        assert rig.client.get(path).status_code == 404


def test_no_cookies_and_no_cors(rig):
    res = rig.client.get("/api/formats", headers={"Origin": "https://evil.example"})
    assert "set-cookie" not in res.headers
    assert "access-control-allow-origin" not in res.headers


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_other_sites_cannot_drive_the_api(rig, site):
    res = rig.client.put("/api/jobs", params={"to": "pdf"}, content=b"x", headers={"Sec-Fetch-Site": site})
    assert res.status_code == 403


def test_formats_and_routes(rig):
    data = rig.client.get("/api/formats").json()
    assert data["server"] == {"worker_online": True, "isolation": "test"}
    assert {f["id"] for f in data["formats"]} == {"docx", "pdf", "zip"}
    routes = rig.client.get("/api/routes", params={"from": "docx"}).json()["routes"]
    by_target = {r["to"]: r for r in routes}
    assert by_target["pdf"]["grade"] == "lossy"
    assert by_target["zip"]["grade"] == "package"


def test_unknown_source_gets_only_fallbacks(rig):
    routes = rig.client.get("/api/routes", params={"from": "sldprt"}).json()["routes"]
    assert [r["to"] for r in routes] == ["zip"]


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "pdf;rm -rf /", "PDF", "x" * 40, "--help", ""])
def test_target_must_be_in_the_catalog(rig, bad):
    assert rig.upload(to=bad).status_code == 400


def test_target_with_no_writer_is_refused(rig):
    assert rig.upload(to="docx").status_code == 400


def test_a_conversion_that_isnt_offered_is_refused_before_the_upload_is_read(rig):
    before = rig.store.free_bytes()
    res = rig.upload(b"x" * 500, to="pdf", filename="a.pdf")          # PDF to PDF isn't a conversion
    assert res.status_code == 400 and "PDF files can't be converted to PDF" in res.json()["error"]
    res = rig.upload(to="pdf", filename="a.docx", **{"from": "zip"})   # what the page says it is decides
    assert res.status_code == 400 and "ZIP files can't" in res.json()["error"]
    res = rig.upload(to="pdf", filename="mystery.bin")
    assert res.status_code == 400 and "wasn't recognised" in res.json()["error"]
    assert rig.store.jobs == {} and rig.store.uploading == {} and rig.store.free_bytes() == before


def test_the_fallbacks_are_accepted_for_any_file(rig):
    assert rig.upload(to="zip", filename="mystery.bin").status_code == 202
    assert rig.upload(to="zip", filename="a.docx").status_code == 202


# ---- nothing is kept ------------------------------------------------------------------------------
def test_an_upload_has_no_name_and_no_path(rig):
    rig.occupied()
    job_id = rig.upload(b"content", filename="../../secret/Report.docx").json()["id"]
    job = rig.store.jobs[job_id]
    assert job.filename == "Report.docx"
    assert "(deleted)" in os.readlink(f"/proc/self/fd/{job.fd}")
    assert stat.S_IMODE(os.fstat(job.fd).st_mode) == 0o600
    assert os.pread(job.fd, 100, 0) == b"content"
    assert os.listdir(rig.settings.job_dir) == [".alive"]


def test_a_name_in_the_url_is_ignored(rig):
    res = rig.client.put("/api/jobs", params={"to": "pdf", "from": "docx", "filename": "from-the-url.docx"}, content=b"x")
    assert rig.store.jobs[res.json()["id"]].filename == "file"


def test_the_worker_is_told_nothing_about_who_or_what(rig):
    res = rig.upload(b"content", filename="Report.docx")
    job_id = res.json()["id"]
    kind, payload = rig.worker.recv()
    assert kind == wire.JOB
    assert set(json.loads(payload)) == {"to", "hint", "size"}
    for private in (b"Report", b"203.0.113.7", job_id.encode()):
        assert private not in payload
    assert rig.worker.recv() == (wire.DATA, b"content")
    assert rig.worker.recv() == (wire.END, b"")


def test_the_upload_is_freed_once_the_worker_has_it(rig):
    before = rig.store.free_bytes()
    body = b"x" * 1000
    job_id = rig.upload(body).json()["id"]
    _meta, received, ended = rig.worker.read_job()
    assert received == body and ended == wire.END
    assert wait_until(lambda: rig.store.jobs[job_id].fd is None and rig.store.free_bytes() >= before)


def test_a_download_deletes_the_result(rig):
    before = rig.store.free_bytes()
    job_id = rig.finished(b"%PDF-" * 1000)
    assert rig.store.free_bytes() < before
    res = rig.client.get(f"/api/jobs/{job_id}/download")
    assert res.content == b"%PDF-" * 1000
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404
    assert rig.client.get(f"/api/jobs/{job_id}/download").status_code == 404
    assert rig.store.jobs == {}
    assert rig.store.free_bytes() >= before


def test_a_broken_download_keeps_the_result_to_try_again(rig):
    job_id = rig.finished(b"x" * 500000)

    async def attempt():
        async def receive():
            await asyncio.sleep(3600)

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                raise OSError("connection lost")

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": f"/api/jobs/{job_id}/download", "raw_path": f"/api/jobs/{job_id}/download".encode(), "query_string": b"",
                 "headers": [(b"x-real-ip", b"203.0.113.7")], "client": (GATEWAY_IP, 1), "server": ("test", 80)}
        try:
            await rig.app(scope, receive, send)
        except Exception:
            pass

    rig.client.portal.call(attempt)
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 200
    assert rig.client.get(f"/api/jobs/{job_id}/download").content == b"x" * 500000
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404


def test_an_unclaimed_result_expires(rig):
    before = rig.store.free_bytes()
    job_id = rig.finished(b"x" * 100000)
    rig.store.expire(time.monotonic() + rig.settings.retention_minutes * 60 + 1)
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404
    assert rig.client.get(f"/api/jobs/{job_id}/download").status_code == 404
    assert rig.store.free_bytes() >= before


def test_a_failure_holds_no_file_and_expires_too(rig):
    job_id = rig.upload().json()["id"]
    rig.worker.read_job()
    rig.worker.fail("Password protected.")
    assert wait_until(lambda: rig.store.jobs[job_id].state == "failed")
    view = rig.client.get(f"/api/jobs/{job_id}").json()
    assert view["error"] == "Password protected."
    assert rig.client.get(f"/api/jobs/{job_id}/download").status_code == 404
    rig.store.expire(time.monotonic() + rig.settings.retention_minutes * 60 + 1)
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404


def test_download_headers(rig):
    rig_job = rig.upload(filename="Τιμή <script>.docx").json()["id"]
    rig.worker.read_job()
    rig.worker.finish(b"%PDF-")
    assert wait_until(lambda: rig.store.jobs[rig_job].state == "done")
    view = rig.client.get(f"/api/jobs/{rig_job}").json()
    assert view["output"]["name"].endswith(".pdf")
    assert view["expires_in"] > 0
    res = rig.client.get(f"/api/jobs/{rig_job}/download")
    assert res.status_code == 200
    assert res.content == b"%PDF-"
    assert res.headers["content-type"] == "application/octet-stream"
    assert res.headers["content-disposition"].startswith("attachment;")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "sandbox allow-downloads" in res.headers["content-security-policy"]
    assert len(res.headers.get_list("content-security-policy")) == 1
    assert res.headers["cache-control"] == "no-store"


# ---- the line --------------------------------------------------------------------------------------
def test_one_job_at_a_time(rig):
    first = rig.upload(b"first").json()["id"]
    second = rig.upload(b"second").json()["id"]
    assert rig.worker.read_job()[1] == b"first"
    rig.worker.sock.settimeout(0.3)
    with pytest.raises(TimeoutError):
        rig.worker.recv()
    rig.worker.sock.settimeout(5)
    assert rig.client.get(f"/api/jobs/{second}").json() == {"id": second, "state": "queued", "queue_position": 1}
    rig.worker.finish()
    assert rig.worker.read_job()[1] == b"second"
    assert rig.client.get(f"/api/jobs/{first}").json()["state"] == "done"


def test_uploads_wait_for_a_worker(tmp_path):
    rig = Rig(tmp_path)
    with rig.client:
        assert rig.client.get("/api/formats").json()["server"]["worker_online"] is False
        assert rig.upload().status_code == 503


def test_empty_upload_is_refused(rig):
    assert rig.upload(b"").status_code == 400
    assert rig.store.jobs == {}


def test_oversized_upload_is_refused(rig):
    assert rig.upload(b"x" * 1001).status_code == 413
    assert rig.store.jobs == {} and rig.store.uploading == {}


def test_oversized_upload_without_a_declared_size_is_stopped(rig):
    def chunks():
        for _ in range(20):
            yield b"x" * 100
    res = rig.client.put("/api/jobs", params={"to": "pdf", "from": "docx"}, content=chunks())
    assert res.status_code == 413
    assert rig.store.jobs == {} and rig.store.uploading == {}


def test_a_full_store_is_reported_even_with_no_worker(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    with rig.client:
        monkeypatch.setattr(rig.store, "free_bytes", lambda: 0)
        res = rig.upload()
        assert res.status_code == 507
        assert "memory" in res.json()["error"]


def test_a_store_filling_mid_upload_is_a_clean_refusal(rig, monkeypatch):
    def no_room(_fd, _data):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr("gateway.app.write_all", no_room)
    assert rig.upload().status_code == 507
    assert rig.store.jobs == {} and rig.store.uploading == {}


def test_queue_limit(tmp_path):
    rig = Rig(tmp_path, MAX_QUEUE="2")
    with rig.client:
        rig.connect()
        rig.occupied()
        assert rig.upload().status_code == 202
        assert rig.upload().status_code == 202
        assert rig.upload().status_code == 503
        # A job moving on to the worker frees a place in line.
        rig.worker.finish()
        assert wait_until(lambda: len(rig.store.queue) == 1)
        assert rig.upload().status_code == 202
        rig.worker.close()


def test_per_client_limit(tmp_path):
    rig = Rig(tmp_path, MAX_JOBS_PER_IP="1")
    with rig.client:
        rig.connect()
        assert rig.upload().status_code == 202
        assert rig.upload().status_code == 429
        rig.worker.close()


def test_rate_limit(tmp_path):
    rig = Rig(tmp_path, RATE_LIMIT_PER_MINUTE="3")
    with rig.client:
        assert [rig.client.get("/api/formats").status_code for _ in range(4)][-1] == 429


def test_token_when_configured(tmp_path):
    rig = Rig(tmp_path, API_TOKEN="a" * 32)
    with rig.client:
        assert rig.client.get("/api/formats").status_code == 401
        assert rig.client.get("/api/formats", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert rig.client.get("/api/formats", headers={"Authorization": f"Bearer {'a' * 32}"}).status_code == 200


def test_delete_while_queued(rig):
    rig.occupied()
    before = rig.store.free_bytes()
    job_id = rig.upload(b"x" * 1000).json()["id"]
    assert rig.client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404
    assert rig.store.queue == [] and rig.store.free_bytes() >= before


def test_delete_while_running_tells_the_worker_and_moves_on(rig):
    job_id = rig.occupied()
    assert rig.client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert rig.worker.recv() == (wire.CANCEL, b"")
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404
    next_job = rig.upload(b"next").json()["id"]
    rig.worker.ready()
    assert rig.worker.read_job()[1] == b"next"
    assert next_job in rig.store.jobs


def test_a_result_arriving_for_a_deleted_job_is_dropped(rig):
    before = rig.store.free_bytes()
    job_id = rig.occupied()
    rig.client.delete(f"/api/jobs/{job_id}")
    assert rig.worker.recv() == (wire.CANCEL, b"")
    rig.worker.finish(b"x" * 100000)
    assert wait_until(lambda: rig.link.ready.is_set())
    assert rig.store.jobs == {} and rig.store.free_bytes() >= before
    assert rig.link.online


def test_delete_after_finishing(rig):
    job_id = rig.finished()
    assert rig.client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert rig.client.get(f"/api/jobs/{job_id}").status_code == 404
    assert rig.client.get(f"/api/jobs/{job_id}/download").status_code == 404


def test_a_result_with_no_room_fails_cleanly(rig, monkeypatch):
    async def no_room(_size):
        return False
    monkeypatch.setattr(rig.link, "_wait_for_room", no_room)
    job_id = rig.upload().json()["id"]
    rig.worker.read_job()
    rig.worker.finish(b"x" * 1000)
    assert wait_until(lambda: rig.store.jobs[job_id].state == "failed")
    assert "memory" in rig.client.get(f"/api/jobs/{job_id}").json()["error"]
    assert wait_until(lambda: rig.link.ready.is_set())
    assert rig.link.online


def test_a_store_filling_mid_result_fails_cleanly(rig, monkeypatch):
    def no_room(_fd, _data):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(gateway_link, "write_all", no_room)
    job_id = rig.upload().json()["id"]
    rig.worker.read_job()
    rig.worker.finish(b"x" * 1000)
    assert wait_until(lambda: rig.store.jobs[job_id].state == "failed")
    assert wait_until(lambda: rig.link.ready.is_set())
    assert rig.link.online


def test_a_failure_during_the_result_drops_what_arrived(rig):
    before = rig.store.free_bytes()
    job_id = rig.upload().json()["id"]
    rig.worker.read_job()
    rig.worker.say(wire.OUTPUT, {"size": 200000, "out_ext": "pdf"})
    rig.worker.send(wire.DATA, b"x" * 100000)
    rig.worker.fail("It broke.")
    assert wait_until(lambda: rig.store.jobs[job_id].state == "failed")
    assert rig.client.get(f"/api/jobs/{job_id}").json()["error"] == "It broke."
    assert wait_until(lambda: rig.store.free_bytes() >= before)


# ---- a hostile or broken worker ----------------------------------------------------------------------
def test_the_socket_is_closed_to_others(rig):
    assert stat.S_IMODE(os.stat(rig.settings.socket_path).st_mode) == 0o660


def test_a_worker_dying_fails_its_job_only(rig):
    running = rig.occupied()
    waiting = rig.upload(b"waiting").json()["id"]
    rig.worker.close()
    assert wait_until(lambda: not rig.link.online)
    assert wait_until(lambda: rig.store.jobs[running].state == "failed")
    assert "stopped" in rig.client.get(f"/api/jobs/{running}").json()["error"]
    assert rig.upload().status_code == 503
    # The next worker to arrive picks up what was waiting.
    rig.connect()
    assert rig.worker.read_job()[1] == b"waiting"
    assert waiting in rig.store.jobs


def test_a_second_connection_is_refused_and_disturbs_nothing(rig):
    impostor = FakeWorker(rig.settings.socket_path)
    assert impostor.closed()
    impostor.close()
    assert rig.link.online
    rig.upload(b"mine")
    assert rig.worker.read_job()[1] == b"mine"


def test_nobody_can_connect_just_after_a_session_ends(rig):
    rig.link.cooldown = 30
    rig.worker.close()
    assert wait_until(lambda: not rig.link.session_open)
    impostor = FakeWorker(rig.settings.socket_path)
    assert impostor.closed()
    impostor.close()
    assert not rig.link.online


def test_nobody_can_connect_just_after_the_gateway_starts(tmp_path):
    rig = Rig(tmp_path)
    rig.link.cooldown = 30
    with rig.client:
        impostor = FakeWorker(rig.settings.socket_path)
        assert impostor.closed()
        impostor.close()


def test_the_wrong_user_cannot_connect(tmp_path):
    rig = Rig(tmp_path, WORKER_UID="1")
    with rig.client:
        impostor = FakeWorker(rig.settings.socket_path)
        assert impostor.closed()
        impostor.close()


def expect_hang_up(rig):
    assert rig.worker.closed()
    assert wait_until(lambda: not rig.link.online)


def test_a_bad_catalog_ends_the_session_and_keeps_the_good_one(rig):
    good = rig.link.catalog
    rig.worker.close()
    assert wait_until(lambda: not rig.link.session_open)
    bad = FakeWorker(rig.settings.socket_path)
    bad.hello({"formats": [{"id": "../x"}], "groups": []})
    assert bad.closed()
    bad.close()
    assert rig.link.catalog is good
    assert rig.client.get("/api/routes", params={"from": "docx"}).status_code == 200


def test_it_must_say_hello_first(tmp_path):
    rig = Rig(tmp_path)
    with rig.client:
        bad = FakeWorker(rig.settings.socket_path)
        assert bad.recv() == (wire.WELCOME, b"")
        bad.send(wire.READY)
        assert bad.closed()
        bad.close()
        assert not rig.link.online


@pytest.mark.parametrize("kind,length", [
    (200, 0),                          # a kind that doesn't exist
    (wire.JOB, 0),                     # only the gateway sends these
    (wire.WELCOME, 0),
    (wire.STATUS, wire.MAX_MESSAGE + 1),
    (wire.DATA, wire.MAX_DATA + 1),
    (wire.HELLO, wire.MAX_HELLO + 1),
])
def test_frames_that_are_wrong_on_their_face_end_the_session(rig, kind, length):
    rig.occupied()
    rig.worker.sock.sendall(wire.HEADER.pack(kind, length) + b"\0" * min(length, 16))
    expect_hang_up(rig)


@pytest.mark.parametrize("kind,payload", [
    (wire.STATUS, b"not json"),
    (wire.STATUS, b'{"step": 99}'),
    (wire.STATUS, b'{"step": -1}'),
    (wire.STATUS, b'{"route": {"grade": "nonsense", "steps": []}}'),
    (wire.STATUS, b'{"detected": "' + b"x" * 40 + b'"}'),
    (wire.FAILED, b'{"error": "' + b"x" * 501 + b'"}'),
    (wire.FAILED, b"{}"),
    (wire.OUTPUT, b'{"size": -1, "out_ext": "pdf"}'),
    (wire.OUTPUT, b'{"size": 1, "out_ext": "../../evil"}'),
    (wire.OUTPUT, b'{"size": 1}'),
    (wire.OUTPUT, b'{"size": 1000001, "out_ext": "pdf"}'),   # past MAX_OUTPUT_BYTES
    (wire.DATA, b"data with no output open"),
    (wire.END, b""),
])
def test_invalid_messages_end_the_session_and_reach_nobody(rig, kind, payload):
    job_id = rig.occupied()
    rig.worker.send(kind, payload)
    expect_hang_up(rig)
    view = rig.client.get(f"/api/jobs/{job_id}").json()
    assert view["state"] == "failed"
    assert "stopped" in view["error"]


def test_more_output_than_announced_ends_the_session(rig):
    rig.occupied()
    rig.worker.say(wire.OUTPUT, {"size": 3, "out_ext": "pdf"})
    rig.worker.send(wire.DATA, b"too long")
    expect_hang_up(rig)


def test_less_output_than_announced_ends_the_session(rig):
    job_id = rig.occupied()
    rig.worker.say(wire.OUTPUT, {"size": 300, "out_ext": "pdf"})
    rig.worker.send(wire.DATA, b"short")
    rig.worker.send(wire.END)
    expect_hang_up(rig)
    assert rig.client.get(f"/api/jobs/{job_id}/download").status_code == 404


def test_two_outputs_for_one_job_end_the_session(rig):
    rig.occupied()
    rig.worker.say(wire.OUTPUT, {"size": 3, "out_ext": "pdf"})
    rig.worker.say(wire.OUTPUT, {"size": 3, "out_ext": "pdf"})
    expect_hang_up(rig)


def test_status_after_the_end_ends_the_session(rig):
    rig.occupied()
    rig.worker.fail("Done for.", ready=False)
    rig.worker.say(wire.STATUS, {"step": 0})
    expect_hang_up(rig)


def test_speaking_with_no_job_running_ends_the_session(rig):
    rig.worker.say(wire.STATUS, {"step": 0})
    expect_hang_up(rig)


def test_a_job_past_its_deadline_ends_the_session(rig):
    job_id = rig.occupied()
    assert wait_until(lambda: rig.store.jobs[job_id].deadline is not None)
    rig.store.jobs[job_id].deadline = time.monotonic() - 1
    rig.worker.send(wire.BEAT)
    expect_hang_up(rig)
    assert rig.store.jobs[job_id].state == "failed"


@pytest.mark.parametrize("bad", ["../../../etc/passwd", "short", "a" * 44, "..%2f..%2fetc", "a" * 42 + "/"])
def test_bad_job_ids_are_just_not_found(rig, bad):
    assert rig.client.get(f"/api/jobs/{bad}").status_code in (404, 405)
    assert rig.client.get(f"/api/jobs/{bad}/download").status_code in (404, 405)


def test_unknown_job_is_not_found(rig):
    assert rig.client.get(f"/api/jobs/{new_job_id()}").status_code == 404
    assert rig.client.delete(f"/api/jobs/{new_job_id()}").status_code == 404


def test_subpath_is_accepted_when_nginx_doesnt_strip_it(tmp_path):
    rig = Rig(tmp_path, BASE_PATH="/tools/convert")
    with rig.client:
        assert rig.client.get("/tools/convert/").status_code == 200
        assert rig.client.get("/tools/convert/api/formats").status_code == 200
        assert rig.client.get("/").status_code == 200
        assert rig.client.get("/tools/convertx/").status_code == 404


def test_the_page_only_uses_relative_urls():
    text = PAGE.read_text(encoding="utf-8")
    script = text[text.rindex("<script>"):]
    for call in ("api('/", "fetch('/", '"/api', "'/api", "`/api"):
        assert call not in script
    assert 'href="/' not in text and 'src="/' not in text


def test_the_page_never_puts_a_file_name_in_a_url():
    text = PAGE.read_text(encoding="utf-8")
    assert "filename:" not in text
    assert "'X-Filename'" in text


def test_catalog_refuses_bad_ids(tmp_path):
    from shared.router import Catalog
    with pytest.raises(ValueError):
        Catalog({"formats": [{"id": "../x"}], "groups": []})
    with pytest.raises(ValueError):
        Catalog({"formats": [], "groups": [{"engine": "e", "reads": [], "writes": [], "grade": "nonsense"}]})
