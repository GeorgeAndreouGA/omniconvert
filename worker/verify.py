"""Is the output really what was asked for? Catches a tool that "succeeded" with the wrong thing.

Formats libmagic identifies reliably are checked directly. Most others can't be, so there's a second
rule for them: a file that's unmistakably a PNG, JPEG, GIF, WebP or PDF is only accepted if that's what
was asked for. ImageMagick, for one, will write PNG bytes to a name it has no writer for.
"""

from __future__ import annotations

_ZIP = ("application/zip", "application/octet-stream")

EXPECTED: dict[str, tuple[str, ...]] = {
    "png": ("image/png", "image/apng", "image/vnd.mozilla.apng"),
    "jpg": ("image/jpeg",),
    "gif": ("image/gif",),
    "webp": ("image/webp",),
    "tiff": ("image/tiff",),
    "bmp": ("image/bmp", "image/x-ms-bmp"),
    "avif": ("image/avif", "image/heif"),
    "heic": ("image/heic", "image/heif"),
    "ico": ("image/vnd.microsoft.icon", "image/x-icon"),
    "svg": ("image/svg+xml", "text/", "application/xml"),
    "pdf": ("application/pdf",),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml", *_ZIP),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml", *_ZIP),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml", *_ZIP),
    "odt": ("application/vnd.oasis.opendocument.text", *_ZIP),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", *_ZIP),
    "odp": ("application/vnd.oasis.opendocument.presentation", *_ZIP),
    "epub": ("application/epub+zip", *_ZIP),
    "rtf": ("text/rtf", "application/rtf", "text/"),
    "html": ("text/html", "text/xml", "application/xml", "text/plain"),
    "txt": ("text/", "application/json", "application/xml", "message/", "application/x-empty", "application/csv"),
    "csv": ("text/", "application/csv"),
    "json": ("application/json", "text/"),
    "xml": ("text/xml", "application/xml", "text/"),
    "zip": ("application/zip",),
    "7z": ("application/x-7z-compressed",),
    "tar": ("application/x-tar",),
    "tar.gz": ("application/gzip", "application/x-gzip"),
    "tar.bz2": ("application/x-bzip2",),
    "tar.xz": ("application/x-xz",),
    "tar.zst": ("application/zstd", "application/x-zstd"),
    "gz": ("application/gzip", "application/x-gzip"),
    "bz2": ("application/x-bzip2",),
    "xz": ("application/x-xz",),
    "zst": ("application/zstd", "application/x-zstd"),
    "mp3": ("audio/mpeg",),
    "flac": ("audio/flac", "audio/x-flac"),
    "wav": ("audio/x-wav", "audio/wav", "audio/vnd.wave"),
    "ogg": ("audio/ogg", "video/ogg", "application/ogg"),
    "mp4": ("video/mp4", "audio/mp4", "audio/x-m4a", "application/octet-stream"),
    "mkv": ("video/x-matroska", "application/x-matroska", "audio/x-matroska", "application/octet-stream"),
    "webm": ("video/webm", "audio/webm", "application/octet-stream", "video/x-matroska"),
    "mov": ("video/quicktime", "application/octet-stream"),
    "avi": ("video/x-msvideo",),
    "ttf": ("font/sfnt", "font/ttf", "application/x-font-ttf", "application/octet-stream"),
    "otf": ("font/sfnt", "font/otf", "application/vnd.ms-opentype", "application/octet-stream"),
    "woff": ("font/woff", "application/font-woff", "application/octet-stream"),
    "woff2": ("font/woff2", "application/font-woff2", "application/octet-stream"),
    "sqlite": ("application/vnd.sqlite3", "application/x-sqlite3"),
}


# Unmistakable types, and the targets that may legitimately come out as each one.
_STRONG = {
    "image/png": {"png", "apng", "ico", "icns"},
    "image/jpeg": {"jpg", "jps", "pjpeg", "mpo", "jpe"},
    "image/gif": {"gif"},
    "image/webp": {"webp"},
    "application/pdf": {"pdf", "pdfocr", "ai", "epdf"},
}
# Text can carry many types (a subtitle file reads as application/x-subrip). Only reject what plainly
# isn't text.
_NOT_TEXT = ("image/", "audio/", "video/", "font/")


# Written by our own encoder (worker/tools/encode.py), which only ever writes text. That text keeps the
# original's signature (the strings of a GIF start with GIF89a, a PDF's hex dump shows %PDF), so the
# type detected for it says nothing about whether it worked.
_OWN_TEXT = ("strings", "hex", "b64")


def output_matches(target: str, mime: str) -> bool:
    if not mime or target in _OWN_TEXT:
        return True
    if target in ("txt", "csv", "tsv", "strings"):
        return not mime.startswith(_NOT_TEXT)
    allowed = _STRONG.get(mime)
    if allowed is not None and target not in allowed:
        return False
    expected = EXPECTED.get(target)
    if expected is None:
        return True
    return any(mime.startswith(prefix) for prefix in expected)
