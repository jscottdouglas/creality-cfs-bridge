"""Decide which CFS slot serves which G-code tool.

Two ways in:

  explicit   `--map T0=2B --map T1=1C`
  automatic  match each extruder's filament to a slot by Creality material id,
             then by type, then by nearest colour

The result is a list of Assignment records that both the `colormatch` protocol
method and the `gcode` rewrite method can consume.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .gcode import Extruder, GcodeInfo
from .slots import (
    Slot,
    SlotError,
    SlotTable,
    colour_distance,
    index_to_tnn,
    normalise_colour,
    parse_slot,
)


class MappingError(ValueError):
    """No usable slot, or a mapping the printer would reject."""


@dataclass
class Assignment:
    tool: int                # the G-code tool number, T<tool>
    slot: Slot               # the physical slot that will serve it
    reason: str = ""         # why the auto mapper picked it
    extruder: Optional[Extruder] = None

    @property
    def gcode_tnn(self) -> str:
        """The slot label the box module resolves this tool to today."""
        return index_to_tnn(self.tool)

    @property
    def target_index(self) -> int:
        """The tool number that would reach this slot with no remapping."""
        return self.slot.index

    def color_match_entry(self) -> dict:
        """One entry of the protocol `colorMatch` list."""
        return {
            "id": self.gcode_tnn,
            "type": self.slot.material_type or (
                self.extruder.filament_type if self.extruder else ""
            ),
            "color": self.slot.colour or "#000000",
            "boxId": self.slot.unit,
            "materialId": self.slot.slot,
        }


def parse_explicit(specs: list[str], table: SlotTable) -> list[Assignment]:
    """Parse `T0=2B` style arguments against the live slot table."""
    out: list[Assignment] = []
    used_tools: set[int] = set()
    for spec in specs:
        if "=" not in spec:
            raise MappingError("bad --map %r: expected TOOL=SLOT, for example T0=2B" % spec)
        left, right = spec.split("=", 1)
        left = left.strip().upper()
        if not left.startswith("T") or not left[1:].isdigit():
            raise MappingError("bad --map %r: the left side must be T0, T1, ..." % spec)
        tool = int(left[1:])
        if tool in used_tools:
            raise MappingError("tool T%d is mapped twice" % tool)
        used_tools.add(tool)
        try:
            unit, index = parse_slot(right)
        except SlotError as exc:
            raise MappingError("bad --map %r: %s" % (spec, exc)) from exc
        slot = table.get(unit, index)
        if slot is None:
            raise MappingError(
                "slot %s does not exist on this printer; units seen: %s"
                % (right.strip(), ", ".join(str(u) for u in sorted(table.units_present)) or "none")
            )
        if not slot.present or not slot.material_type:
            raise MappingError(
                "slot %s is empty. Load it, or set its material on the touchscreen, "
                "then try again." % slot.label
            )
        out.append(Assignment(tool=tool, slot=slot, reason="requested with --map"))
    out.sort(key=lambda a: a.tool)
    return out


def _score(extruder: Extruder, slot: Slot) -> tuple[int, float, int, str]:
    """Lower is better. Returns (tier, colour distance, id tiebreak, reason).

    Type is the hard gate: a PETG job never lands in a PLA slot unless the user
    passes --allow-type-mismatch. Within the compatible slots colour decides,
    because a Creality material id like 00001 ("Generic PLA") is shared by every
    generic spool and says nothing about which one the user meant. The id only
    breaks ties when the colours are equally close or absent.
    """
    want_type = (extruder.filament_type or "").strip().upper()
    have_type = (slot.material_type or "").strip().upper()
    distance = colour_distance(extruder.colour, slot.colour)
    same_id = bool(
        extruder.filament_id
        and slot.rfid
        and extruder.filament_id.lstrip("0") == slot.rfid.lstrip("0")
    )
    id_rank = 0 if same_id else 1

    if want_type and have_type and want_type == have_type:
        if normalise_colour(extruder.colour) and normalise_colour(slot.colour):
            reason = "type %s, colour distance %.0f" % (slot.material_type, distance)
        else:
            reason = "type %s, no colour to compare" % slot.material_type
        if same_id:
            reason += ", material id %s" % slot.rfid
        return (0, distance, id_rank, reason)
    if same_id:
        return (1, distance, id_rank, "same Creality material id %s" % slot.rfid)
    return (9, distance, id_rank, "type mismatch")


def auto_map(info: GcodeInfo, table: SlotTable, allow_type_mismatch: bool = False) -> list[Assignment]:
    """Pick a slot per tool: material id first, then type, then nearest colour.

    Raises MappingError naming the missing type when no slot can serve a tool.
    A slot is never assigned to two tools.
    """
    candidates = table.filled()
    if not candidates:
        raise MappingError(
            "no CFS slot has any filament in it. Load a spool, or set the slot "
            "material on the touchscreen, then try again."
        )

    assignments: list[Assignment] = []
    taken: set[str] = set()

    for tool in info.tools_used:
        extruder = info.extruders[tool] if tool < len(info.extruders) else Extruder(index=tool)
        scored = []
        for slot in candidates:
            if slot.label in taken:
                continue
            tier, distance, id_rank, reason = _score(extruder, slot)
            if tier == 9 and not allow_type_mismatch:
                continue
            scored.append((tier, distance, id_rank, slot, reason))
        if not scored:
            wanted = extruder.filament_type or "(unknown type)"
            available = ", ".join(
                "%s %s" % (s.label, s.material_type or "empty") for s in candidates
            ) or "none"
            raise MappingError(
                "T%d needs %s and no free CFS slot holds it. Slots available: %s. "
                "Load %s into a slot, or give an explicit --map T%d=<slot>, or pass "
                "--allow-type-mismatch if you really mean it."
                % (tool, wanted, available, wanted, tool)
            )
        scored.sort(key=lambda item: (item[0], item[1], item[2], item[3].label))
        tier, distance, id_rank, slot, reason = scored[0]
        taken.add(slot.label)
        assignments.append(Assignment(tool=tool, slot=slot, reason=reason, extruder=extruder))

    return assignments


def validate(assignments: list[Assignment], table: SlotTable) -> list[str]:
    """Return a list of human readable warnings. An empty list means clean."""
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for a in assignments:
        if a.slot.label in seen:
            warnings.append(
                "slot %s is assigned to both T%d and T%d; the printer cannot "
                "feed one slot to two tools at once"
                % (a.slot.label, seen[a.slot.label], a.tool)
            )
        seen[a.slot.label] = a.tool
        if a.slot.unit not in table.units_present:
            warnings.append("CFS unit %d is not reporting as connected" % a.slot.unit)
        if a.extruder is not None and a.extruder.filament_type and a.slot.material_type:
            if a.extruder.filament_type.upper() != a.slot.material_type.upper():
                warnings.append(
                    "T%d is sliced for %s but slot %s holds %s"
                    % (a.tool, a.extruder.filament_type, a.slot.label, a.slot.material_type)
                )
        if a.slot.edited:
            warnings.append(
                "slot %s has no RFID tag; its material was set by hand on the "
                "touchscreen, so remaining length is not tracked"
                % a.slot.label
            )
        if a.slot.remain_len is not None and a.slot.remain_len < 1.0:
            warnings.append(
                "slot %s reports only %.1f m remaining" % (a.slot.label, a.slot.remain_len)
            )
    return warnings


def tool_rewrite_table(assignments: list[Assignment]) -> dict[int, int]:
    """old tool number -> new tool number, for `send --method gcode`."""
    return {a.tool: a.target_index for a in assignments}


def color_match_list(assignments: list[Assignment]) -> list[dict]:
    return [a.color_match_entry() for a in assignments]
