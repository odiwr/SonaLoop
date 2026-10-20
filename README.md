# SonaLoop

A small, unofficial exporter for [360sona](https://360sona.com).
Paste a display-case link, preview it, and export an MP4 or GIF.
Supports ½×, 1×, 2×, and 3× resolution.

**This is a just-for-fun project, not an actively maintained app.**
Updates and support aren't promised. Changes to 360sona may break it.

## Download

Get your version from [Releases](https://github.com/odiwr/SonaLoop/releases).

- **Windows:** run `SonaLoop-Windows.exe`. Requires Microsoft Edge.
- **macOS:** unzip and open SonaLoop. The app is unsigned.
- **Linux:** extract the archive and run `SonaLoop/SonaLoop`.
- **Ubuntu 24.04+:** install the `.deb` with `sudo apt install ./sonaloop*.deb`.

For the Linux archive, install its desktop/browser dependencies:

```sh
sudo apt install libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1 gir1.2-gtk-3.0 libnss3 libnspr4 libatk-bridge2.0-0 libxkbcommon0 libgbm1 libasound2t64
```

Internet is required to load the avatar. Exports stay on your computer.
Synchronized mode repeats one animation and full turn; natural mode records the
original movement. Not every source animation has a seamless start and end.

Not affiliated with 360sona or Microsoft.
