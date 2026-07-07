from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def current_env_prefix() -> Path:
    return Path(sys.executable).resolve().parent


def ensure_windows_cairo_runtime() -> None:
    """Make CairoSVG usable when conda env python.exe is called directly."""
    if os.name != "nt":
        return
    env_root = current_env_prefix()
    dll_dir = env_root / "Library" / "bin"
    if not dll_dir.exists():
        return
    dll_dir_text = str(dll_dir)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if dll_dir_text not in path_parts:
        os.environ["PATH"] = dll_dir_text + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(dll_dir_text)
    except (AttributeError, FileNotFoundError, OSError):
        pass
    cairo_dll = dll_dir / "cairo-2.dll"
    if cairo_dll.exists():
        ctypes.CDLL(str(cairo_dll))
