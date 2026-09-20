"""Runs the LiveKit adapter audio-mixing harness in headless Chrome.

The harness page publishes two simultaneous audio tracks through a stubbed LiveKit
SDK and checks that both are still audible in the single stream the adapter hands
to the bot's webcam output. Exits non-zero when a tone went missing.

Usage:
    python tests/browser/run_mix_audio.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = "/tests/browser/mix-audio.html"
TIMEOUT_SECONDS = 60

CHROME_CANDIDATES = (
    os.environ.get("CHROME_BINARY"),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    str(Path.home() / ".kombai-binaries/binaries/chrome-linux64/chrome"),
)


def find_chrome():
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    raise SystemExit(
        "No Chrome binary found. Set CHROME_BINARY to a Chrome or Chromium executable."
    )


class HarnessHandler(SimpleHTTPRequestHandler):
    """Serves the repository and collects the harness verdict posted back to /result."""

    result = None
    result_received = threading.Event()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")

        self.send_response(204)
        self.end_headers()

        try:
            HarnessHandler.result = json.loads(body)
        except json.JSONDecodeError:
            HarnessHandler.result = {"passed": False, "error": f"bad payload: {body!r}"}

        HarnessHandler.result_received.set()

    def log_message(self, *args):
        pass


def main():
    chrome = find_chrome()

    handler = partial(HarnessHandler, directory=str(REPOSITORY_ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    threading.Thread(target=server.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as profile:
        browser = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--autoplay-policy=no-user-gesture-required",
                f"--user-data-dir={profile}",
                f"http://127.0.0.1:{port}{HARNESS_PATH}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            received = HarnessHandler.result_received.wait(TIMEOUT_SECONDS)
        finally:
            browser.terminate()
            browser.wait(timeout=10)
            server.shutdown()

    if not received:
        print(f"FAIL: harness reported nothing within {TIMEOUT_SECONDS}s")
        return 1

    result = HarnessHandler.result
    print(json.dumps(result, indent=2))

    if not result.get("passed"):
        print("FAIL: the adapter did not mix both audio tracks")
        return 1

    print("PASS: both audio tracks are audible in the mixed stream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
