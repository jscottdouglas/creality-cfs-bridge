"""High level operations shared by the CLI and the serve facade."""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import MOONRAKER_PORT
from .gcode import GcodeInfo, parse_gcode, rewrite_tool_numbers
from .mapping import (
    Assignment,
    MappingError,
    auto_map,
    color_match_list,
    parse_explicit,
    tool_rewrite_table,
    validate,
)
from .protocol import CrealityClient, PendingMessage, UploadResult, upload_gcode
from .slots import SlotTable, build_slot_table

METHODS = ("colormatch", "gcode")

# How hard to try to get the colorMatch table installed, same budget as the Orca
# fork (CrealityCFS.cpp): the write lands in box.map well under a second after
# it is sent when it is going to land at all. Worst case 3 * 6 * 0.4 s = 7.2 s.
COLORMATCH_WRITE_ATTEMPTS = 3
COLORMATCH_READBACK_POLLS = 6
COLORMATCH_POLL_SECONDS = 0.4

# How long to wait for the printer to show the job as running after the start
# message. A start with the self-test ticked runs calibration first.
START_CONFIRM_SECONDS = 45.0
START_CONFIRM_SELF_TEST_SECONDS = 120.0


def read_moonraker_box(host: str, port: int = MOONRAKER_PORT, timeout: float = 8.0) -> Optional[dict]:
    """GET the Moonraker `box` object. Read-only. Returns None if unavailable."""
    try:
        response = requests.get(
            "http://%s:%d/printer/objects/query?box" % (host, port), timeout=timeout
        )
        response.raise_for_status()
        return response.json()["result"]["status"]["box"]
    except Exception:
        return None


def read_moonraker_print_stats(host: str, port: int = MOONRAKER_PORT,
                               timeout: float = 8.0) -> Optional[dict]:
    """GET the Moonraker `print_stats` object. Read-only. None if unavailable.

    This is the authority on whether a job is on the bed, and the only source
    that names `paused` separately from `printing`; the Creality protocol's
    `deviceState` collapses both into "not 0". See `cfs.print_state`.
    """
    try:
        response = requests.get(
            "http://%s:%d/printer/objects/query?print_stats" % (host, port),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["result"]["status"]["print_stats"]
    except Exception:
        return None


def read_slots(client: CrealityClient, host: str, moonraker_port: int = MOONRAKER_PORT) -> SlotTable:
    """Live CFS state, merged from the Creality protocol and Moonraker."""
    boxs_info = client.get_boxs_info()
    return build_slot_table(boxs_info, read_moonraker_box(host, moonraker_port))


@dataclass
class SendPlan:
    """Everything `send` decided, and everything it would do."""

    gcode: GcodeInfo
    assignments: list[Assignment]
    warnings: list[str] = field(default_factory=list)
    method: str = "colormatch"
    remote_name: str = ""
    remote_path: str = ""
    upload_url: str = ""
    rewritten_path: Optional[str] = None
    rewrites: int = 0
    messages: list[PendingMessage] = field(default_factory=list)
    upload: Optional[UploadResult] = None
    started: bool = False
    applied_color_match: object = field(default_factory=list)

    def describe(self) -> str:
        lines = []
        lines.append("file      : %s (%s)" % (self.gcode.name, _human(self.gcode.size)))
        lines.append("tools     : %s" % ", ".join("T%d" % t for t in self.gcode.tools_used))
        lines.append("method    : %s" % self.method)
        lines.append("remote    : %s" % (self.remote_path or "(unknown)"))
        lines.append("")
        lines.append("mapping:")
        for a in self.assignments:
            extruder_type = a.extruder.filament_type if a.extruder else ""
            lines.append(
                "  T%-3d -> %-3s  %-6s %-9s %s"
                % (
                    a.tool,
                    a.slot.label,
                    a.slot.material_type or "?",
                    a.slot.colour or "-",
                    ("[sliced as %s] " % extruder_type if extruder_type else "") + a.reason,
                )
            )
        if self.method == "gcode":
            lines.append("")
            lines.append("gcode rewrite:")
            for old, new in sorted(tool_rewrite_table(self.assignments).items()):
                lines.append(
                    "  T%d -> T%d   (%s)" % (old, new, _index_label(new))
                )
            lines.append("  lines changed: %d" % self.rewrites)
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            for w in self.warnings:
                lines.append("  ! " + w)
        lines.append("")
        lines.append("would send:")
        lines.append("  1. POST %s   (%s, multipart, field name = the file name)"
                     % (self.upload_url, _human(self.gcode.size)))
        for i, message in enumerate(self.messages, start=2):
            lines.append("  %d. ws://<printer>:9999  %s" % (i, message.description))
            lines.append("     %s" % message.as_json())
        return "\n".join(lines)


def _index_label(index: int) -> str:
    from .slots import index_to_slot, slot_label

    return slot_label(*index_to_slot(index))


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit) if unit != "B" else "%d B" % size
        size /= 1024.0
    return str(size)


def plan_send(
    client: CrealityClient,
    host: str,
    local_path: str,
    explicit: Optional[list[str]] = None,
    auto: bool = False,
    method: str = "colormatch",
    remote_name: Optional[str] = None,
    self_test: int = 0,
    allow_type_mismatch: bool = False,
    moonraker_port: int = MOONRAKER_PORT,
    force_multi_color: Optional[bool] = None,
) -> SendPlan:
    """Work out the mapping and the exact messages, without sending anything.

    `client.dry_run` decides whether the messages are actually transmitted when
    `execute_send` is called afterwards.
    """
    if method not in METHODS:
        raise MappingError("unknown method %r, expected one of %s" % (method, ", ".join(METHODS)))
    if not os.path.isfile(local_path):
        raise MappingError("no such file: %s" % local_path)

    info = parse_gcode(local_path)
    table = read_slots(client, host, moonraker_port)

    if explicit:
        assignments = parse_explicit(explicit, table)
        missing = [t for t in info.tools_used if t not in {a.tool for a in assignments}]
        if missing and auto:
            remaining = auto_map(
                _restrict(info, missing), table, allow_type_mismatch=allow_type_mismatch
            )
            for a in remaining:
                if a.slot.label not in {x.slot.label for x in assignments}:
                    assignments.append(a)
            assignments.sort(key=lambda a: a.tool)
        elif missing:
            raise MappingError(
                "no mapping for %s. Add --map for each, or pass --auto."
                % ", ".join("T%d" % t for t in missing)
            )
    elif auto:
        assignments = auto_map(info, table, allow_type_mismatch=allow_type_mismatch)
    else:
        raise MappingError("give at least one --map, or --auto")

    # An explicit --map carries no filament info, so attach the extruder it
    # belongs to; validate() needs it to catch a PETG job aimed at a PLA slot.
    for a in assignments:
        if a.extruder is None and a.tool < len(info.extruders):
            a.extruder = info.extruders[a.tool]

    plan = SendPlan(gcode=info, assignments=assignments, method=method)
    plan.warnings = validate(assignments, table)

    # The two mechanisms compose. A rewritten tool number is still looked up in
    # the box module's map, so a stale non-identity map from a previous job
    # would send it somewhere else entirely. See docs/PROTOCOL.md section 4.5.
    if method == "gcode" and table.gcode_map and not table.map_is_identity:
        redirects = ", ".join(
            "%s to %s" % (k[1:], v[1:]) for k, v in sorted(table.gcode_map.items()) if k != v
        )
        actual = []
        for a in assignments:
            landed = table.resolves_to(a.target_index)
            if landed and landed != a.slot.label:
                actual.append("T%d would actually load %s, not %s"
                              % (a.tool, landed, a.slot.label))
        plan.warnings.append(
            "the printer's slot map is not identity (it redirects %s), and "
            "--method gcode does not clear it. %s Use --method colormatch, "
            "which overwrites the map explicitly."
            % (redirects, " ".join(actual) + "." if actual else "")
        )
    if method == "colormatch" and table.gcode_map and not table.map_is_identity:
        untouched = {k for k, v in table.gcode_map.items() if k != v}
        covered = {a.gcode_tnn for a in assignments}
        stale = sorted(untouched - covered)
        if stale:
            plan.warnings.append(
                "the printer still redirects %s from a previous job, and this "
                "mapping does not mention %s, so those redirects stay in place"
                % (", ".join(s[1:] for s in stale), ", ".join(s[1:] for s in stale))
            )
    plan.remote_name = remote_file_name(remote_name, info.name)
    root = client.get_gcode_root()
    plan.remote_path = "%s/%s" % (root, plan.remote_name)
    plan.upload_url = "http://%s:80/upload/%s" % (host, plan.remote_name)

    upload_source = local_path
    if method == "gcode":
        table_map = tool_rewrite_table(assignments)
        handle, rewritten = tempfile.mkstemp(prefix="cfsbridge-", suffix=".gcode")
        os.close(handle)
        plan.rewrites = rewrite_tool_numbers(local_path, rewritten, table_map)
        plan.rewritten_path = rewritten
        upload_source = rewritten
    plan._upload_source = upload_source  # type: ignore[attr-defined]

    # Anything served from a CFS slot starts with multiColorPrint, as the Orca
    # fork does. opGcodeFile means "print from the rear spool holder": on
    # 2026-10-05 the printer answered it by selecting the empty rack and
    # raising its held error, so the job never ran. See docs/PROTOCOL.md 4.3.
    if force_multi_color is None:
        multi = info.is_multi_filament or any(
            e.get("boxId", 0) >= 1 for e in color_match_list(assignments))
    else:
        multi = force_multi_color

    # Build the message list. On a dry-run client these are recorded, not sent.
    if method == "colormatch":
        plan.messages.append(
            client.set_color_match(plan.remote_path, color_match_list(assignments))
        )
    if multi:
        plan.messages.append(client.start_multi_color_print(plan.remote_path, self_test))
    else:
        plan.messages.append(client.start_normal_print(plan.remote_path, self_test))
    return plan


def _restrict(info: GcodeInfo, tools: list[int]) -> GcodeInfo:
    clone = GcodeInfo(
        path=info.path,
        name=info.name,
        size=info.size,
        settings=info.settings,
        extruders=info.extruders,
        tools_used=tools,
        generated_by=info.generated_by,
    )
    return clone



GCODE_EXTENSIONS = (".gcode", ".g", ".gco")


def remote_file_name(remote_name: Optional[str], local_name: str) -> str:
    """Name to store the job under. The printer only starts (and Moonraker only lists) G-code
    files with a G-code extension, so a --name without one gets the local file's extension."""
    if not remote_name:
        return local_name
    if remote_name.lower().endswith(GCODE_EXTENSIONS):
        return remote_name
    return remote_name + (os.path.splitext(local_name)[1] or ".gcode")


def send_live(
    host: str,
    local_path: str,
    explicit: Optional[list[str]] = None,
    auto: bool = False,
    method: str = "colormatch",
    remote_name: Optional[str] = None,
    self_test: int = 0,
    allow_type_mismatch: bool = False,
    moonraker_port: int = MOONRAKER_PORT,
    force_multi_color: Optional[bool] = None,
) -> SendPlan:
    """Plan against a dry-run client, upload, then replay the messages live.

    Doing it in this order means the mapping is fully validated, and the file is
    on the printer, before anything that starts a job is transmitted.
    """
    with CrealityClient(host, dry_run=True) as planner:
        plan = plan_send(
            planner, host, local_path, explicit, auto, method, remote_name,
            self_test, allow_type_mismatch, moonraker_port, force_multi_color,
        )

    source = getattr(plan, "_upload_source", local_path)
    plan.upload = upload_gcode(host, source, plan.remote_name, dry_run=False)
    if not plan.upload.ok:
        raise MappingError(
            "upload failed: HTTP %s from %s: %s"
            % (plan.upload.status, plan.upload.url, plan.upload.body)
        )

    with CrealityClient(host, dry_run=False) as live:
        for message in plan.messages:
            params = message.payload.get("params", {})
            if "colorMatch" in params:
                _install_color_match(live, host, moonraker_port, message, plan)
                continue
            with live._lock:
                for key in ("err", "printFileName", "deviceState"):
                    live._state.pop(key, None)
            live._send_raw(message.payload)
            if "opGcodeFile" in params or "multiColorPrint" in params:
                _confirm_started(live, host, moonraker_port, plan.remote_name,
                                 START_CONFIRM_SELF_TEST_SECONDS if self_test
                                 else START_CONFIRM_SECONDS)
            else:
                time.sleep(0.5)
    plan.started = True

    if plan.rewritten_path and os.path.exists(plan.rewritten_path):
        os.unlink(plan.rewritten_path)
        plan.rewritten_path = None
    return plan


def expected_box_map(entries: list[dict]) -> dict:
    """The box.map entries a colorMatch list should produce: {"T1A": "T3C"}.

    The external spool holder (boxId 0) has no map entry.
    """
    out = {}
    for e in entries:
        box, mat = e.get("boxId", -1), e.get("materialId", -1)
        if e.get("id") and isinstance(box, int) and box >= 1 and isinstance(mat, int) and 0 <= mat < 4:
            out[e["id"]] = "T%d%s" % (box, "ABCD"[mat])
    return out


def color_match_applied(entries: list[dict], box: Optional[dict],
                        boxs_info: Optional[dict]) -> tuple:
    """(accepted, detail) for a colorMatch list against the printer's state.

    Klipper's box.map (Moonraker) is the authority. K2 firmware leaves
    colorMatch out of the boxsInfo reply unless a job is holding a map, so
    boxsInfo is only the fallback when Moonraker gave no map.
    """
    wanted = expected_box_map(entries)
    live_map = (box or {}).get("map")
    if isinstance(live_map, dict) and live_map:
        bad = ["%s should serve from %s but reads %s" % (k, v, live_map.get(k, "nothing"))
               for k, v in sorted(wanted.items()) if live_map.get(k) != v]
        return (not bad, "; ".join(bad) or "box.map %s" % live_map)
    have = (boxs_info or {}).get("colorMatch")
    if not isinstance(have, list):
        return (False, "the printer reports its slot mapping on neither Moonraker nor port 9999")
    got = {(h.get("id"), h.get("boxId"), h.get("materialId")) for h in have}
    missing = [e["id"] for e in entries
               if (e.get("id"), e.get("boxId"), e.get("materialId")) not in got]
    return (not missing, "missing %s" % ", ".join(missing) if missing else "boxsInfo %s" % have)


def _install_color_match(live: CrealityClient, host: str, moonraker_port: int,
                         message: PendingMessage, plan: SendPlan) -> None:
    """Write the mapping, read it back, re-write if needed. Raises if it never lands."""
    entries = message.payload["params"]["colorMatch"]["list"]
    detail = ""
    for _attempt in range(COLORMATCH_WRITE_ATTEMPTS):
        live._send_raw(message.payload)
        for _poll in range(COLORMATCH_READBACK_POLLS):
            time.sleep(COLORMATCH_POLL_SECONDS)
            box = read_moonraker_box(host, moonraker_port, timeout=3.0)
            boxs_info = None
            if not (box and box.get("map")):
                try:
                    boxs_info = live.get_boxs_info().get("boxsInfo", {})
                except Exception:
                    boxs_info = None
            ok, detail = color_match_applied(entries, box, boxs_info)
            if ok:
                plan.applied_color_match = (box or {}).get("map") or (boxs_info or {}).get("colorMatch", [])
                return
    raise MappingError("the printer did not accept the slot mapping (%s). Nothing was started." % detail)


def _confirm_started(live: CrealityClient, host: str, moonraker_port: int,
                     remote_name: str, timeout: float) -> None:
    """Wait until the printer shows the job running. Raises if it does not.

    Sending the start message proves nothing: on 2026-10-05 the printer took
    an opGcodeFile, selected the empty rear spool and stayed in standby.
    """
    deadline = time.time() + timeout
    state, err_key = "unknown", None
    while time.time() < deadline:
        time.sleep(1.0)
        stats = read_moonraker_print_stats(host, moonraker_port, timeout=3.0) or {}
        state = (stats.get("state") or "unknown").lower()
        filename = os.path.basename(stats.get("filename") or "")
        if filename == remote_name and state in ("printing", "paused"):
            return
        # Creality's own pre-print (heat, wipe, probe, CFS load) runs before
        # Klipper's print_stats leaves standby, often for over a minute. The
        # LAN socket announces the job at once: printFileName + deviceState 1
        # ("print work start" in web-server.log, 2026-10-05 rails job).
        snap = live.snapshot()
        if (os.path.basename(str(snap.get("printFileName") or "")) == remote_name
                and str(snap.get("deviceState")) == "1"):
            return
        err = snap.get("err")
        if isinstance(err, dict) and err.get("key"):
            err_key = err.get("key")
            if state in ("standby", "complete", "cancelled", "error") and time.time() > deadline - timeout + 5:
                break
    raise MappingError(
        "the start message was sent but the printer did not start %s (state %s%s). "
        "Check the touchscreen%s."
        % (remote_name, state,
           ", error key%s" % err_key if err_key is not None else "",
           " and clear the error there" if err_key is not None else "")
    )
