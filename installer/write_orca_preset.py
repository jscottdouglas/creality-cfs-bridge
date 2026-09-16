#!/usr/bin/env python3
"""Write the printer preset that points the slicer's Device tab at the bridge.

The preset is a normal user machine preset in the slicer's own data directory:

    <appdata>/OrcaSlicerCFS/user/default/machine/Creality K2 Plus (CFS).json

It inherits the system preset the fork ships and overrides only the three
connection keys, so a profile update to the system preset still reaches it.

`written_by` marks the file as ours. Anything else in that place is left
alone, which covers both a preset the user edited by hand and one the slicer
itself has saved for them (the slicer keeps only keys it knows, so it drops
`written_by` when it writes the preset back out). Both cases mean "somebody
else owns this file now", and not touching it is the safe direction. So is
leaving a file we cannot read or make sense of: an installer that overwrites
what it does not understand is an installer that loses somebody's work.

`version` is not decoration: the slicer parses it when it loads a user preset
and silently skips the preset when it is missing or unparseable. The value
only has to be a version number; the slicer replaces it with its own the
first time it saves the preset.
"""
import argparse
import json
import os
import tempfile

NAME = "Creality K2 Plus (CFS)"
INHERITS = "Creality K2 Plus 0.4 nozzle"
APP_KEY = "OrcaSlicerCFS"
MARKER = "CrealityCFSBridge"
# Any parseable version number does; see the note in the module docstring.
VERSION = "1.0.0.0"


def write_preset(appdata_dir, host="127.0.0.1", port=7126):
    """Write the preset under `appdata_dir` and return its path.

    Returns the path whether or not it wrote anything, so the caller can
    report the file it means either way.
    """
    d = os.path.join(appdata_dir, APP_KEY, "user", "default", "machine")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, NAME + ".json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                existing = json.load(f)
            written_by = existing.get("written_by")
        except (ValueError, AttributeError, OSError):
            # Not json, not an object (`.get` on a list or a number), or not
            # readable at all. Whatever it is, it is not ours to replace.
            return p
        if written_by != MARKER:
            return p
    preset = {
        "type": "machine",
        "name": NAME,
        "from": "User",
        "inherits": INHERITS,
        "version": VERSION,
        "printer_model": "Creality K2 Plus",
        "printer_variant": "0.4",
        "printer_settings_id": NAME,
        "host_type": "crealitycfs",
        "print_host": "http://%s:%d" % (host, port),
        "print_host_webui": "http://%s:%d/fluidd/" % (host, port),
        "written_by": MARKER,
    }
    # Write beside the target and rename over it, so an interrupted run
    # cannot leave the slicer a half written preset to skip.
    fd, tmp = tempfile.mkstemp(dir=d, prefix=NAME + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(preset, f, indent=4)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Write the bridge printer preset.")
    ap.add_argument("--appdata", default=os.environ.get("APPDATA", ""),
                    help="the roaming application data directory "
                         "(defaults to APPDATA in the environment)")
    a = ap.parse_args()
    if not a.appdata:
        ap.error("no APPDATA in the environment; pass --appdata")
    print(write_preset(a.appdata))
