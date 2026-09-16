# Creality K2 Plus LAN protocol

Target: Creality K2 Plus at `192.168.1.50` (substitute your own printer's address), firmware model
id `F008`.

Source of every claim is either a `path:line` citation into one of the reference source trees, or a live
capture. Live captures were taken while the printer was mid-print, using read-only requests only
(`{"method":"get",...}` on the LAN socket, HTTP GET on Moonraker). Nothing in this document that writes
to the printer was executed.

Capture file names below (`captures/creality_boxsInfo.json` and friends) name the raw session logs
recorded while the protocol was worked out. They are cited for provenance; the raw logs themselves are
not published, because they contain one particular printer's identifiers.

Reference roots, each a clone of the upstream source:

- `CP` = CrealityPrint (Creality's slicer, source release)
- `OS` = OrcaSlicer (SoftFever)
- `KL` = K2_Series_Klipper (Creality's Klipper fork for the K2 family)

## 0. Summary of the wire surface

| Port | Service | Verified |
| --- | --- | --- |
| 80 | Creality file upload (`POST /upload/<name>`), also serves `/downloads/...` | open, `GET /` returns 404 (`captures/port_and_http_scan.txt`) |
| 443 | TLS, not used by the slicer path | open |
| 4408 | Fluidd web UI | open |
| 7125 | Moonraker HTTP and websocket, OctoPrint compatibility on | open, `captures/octoprint_api_version.json` |
| 8000 | camera WebRTC signalling (`POST /call/webrtc_local`) | open, see `docs/CAMERA.md` |
| 9999 | Creality LAN protocol, plain WebSocket, JSON frames, no auth | open, `captures/creality_ws9999_session.jsonl` |

`CP/src/slic3r/GUI/print_manage/AppUtils.cpp:351` checks exactly ports 80 and 9999 to decide a K2 is
reachable on the LAN (`const int ports_to_check[] = { 80, 9999 };`).

## 1. Connection, handshake, auth, keepalive

The client is a plain WebSocket with no subprotocol, no handshake message and no authentication.

`CP/resources/web/deviceMgr/assets/C_Lh0d9R.js` (minified bundle, byte offset 3991380, `class Client`):

```js
class Client {
  constructor(n, i = 5, y = 1000) {
    this.url = "ws://" + n + ":9999";   // n = printer IP
    ...
  }
  reconnect() { this.socket = new WebSocket(this.url); ... }
}
```

Behaviour taken from the same class:

- Connect timeout: 5000 ms. If the socket is still `CONNECTING` after 5 s it is closed.
- Read watchdog: every received frame resets a 20000 ms timer; on expiry with the socket still `OPEN`
  the client reconnects. There is no application-level ping. The printer pushes telemetry roughly twice
  a second, which is what keeps the watchdog fed.
- Reconnect backoff: `initialDelay * 2**retryCount`, initial 1000 ms, max 5 retries, then the printer is
  marked offline. Close code 1006 triggers the backoff path; any other close marks offline immediately.
- A received frame whose body is the literal string `ok` is an acknowledgement and is not parsed as JSON
  (`n.data != "ok" && dataCenter.setDataFromDevice(...)`).

The C++ side never opens this socket itself. `CP/src/slic3r/GUI/print_manage/App/PrinterMgrView.cpp:81`
installs a `ProxyWebSocket` shim into the embedded WebView and relays frames over `boost::beast::websocket`
(`PrinterMgrView.cpp:141` onwards), so the protocol lives entirely in the bundled JS.

**Live verification.** `captures/creality_ws9999_session.jsonl` is a full session: connect, receive the
initial state frame, send four `get` messages, receive their replies. No credentials were supplied and
the connection was accepted immediately.

### Frame shapes

Client to printer, only two forms exist:

```json
{"method": "get", "params": { "<name>": <arg>, ... }}
{"method": "set", "params": { "<name>": <arg>, ... }}
```

Printer to client: a bare JSON object of changed fields. There is no envelope, no request id and no
correlation between a `get` and its reply beyond the key name. The first frame after connect is a full
state dump (79 keys in `captures/creality_initial_state.json`); afterwards the printer sends only deltas,
for example `{"nozzleTemp": "225.29"}`.

`{"connectionCount": 1}` arrives shortly after connect and reports how many clients are attached.

## 2. Reading CFS state

### 2.1 The read

```json
{"method": "get", "params": {"boxsInfo": 1}}
```

`CP/.../C_Lh0d9R.js`, `DeviceInterface.GetBoxsInfo`:
`i.socket.sendMsg({method:"get",params:{boxsInfo:1}})`.

The application actually issues one combined request on connect, `DeviceInterface.ReqInit`:

```json
{"method":"get","params":{"reqGcodeFile":1,"reqGcodeList":1,"reqMaterials":1,"boxsInfo":1,"boxConfig":1,"getToken":1}}
```

All six are reads. Verified live one at a time, see `captures/creality_ws9999_session.jsonl`.

### 2.2 The reply

Full live reply: `captures/creality_boxsInfo.json`. Shape:

```jsonc
{"boxsInfo": {
  "enable": 1,
  "same_material": [ ["0P1003", "0ba552a", [{"boxId":1,"materialId":0}], "PLA"], ... ],
  "colorMatch":  [ {"id":"T1A","boxId":1,"materialId":0} ],
  "materialBoxs": [
    {"id":0, "state":0, "type":1, "materials":[ {...} ]},                       // the EXT spool holder
    {"id":1, "materialBoxName":"MF003", "ac":0, "state":1, "type":0,
     "temp":20.0, "humidity":43.0, "sn":"...XXXXXX",
     "materials":[
       {"id":0,"vendor":"Polymaker","type":"PLA","name":"Panchroma PLA Matte",
        "rfid":"P1003","color":"#0ba552a","minTemp":190,"maxTemp":230,
        "percent":100,"state":1,"selected":0,"editStatus":1,"scrap":0},
       ... ids 1..3 ...
     ]},
    ... ids 2,3,4 ...
  ]
}}
```

Field meanings, cross-checked against `CP/src/slic3r/GUI/print_manage/data/DataType.cpp:149` (the C++
deserialiser for the same JSON) and the live capture:

| Field | Meaning |
| --- | --- |
| `materialBoxs[].id` | `0` is the external spool holder (EXT), `1..4` are CFS units T1..T4 |
| `materialBoxs[].type` | `0` = CFS unit, `1` = external holder. `DataType.cpp:156` reads this into `box_type` |
| `materialBoxs[].state` | `1` = unit present on the bus, `0` = absent |
| `materialBoxs[].materialBoxName` | hardware id: `MF003` CFS, `MF040` CFS Lite, `MF046` CFS Mini, `MF049` CFS Nano, `MF042` CFS Pro, `MF050` CFS-C, `MF054` CFS Nano2. Table at `C_Lh0d9R.js` `CFS_NAME` |
| `materialBoxs[].sn`, `temp`, `humidity` | unit serial, chamber temperature in C, relative humidity in percent |
| `materialBoxs[].ac` | mains power to the unit's drying cabinet. `0` means none, and the drying page refuses to start on it. All four units here report `0` |
| `materials[].minTemp`, `maxTemp` | the slot's own temperature limits, written by the last tag read or slot edit. Section 6.1 |
| `materials[].id` | slot index inside the unit, `0..3` = A..D |
| `materials[].type` | filament type string, e.g. `PLA`, `PETG`. Empty string means the slot is empty |
| `materials[].color` | `#0RRGGBB`, seven hex digits. The extra `0` after `#` is inserted by the client when writing (`SetMaterials`: `y.color.slice(0,1)+"0"+y.color.slice(1)`), so strip `#0` to get the RGB triple |
| `materials[].vendor`, `name`, `rfid` | from the spool tag, or from a manual slot edit |
| `materials[].percent` | remaining, as a percentage. Always `100` on this printer for tagged spools |
| `materials[].state` | `0` = empty, anything else = filament present. **Not a boolean**: a slot that is feeding the toolhead reports `2`, observed live on 2026-09-13 with slot 2B mid-print. The client tests `Number(state) !== 0`, never `=== 1`, and so does the bridge |
| `materials[].editStatus` | `0` empty, `1` set (tag read or user edit), `2` seen on the EXT pseudo box. Unreliable while a slot is in use: the live 2026-09-13 read gave `editStatus: 0` for the tagged PETG in 2B that was printing at the time, so the bridge does not use it to decide whether a slot holds anything |
| `materials[].pressure`, `remaining_length` | present in the 2026-09-13 read and absent from the 2026-09-12 one. `pressure` is the pressure advance that was written with the material; `remaining_length` read `191400.0` for a slot Moonraker put at 76 m, so its unit is not settled and the bridge ignores it in favour of Moonraker's `remain_len` |
| `materials[].selected` | `1` = currently loaded to the toolhead |
| `same_material` | grouping of slots that hold the same material: `[rfid_code, color, [{boxId,materialId},...], type]` |
| `colorMatch` | the **currently active gcode-slot to physical-slot map** for the running job. See section 4 |

Notes that matter for the bridge:

- The reply does **not** contain `boxColorInfo` or a top level `cfsName`, even though
  `CP/.../DataType.cpp:121` and `:142` read both. Those are synthesised by the JS layer from
  `materialBoxs` before the object is handed to C++. A Python client must build them itself.
- The EXT pseudo box (`id:0`) always appears, with a single material entry, even when nothing is on the
  external holder.

### 2.3 The third party spool, and where Moonraker disagrees

Slot 2B on this printer holds an untagged third party spool (Anycubic PETG) that was set by hand on the
touchscreen. The two sources disagree, and the disagreement is systematic:

| Source | 2B type | 2B colour | 2B remaining |
| --- | --- | --- | --- |
| `boxsInfo` (`captures/creality_boxsInfo.json`, box 2 material 1) | `PETG`, vendor `Generic`, rfid `00003` | `#0000000` | `percent: 100` |
| Moonraker `box` object (`captures/moonraker_box_object.json`, `box.T2`) | `material_type[1] = "unknown"` | `color_value[1] = "unknown"` | `remain_len[1] = "76"` |

Reading: Moonraker's `box` object reports the **raw RFID layer**. A slot with no readable tag is
`unknown` there forever, whatever the user typed on the screen. `boxsInfo` reports the **effective**
material the box module will act on, including manual edits. `remain_len` (metres) only exists in the
Moonraker view; `boxsInfo` only offers `percent`.

Consequence for `cfsbridge slots`: read **both**. Take type, vendor, name and colour from `boxsInfo`;
take `remain_len` from Moonraker `box.T<n>.remain_len`. Flag a slot where the two disagree, because that
is exactly the manual-edit case, and a type-based auto map that trusted Moonraker alone would refuse a
slot the printer is perfectly happy to use.

The material type codes in the Moonraker view are `"0" + rfid`: `0P1003` is Creality/Polymaker
`P1003`, `000001` is `Generic PLA` (`00001`), `-1` is an empty slot.

### 2.4 Box configuration

```json
{"method": "get", "params": {"boxConfig": 1}}
```

Live reply (`captures/creality_ws9999_session.jsonl`):

```json
{"boxConfig": {"autoRefill": 1, "cAutoFeed": 1, "cSelfTest": 0, "cAutoUpdateFilament": 0}}
```

`autoRefill` is the "use another slot with the same material when one runs out" behaviour, which is why
`same_material` exists. The reply is a **single object for the whole printer**, not one per CFS unit,
and so is the write. The write form is `DeviceInterface.SetBoxsConfig`; see section 6.3.

## 3. Uploading a G-code file

Creality Print uploads over **plain HTTP on port 80**, not over the 9999 socket.

`CP/src/slic3r/GUI/print_manage/Device/Klipper4408Interface.cpp:37`:

```cpp
std::string urlUpload = "http://" + serverIp + ":" + std::to_string(80) + "/upload/" + Slic3r::Http::url_encode(uploadFileName);
```

`Klipper4408Interface.cpp:64` sets `Content-Type: multipart/form-data` and adds the file with
`mime_form_add_file(temp_upload_name, filePath)`, so the form field name is the **file name itself**,
not `file`. `Klipper4408Interface.cpp:52` clears all other headers first. An MD5 header is prepared but
commented out (`Klipper4408Interface.cpp:54`). Success is decided by a case-insensitive substring test
for `OK` in the response body (`Klipper4408Interface.cpp:68`).

`RemotePrinterManager::determinePrinterType` (`CP/.../RemotePrinterManager.cpp:489`) routes any address
containing a dot, that is any LAN IP, to `REMOTE_PRINTER_TYPE_KLIPPER4408`, and
`RemotePrinterManager.cpp:414` calls that interface with a hard coded port 80. So for the K2 Plus on the
LAN this is always the upload path. The other two interfaces are cloud (`KlipperCXInterface.cpp`, Aliyun
OSS then `uploadGcodeToCXCloud`) and generic Moonraker (`KlipperInterface.cpp`), neither of which the LAN
flow uses.

### Where the file lands

`/mnt/UDISK/printer_data/gcodes/`. Two independent confirmations:

- Moonraker `GET /server/files/roots` (`captures/moonraker_files_roots.json`) lists root `gcodes` at
  `/mnt/UDISK/printer_data/gcodes`.
- The live `reqGcodeFile` reply (`captures/creality_reqGcodeFile.json`) gives
  `"path": "/mnt/UDISK/printer_data/gcodes/<name>.gcode"` for the job that was printing.

This matters because the start-print message takes an absolute path, not a name. The JS derives it
rather than hard coding it:

```js
if (z.data.retGcodeFileInfo2) {
  let de = z.data.retGcodeFileInfo2[0].path;
  L = de.replace(/gcodes\/.*/, `gcodes/${$}`);          // splice the new name onto the known root
} else if (z.data.retGcodeFileInfo) {
  L = z.data.retGcodeFileInfo.fileInfo.split(":")[0] + "/" + $;
}
```

(`C_Lh0d9R.js`, `BackendInterface.processPlateData`, offset ~4640400.) A second call site in the same
bundle hard codes `/mnt/UDISK/printer_data/gcodes/<name>` for the K2 family and
`/usr/data/printer_data/gcodes/<name>` for the K1 family. **The bridge should ask, not assume**: issue
`{"method":"get","params":{"reqGcodeFile":1}}` and splice onto `retGcodeFileInfo2[0].path`.

### File listing read

```json
{"method": "get", "params": {"reqGcodeFile": 1}}
```

Reply key `retGcodeFileInfo2`, an array of
`{custom_types, type, name, path, file_size, create_time, timeCost, ...}`.
Live sample in `captures/creality_reqGcodeFile.json`.

### Size limits

None found in the client. The observed job on the printer is 12.7 MB
(`captures/creality_reqGcodeFile.json`, `file_size: 12744090`) and uploaded fine. Not tested at the
extremes.

## 4. Starting a print with a filament to slot mapping

This is the part that matters most, and there are **two independent mechanisms**. Both are real, both
are used, and they compose, so a client must use exactly one.

### 4.1 Mechanism A: G-code names the physical slot (`M8200 L I<n>` and `T<n>`)

Creality Print's own K2 profile emits, in `change_filament_gcode`
(`CP/resources/profiles/Creality/machine/Creality K2 Plus 0.4 nozzle.json:16`):

```gcode
M8200 P S[next_extruder]          ; announce the upcoming change
M8200 R E-<len>                   ; retract before the cut
M8200 C S0                        ; cut
M8200 R                           ; retrude, pull filament back to the CFS
M8200 L I[next_extruder]          ; LOAD physical slot number next_extruder
T[next_extruder]                  ; Klipper tool select
...flush, wipe...
M8200 O                           ; end of change
```

and the same `M8200 P` / `M8200 C` / `M8200 R` / `M8200 L I[initial_no_support_extruder]` /
`T[initial_no_support_extruder]` block in `machine_start_gcode` (same file, `:58`).

The `I` parameter is a **0-based global slot index**. The `M8200` macro on the printer decodes it.
Live copy of the printer's own config, `captures/printer_box.cfg:177`:

```jinja
{% if params.L is defined %}
  {% set I_param = params.I|int %}
  {% set addr = (I_param / 4 + 1)|int|string %}
  {% set num_map = ['A', 'B', 'C', 'D'] %}
  {% set num = (I_param % 4)|int %}
  {% set tnn = 'T' + addr + num_map[num] %}
  CR_BOX_EXTRUDE TNN={tnn}
  SET_GCODE_VARIABLE MACRO=M8200 VARIABLE=tnn VALUE='"{tnn}"'
  CR_BOX_WASTE
{% endif %}
```

So `I0` to `I15` map to `T1A`, `T1B`, `T1C`, `T1D`, `T2A`, ... `T4D`. `I5` is `T2B`.

The bare `T<n>` command is handled by the compiled box module, not by a macro: there is no
`[gcode_macro T0]` anywhere in the printer's config. The same index convention applies, which is why an
Orca-sliced single-filament job that emits `T0` makes the printer load slot 1A.

**This is the cause of the observed failure.** The Orca job emitted `T0`, the box module read that as
slot 1A, unloaded the manually loaded 2B PETG and tried to load 1A. The load then failed repeatedly with
`EXTRUDE_ERR8 (0xc)` and `key836` (blockage between the connections and the extruder).

The full `M8200` sub-command set, from `captures/printer_box.cfg:158` and the Chinese comments at
`:151` to `:157`:

| Command | Macro | Meaning |
| --- | --- | --- |
| `M8200 P S<n>` | `CR_BOX_PRE_OPT` | prepare for a filament change |
| `M8200 C S0` | `CR_BOX_CUT` | cut the filament |
| `M8200 R [E-<len>]` | `CR_BOX_RETRUDE [LENGTH=]` | retrude, pull the old filament back to the CFS |
| `M8200 L I<n>` | `CR_BOX_EXTRUDE TNN=T<a><A-D>` then `CR_BOX_WASTE` | load slot `n`, then waste-chute check |
| `M8200 W` | `CR_BOX_WASTE` | waste chute detection |
| `M8200 F S<speed> L<length>` | `CR_BOX_FLUSH TNN=<last loaded>` | flush |
| `M8200 O S<n>` | `CR_BOX_END_OPT` | end of the change |

### 4.2 Mechanism B: the protocol carries a remap table (`colorMatch`)

`C_Lh0d9R.js`, `DeviceInterface.SetColorMatch` / `SetColorMatchWithRetry`:

```js
{method:"set", params:{colorMatch:{ path: <absolute gcode path>, list: [ ... ] }}}
```

and each list entry, built in `BackendInterface.processPlateData`:

```js
const oe = re.map(de => {
  const ue = de.extruderId, pe = de.extruderFilamentType;
  return {
    id: `T${Math.floor((ue - 1) / 4 + 1)}${String.fromCharCode(65 + (ue - 1) % 4)}`,
    type: pe,
    color: de.matchColor,
    boxId: de.boxId,
    materialId: de.materialId
  };
});
```

So each entry is:

```json
{"id": "T1A", "type": "PETG", "color": "#RRGGBB", "boxId": 2, "materialId": 1}
```

`id` is the slot label **the G-code will ask for**, computed from the 1-based slicer extruder index with
the same `//4` and `%4` arithmetic as `M8200 L I`. `boxId` and `materialId` are the **physical** slot to
serve it from. In other words `colorMatch` installs a lookup table `gcode slot -> physical slot`.

That table is directly visible in Moonraker. `captures/moonraker_box_object.json` contains

```json
"map": {"T1A":"T1A","T1B":"T1B", ... ,"T4D":"T4D"}
```

which is the identity map, and `boxsInfo.colorMatch` read
`[{"id":"T1A","boxId":1,"materialId":0}]` at the same moment, matching the running job.

**This was then confirmed live in the other direction.** A later read, after the slot assignment was
changed on the printer, gave (`captures/colormatch_nonidentity_map.json`):

```json
"boxsInfo.colorMatch": [{"id": "T1A", "boxId": 2, "materialId": 1}]
"box.map":             {"T1A": "T2B", "T1B": "T1B", ... , "T4D": "T4D"}
```

So `colorMatch` and `box.map` are the same table in two representations, and the table really does hold a
non-identity entry: the G-code slot label `T1A`, which is what a bare `T0` resolves to, is served by
physical slot `2B`. That is direct evidence that the mapping is applied at slot-label resolution and is
not tied to the `M8200 L` macro, which answers the main open question about whether `colorMatch` alone
can redirect an Orca `T0`.

`cfsbridge slots` prints this map and says plainly which physical slot a `T0` would load.

### 4.3 Starting the job

Two start messages, chosen by whether the job is multi-colour:

```json
{"method":"set","params":{"opGcodeFile":"printprt:<absolute path>","enableSelfTest":0}}
```

```json
{"method":"set","params":{"multiColorPrint":{"gcode":"<absolute path>","enableSelfTest":0}}}
```

(`DeviceInterface.StartNormalPrint` and `DeviceInterface.StartMultiColorPrint`.) `enableSelfTest` is `1`
when the user ticked calibration, otherwise `0`.

The decision logic (`BackendInterface.processPlateData`) is:

1. If the device has no `boxColorInfo` entries and no colour match info was supplied, call
   `StartNormalPrint` and stop.
2. Otherwise, if there is colour match info, send `colorMatch` first and wait for it to succeed
   (`SetColorMatchWithRetry`, up to 5 attempts 2 s apart). If that fails, abort with a user message.
3. If CFS mode is on (`open_cfs == 1`), call `StartMultiColorPrint`; otherwise `StartNormalPrint`.

**Ordering is mandatory**: `colorMatch` is sent before the start message, and the start message is only
sent if `colorMatch` was acknowledged.

### 4.4 What the printer does with no mapping

Nothing is remapped. `box.map` stays at whatever it was, which is the identity map by default, so a
`T0` in the G-code loads `T1A`. This is not a guess: the currently running Orca job is exactly that case
and `boxsInfo.colorMatch` reads `[{"id":"T1A","boxId":1,"materialId":0}]`.

### 4.5 Which mechanism the bridge should use

Creality Print uses **both, in the same job**: the G-code it generates names slots directly via
`M8200 L I<n>` and `T<n>`, and it *additionally* sends `colorMatch` to remap those names onto whatever
physical slots the user picked in the send dialog. The G-code indices are the slicer's extruder order;
the `colorMatch` table is the user's choice.

An Orca-sliced G-code has no `M8200` at all and emits `T0..Tn` for the slicer's own extruder order. That
gives the bridge two options:

- **`colormatch` (default).** Send `colorMatch` with `id` = the label Orca's tool number resolves to
  (`T0` -> `T1A`, `T1` -> `T1B`, ...) and `boxId`/`materialId` = the requested physical slot, then start.
  This mirrors Creality Print exactly and needs no G-code edit. It also explicitly overwrites any stale
  map left by a previous job.
- **`gcode`.** Rewrite the tool numbers in the uploaded file (`T0` -> `T5` for slot 2B) and start
  normally. Simpler, no protocol state, but it composes with whatever `box.map` currently holds. If a
  previous job left a non-identity map, a rewritten `T5` resolves through `map["T2B"]` and lands
  somewhere else. Only safe when the map is known to be identity.

The bridge implements both and defaults to `colormatch` for that reason. Neither has been executed
against the printer by the agent.

## 5. Progress and errors

All of these arrive unsolicited on the same socket. Key names taken from
`dataCenter.setDataFromDevice` (`C_Lh0d9R.js`, offset ~4142900) and confirmed against
`captures/creality_initial_state.json`.

| Key | Meaning |
| --- | --- |
| `state` | printer run state |
| `deviceState` | `0` = idle and accepting jobs. The JS refuses to auto-start unless `online && deviceState == 0` |
| `printProgress` | percent complete |
| `printLeftTime`, `printJobTime`, `printStartTime` | seconds |
| `layer`, `TotalLayer` | current and total layer |
| `printFileName` | absolute path of the running job |
| `nozzleTemp`, `bedTemp0..2`, `boxTemp` | temperatures, as decimal strings |
| `feedState` | CFS feed state machine value. `101` observed mid-print |
| `materialState`, `materialStatus`, `materialCutterState` | filament path state. `materialStatus == 1` is forced to error `2839` by the client |
| `cfsConnect` | CFS bus connectivity |
| `repoPlrStatus` | power-loss-recovery prompt pending. The client forces error code `115` while this is `1` |
| `err` | `{"errcode": <n>, "key": <n>, "value": ...}` |
| `selfTestStep` | calibration progress |
| `diskTotalSize`, `diskUsedSize`, `deviceSn`, `modelVersion`, `hostname`, `model` | device info |

The `key` field inside `err` carries the `key8xx` codes seen in the Klipper log (`key865`, `key849`,
`key836`). The client treats that field and `errcode` as interchangeable numbers and looks the text up in its own
translation table; the mapping from number to message is not in the protocol.

Error acknowledgement messages, all writes, all listed here only so they are recognisable and avoided:
`{"method":"set","params":{"errorHandling":1}}` and `{"cleanErr":1}` (OK),
`{"errorHandling":0}` (retry), `{"repoPlrStatus":0|1}` (power loss recovery), `{"stop":1}`,
`{"pause":0|1}`.

## 6. Managing the CFS: slot edits, options, load, tag re-read, drying

Everything in this section is a **write**. All of it is implemented in `cfsbridge/protocol.py` as a
frame builder per message, each one checked against the exact JSON quoted here. As of
2026-09-13 **none of it has been sent to the printer**: the frames were built and compared, and every
attempt on live hardware so far has gone through the bridge's dry-run path. Section 6.7 lists what is
still owed.

All citations are into `CP/resources/web/deviceMgr/assets/C_Lh0d9R.js`, the device manager bundle,
whose `DeviceInterface` class is the only place these messages are constructed.

### 6.1 `modifyMaterial`: set one slot's material by hand

`DeviceInterface.SetMaterials(address, boxId, material)`:

```js
static SetMaterials(n,i,y){const k=dataCenter.getPrinter(n);
  let $=y.color.slice(0,1)+"0"+y.color.slice(1),
  z={method:"set",params:{modifyMaterial:{boxId:i,id:y.id,rfid:y.rfid,type:y.type,
     vendor:y.vendor,name:y.name,color:$,minTemp:Number(y.minTemp)+1e-8,
     maxTemp:Number(y.maxTemp)+1e-8,pressure:y.pressure}}};
  k.socket!=null&&k.socket.sendMsg(z)}
```

so the wire form, keys in that order, is:

```json
{"method":"set","params":{"modifyMaterial":{
  "boxId": 2, "id": 1,
  "rfid": "00003", "type": "PETG", "vendor": "Anycubic", "name": "Anycubic PETG",
  "color": "#0112233", "minTemp": 220.00000001, "maxTemp": 270.00000001, "pressure": 0.1
}}}
```

Four details from the source:

- `boxId` is the CFS unit, 1 to 4. `id` is the slot inside it, 0 to 3 for A to D. Both are the same
  numbers `boxsInfo` reports.
- The colour gains an extra `0` after the `#` on the way out:
  `let $ = y.color.slice(0,1) + "0" + y.color.slice(1)`, so `#112233` is sent as `#0112233`. That is
  why the reply carries seven hex digits (section 2.2).
- `minTemp` and `maxTemp` have `1e-8` added (`Number(y.minTemp)+1e-8`) purely to force JSON to
  serialise them as floats rather than integers.
- **`rfid`, `minTemp`, `maxTemp` and `pressure` are not typed by the user.** The dialog that builds
  this message picks brand, then material type, then name out of the printer's own filament database
  and reads all four off the entry that matched. The temperature lookup is
  `const Ie = Ce.value.find(At => At.name === re.value); const Ne = Ie?.minTemp, $e = Ie?.maxTemp`,
  and the pressure lookup is

  ```js
  he=(brand,type,name)=>{const Ne=ie.deviceType===0?ie.data?.retMaterials:ie.material;
    if(!Array.isArray(Ne))return 0;
    const $e=Ne.find(Oe=>Oe.base.brand===brand&&Oe.base.meterialType===type&&Oe.base.name===name);
    return $e?.kvParam?.pressure_advance??0}
  ```

  That database is `reqMaterials`; see 6.1.1. The bridge does the same lookup, and then allows the two
  temperatures to be overridden, which is the one thing Creality's own dialog will not do.

**Clearing a slot** is the same message with every string empty and every number a plain `0` (integer,
not the `+1e-8` float). `DeviceInterface.ResetMaterials`:

```json
{"method":"set","params":{"modifyMaterial":{"boxId":2,"id":3,"rfid":"","type":"","vendor":"",
  "name":"","color":"","minTemp":0,"maxTemp":0,"pressure":0}}}
```

**The CFS Mini variant** inserts `boxType` between `boxId` and `id`:

```js
static SetCfsMiniMaterials(n,i,y,k){ ... modifyMaterial:{boxId:i,boxType:y,id:k.id, ...} ... }
static ResetCfsMiniMaterials(n,i,y,k){ ... modifyMaterial:{boxId:i,boxType:y,id:k.id, ...} ... }
```

`boxType` is the box's own `materialBoxs[].type`: `0` a CFS, `1` the external spool holder, `2` a CFS
Mini box. The Vue component that builds the payload sets it as
`{cfsId, filamentId, boxType: Number(isRack) === 1 ? 1 : 0}`. **This printer's four units all report
`materialBoxName` `MF003`, the plain CFS, so the bridge sends the form without `boxType`**, and only
adds the key when the unit is an `MF046`. Note that `SetCfsMiniMaterials` itself is defined in the
bundle but never called from it; the Mini path goes through the same inline `sendMsg` the standard
edit dialog uses, which is where the key ordering above comes from.

The printer replies with a `modifyMaterial` key echoing the result. The echo is advisory: the bridge
waits for it, then re-reads `boxsInfo` and compares the slot, and it is the **read-back** that decides
whether the edit is reported as a success.

### 6.1.1 `reqMaterials`: the printer's filament database

A read, listed here because 6.1 depends on it.

```json
{"method": "get", "params": {"reqMaterials": 1}}
```

Reply key `retMaterials`, a list of 103 entries on this printer, 388 KB in total. Each entry is

```jsonc
{"engineVersion": "3.0.0", "printerIntName": "F008", "nozzleDiameter": ["0.4"],
 "kvParam": { 91 slicer keys, including "pressure_advance": "0.1" },
 "base": {"id": "00003", "brand": "Generic", "name": "Generic PETG",
          "meterialType": "PETG", "colors": ["#ffffff"], "density": 1.27,
          "minTemp": 220, "maxTemp": 270, "dryingTemp": 55, "dryingTime": 8,
          "dryingTempLow": 50, "dryingTempHigh": 60, ...}}
```

`meterialType` is spelt that way on the wire. The four brands present are `Creality` (34),
`Generic` (38), `eSUN` (18) and `Polymaker` (13), and `base.id` is the same material id that appears
as `materials[].rfid` in `boxsInfo`, so `00003` is `Generic PETG` and `P1003` is
`Polymaker Panchroma PLA Matte`.

Two ids repeat (`00003` and `14001`), because a filament synced from the slicer can reuse one: this
printer carries `Anycubic PETG (support interface) @Creality K2 Plus 0.4 nozzle` under `00003`
alongside `Generic PETG`. The bridge keys the database on (brand, material type, name), which is what
the client's own lookup does.

Live capture: `captures/creality_retMaterials.json`, trimmed to `base` plus
`kvParam.pressure_advance`; the untrimmed reply is in `captures/creality_ws9999_session.jsonl`.

### 6.2 What the bridge refuses to write, and when

Not protocol, but it belongs next to the frames. `cfsbridge/cfs.py` gates every write on
Moonraker's `print_stats.state`, which is the only source that names `paused` separately from
`printing` (`deviceState` collapses both into "not 0"). While the state is `printing` or `paused`,
**every** write is refused except:

- drying, which heats a cabinet and touches no filament path;
- editing or clearing a slot that is **neither loaded to the toolhead nor named by the active
  `colorMatch` map**. A two-colour job holds two slots and only one of them is in the toolhead, so
  both tests are needed.

If Moonraker cannot be reached the state is `unknown` and everything is refused, because refusing is
the safe way to be wrong.

### 6.3 `boxConfig`: the CFS options

`DeviceInterface.SetBoxsConfig(address, autoRefill, cAutoFeed, cSelfTest)`:

```js
static SetBoxsConfig(n,i,y,k){const $=dataCenter.getPrinter(n);
  $.socket!=null&&$.socket.sendMsg({method:"set",
    params:{boxConfig:{autoRefill:i,cAutoFeed:y,cSelfTest:k}}})}
```

```json
{"method":"set","params":{"boxConfig":{"autoRefill":1,"cAutoFeed":1,"cSelfTest":0}}}
```

**There is no box id: this is printer-wide, not per CFS unit.** The call takes an address and three
flags and nothing else, and the matching read (section 2.4) answers with a single object rather than
one per unit. Two hardware-specific variants exist:

| Variant | Function | Frame |
| --- | --- | --- |
| CFS-C (`MF050`) | `SetBoxsCfscConfig` | adds `ignoreColorAutoFeed` as a fourth key |
| CFS-Lite (`MF040`) | `SetBoxsCfsLiteConfig` | sends only `cAutoFeed` and `autoRefill` |
| everything else | `SetBoxsConfig` | the three keys above |

The dispatch is `getPrinterCfsType(printer)` on `boxsInfo.cfsName`, and the fallback is the plain
three-key form, which is what an `MF003` printer gets.

**`cAutoUpdateFilament` has no write.** The printer reports it in the `boxConfig` read, but the string
`cAutoUpdateFilament` does not appear anywhere in the device manager bundle: no setter, no cloud call,
no UI control. The bridge therefore shows it and refuses to set it, rather than guessing a key name.

### 6.4 `feedInOrOut`: load or unload a slot

`DeviceInterface.feedInOrOutMaterial(address, boxId, materialId, isFeed)`:

```js
$.socket.sendMsg({method:"set",params:{feedInOrOut:{boxId:i,materialId:y,isFeed:k}}})
```

```json
{"method":"set","params":{"feedInOrOut":{"boxId":2,"materialId":1,"isFeed":1}}}
```

`isFeed` is `1` to load the slot to the toolhead and `0` to unload it. The printer sends no
acknowledgement for this message, so the bridge reports it as sent and lets the next poll show the
slot's `selected` flag change.

### 6.5 `refreshBox`: re-read a slot's RFID tag

`DeviceInterface.SetRFIDRefresh(address, boxId, materialId)`:

```js
k.socket.sendMsg({method:"set",params:{refreshBox:{boxId:i,materialId:y}}})
```

```json
{"method":"set","params":{"refreshBox":{"boxId":4,"materialId":3}}}
```

Also unacknowledged. This one is **not undoable**: it replaces the slot's material with whatever the
tag says, and there is no message that puts the previous reading back. Edit the slot instead.

### 6.6 `dryBox` and `autoDry`: the drying cabinet

Three functions, all in `DeviceInterface`:

```js
static setDryBoxState(n,i,y,k,$,z,L){ ... sendMsg({method:"set",params:{dryBox:{addr:i,num:y,
  dryMode:k,targetTemp:$,totalTime:z,materialType:L}}}) }
static setDryStopState(n,i,y){ ... sendMsg({method:"set",params:{dryBox:{addr:i,num:y,
  dryMode:0,targetTemp:35,totalTime:10,materialType:["PLA","PLA"]}}}) }
static setAutoDryState(n,i,y,k){ ... sendMsg({method:"set",params:{autoDry:{addr:i,
  keepDryBinChoice:y,enable:k}}}) }
```

```json
{"method":"set","params":{"dryBox":{"addr":2,"num":1,"dryMode":1,"targetTemp":55.0,
  "totalTime":480,"materialType":["PLA","PETG"]}}}
{"method":"set","params":{"dryBox":{"addr":2,"num":2,"dryMode":0,"targetTemp":35,
  "totalTime":10,"materialType":["PLA","PLA"]}}}
{"method":"set","params":{"autoDry":{"addr":3,"keepDryBinChoice":3,"enable":1}}}
```

The two arguments that are not obvious, both settled from the calling component `CFSDrying`:

- **`addr` is the CFS unit id**, the same number as `materialBoxs[].id`. The page finds its unit with
  `materialBoxs.find(b => b.id === currentUsedCFSIndex)` and then passes that same
  `currentUsedCFSIndex` straight into `setDryBoxState(address, currentUsedCFSIndex, binNum, ...)`.
- **`num` is the drying bin inside the unit**, 1 or 2. The page reads a bin's two slots as
  `materials[binNum === 1 ? i : i + 2]` for `i` in 0,1, so bin 1 is slots A and B and bin 2 is slots
  C and D. `materialType` carries one entry per slot in that bin, which is why it has two elements.
- `totalTime` is in **minutes**: the page computes `Number(selectedHours) * 60`. `targetTemp` is in C
  and the page clamps the control to 45 to 85.
- `keepDryBinChoice` is the bin, or `3` for both at once
  (`const At = enable && otherBinAlreadyOn ? 3 : thisBin`).

The page also refuses to start when `Number(box.ac) === 0`, with "Insufficient power supply, please
plug in the power cord". **All four units on this printer report `ac: 0` and `materialBoxName`
`MF003`**, and the plain CFS has no drying cabinet at all: drying is a CFS-Pro (`MF042`) and CFS-C
(`MF050`) feature. So the bridge implements the frames, refuses them for a unit that is neither of
those, and refuses them again for a unit reporting `ac: 0`. **Nothing here can be tried on this
hardware.**

### 6.7 What is implemented and what is still unverified

| Frame | Built and unit-tested | Sent to the printer |
| --- | --- | --- |
| `modifyMaterial` set | yes | **no** |
| `modifyMaterial` clear | yes | **no** |
| `boxConfig` (three flags) | yes | **no** |
| `feedInOrOut` | yes | **no** |
| `refreshBox` | yes | **no** |
| `dryBox` start and stop | yes | **no**, and not possible on `MF003` hardware |
| `autoDry` | yes | **no**, same |
| `boxConfig` `cAutoUpdateFilament` | **not implemented** | no write exists in the client to copy |
| `boxConfig` `ignoreColorAutoFeed` | builder supports it | **no**, and this printer is not a CFS-C |

## 7. What Orca 2.4.2 sends, and what the facade must answer

`cfsbridge serve` presents itself to Orca as an OctoPrint host. Orca's OctoPrint client is
`OS/src/slic3r/Utils/OctoPrint.cpp`.

### Test button

`OctoPrint::test` (`OctoPrint.cpp:196`) does `GET <host>/api/version` with header `X-Api-Key`
(`OctoPrint.cpp:499`). It requires the JSON body to contain an `api` key (`OctoPrint.cpp:222`) and, if a
`text` key is present, `OctoPrint::validate_version_text` (`OctoPrint.cpp:492`) requires it to start with
the literal `OctoPrint`. So the minimum valid reply is:

```json
{"api": "0.1", "server": "1.5.0", "text": "OctoPrint (cfsbridge)"}
```

The real printer answers `{"server":"1.5.0","api":"0.1","text":"OctoPrint (Moonraker ?)"}`
(`captures/octoprint_api_version.json`), which also passes.

### Upload

`OctoPrint::upload_inner_with_host` (`OctoPrint.cpp:423` for the URL, `:460` for the fields) does
`POST <host>/api/files/local` as `multipart/form-data` with:

| Field | Value |
| --- | --- |
| `print` | `"true"` when the user pressed Print, `"false"` for Send only. `OctoPrint.cpp:460` |
| `path` | the parent path the user typed in the upload dialog, often empty. `OctoPrint.cpp:461` |
| `plateindex` | 1-based plate number, only when the payload is a `.gcode.3mf`. `OctoPrint.cpp:465` |
| `file` | the file itself, filename = the upload name. `OctoPrint.cpp:467` |

`make_url` (`OctoPrint.cpp:506`) prepends `http://` when the host field has no scheme, so a host of
`127.0.0.1:7126` and a host of `http://127.0.0.1:7126` both work.

Orca ignores the response body on success and only reports the HTTP status, so a non-2xx status with a
plain text body is how the facade reports "no slot matches this filament" back to the user.

Orca's Moonraker host type (`OS/src/slic3r/Utils/Moonraker.cpp`) is a different code path:
`GET /server/info`, `GET /server/files/roots`, `POST /server/files/upload` with `root=gcodes`, then
`POST /printer/print/start`. The facade answers `/server/info` too so that host type also connects, but
the OctoPrint type is the documented setup because its upload is a single request.

## 8. Message index

Reads (safe):

| Message | Reply key |
| --- | --- |
| `{"method":"get","params":{"boxsInfo":1}}` | `boxsInfo` |
| `{"method":"get","params":{"boxConfig":1}}` | `boxConfig` |
| `{"method":"get","params":{"reqGcodeFile":1}}` | `retGcodeFileInfo2` |
| `{"method":"get","params":{"reqGcodeList":1}}` | file list |
| `{"method":"get","params":{"reqMaterials":1}}` | `retMaterials`, the printer's filament preset database. Section 6.1.1 |
| `{"method":"get","params":{"getToken":1}}` | `videoToken` |
| `{"method":"get","params":{"nozzleList":1}}` | `nozzleList` |
| `{"method":"get","params":{"nozzleState":<n>}}` | `nozzleState` |
| `{"method":"get","params":{"nozzleFilament":<n>}}` | `nozzleFilament` |
| `{"method":"get","params":{"reqPrintObjects":1}}` | object exclusion list |
| `{"method":"get","params":{"materialBinStatus":{"addr":-1}}}` | `materialBinStatus` |

Writes the bridge builds (none sent to the printer as of 2026-09-13):

| Message | Built by | Documented in |
| --- | --- | --- |
| `colorMatch` | `CrealityClient.set_color_match` | 4.2 |
| `opGcodeFile`, `multiColorPrint` | `start_normal_print`, `start_multi_color_print` | 4.3 |
| `stop` | `stop_print` | 5 |
| `modifyMaterial` | `modify_material_frame`, `reset_material_frame` | 6.1 |
| `boxConfig` | `box_config_frame` | 6.3 |
| `feedInOrOut` | `feed_frame` | 6.4 |
| `refreshBox` | `refresh_frame` | 6.5 |
| `dryBox`, `autoDry` | `dry_box_frame`, `dry_stop_frame`, `auto_dry_frame` | 6.6 |

Writes the bridge does not build, listed for recognition only: `pause`, `errorHandling`, `cleanErr`,
`repoPlrStatus`, `abortSlice`, `setPosition`, `autohome`, `gcodeCmd`, `nozzleTempControl`,
`bedTempControl`, `boxTempControl`, `setFeedratePct`, `fan`, `fanAuxiliary`, `fanCase`, `lightSw`,
`hostname`, `replaceCutter`, `deleteHistory`, `ctrlVideoFiles`, `enableSelfTest`.

## 9. Open questions

- Whether a `colorMatch` written by `cfsbridge` is accepted the same way as one written by Creality
  Print. The table it targets has been observed holding a non-identity entry (section 4.2), so the
  mechanism is real; what has not been executed is cfsbridge writing to it.
- Whether `multiColorPrint` is required, or merely preferred, for a job that carries `colorMatch`.
  `StartNormalPrint` is used when CFS mode is off even with a mapping present, which suggests the mapping
  is honoured either way.
- The numeric error `key` to message table. Not in the protocol; it lives in the client's i18n resources.
- Upload size limit on port 80.
- Whether a `modifyMaterial` written by `cfsbridge` is accepted. The frame matches the client's
  character for character and the read-back check is in place, but nothing has been sent (section 6.7).
- What the `modifyMaterial` echo actually contains. The client logs it and ignores it, so the bridge
  treats its presence as an ack and lets the `boxsInfo` read-back decide the outcome.
- Whether `feedInOrOut` and `refreshBox` are refused mid-print by the firmware as well as by the
  bridge. The bridge refuses them itself, so this has not been probed.
- Everything about drying. No `MF042` or `MF050` unit is attached here, so `dryBox` and `autoDry`
  cannot be tried at all.
- Whether `cAutoUpdateFilament` has a write under some other key. Nothing in the device manager bundle
  sets it.
