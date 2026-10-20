"""Local 360sona renderer. The website supplies assets; no account credentials are stored."""
import base64
import html
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse

from PIL import Image
import playwright
from playwright.sync_api import sync_playwright


class Cancelled(Exception):
    pass


def parse_source(text):
    text = text.strip()
    if not text:
        raise ValueError("Paste a published 360sona link or its iframe code first.")
    if text.startswith("{"):
        data = json.loads(text)
        if isinstance(data.get("config"), dict) and data.get("avatar"):
            source = data.get("source", "https://360sona.com/w/k_sonalooplocal12345")
            if not re.fullmatch(r"https://360sona\.com/w/[A-Za-z0-9_-]+", source):
                raise ValueError("The snapshot has an invalid source URL.")
            return source, data, data.get("export", {})
        link = data.get("url") or data.get("embedUrl")
        if link:
            return parse_source(link)[0], None, data.get("export", {})
        raise ValueError("This config has no avatar data. Use the published widget link, or a SonaLoop snapshot.")
    match = re.search(r'https://360sona\.com/w/[A-Za-z0-9_-]+', html.unescape(text))
    if match:
        return match.group(), None, {}
    # Saved widget pages can retain the original URL in the browser's saved-from comment.
    raise ValueError("No published widget link was found. The saved editor page contains controls, not your live avatar. Paste its published /w/ link or import a SonaLoop snapshot.")


def bridge_script(source):
    """Use exported names rather than binding to the site's changing minified aliases."""
    match = re.search(r"export\{([^}]+)\}", source)
    if not match:
        raise RuntimeError("360sona's renderer format changed. This version cannot safely control its animation.")
    exports = {}
    for item in match.group(1).split(","):
        bits = item.strip().split(" as ")
        if len(bits) == 2:
            exports[bits[1]] = bits[0]
    required = ("live", "draw", "resizeLive")
    if not all(k in exports for k in required):
        raise RuntimeError("The widget is missing its renderer exports.")
    decoder = re.search(r"const\s+\w+=(\w+);\(function", source)
    if not decoder:
        raise RuntimeError("The renderer adapter needs an update for this website version.")
    names = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", source))
    refs = ",".join(json.dumps(n) + f":typeof {n}==='function'?{n}:null" for n in names)
    api = ",".join(f"{k}:{v}" for k, v in exports.items())
    inject = f"""window.__sl={{{api},functions:{{{refs}}},decode:{decoder.group(1)}}};
    window.__sl.connect=()=>{{
      const api=window.__sl;
      const decode=fn=>fn.toString().replace(/\\b[A-Za-z_$][\\w$]*\\(0x([0-9a-f]+)\\)/g,
        (m,n)=>{{try{{const v=api.decode(parseInt(n,16));return typeof v==='string'?JSON.stringify(v):m;}}catch{{return m;}}}});
      const tick=Object.values(api.functions).filter(Boolean).map(decode).find(s=>s.includes('requestAnimationFrame(')&&s.includes('yaw')&&s.includes('frameAt'));
      if(!tick) throw new Error('Cannot locate the animation clock in this renderer version.');
      const update=tick.match(/yaw[\\s\\S]{{0,180}}?,([\\w$]+)\\(\\)/);
      const pose=tick.match(/frameAt[\\s\\S]{{0,180}}?,([\\w$]+)\\(\\)/);
      if(!update||!pose||!api.functions[update[1]]||!api.functions[pose[1]])
        throw new Error('Cannot safely synchronize this renderer version.');
      api.update=api.functions[update[1]];api.pose=api.functions[pose[1]];
    }};
    """
    return source[:match.start()] + inject + source[match.start():]


class Renderer:
    def __init__(self, text, options, cancel=None, report=None):
        self.url, self.snapshot, self.saved_export = parse_source(text)
        self.options = options
        self.cancel = cancel or threading.Event()
        self.report = report or (lambda *args: None)
        self.payload = None
        self.pw = self.browser = None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("Cancelled. No unfinished export was saved.")

    def open(self):
        self.check()
        self.report("Loading your display case…", 1)
        bundled = Path(sys.executable).parent / ".local-browsers"
        if bundled.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
        elif (Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers").is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        self.pw = sync_playwright().start()
        browser_choice = self.options.get("browser", "auto")
        candidates = []
        browser_type = self.pw.firefox if browser_choice == "firefox" else self.pw.chromium
        if browser_choice == "firefox":
            candidates = [{}]
        elif browser_choice == "auto":
            candidates = [{}, {"channel": "chrome"}, {"channel": "msedge"}]
        elif browser_choice in ("chrome", "msedge", "chromium"):
            candidates = [{}] if browser_choice == "chromium" else [{"channel": browser_choice}]
        elif browser_choice == "custom":
            browser_path = Path(self.options.get("browser_path", "")).expanduser()
            if not browser_path.is_file():
                raise ValueError("Choose the executable file for your Chromium-based browser.")
            candidates = [{"executable_path": str(browser_path)}]
        else:
            raise ValueError("Choose a supported rendering browser.")
        errors = []
        for candidate in candidates:
            try:
                launch_args = {"headless": True, **candidate}
                if browser_choice != "firefox":
                    launch_args["args"] = ["--enable-unsafe-swiftshader"]
                self.browser = browser_type.launch(**launch_args)
                break
            except Exception as exc:
                errors.append(str(exc))
        if not self.browser:
            raise RuntimeError("No compatible rendering browser could start. Try Automatic or the included Firefox engine. " + errors[-1][-400:])
        self.page = self.browser.new_page(viewport={"width": 320, "height": 500},
            device_scale_factor=max(1, float(self.options.get("scale", 1))),
            reduced_motion="no-preference")
        self.page.set_default_timeout(90000)

        def document(route):
            response = route.fetch(headers={**route.request.headers, "accept-encoding": "identity"})
            if not response.ok:
                raise RuntimeError(f"The widget could not be loaded (HTTP {response.status}).")
            route.fulfill(response=response, body=bridge_script(response.text()))

        def data(route):
            if self.snapshot:
                value = self.snapshot
                route.fulfill(status=200, content_type="application/json", body=json.dumps(value))
            else:
                response = route.fetch()
                if not response.ok:
                    route.fulfill(response=response)
                    return
                value = response.json()
                if isinstance(value, dict) and isinstance(self.options.get("config_override"), dict):
                    value["config"] = self.options["config_override"]
                    route.fulfill(response=response, body=json.dumps(value))
                else:
                    route.fulfill(response=response)
            if isinstance(value, dict) and "config" in value and "avatar" in value:
                self.payload = value

        self.page.route(self.url, document)
        self.page.route("https://360sona.com/sona/w/*", data)
        self.page.goto(self.url, wait_until="domcontentloaded", timeout=90000)
        try:
            self.page.wait_for_function("window.__sl && __sl.live.ready && document.querySelector('.hasLive')",
                timeout=90000)
        except Exception as exc:
            message = self.page.locator("body").inner_text()[:200]
            raise RuntimeError("The avatar did not finish loading. Check that the widget is published and accessible. " + message) from exc
        self.check()
        if not self.payload:
            raise RuntimeError("The widget loaded without its display-case settings.")
        config = self.payload["config"]
        size = config.get("size", {})
        self.w = max(120, min(1200, int(size.get("w", 320))))
        self.h = max(120, min(1246, int(size.get("h", 500))))
        self.pad = int(self.options.get("padding", 20))
        if not 0 <= self.pad <= 100:
            raise ValueError("Outer spacing must be between 0 and 100 pixels.")
        self.page.set_viewport_size({"width": self.w + 2*self.pad, "height": self.h + 2*self.pad})
        outside = self.options.get("outside", "auto")
        if outside == "auto":
            outside = self.saved_export.get("outside", "white")
            if outside == "auto":
                outside = "white"
        colors = {"white": "#ffffff", "dark": "#202124", "transparent": "transparent"}
        color = colors.get(outside, outside)
        if not re.fullmatch(r"#[0-9a-fA-F]{6}|transparent", color):
            raise ValueError("Choose a valid outer background color.")
        if color == "transparent" and self.options.get("format") == "mp4":
            raise ValueError("MP4 cannot store transparency. Choose a solid outer background or export a GIF.")
        self.transparent = color == "transparent"
        self.outside = outside
        css = f"""html,body{{background:{color}!important}}
        #wbox{{width:{self.w}px;height:{self.h}px;margin:{self.pad}px}}
        .w{{animation:none!important}}"""
        shadow = self.options.get("shadow", "source")
        if shadow == "on":
            css += ".w{box-shadow:0 5px 13px #00000040!important}"
        elif shadow == "off":
            css += ".w{box-shadow:none!important}"
        self.page.add_style_tag(content=css)
        self.page.evaluate("async()=>{await document.fonts.ready;await __sl.resizeLive(document.querySelector('.stage'));}")
        self.page.wait_for_timeout(500)
        self.page.evaluate("__sl.connect()")
        self.info = self.page.evaluate("""()=>{
            const f=__sl.live,c=f.clips.get(f.clipId);
            return {clip:f.clipId,duration:c?.dur||0,spin:f.spin,frozen:f.frozen,playlist:f.playlist,
              shot:f.shot,mode:f.avatar?.posing};
        }""")
        self.info.pop("mode", None)
        self.info.update({"name": config.get("plate", {}).get("name", "Avatar"),
            "width": self.w, "height": self.h, "config": config})
        self.report("Display case loaded.", 5)
        return self.info

    def frame_bytes(self):
        self.check()
        raw = self.page.screenshot(omit_background=self.transparent, animations="allow")
        scale = float(self.options.get("scale", 1))
        if scale < 1:
            im = Image.open(io.BytesIO(raw))
            im = im.resize((max(2, round((self.w+2*self.pad)*scale)),
                            max(2, round((self.h+2*self.pad)*scale))), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            im.save(output, "PNG")
            raw = output.getvalue()
        return raw

    def prepare_cycle(self):
        rotation = self.options.get("rotation", "source")
        turns = 1 if rotation == "full" or (rotation == "source" and self.info["spin"]) else 0
        if self.info["spin"] < 0:
            turns *= -1
        self.page.evaluate("""turns=>{
            const f=__sl.live;cancelAnimationFrame(f.raf);f.paused=true;
            __sl.cycle={yaw:f.home?.yaw??f.orbit.yaw,turns,duration:f.clips.get(f.clipId)?.dur||0,
              animate:!f.frozen&&!!f.clipId};
            f.spin=0;f.playlist=false;f.clipEnd=null;
        }""", turns)
        return turns

    def cycle_frame(self, phase):
        self.page.evaluate("""async phase=>{
            const f=__sl.live,c=__sl.cycle;
            f.orbit.yaw=c.yaw+c.turns*2*Math.PI*phase;
            if(c.animate){f.t=(phase%1)*c.duration;await __sl.pose();}
            await __sl.update();__sl.draw();
        }""", phase)

    def close(self):
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()


def ffmpeg_path():
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    return str(bundled) if bundled.exists() else shutil.which("ffmpeg")


def run_process(args, cancel):
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while proc.poll() is None:
            if cancel.wait(.15):
                proc.terminate()
                proc.wait()
                raise Cancelled("Cancelled. No unfinished export was saved.")
        if proc.returncode:
            log.seek(0)
            raise RuntimeError("Video encoding failed: " + log.read().decode(errors="replace")[-1800:])


def export_media(text, options, destination, cancel, report):
    fmt = options.get("format", "mp4")
    if fmt not in ("mp4", "gif"):
        raise ValueError("Choose MP4 or GIF.")
    scale = float(options.get("scale", 1))
    if scale not in (.5, 1, 2, 3):
        raise ValueError("Choose ½×, 1×, 2×, or 3×.")
    fps = int(options.get("fps", 30))
    repeats = int(options.get("repeats", 2))
    if fps not in (15, 20, 30, 60) or not 1 <= repeats <= 10:
        raise ValueError("Frame rate or repeat count is out of range.")
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("The bundled video encoder is missing.")
    renderer = Renderer(text, options, cancel, report)
    try:
        info = renderer.open()
        mode = options.get("mode", "cycle")
        duration = float(options.get("duration", 0)) or info["duration"] or 12
        if not 1 <= duration <= 120:
            raise ValueError("Cycle duration must be between 1 and 120 seconds.")
        count = max(2, round(duration*fps))
        if count*(renderer.w+2*renderer.pad)*(renderer.h+2*renderer.pad)*scale**2 > 15_000_000_000:
            raise ValueError("This export is too large. Reduce the size, frame rate, or duration.")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Use the output drive for working space; cleanup is scoped to this unique directory.
        with tempfile.TemporaryDirectory(prefix=".sonaloop-", dir=destination.parent) as tmp:
            work = Path(tmp)
            if mode == "cycle":
                renderer.prepare_cycle()
                renderer.cycle_frame(0)
                renderer.cycle_frame(0)  # Settle face-sheet updates before the first frame.
            else:
                renderer.page.clock.install()
                renderer.page.clock.pause_at(renderer.page.evaluate("Date.now()"))
            first = last = None
            for i in range(count):
                renderer.check()
                if mode == "cycle":
                    renderer.cycle_frame(i/count)
                raw = renderer.frame_bytes()
                (work/f"{i:06d}.png").write_bytes(raw)
                first = raw if first is None else first
                last = raw
                if mode != "cycle":
                    renderer.page.clock.run_for(round((i+1)*1000/fps)-round(i*1000/fps))
                report(f"Rendering frame {i+1} of {count}…", 5+75*(i+1)/count)
            # Record reproducible settings, not session cookies or service tokens.
            metadata = {"app": "SonaLoop 1.2.1", "source": renderer.url,
                "options": options, "clip": info["clip"], "cycleSeconds": count/fps,
                "framesPerCycle": count, "repeats": repeats}
            if mode == "cycle":
                renderer.cycle_frame(1)
                endpoint = renderer.frame_bytes()
                from PIL import ImageChops, ImageStat
                a, b = (Image.open(io.BytesIO(x)).convert("RGB") for x in (first, endpoint))
                metadata["endpointMeanDifference"] = ImageStat.Stat(ImageChops.difference(a,b)).mean
            renderer.close()
            renderer = None
            report("Encoding your "+fmt.upper()+"…", 83)
            flags = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            one = work/"cycle.mp4"
            if fmt == "mp4":
                run_process(flags+["-framerate", str(fps), "-i", str(work/"%06d.png"),
                    "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2:color=white",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p", str(one)], cancel)
                output = work/"result.mp4"
                run_process(flags+["-stream_loop", str(repeats-1), "-i", str(one),
                    "-c", "copy", "-movflags", "+faststart", str(output)], cancel)
            else:
                output = work/"result.gif"
                run_process(flags+["-framerate", str(fps), "-i", str(work/"%06d.png"),
                    "-filter_complex", "[0:v]split[a][b];[a]palettegen=reserve_transparent=1[p];[b][p]paletteuse=dither=sierra2_4a:alpha_threshold=128",
                    "-loop", str(0 if options.get("gif_forever", True) else repeats-1),
                    str(output)], cancel)
            if cancel.is_set():
                raise Cancelled("Cancelled.")
            os.replace(output, destination)
            metadata_path = destination.with_suffix(destination.suffix+".json")
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            report("Saved "+destination.name, 100)
            return str(destination)
    finally:
        if renderer:
            renderer.close()
