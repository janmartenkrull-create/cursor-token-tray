# Cursor Token Tray

Windows system-tray app that shows live Cursor token billing from the official dashboard API.

Inspired by the [Cursor Token Usage](https://open-vsx.org/extension/akitogo/cursor-token-usage) VS Code extension. This tray app works in Cursor's Agent/Glass layout because it runs outside the editor.

## Features

- Polls `https://cursor.com/api/usage-summary` every **5 minutes**
- Reads your local Cursor session from `state.vscdb` (no manual token setup if you're signed in)
- Tray icon with short label, e.g. `C35 O82` or `$12.40/$20.00`
- Color-coded by usage level (green → yellow → orange → red)
- Tooltip with details; right-click menu: refresh / quit

## Requirements

- Windows 10/11
- Python 3.10+
- Cursor signed in on this PC

## Install

```bat
git clone https://github.com/janmartenkrull-create/cursor-token-tray.git
cd cursor-token-tray
py -3 -m pip install -r requirements.txt
```

## Run

Double-click `start-tray.bat`, or:

```bat
pyw -3 cursor_token_tray.py
```

The icon appears in the system tray (near the clock). Pin it via the `^` overflow menu if needed.

## Autostart

Create a shortcut to `start-tray.bat` in:

```
shell:startup
```

## How it works

1. Reads `cursorAuth/accessToken` and user id from `%APPDATA%\Cursor\User\globalStorage\state.vscdb`
2. Builds the `WorkosCursorSessionToken` cookie (same as the official extension)
3. Fetches usage from Cursor's dashboard API only — no local token estimates

## Privacy

- Session data stays on your machine
- API requests go only to `cursor.com`
- No telemetry

## License

MIT
