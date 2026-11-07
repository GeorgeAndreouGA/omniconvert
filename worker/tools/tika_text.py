"""Whatever text Apache Tika can read from a file. Runs inside the sandbox.

Tika runs with an empty PATH because it would otherwise launch Tesseract and FFmpeg on what it finds,
which is slow, and both already have their own routes. This is the last resort for files nothing else
can read, so Tika giving up on one is expected. Finding no text is a real answer, not a failed
conversion, so either way it gets its own exit code (79) and a plain message.
"""

import os
import shutil
import subprocess
import sys


def main():
    source, output, tmp = sys.argv[1:4]
    java = shutil.which("java")
    env = {**os.environ, "PATH": "/nonexistent"}
    with open(output, "wb") as out:
        done = subprocess.run([java, "-Xmx768m", "-XX:+UseSerialGC", f"-Djava.io.tmpdir={tmp}", "-jar", "/opt/tika/tika-app.jar", "--text", source],
                              stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env, check=False)
    # Killed by a limit is a different story, and the supervisor explains those.
    if done.returncode < 0:
        os.kill(os.getpid(), -done.returncode)
    with open(output, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            if chunk.strip():
                return
    os.unlink(output)
    sys.exit(79)


main()
