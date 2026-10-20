"""Check that a packaged app serves its installed UI without a webview."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def main():
    executable = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as folder:
        flag = Path(folder) / "url.txt"
        process = subprocess.Popen([str(executable), "--smoke-browser", str(flag)])
        try:
            for _ in range(600):
                if flag.exists():
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"App exited early ({process.returncode})")
                time.sleep(.1)
            url = flag.read_text(encoding="utf-8")
            origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
            page = urlopen(url, timeout=10).read().decode()
            assert "window.SONALOOP_BROWSER=true" in page
            assert "SonaLoop" in page
            assert urlopen(url + "logo.svg", timeout=10).status == 200
            assert json.loads(urlopen(url + "events", timeout=10).read()) == []
            headers = {"Content-Type": "application/json", "Origin": origin}
            rejected = Request(url + "api/close", data=b"[]", headers={
                "Content-Type": "application/json", "Origin": "https://example.com"}, method="POST")
            try:
                urlopen(rejected, timeout=10)
                raise AssertionError("Cross-origin request was accepted")
            except HTTPError as exc:
                assert exc.code == 403
            request = Request(url + "api/save_snapshot", data=b"[]", headers=headers, method="POST")
            assert json.loads(urlopen(request, timeout=10).read())["error"] == "Load a preview first."
            request = Request(url + "api/close", data=b"[]", headers=headers, method="POST")
            urlopen(request, timeout=10).read()
            assert process.wait(timeout=15) == 0
            print("Local browser UI passed")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
