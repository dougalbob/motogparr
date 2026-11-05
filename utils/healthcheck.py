#!/usr/bin/env python3
"""
Container health probe for the WebUI.

Used by the Dockerfile HEALTHCHECK. It exists because the WebUI used to be able
to die while the container reported a healthy "Up": the scheduler kept running,
so nothing looked wrong except an empty browser tab. Docker now gets an explicit
answer instead of an inference.

Kept deliberately tiny and dependency-free - it runs inside the image, where
curl is not installed, and it must never be the reason a container looks
unhealthy for its own sake.

Exit codes:
    0  the WebUI answered 200 on /healthz
    1  it did not
"""

import os
import sys
import urllib.request

PORT = int(os.environ.get("MOTOGPARR_WEBUI_PORT", "5679"))
TIMEOUT = float(os.environ.get("MOTOGPARR_HEALTHCHECK_TIMEOUT", "3"))
# /healthz, not /: the WebUI requires a verified Cloudflare identity on every
# other route, and a probe cannot hold one. A healthcheck that got a 401 would
# report every container unhealthy and defeat the purpose of having one.
URL = f"http://127.0.0.1:{PORT}/healthz"


def check(url: str = URL, timeout: float = TIMEOUT) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    ok = check()
    print(f"WebUI on port {PORT}: {'ok' if ok else 'not responding'}")
    sys.exit(0 if ok else 1)
