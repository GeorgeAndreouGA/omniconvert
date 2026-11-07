"""Docker health check: the event loop refreshes this file every few seconds.

It reads a file rather than calling the API because this container is denied the `connect` syscall, so
nothing in it can open a connection, including this.
"""

import os
import sys
import time

try:
    age = time.time() - os.stat("/jobs/.alive").st_mtime
except OSError:
    sys.exit(1)
sys.exit(0 if age < 30 else 1)
