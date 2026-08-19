"""Optional per-user install when running as a frozen exe."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path

APP_NAME = "Cursor Token Usage"
EXE_NAME = "CursorTokenUsage.exe"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "CursorTokenUsage"


def installed_exe() -> Path:
    return install_dir() / EXE_NAME


def current_exe() -> Path:
    return Path(sys.executable).resolve()


def is_running_from_install() -> bool:
    try:
        return current_exe() == installed_exe().resolve()
    except OSError:
        return False


def start_menu_dir() -> Path:
    return (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
    )


def startup_dir() -> Path:
    return start_menu_dir() / "Startup"


def _create_shortcut(
    lnk: Path,
    target: Path,
    arguments: str = "",
    working_dir: Path | None = None,
) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    target_s = str(target).replace("'", "''")
    work_s = str((working_dir or target.parent)).replace("'", "''")
    lnk_s = str(lnk).replace("'", "''")
    args_s = arguments.replace("'", "''")
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk_s}'); "
        f"$s.TargetPath = '{target_s}'; "
        f"$s.WorkingDirectory = '{work_s}'; "
        "$s.WindowStyle = 7; "
    )
    if args_s:
        script += f"$s.Arguments = '{args_s}'; "
    script += "$s.Save()"
    subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def project_root() -> Path:
    return Path(__file__).resolve().parent


def _pythonw_exe() -> Path:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return exe
    pyw = exe.with_name("pythonw.exe")
    if pyw.is_file():
        return pyw
    pyw_cmd = shutil.which("pyw")
    if pyw_cmd:
        return Path(pyw_cmd)
    return exe


def autostart_shortcut() -> Path:
    return startup_dir() / f"{APP_NAME}.lnk"


def ensure_autostart() -> None:
    """Register a per-user startup shortcut if it is not already present."""
    if "--no-autostart" in sys.argv:
        return

    lnk = autostart_shortcut()
    if lnk.is_file():
        return

    if is_frozen():
        target = installed_exe() if installed_exe().is_file() else current_exe()
        _create_shortcut(lnk, target)
        return

    script = project_root() / "cursor_token_tray.py"
    if not script.is_file():
        return
    _create_shortcut(
        lnk,
        _pythonw_exe(),
        arguments=f'-3 "{script}"',
        working_dir=project_root(),
    )


def disable_autostart() -> None:
    lnk = autostart_shortcut()
    if lnk.is_file():
        lnk.unlink()


def write_uninstaller(dest_dir: Path) -> None:
    start_lnk = start_menu_dir() / f"{APP_NAME}.lnk"
    startup_lnk = startup_dir() / f"{APP_NAME}.lnk"
    text = f"""@echo off
taskkill /IM {EXE_NAME} /F >nul 2>&1
del /f /q "{start_lnk}" >nul 2>&1
del /f /q "{startup_lnk}" >nul 2>&1
cd /d "%TEMP%"
rmdir /s /q "{dest_dir}"
"""
    (dest_dir / "Uninstall.bat").write_text(text, encoding="utf-8")


def install(start_menu: bool = True, autostart: bool = True) -> Path:
    dest_dir = install_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = installed_exe()
    shutil.copy2(current_exe(), dest)
    write_uninstaller(dest_dir)
    if start_menu:
        _create_shortcut(start_menu_dir() / f"{APP_NAME}.lnk", dest)
    else:
        lnk = start_menu_dir() / f"{APP_NAME}.lnk"
        if lnk.is_file():
            lnk.unlink()
    if autostart:
        _create_shortcut(startup_dir() / f"{APP_NAME}.lnk", dest)
    else:
        lnk = startup_dir() / f"{APP_NAME}.lnk"
        if lnk.is_file():
            lnk.unlink()
    return dest


def launch_installed_and_exit(exe: Path) -> None:
    subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        close_fds=True,
    )
    sys.exit(0)


def _offer_dialog() -> dict[str, object]:
    result = {"action": "run"}

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.configure(bg="#202020")

    frame = tk.Frame(root, bg="#202020", padx=24, pady=20)
    frame.pack()

    tk.Label(
        frame,
        text="Set up Cursor Token Usage",
        font=("Segoe UI", 14, "bold"),
        fg="#f3f3f3",
        bg="#202020",
    ).pack(anchor="w", pady=(0, 16))

    start_menu_var = tk.BooleanVar(value=True)
    autostart_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        frame,
        text="Show in Start menu",
        variable=start_menu_var,
        fg="#f3f3f3",
        bg="#202020",
        selectcolor="#2a2a2a",
        activebackground="#202020",
        activeforeground="#f3f3f3",
        font=("Segoe UI", 9),
    ).pack(anchor="w")
    tk.Checkbutton(
        frame,
        text="Start automatically when Windows signs in",
        variable=autostart_var,
        fg="#f3f3f3",
        bg="#202020",
        selectcolor="#2a2a2a",
        activebackground="#202020",
        activeforeground="#f3f3f3",
        font=("Segoe UI", 9),
    ).pack(anchor="w")

    buttons = tk.Frame(frame, bg="#202020")
    buttons.pack(fill="x", pady=(18, 0))

    def install_clicked() -> None:
        result["action"] = "install"
        result["start_menu"] = start_menu_var.get()
        result["autostart"] = autostart_var.get()
        root.destroy()

    def run_clicked() -> None:
        result["action"] = "run"
        root.destroy()

    tk.Button(
        buttons,
        text="Run once",
        command=run_clicked,
        font=("Segoe UI", 9),
        relief="flat",
        padx=12,
        pady=6,
    ).pack(side="right")
    tk.Button(
        buttons,
        text="Install",
        command=install_clicked,
        font=("Segoe UI", 9, "bold"),
        relief="flat",
        padx=14,
        pady=6,
        bg="#3b82f6",
        fg="#ffffff",
        activebackground="#2563eb",
        activeforeground="#ffffff",
    ).pack(side="right", padx=(0, 8))

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    return result


def maybe_install() -> None:
    if not is_frozen():
        return
    if "--portable" in sys.argv or "--skip-install" in sys.argv:
        return
    if is_running_from_install():
        return
    if "--install" in sys.argv:
        dest = install(
            start_menu="--no-start-menu" not in sys.argv,
            autostart="--no-autostart" not in sys.argv,
        )
        launch_installed_and_exit(dest)

    choice = _offer_dialog()
    if choice.get("action") == "install":
        dest = install(
            start_menu=bool(choice.get("start_menu", True)),
            autostart=bool(choice.get("autostart", True)),
        )
        launch_installed_and_exit(dest)
