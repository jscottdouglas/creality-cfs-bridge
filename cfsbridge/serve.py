"""An OctoPrint-compatible print host, and the page Orca shows next to it.

Orca 2.4.2's OctoPrint client (OrcaSlicer/src/slic3r/Utils/OctoPrint.cpp) only
uses two endpoints, so those are the two that must be right:

  GET  /api/version      the Test button. Must return JSON with an "api" key,
                         and if it returns "text" that must start with
                         "OctoPrint" (OctoPrint.cpp:222 and :492).
  POST /api/files/local  multipart upload with fields print, path, plateindex
                         and file (OctoPrint.cpp:460 to :467).

On `print=true` the facade parses the uploaded G-code and picks CFS slots from
the Orca filament headers. It never answers an upload with an error just
because it could not decide: an upload it cannot map, or that arrives while the
printer is busy, is stored and listed on the page with a slot chooser and a
Start button. Orca always sees 201.

The page itself is `GET /`, which Orca renders in its Device tab when the
preset's `print_host_webui` points here. Everything on it comes from
`GET /cfs/state`; its buttons post to `/cfs/start`, `/cfs/discard` and
`/cfs/undo`.

/server/info is also answered so Orca's Moonraker host type connects too.
"""
from __future__ import annotations

import email.parser
import email.policy
import json
import os
import re
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import requests

from . import CAMERA_PORT, FLUIDD_PORT, MOONRAKER_PORT, __version__
from . import camera, cfs, fluidd as fluidd_app, materials as materials_db
from . import upstream
from .bridge import (_human, read_moonraker_box, read_moonraker_print_stats,
                     send_live)
from .cfscard import cfs_card_page
from .config import config_path, load_config, save_config
from .gcode import GcodeInfo, parse_gcode
from .logs import get_logger
from .mapping import Assignment, MappingError, auto_map
from .protocol import (CrealityClient, ProtocolError, box_config_frame,
                       modify_material_frame, reset_material_frame)
from .slots import SlotTable, build_slot_table, colour_distance, normalise_colour
from .webui import PAGE, setup_page

OCTOPRINT_VERSION = {
    "api": "0.1",
    "server": "1.5.0",
    "text": "OctoPrint (cfsbridge %s)" % __version__,
}

UNDO_SECONDS = 60.0
POLL_SECONDS = 5.0
POLL_BACKOFF = 20.0

# What the setup page is allowed to save as a printer address: an IPv4 address
# or a hostname, and nothing that could be a URL, a path or a shell word. The
# bridge puts this straight into http:// URLs, so it stays narrow on purpose.
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
# How long the setup page's Test button waits. Short, because the answer
# "nothing there" is the useful one and the user is watching.
SETUP_PROBE_TIMEOUT = 5.0

# The filament database is 388 KB and changes only when the user syncs a new
# profile to the printer, so it is read once and then once an hour.
MATERIALS_SECONDS = 3600.0
# How many CFS writes the page keeps offering an undo for.
CFS_WRITE_LOG = 20

log = get_logger()


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Minimal multipart/form-data parser.

    Returns (plain fields, file fields as name -> (filename, bytes)). Written by
    hand because the stdlib `cgi` module is gone in Python 3.13 and Orca only
    ever sends four simple parts.
    """
    header = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    parser = email.parser.BytesParser(policy=email.policy.HTTP)
    message = parser.parsebytes(header + body)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    if not message.is_multipart():
        return fields, files
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


# -- printer state helpers -------------------------------------------------

def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def describe_state(snapshot: dict) -> tuple[str, bool]:
    """(human readable state, is the printer idle and accepting jobs).

    `deviceState == 0` is the printer's own "idle and accepting jobs" flag; the
    Creality client refuses to auto-start a job unless it is 0. See
    docs/PROTOCOL.md section 5.
    """
    if not snapshot:
        return "unknown", False
    device_state = int(_num(snapshot.get("deviceState"), -1))
    progress = _num(snapshot.get("printProgress"))
    layer = _num(snapshot.get("layer"))
    target = _num(snapshot.get("targetNozzleTemp"))
    if device_state == 0:
        return "idle", True
    if progress > 0 or layer > 0:
        return "printing", False
    if target > 0:
        return "starting the job, heating", False
    if device_state < 0:
        return "unknown", False
    return "busy", False


def stop_job(host: str) -> None:
    """Cancel the running job. Only ever called by the page's Undo button, and
    only after `Facade.undo_recent` has confirmed the job is one cfsbridge just
    started and that it has not begun heating."""
    with CrealityClient(host, dry_run=False) as client:
        client.stop_print()


# -- the setup probe -------------------------------------------------------

def probe_printer(host: str, moonraker_port: int = MOONRAKER_PORT,
                  timeout: float = SETUP_PROBE_TIMEOUT) -> dict:
    """Is there a K2 at this address? Behind the setup page's Test button.

    Two read-only HTTP GETs against Moonraker and nothing else: `/printer/info`
    for the name and the model, and the `box` object the poller already reads
    for the number of CFS units. Nothing is written, no protocol session is
    opened, and every failure comes back as a sentence rather than a traceback,
    because the person reading it is typing an address into a web page.
    """
    out = {"ok": False, "hostname": "", "model": "", "cfs_units": 0, "error": ""}
    try:
        response = requests.get("http://%s:%d/printer/info" % (host, moonraker_port),
                                timeout=timeout)
        response.raise_for_status()
        result = (response.json() or {}).get("result") or {}
        out["hostname"] = str(result.get("hostname") or "")
        # Moonraker's own /printer/info has no model field; Creality's build
        # reports one, and where it does not the software version is the next
        # most useful thing to show.
        out["model"] = str(result.get("model")
                           or result.get("software_version") or "")
        box = read_moonraker_box(host, moonraker_port, timeout=timeout) or {}
        box = box.get("box", box) if isinstance(box, dict) else {}
        out["cfs_units"] = sum(1 for unit in range(1, 5)
                               if isinstance(box.get("T%d" % unit), dict))
        out["ok"] = True
    except Exception as exc:
        out["error"] = str(exc) or type(exc).__name__
    return out


# -- pending uploads -------------------------------------------------------

class PendingUpload:
    """A file Orca uploaded that is waiting for a human to choose slots."""

    def __init__(self, name: str, path: str, info: GcodeInfo, reason: str,
                 proposed: Optional[dict[int, str]] = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.path = path
        self.info = info
        self.reason = reason
        self.proposed = proposed or {}
        self.received = time.time()
        self.message = ""
        self.ok = True

    def as_json(self, table: Optional[SlotTable], now: float) -> dict:
        tools = []
        for tool in self.info.tools_used:
            extruder = (self.info.extruders[tool]
                        if tool < len(self.info.extruders) else None)
            options = slot_options(table, extruder)
            selected = self.proposed.get(tool)
            if selected not in {o["label"] for o in options}:
                selected = options[0]["label"] if options else None
            tools.append({
                "tool": tool,
                "type": (extruder.filament_type if extruder else "") or "",
                "colour": (extruder.colour if extruder else None),
                "options": options,
                "selected": selected,
            })
        return {
            "id": self.id,
            "name": self.name,
            "size": self.info.size,
            "size_text": _human(self.info.size),
            "received": self.received,
            "age_s": max(0.0, now - self.received),
            "reason": self.reason,
            "message": self.message,
            "ok": self.ok,
            "tools": tools,
        }


def slot_options(table: Optional[SlotTable], extruder) -> list[dict]:
    """Every usable slot, the ones holding the right material first.

    The dropdown is the manual escape hatch, so a slot of the wrong type is
    still offered; it is just sorted below the compatible ones and labelled.
    """
    if table is None:
        return []
    want = ((extruder.filament_type if extruder else "") or "").strip().upper()
    colour = extruder.colour if extruder else None
    ranked = []
    for slot in table.filled():
        have = (slot.material_type or "").strip().upper()
        compatible = bool(want) and have == want
        distance = colour_distance(colour, slot.colour)
        text = "%s   %s   %s" % (slot.label, slot.material_type or "?",
                                 slot.vendor or "no vendor")
        if slot.remain_len is not None:
            text += "   %.1f m" % slot.remain_len
        if want and not compatible:
            text += "   (holds %s, not %s)" % (slot.material_type or "?", want)
        ranked.append((0 if compatible else 1, distance, slot.label, {
            "label": slot.label,
            "type": slot.material_type,
            "colour": slot.colour,
            "vendor": slot.vendor,
            "remain_len_m": slot.remain_len,
            "compatible": compatible,
            "text": text,
        }))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in ranked]


# -- the facade ------------------------------------------------------------

class Facade:
    """The state a handler needs. One instance is shared by all requests.

    A background thread keeps a cached view of the printer so that every page
    refresh is free and so that a printer that is switched off costs a log line
    rather than a stack trace.
    """

    def __init__(self, host: str, method: str = "colormatch", live: bool = False,
                 allow_type_mismatch: bool = False,
                 moonraker_port: int = MOONRAKER_PORT,
                 spool_dir: Optional[str] = None,
                 poll_interval: float = POLL_SECONDS,
                 undo_seconds: float = UNDO_SECONDS,
                 fluidd_url: Optional[str] = None,
                 log_path: Optional[str] = None,
                 fluidd_dist: Optional[str] = None,
                 bind: str = "127.0.0.1",
                 port: int = 7126) -> None:
        self.host = host
        self.method = method
        self.live = live
        self.allow_type_mismatch = allow_type_mismatch
        self.moonraker_port = moonraker_port
        self.spool_dir = spool_dir or os.path.join(tempfile.gettempdir(), "cfsbridge-spool")
        os.makedirs(self.spool_dir, exist_ok=True)
        self.poll_interval = poll_interval
        self.undo_seconds = undo_seconds
        self.fluidd_url = fluidd_url or "http://%s:%d" % (host, FLUIDD_PORT)
        self.log_path = log_path
        self.bind = bind
        self.port = port
        # Kept so that `apply_host` can rebuild the Fluidd app for a new
        # printer address without being told the dist again.
        self.fluidd_dist = fluidd_dist
        # The tests' guarantee, set by `make_server(dry_run=True)`: a dry-run
        # facade never starts a poller, so no test can read a printer even
        # after `apply_host` is handed a real looking address. It is not the
        # per-write dry run, which is `live` and `_dry()`.
        self.dry_run = False

        # The forked Fluidd. `GET /fluidd/` is its dist; see fluidd.py and
        # docs/FLUIDD_FORK.md. The stock-Fluidd fallback pages (`/camera`,
        # `/cfscard`) stay where they were, because a checkout with no build in
        # it still has to be useful.
        self.fluidd = fluidd_app.FluiddApp(host, moonraker_port, fluidd_dist)
        # The fork update checker. Cached six hours; it never touches a clone.
        self.updates = upstream.UpdateChecker()
        # Filled by `sync_webcams()` at startup so `/updates` and the log can
        # say what happened without repeating the work.
        self.webcam_sync: dict = {}

        self.lock = threading.RLock()
        # Bumped by `apply_host` under the lock. A poll cycle carries the
        # generation it started in and throws its reading away if the number
        # moved while it was reading, because that reading describes a printer
        # this bridge is no longer pointed at.
        self._generation = 0
        self.table: Optional[SlotTable] = None
        self.snapshot: dict = {}
        self.colour_match: list = []
        self.last_ok: float = 0.0
        self.last_error: Optional[str] = None
        self.pending: list[PendingUpload] = []
        self.recent: Optional[dict] = None
        self.last_plan = None

        # CFS card state. `box_config` and `print_stats` come from the same
        # poller as everything else; there is deliberately no second poller.
        self.box_config: dict = {}
        self.print_stats: dict = {}
        self.materials: list = []
        self._materials_at: float = 0.0
        self.cfs_writes: list = []

        # Seams. The tests replace these with a fake upstream; nothing in the
        # test suite is ever allowed to reach a real printer.
        self.reader: Callable[[], tuple] = self._read_live
        self.send_live_fn = send_live
        self.stop_fn = stop_job
        # The factory `CfsWriter` builds its client with. None means the real
        # `CrealityClient`, whose dry-run mode is what makes a mistake here
        # harmless.
        self.cfs_client_factory = None
        # How long a write waits for the printer's echo, and how long it lets
        # the printer act before reading back. Zeroed in the tests.
        self.cfs_ack_timeout = 8.0
        self.cfs_settle = 1.0

        self._client: Optional[CrealityClient] = None
        self._stop = threading.Event()
        self._poller: Optional[threading.Thread] = None
        self._local_ip: Optional[str] = None

    # -- camera ------------------------------------------------------------

    def local_ip(self) -> str:
        """The address the camera page writes into its ICE candidates.

        Worked out once and kept, because working it out can mean asking
        Windows. See camera.local_address.
        """
        with self.lock:
            found = self._local_ip
        if found is None:
            found = camera.local_address(self.host, CAMERA_PORT)
            with self.lock:
                self._local_ip = found
        return found

    def bridge_url(self) -> str:
        """This bridge's own address, as another machine would write it.

        Only used to fill in the fallback webcam record, which the browser
        fetches, not the printer, so loopback is right here.
        """
        host = "127.0.0.1" if self.bind in ("", "0.0.0.0", "::") else self.bind
        return "http://%s:%d" % (host, self.port)

    def sync_webcams(self, dry_run: bool = False) -> dict:
        """Point Moonraker's webcam records at the fork's camera service.

        A Moonraker database write, not a printer-state write: it changes a row
        in Moonraker's own store and touches no motion, heater or filament
        path. It runs once at startup, writes only what differs, and never
        raises. See camera.sync_webcams.
        """
        result = camera.sync_webcams(self.host, self.bridge_url(),
                                     self.moonraker_port, CAMERA_PORT,
                                     dry_run=dry_run)
        with self.lock:
            self.webcam_sync = dict(result)
        log.info("%s", result.get("message", ""))
        return result

    def writes_json(self) -> dict:
        """`GET /cfs/writes`: the write log on its own, for the CFS page."""
        with self.lock:
            writes = [w.as_json() for w in reversed(self.cfs_writes)]
        return {"ok": True, "now": time.time(), "live": self.live,
                "writes": writes}

    def camera_page(self, local_ip: Optional[str] = None,
                    bare: bool = False) -> str:
        """The body of `GET /camera`.

        The printer address comes from this facade, so the page is correct for
        whatever `--host` the bridge was started with. `local_ip` is the `?lan=`
        override.
        """
        return camera.camera_page(self.host, CAMERA_PORT,
                                  local_ip=local_ip or self.local_ip(),
                                  bare=bare)

    def fluidd_page(self, local_ip: Optional[str] = None) -> str:
        """The body of `GET /fluidd`: the printer's Fluidd with the camera and
        CFS cards floating over it, which is the only way to get any of them on
        this printer. See docs/CAMERA.md section 7 and docs/PROTOCOL.md 6."""
        camera_url = "/camera?bare=1"
        if local_ip:
            camera_url += "&lan=" + urllib.parse.quote(local_ip)
        return camera.fluidd_shell(self.fluidd_url + "/", camera_url,
                                   "/cfscard?bare=1")

    # -- polling ----------------------------------------------------------

    def _read_live(self) -> tuple:
        if self._client is None:
            client = CrealityClient(self.host, dry_run=True)
            client.connect()
            self._client = client
        boxs_info = self._client.get_boxs_info()
        snapshot = self._client.snapshot()
        table = build_slot_table(
            boxs_info, read_moonraker_box(self.host, self.moonraker_port)
        )
        colour_match = (boxs_info.get("boxsInfo") or {}).get("colorMatch") or []
        extras = {
            "box_config": (self._client.get_box_config() or {}).get("boxConfig") or {},
            "print_stats": read_moonraker_print_stats(self.host, self.moonraker_port),
        }
        # Once an hour, not once a poll: the reply is 388 KB.
        if time.time() - self._materials_at > MATERIALS_SECONDS:
            self._materials_at = time.time()
            try:
                extras["materials"] = materials_db.parse_materials(
                    self._client.get("reqMaterials"))
            except Exception as exc:
                log.debug("could not read the filament database: %s", exc)
        return table, snapshot, colour_match, extras

    def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def poll_once(self) -> bool:
        """Refresh the cache. Never raises. Returns True when it worked.

        A read is not instant and `apply_host` can land in the middle of one.
        A cycle that began before the switch describes the old printer, so it
        writes nothing: putting that reading back would undo the cache clear
        and make `last_ok` claim the new address had answered. Hence the
        generation check on both the way out and the way in.
        """
        with self.lock:
            generation = self._generation
        try:
            # `reader` is a test seam and the fakes predate the CFS card, so a
            # three-part answer is still accepted and simply carries no extras.
            reading = self.reader()
            table, snapshot, colour_match = reading[0], reading[1], reading[2]
            extras = reading[3] if len(reading) > 3 else {}
        except Exception as exc:
            self._drop_client()
            message = "%s: %s" % (type(exc).__name__, exc)
            with self.lock:
                if generation != self._generation:
                    return False
                first = self.last_error is None
                self.last_error = message
            if first:
                log.warning("printer %s is not answering (%s); retrying quietly",
                            self.host, message)
            return False
        with self.lock:
            if generation != self._generation:
                log.debug("dropped a reading from a previous printer address")
                return False
            recovered = self.last_error is not None or self.last_ok == 0.0
            self.table = table
            self.snapshot = snapshot
            self.colour_match = colour_match
            if extras.get("box_config"):
                self.box_config = dict(extras["box_config"])
            if extras.get("print_stats") is not None:
                self.print_stats = dict(extras["print_stats"] or {})
            if extras.get("materials"):
                self.materials = list(extras["materials"])
            self.last_ok = time.time()
            self.last_error = None
        if recovered:
            log.info("printer %s answered: %d slots, state %s",
                     self.host, len(table.slots), describe_state(snapshot)[0])
        return True

    def _poll_loop(self) -> None:
        """Read, wait, read again, until this thread is retired.

        Retirement is identity, not just the stop flag: `stop_poller` gives up
        joining after three seconds, so a thread inside a slow read outlives
        it, and `start_poller` clears the flag. A thread that is no longer
        `self._poller` has been replaced and must stop, whatever the flag says.
        """
        while True:
            with self.lock:
                if (self._stop.is_set()
                        or threading.current_thread() is not self._poller):
                    return
                generation = self._generation
            ok = self.poll_once()
            with self.lock:
                stale = generation != self._generation
            if stale:
                # The address changed while that cycle was reading, so its
                # result was dropped. Read the new printer now rather than
                # sitting out a backoff the old printer earned.
                continue
            self._stop.wait(self.poll_interval if ok else POLL_BACKOFF)

    def start_poller(self) -> None:
        """Start polling, unless there is nothing to poll.

        With no printer address there is no poller: a fresh install would
        otherwise spend its life retrying Moonraker at "" and filling the log
        with it. `apply_host` starts one the moment an address arrives.

        Under the lock and idempotent, so two saves arriving together cannot
        each decide the poller is missing and spawn one.
        """
        with self.lock:
            if not self.host or self.dry_run:
                return
            # A thread that is still alive counts, even one a timed-out
            # `stop_poller` has already given up on: two pollers reading one
            # printer is worse than a gap in the polling. A `_poller` that has
            # since finished is only a stale reference and may be replaced.
            if self._poller is not None and self._poller.is_alive():
                return
            # `stop_poller` sets this and nothing used to clear it, so a
            # restarted poller would have exited on its first check.
            self._stop.clear()
            self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                            name="cfsbridge-poll")
            self._poller.start()

    def apply_host(self, host: str) -> bool:
        """Point the running bridge at a printer address. Behind POST /api/setup.

        Everything that names the printer is derived from `self.host`, so this
        is a setter plus a cache clear plus, when there was no address before,
        the first start of the poller. Returns True when the address changed.

        All of it happens under the lock, including the client drop and the
        poller start: a reader that is mid-poll must not be able to interleave
        with the clear, and two concurrent saves must not produce two pollers.
        """
        host = (host or "").strip()
        with self.lock:
            changed = host != self.host
            if changed:
                self._generation += 1
                self.host = host
                self.fluidd_url = "http://%s:%d" % (host, FLUIDD_PORT)
                self.fluidd = fluidd_app.FluiddApp(host, self.moonraker_port,
                                                   self.fluidd_dist)
                # Everything cached below describes the old printer.
                self._local_ip = None
                self.table = None
                self.snapshot = {}
                self.colour_match = []
                self.box_config = {}
                self.print_stats = {}
                self.materials = []
                self._materials_at = 0.0
                self.last_ok = 0.0
                self.last_error = None
                self._drop_client()
                log.info("printer address is now %s", host or "(none)")
            self.start_poller()
        return changed

    def stop_poller(self) -> None:
        """Stop polling. The join is outside the lock on purpose.

        Waiting on the poll thread while holding the lock it takes at the top
        of every cycle would deadlock. And the reference is only cleared when
        the thread has really finished: clearing it while the thread is still
        inside a read is exactly what would let `start_poller` add a second
        poller next to the first.
        """
        self._stop.set()
        poller = self._poller
        if poller is not None:
            poller.join(timeout=3.0)
            with self.lock:
                if poller.is_alive():
                    log.warning("the poll thread is still reading %s; it stops "
                                "when that read returns, and no new poller "
                                "starts until it does", self.host or "(none)")
                elif self._poller is poller:
                    self._poller = None
        self._drop_client()

    # -- state for the page ------------------------------------------------

    def printer_json(self) -> dict:
        with self.lock:
            snapshot = dict(self.snapshot)
            last_ok, error = self.last_ok, self.last_error
        text, idle = describe_state(snapshot)
        path = snapshot.get("printFileName") or ""
        return {
            "reachable": bool(last_ok) and error is None,
            "error": error,
            "last_ok": last_ok,
            "age_s": (time.time() - last_ok) if last_ok else None,
            "state": snapshot.get("state"),
            "device_state": snapshot.get("deviceState"),
            "state_text": text,
            "idle": idle,
            "file_path": path,
            "file_name": os.path.basename(path) if path else "",
            "progress": _num(snapshot.get("printProgress")),
            "layer": int(_num(snapshot.get("layer"))),
            "total_layer": int(_num(snapshot.get("TotalLayer"))),
            "left_time": _num(snapshot.get("printLeftTime")),
            "job_time": _num(snapshot.get("printJobTime")),
            "nozzle": round(_num(snapshot.get("nozzleTemp")), 1),
            "nozzle_target": round(_num(snapshot.get("targetNozzleTemp")), 1),
            "bed": round(_num(snapshot.get("bedTemp0")), 1),
            "bed_target": round(_num(snapshot.get("targetBedTemp0")), 1),
            "box_temp": snapshot.get("boxTemp"),
            "cfs_connect": snapshot.get("cfsConnect"),
            "error_code": (snapshot.get("err") or {}).get("errcode") if isinstance(
                snapshot.get("err"), dict) else None,
            "hostname": snapshot.get("hostname"),
            "model": snapshot.get("model"),
        }

    def units_json(self) -> list[dict]:
        with self.lock:
            table = self.table
        units = []
        for unit in range(1, 5):
            slots = [s for s in (table.slots if table else []) if s.unit == unit]
            if not slots:
                continue
            units.append({
                "unit": unit,
                "connected": unit in (table.units_present if table else set()),
                "slots": [{
                    "label": s.label, "tool_index": s.index,
                    "type": s.material_type, "colour": s.colour,
                    "vendor": s.vendor, "name": s.name,
                    "remain_len_m": s.remain_len, "loaded": s.loaded,
                    "mapped": s.mapped, "present": s.present,
                    "manually_set": s.edited,
                } for s in slots],
            })
        return units

    def active_map_json(self) -> list[dict]:
        """The printer's live `colorMatch`, resolved to physical slots."""
        from .slots import parse_slot, slot_to_index

        with self.lock:
            table, entries = self.table, list(self.colour_match)
        out = []
        for entry in entries:
            unit = int(_num(entry.get("boxId"), -1))
            index = int(_num(entry.get("materialId"), -1))
            slot = table.get(unit, index) if table else None
            tool = str(entry.get("id") or "")
            try:
                tool_index = slot_to_index(*parse_slot(tool))
            except Exception:
                tool_index = None
            out.append({
                "tool": tool,
                "tool_index": tool_index,
                "slot": slot.label if slot else "%d?%d" % (unit, index),
                "type": slot.material_type if slot else entry.get("type", ""),
                "colour": slot.colour if slot else None,
                "vendor": slot.vendor if slot else "",
            })
        return out

    def recent_json(self, now: float) -> Optional[dict]:
        with self.lock:
            recent = dict(self.recent) if self.recent else None
        if recent is None:
            return None
        left = self.undo_seconds - (now - recent["started_at"])
        if left < -600:
            with self.lock:
                self.recent = None
            return None
        recent["undo_left_s"] = left
        available = left > 0 and not recent["undone"]
        recent["undo_available"] = available
        if recent["undone"]:
            recent["undo_reason"] = "already cancelled"
        elif left <= 0:
            recent["undo_reason"] = (
                "The %d second undo window has passed. Cancel from the printer's "
                "screen or from Fluidd instead." % int(self.undo_seconds))
        else:
            recent["undo_reason"] = ""
        return recent

    def state_json(self) -> dict:
        now = time.time()
        with self.lock:
            table = self.table
            pending = [p.as_json(table, now) for p in self.pending]
        return {
            "version": __version__,
            "host": self.host,
            "live": self.live,
            "method": self.method,
            "now": now,
            "spool_dir": self.spool_dir,
            "log_path": self.log_path,
            "fluidd_url": self.fluidd_url,
            "camera": {
                # The K2's camera serves no JPEG and no MJPEG; the only frame
                # source is a WebRTC exchange on port 8000, which cannot be put
                # in an <img>. Fluidd's own camera types cannot speak Creality's
                # signalling envelope either, so the bridge plays it itself on
                # /camera and everything that wants a picture embeds that. See
                # docs/CAMERA.md.
                "embeddable": True,
                "embed_url": "/camera",
                "page_url": "/camera",
                "stream_url": camera.signalling_url(self.host, CAMERA_PORT),
                "snapshot_url": "",
                "note": ("The camera is WebRTC only. /camera on this bridge does "
                         "the signalling and serves nothing but the picture, so it "
                         "embeds anywhere an iframe fits, including Fluidd's own "
                         "iframe camera card. See docs/CAMERA.md."),
            },
            "printer": self.printer_json(),
            "units": self.units_json(),
            "map_is_identity": bool(table.map_is_identity) if table else True,
            "active_map": self.active_map_json(),
            "pending": pending,
            "recent": self.recent_json(now),
            # Added for the CFS card. Every key above is unchanged.
            "cfs": self.cfs_json(),
        }

    def slots_json(self) -> dict:
        """The machine readable slot table.

        The original keys are unchanged, because scripts read them. The CFS
        card needed more per slot (the name, the tag id, the slot's own
        temperature limits) and a per-unit block, so both were added alongside.
        """
        with self.lock:
            table = self.table
        if table is None and not self.poll_once():
            raise ProtocolError(self.last_error or "the printer has not answered yet")
        with self.lock:
            table = self.table
        return {
            "units_present": sorted(table.units_present),
            "slots": [
                {"slot": s.label, "type": s.material_type, "colour": s.colour,
                 "vendor": s.vendor, "remain_len_m": s.remain_len,
                 "loaded": s.loaded, "tool_index": s.index,
                 "name": s.name, "rfid": s.rfid, "percent": s.percent,
                 "min_temp": s.min_temp, "max_temp": s.max_temp,
                 "mapped": s.mapped, "present": s.present,
                 "manually_set": s.edited}
                for s in table.slots
            ],
            "units": [_unit_json(info) for _, info in sorted(table.units.items())],
            "box_config": dict(self.box_config),
        }

    # -- the CFS card ------------------------------------------------------

    def active_map_labels(self) -> set:
        """Every physical slot label the printer's live `colorMatch` names.

        Not the same as the loaded slot: a two-colour job holds two slots and
        only one of them is in the toolhead at any moment. Both are off limits
        while it runs.
        """
        with self.lock:
            table, entries = self.table, list(self.colour_match)
        out = set()
        for entry in entries:
            unit = int(_num(entry.get("boxId"), -1))
            index = int(_num(entry.get("materialId"), -1))
            slot = table.get(unit, index) if table else None
            if slot is not None:
                out.add(slot.label)
        return out

    def print_state(self) -> str:
        with self.lock:
            stats, snapshot = dict(self.print_stats), dict(self.snapshot)
        return cfs.print_state(stats, snapshot)

    def cfs_json(self) -> dict:
        """Everything the CFS card draws, from the cache the poller fills."""
        with self.lock:
            table = self.table
            config = dict(self.box_config)
            items = list(self.materials)
            writes = [w.as_json() for w in reversed(self.cfs_writes)]
        state = self.print_state()
        mapped = self.active_map_labels()
        loaded = None
        units = []
        for unit in range(1, 5):
            info = table.unit_info(unit) if table else None
            if info is None:
                continue
            slots = [s for s in table.slots if s.unit == unit]
            for s in slots:
                if s.loaded:
                    loaded = s.label
            units.append(dict(_unit_json(info), slots=[{
                "label": s.label, "letter": s.label[1], "material_id": s.slot,
                "tool_index": s.index, "type": s.material_type,
                "colour": s.colour, "vendor": s.vendor, "name": s.name,
                "rfid": s.rfid, "percent": s.percent,
                "remain_len_m": s.remain_len, "min_temp": s.min_temp,
                "max_temp": s.max_temp, "present": s.present,
                "loaded": s.loaded, "mapped": s.label in mapped,
                "manually_set": s.edited, "edit_status": s.edit_status,
            } for s in slots]))
        return {
            "print_state": state,
            "busy": cfs.is_busy(state),
            "live": self.live,
            "writes_allowed": self.live,
            "loaded": loaded,
            "mapped": sorted(mapped),
            "units": units,
            "box_config": {
                "auto_refill": int(_num(config.get("autoRefill"))),
                "auto_feed": int(_num(config.get("cAutoFeed"))),
                "self_test": int(_num(config.get("cSelfTest"))),
                "auto_update_filament": int(_num(config.get("cAutoUpdateFilament"))),
                "known": bool(config),
                # `SetBoxsConfig` writes exactly these three, printer-wide.
                # There is no documented write for cAutoUpdateFilament and no
                # per-unit form of any of them. docs/PROTOCOL.md section 6.3.
                "writable": ["auto_refill", "auto_feed", "self_test"],
                "read_only": ["auto_update_filament"],
                "note": ("These are printer-wide, not per CFS unit: the write "
                         "carries no box id. cAutoUpdateFilament is reported "
                         "but never written by Creality's own client, so the "
                         "bridge shows it and will not set it."),
            },
            "materials": materials_db.as_json(items),
            "writes": writes,
            "drying": {
                "supported": any(u["can_dry"] for u in units),
                "note": ("Drying needs a CFS-Pro or CFS-C cabinet. Every unit "
                         "on this printer reports MF003, the plain CFS, which "
                         "has no dryer, so the buttons stay off. The frames "
                         "are implemented and documented; none has been sent."),
            },
        }

    # -- CFS writes --------------------------------------------------------

    def _writer(self):
        return cfs.CfsWriter(self.host, client_factory=self.cfs_client_factory,
                             ack_timeout=self.cfs_ack_timeout,
                             settle=self.cfs_settle)

    def _find_slot(self, unit, slot):
        """(slot, error). Accepts unit 1..4 with slot 0..3 or 'A'..'D', or '2B'."""
        from .slots import SlotError, parse_slot

        with self.lock:
            table = self.table
        if table is None:
            return None, "The printer has not answered yet, so no slot could be found."
        label = None
        if unit is None and slot is not None:
            label = str(slot)
        elif unit is not None and slot is None:
            label = str(unit)
        if label is not None:
            try:
                unit_no, index = parse_slot(label)
            except SlotError as exc:
                return None, str(exc)
        else:
            try:
                unit_no = int(unit)
            except (TypeError, ValueError):
                return None, "%r is not a CFS unit number." % (unit,)
            text = str(slot).strip().upper()
            if text.isdigit():
                index = int(text)
            elif len(text) == 1 and text in "ABCD":
                index = "ABCD".index(text)
            else:
                return None, "%r is not a slot; expected A, B, C or D." % (slot,)
        found = table.get(unit_no, index)
        if found is None:
            return None, "This printer has no slot %s." % (
                "%s%s" % (unit_no, "ABCD"[index]) if 0 <= index < 4 else
                "%s/%s" % (unit_no, index))
        return found, ""

    def _gate(self, kind: str, slot=None) -> Optional[str]:
        """The one place a CFS write is allowed or refused. Server side."""
        if not self.live:
            return ("cfsbridge is running without --live, so nothing was sent. "
                    "This is what it would have written.")
        return cfs.check_write(kind, self.print_state(), slot=slot,
                               active_map=self.active_map_labels())

    def _record(self, record) -> dict:
        with self.lock:
            self.cfs_writes.append(record)
            del self.cfs_writes[:-CFS_WRITE_LOG]
        return record.as_json()

    def _refused(self, kind: str, message: str, dry_run: bool) -> dict:
        log.info("CFS %s refused: %s", kind, message)
        return {"ok": False, "refused": True, "sent": False,
                "dry_run": dry_run, "message": message, "write": None}

    def _finish(self, kind: str, description: str, result, before: dict,
                after: dict) -> dict:
        undoable, undo_reason = cfs.UNDO_RULES.get(kind, (False, ""))
        record = cfs.WriteRecord(
            kind=kind, description=description, before=before, after=after,
            sent=result.sent, ok=result.ok, message=result.message,
            undoable=bool(undoable and result.ok),
            undo_reason="" if (undoable and result.ok) else undo_reason,
        )
        log.info("CFS %s: %s (%s)", kind, description,
                 "sent" if result.sent else "dry run")
        payload = self._record(record)
        if result.sent:
            self.poll_once()
        return {"ok": result.ok, "refused": False, "sent": result.sent,
                "dry_run": result.dry_run, "message": result.message,
                "write": payload}

    def edit_slot(self, unit, slot, fields: dict,
                  dry_run: Optional[bool] = None) -> dict:
        """Write one slot's material. `modifyMaterial`, docs/PROTOCOL.md 6.1."""
        target, error = self._find_slot(unit, slot)
        if target is None:
            return self._refused("edit", error, bool(dry_run))
        clear = bool(fields.get("clear"))
        kind = "clear" if clear else "edit"
        refusal = self._gate(kind, slot=target)
        dry = self._dry(dry_run)
        if refusal and not dry:
            return self._refused(kind, refusal, False)

        with self.lock:
            table = self.table
        info = table.unit_info(target.unit) if table else None
        # Only the CFS Mini's edit carries boxType; MF003 does not.
        box_type = info.box_type if (info is not None and info.is_mini) else None

        before = _slot_values(target)
        if clear:
            params = reset_material_frame(target.unit, target.slot, box_type)
            after = {"type": "", "vendor": "", "name": "", "colour": "",
                     "rfid": "", "min_temp": 0, "max_temp": 0}
            description = "clear CFS slot %s" % target.label
        else:
            after = _wanted_values(fields, before, list(self.materials))
            if not after.get("type"):
                return self._refused(kind, "Choose a material type.", dry)
            try:
                params = modify_material_frame(
                    target.unit, target.slot, rfid=after["rfid"],
                    material_type=after["type"], vendor=after["vendor"],
                    name=after["name"], colour=after["colour"],
                    min_temp=after["min_temp"], max_temp=after["max_temp"],
                    pressure=after["pressure"], box_type=box_type)
            except ProtocolError as exc:
                return self._refused(kind, str(exc), dry)
            description = ("set CFS slot %s to %s %s %s"
                           % (target.label, after["vendor"] or "?",
                              after["name"] or after["type"], after["colour"] or ""))

        def verify(client):
            return _verify_slot(client, target.unit, target.slot, after)

        result = self._writer().send(description, params, "modifyMaterial",
                                     dry_run=dry, verify=verify)
        return self._finish(kind, description, result, before, after)

    def set_box_config(self, values: dict, dry_run: Optional[bool] = None) -> dict:
        """Write the printer-wide CFS options. `boxConfig`, section 6.3."""
        dry = self._dry(dry_run)
        refusal = self._gate("config")
        if refusal and not dry:
            return self._refused("config", refusal, False)
        with self.lock:
            current = dict(self.box_config)
        before = {"auto_refill": int(_num(current.get("autoRefill"))),
                  "auto_feed": int(_num(current.get("cAutoFeed"))),
                  "self_test": int(_num(current.get("cSelfTest")))}
        after = {key: (1 if _truthy(values[key]) else 0) if key in values else before[key]
                 for key in ("auto_refill", "auto_feed", "self_test")}
        params = box_config_frame(after["auto_refill"], after["auto_feed"],
                                  after["self_test"])
        description = ("set the CFS options to autoRefill %d, cAutoFeed %d, "
                       "cSelfTest %d"
                       % (after["auto_refill"], after["auto_feed"], after["self_test"]))

        def verify(client):
            got = (client.get_box_config() or {}).get("boxConfig") or {}
            read = {"auto_refill": int(_num(got.get("autoRefill"))),
                    "auto_feed": int(_num(got.get("cAutoFeed"))),
                    "self_test": int(_num(got.get("cSelfTest")))}
            if read == after:
                return True, "The printer now reports %s." % read, read
            return (False, "The printer reports %s, not %s. Nothing else was "
                           "changed." % (read, after), read)

        result = self._writer().send(description, params, "boxConfig",
                                     dry_run=dry, verify=verify)
        return self._finish("config", description, result, before, after)

    def feed_slot(self, unit, slot, feed: bool,
                  dry_run: Optional[bool] = None) -> dict:
        """Load or unload one slot. `feedInOrOut`, section 6.4."""
        target, error = self._find_slot(unit, slot)
        if target is None:
            return self._refused("feed", error, bool(dry_run))
        dry = self._dry(dry_run)
        refusal = self._gate("feed", slot=target)
        if refusal and not dry:
            return self._refused("feed", refusal, False)
        if feed and not target.present:
            return self._refused("feed", "Slot %s is empty; there is nothing to "
                                         "load." % target.label, dry)
        from .protocol import feed_frame

        params = feed_frame(target.unit, target.slot, 1 if feed else 0)
        description = "%s CFS slot %s" % ("load" if feed else "unload", target.label)
        # The printer echoes nothing for this one, so there is no ack to wait
        # for and no read-back that proves it instantly; the card watches the
        # slot's `loaded` flag on the next poll instead.
        result = self._writer().send(description, params, None, dry_run=dry)
        if result.ok and result.sent:
            result.message = ("Sent. The printer does not acknowledge this "
                              "message; watch the LOADED marker and the "
                              "printer's screen.")
        return self._finish("feed", description, result,
                            {"loaded": target.loaded}, {"loaded": bool(feed)})

    def refresh_slot(self, unit, slot, dry_run: Optional[bool] = None) -> dict:
        """Re-read one slot's RFID tag. `refreshBox`, section 6.5."""
        target, error = self._find_slot(unit, slot)
        if target is None:
            return self._refused("refresh", error, bool(dry_run))
        dry = self._dry(dry_run)
        refusal = self._gate("refresh", slot=target)
        if refusal and not dry:
            return self._refused("refresh", refusal, False)
        from .protocol import refresh_frame

        params = refresh_frame(target.unit, target.slot)
        description = "re-read the RFID tag in CFS slot %s" % target.label
        result = self._writer().send(description, params, None, dry_run=dry)
        if result.ok and result.sent:
            result.message = ("Sent. The tag read takes a moment and the "
                              "printer does not acknowledge the request; the "
                              "slot updates on the next poll.")
        return self._finish("refresh", description, result,
                            _slot_values(target), {})

    def dry_unit(self, unit, action: str, bin_no: int = 1,
                 target_temp: float = 45.0, minutes: int = 360,
                 material_types: Optional[list] = None, enable: bool = True,
                 dry_run: Optional[bool] = None) -> dict:
        """Start or stop drying, or set auto-dry. `dryBox` / `autoDry`, 6.6."""
        from .protocol import auto_dry_frame, dry_box_frame, dry_stop_frame

        dry = self._dry(dry_run)
        try:
            unit_no = int(unit)
        except (TypeError, ValueError):
            return self._refused("dry", "%r is not a CFS unit number." % (unit,), dry)
        with self.lock:
            table = self.table
        info = table.unit_info(unit_no) if table else None
        if info is None:
            return self._refused("dry", "This printer has no CFS %s." % unit_no, dry)
        if not info.can_dry:
            return self._refused(
                "dry", "CFS %d is a %s (%s). Only the CFS-Pro and CFS-C have a "
                       "drying cabinet, so there is nothing to start."
                       % (unit_no, info.model_name, info.model or "no model id"), dry)
        if info.ac == 0:
            return self._refused(
                "dry", "CFS %d reports no mains power (ac 0). Creality's own "
                       "page refuses to dry in that state, and so does this "
                       "one." % unit_no, dry)
        refusal = self._gate("dry")
        if refusal and not dry:
            return self._refused("dry", refusal, False)
        if bin_no not in (1, 2, 3):
            return self._refused("dry", "The drying bin is 1 (slots A and B) or "
                                        "2 (slots C and D).", dry)

        if action == "stop":
            params = dry_stop_frame(unit_no, bin_no)
            description = "stop drying CFS %d bin %d" % (unit_no, bin_no)
            after = {"drying": False}
        elif action == "auto":
            params = auto_dry_frame(unit_no, bin_no, 1 if enable else 0)
            description = ("%s auto-dry on CFS %d bin %d"
                           % ("enable" if enable else "disable", unit_no, bin_no))
            after = {"auto_dry": bool(enable)}
        elif action == "start":
            types = list(material_types or [])
            if len(types) != 2:
                slots = [s for s in (table.slots if table else []) if s.unit == unit_no]
                pair = slots[0:2] if bin_no == 1 else slots[2:4]
                types = [(s.material_type or "PLA") for s in pair] or ["PLA", "PLA"]
            params = dry_box_frame(unit_no, bin_no, 1, float(target_temp),
                                   int(minutes), types[:2])
            description = ("start drying CFS %d bin %d at %.0f C for %d minutes"
                           % (unit_no, bin_no, float(target_temp), int(minutes)))
            after = {"drying": True, "target_temp": float(target_temp),
                     "minutes": int(minutes), "materials": types[:2]}
        else:
            return self._refused("dry", "Unknown drying action %r; expected "
                                        "start, stop or auto." % action, dry)

        result = self._writer().send(description, params, None, dry_run=dry)
        return self._finish("dry", description, result, {}, after)

    def undo_write(self, write_id: str, dry_run: Optional[bool] = None) -> dict:
        """Put back the values one recorded write replaced, where that exists."""
        with self.lock:
            record = next((w for w in self.cfs_writes if w.id == write_id), None)
        if record is None:
            return self._refused("edit", "That change is no longer listed.",
                                 bool(dry_run))
        if record.undone:
            return self._refused(record.kind, "That change was already undone.",
                                 bool(dry_run))
        if not record.sent:
            return self._refused(record.kind, "That was a dry run, so there is "
                                              "nothing to put back.",
                                 bool(dry_run))
        undoable, reason = cfs.UNDO_RULES.get(record.kind, (False, ""))
        if not undoable:
            return self._refused(record.kind, reason, bool(dry_run))

        before = dict(record.before)
        if record.kind in ("edit", "clear"):
            label = before.get("label") or ""
            fields = dict(before)
            fields["clear"] = not before.get("type")
            out = self.edit_slot(None, label, fields, dry_run=dry_run)
        else:
            out = self.set_box_config(before, dry_run=dry_run)
        if out.get("ok"):
            with self.lock:
                record.undone = True
                record.undoable = False
            out["message"] = "Put the previous values back. " + out["message"]
        return out

    def _dry(self, dry_run: Optional[bool]) -> bool:
        """A write is a dry run unless the facade is live and nobody asked."""
        if dry_run is not None:
            return bool(dry_run)
        return not self.live

    # -- uploads ----------------------------------------------------------

    def _store_pending(self, info: GcodeInfo, path: str, name: str, reason: str,
                       proposed: Optional[list[Assignment]] = None) -> PendingUpload:
        mapping = {a.tool: a.slot.label for a in (proposed or [])}
        item = PendingUpload(name, path, info, reason, mapping)
        with self.lock:
            self.pending = [p for p in self.pending if p.name != name]
            self.pending.append(item)
        log.info("pending: %s (%s)", name, reason)
        return item

    def handle_upload(self, local_path: str, name: str, start: bool) -> tuple[int, str]:
        """Returns (http status, message). Never raises for a mapping problem.

        Orca only shows the body of a non-2xx reply, and the whole point of the
        page is that it, not an error dialog, is where a choice gets made. So
        every outcome here is 201 and every explanation lands on the page.
        """
        info = parse_gcode(local_path)
        with self.lock:
            table = self.table
        if table is None:
            self.poll_once()
            with self.lock:
                table = self.table

        if table is None:
            self._store_pending(info, local_path, name, reason=(
                "The printer could not be read (%s), so no slot could be chosen. "
                "The file is safe here; press Start when the printer is back."
                % (self.last_error or "no answer yet")))
            return 201, "stored %s; the printer is not answering, choose slots on the Device tab" % name

        assignments: Optional[list[Assignment]] = None
        try:
            assignments = auto_map(info, table,
                                   allow_type_mismatch=self.allow_type_mismatch)
        except MappingError as exc:
            self._store_pending(info, local_path, name, reason=str(exc))
            return 201, ("stored %s; cfsbridge could not choose a slot on its own. "
                         "Open the Device tab and pick one. %s" % (name, exc))

        if not start:
            self._store_pending(info, local_path, name, proposed=assignments, reason=(
                "Uploaded with Print unticked, so nothing was started. The mapping "
                "below is what cfsbridge would use; press Start when you want it."))
            return 201, "stored %s; not started because print=false" % name

        printer = self.printer_json()
        if not self.live:
            self._store_pending(info, local_path, name, proposed=assignments, reason=(
                "cfsbridge is running without --live, so nothing was uploaded or "
                "started. This is the mapping it would have used."))
            return 201, ("stored %s; cfsbridge is not live. Mapping it would use: %s"
                         % (name, _summary(assignments)))
        if not printer["idle"]:
            self._store_pending(info, local_path, name, proposed=assignments, reason=(
                "The printer is busy (%s%s), so cfsbridge did not start this job. "
                "Press Start when the printer is free."
                % (printer["state_text"],
                   ", running " + printer["file_name"] if printer["file_name"] else "")))
            return 201, ("stored %s; the printer is busy (%s). Start it from the "
                         "Device tab when it is free." % (name, printer["state_text"]))

        return self._start(local_path, name, assignments, auto=True)

    def _start(self, local_path: str, name: str, assignments: list[Assignment],
               auto: bool) -> tuple[int, str]:
        explicit = ["T%d=%s" % (a.tool, a.slot.label) for a in assignments]
        log.info("starting %s with %s", name, _summary(assignments))
        try:
            plan = self.send_live_fn(
                self.host, local_path, explicit=explicit, auto=False,
                method=self.method, remote_name=name,
                allow_type_mismatch=self.allow_type_mismatch,
                moonraker_port=self.moonraker_port,
            )
        except Exception as exc:
            log.error("start of %s failed: %s", name, exc)
            return 201, "could not start %s: %s" % (name, exc)
        with self.lock:
            self.last_plan = plan
            self.recent = {
                "name": name,
                "auto": auto,
                "started_at": time.time(),
                "undone": False,
                "ok": True,
                "message": "",
                "remote_path": getattr(plan, "remote_path", ""),
                "mapping": [{
                    "tool": a.tool, "slot": a.slot.label,
                    "type": a.slot.material_type, "colour": a.slot.colour,
                    "reason": a.reason,
                } for a in assignments],
            }
        return 201, "started %s with %s" % (name, _summary(assignments))

    # -- page actions ------------------------------------------------------

    def find_pending(self, pending_id: str) -> Optional[PendingUpload]:
        with self.lock:
            for item in self.pending:
                if item.id == pending_id:
                    return item
        return None

    def discard_pending(self, pending_id: str) -> tuple[bool, str]:
        item = self.find_pending(pending_id)
        if item is None:
            return False, "that upload is no longer listed."
        with self.lock:
            self.pending = [p for p in self.pending if p.id != pending_id]
        try:
            os.unlink(item.path)
        except OSError:
            pass
        log.info("discarded %s", item.name)
        return True, "discarded %s." % item.name

    def start_pending(self, pending_id: str, mapping: dict) -> tuple[bool, str]:
        """Apply the slots chosen on the page and start the job."""
        item = self.find_pending(pending_id)
        if item is None:
            return False, "that upload is no longer listed."
        with self.lock:
            table = self.table
        if table is None:
            self.poll_once()
            with self.lock:
                table = self.table
        if table is None:
            return self._fail(item, "the printer is not answering (%s), so nothing "
                                    "was sent." % (self.last_error or "no answer"))

        assignments = []
        for tool in item.info.tools_used:
            label = str(mapping.get(str(tool), mapping.get(tool, "")) or "").strip()
            if not label:
                return self._fail(item, "no slot was chosen for T%d." % tool)
            slot = table.by_label(label) if _is_slot(label) else None
            if slot is None:
                return self._fail(item, "slot %s does not exist on this printer." % label)
            if not slot.present or not slot.material_type:
                return self._fail(item, "slot %s is empty. Load it, or set its "
                                        "material on the touchscreen." % label)
            extruder = (item.info.extruders[tool]
                        if tool < len(item.info.extruders) else None)
            assignments.append(Assignment(tool=tool, slot=slot,
                                          reason="chosen on the Device tab",
                                          extruder=extruder))
        labels = [a.slot.label for a in assignments]
        if len(set(labels)) != len(labels):
            return self._fail(item, "one slot cannot feed two tools at once; "
                                    "chosen: %s." % ", ".join(labels))

        if not self.live:
            item.ok = True
            item.message = ("cfsbridge is running without --live, so nothing was "
                            "uploaded or started. It would have used %s."
                            % _summary(assignments))
            return False, item.message

        printer = self.printer_json()
        if not printer["idle"]:
            return self._fail(item, "the printer is busy (%s). cfsbridge will not "
                                    "start a second job on top of it."
                                    % printer["state_text"])

        status, message = self._start(item.path, item.name, assignments, auto=False)
        if status == 201 and message.startswith("started"):
            with self.lock:
                self.pending = [p for p in self.pending if p.id != pending_id]
            return True, message
        return self._fail(item, message)

    def _fail(self, item: PendingUpload, message: str) -> tuple[bool, str]:
        item.ok = False
        item.message = message
        log.warning("%s: %s", item.name, message)
        return False, message

    def undo_recent(self) -> tuple[bool, str]:
        """Cancel the job cfsbridge just started, if it is safe to.

        Safe means: cfsbridge started it, inside the undo window, the printer
        has not begun heating for it, and no layer has been printed. Anything
        else is refused with the reason, because cancelling a running job is
        never this button's job.
        """
        with self.lock:
            recent = self.recent
        if recent is None:
            return False, "there is nothing to undo."
        if recent["undone"]:
            return False, "that job was already cancelled."
        left = self.undo_seconds - (time.time() - recent["started_at"])
        if left <= 0:
            return False, ("the %d second undo window has passed. Cancel from the "
                           "printer's screen instead." % int(self.undo_seconds))
        if not self.live:
            with self.lock:
                recent["undone"] = True
                recent["message"] = ("nothing was ever sent, because cfsbridge is "
                                     "not running with --live.")
            return True, recent["message"]

        self.poll_once()
        printer = self.printer_json()
        if not printer["reachable"]:
            return self._undo_fail(recent, "the printer is not answering, so "
                                           "nothing was cancelled.")
        if printer["progress"] > 0 or printer["layer"] > 0:
            return self._undo_fail(recent, (
                "the job is already running (%.0f%%, layer %d). cfsbridge never "
                "cancels a running print; stop it from the printer's screen if you "
                "really mean to." % (printer["progress"], printer["layer"])))
        if printer["nozzle_target"] > 0 or printer["bed_target"] > 0:
            return self._undo_fail(recent, (
                "the printer has already started heating (nozzle target %.0f C, bed "
                "target %.0f C). Undo only works before heating begins; stop it from "
                "the printer's screen instead."
                % (printer["nozzle_target"], printer["bed_target"])))
        running = printer["file_name"]
        if running and running != recent["name"]:
            return self._undo_fail(recent, (
                "the printer is running %s, which cfsbridge did not start. Nothing "
                "was cancelled." % running))
        try:
            self.stop_fn(self.host)
        except Exception as exc:
            return self._undo_fail(recent, "the cancel was refused: %s" % exc)
        with self.lock:
            recent["undone"] = True
            recent["ok"] = True
            recent["message"] = "cancelled before the printer started heating."
        log.info("undo: cancelled %s", recent["name"])
        return True, recent["message"]

    def _undo_fail(self, recent: dict, message: str) -> tuple[bool, str]:
        with self.lock:
            recent["ok"] = False
            recent["message"] = message
        log.warning("undo refused: %s", message)
        return False, message


def _unit_json(info) -> dict:
    return {
        "unit": info.unit,
        "present": info.present,
        "connected": info.present,
        "model": info.model,
        "model_name": info.model_name,
        "box_type": info.box_type,
        "serial": info.serial,
        "serial_short": info.serial_short,
        "temp": info.temp,
        "humidity": info.humidity,
        "ac": info.ac,
        "is_mini": info.is_mini,
        "can_dry": info.can_dry,
    }


def _slot_values(slot) -> dict:
    """The fields a `modifyMaterial` sets, as they are right now.

    This is what an undo writes back, so it carries exactly the arguments the
    edit takes and nothing else.
    """
    return {
        "label": slot.label,
        "type": slot.material_type,
        "vendor": slot.vendor,
        "name": slot.name,
        "colour": slot.colour or "",
        "rfid": slot.rfid,
        "min_temp": slot.min_temp if slot.min_temp is not None else 0,
        "max_temp": slot.max_temp if slot.max_temp is not None else 0,
        "pressure": 0.0,
    }


def _wanted_values(fields: dict, before: dict, items: list) -> dict:
    """Fill an edit in from the form, then from the filament database.

    The Creality dialog does not let anyone type a temperature: it picks brand,
    material type and name, and reads `rfid`, `minTemp`, `maxTemp` and
    `pressure` off the matching database entry. The card copies that, and then
    lets the two temperatures be overridden, which is the one thing the user
    asked for that Creality's own dialog will not do.
    """
    def pick(key, fallback=""):
        value = fields.get(key)
        return before.get(key, fallback) if value is None else value

    vendor = str(pick("vendor")).strip()
    material_type = str(pick("type")).strip()
    name = str(pick("name")).strip()
    entry = materials_db.find(items, vendor, material_type, name)

    colour = fields.get("colour", fields.get("color"))
    colour = before.get("colour", "") if colour is None else str(colour).strip()
    # An unreadable colour is left exactly as typed, so that
    # `creality_colour` refuses it by name rather than quietly writing "".
    colour = normalise_colour(colour) or ("" if not colour else colour)

    rfid = fields.get("rfid")
    if rfid is None:
        rfid = entry.rfid if entry else before.get("rfid", "")

    def temp(key, entry_value):
        value = fields.get(key)
        if value not in (None, ""):
            return _num(value)
        if entry_value is not None:
            return float(entry_value)
        return _num(before.get(key))

    return {
        "label": before.get("label", ""),
        "type": material_type,
        "vendor": vendor,
        "name": name or material_type,
        "colour": colour,
        "rfid": str(rfid or ""),
        "min_temp": temp("min_temp", entry.min_temp if entry else None),
        "max_temp": temp("max_temp", entry.max_temp if entry else None),
        "pressure": float(entry.pressure) if entry else 0.0,
    }


def _verify_slot(client, unit: int, index: int, wanted: dict):
    """Re-read `boxsInfo` and say whether the slot really changed.

    An ack is only the printer saying it heard. This is the printer saying it
    did it, which is the thing worth reporting.
    """
    got = build_slot_table(client.get_boxs_info(), None).get(unit, index)
    if got is None:
        return False, ("The printer no longer reports slot %d/%d at all."
                       % (unit, index)), None
    read = _slot_values(got)
    checks = [("type", (wanted.get("type") or "").upper(),
               (got.material_type or "").upper()),
              ("vendor", wanted.get("vendor") or "", got.vendor or ""),
              ("name", wanted.get("name") or "", got.name or "")]
    wrong = ["%s is %r, expected %r" % (field, actual, want)
             for field, want, actual in checks if want != actual]
    want_colour = normalise_colour(wanted.get("colour") or "") or ""
    if want_colour and (got.colour or "") != want_colour:
        wrong.append("colour is %r, expected %r" % (got.colour or "", want_colour))
    if wrong:
        return False, ("The printer accepted the message but slot %s still "
                       "reads back wrong: %s." % (got.label, "; ".join(wrong))), read
    return True, "Slot %s now reads back as written." % got.label, read


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _summary(assignments: list[Assignment]) -> str:
    return ", ".join("T%d to CFS %s" % (a.tool, a.slot.label) for a in assignments)


def _is_slot(label: str) -> bool:
    from .slots import SlotError, parse_slot
    try:
        parse_slot(label)
    except SlotError:
        return False
    return True


# -- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "cfsbridge/" + __version__
    protocol_version = "HTTP/1.1"
    facade: Facade = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args) -> None:
        log.warning("%s %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str,
              headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {"Cache-Control": "no-store"}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        # The fork is normally served by this bridge and talks to it with
        # relative URLs, but it can also be opened from the printer's own
        # Fluidd or from a dev server, with the bridge address typed into its
        # settings. Those are cross-origin GETs and POSTs of JSON to a
        # loopback service on a machine the user already controls, so they are
        # allowed rather than silently failing in the console.
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json",
                   {"Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS"})

    def do_OPTIONS(self) -> None:
        self._send(204, b"", "text/plain",
                   {"Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Max-Age": "600"})

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except ValueError:
            return {}

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        raw, _, query = self.path.partition("?")
        raw = urllib.parse.unquote(raw)
        path = raw.rstrip("/") or "/"
        try:
            # The forked Fluidd is a directory of files, so it needs the path
            # as sent, trailing slash and all, before the tidy-up below.
            if raw == fluidd_app.MOUNT or raw.startswith(fluidd_app.MOUNT + "/"):
                self._fluidd(raw)
                return
            self._get(path, urllib.parse.parse_qs(query))
        except Exception as exc:  # pragma: no cover - defensive
            log.error("GET %s failed: %s", path, traceback.format_exc())
            self._text(500, "cfsbridge failed: %s" % exc)

    def _fluidd(self, raw: str) -> None:
        """`GET /fluidd` and everything under it.

        Vite built the app with `base: './'`, so index.html asks for
        `./assets/...` and the app has to live at a path that ends in a slash.
        `/fluidd` therefore redirects rather than serving index.html, which
        also keeps the Device UI value in Orca (`http://127.0.0.1:7126/fluidd/`)
        working unchanged.
        """
        if raw == fluidd_app.MOUNT:
            self.send_response(302)
            self.send_header("Location", fluidd_app.MOUNT + "/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        rel = raw[len(fluidd_app.MOUNT) + 1:]
        status, body, ctype, headers = self.facade.fluidd.serve(rel)
        self._send(status, body, ctype, headers)

    def _get(self, path: str, query: Optional[dict] = None) -> None:
        query = query or {}
        if path in ("/api/version", "/api/v1/version"):
            self._json(200, OCTOPRINT_VERSION)
        elif path == "/server/info":
            self._json(200, {"result": {
                "klippy_connected": True, "klippy_state": "ready",
                "components": ["file_manager", "octoprint_compat"],
                "moonraker_version": "cfsbridge-" + __version__,
            }})
        elif path == "/server/files/roots":
            self._json(200, {"result": [
                {"name": "gcodes", "path": self.facade.spool_dir, "permissions": "rw"}
            ]})
        elif path == "/api/settings":
            self._json(200, {"webcam": {"webcamEnabled": False},
                             "feature": {"sdSupport": False}})
        elif path == "/api/job":
            printer = self.facade.printer_json()
            self._json(200, {"job": {"file": {"name": printer["file_name"] or None}},
                             "state": "Printing" if not printer["idle"] else "Operational",
                             "progress": {"completion": printer["progress"]}})
        elif path == "/cfs/slots":
            try:
                self._json(200, self.facade.slots_json())
            except (ProtocolError, OSError) as exc:
                self._text(502, "cannot read the CFS: %s" % exc)
        elif path == "/cfs/state":
            self._json(200, self.facade.state_json())
        elif path == "/cfs/card":
            # The CFS card's own state. Same cache as /cfs/state, without the
            # pending-upload work, because the card polls more often.
            self._json(200, {"ok": True, "now": time.time(),
                             "host": self.facade.host,
                             "version": __version__,
                             "printer": self.facade.printer_json(),
                             "cfs": self.facade.cfs_json()})
        elif path == "/camera":
            # A camera-only page with no page furniture, so that Fluidd's
            # "iframe" camera card can embed it and it looks like an ordinary
            # camera tile. Fluidd's own camera types cannot play this printer;
            # see docs/CAMERA.md section 6.
            # ?lan=<address> overrides the address the page writes into its ICE
            # candidates, for the case where the bridge cannot work out the
            # address of the machine the browser runs on. See camera.py.
            lan = (query.get("lan") or [""])[0].strip()
            bare = (query.get("bare") or [""])[0].strip() not in ("", "0", "false")
            self._send(200,
                       self.facade.camera_page(lan or None, bare).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/cfs/writes":
            # The write log on its own. `/cfs/card` carries it too; the CFS
            # page polls this one so that its Undo list keeps working when the
            # card is not on screen.
            self._json(200, self.facade.writes_json())
        elif path == "/updates":
            # Are the two forks still close to what they were forked from?
            # Cached six hours; ?refresh=1 forces. Never touches a clone.
            refresh = (query.get("refresh") or [""])[0].strip() not in (
                "", "0", "false")
            self._json(200, self.facade.updates.report(refresh=refresh))
        elif path == "/camera/local_address":
            # The forked Fluidd's camera component has to write this machine's
            # real LAN address into its ICE candidates, because Chromium hides
            # it behind an mDNS .local name the printer cannot resolve and the
            # printer handshakes to whatever the offer says. A page cannot read
            # its own LAN address, so it asks the bridge. docs/CAMERA.md 7.2.
            self._json(200, {"ok": True, "address": self.facade.local_ip(),
                             "printer": self.facade.host,
                             "signalling_url": camera.signalling_url(
                                 self.facade.host, CAMERA_PORT)})
        elif path == "/fluidd-shell":
            # The pre-fork page: the printer's own Fluidd in a frame with two
            # floating cards drawn on top. Kept because it is the only thing
            # that works against a checkout with no build in it, and because it
            # is the fallback if the fork ever misbehaves. docs/CAMERA.md 7.4.
            lan = (query.get("lan") or [""])[0].strip()
            self._send(200, self.facade.fluidd_page(lan or None).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/cfscard":
            # The CFS card on its own, with no page furniture, so the Fluidd
            # shell can frame it the way it frames the camera.
            bare = (query.get("bare") or [""])[0].strip() not in ("", "0", "false")
            self._send(200, cfs_card_page(bare=bare).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/camera/snapshot":
            # There is no still to serve. The printer has no JPEG endpoint, and
            # producing one here would mean holding a WebRTC session open with
            # aiortc, which is not a dependency of the bridge. docs/CAMERA.md.
            self._text(501, camera.mjpeg_note())
        elif path == "/health":
            self._json(200, {"ok": True, "version": __version__})
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path == "/setup":
            # The one page that works before a printer is known. Reachable at
            # any time, so a printer that moved can be re-pointed.
            self._send(200, setup_page().encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/setup":
            self._json(200, self._setup_state())
        elif path == "/":
            if not self.facade.host:
                # A fresh install has no printer to draw, so the first thing
                # anyone sees is the address box. Once an address is saved this
                # is the bridge page again.
                self._redirect("/setup")
            else:
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._text(404, "not found: %s" % path)

    def _setup_state(self) -> dict:
        """`GET /api/setup`: what the setup page fills its box in from.

        The running bridge's own address, not the file's, so that a `--host`
        flag or a save that has not been written yet still reads back as what
        the bridge is actually talking to.
        """
        host = self.facade.host or load_config().get("host", "")
        return {"host": host, "moonraker_port": self.facade.moonraker_port,
                "configured": bool(host)}

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            if path in ("/api/files/local", "/api/files/sdcard"):
                self._handle_upload()
            elif path == "/cfs/start":
                body = self._body_json()
                ok, message = self.facade.start_pending(
                    str(body.get("id", "")), body.get("map") or {})
                self._json(200, {"ok": ok, "message": message})
            elif path == "/cfs/discard":
                ok, message = self.facade.discard_pending(
                    str(self._body_json().get("id", "")))
                self._json(200, {"ok": ok, "message": message})
            elif path == "/cfs/undo":
                self._body_json()
                ok, message = self.facade.undo_recent()
                self._json(200, {"ok": ok, "message": message})
            elif path in ("/cfs/edit", "/cfs/config", "/cfs/feed",
                          "/cfs/refresh", "/cfs/dry", "/cfs/undo_write"):
                self._json(200, self._cfs_write(path, self._body_json()))
            elif path in ("/api/setup", "/api/setup/test"):
                # The body is read before the guard refuses anything, so that
                # a refused request still leaves the connection usable: an
                # unread body would be parsed as the next HTTP/1.1 request.
                body = self._body_json()
                if self._setup_guard():
                    if path == "/api/setup":
                        self._save_setup(body)
                    else:
                        self._test_setup(body)
            else:
                self._text(404, "not found: %s" % path)
        except MappingError as exc:
            # Orca shows the body of a failed upload to the user.
            self._text(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.error("POST %s failed: %s", path, traceback.format_exc())
            self._text(500, "cfsbridge failed: %s" % exc)

    # -- the setup page's two writes --------------------------------------

    def _setup_guard(self) -> bool:
        """May this request touch the setup? Answers it itself when not.

        `_json` sends `Access-Control-Allow-Origin: *` so the forked Fluidd
        can be opened from the printer's own web server, and `_body_json`
        takes any body. For a read that is fine. For the endpoint that
        re-points the bridge it is not: without this, any page in any other
        tab could POST a new printer address to 127.0.0.1:7126 while the user
        was looking at something else.

        Two checks, both cheap and both aimed at exactly that:

          * `Content-Type` must be JSON. A cross-site HTML form can only send
            the three form types, and anything else makes the browser ask for
            a preflight first, which this bridge answers for the origin check
            below to then reject.
          * an `Origin`, when the browser sent one, must be this bridge.

        A request with no `Origin` at all is left alone, because that is curl,
        the installer's smoke test and the service's own health check, none of
        which a web page can forge.

        Only the two `/api/setup` routes go through this. The `/cfs/*` writes
        are unchanged; they are a separate job.
        """
        ctype = self.headers.get("Content-Type", "") or ""
        if not ctype.strip().lower().startswith("application/json"):
            self._json(415, {"ok": False, "error": (
                "Send this as application/json; %r is not accepted."
                % (ctype or "no content type"))})
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            host = (self.headers.get("Host") or "").strip()
            allowed = {"http://" + host, "https://" + host} if host else set()
            if origin.rstrip("/") not in allowed:
                log.warning("refused a setup write from origin %s", origin)
                self._json(403, {"ok": False, "error": (
                    "This request came from %s, which is not this bridge. "
                    "Open the setup page at http://%s/setup and use that."
                    % (origin, host))})
                return False
        return True

    @staticmethod
    def _wanted_host(body: dict) -> tuple[str, str]:
        """(host, refusal). A refusal is a sentence for the setup page."""
        host = str(body.get("host") or "").strip()
        if not host:
            return "", ("Type the printer's address, for example "
                        "192.168.1.50.")
        if not HOST_RE.match(host):
            return host, ("%r is not an address or a hostname. Use the numbers "
                          "the printer shows under Settings, Network, for "
                          "example 192.168.1.50, with no http:// and no port."
                          % host)
        return host, ""

    def _save_setup(self, body: dict) -> None:
        """`POST /api/setup`: save the address and re-point the running bridge.

        A refused address is a 200 with `ok` false, because the page shows the
        reason and a 4xx would only give it an empty dialog. A config file that
        cannot be written is a real 500: it means the install is broken (an
        unwritable %ProgramData%, usually) and the message has to name the path
        so it can be fixed.
        """
        host, refusal = self._wanted_host(body)
        if refusal:
            self._json(200, {"ok": False, "host": host, "error": refusal})
            return
        try:
            path = save_config({"host": host})
        except OSError as exc:
            path = config_path()
            log.error("could not save the printer address to %s: %s", path, exc)
            self._json(500, {"ok": False, "host": host, "error": (
                "Could not save the printer address to %s: %s. The bridge is "
                "still running, but it will forget this address when it "
                "restarts." % (path, exc))})
            return
        self.facade.apply_host(host)
        log.info("setup: saved the printer address %s to %s", host, path)
        self._json(200, {"ok": True, "host": host, "config_path": path,
                         "error": ""})

    def _test_setup(self, body: dict) -> None:
        """`POST /api/setup/test`: is there a printer at this address?

        Saves nothing and changes nothing, so it is safe to press at any time,
        including against an address that turns out to be someone else's.
        """
        host, refusal = self._wanted_host(body)
        if refusal:
            self._json(200, {"ok": False, "hostname": "", "model": "",
                             "cfs_units": 0, "error": refusal})
            return
        self._json(200, probe_printer(host, self.facade.moonraker_port))

    def _cfs_write(self, path: str, body: dict) -> dict:
        """One place for every CFS write route, so they cannot drift apart.

        Each of them answers 200 with `{"ok", "refused", "sent", "dry_run",
        "message", "write"}`. A refusal is not an HTTP error: the page has to
        show the reason, and Orca's embedded browser swallows error bodies.
        """
        facade = self.facade
        dry_run = body.get("dry_run")
        if dry_run is not None:
            dry_run = _truthy(dry_run)
        unit, slot = body.get("unit"), body.get("slot")
        if path == "/cfs/edit":
            return facade.edit_slot(unit, slot, body, dry_run=dry_run)
        if path == "/cfs/config":
            return facade.set_box_config(body, dry_run=dry_run)
        if path == "/cfs/feed":
            return facade.feed_slot(unit, slot, _truthy(body.get("feed")),
                                    dry_run=dry_run)
        if path == "/cfs/refresh":
            return facade.refresh_slot(unit, slot, dry_run=dry_run)
        if path == "/cfs/dry":
            return facade.dry_unit(
                unit, str(body.get("action") or "start"),
                bin_no=int(_num(body.get("bin"), 1)),
                target_temp=_num(body.get("target_temp"), 45.0),
                minutes=int(_num(body.get("minutes"), 360)),
                material_types=body.get("materials"),
                enable=_truthy(body.get("enable")), dry_run=dry_run)
        return facade.undo_write(str(body.get("id", "")), dry_run=dry_run)

    def _handle_upload(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._text(400, "expected multipart/form-data")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        fields, files = parse_multipart(ctype, body)
        start = fields.get("print", "false").strip().lower() in ("true", "1", "yes")
        if "file" not in files:
            self._text(400, "no file in the upload")
            return
        filename, payload = files["file"]
        name = os.path.basename(filename)
        local = os.path.join(self.facade.spool_dir, name)
        with open(local, "wb") as handle:
            handle.write(payload)
        log.info("upload: %s (%s), print=%s", name, _human(len(payload)), start)

        status, message = self.facade.handle_upload(local, name, start)
        if status >= 400:
            self._text(status, message)
        else:
            self._json(status, {"done": True, "message": message,
                                "files": {"local": {"name": name}}})


def build_server(facade: Facade, bind: str = "127.0.0.1",
                 port: int = 0) -> ThreadingHTTPServer:
    """Bind a server around one facade. Port 0 asks the OS for a free port,
    which is how the tests get a real HTTP surface with no printer in sight."""
    Handler.facade = facade
    return ThreadingHTTPServer((bind, port), Handler)


def make_server(host: str = "", bind: str = "127.0.0.1", port: int = 7126,
                method: str = "colormatch", live: bool = False,
                allow_type_mismatch: bool = False,
                moonraker_port: int = MOONRAKER_PORT,
                spool_dir: Optional[str] = None,
                log_path: Optional[str] = None,
                poll_interval: float = POLL_SECONDS,
                fluidd_dist: Optional[str] = None,
                dry_run: bool = False) -> ThreadingHTTPServer:
    """A bound but unstarted server, with its facade on `server.facade`.

    `serve()` is this plus the poller, the log lines and `serve_forever`. The
    split exists so the tests can drive the real HTTP surface without any of
    that; `dry_run=True` is their guarantee that nothing reaches a printer,
    whatever address gets saved through it: no poller, and no live writes.
    """
    live = live and not dry_run
    facade = Facade(host, method, live, allow_type_mismatch, moonraker_port,
                    spool_dir, poll_interval=poll_interval, log_path=log_path,
                    fluidd_dist=fluidd_dist, bind=bind, port=port)
    facade.dry_run = dry_run
    server = build_server(facade, bind, port)
    server.facade = facade  # type: ignore[attr-defined]
    return server


def apply_host(host: str) -> bool:
    """Re-point the facade this process is serving. See `Facade.apply_host`."""
    facade = Handler.facade
    if facade is None:
        return False
    return facade.apply_host(host)


def serve(host: str, bind: str = "127.0.0.1", port: int = 7126,
          method: str = "colormatch", live: bool = False,
          allow_type_mismatch: bool = False,
          moonraker_port: int = MOONRAKER_PORT,
          spool_dir: Optional[str] = None,
          log_path: Optional[str] = None,
          poll_interval: float = POLL_SECONDS,
          fluidd_dist: Optional[str] = None,
          sync_webcams: bool = True) -> int:
    try:
        server = make_server(
            host, bind, port, method, live, allow_type_mismatch, moonraker_port,
            spool_dir, log_path=log_path, poll_interval=poll_interval,
            fluidd_dist=fluidd_dist)
    except OSError as exc:
        # The autostart task fires every minute, so this happens 1439 times a
        # day on a healthy machine. At debug level it stays out of the way of
        # the lines that mean something.
        log.debug("not starting: %s:%d is already in use (%s)", bind, port, exc)
        return 0
    facade = server.facade  # type: ignore[attr-defined]
    # No address means no poller: it would only retry Moonraker at "".
    facade.start_poller()
    if host:
        log.info("cfsbridge %s serving on http://%s:%d, printer %s, method %s, live %s",
                 __version__, bind, port, host, method, "yes" if live else "no (dry run)")
    else:
        log.info("cfsbridge %s serving on http://%s:%d; no printer configured; "
                 "open http://%s:%d/setup", __version__, bind, port,
                 bind if bind not in ("", "0.0.0.0", "::") else "127.0.0.1", port)
    if facade.fluidd.available:
        log.info("forked Fluidd from %s at http://%s:%d/fluidd/",
                 facade.fluidd.dist, bind, port)
    else:
        log.warning("no forked Fluidd build at %s; /fluidd answers 503 and the "
                    "fallback pages /camera and /cfscard still work",
                    facade.fluidd.dist)
    if sync_webcams and host:
        # One Moonraker database write at most, and only when the stored record
        # is not the one the fork's camera card needs. Off the request path so
        # a quiet printer cannot delay the first page. There is nothing to sync
        # until a printer address exists; the setup page's save does it next
        # time the bridge starts.
        threading.Thread(target=facade.sync_webcams, name="webcam-sync",
                         daemon=True).start()
    log.info("Orca: printer settings, host type OctoPrint, address http://%s:%d, "
             "web UI http://%s:%d/, API key anything.",
             bind if bind != "0.0.0.0" else "127.0.0.1", port,
             bind if bind != "0.0.0.0" else "127.0.0.1", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        facade.stop_poller()
        server.server_close()
    return 0
