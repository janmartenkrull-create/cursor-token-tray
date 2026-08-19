#!/usr/bin/env python3
from pathlib import Path
import subprocess

out = Path(__file__).resolve().parent
png = out / "icon.png"
ico = out / "icon.ico"
font = "C:/Windows/Fonts/segoeuib.ttf"
subprocess.check_call(
    [
        "magick",
        "-size",
        "256x256",
        "xc:none",
        "-fill",
        "#1f2937",
        "-draw",
        "roundrectangle 20,20 236,236 48,48",
        "-fill",
        "#7dd3fc",
        "-font",
        font,
        "-pointsize",
        "128",
        "-gravity",
        "center",
        "-annotate",
        "+0+10",
        "C",
        str(png),
    ]
)
subprocess.check_call(
    [
        "magick",
        str(png),
        "-define",
        "icon:auto-resize=256,128,64,48,32,16",
        str(ico),
    ]
)
print(png, png.stat().st_size)
print(ico, ico.stat().st_size)
