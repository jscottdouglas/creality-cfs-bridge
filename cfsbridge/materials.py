"""The printer's own filament database, as the slot editor needs it.

`{"method": "get", "params": {"reqMaterials": 1}}` answers with `retMaterials`,
a list of 103 entries on this printer, each of which is

    {"engineVersion": "3.0.0", "printerIntName": "F008",
     "nozzleDiameter": ["0.4"],
     "kvParam": { 91 slicer keys, including pressure_advance },
     "base": {"id": "00003", "brand": "Generic", "name": "Generic PETG",
              "meterialType": "PETG", "colors": ["#ffffff"],
              "minTemp": 220, "maxTemp": 270, "dryingTemp": 55, ...}}

(`meterialType` is spelt that way on the wire.) The whole reply is 388 KB, so
the bridge reads it once and keeps the handful of fields the editor uses. A
trimmed live copy was taken from a `retMaterials` reply; docs/PROTOCOL.md quotes it.

This is the same database the CrealityPrint slot dialog drives its three
dropdowns from, and it matters that it is: the dialog does not let the user
type a temperature. It picks brand, then material type, then name, and takes
`rfid`, `minTemp`, `maxTemp` and `pressure` from the entry that matched
(`kvParam.pressure_advance` for the last one). A slot edit that made those up
would write a material the printer has never heard of.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .slots import normalise_colour


@dataclass
class Filament:
    """One entry of the printer's filament database."""

    rfid: str = ""            # `base.id`, the Creality material id
    vendor: str = ""          # `base.brand`
    name: str = ""            # `base.name`
    material_type: str = ""   # `base.meterialType`
    min_temp: Optional[float] = None
    max_temp: Optional[float] = None
    pressure: float = 0.0     # `kvParam.pressure_advance`
    colours: list = None      # type: ignore[assignment]

    def as_json(self) -> dict:
        return {
            "rfid": self.rfid,
            "vendor": self.vendor,
            "name": self.name,
            "type": self.material_type,
            "min_temp": self.min_temp,
            "max_temp": self.max_temp,
            "pressure": self.pressure,
            "colours": list(self.colours or []),
        }


def _number(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_materials(reply: dict) -> list[Filament]:
    """Turn a `retMaterials` reply into a list of `Filament`.

    Accepts the whole reply (`{"retMaterials": [...]}`), the list on its own, or
    the trimmed capture shape (`{"materials": [...]}`). Anything malformed is
    skipped rather than raising, because a database the bridge cannot read must
    degrade to a free-text edit form, not to an error page.
    """
    entries = reply
    if isinstance(reply, dict):
        entries = reply.get("retMaterials")
        if entries is None:
            entries = reply.get("materials")
    if not isinstance(entries, list):
        return []

    out: list[Filament] = []
    seen: set = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        base = entry.get("base") if isinstance(entry.get("base"), dict) else entry
        pressure = entry.get("pressure_advance")
        if pressure is None and isinstance(entry.get("kvParam"), dict):
            pressure = entry["kvParam"].get("pressure_advance")
        vendor = (base.get("brand") or "").strip()
        name = (base.get("name") or "").strip()
        material_type = (base.get("meterialType") or base.get("type") or "").strip()
        if not (vendor and name and material_type):
            continue
        key = (vendor, material_type, name)
        if key in seen:
            continue
        seen.add(key)
        colours = []
        for colour in base.get("colors") or []:
            normalised = normalise_colour(colour)
            if normalised:
                colours.append(normalised)
        out.append(Filament(
            rfid=(base.get("id") or "").strip(),
            vendor=vendor,
            name=name,
            material_type=material_type,
            min_temp=_number(base.get("minTemp")),
            max_temp=_number(base.get("maxTemp")),
            pressure=_number(pressure) or 0.0,
            colours=colours,
        ))
    out.sort(key=lambda f: (f.vendor.lower(), f.material_type, f.name.lower()))
    return out


def find(items: list, vendor: str, material_type: str, name: str) -> Optional[Filament]:
    """The entry the edit dialog would have matched, or None.

    The lookup is the client's own: brand, material type and name all equal,
    which is what `he(brand, type, name)` does to find `pressure_advance`.
    """
    for item in items:
        if (item.vendor == vendor and item.material_type == material_type
                and item.name == name):
            return item
    return None


def as_json(items: list) -> dict:
    """The shape the card's edit form wants: the flat list plus its index.

    The form cascades vendor -> type -> name, so it needs to know, for each
    vendor, which types exist, and for each (vendor, type), which names.
    """
    vendors: dict = {}
    for item in items:
        types = vendors.setdefault(item.vendor, {})
        types.setdefault(item.material_type, []).append(item.name)
    return {
        "count": len(items),
        "vendors": sorted(vendors),
        "types": {v: sorted(t) for v, t in vendors.items()},
        "names": {v: {t: sorted(n) for t, n in types.items()}
                  for v, types in vendors.items()},
        "entries": [item.as_json() for item in items],
    }
