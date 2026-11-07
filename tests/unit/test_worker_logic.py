"""The parts of the worker that need no converters installed. Run with: PYTHONPATH=. pytest tests/unit"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from shared import link as wire
from shared.router import Catalog
from worker.formats import FormatDB
from worker.main import Link, LinkClosed, explain_failure, resolve_source
from worker.sandbox_exec import landlock_abi
from worker.verify import output_matches

ROOT = Path(__file__).resolve().parents[2]


def catalog():
    db = FormatDB()
    return db, Catalog({"families": {}, "formats": db.export(), "groups": [
        {"engine": "LibreOffice", "reads": ["pages", "docx"], "writes": ["odt", "pdf"], "grade": "lossy"},
        {"engine": "Calibre", "reads": ["odt"], "writes": ["epub"], "grade": "lossy"},
        {"engine": "ImageMagick", "reads": ["png", "jpg"], "writes": ["png", "jpg", "webp"], "grade": "auto"},
        {"engine": "Frames", "reads": ["mp4"], "writes": ["png"], "grade": "extract", "terminal": True},
        {"engine": "Package", "reads": ["*"], "writes": ["zip"], "grade": "package", "terminal": True},
        {"engine": "Tika", "reads": ["*"], "writes": ["txt"], "grade": "extract", "terminal": True},
        {"engine": "Sidegrade", "reads": ["txt", "zip"], "writes": ["epub"], "grade": "lossy"},
        {"engine": "Long1", "reads": ["rtf"], "writes": ["doc"], "grade": "exact"},
        {"engine": "Long2", "reads": ["doc"], "writes": ["fodt"], "grade": "exact"},
        {"engine": "Long3", "reads": ["fodt"], "writes": ["docx"], "grade": "exact"},
        {"engine": "Long4", "reads": ["docx"], "writes": ["md"], "grade": "exact"},
    ]})


def names(route):
    return [step["engine"] for step in route["steps"]]


def test_chains_engines_when_no_single_one_can():
    _db, cat = catalog()
    route = cat.route("pages", "epub")
    assert names(route) == ["LibreOffice", "Calibre"]
    assert [s["to"] for s in route["steps"]] == ["odt", "epub"]
    assert route["grade"] == "lossy"


def test_grade_is_the_worst_step():
    _db, cat = catalog()
    assert cat.route("png", "jpg")["grade"] == "lossy"
    assert cat.route("jpg", "png")["grade"] == "exact"


def test_auto_grade_treats_a_lossless_target_as_exact():
    _db, cat = catalog()
    assert cat.route("png", "webp")["grade"] == "lossy"


def test_fallbacks_never_chain_into_real_looking_formats():
    _db, cat = catalog()
    assert cat.route("sldprt", "epub") is None
    assert cat.route("unknown", "epub") is None
    # Only the fallbacks: what Tika could read from a file nobody recognised is a guess.
    assert {r["to"] for r in cat.routes_from("unknown")} == {"zip"}


def test_terminal_steps_dont_chain():
    _db, cat = catalog()
    route = cat.route("mp4", "png")
    assert route["steps"][0]["engine"] == "Frames"
    assert cat.route("mp4", "webp") is None


def test_every_route_is_labelled_for_a_closed_format():
    _db, cat = catalog()
    grades = {r["grade"] for r in cat.routes_from("unknown")}
    assert grades <= {"package", "extract"}


def test_no_route_passes_the_hop_limit():
    _db, cat = catalog()
    assert names(cat.route("rtf", "docx")) == ["Long1", "Long2", "Long3"]
    assert cat.route("rtf", "md") is None


def test_no_route_returns_to_its_source():
    _db, cat = catalog()
    assert "png" not in {r["to"] for r in cat.routes_from("png")}


def test_every_table_row_is_well_formed():
    db = FormatDB()
    seen = set()
    for fmt in db.export():
        assert fmt["id"] not in seen
        seen.add(fmt["id"])
    Catalog({"formats": db.export(), "groups": []})


def ext_index(cat):
    index = {}
    for fmt in cat.formats.values():
        for ext in fmt["exts"]:
            index.setdefault(ext, fmt["id"])
    return index


@pytest.mark.parametrize("hint,info,expected", [
    # Content beats the name when the content is specific.
    ("jpg", {"mime": "image/png", "exts": ["png"]}, "png"),
    (None, {"mime": "image/png", "exts": ["png"]}, "png"),
    # A .docx really is a zip. The name is more specific.
    ("docx", {"mime": "application/zip", "exts": []}, "docx"),
    ("xlsx", {"mime": "application/octet-stream"}, "xlsx"),
    ("msg", {"mime": "application/cdfv2"}, "msg"),
    ("cr2", {"mime": "image/tiff", "exts": ["tiff", "tif"]}, "cr2"),
    # Every kind of text looks like text.
    ("md", {"mime": "text/plain"}, "md"),
    ("csv", {"mime": "text/plain"}, "csv"),
    # .m4v and .mp4 are the same container.
    ("mkv", {"mime": "video/webm", "exts": ["webm"]}, "mkv"),
    # And one container serves sound and video.
    ("mka", {"mime": "video/x-matroska", "exts": ["mkv"]}, "mka"),
    ("m4a", {"mime": "video/mp4", "exts": ["mp4"]}, "m4a"),
    # But a real image named as video is an image.
    ("mp4", {"mime": "image/png", "exts": ["png"]}, "png"),
    # A format with its own signature is never really a zip or plain text under another name. That's
    # how a playlist named .mp4 or a script named .png would otherwise reach a parser.
    ("mp4", {"mime": "text/plain"}, "txt"),
    ("png", {"mime": "text/plain"}, "txt"),
    ("wav", {"mime": "text/plain"}, "txt"),
    ("png", {"mime": "application/zip"}, "zip"),
    ("mp4", {"mime": "application/xml"}, "xml"),
    # A PDF isn't whatever its name says, with one exception.
    ("docx", {"mime": "application/pdf", "exts": ["pdf"]}, "pdf"),
    ("ai", {"mime": "application/pdf", "exts": ["pdf"]}, "ai"),
    # TIFF only hides camera RAW.
    ("docx", {"mime": "image/tiff", "exts": ["tiff"]}, "tiff"),
    # A playlist names other files to open, so it never reaches a media parser under any name.
    ("mp4", {"mime": "audio/x-mpegurl", "exts": ["m3u8"]}, "unknown"),
    (None, {"mime": "application/vnd.apple.mpegurl"}, "unknown"),
    # libmagic having no opinion isn't evidence against the name, even for a media format.
    ("wv", {"mime": "application/octet-stream"}, "wv"),
    ("mp4", {"mime": ""}, "mp4"),
    # No name and nothing recognised.
    (None, {"mime": "application/octet-stream"}, "unknown"),
    (None, {}, "unknown"),
    # No name, but Magika knows.
    (None, {"mime": "application/zip", "magika": "docx"}, "docx"),
    (None, {"mime": "text/plain"}, "txt"),
    # A hint that isn't a format is dropped.
    ("nonsense", {"mime": "image/png", "exts": ["png"]}, "png"),
    # Malformed replies from the sandbox don't crash the supervisor.
    ("docx", {"mime": 7, "exts": "x", "magika": []}, "docx"),
    (None, {"exts": [1, None, {}]}, "unknown"),
])
def test_resolve_source(hint, info, expected):
    _db, cat = catalog()
    assert resolve_source(hint, info, cat, ext_index(cat)) == expected


@pytest.mark.parametrize("target,mime,ok", [
    ("png", "image/png", True),
    ("png", "text/plain", False),
    ("pdf", "text/html", False),
    ("pdf", "application/pdf", True),
    ("docx", "application/zip", True),
    ("zip", "application/x-tar", False),
    ("nonesuch", "anything/at-all", True),
    ("png", "", True),
    # A tool that writes a common format under another name, for a target with no check of its own.
    ("exr", "image/png", False),
    ("nonesuch", "image/jpeg", False),
    ("nonesuch", "application/pdf", False),
    ("pdfocr", "application/pdf", True),
    ("apng", "image/png", True),
    # Text comes in many types, but an image isn't one of them.
    ("txt", "application/x-subrip", True),
    ("txt", "image/png", False),
    ("strings", "application/octet-stream", True),
    # Our own encoder's text keeps the original's signature, so its detected type means nothing.
    ("strings", "image/gif", True),
    ("hex", "application/pdf", True),
    ("b64", "image/png", True),
])
def test_output_verification(target, mime, ok):
    assert output_matches(target, mime) is ok


def test_a_table_becomes_xml_with_one_root(tmp_path):
    xmltodict = pytest.importorskip("xmltodict")
    pytest.importorskip("defusedxml")

    def to_xml(data):
        src, out = tmp_path / "in.json", tmp_path / "out.xml"
        src.write_text(json.dumps(data), encoding="utf-8")
        subprocess.run([sys.executable, "-I", str(ROOT / "worker/tools/config_convert.py"), str(src), str(out), "json", "xml"], check=True)
        return xmltodict.parse(out.read_text(encoding="utf-8"))

    rows = [{"1st name": "Ana", "age": 30, "xml id": "a"}, {"1st name": "Bo", "age": None, "xml id": "b"}]
    back = to_xml(rows)
    assert [row["_1st_name"] for row in back["data"]["row"]] == ["Ana", "Bo"]
    assert back["data"]["row"][0]["_xml_id"] == "a"
    assert to_xml({"a": 1, "b": [1, 2]})["data"]["b"] == ["1", "2"]


def test_failure_messages_never_quote_the_tool():
    secret = b"/work/abc: something internal failed at 0xdeadbeef"
    message = explain_failure("FFmpeg", 1, secret)
    assert "/work" not in message and "deadbeef" not in message
    assert "FFmpeg" in message


def test_failure_messages_explain_clear_causes():
    assert "memory" in explain_failure("X", -9, b"")
    assert "too large" in explain_failure("X", -25, b"")
    assert "password" in explain_failure("X", 1, b"Error: file is encrypted")
    assert "too much data" in explain_failure("X", 77, b"")
    assert "other files" in explain_failure("X", 78, b"")


def test_running_out_of_room_is_explained_as_memory():
    assert "memory" in explain_failure("X", 1, b"write error: No space left on device")


# ---- the socket to the gateway ----
def linked_pair():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    a.settimeout(5)
    b.settimeout(5)
    return Link(a), b


def test_frames_arrive_whole_however_they_are_cut():
    link, far = linked_pair()
    data = wire.frame(wire.DATA, b"x" * 70000) + wire.message(wire.JOB, {"to": "pdf", "hint": None, "size": 1})

    def send_in_pieces():
        for start in range(0, len(data), 4099):
            far.sendall(data[start:start + 4099])

    sender = threading.Thread(target=send_in_pieces)
    sender.start()
    assert link.recv() == (wire.DATA, b"x" * 70000)
    kind, payload = link.recv()
    sender.join()
    assert kind == wire.JOB and json.loads(payload)["to"] == "pdf"


def test_a_closed_socket_is_noticed_even_mid_frame():
    link, far = linked_pair()
    far.sendall(wire.HEADER.pack(wire.DATA, 100) + b"short")
    far.close()
    with pytest.raises(LinkClosed):
        link.recv()


@pytest.mark.parametrize("kind,length", [(wire.DATA, wire.MAX_DATA + 1), (wire.JOB, wire.MAX_MESSAGE + 1), (200, 0)])
def test_frames_past_their_limit_are_refused_unread(kind, length):
    link, far = linked_pair()
    far.sendall(wire.HEADER.pack(kind, length))
    with pytest.raises(LinkClosed):
        link.recv()


def test_threads_cannot_interleave_a_frame():
    link, far = linked_pair()
    received = []

    def read_all():
        reader = Link(far)
        for _ in range(40):
            received.append(reader.recv())

    reader = threading.Thread(target=read_all)
    reader.start()
    senders = [threading.Thread(target=lambda c=c: [link.send(wire.DATA, c * 300000) for _ in range(20)]) for c in (b"a", b"b")]
    for t in senders:
        t.start()
    for t in senders:
        t.join()
    reader.join()
    assert len(received) == 40
    assert all(len(set(payload)) == 1 for _, payload in received)


# ---- the sandbox launcher, on whatever kernel runs the tests ----
SANDBOX = ROOT / "worker" / "sandbox_exec.py"


def run_sandboxed(spec, *argv, **kwargs):
    return subprocess.run([sys.executable, "-I", "-S", str(SANDBOX), *argv],
                          env={**os.environ, "OMNI_SANDBOX_SPEC": json.dumps(spec)},
                          capture_output=True, text=True, timeout=30, **kwargs)


def start_victim(folder, tag="not-a-secret"):
    return subprocess.Popen([sys.executable, "-I", "-S", str(SANDBOX), sys.executable, "-I", "-c", "import time; time.sleep(20)"],
                            env={**os.environ, "OMNI_SANDBOX_SPEC": json.dumps({"landlock": landlock_rules(folder)}), "VICTIM_TAG": tag})


def test_limits_apply():
    code = "import resource; print(resource.getrlimit(resource.RLIMIT_CORE)[1], resource.getrlimit(resource.RLIMIT_FSIZE)[1])"
    res = run_sandboxed({"limits": {"core": 0, "fsize": 4096}}, sys.executable, "-I", "-c", code)
    assert res.stdout.split() == ["0", "4096"]


def test_file_size_limit_stops_a_write(tmp_path):
    code = f"open({str(tmp_path / 'f')!r},'wb').write(b'x'*100000)"
    res = run_sandboxed({"limits": {"fsize": 1000}}, sys.executable, "-I", "-c", code)
    assert res.returncode != 0
    assert (tmp_path / "f").stat().st_size <= 1000


def test_new_files_are_private(tmp_path):
    code = f"open({str(tmp_path / 'f')!r},'w')"
    run_sandboxed({}, sys.executable, "-I", "-c", code)
    assert (tmp_path / "f").stat().st_mode & 0o077 == 0


def test_oom_score_is_raised():
    code = "print(open('/proc/self/oom_score_adj').read().strip())"
    res = run_sandboxed({}, sys.executable, "-I", "-c", code)
    assert res.stdout.strip() == "1000"


landlock = pytest.mark.skipif(landlock_abi() == 0, reason="this kernel has no Landlock")


def landlock_rules(rw):
    return {"rx": ["/usr", "/bin", "/lib", "/lib64", "/etc", "/opt", sys.prefix, sys.base_prefix], "ro": ["/proc", "/sys", "/dev/urandom"], "rw": [str(rw)]}


@landlock
def test_landlock_blocks_other_folders(tmp_path):
    mine = tmp_path / "mine"
    other = tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    (other / "secret").write_text("secret")
    code = f"print(open({str(other / 'secret')!r}).read())"
    res = run_sandboxed({"landlock": landlock_rules(mine)}, sys.executable, "-I", "-c", code)
    assert res.returncode != 0
    assert "secret" not in res.stdout


@landlock
def test_landlock_blocks_writing_elsewhere(tmp_path):
    mine = tmp_path / "mine"
    other = tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    code = f"open({str(other / 'x')!r},'w')"
    assert run_sandboxed({"landlock": landlock_rules(mine)}, sys.executable, "-I", "-c", code).returncode != 0
    assert not (other / "x").exists()


@landlock
def test_landlock_blocks_listing_elsewhere(tmp_path):
    mine = tmp_path / "mine"
    other = tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    code = f"import os; print(os.listdir({str(other)!r}))"
    assert run_sandboxed({"landlock": landlock_rules(mine)}, sys.executable, "-I", "-c", code).returncode != 0


@landlock
def test_landlock_allows_its_own_folder(tmp_path):
    code = f"p={str(tmp_path / 'f')!r}; open(p,'w').write('ok'); print(open(p).read())"
    res = run_sandboxed({"landlock": landlock_rules(tmp_path)}, sys.executable, "-I", "-c", code)
    assert res.stdout.strip() == "ok", res.stderr


@landlock
def test_landlock_is_inherited_by_children(tmp_path):
    mine = tmp_path / "mine"
    other = tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    (other / "secret").write_text("secret")
    code = f"import subprocess; print(subprocess.run(['cat',{str(other / 'secret')!r}],capture_output=True,text=True).stdout)"
    res = run_sandboxed({"landlock": landlock_rules(mine)}, sys.executable, "-I", "-c", code)
    assert "secret" not in res.stdout


@landlock
def test_two_jobs_cannot_read_each_others_processes(tmp_path):
    """A job in one sandbox can't read the memory or open files of a job in another."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    victim = start_victim(a)
    try:
        time.sleep(0.5)
        for attack in (
            f"print(open('/proc/{victim.pid}/environ').read())",
            f"print(open('/proc/{victim.pid}/mem','rb').read(1))",
            f"import os; print(os.readlink('/proc/{victim.pid}/fd/0'))",
            f"import os; print(os.readlink('/proc/{victim.pid}/cwd'))",
        ):
            res = run_sandboxed({"landlock": landlock_rules(b)}, sys.executable, "-I", "-c", attack)
            assert res.returncode != 0, (attack, res.stdout)
    finally:
        victim.kill()
        victim.wait()


def test_the_spec_never_reaches_the_converter(tmp_path):
    res = run_sandboxed({"limits": {"core": 0}}, sys.executable, "-I", "-c", "import os,sys; print('OMNI_SANDBOX_SPEC' in os.environ, sys.argv)")
    assert res.stdout.startswith("False")
    assert "limits" not in res.stdout


@landlock
def test_a_job_cannot_signal_another_job(tmp_path):
    if landlock_abi() < 6:
        pytest.skip("signal scoping needs Landlock ABI 6")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    victim = start_victim(a)
    try:
        time.sleep(0.5)
        code = f"import os,signal; os.kill({victim.pid}, signal.SIGKILL)"
        assert run_sandboxed({"landlock": landlock_rules(b)}, sys.executable, "-I", "-c", code).returncode != 0
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


@landlock
def test_setup_failure_stops_the_job(tmp_path):
    res = run_sandboxed({"limits": {"core": "nonsense"}}, "/bin/echo", "should not run")
    assert res.returncode == 126
    assert "should not run" not in res.stdout


def test_ffmpeg_takes_the_sound_straight_out_of_a_video(monkeypatch):
    # Raw audio streams such as aptX, and formats that can hold video such as DV, are "media" in the
    # format table. When their muxers were sorted by that, every video's sound went MP4 > aptX > MP3 or
    # MP4 > DV > AAC: encoded twice, cheaper on paper than taking it out, and graded exact.
    from worker.engines import media

    demuxers = {"mov,mp4": ["mov", "mp4"], "matroska": ["mkv"], "mp3": ["mp3"], "aac": ["aac"], "aptx": ["aptx"], "dv": ["dv"]}
    muxers = {  # name: (extension, video codec, audio codec)
        "mp4": ("mp4", "h264", "aac"), "matroska": ("mkv", "h264", "vorbis"), "mp3": ("mp3", None, "mp3"),
        "adts": ("aac", None, "aac"), "aptx": ("aptx", None, "aptx"), "dv": ("dv", "dvvideo", "pcm_s16le"),
    }

    def fake_run(*args):
        if args[-1] == "-demuxers":
            return " ---\n" + "".join(f" D  {name} x\n" for name in demuxers)
        if args[-1] == "-muxers":
            return " ---\n" + "".join(f"  E {name} x\n" for name in muxers)
        kind, name = args[-1].split("=")
        if kind == "demuxer":
            exts = next(e for spec, e in demuxers.items() if spec.split(",")[0] == name)
            return f"Demuxer {name} [x]:\n    Common extensions: {','.join(exts)}.\n"
        ext, video, audio = muxers[name]
        return (f"Muxer {name} [x]:\n    Common extensions: {ext}.\n"
                + (f"    Default video codec: {video}.\n" if video else "") + f"    Default audio codec: {audio}.\n")

    monkeypatch.setattr(media, "_run", fake_run)
    db = FormatDB()
    groups = media.FFmpeg().describe(db)
    cat = Catalog({"families": {}, "formats": db.export(), "groups": groups})

    def steps(src, dst):
        route = cat.route(src, dst)
        return route and ([s["to"] for s in route["steps"]], route["grade"])

    assert steps("mp4", "aac") == (["aac"], "extract")
    assert steps("mp4", "mp3") == (["mp3"], "extract")
    assert steps("mp4", "aptx") == (["aptx"], "extract")
    assert steps("dv", "aac") == (["aac"], "extract")      # DV holds video, so its sound is extracted too
    assert steps("mp3", "aptx")[0] == ["aptx"]             # an audio stream is still sound
    assert steps("mp4", "mkv") == (["mkv"], "lossy")
    assert cat.route("mp3", "dv") is None                 # sound never becomes a video stream
    assert cat.route("aptx", "dv") is None                # nor does a raw audio stream
    assert [s["to"] for s in cat.route("aptx", "mkv")["steps"]][-1] == "mkv"   # the sound alone, in a container
