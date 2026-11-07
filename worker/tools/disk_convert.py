"""Convert virtual disk images. Runs inside the sandbox.

A disk image can name other files: a qcow2 can have a backing file, and a VMDK lists extents.
qemu-img would open those on request, reading arbitrary files into the output. So every image is
inspected first, and any reference to another file refuses it (exit 78). A small image can also
claim a huge virtual size, so that's checked before anything is written (exit 77).
"""

import json
import os
import subprocess
import sys

QEMU_FORMAT = {"raw": "raw", "qcow2": "qcow2", "vmdk": "vmdk", "vdi": "vdi", "vhd": "vpc", "vhdx": "vhdx", "qed": "qed"}


def main():
    src, output, src_fmt, dst_fmt, max_bytes = sys.argv[1:6]
    source = QEMU_FORMAT[src_fmt]
    # A text descriptor is nothing but a list of other files. An ordinary sparse VMDK starts with
    # the KDMV magic and embeds one that names itself, which the extent check below confirms.
    with open(src, "rb") as fh:
        if fh.read(64).lstrip().startswith(b"#"):
            print("VMDK descriptor files name other files", file=sys.stderr)
            sys.exit(78)

    info = subprocess.run(["qemu-img", "info", "--output=json", "-f", source, src],
                          capture_output=True, text=True, timeout=120, check=True, stdin=subprocess.DEVNULL)
    details = json.loads(info.stdout)
    own_path = os.path.realpath(src)

    def refers_elsewhere(node, top=False):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("backing-filename", "full-backing-filename", "data-file", "data-file-raw") and value:
                    return True
                # Every extent has to be this same file. The image's own "filename" at the top is fine.
                if key == "filename" and not top and os.path.realpath(str(value)) != own_path:
                    return True
                if refers_elsewhere(value):
                    return True
        elif isinstance(node, list):
            return any(refers_elsewhere(item) for item in node)
        return False

    if refers_elsewhere(details, top=True):
        print("image refers to another file", file=sys.stderr)
        sys.exit(78)
    if details.get("virtual-size", 0) > int(max_bytes):
        print("virtual size over the limit", file=sys.stderr)
        sys.exit(77)

    subprocess.run(["qemu-img", "convert", "-U", "-f", source, "-O", QEMU_FORMAT[dst_fmt], src, output],
                   check=True, stdin=subprocess.DEVNULL)


main()
