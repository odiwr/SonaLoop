import base64
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
import traceback
import subprocess
import webview
from engine import Renderer, Cancelled, export_media, ffmpeg_path, run_process

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


class API:
    def __init__(self):
        self._window = None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._snapshot = None
        self._last_output = None

    def _emit(self, kind, data):
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
            self._emit("done", {"path": self._last_output})
        return self._job(task)

    def import_file(self):
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

    def save_snapshot(self):
        if not self._snapshot:
            return {"error": "Load a preview first."}
        result = self._window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename="SonaLoop-display-case.json", file_types=("JSON (*.json)",))
        if result:
            path = result if isinstance(result, str) else result[0]
            Path(path).write_text(json.dumps(self._snapshot, indent=2), encoding="utf-8")
        return {"ok": True}

    def minimize(self):
        self._window.minimize()

    def close(self):
        self._cancel_event.set()
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


def main():
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
