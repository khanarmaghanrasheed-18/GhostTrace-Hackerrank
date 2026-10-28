"""The GhostTrace version, and the free command-line tools it uses to read PDFs and images."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any

VERSION = "1.0.0"

TOOLS = [
    {"name": "Tesseract", "command": "tesseract", "needed_for": "screenshots, photos and scanned PDFs"},
    {"name": "Poppler pdftotext", "command": "pdftotext", "needed_for": "PDF documents"},
    {"name": "Poppler pdftoppm", "command": "pdftoppm", "needed_for": "scanned PDFs"},
]
INSTALL = {
    "Windows": ["winget install --id UB-Mannheim.TesseractOCR -e", "winget install --id oschwartz10612.Poppler -e",
                "Then add the folders that contain tesseract.exe and pdftotext.exe to PATH, and open a new terminal."],
    "Darwin": ["brew install tesseract poppler"],
    "Linux": ["sudo apt-get install -y tesseract-ocr poppler-utils   (Debian, Ubuntu)",
              "sudo dnf install -y tesseract poppler-utils           (Fedora)"],
}


def status() -> list[dict[str, Any]]:
    """Which tools are on this computer, and where."""
    return [{**tool, "found": bool(shutil.which(tool["command"])), "path": shutil.which(tool["command"]) or ""}
            for tool in TOOLS]


def install_hint() -> list[str]:
    return INSTALL.get(platform.system(), INSTALL["Linux"])


def doctor() -> int:
    """Print what this computer has and what is missing. Returns 0 when everything is ready."""
    print(f"GhostTrace {VERSION}")
    ok = sys.version_info >= (3, 10)
    print(f"  {'OK ' if ok else 'NO '} Python {platform.python_version()} ({sys.executable})"
          + ("" if ok else " - GhostTrace needs Python 3.10 or newer"))
    missing = []
    for tool in status():
        version = ""
        if tool["found"]:
            try:
                output = subprocess.run([tool["command"], "-v" if "pdf" in tool["command"] else "--version"],
                                        capture_output=True, text=True, timeout=20)
                version = (output.stdout or output.stderr).strip().splitlines()[0] if (output.stdout or output.stderr) else ""
            except (OSError, subprocess.SubprocessError):
                version = ""
        print(f"  {'OK ' if tool['found'] else 'NO '} {tool['name']}: "
              + (f"{tool['path']} {version}".strip() if tool["found"] else f"not found; needed for {tool['needed_for']}"))
        if not tool["found"]:
            missing.append(tool)
    if missing:
        print("\nEverything else works without them. To read those files, install:")
        for line in install_hint():
            print(f"  {line}")
    else:
        print("\nEverything is ready.")
    return 0 if ok and not missing else 1
