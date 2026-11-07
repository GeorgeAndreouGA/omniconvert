"""The only channel between the two containers: framed messages over one Unix socket.

The worker has no network and the containers share no files, so this is how a job gets in and a result
gets out. A frame is one byte of kind, four bytes of length, then the payload. Everything has a size
limit, checked before anything is read, because the gateway treats the worker as hostile.
"""

from __future__ import annotations

import json
import struct

SOCKET_PATH = "/run/omniconvert/link.sock"

HEADER = struct.Struct(">BI")

# Worker to gateway
HELLO = 1    # {isolation, catalog}: the first thing it says
READY = 2    # every trace of the last job is gone, so send the next one
BEAT = 3     # still alive
STATUS = 4   # {step, route, detected}
FAILED = 5   # {error, route}
OUTPUT = 6   # {size, bundle, out_ext, route, detected, step}, then DATA frames, then END
# Either way
DATA = 7
END = 8
# Gateway to worker
JOB = 10     # {to, hint, size}, then DATA frames, then END. Never a job id, a file name or an address
CANCEL = 11  # drop the current job, which may stand in for the input's END
WELCOME = 12 # sent once on accepting a connection; being refused is just the socket closing

MAX_DATA = 1024 * 1024
MAX_MESSAGE = 64 * 1024
MAX_HELLO = 8 * 1024 * 1024

KINDS = {HELLO, READY, BEAT, STATUS, FAILED, OUTPUT, DATA, END, JOB, CANCEL, WELCOME}


def limit_for(kind: int) -> int:
    """How long a frame of this kind may be. Unknown kinds get nothing at all."""
    if kind not in KINDS:
        return -1
    if kind == DATA:
        return MAX_DATA
    if kind == HELLO:
        return MAX_HELLO
    return MAX_MESSAGE


def frame(kind: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


def message(kind: int, data: dict) -> bytes:
    return frame(kind, json.dumps(data).encode())
