# The printer preset

One file decides whether the slicer's Print button goes straight to the printer or
through the bridge. The installer writes it for you:

    %APPDATA%\OrcaSlicerCFS\user\default\machine\Creality K2 Plus (CFS).json

`%APPDATA%` expands to your own user profile's `AppData\Roaming` directory, and
`OrcaSlicerCFS` is this fork's data directory, separate from a stock OrcaSlicer
install alongside it.

The preset is called **Creality K2 Plus (CFS)**. It appears in the printer list next
to the system preset it inherits, so nothing you already had is replaced or renamed.
Selecting it points the Device tab and the Print button at the bridge on
`127.0.0.1:7126`.

## What the file contains

```json
{
    "type": "machine",
    "name": "Creality K2 Plus (CFS)",
    "from": "User",
    "inherits": "Creality K2 Plus 0.4 nozzle",
    "version": "1.0.0.0",
    "printer_model": "Creality K2 Plus",
    "printer_variant": "0.4",
    "printer_settings_id": "Creality K2 Plus (CFS)",
    "host_type": "crealitycfs",
    "print_host": "http://127.0.0.1:7126",
    "print_host_webui": "http://127.0.0.1:7126/fluidd/",
    "written_by": "CrealityCFSBridge"
}
```

Three keys do the work:

| Key | Value | What it does |
| --- | --- | --- |
| `host_type` | `crealitycfs` | Selects this fork's CFS print host, which is what puts the slot table and the colour match in the upload dialog. |
| `print_host` | `http://127.0.0.1:7126` | Where the slicer uploads and presses Print. The bridge forwards it to the printer. |
| `print_host_webui` | `http://127.0.0.1:7126/fluidd/` | What the Device tab renders: the bridge's Fluidd page, with the CFS card on it. |

Everything else about the printer comes from `inherits`, so a profile update to the
system preset still reaches this one. `printhost_apikey` and the other print host
credential fields stay unset; the bridge does not use them.

Four keys are bookkeeping rather than settings, and two of them are not optional:

- `version` has to be there and has to parse as a version number. When the slicer
  loads user presets (`PresetCollection::load_presets` in `libslic3r/Preset.cpp`) it
  parses this key and **skips the whole preset** if it cannot, with no error and no
  dialog: the preset simply never shows up in the printer list. The value itself does
  not matter, and the slicer replaces it with its own version the first time it saves
  the preset.
- `printer_settings_id` names the preset to itself. Every preset the slicer saves
  carries it, and the slicer's own json import uses it to recognise a printer preset.
- `type`, `printer_model` and `printer_variant` say what kind of preset this is and
  which machine it belongs to.
- `written_by` is the marker described below.

## Running it by hand

The Windows installer ships this as a command of its own, and running it is exactly
what the installer does:

    cfsbridge.exe preset --appdata %APPDATA%

From a source checkout, where there is no built executable, run the script:

    python installer\write_orca_preset.py --appdata %APPDATA%

It prints the path it wrote. With no `--appdata` it uses `APPDATA` from the
environment, and it stops rather than guessing if that is not set. Running it twice
is safe.

The slicer reads user presets when it starts, so a preset written while the slicer is
open appears the next time you start it. Nothing is lost by writing it first and
restarting.

## A bridge on another host or port

The defaults assume the bridge runs on the same PC as the slicer, which is the normal
install. If the bridge runs somewhere else, or on a different port, call the writer
directly:

```python
import os

from write_orca_preset import write_preset

write_preset(os.environ["APPDATA"], host="192.168.1.60", port=7126)
```

`host` and `port` are only ever the **bridge's** address, never the printer's. The
printer's address belongs in the bridge's own configuration
(`%ProgramData%\CrealityCFSBridge\config.json`), which is where the setup page puts
it. So a bridge running on another PC at `192.168.1.60` gives
`print_host` `http://192.168.1.60:7126` and Device UI
`http://192.168.1.60:7126/fluidd/`, while that bridge's config still names the
printer's own address, `192.168.1.50`.

You can equally do it in the slicer: **Printer settings, Connection**, set Host Type
to `Creality CFS (K2)` and type the bridge's address into Hostname, IP or URL and
Device UI, then press the save icon next to the preset name.

## The overwrite guard

`written_by` marks the file as the installer's. On every run the writer reads any
existing preset of that name first and leaves it alone unless it finds that marker, so
a reinstall or an upgrade never overwrites a preset you have changed.

The marker does not survive your own save, and that is deliberate. When you save this
preset in the slicer, the slicer writes out the keys it knows about, and `written_by`
is not one of them, so the marker is gone from that point on and the writer treats the
preset as yours. Simply starting and closing the slicer does not do this: it saves a
user preset when you save it, not on exit. The consequence worth knowing: once you
have saved the preset yourself, a later run of the writer will not update it for you,
so if the bridge moves to another port after that you set the new address in the
slicer as above.

A file of that name that the writer cannot read, or that is not a json object, is left
exactly as it is. An installer has no business overwriting a file it does not
understand.

## Using the slicer's built-in camera page instead

The bridge preset always shows the bridge's Fluidd in the Device tab. The camera page
this fork can generate itself is a different thing: it is built from the address in
`print_host`, so it only makes sense when `print_host` is the printer's own address.
Clearing the Device UI field on the bridge preset would aim that page at this PC,
where there is no printer camera.

To have both, make a second printer preset alongside the bridge one: in **Printer
settings, Connection** set Hostname, IP or URL to the printer's own address
(`http://192.168.1.50`), leave Device UI **empty**, and save it under a name of its
own. Selecting that preset gives you the camera page; selecting
**Creality K2 Plus (CFS)** gives you the bridge's Fluidd with the CFS card.
