import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import sys
import threading
import tempfile
import time
import traceback
import subprocess
import webbrowser
from urllib.parse import quote, unquote
import webview
from engine import Renderer, Cancelled, export_media, ffmpeg_path, run_process

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


class API:
    def __init__(self, browser_mode=False):
        self._window = None
        self.browser_mode = browser_mode
        self._events = queue.SimpleQueue()
        self._server = None
        self._downloads = tempfile.TemporaryDirectory(prefix="sonaloop-downloads-") if browser_mode else None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._snapshot = None
        self._last_output = None

    def _emit(self, kind, data):
        if self.browser_mode:
            self._events.put({"kind": kind, "data": data})
        else:
            self._window.evaluate_js("window.receive("+json.dumps(kind)+","+json.dumps(data)+")")

    def _job(self, fn):
        if not self._lock.acquire(blocking=False):
            return {"error": "An export or preview is already running."}
        self._cancel_event.clear()
        def work():
            try:
                fn()
            except Cancelled as exc:
                self._emit("cancelled", str(exc))
            except Exception as exc:
                self._emit("error", str(exc))
                log = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SonaLoop"
                log.mkdir(exist_ok=True)
                (log/"last-error.log").write_text(traceback.format_exc(), encoding="utf-8")
            finally:
                self._lock.release()
                self._emit("idle", {})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def preview(self, text, options):
        def task():
            preview_options = dict(options, scale=1)
            if preview_options.get("outside") == "transparent":
                preview_options["outside"] = "white"
            renderer = Renderer(text, preview_options, self._cancel_event,
                lambda message, percent: self._emit("progress", {"message": message, "percent": percent}))
            try:
                info = renderer.open()
                self._snapshot = dict(renderer.payload)
                self._snapshot["source"] = renderer.url
                self._snapshot["export"] = {"outside": renderer.outside}
                seconds = float(options.get("duration", 0)) or info["duration"] or 12
                count = max(30, round(seconds * 30))
                with tempfile.TemporaryDirectory(prefix="sonaloop-preview-") as tmp:
                    folder = Path(tmp)
                    if options.get("mode", "cycle") == "cycle":
                        renderer.prepare_cycle()
                        renderer.cycle_frame(0)
                    else:
                        renderer.page.clock.install()
                        renderer.page.clock.pause_at(renderer.page.evaluate("Date.now()"))
                    for i in range(count):
                        if options.get("mode", "cycle") == "cycle":
                            renderer.cycle_frame(i/count)
                        (folder/f"{i:06d}.png").write_bytes(renderer.frame_bytes())
                        if options.get("mode", "cycle") != "cycle":
                            renderer.page.clock.run_for(round((i+1)*1000/30)-round(i*1000/30))
                        self._emit("progress", {"message":"Preparing preview", "percent":10+80*(i+1)/count})
                    target = folder/"preview.mp4"
                    run_process([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                        "-framerate", "30", "-i", str(folder/"%06d.png"), "-vf",
                        "pad=ceil(iw/2)*2:ceil(ih/2)*2:color=white", "-c:v", "libx264",
                        "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(target)], self._cancel_event)
                    self._emit("preview", {"video":base64.b64encode(target.read_bytes()).decode(),
                        "info":info, "seconds":seconds})
            finally:
                renderer.close()
        return self._job(task)

    def export(self, text, options):
        suffix = options.get("format", "mp4")
        if suffix not in ("mp4", "gif"):
            return {"error": "Invalid format."}
        name = "SonaLoop."+suffix
        if self._snapshot:
            raw = self._snapshot.get("config", {}).get("plate", {}).get("name") or "SonaLoop"
            name = "".join(c for c in raw if c.isalnum() or c in "-_ ")[:60]+"."+suffix
        if self.browser_mode:
            path = str(Path(self._downloads.name) / name)
        else:
            result = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name,
                file_types=(f"{suffix.upper()} (*.{suffix})",))
            if not result:
                return {"cancelled": True}
            path = result if isinstance(result, str) else result[0]
        if not path.lower().endswith("."+suffix):
            path += "."+suffix
        def task():
            self._last_output = export_media(text, options, path, self._cancel_event,
                lambda message, percent: self._emit("progress", {"message": message, "percent": percent}))
            self._emit("done", {"path": self._last_output,
                "download": "download/export" if self.browser_mode else None})
        return self._job(task)

    def import_file(self):
        if self.browser_mode:
            return {"error": "Choose the file using your browser."}
        result = self._window.create_file_dialog(webview.FileDialog.OPEN,
            file_types=("Display case (*.html;*.htm;*.json;*.txt)",))
        if not result:
            return None
        path = Path(result[0])
        if path.stat().st_size > 10_000_000:
            return {"error": "This file is too large to import."}
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            data = json.loads(text)
            config = data.get("config", data)
            if isinstance(config, dict) and "stage" in config and "frame" in config and not data.get("avatar"):
                return {"config": config, "name": path.name}
        except (ValueError, AttributeError):
            pass
        return {"text": text, "name": path.name}

    def choose_browser(self):
        if self.browser_mode:
            return {"error": "Enter the browser executable path in the field."}
        selected = self._window.create_file_dialog(webview.FileDialog.OPEN,
            file_types=("Browser executable (*.exe)" if sys.platform == "win32" else "All files (*.*)",))
        return selected[0] if selected else None

    def save_snapshot(self):
        if not self._snapshot:
            return {"error": "Load a preview first."}
        if self.browser_mode:
            return {"download": "download/snapshot"}
        result = self._window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename="SonaLoop-display-case.json", file_types=("JSON (*.json)",))
        if result:
            path = result if isinstance(result, str) else result[0]
            Path(path).write_text(json.dumps(self._snapshot, indent=2), encoding="utf-8")
        return {"ok": True}

    def minimize(self):
        if not self.browser_mode:
            self._window.minimize()

    def close(self):
        self._cancel_event.set()
        if self.browser_mode:
            if self._server:
                threading.Thread(target=self._server.shutdown, daemon=True).start()
        else:
            self._window.destroy()

    def cancel(self):
        self._cancel_event.set()

    def open_output(self):
        if self._last_output and Path(self._last_output).is_file():
            open_path(self._last_output)

    def open_folder(self):
        if self._last_output:
            open_path(str(Path(self._last_output).parent))


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def browser_ui(api, smoke_path=None):
    """Serve the installed UI only on this computer, behind an unguessable URL."""
    token = secrets.token_urlsafe(24)
    prefix = "/" + token + "/"
    last_seen = [time.monotonic()]
    methods = {"preview", "export", "cancel", "open_output", "open_folder",
        "save_snapshot", "minimize", "close"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_bytes(self, body, content_type, status=200, filename=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            return (self.headers.get("Host") == f"127.0.0.1:{server.server_port}"
                and self.path.startswith(prefix))

        def do_GET(self):
            if not self.allowed():
                self.send_error(404)
                return
            route = unquote(self.path[len(prefix):]).split("?", 1)[0]
            if route in ("", "index.html"):
                page = (ROOT / "index.html").read_bytes().replace(
                    b"<script>", b"<script>window.SONALOOP_BROWSER=true;", 1)
                self.send_bytes(page, "text/html; charset=utf-8")
            elif route == "logo.svg":
                self.send_bytes((ROOT / "logo.svg").read_bytes(), "image/svg+xml")
            elif route == "events":
                last_seen[0] = time.monotonic()
                events = []
                while len(events) < 100:
                    try:
                        events.append(api._events.get_nowait())
                    except queue.Empty:
                        break
                self.send_bytes(json.dumps(events).encode(), "application/json")
            elif route == "download/snapshot" and api._snapshot:
                self.send_bytes(json.dumps(api._snapshot, indent=2).encode(),
                    "application/json", filename="SonaLoop-display-case.json")
            elif route == "download/export" and api._last_output and Path(api._last_output).is_file():
                path = Path(api._last_output)
                ascii_name = "".join(c if c.isascii() and (c.isalnum() or c in "-_. ") else "_" for c in path.name)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4" if path.suffix == ".mp4" else "image/gif")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition",
                    f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(path.name)}')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                with path.open("rb") as file:
                    while chunk := file.read(1024 * 1024):
                        self.wfile.write(chunk)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.allowed() or self.headers.get("Origin") != f"http://127.0.0.1:{server.server_port}":
                self.send_error(403)
                return
            method = self.path[len(prefix):]
            if not method.startswith("api/") or method[4:] not in methods:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length > 10_000_000:
                self.send_error(413)
                return
            try:
                args = json.loads(self.rfile.read(length))
                if not isinstance(args, list):
                    raise ValueError("Invalid request")
                result = getattr(api, method[4:])(*args)
                self.send_bytes(json.dumps(result).encode(), "application/json")
            except Exception as exc:
                self.send_bytes(json.dumps({"error": str(exc)}).encode(), "application/json", 400)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    api._server = server
    url = f"http://127.0.0.1:{server.server_port}{prefix}"
    def stop_when_unused():
        while True:
            time.sleep(15)
            if time.monotonic() - last_seen[0] > 90 and not api._lock.locked():
                server.shutdown()
                return
    threading.Thread(target=stop_when_unused, daemon=True).start()
    if smoke_path:
        Path(smoke_path).write_text(url, encoding="utf-8")
    elif not webbrowser.open_new_tab(url):
        open_path(url)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()
        api._cancel_event.set()
        api._downloads.cleanup()


def main():
    if "--smoke-render" in sys.argv:
        position = sys.argv.index("--smoke-render")
        source, target = sys.argv[position+1:position+3]
        browser = sys.argv[sys.argv.index("--browser")+1] if "--browser" in sys.argv else "chromium"
        export_media(source, {"browser": browser, "scale": .5, "format": "mp4",
            "fps": 15, "duration": 1, "repeats": 1}, target,
            threading.Event(), lambda *args: None)
        return
    if "--desktop" not in sys.argv and "--smoke-ui" not in sys.argv:
        api = API(browser_mode=True)
        smoke_path = sys.argv[sys.argv.index("--smoke-browser")+1] if "--smoke-browser" in sys.argv else None
        browser_ui(api, smoke_path)
        return
    api = API()
    window = webview.create_window("SonaLoop", str(ROOT/"index.html"), js_api=api,
        width=1240, height=850, min_size=(1020, 720), background_color="#d6d6d6",
        hidden="--smoke-ui" in sys.argv, frameless=True, easy_drag=False)
    api._window = window
    window.events.closing += lambda: api._cancel_event.set()
    def smoke():
        if "--smoke-ui" in sys.argv:
            import time
            for _ in range(30):
                time.sleep(.3)
                try:
                    result = window.evaluate_js("({title:document.title,bridge:!!window.pywebview?.api,exportButton:!!document.getElementById('export')})")
                    if result.get("bridge"):
                        Path(sys.argv[sys.argv.index("--smoke-ui")+1]).write_text(json.dumps(result))
                        window.destroy()
                        return
                except Exception:
                    pass
            window.destroy()
    icon = "logo.ico" if sys.platform == "win32" else "logo.png"
    webview.start(smoke, gui="edgechromium" if sys.platform == "win32" else "cocoa" if sys.platform == "darwin" else "gtk", debug=False, icon=str(ROOT/icon))


if __name__ == "__main__":
    main()
