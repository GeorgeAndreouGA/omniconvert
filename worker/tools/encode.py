"""Base64, a hex dump, or the readable strings from any file. Runs inside the sandbox."""

import base64
import re
import sys

CHUNK_B64 = 57 * 1024     # a multiple of 3, so chunks join into one valid stream
CHUNK = 1024 * 1024
STRING = re.compile(rb"[\x20-\x7e\t]{6,}")


def to_base64(src, out):
    while chunk := src.read(CHUNK_B64):
        encoded = base64.encodebytes(chunk)
        out.write(encoded)


def to_hex(src, out):
    offset = 0
    while line := src.read(16):
        parts = " ".join(f"{b:02x}" for b in line)
        text = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in line)
        out.write(f"{offset:08x}  {parts:<47}  {text}\n".encode("ascii"))
        offset += len(line)


def to_strings(src, out):
    tail = b""
    found = False
    while chunk := src.read(CHUNK):
        data = tail + chunk
        keep = max(0, len(data) - 4096)
        matches = list(STRING.finditer(data))
        last_end = 0
        for match in matches:
            # A string touching the end may continue in the next chunk.
            if match.end() == len(data):
                break
            out.write(match.group() + b"\n")
            found = True
            last_end = match.end()
        tail = data[max(last_end, keep):]
    for match in STRING.finditer(tail):
        out.write(match.group() + b"\n")
        found = True
    if not found:
        sys.exit(79)


def main():
    source, output, mode = sys.argv[1:4]
    worker = {"b64": to_base64, "hex": to_hex, "strings": to_strings}[mode]
    with open(source, "rb") as src, open(output, "wb") as out:
        worker(src, out)


main()
