"""CFS slot model and the label arithmetic the K2 box module uses.

Slot naming, all verified against the live printer (docs/PROTOCOL.md section 4):

  physical slot   "2B"    unit 2, slot B
  box module name "T2B"   the same slot, as the Klipper box module names it
  global index    5       0 based, used by `M8200 L I<n>` and by a bare `T<n>`

  index = (unit - 1) * 4 + slot      unit 1..4, slot 0..3 (A..D)

A G-code `T<n>` therefore asks for the slot at global index n, which is why an
Orca single filament job emitting `T0` makes the printer load slot 1A.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SLOT_LETTERS = "ABCD"
SLOTS_PER_UNIT = 4
MAX_UNITS = 4


class SlotError(ValueError):
    """A slot label could not be parsed or is out of range."""


def parse_slot(label: str) -> tuple[int, int]:
    """'2B' or 'T2B' or '2b' -> (unit, slot_index). Raises SlotError."""
    text = label.strip().upper()
    if text.startswith("T"):
        text = text[1:]
    if len(text) != 2 or not text[0].isdigit() or text[1] not in SLOT_LETTERS:
        raise SlotError(
            "bad slot %r: expected a unit digit 1-%d then a slot letter A-D, "
            "for example 2B" % (label, MAX_UNITS)
        )
    unit = int(text[0])
    if not 1 <= unit <= MAX_UNITS:
        raise SlotError("bad slot %r: unit must be 1..%d" % (label, MAX_UNITS))
    return unit, SLOT_LETTERS.index(text[1])


def slot_label(unit: int, slot: int) -> str:
    """(2, 1) -> '2B'."""
    return "%d%s" % (unit, SLOT_LETTERS[slot])


def tnn(unit: int, slot: int) -> str:
    """(2, 1) -> 'T2B', the name the box module uses."""
    return "T" + slot_label(unit, slot)


def slot_to_index(unit: int, slot: int) -> int:
    """(2, 1) -> 5, the value for `M8200 L I<n>` and for a bare `T<n>`."""
    return (unit - 1) * SLOTS_PER_UNIT + slot


def index_to_slot(index: int) -> tuple[int, int]:
    """5 -> (2, 1). Mirrors the printer's own M8200 macro arithmetic."""
    if not 0 <= index < MAX_UNITS * SLOTS_PER_UNIT:
        raise SlotError("tool index %d is outside 0..%d" % (index, MAX_UNITS * SLOTS_PER_UNIT - 1))
    return index // SLOTS_PER_UNIT + 1, index % SLOTS_PER_UNIT


def index_to_tnn(index: int) -> str:
    """5 -> 'T2B'."""
    return tnn(*index_to_slot(index))


def normalise_colour(value: Optional[str]) -> Optional[str]:
    """Creality colours are '#0RRGGBB' (seven hex digits). Return '#RRGGBB'.

    Returns None for the empty string, 'unknown' and '-1', all of which the
    printer uses for "no value".
    """
    if not value:
        return None
    text = value.strip()
    if text.lower() in ("unknown", "-1", "none", ""):
        return None
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 7:
        # The client inserts an extra leading 0 when it writes a colour, so the
        # printer reports seven digits. Drop it.
        text = text[1:]
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return "#" + text.upper()


def _optional_float(value) -> Optional[float]:
    """A number the printer may simply not have sent. Never raises."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def colour_distance(a: Optional[str], b: Optional[str]) -> float:
    """Squared RGB distance between two '#RRGGBB' strings.

    Returns a large finite number when either side is unknown so that a slot
    with no colour still loses to any slot with one, without being excluded.
    """
    ca, cb = normalise_colour(a), normalise_colour(b)
    if ca is None or cb is None:
        return 1e6
    pa = [int(ca[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(cb[i:i + 2], 16) for i in (1, 3, 5)]
    return float(sum((x - y) ** 2 for x, y in zip(pa, pb)))


@dataclass
class Slot:
    """One CFS slot, merged from the Creality protocol and Moonraker."""

    unit: int
    slot: int
    material_type: str = ""          # "PLA", "PETG", ... "" when empty
    colour: Optional[str] = None     # "#RRGGBB"
    vendor: str = ""
    name: str = ""
    rfid: str = ""                   # Creality material id, e.g. "P1003", "00001"
    percent: int = 0
    min_temp: Optional[float] = None  # the slot's own limits, straight from boxsInfo
    max_temp: Optional[float] = None
    edit_status: int = 0             # 0 empty, 1 set (tag read or user edit), 2 the EXT box
    remain_len: Optional[float] = None   # metres, Moonraker only
    loaded: bool = False             # the printer reports this slot as selected
    mapped: bool = False             # a gcode slot label currently resolves here
    present: bool = False            # a spool is in this slot
    edited: bool = False             # the type came from a manual edit, not an RFID tag
    raw_moonraker_type: Optional[str] = None

    @property
    def label(self) -> str:
        return slot_label(self.unit, self.slot)

    @property
    def tnn(self) -> str:
        return tnn(self.unit, self.slot)

    @property
    def index(self) -> int:
        return slot_to_index(self.unit, self.slot)


# `materialBoxs[].materialBoxName` is the hardware id. The table is
# `CFS_NAME` in CrealityPrint's device manager bundle; see docs/PROTOCOL.md
# section 2.2. Only the CFS-2 family (Pro and C) carries a dryer.
CFS_MODELS = {
    "MF003": "CFS",
    "MF040": "CFS Lite",
    "MF046": "CFS Mini",
    "MF049": "CFS Nano",
    "MF042": "CFS Pro",
    "MF050": "CFS-C",
    "MF054": "CFS Nano2",
}
# The variant whose slot edit carries `boxType` (`SetCfsMiniMaterials`).
CFS_MINI = "MF046"
# The variants with a drying cabinet. The captured traffic seen so far does not
# prove this list on hardware; it is the set the drying page is reachable for.
CFS_DRYING_MODELS = {"MF042", "MF050"}


@dataclass
class UnitInfo:
    """One CFS unit's own state, as `boxsInfo.materialBoxs[]` reports it."""

    unit: int
    model: str = ""                  # "MF003"
    model_name: str = ""             # "CFS"
    box_type: int = 0                # 0 CFS, 1 the external spool holder, 2 CFS Mini
    present: bool = False            # `state == 1`, the unit is on the bus
    temp: Optional[float] = None     # chamber temperature, C
    humidity: Optional[float] = None  # relative humidity, percent
    serial: str = ""
    ac: Optional[int] = None         # mains present. The drying page refuses on 0

    @property
    def serial_short(self) -> str:
        """The last six characters, which is what tells two units apart."""
        return self.serial[-6:] if self.serial else ""

    @property
    def is_mini(self) -> bool:
        return self.model == CFS_MINI

    @property
    def can_dry(self) -> bool:
        return self.model in CFS_DRYING_MODELS


@dataclass
class SlotTable:
    slots: list[Slot] = field(default_factory=list)
    units_present: set[int] = field(default_factory=set)
    # The box module's gcode-slot to physical-slot indirection, exactly as
    # Moonraker reports it: {"T1A": "T2B", "T1B": "T1B", ...}. A `colorMatch`
    # message is what rewrites it. See docs/PROTOCOL.md section 4.2.
    gcode_map: dict = field(default_factory=dict)
    # unit number -> UnitInfo, for the units `boxsInfo` actually listed.
    units: dict = field(default_factory=dict)

    def unit_info(self, unit: int) -> Optional[UnitInfo]:
        return self.units.get(int(unit))

    def resolves_to(self, tool_index: int) -> Optional[str]:
        """Which physical slot a bare `T<tool_index>` would load today."""
        label = index_to_tnn(tool_index)
        target = self.gcode_map.get(label, label)
        return target[1:] if target.startswith("T") else target

    @property
    def map_is_identity(self) -> bool:
        return all(k == v for k, v in self.gcode_map.items())

    def get(self, unit: int, slot: int) -> Optional[Slot]:
        for s in self.slots:
            if s.unit == unit and s.slot == slot:
                return s
        return None

    def by_label(self, label: str) -> Optional[Slot]:
        return self.get(*parse_slot(label))

    def filled(self) -> list[Slot]:
        return [s for s in self.slots if s.present and s.material_type]


def build_slot_table(boxs_info: dict, moonraker_box: Optional[dict] = None) -> SlotTable:
    """Merge a `boxsInfo` reply and a Moonraker `box` object into a SlotTable.

    `boxsInfo` is authoritative for type, vendor, name and colour, because it
    reflects manual slot edits made on the touchscreen. Moonraker is the only
    source of `remain_len`, and its `material_type` is the raw RFID reading, so
    a mismatch between the two marks the slot as manually edited.
    See docs/PROTOCOL.md section 2.3.
    """
    table = SlotTable()
    info = boxs_info.get("boxsInfo", boxs_info) or {}

    for box in info.get("materialBoxs", []) or []:
        box_id = int(box.get("id", -1))
        # id 0 is the external spool holder, not a CFS unit.
        if not 1 <= box_id <= MAX_UNITS:
            continue
        if int(box.get("state", 0)) != 0:
            table.units_present.add(box_id)
        model = (box.get("materialBoxName") or "").strip()
        table.units[box_id] = UnitInfo(
            unit=box_id,
            model=model,
            model_name=CFS_MODELS.get(model, model or "CFS"),
            box_type=int(box.get("type") or 0),
            present=int(box.get("state", 0)) != 0,
            temp=_optional_float(box.get("temp")),
            humidity=_optional_float(box.get("humidity")),
            serial=(box.get("sn") or "").strip(),
            ac=int(box["ac"]) if str(box.get("ac", "")).strip() not in ("", "None") else None,
        )
        for mat in box.get("materials", []) or []:
            mid = int(mat.get("id", -1))
            if not 0 <= mid < SLOTS_PER_UNIT:
                continue
            table.slots.append(
                Slot(
                    unit=box_id,
                    slot=mid,
                    material_type=(mat.get("type") or "").strip(),
                    colour=normalise_colour(mat.get("color")),
                    vendor=(mat.get("vendor") or "").strip(),
                    name=(mat.get("name") or "").strip(),
                    rfid=(mat.get("rfid") or "").strip(),
                    percent=int(mat.get("percent") or 0),
                    min_temp=_optional_float(mat.get("minTemp")),
                    max_temp=_optional_float(mat.get("maxTemp")),
                    edit_status=int(mat.get("editStatus") or 0),
                    loaded=bool(int(mat.get("selected") or 0)),
                    # `state` is 0 for an empty slot and non-zero for a slot
                    # with filament in it. It is not a boolean: a slot that is
                    # currently feeding the toolhead reports 2, observed live
                    # on 2026-09-13 while slot 2B was printing. The client's
                    # own test is `Number(state) !== 0`, never `=== 1`.
                    present=int(_optional_float(mat.get("state")) or 0) != 0,
                )
            )

    # colorMatch says which gcode slot label is currently served by which
    # physical slot. That is a mapping, not a load, so it gets its own flag.
    for entry in info.get("colorMatch", []) or []:
        slot = table.get(int(entry.get("boxId", -1)), int(entry.get("materialId", -1)))
        if slot is not None:
            slot.mapped = True

    if moonraker_box:
        box = moonraker_box
        # Accept either the raw query result or the inner object.
        box = box.get("result", {}).get("status", {}).get("box", box) if "result" in box else box
        box = box.get("box", box)
        raw_map = box.get("map")
        if isinstance(raw_map, dict):
            table.gcode_map = dict(raw_map)
        for unit in range(1, MAX_UNITS + 1):
            unit_data = box.get("T%d" % unit)
            if not isinstance(unit_data, dict):
                continue
            remains = unit_data.get("remain_len") or []
            types = unit_data.get("material_type") or []
            for mid in range(SLOTS_PER_UNIT):
                slot = table.get(unit, mid)
                if slot is None:
                    continue
                if mid < len(remains):
                    try:
                        value = float(remains[mid])
                        slot.remain_len = value if value >= 0 else None
                    except (TypeError, ValueError):
                        slot.remain_len = None
                if mid < len(types):
                    raw = str(types[mid])
                    slot.raw_moonraker_type = raw
                    # "0" + rfid for a tagged spool, "unknown" for an untagged
                    # one, "-1" for an empty slot.
                    if raw == "unknown" and slot.material_type:
                        slot.edited = True

    table.slots.sort(key=lambda s: (s.unit, s.slot))
    return table
