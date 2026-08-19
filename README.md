# Cursor Token Tray

Taskbar display for live Cursor token billing from the official dashboard API.

Inspired by the [Cursor Token Usage](https://open-vsx.org/extension/akitogo/cursor-token-usage) VS Code extension. This app works outside Cursor (including Agent/Glass layout) and shows **readable text** in the taskbar area.

## Features

- Polls `https://cursor.com/api/usage-summary` every **5 minutes**
- Reads your local Cursor session from `state.vscdb` (no manual token setup if you're signed in)
- Taskbar display: `Cursor 35%   Other 83%` (readable width, like weather/clock widgets)
- Color-coded by usage level (blue → yellow → red)
- **Windows 11:** uses a docked taskbar widget automatically (native deskbands are removed)
- **Windows 10:** can use the optional deskband toolbar (see below)

## Für andere Nutzer (eine Datei)

1. `packaging\build.bat` ausführen
2. `dist\CursorTokenUsage.exe` weitergeben (USB, Chat, GitHub Release)

Andere brauchen **kein Python**. Beim ersten Start erscheint ein kleiner Dialog:

- **Installieren** — kopiert nach `%LOCALAPPDATA%\CursorTokenUsage`, optional Startmenü + Autostart
- **Nur starten** — einmalig, ohne Installation

Deinstallieren: `%LOCALAPPDATA%\CursorTokenUsage\Uninstall.bat`

## Entwicklung

```bat
git clone https://github.com/janmartenkrull-create/cursor-token-tray.git
cd cursor-token-tray
pyw -3 cursor_token_tray.py
```

Oder `start-tray.bat`. Python 3.10+ mit tkinter, Cursor muss auf dem PC angemeldet sein.

### Optional: deskband toolbar (Windows 10 only)

On Windows 10 you can register a native toolbar instead:

1. Run `register-deskband.bat` as admin
2. Right-click taskbar → **Toolbars** → **Cursor Token Usage**
3. Start the app as above

> **Note:** Windows 11 removed classic taskbar toolbars. The deskband DLL can be registered, but Explorer will not load it without third-party tools like [ExplorerPatcher](https://github.com/valinet/ExplorerPatcher).

## Autostart

Create a shortcut to `start-tray.bat` in:

```
shell:startup
```

## How it works

1. A small COM DLL (`pydeskband/dlls/PyDeskband_x64.dll`) draws text on the taskbar
2. Python polls the Cursor API and updates the display via a named pipe
3. Session data stays local; requests go only to `cursor.com`

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

MIT — includes [PyDeskband](https://github.com/kylebenz/PyDeskband) (MIT) for the taskbar toolbar.
