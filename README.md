# Cursor Token Tray

Taskbar display for live Cursor token billing from the official dashboard API.

Inspired by the [Cursor Token Usage](https://open-vsx.org/extension/akitogo/cursor-token-usage) VS Code extension. This app works outside Cursor (including Agent/Glass layout) and shows **readable text** in the taskbar area.

## Features

- Polls `https://cursor.com/api/usage-summary` every **5 minutes**
- Reads your local Cursor session from `state.vscdb` (no manual token setup if you're signed in)
- Taskbar display: `Cursor 35%   Other 83%` (readable width, like weather/clock widgets)
- Color-coded by usage level (gray → orange → red)
- **Windows 11:** uses a docked taskbar widget automatically (native deskbands are removed)
- **Windows 10:** can use the optional deskband toolbar (see below)

## Install

Download `CursorTokenUsage.exe` from the [latest release](https://github.com/janmartenkrull-create/cursor-token-tray/releases/latest), run it, and choose **Install** (or **Run once**).

Requires Windows 10/11 (64-bit) and Cursor signed in on this PC.

## Development

```bat
git clone https://github.com/janmartenkrull-create/cursor-token-tray.git
cd cursor-token-tray
pyw -3 cursor_token_tray.py
```

Or double-click `start-tray.bat`. Requires Python 3.10+ with tkinter.

Build the portable exe:

```bat
packaging\build.bat
```

### Optional: deskband toolbar (Windows 10 only)

On Windows 10 you can register a native toolbar instead:

1. Run `register-deskband.bat` as admin
2. Right-click taskbar → **Toolbars** → **Cursor Token Usage**
3. Start the app as above

> **Note:** Windows 11 removed classic taskbar toolbars. The deskband DLL can be registered, but Explorer will not load it without third-party tools like [ExplorerPatcher](https://github.com/valinet/ExplorerPatcher).

## Autostart

The app creates a shortcut in the Windows startup folder (`shell:startup`) on first run if one does not already exist. It will start automatically on the next sign-in.

To disable autostart, delete the **Cursor Token Usage** shortcut from the startup folder, or launch the app with `--no-autostart`.

## How it works

1. On Windows 11, a small taskbar widget shows usage next to the system tray
2. On Windows 10, an optional COM DLL (`pydeskband/dlls/PyDeskband_x64.dll`) can draw text on the taskbar
3. The app polls the Cursor API and reads your local session from `state.vscdb`
4. Session data stays local; requests go only to `cursor.com`

## Building the DLL (optional)

If you change the C++ sources under `vendor/PyDeskband/`:

```bat
"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe" vendor\PyDeskband\dll\PyDeskband\PyDeskband.sln /p:Configuration=Release /p:Platform=x64
copy vendor\PyDeskband\dll\PyDeskband\x64\Release\PyDeskband.dll pydeskband\dlls\PyDeskband_x64.dll
```

Then run `register-deskband.bat` again.

## Privacy

- Session data stays on your machine
- API requests go only to `cursor.com`
- No telemetry

## License

MIT — see [LICENSE](LICENSE).

Includes [PyDeskband](https://github.com/kylebenz/PyDeskband) (MIT) for the optional Windows 10 taskbar toolbar.
