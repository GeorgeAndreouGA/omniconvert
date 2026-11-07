"""Docker health check: the supervisor refreshes this file every couple of seconds."""

import os
import sys
import time

try:
    age = time.time() - os.stat("/tmp/.alive").st_mtime
except OSError:
    sys.exit(1)
sys.exit(0 if age < 30 else 1)
