"""Text to speech with eSpeak NG, picking Greek or English from the text. Runs inside the sandbox."""

import subprocess
import sys


def main():
    source, output = sys.argv[1:3]
    with open(source, encoding="utf-8", errors="replace") as fh:
        sample = fh.read(65536)
    greek = sum(1 for c in sample if "Ͱ" <= c <= "Ͽ" or "ἀ" <= c <= "῿")
    latin = sum(1 for c in sample if c.isascii() and c.isalpha())
    voice = "el" if greek > latin else "en"
    subprocess.run(["espeak-ng", "-v", voice, "-f", source, "-w", output], check=True, stdin=subprocess.DEVNULL)


main()
