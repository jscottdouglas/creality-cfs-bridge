"""Creality LAN protocol client (ws://<ip>:9999) plus the port 80 uploader.

Everything here is derived from CrealityPrint's own device manager bundle; see
docs/PROTOCOL.md for the citations and for the live captures that confirm it.

Safety: `CrealityClient.get()` is the only method that talks to the printer on
its own. Every method that sends a `set` message goes through `_send_set()`,
which refuses to do anything at all when the client was built with
`dry_run=True`; it records the message instead. The CLI passes `dry_run=True`
unless the user explicitly asked for a live run.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import CREALITY_PORT, UPLOAD_PORT

# Every `params` key the printer accepts that only reads state. Anything not in
# this set is treated as a write.
READ_ONLY_PARAMS = {
    "boxsInfo",
    "boxConfig",
    "reqGcodeFile",
    "reqGcodeList",
    "reqMaterials",
    "getToken",
    "nozzleList",
    "nozzleState",
    "nozzleFilament",
    "reqPrintObjects",
    "materialBinStatus",
}


class ProtocolError(RuntimeError):
    pass


# -- CFS write frames ------------------------------------------------------
#
# Every builder below returns the exact `params` object CrealityPrint's own
# device manager bundle builds, with the keys in the same order, so that the
# serialised JSON can be compared against the citation character for
# character. Citations are into
# `CP/resources/web/deviceMgr/assets/C_Lh0d9R.js`; docs/PROTOCOL.md section 6
# quotes the source of each one.

def creality_colour(colour: str) -> str:
    """'#RRGGBB' -> '#0RRGGBB', the shape the printer stores.

    The client inserts the extra zero on the way out:
    `let $ = y.color.slice(0,1) + "0" + y.color.slice(1)` (`SetMaterials`).
    An empty string stays empty, because that is how a slot is cleared.
    """
    text = (colour or "").strip()
    if not text:
        return ""
    if not text.startswith("#"):
        text = "#" + text
    body = text[1:]
    if len(body) == 7 and body[0] == "0":
        return "#0" + body[1:].upper()   # already in the printer's own shape
    if len(body) != 6:
        raise ProtocolError("bad colour %r: expected #RRGGBB" % colour)
    try:
        int(body, 16)
    except ValueError:
        raise ProtocolError("bad colour %r: expected #RRGGBB" % colour) from None
    return "#0" + body.upper()


def float_temp(value) -> float:
    """`Number(x) + 1e-8`, which is only there to force a float in the JSON.

    `SetMaterials`: `minTemp: Number(y.minTemp)+1e-8`. The printer is handed a
    float, never an integer, and this is how the client guarantees one.
    """
    return float(value) + 1e-8


def modify_material_frame(box_id: int, material_id: int, *, rfid: str = "",
                          material_type: str = "", vendor: str = "",
                          name: str = "", colour: str = "",
                          min_temp: float = 0.0, max_temp: float = 0.0,
                          pressure: float = 0.0,
                          box_type: Optional[int] = None) -> dict:
    """`{"modifyMaterial": {...}}`, the slot edit. `DeviceInterface.SetMaterials`.

    `box_type` appears only on the CFS Mini variant
    (`DeviceInterface.SetCfsMiniMaterials`), where it carries the box's own
    `materialBoxs[].type`. The plain CFS (`materialBoxName` `MF003`, which is
    what this printer has) uses the form without it, so the caller passes
    `box_type=None` and the key is left out entirely.
    """
    inner: dict = {"boxId": int(box_id)}
    if box_type is not None:
        inner["boxType"] = int(box_type)
    inner["id"] = int(material_id)
    inner["rfid"] = rfid or ""
    inner["type"] = material_type or ""
    inner["vendor"] = vendor or ""
    inner["name"] = name or ""
    inner["color"] = creality_colour(colour)
    inner["minTemp"] = float_temp(min_temp)
    inner["maxTemp"] = float_temp(max_temp)
    inner["pressure"] = float(pressure or 0.0)
    return {"modifyMaterial": inner}


def reset_material_frame(box_id: int, material_id: int,
                         box_type: Optional[int] = None) -> dict:
    """Clear a slot. `DeviceInterface.ResetMaterials`.

    The same message with every string empty and every number a plain `0`. The
    temperatures here are integers, not the `+1e-8` floats the set form uses;
    that is how the client writes it.
    """
    inner: dict = {"boxId": int(box_id)}
    if box_type is not None:
        inner["boxType"] = int(box_type)
    inner["id"] = int(material_id)
    inner["rfid"] = ""
    inner["type"] = ""
    inner["vendor"] = ""
    inner["name"] = ""
    inner["color"] = ""
    inner["minTemp"] = 0
    inner["maxTemp"] = 0
    inner["pressure"] = 0
    return {"modifyMaterial": inner}


def box_config_frame(auto_refill: int, auto_feed: int, self_test: int,
                     ignore_colour_auto_feed: Optional[int] = None) -> dict:
    """`{"boxConfig": {...}}`. `DeviceInterface.SetBoxsConfig`.

    **There is no box id.** The setting is printer-wide, not per CFS unit: the
    client's own call takes an address and three flags and nothing else, and
    the reply to `{"get": {"boxConfig": 1}}` is likewise a single object. A
    CFS-C adds `ignoreColorAutoFeed` (`SetBoxsCfscConfig`) and a CFS-Lite sends
    only `cAutoFeed` and `autoRefill` (`SetBoxsCfsLiteConfig`).

    `cAutoUpdateFilament` is reported by the printer but **never written by any
    code path in the bundle**, so there is no documented write for it.
    """
    inner = {
        "autoRefill": int(auto_refill),
        "cAutoFeed": int(auto_feed),
        "cSelfTest": int(self_test),
    }
    if ignore_colour_auto_feed is not None:
        inner["ignoreColorAutoFeed"] = int(ignore_colour_auto_feed)
    return {"boxConfig": inner}


def feed_frame(box_id: int, material_id: int, is_feed: int) -> dict:
    """`{"feedInOrOut": {...}}`. `DeviceInterface.feedInOrOutMaterial`.

    `isFeed` is 1 to load the slot to the toolhead and 0 to unload it.
    """
    return {"feedInOrOut": {"boxId": int(box_id),
                            "materialId": int(material_id),
                            "isFeed": int(is_feed)}}


def refresh_frame(box_id: int, material_id: int) -> dict:
    """`{"refreshBox": {...}}`, re-read the slot's RFID tag.
    `DeviceInterface.SetRFIDRefresh`."""
    return {"refreshBox": {"boxId": int(box_id), "materialId": int(material_id)}}


# Drying. `addr` is the CFS unit id, the same number as `materialBoxs[].id`:
# the drying page finds its unit with
# `materialBoxs.find(b => b.id === currentUsedCFSIndex)` and hands that same
# `currentUsedCFSIndex` straight to `setDryBoxState`. `num` is the drying bin
# inside the unit, 1 for slots A and B and 2 for slots C and D (the page reads
# `materials[binNum === 1 ? i : i + 2]`), and `materialType` carries one entry
# per slot in that bin.

DRY_STOP_TEMP = 35
DRY_STOP_TIME = 10


def dry_box_frame(addr: int, num: int, dry_mode: int, target_temp: float,
                  total_time: int, material_types: list) -> dict:
    """`{"dryBox": {...}}`, start drying. `DeviceInterface.setDryBoxState`.

    `total_time` is in **minutes**: the page computes it as
    `Number(selectedHours) * 60`. `dry_mode` is 1 to run.
    """
    return {"dryBox": {"addr": int(addr), "num": int(num),
                       "dryMode": int(dry_mode),
                       "targetTemp": float(target_temp),
                       "totalTime": int(total_time),
                       "materialType": list(material_types)}}


def dry_stop_frame(addr: int, num: int) -> dict:
    """`{"dryBox": {...}}` with `dryMode` 0, which is how the client stops it.

    `DeviceInterface.setDryStopState` sends exactly these constants. The
    temperature, time and material list mean nothing once the mode is 0, but
    they are sent, so they are sent here too.
    """
    return {"dryBox": {"addr": int(addr), "num": int(num), "dryMode": 0,
                       "targetTemp": DRY_STOP_TEMP, "totalTime": DRY_STOP_TIME,
                       "materialType": ["PLA", "PLA"]}}


def auto_dry_frame(addr: int, keep_dry_bin_choice: int, enable: int) -> dict:
    """`{"autoDry": {...}}`. `DeviceInterface.setAutoDryState`.

    `keepDryBinChoice` is the bin, 1 or 2, or 3 for both bins at once (the page
    passes 3 when the other bin already has auto-dry switched on).
    """
    return {"autoDry": {"addr": int(addr),
                        "keepDryBinChoice": int(keep_dry_bin_choice),
                        "enable": int(enable)}}


class DryRunRefusal(RuntimeError):
    """Raised when a write was attempted on a dry-run client."""


@dataclass
class PendingMessage:
    """A `set` message that a dry-run client declined to send."""

    description: str
    payload: dict

    def as_json(self) -> str:
        return json.dumps(self.payload, separators=(",", ":"))


class CrealityClient:
    """A blocking wrapper around the printer's WebSocket.

    The printer pushes unsolicited state constantly and never correlates a
    reply to a request, so `get()` sends the request then waits for the first
    frame that carries the expected reply key.
    """

    def __init__(
        self,
        host: str,
        port: int = CREALITY_PORT,
        dry_run: bool = True,
        timeout: float = 15.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.dry_run = dry_run
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.pending: list[PendingMessage] = []
        self._ws = None
        self._frames: list[dict] = []
        self._state: dict = {}
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._on_frame: Optional[Callable[[dict], None]] = None

    # -- connection -------------------------------------------------------

    def __enter__(self) -> "CrealityClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        try:
            from websocket import create_connection  # websocket-client
        except ImportError:
            create_connection = None

        if create_connection is not None:
            self._ws = create_connection(self.url, timeout=self.connect_timeout)
            self._ws.settimeout(1.0)
            self._backend = "websocket-client"
        else:
            self._connect_websockets()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Give the printer a moment to push its opening state dump.
        deadline = time.time() + 3.0
        while time.time() < deadline and not self._state:
            time.sleep(0.05)

    def _connect_websockets(self) -> None:
        """Fallback onto the `websockets` package via its sync bridge."""
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProtocolError(
                "no websocket client available: install either 'websockets' or "
                "'websocket-client' into the venv"
            ) from exc
        self._ws = connect(self.url, open_timeout=self.connect_timeout)
        self._backend = "websockets"

    @property
    def url(self) -> str:
        return "ws://%s:%d" % (self.host, self.port)

    def close(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    # -- reading ----------------------------------------------------------

    def _recv(self) -> Optional[str]:
        try:
            if self._backend == "websocket-client":
                return self._ws.recv()
            return self._ws.recv(timeout=1.0)
        except Exception:
            return None

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            raw = self._recv()
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            if raw.strip() == "ok":
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            with self._lock:
                self._frames.append(frame)
                self._state.update(frame)
            if self._on_frame is not None:
                try:
                    self._on_frame(frame)
                except Exception:
                    pass

    def snapshot(self) -> dict:
        """Everything the printer has told us so far, merged."""
        with self._lock:
            return dict(self._state)

    # -- requests ---------------------------------------------------------

    def get(self, name: str, arg: Any = 1, reply_key: Optional[str] = None) -> dict:
        """Send one read request and wait for its reply.

        Refuses to send anything that is not a known read.
        """
        if name not in READ_ONLY_PARAMS:
            raise ProtocolError(
                "%r is not a known read-only request; refusing to send it" % name
            )
        key = reply_key or _REPLY_KEYS.get(name, name)
        with self._lock:
            self._state.pop(key, None)
        self._send_raw({"method": "get", "params": {name: arg}})
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            with self._lock:
                if key in self._state:
                    return {key: self._state[key]}
            time.sleep(0.05)
        raise ProtocolError("no %r reply from %s within %.0fs" % (key, self.host, self.timeout))

    def get_boxs_info(self) -> dict:
        return self.get("boxsInfo")

    def get_box_config(self) -> dict:
        return self.get("boxConfig")

    def get_gcode_root(self) -> str:
        """Ask the printer where uploaded G-code lands, do not assume.

        Returns a directory path with no trailing slash. Falls back to the K2
        family default if the printer has no files yet.
        """
        try:
            reply = self.get("reqGcodeFile")
        except ProtocolError:
            return "/mnt/UDISK/printer_data/gcodes"
        entries = reply.get("retGcodeFileInfo2") or []
        for entry in entries:
            path = entry.get("path")
            if path and "/" in path:
                return path.rsplit("/", 1)[0]
        return "/mnt/UDISK/printer_data/gcodes"

    def _send_raw(self, payload: dict) -> None:
        if self._ws is None:
            raise ProtocolError("not connected")
        self._ws.send(json.dumps(payload, separators=(",", ":")))

    def _send_set(self, description: str, params: dict) -> PendingMessage:
        """Record, and only actually send when this client is not a dry run."""
        message = PendingMessage(description, {"method": "set", "params": params})
        self.pending.append(message)
        if self.dry_run:
            return message
        self._send_raw(message.payload)
        return message

    # -- writes (all guarded by dry_run) ----------------------------------

    def set_color_match(self, path: str, entries: list[dict]) -> PendingMessage:
        """Install the gcode-slot to physical-slot map for one job.

        `entries` are {"id": "T1A", "type": "PETG", "color": "#RRGGBB",
        "boxId": 2, "materialId": 1}. See docs/PROTOCOL.md section 4.2.
        """
        return self._send_set(
            "set the filament slot mapping for %s" % path,
            {"colorMatch": {"path": path, "list": entries}},
        )

    def start_normal_print(self, path: str, self_test: int = 0) -> PendingMessage:
        return self._send_set(
            "start printing %s" % path,
            {"opGcodeFile": "printprt:" + path, "enableSelfTest": int(self_test)},
        )

    def start_multi_color_print(self, path: str, self_test: int = 0) -> PendingMessage:
        return self._send_set(
            "start printing %s in CFS multi-colour mode" % path,
            {"multiColorPrint": {"gcode": path, "enableSelfTest": int(self_test)}},
        )

    # -- CFS writes -------------------------------------------------------
    #
    # One method per documented frame. Each one builds its params with the
    # matching builder above and hands it to `_send_set`, so a dry-run client
    # records it and sends nothing, exactly like every other write here.

    def send_and_wait(self, description: str, params: dict,
                      reply_key: Optional[str] = None,
                      timeout: Optional[float] = None) -> tuple:
        """Send one `set` and wait for the printer's echo of `reply_key`.

        Returns `(message, reply)`. `reply` is None on a dry-run client, which
        sent nothing, and None when the printer said nothing within the
        timeout. The printer correlates nothing, so "the reply" is the first
        frame after the send that carries the key.
        """
        if reply_key:
            with self._lock:
                self._state.pop(reply_key, None)
        message = self._send_set(description, params)
        if self.dry_run or not reply_key:
            return message, None
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        while time.time() < deadline:
            with self._lock:
                if reply_key in self._state:
                    return message, {reply_key: self._state[reply_key]}
            time.sleep(0.05)
        return message, None

    def modify_material(self, box_id: int, material_id: int, **fields) -> PendingMessage:
        """Set one slot's material by hand. docs/PROTOCOL.md section 6."""
        return self._send_set(
            "set CFS %d slot %d to %s %s"
            % (box_id, material_id, fields.get("vendor") or "?",
               fields.get("name") or fields.get("material_type") or "?"),
            modify_material_frame(box_id, material_id, **fields),
        )

    def reset_material(self, box_id: int, material_id: int,
                       box_type: Optional[int] = None) -> PendingMessage:
        return self._send_set(
            "clear CFS %d slot %d" % (box_id, material_id),
            reset_material_frame(box_id, material_id, box_type),
        )

    def set_box_config(self, auto_refill: int, auto_feed: int, self_test: int,
                       ignore_colour_auto_feed: Optional[int] = None) -> PendingMessage:
        return self._send_set(
            "set the CFS options: autoRefill %d, cAutoFeed %d, cSelfTest %d"
            % (auto_refill, auto_feed, self_test),
            box_config_frame(auto_refill, auto_feed, self_test,
                             ignore_colour_auto_feed),
        )

    def feed_in_or_out(self, box_id: int, material_id: int, is_feed: int) -> PendingMessage:
        return self._send_set(
            "%s CFS %d slot %d" % ("load" if is_feed else "unload", box_id, material_id),
            feed_frame(box_id, material_id, is_feed),
        )

    def refresh_box(self, box_id: int, material_id: int) -> PendingMessage:
        return self._send_set(
            "re-read the RFID tag in CFS %d slot %d" % (box_id, material_id),
            refresh_frame(box_id, material_id),
        )

    def start_drying(self, addr: int, num: int, target_temp: float,
                     total_time: int, material_types: list,
                     dry_mode: int = 1) -> PendingMessage:
        return self._send_set(
            "start drying CFS %d bin %d at %.0f C for %d minutes"
            % (addr, num, target_temp, total_time),
            dry_box_frame(addr, num, dry_mode, target_temp, total_time,
                          material_types),
        )

    def stop_drying(self, addr: int, num: int) -> PendingMessage:
        return self._send_set("stop drying CFS %d bin %d" % (addr, num),
                              dry_stop_frame(addr, num))

    def set_auto_dry(self, addr: int, keep_dry_bin_choice: int,
                     enable: int) -> PendingMessage:
        return self._send_set(
            "%s auto-dry on CFS %d bin %d"
            % ("enable" if enable else "disable", addr, keep_dry_bin_choice),
            auto_dry_frame(addr, keep_dry_bin_choice, enable),
        )

    def stop_print(self) -> PendingMessage:
        """Cancel the current job. Only ever used by the page's Undo button.

        The caller is responsible for checking that the job it is cancelling is
        the one it just started and that it has not begun heating; this method
        does no checking of its own, and like every other write it does nothing
        at all on a dry-run client. See `serve.Facade.undo_recent`.
        """
        return self._send_set("cancel the current job", {"stop": 1})


_REPLY_KEYS = {
    "reqGcodeFile": "retGcodeFileInfo2",
    "reqMaterials": "retMaterials",
    "getToken": "videoToken",
}


# -- upload ---------------------------------------------------------------

@dataclass
class UploadResult:
    status: int
    body: str
    url: str
    sent: bool = True
    remote_path: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and "ok" in self.body.lower()


def upload_gcode(
    host: str,
    local_path: str,
    remote_name: Optional[str] = None,
    port: int = UPLOAD_PORT,
    dry_run: bool = True,
    timeout: float = 600.0,
    progress: Optional[Callable[[int, int], None]] = None,
) -> UploadResult:
    """POST a G-code file the way CrealityPrint does.

    `http://<ip>:80/upload/<url-encoded name>`, multipart/form-data, and the
    form field name is the file name itself, not "file". See
    CrealityPrint/src/slic3r/GUI/print_manage/Device/Klipper4408Interface.cpp:37.
    """
    import os
    import urllib.parse

    name = remote_name or os.path.basename(local_path)
    url = "http://%s:%d/upload/%s" % (host, port, urllib.parse.quote(name))
    size = os.path.getsize(local_path)

    if dry_run:
        return UploadResult(
            status=0,
            body="(dry run, not sent: %d bytes)" % size,
            url=url,
            sent=False,
        )

    import requests

    with open(local_path, "rb") as handle:
        files = {name: (name, handle, "application/octet-stream")}
        response = requests.post(url, files=files, timeout=timeout)
    if progress is not None:
        progress(size, size)
    return UploadResult(status=response.status_code, body=response.text[:500], url=url)
