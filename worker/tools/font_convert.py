"""Web font and XML wrappers around the same font data. Runs inside the sandbox."""

import os
import subprocess
import sys

from fontTools.ttLib import TTFont

FLAVOR = {"woff": "woff", "woff2": "woff2", "ttf": None, "otf": None}


def main():
    source, output, src_fmt, dst_fmt = sys.argv[1:5]
    font = TTFont()
    if src_fmt == "ttx":
        font.importXML(source)
    else:
        font = TTFont(source)

    if dst_fmt == "ttx":
        font.flavor = None
        font.saveXML(output)
        return

    is_cff = "CFF " in font or "CFF2" in font
    if (dst_fmt == "ttf" and is_cff) or (dst_fmt == "otf" and not is_cff and "glyf" in font):
        # A web font can hold either kind of outline, which isn't known until it's opened. Changing
        # the kind means redrawing the glyphs, so unwrap it and let FontForge do that.
        font.flavor = None
        middle = f"{output}.unwrapped.{'otf' if is_cff else 'ttf'}"
        font.save(middle)
        subprocess.run(["fontforge", "-quiet", "-lang=ff", "-c", "Open($1); Generate($2);", middle, output],
                       check=True, stdin=subprocess.DEVNULL)
        os.unlink(middle)
        return
    font.flavor = FLAVOR[dst_fmt]
    font.save(output)


main()
