"""Where the bridge keeps its settings: %ProgramData%\\CrealityCFSBridge\\config.json
on Windows, ~/.cfsbridge/config.json elsewhere. Missing or unreadable means
defaults, and defaults mean 'no printer configured yet' (host empty).

CFSBRIDGE_CONFIG overrides the location entirely, which is what the installer
and the tests use to point the bridge at a scratch file.
"""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
import sys
from typing import Optional

ENV_VAR = "CFSBRIDGE_CONFIG"

DEFAULTS = {"host": "", "moonraker_port": 7125, "port": 7126, "live": True}


def config_path(platform: Optional[str] = None, programdata: Optional[str] = None,
                home: Optional[str] = None) -> str:
    override = os.environ.get(ENV_VAR)
    if override:
        return override
    platform = platform or sys.platform
    # Join with the rules of the *target* platform, not the running one, so the
    # Windows branch still reads C:\ProgramData\... when exercised from Linux.
    if platform.startswith("win"):
        base = programdata or os.environ.get("ProgramData", r"C:\ProgramData")
        return ntpath.join(base, "CrealityCFSBridge", "config.json")
    return posixpath.join(home or os.path.expanduser("~"), ".cfsbridge", "config.json")


def load_config(path: Optional[str] = None) -> dict:
    path = path or config_path()
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULTS:
                if k in data and isinstance(data[k], type(DEFAULTS[k])):
                    cfg[k] = data[k]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict, path: Optional[str] = None) -> str:
    path = path or config_path()
    merged = load_config(path)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, path)
    return path
