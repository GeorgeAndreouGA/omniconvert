"""Response safety: security headers, the served page and its CSP, and download names."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote

# The same reset the Artifact tool wraps the page in, so the page renders the same in both places.
_RESET = (
    ":root{color-scheme:light;padding-top:env(safe-area-inset-top,0);"
    "padding-bottom:env(safe-area-inset-bottom,0)}"
    "body{margin:0;font:14px system-ui}img{max-width:100%}[hidden]{display:none!important}"
)

_BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (
        b"permissions-policy",
        b"accelerometer=(), autoplay=(), camera=(), display-capture=(), encrypted-media=(), "
        b"geolocation=(), gyroscope=(), hid=(), idle-detection=(), magnetometer=(), microphone=(), "
        b"midi=(), payment=(), publickey-credentials-get=(), screen-wake-lock=(), serial=(), "
        b"usb=(), xr-spatial-tracking=()",
    ),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-embedder-policy", b"require-corp"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"origin-agent-cluster", b"?1"),
    (b"x-permitted-cross-domain-policies", b"none"),
    (b"x-dns-prefetch-control", b"off"),
]

# Applied to everything except the page, which has its own policy below.
_LOCKED_CSP = b"default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"


# The sandbox is for a browser that ignores the attachment header and tries to render the file: it
# still runs nothing from it. allow-downloads says plainly that saving the file is the one thing
# this response is for. Downloads were tested with these headers in Chromium and Firefox.
DOWNLOAD_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox allow-downloads"


def security_headers(has_csp: bool) -> list[tuple[bytes, bytes]]:
    headers = list(_BASE_HEADERS)
    if not has_csp:
        headers.append((b"content-security-policy", _LOCKED_CSP))
    return headers


def _sha256(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


class Page:
    """ui/page.html, wrapped the way the Artifact tool wraps it, with a CSP that names its exact
    inline blocks by hash. Nothing else can run on the page, and it can't talk to any other host."""

    def __init__(self, path: Path):
        body = path.read_text(encoding="utf-8")
        self.html = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="omniconvert-live" content="1">'
            f"<style>{_RESET}</style></head><body>{body}</body></html>"
        ).encode("utf-8")
        styles = re.findall(r"<style>(.*?)</style>", body, re.S)
        scripts = re.findall(r"<script>(.*?)</script>", body, re.S)
        style_src = " ".join(_sha256(s) for s in [_RESET, *styles])
        script_src = " ".join(_sha256(s) for s in scripts)
        self.csp = (
            "default-src 'none'; "
            f"script-src {script_src}; "
            f"style-src {style_src}; "
            "font-src data:; "
            "connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
            "require-trusted-types-for 'script'"
        )


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_filename(name: str | None, fallback: str = "file") -> str:
    """A name fit to show and offer for download. It never touches the disk or a command line."""
    text = unicodedata.normalize("NFC", name or "")
    text = _CONTROL.sub("", text).replace("\\", "/")
    text = text.rsplit("/", 1)[-1].strip().lstrip(".").strip()
    # Bidirectional overrides can make `exe.fdp` display as `pdf.exe`.
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    if not text:
        return fallback
    encoded = text.encode("utf-8")
    if len(encoded) > 200:
        stem, ext = os.path.splitext(text)
        ext = ext[:16]
        keep = 200 - len(ext.encode("utf-8"))
        stem = stem.encode("utf-8")[:keep].decode("utf-8", "ignore")
        text = stem + ext
    return text


def output_name(original: str, out_ext: str, bundle: bool) -> str:
    match = re.search(r"\.(tar\.[a-z0-9]+|[A-Za-z0-9_-]{1,16})$", original)
    stem = original[: match.start()] if match and match.start() > 0 else original
    name = f"{stem}.{out_ext}"
    return f"{name}.zip" if bundle and out_ext != "zip" else name


def content_disposition(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
