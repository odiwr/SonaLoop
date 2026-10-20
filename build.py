"""Build on the target operating system; release automation packages the result."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import imageio_ffmpeg
import playwright

root = Path(__file__).parent
os.chdir(root)
encoder = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), encoder)
args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--name", "SonaLoop", "--collect-all", "playwright",
        "--collect-all", "webview", "--add-binary", f"{encoder}{os.pathsep}."]
for file in ["index.html", "logo.svg", "logo.ico", "logo.png", "README.md", "licenses"]:
    args += ["--add-data", f"{file}{os.pathsep}."]
browser_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
browsers = Path(browser_env) if browser_env and browser_env != "0" else Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
if not browsers.is_dir():
    raise RuntimeError("Install the bundled renderers first: PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install chromium --only-shell; python -m playwright install firefox")
if sys.platform == "win32":
    args += ["--add-data", f"{browsers}{os.pathsep}playwright/driver/package/.local-browsers"]
    args += ["--onefile", "--icon", "logo.ico", "--collect-all", "clr_loader",
             "--collect-all", "pythonnet"]
elif sys.platform == "darwin":
    from PIL import Image
    Image.open("logo.png").save("logo.icns")
    args += ["--onedir", "--icon", "logo.icns", "--osx-bundle-identifier", "com.odiwr.sonaloop"]
else:
    args += ["--onedir", "--hidden-import", "gi", "--hidden-import", "gi.repository.Gtk",
             "--hidden-import", "gi.repository.WebKit2", "--hidden-import", "gi.repository.Soup"]
args += ["app.py"]
subprocess.run(args, check=True)
if sys.platform == "darwin":
    shutil.copytree(browsers, root / "dist" / "SonaLoop.app" / "Contents" / "MacOS" / ".local-browsers", dirs_exist_ok=True)
elif sys.platform == "linux":
    shutil.copytree(browsers, root / "dist" / "SonaLoop" / ".local-browsers", dirs_exist_ok=True)
