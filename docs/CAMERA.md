# K2 Plus camera: how the stream is actually negotiated

Target: let a Python tool grab a still frame and relay video from the Creality K2 Plus
at `192.168.1.50`.

Two sources of truth were used:

1. The CrealityPrint source release (paths below are relative to its tree root), which is
   the only client that successfully plays this camera.
2. Live read-only probing of the printer itself (it was mid-print, so only GETs plus
   two camera-signalling POSTs were issued, see "Live probe" below).

Short version up front: **there is no MJPEG and no JPEG snapshot endpoint on this
machine. The camera is WebRTC-only, over a Creality-specific base64 signalling
envelope on port 8000.** Details and the recommended Python approach are in
"Feasible in Python?" at the end.

---

## 1. What CrealityPrint does

### 1.1 The endpoint

`http://<printer-ip>:8000/call/webrtc_local`

Cited in three independent places in the tree:

- `CrealityPrint/src/slic3r/GUI/HttpServer.cpp:180`
  ```cpp
  video_url = (boost::format("http://%1%:8000/call/webrtc_local") % ip).str();
  ```
- `CrealityPrint/src/slic3r/GUI/print_manage/App/PrinterMgrView.cpp:1424`
  (same format string)
- `CrealityPrint/resources/web/deviceMgr/assets/C_Lh0d9R.js:1256`
  ```js
  const sr = ir ? `https://${ue.address}/call/webrtc_local`
               : `http://${ue.address}:8000/call/webrtc_local`;
  ```

`ir` is the device capability flag `videoInfo.videoEncryption`. When a printer
advertises that feature, the client instead talks TLS to `https://<ip>/call/webrtc_local`
(port 443) and adds a `token` field. The K2 Plus at 192.168.1.50 does **not** use that
path: Moonraker itself advertises the plain `http://…:8000/call/webrtc_local` URL
(see `captures/moonraker_webcams_list.json`), and the plain endpoint answers.

### 1.2 The browser-side peer connection

`resources/web/deviceMgr/assets/C_Lh0d9R.js:1256`, function `Lt()` (the "open video"
routine). Reduced to the parts that matter:

```js
cr = new RTCPeerConnection({ iceServers: [] });      // no STUN, no TURN: LAN only
cr.ontrack = function (pr) { videoEl.srcObject = pr.streams[0]; /* ... */ };
cr.oniceconnectionstatechange = /* reconnect on disconnected/failed */;
cr.onicecandidate = pr => { if (pr.candidate === null) br(cr.localDescription.sdp); };
cr.addTransceiver("video", { direction: "sendrecv" });
cr.createOffer().then(pr => cr.setLocalDescription(pr));
```

Three non-obvious requirements, all of which Creality's own docs call out as
load-bearing (`doc/ai-chat-hotbed-camera-inspection-flow.md:213-232`):

- `iceServers: []`. Purely host candidates on the LAN.
- The video transceiver must be **`sendrecv`**, not `recvonly`. With `recvonly` the
  device completes signalling, fires `ontrack`, and then never sends a frame
  (`trackMuted` stays true forever).
- The offer is only sent **after ICE gathering completes** (`candidate === null`),
  i.e. non-trickle. Sending early reproduces the same permanently-muted track.

### 1.3 SDP munging before the POST

`C_Lh0d9R.js:1256`, function `fr(hr)` (called first thing inside `br()`):
it walks the offer SDP, groups the `a=rtpmap:` blocks, deletes every codec group that
is not H264 or whose payload type is outside 96-127, and emits an SDP carrying a single
H264 codec. The device is H264-only, so the client hands it a single-codec offer.

Separately, the C++ host rewrites the ICE candidate address. In
`CrealityPrint/src/slic3r/GUI/print_manage/Routes/DeviceMgrRoutes.cpp:149-210`
and the identical copy in
`CrealityPrint/src/slic3r/GUI/print_manage/App/SendToPrinter.cpp:677-733`:
it opens a UDP socket toward the printer IP to learn the machine's own LAN address,
then replaces the address field (token index 4) of the **first** `a=candidate` line with
that LAN IP. This exists because Chromium emits `.local` mDNS candidates that the
device cannot resolve. A Python client that emits real IPv4 host candidates does not
need this step.

### 1.4 The exact request

Built in `DeviceMgrRoutes.cpp:213-226` (and `SendToPrinter.cpp:735-747`):

```cpp
nlohmann::json j;
j["type"] = "offer";
j["sdp"]  = sdp;
if (!videoToken.empty()) j["token"] = videoToken;   // encrypted devices only
std::string d = j.dump();
std::string e = cereal::base64::encode(..., d.length());   // base64 of the JSON
```

and sent by the JS at `C_Lh0d9R.js:1256`:

```js
axios.post(sr, ar.sdp, { headers: { "Content-Type": "plain/text" }, timeout: 5e3 })
     .then(xr => wr(xr.data))
```

where `ar.sdp` is the base64 string `e` handed back by the host. So on the wire:

| Field | Value |
| --- | --- |
| Method | `POST` |
| URL | `http://192.168.1.50:8000/call/webrtc_local` |
| Headers | `Content-Type: plain/text` (sic, not `text/plain`, not `application/json`). No auth, no cookie, no API key. |
| Body | **base64 of the UTF-8 JSON** `{"type":"offer","sdp":"v=0\r\no=- …\r\n"}` |

Example body before base64 (encrypted devices add a `token` field carrying the device's video token):

```json
{"type": "offer", "sdp": "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 102\r\nc=IN IP4 0.0.0.0\r\na=rtcp-mux\r\na=mid:0\r\na=sendrecv\r\na=rtpmap:102 H264/90000\r\na=fmtp:102 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f\r\na=ice-ufrag:abcd\r\na=ice-pwd:…\r\na=fingerprint:sha-256 …\r\na=setup:actpass\r\n"}
```

The body is the base64 text only. No JSON wrapper around the base64, no form encoding.

### 1.5 The response and how the answer is applied

`C_Lh0d9R.js:1256`, handler `wr`:

```js
const wr = xr => {
  const Sr = JSON.parse(atob(xr));            // base64 -> JSON -> {type:"answer", sdp:"..."}
  if (peer.signalingState !== "have-local-offer") return;   // ignore late/dup answers
  if (peer.remoteDescription) return;
  peer.setRemoteDescription(new RTCSessionDescription(Sr));
};
```

So the response body is itself **base64 of JSON** `{"type":"answer","sdp":"…"}`,
served as `Content-Type: text/plain`, HTTP 200. Symmetric with the request.

For the encryption path the host performs the POST itself
(`DeviceMgrRoutes.cpp:243-274`, `Http::post(url).header("Content-Type","plain/text").set_post_body(e)`,
with `ssl_verify_peer(true)` against `resources/cert/ca.crt` and `ssl_verify_host(false)`)
and passes the raw response body back to the page through
`window.handleStudioCmd(...)` as `{"command":"get_webrtc_local_param","data":{"sdp":…,"url":…,"videoEncryption":…,"status":…}}`.
The page then feeds `data.sdp` into the same `wr()` decoder. Same envelope either way.

### 1.6 The Linux/native path is a useful precedent

On Linux, CrealityPrint does not use a browser at all. `HttpServer.cpp:160-209` exposes a
local `/videostream?ip=…` endpoint that:

- calls `WebRTCDecoder::GetInstance()->startPlay("http://<ip>:8000/call/webrtc_local")`
  (`HttpServer.cpp:180-183`, decoder in `src/video/WebRTCDecoder.cpp:54`, built on the
  metaRTC/"Yang" stack in `src/video/Yang*.cpp`), and
- re-serves the decoded frames locally as **`multipart/x-mixed-replace; boundary=boundarydonotcross`**,
  i.e. plain MJPEG (`HttpServer.cpp:187-209`, frames pulled via
  `WebRTCDecoder::GetInstance()->getFrameData()` at `HttpServer.cpp:245`).

That is exactly the shape a Python tool should copy: consume WebRTC once, republish as
MJPEG locally.

### 1.7 MJPEG in CrealityPrint is for other machines, not this one

The MJPEG paths exist only as a fallback for old printers:

- `src/slic3r/GUI/simple/toolcalls/MCPToolCallsRegistration.cpp:284-301`
  (`capture_mjpeg_frame`) builds `http://<ip>:8080/?action=stream`.
- `C_Lh0d9R.js:1256`, function `dr()`: if `ue.webrtcSupport` is false it uses
  `hr.linuxVideoUrl` or falls back to `http://${ue.address}:8080/?action=stream`.
- `doc/ai-chat-hotbed-camera-inspection-flow.md:248-257` states plainly that new
  machines go WebRTC first and MJPEG is only allowed when `old_printer == true`.

Port 8080 is **closed** on this K2 Plus (see `captures/printer_port_scan.txt`), so this
fallback does not apply here.

Creality's `K2_Series_Klipper` source release contains only Klipper firmware
(`klippy/`, `src/`, `lib/`, `config/`); it has no camera-streamer, mjpg-streamer, or
webcam service configuration at all. Nothing to learn there about the camera.

---

## 2. Is this standard WHEP / camera-streamer WebRTC?

**No. It is proprietary Creality signalling around a standard WebRTC media session.**

Evidence:

| Standard | What it requires | What the K2 Plus does |
| --- | --- | --- |
| WHEP (RFC 9725) | `POST` with `Content-Type: application/sdp`, raw SDP offer as the body, `201 Created`, `Location:` resource URL, raw SDP answer body | `POST` with `Content-Type: plain/text`, base64-of-JSON body, `200 OK`, no `Location`, base64-of-JSON answer |
| ayufan camera-streamer | `POST /webrtc` with JSON `{"type":"offer","sdp":…}`, plus `/snapshot`, `/stream`, `/video`, an HTML index at `/` | Path is `/call/webrtc_local`; the JSON is base64-wrapped; `/snapshot`, `/stream`, `/video`, `/` all return HTTP 200 with a **zero-byte** `text/html` body |

Moonraker labels the entry `webrtc-camerastreamer` (see
`captures/moonraker_webcams_list.json`), but that is only a Fluidd/Mainsail player hint.
The service behind port 8000 is not ayufan's camera-streamer: it serves nothing except
`/call/webrtc_local`, and the answer SDP it generates carries `a=ssrc:1 cname:pear`,
which is the signature of the embedded **libpeer** WebRTC stack, not libwebrtc.

The **media** side, once negotiated, is ordinary WebRTC: DTLS-SRTP, H264 baseline
(`profile-level-id=42e01f`, `packetization-mode=1`), `a=sendonly`, `a=rtcp-mux`,
`a=setup:passive`, ICE host candidates over UDP and TCP. Only the signalling envelope is
custom, and the custom part is trivial (two base64 calls).

---

## 3. Live probe of 192.168.1.50

All probes were read-only GETs, except two POSTs to `/call/webrtc_local`, which is a
pure camera-signalling endpoint (it is what Moonraker itself advertises as the webcam
`stream_url`, and CrealityPrint uses it for nothing but video). No Moonraker non-GET
request and no gcode was sent. The printer was mid-print throughout and unaffected.

Capture file names below name the raw probe logs recorded at the time. They are cited for
provenance; the raw logs themselves are not published, because they contain one particular
printer's identifiers.

### 3.1 Port 8000 root and path sweep

`captures/camera_port8000_root.txt` is `GET /`:

```
HTTP/1.1 200 OK
Content-Type: text/html
Access-Control-Allow-Headers: DNT,X-CustomHeader,Keep-Alive,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Sample-Source
Access-Control-Allow-Origin: *
Content-Length: 0
```

Empty body. **Not** camera-streamer's index page. It advertises nothing. The
`Sample-Source` entry in the CORS allow-list is a Creality-specific header name.

`captures/camera_port8000_probe_matrix.txt`: every other path tried returned the
identical `200 / text/html / 0 bytes` catch-all:

| Path | Status | Body |
| --- | --- | --- |
| `/` | 200 | 0 bytes |
| `/snapshot` | 200 | 0 bytes (headers in `captures/camera_snapshot_headers.txt`) |
| `/stream` | 200 | 0 bytes |
| `/webrtc` | 200 | 0 bytes |
| `/option` | 200 | 0 bytes |
| `/metrics` | 200 | 0 bytes |
| `/control?action=get` | 200 | 0 bytes |
| `/?action=stream` | 200 | 0 bytes |
| `/?action=snapshot` | 200 | 0 bytes |
| `/video` | 200 | 0 bytes |
| `/index.html` | 200 | 0 bytes |
| `/call/webrtc_local` (GET) | 200 | `{}` (2 bytes, `text/plain`), see `captures/camera_call_webrtc_local_GET.txt` |

**There is no `/snapshot` JPEG and no `/stream` MJPEG.** The 200 status is a catch-all,
not a real resource. Any Python code that trusts the status code without checking
`Content-Length` will silently get nothing.

### 3.2 Port scan

`captures/printer_port_scan.txt` (TCP connect only):

```
OPEN 80   OPEN 443   OPEN 4408   OPEN 7125   OPEN 8000
closed 554 (RTSP)   closed 1984   closed 3031   closed 8001
closed 8080   closed 8081   closed 8082   closed 8083   closed 8554   closed 9000
```

No mjpg-streamer on 8080, no RTSP on 554/8554, no go2rtc on 1984. Port 4408 serves
Fluidd (`captures/fluidd_port4408_root.txt`, 2829-byte HTML). Port 8000 is the camera
signalling service and nothing else.

### 3.3 Moonraker webcam list

`captures/moonraker_webcams_list.json`, verbatim:

```json
{"result": {"webcams": [{"name": "K2 Plus camera", "location": "printer", "service": "webrtc-camerastreamer", "target_fps": 15, "stream_url": "http://192.168.1.50:8000/call/webrtc_local", "snapshot_url": "", "flip_horizontal": false, "flip_vertical": false, "rotation": 0, "source": "database"}]}}
```

Note `"snapshot_url": ""`. Moonraker itself knows there is no snapshot endpoint. This
is why Moonraker/Fluidd thumbnails and any `/server/webcams` snapshot helper will not
produce a still for this printer.

### 3.4 Signalling POST, confirmed working

Probe A, deliberately minimal (`captures/camera_call_webrtc_local_POST_emptysdp.txt`,
decoded to `captures/camera_answer_decoded.txt`): body = base64 of
`{"type":"offer","sdp":""}`, `Content-Type: plain/text`. Response: `200 OK`,
`Content-Type: text/plain`, 1468 bytes of base64 that decodes to a full answer.

Probe B, a realistic single-H264 offer with payload type 102
(`captures/camera_call_webrtc_local_POST_h264pt102.txt`, decoded to
`captures/camera_answer_decoded_pt102.txt`). Response, decoded:

```
{"type": "answer", "sdp":
v=0
o=- 1495799811084970 1495799811084970 IN IP4 0.0.0.0
s=-
t=0 0
a=msid-semantic:WMS *
a=group:BUNDLE 0
m=video 9 UDP/TLS/RTP/SAVPF 96 102
a=rtcp-fb:102 nack
a=rtcp-fb:102 nack pli
a=fmtp:96 profile-level-id=42e01f;level-asymmetry-allowed=1
a=fmtp:102 profile-level-id=42e01f;packetization-mode=1;level-asymmetry-allowed=1
a=fmtp:102 x-google-max-bitrate=6000;x-google-min-bitrate=2000;x-google-start-bitrate=4000
a=rtpmap:96 H264/90000
a=rtpmap:102 H264/90000
a=ssrc:1 cname:pear
c=IN IP4 0.0.0.0
a=sendonly
a=mid:0
a=rtcp-mux
a=ice-ufrag:8Ycu
a=ice-pwd:8Lx+wZl9DNS7d9H5pJQ0t3
a=ice-options:trickle
a=fingerprint:sha-256 6C:46:FD:...:60:AE  (32 bytes, per session)
a=setup:passive
a=candidate:1 1 UDP 2015363327 192.168.1.50 56051 typ host
a=candidate:2 1 TCP 1015021823 192.168.1.50 0 typ host tcptype active
a=candidate:3 1 TCP 1010827519 192.168.1.50 60633 typ host tcptype passive
}
```

Findings from the two probes:

- **No authentication.** No token, no cookie, no header. A bare POST gets an answer.
- The device **echoes the offer's H264 payload type** into the answer (102 in probe B).
  In probe A, with an empty offer SDP, that slot came back as the garbage value
  `-1245706204`, i.e. an uninitialised read. So always send a real offer with a valid
  H264 payload type in 96-127; never send an empty or codec-less SDP.
- The answer declares two H264 entries (96 and the echoed PT) on one m-line. Both map to
  `H264/90000`. It is redundant but parseable.
- Fresh `ice-ufrag`, `ice-pwd`, DTLS fingerprint and UDP port are generated per request,
  so each POST is an independent session.
- `a=setup:passive` means the **client must be the DTLS client** (offer `a=setup:actpass`
  or `active`).
- `a=sendonly` on the device side, hence the client transceiver must be `sendrecv` or
  `recvonly` from its own perspective. CrealityPrint uses `sendrecv` deliberately, and
  their docs say `recvonly` yields a permanently muted track on this hardware. Start with
  `sendrecv`.
- Candidates are host-only, both UDP and TCP-passive. The TCP-passive candidate on a
  fixed-per-session port is a useful fallback if UDP is awkward.
- ICE gathering on the device side is complete at answer time (all candidates are in the
  answer), so no trickle handling is needed on the client.

---

## 4. Feasible in Python?

### 4.1 Plain HTTP snapshot: no

There is no snapshot endpoint. `/snapshot` returns HTTP 200 with a zero-byte
`text/html` body (`captures/camera_snapshot_headers.txt`), Moonraker reports
`"snapshot_url": ""`, and no other port serves an image. Confirmed live. This easy path
does not exist on this printer.

### 4.2 MJPEG relay by proxying `/stream`: no

Same story. `/stream`, `/video`, `/?action=stream` all return 200 with zero bytes on port
8000, and port 8080 (the mjpg-streamer fallback CrealityPrint uses for old machines) is
closed. There is no MJPEG stream anywhere on this device to proxy. An MJPEG relay is
still the right thing to *serve*, but the frames have to be produced locally after
decoding WebRTC, exactly as `HttpServer.cpp:187-209` does.

### 4.3 WebRTC via aiortc: yes, and it is required

WebRTC is the only way to get pixels off this camera. The good news is that everything
outside the media stack is trivial: two base64 calls and one `POST` with no auth.

Recommended implementation, simplest thing that works:

1. `aiortc.RTCPeerConnection(RTCConfiguration(iceServers=[]))`.
2. `pc.addTransceiver("video", direction="sendrecv")`.
3. `offer = await pc.createOffer(); await pc.setLocalDescription(offer)`: aiortc's
   `setLocalDescription` already waits for ICE gathering to finish, which matches
   CrealityPrint's "wait for `candidate === null`" requirement for free.
4. Body: `base64.b64encode(json.dumps({"type": "offer", "sdp": pc.localDescription.sdp}).encode())`.
5. `POST http://192.168.1.50:8000/call/webrtc_local`, header
   `Content-Type: plain/text`, body = that base64 text. No auth.
6. `answer = json.loads(base64.b64decode(response.text))`, then
   `await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))`.
7. On `@pc.on("track")`, `await track.recv()` in a loop. Discard the first frames until
   one decodes with sane dimensions (CrealityPrint's own note: signalling success does
   not mean frames are flowing). Convert with `frame.to_ndarray(format="bgr24")` or
   `frame.to_image()`.
8. Snapshot tool: encode that frame to JPEG and exit.
   MJPEG relay: hold the peer connection open and serve the latest frame as
   `multipart/x-mixed-replace; boundary=frame` on a local port, which is precisely what
   CrealityPrint's Linux build does.

The exact URL a Python tool should hit:

```
POST http://192.168.1.50:8000/call/webrtc_local
Content-Type: plain/text
body: base64(json({"type":"offer","sdp":"<offer sdp>"}))
```

Discover it at runtime rather than hardcoding it: `GET http://<ip>:7125/server/webcams/list`
returns it as `result.webcams[0].stream_url`.

If aiortc trips over the answer (the duplicated H264 payload types on one m-line are
unusual), strip the offer down to a single H264 codec the way CrealityPrint's `fr()`
function does, and pick a payload type in 96-127 so the echoed answer stays valid.

### 4.4 Native dependencies aiortc drags in

This is the real cost, so budget for it:

- `aiortc` itself, plus `pyav` (`av`), which bundles or links FFmpeg (libavcodec,
  libavformat, libavutil, libswscale, libswresample) for the H264 decode.
- `pylibsrtp` (libsrtp2) for SRTP.
- `cryptography` (OpenSSL) for DTLS.
- `pyee`, `google-crc32c`, `ifaddr` for ICE and plumbing.

On modern Linux x86_64 and macOS, `pip install aiortc` pulls prebuilt wheels for all of
these and needs no system packages. On a Raspberry Pi or other aarch64 Linux, `av` may
need building, which means `libavcodec-dev libavdevice-dev libavfilter-dev libavformat-dev
libavutil-dev libswscale-dev libswresample-dev pkg-config libsrtp2-dev libssl-dev` and a
compiler. If the target is constrained, a lighter alternative is to spawn
`ffmpeg -i "whep://…"`-style ingest, but no FFmpeg input handler speaks this
base64-wrapped Creality envelope, so the signalling would still have to be done in Python
and handed to ffmpeg out of band. aiortc is the straightforward choice.

### 4.5 Verdict

- Snapshot URL: **none exists**. Take a still by decoding one WebRTC frame from
  `http://192.168.1.50:8000/call/webrtc_local`.
- MJPEG URL: **none exists** on the printer. Produce MJPEG locally from the WebRTC frames
  and serve it from the Python tool.
- aiortc is required, not optional.
- Pin the design to the CrealityPrint Linux precedent: one long-lived WebRTC session,
  latest-frame cache, local MJPEG endpoint, and a snapshot endpoint that returns the
  cached frame as JPEG.

---

## 5. Capture index

The probe logs, not published for the reason in section 3:

| File | What it is |
| --- | --- |
| `camera_port8000_root.txt` | `GET http://192.168.1.50:8000/` with headers |
| `camera_snapshot_headers.txt` | `GET /snapshot` response headers (zero-byte body, so no JPEG was saved) |
| `camera_port8000_probe_matrix.txt` | Status/size/type for `/stream`, `/webrtc`, `/option`, `/metrics`, `/control?action=get`, `/?action=stream`, `/?action=snapshot`, `/video`, `/index.html` |
| `camera_call_webrtc_local_GET.txt` | `GET /call/webrtc_local` returning `{}` |
| `camera_call_webrtc_local_POST_emptysdp.txt` | POST with an empty offer SDP, raw base64 answer |
| `camera_answer_decoded.txt` | Probe A answer, base64-decoded |
| `camera_call_webrtc_local_POST_h264pt102.txt` | POST with a realistic single-H264 offer (PT 102), raw base64 answer |
| `camera_answer_decoded_pt102.txt` | Probe B answer, base64-decoded (the reference answer SDP) |
| `moonraker_webcams_list.json` | `GET http://192.168.1.50:7125/server/webcams/list` verbatim |
| `printer_port_scan.txt` | TCP connect results for 15 candidate ports |
| `fluidd_port4408_root.txt` | `GET http://192.168.1.50:4408/`, confirming Fluidd on 4408 |

---

## 6. 2026-09-13: can Fluidd's camera card play it? No.

The question was whether the Device tab, which was showing Fluidd on port 4408, could render the
camera through Fluidd's own webcam card, given that Moonraker advertises the webcam as
`webrtc-camerastreamer` (section 3.3).

Two things were checked live.

**The Fluidd on the printer is stock.** `GET http://192.168.1.50:4408/assets/index-DAJGCJUz.js`
(1.72 MB) carries the plain upstream camera-type list, `mjpegadaptive / mjpegstream / hlsstream /
webrtc-camerastreamer / webrtc-go2rtc / webrtc-mediamtx / video / iframe`, and no Creality code.
So its `webrtc-camerastreamer` player is ayufan's flavour, not a vendor patch that knows about the
base64 envelope.

**The endpoint answers exactly one flavour.** The same offer SDP was POSTed to
`http://192.168.1.50:8000/call/webrtc_local` three ways:

| Body | Content-Type | Result |
| --- | --- | --- |
| base64 of `{"type":"offer","sdp":...}` (Creality) | `plain/text` | HTTP 200, 1404 bytes of base64, decodes to a real answer SDP |
| raw JSON `{"type":"offer","sdp":...}` (camera-streamer, i.e. Fluidd) | `application/json` | HTTP 200, body `{}`, 2 bytes |
| raw SDP (WHEP, go2rtc, MediaMTX) | `application/sdp` | HTTP 200, body `{}`, 2 bytes |

So Fluidd's card completes its POST, receives no answer SDP, and shows nothing. No Fluidd camera
type can be configured to work: none of them speak the base64 envelope. That is why the fork's
Device tab now serves its own page instead (section 7.4 below).

**The page works.** The generated page was loaded from a `file:` URL in Chromium, which is the
engine WebView2 is built on:

* `window.isSecureContext` is true for `file:`, so `RTCPeerConnection` is available;
* the cross-origin POST from the `file:` page is allowed, because port 8000 sends
  `Access-Control-Allow-Origin: *` (section 3.1);
* the answer decoded, the track arrived, and the `<video>` element reported **1920 x 1080** with
  `currentTime` running;
* Fluidd on port 4408 iframes into the same page with no `X-Frame-Options` or CSP in the way.

One incidental finding: the mDNS `.local` candidate rewrite (section 1.3) turned out not to be
strictly required. The test ran from a NAT'd Chromium whose real address was not the one
written into the SDP, and media still flowed, so the device does send to wherever the STUN
connectivity check came from. The rewrite is kept anyway: it costs one UDP socket and it is what
CrealityPrint does.

---

## 7. 2026-09-13, second pass: a camera card, and why it cannot be Fluidd's own

Section 6 established that no Fluidd camera type can play this camera. The next question was
whether Fluidd's **`iframe`** camera type could, since that type embeds an arbitrary URL and the
URL could be a page that does Creality's signalling itself.

### 7.1 What was built: `GET /camera` on the bridge

`cfsbridge/camera.py` gained `camera_page()`, and `cfsbridge/serve.py` a `GET /camera` route. The
page is the fork's generated page with every piece of furniture removed: no title bar, no buttons
except a size control, no Fluidd frame. Just the picture on black, a small status overlay that fades
out the moment frames arrive, click-to-reconnect, and an automatic retry. The signalling is
unchanged and still matches CrealityPrint exactly (sections 1.2 to 1.5): `iceServers: []`,
`sendrecv`, non-trickle, a single H264 payload type, `Content-Type: plain/text`, base64-of-JSON both
ways. The printer address comes from the bridge's own `--host`.

| Route | What it is |
| --- | --- |
| `GET /camera` | the camera on its own, made to be embedded |
| `GET /camera?lan=<ip>` | same, with the ICE candidate address set by hand (see 7.2) |
| `GET /camera?bare=1` | same, with the S / M / L buttons dropped, for an outer page that owns the sizing |
| `GET /fluidd` | the printer's Fluidd, with the camera as a floating card on top (see 7.4) |
| `GET /camera/snapshot` | **501**, deliberately. There is no still to serve (section 4.1) |

The page is resizable: **S, M and L** in the top right set the picture to 45, 70 or 100 percent of
whatever container it is in, and the choice is kept in `localStorage`, so it survives a reload. The
video is `max-width: 100%; height: auto`, so the card as a whole follows its container.

### 7.2 The mDNS candidate rewrite is load-bearing after all

Section 6 recorded that the `.local` rewrite "turned out not to be strictly required". **That was
wrong, and it was measured again on 2026-09-13.** With the offer carrying Chromium's mDNS candidate
and nothing else:

* ICE reaches `connected`, and `getStats()` shows a nominated, succeeded candidate pair with eight
  STUN responses received;
* the DTLS transport stays at `dtlsState: "connecting"` forever, `bytesReceived: 0`;
* no frame ever arrives, and the page retries on its 12 second timer.

Rewriting the same offer's candidate to the browser machine's real LAN address makes the identical
page play at **1920 x 1080** within a few seconds. Deleting the `.local` candidate lines instead of
rewriting them does **not** work either. So the device reads the address out of the offer's SDP and
handshakes to it; it does not fall back to the source of the connectivity check.

The address therefore has to be right, and it has to be an address of **the machine running the
browser**. `camera.local_address()` finds it in two steps:

1. a connected UDP socket to the printer, which is what CrealityPrint does
   (`DeviceMgrRoutes.cpp:149`), used only when the answer lands on the printer's own /24;
2. failing that, `powershell.exe (Find-NetRoute -RemoteIPAddress <printer>).IPAddress`, which covers
   the case where the bridge sees the network through a virtual adapter (a Linux VM, a container) and
   the browser runs on the Windows host: the VM's NAT address is not one the printer could ever send
   to.

Measured where step 1 is wrong: it returns the VM NAT address and is rejected, step 2 returns the
host's LAN address, and the page plays. `?lan=<ip>` overrides both; only a dotted quad is accepted,
because the value goes into a JavaScript string literal. If neither source answers, the page still
loads and says on screen that it was given no address and that `?lan=` is the fix.

### 7.3 Fluidd's camera card cannot be shown on this printer at all

The Moonraker webcam entry was updated to the `iframe` service, which Moonraker accepted:

```
POST http://192.168.1.50:7125/server/webcams/item
{"name": "K2 Plus camera", "service": "iframe",
 "stream_url": "http://127.0.0.1:7126/camera", "snapshot_url": "", ...}
```

Fluidd sees it: `webcams/getWebcams` in the live store returns exactly that entry. But
**`webcams/getEnabledWebcams` returns an empty list**, and every camera card in Fluidd hangs off that
getter (`hasCameras` is `getEnabledWebcams.length > 0`). From the bundle actually served by the
printer, `assets/index-DAJGCJUz.js`:

```js
getEnabledWebcams: (r, e) => e.getWebcams.filter(s => s.enabled),
getWebcamById: r => e => r.webcams.find(t => t.uid === e)
```

The printer's Moonraker reports `api_version 1.0.5`, which predates the webcam schema this Fluidd
build expects. Its `/server/webcams/list` carries **no `enabled` and no `uid`**, and it silently
drops both, plus `icon` and `aspect_ratio`, from anything posted to `/server/webcams/item` (verified:
they are absent from the write's own echoed response). `webcams/init` fills the store from
`server.webcams.list` and nothing else, so there is no second source to fix it from.

So the camera card on this printer's own Fluidd is **empty of options and cannot be made to appear**,
whatever the service type is. It is not a mixed-content, CORS or iframe problem: the browser console
on the Fluidd page is completely clean. Switching the entry to `ipstream` or back to
`webrtc-camerastreamer` changes nothing, because the filter runs before the service type is read.

The entry was left on `iframe` pointing at `http://127.0.0.1:7126/camera`. It is harmless as it
stands, and it starts working on its own if the printer's Moonraker is ever updated.

**What does work, and was measured:** an iframe to `http://127.0.0.1:7126/camera` injected into the
Fluidd page at `http://192.168.1.50:4408` loads and plays. No mixed content (both are plain HTTP),
no CORS (the camera page is same-origin with its own script and the printer sends
`Access-Control-Allow-Origin: *`), and no Private Network Access block from the printer's private
address to the browser's loopback. So the only thing missing is Fluidd's willingness to render a card
for a webcam entry its Moonraker describes in an older shape.

### 7.4 `GET /fluidd`: the same result without Fluidd's cooperation

Since Fluidd will not draw the card, the bridge draws it. `GET /fluidd` is Fluidd in a full-window
frame with the camera floating over it as a card in the bottom right: a title strip reading
**Camera**, **S / M / L / Hide** buttons, a 16:9 picture, and the choice kept in `localStorage`. The
card width is 24, 32 or 46 `vw`, so it scales with the window. Hide collapses it to a single
**Camera** button in the corner.

Verified in Chromium on 2026-09-13: Fluidd loads in the frame, the card's video reports
**1920 x 1080** with `currentTime` advancing, the status overlay is gone, S gives a 346 x 225 card, L
gives 662 x 403, and Hide and the Camera button round-trip. Console clean.

### 7.5 Recommended setup

| Where | Value | What you get |
| --- | --- | --- |
| Orca **Device UI** (`print_host_webui`) | `http://127.0.0.1:7126/fluidd/` | **the recommended one.** Fluidd with the camera as a card in the corner, sizeable and hideable |
| Orca **Device UI** | `http://127.0.0.1:7126/` | the bridge's own CFS page; its camera panel now embeds `/camera` rather than the whole Fluidd dashboard |
| Orca **Device UI** | `http://192.168.1.50:4408` | plain Fluidd, and **no camera**, for the reason in 7.3 |
| Orca **Device UI** with host type `Creality CFS (K2)` | left empty | the fork's own standalone camera page, full width, video over a Fluidd frame |
| A browser tab | `http://127.0.0.1:7126/camera` | the camera on its own |

### 7.6 How a real Fluidd camera card would be sized

Worth writing down for the day this printer's Moonraker is updated, because none of it is the
camera's business. Inside Fluidd a card's width is the width of the dashboard column it sits in, and
the columns and the card order are Fluidd's:

1. open Fluidd, then the **gear** (Settings), then **Layout**, and pick the **Dashboard** layout;
2. the dashboard has two containers, and the live store calls them `container1` and `container2`.
   On this printer `camera-card` currently sits in `container1`, third, after `printer-status-card`
   and `spoolman-card`;
3. drag `camera-card` into the other container, or up and down inside one, to move it. The toggle
   beside it enables or disables the card;
4. the column count itself is the **Columns** control on the dashboard (Fluidd stores it as
   `config/setContainerColumnCount`). Two columns makes each card about half the window; more
   columns makes every card, camera included, narrower.

So "move the camera card to a narrower column" means: layout editor, drag `camera-card` into the
container you want, and set the column count so that container is the narrow one. There is no
per-card width in Fluidd.

The bridge's floating card (7.4) has its own S / M / L instead, because nothing in Fluidd's layout
owns it, and `GET /camera` on its own is `max-width: 100%; height: auto`, so it fits whatever box it
is given either way.

---

## 8. 2026-09-13/14: the fork, and Fluidd's own camera card playing this printer

Sections 6 and 7 concluded twice that Fluidd could not show this camera: first because no camera
type speaks Creality's signalling, then because the card is filtered out before the type is even
read. Both conclusions were right about *stock* Fluidd. Neither is fixable from outside the app,
because Fluidd has no plugin system. So the app was forked. `docs/FLUIDD_FORK.md` is the fork's own
page; this section is only the camera half.

### 8.1 The two changes that make the card appear

Section 7.3 measured the cause: this Moonraker reports `api_version 1.0.5`, and its
`/server/webcams/list` carries no `enabled` and no `uid`. Upstream:

```js
getEnabledWebcams: (r, e) => e.getWebcams.filter(s => s.enabled),
getWebcamById: r => e => r.webcams.find(t => t.uid === e)
```

The fork, in `src/store/webcams/getters.ts`:

```ts
.filter(webcam => webcam.enabled !== false)
```

One operator. A record from a current Moonraker always carries the field and behaves exactly as
before; a record from an older one now counts as enabled instead of vanishing.

`uid` needs more than a default, because Fluidd keys the active camera, the fullscreen route and
`getWebcamById` off it, and `undefined === undefined` would make every camera the same camera. The
fork synthesises one in `setWebcamsList` (`src/store/webcams/mutations.ts`) from the record's name,
which is unique inside Moonraker's webcam store and therefore stable across reloads:

```ts
uid: webcam.uid || `name:${webcam.name ?? ''}`
```

Records that already carry both fields are returned untouched, so this is inert on a current
Moonraker.

### 8.2 The `creality-webrtc` service

`src/components/widgets/camera/services/CrealityWebrtcCamera.vue`. It is the JavaScript in
`camera.py`'s page, restructured as a Vue component and nothing else changed, because every part of
it is load-bearing and was measured (sections 1.2, 1.3, 3.4 and 7.2):

* `new RTCPeerConnection({ iceServers: [] })`;
* `addTransceiver('video', { direction: 'sendrecv' })`, not `recvonly`;
* non-trickle: the offer is sent only after `iceGatheringState === 'complete'`, with a three second
  ceiling;
* `singleH264()` reduces the offer to one H264 codec whose payload type is the lowest in 96-127;
* `realCandidates()` replaces the address in every `.local` candidate;
* `POST` with `Content-Type: plain/text`, body `btoa(JSON.stringify({type: 'offer', sdp}))`, answer
  `JSON.parse(atob(text))`;
* a twelve second frame watchdog that reconnects if signalling succeeded and no pixels arrived.

No registration is needed anywhere. Upstream's `CameraItem` resolves a service to a component by
`startCase(service).replace(/ /g, '') + 'Camera'`, and `creality-webrtc` lands on
`CrealityWebrtcCamera` on its own. It appears in the stream-type dropdown as **WebRTC (Creality
K2)**.

### 8.3 `GET /camera/local_address`

Section 7.2 established that the mDNS rewrite is load-bearing: the device reads the address out of
the offer's ICE candidates and DTLS-handshakes to it, so with Chromium's `.local` candidate in
there ICE connects, `dtlsState` stays at `connecting` forever, and no frame ever arrives.

The bridge's own page could simply be generated with the address in it, because the bridge computes
it in Python. A component in a bundle cannot: **a browser has no way to read its own LAN address**,
which is the entire point of the mDNS candidate. So the bridge exposes what `camera.local_address()`
already worked out:

```
GET /camera/local_address
{"ok": true, "address": "<this PC's LAN address>", "printer": "192.168.1.50",
 "signalling_url": "http://192.168.1.50:8000/call/webrtc_local"}
```

The component fetches it once per page and remembers the answer, including an empty one, so a bridge
that cannot work the address out is not polled on every reconnect. The address reported is the one on
the same network as the printer; a virtual adapter's NAT address is the wrong answer and
`local_address()` rejects it, as section 7.2 describes.

### 8.4 The Moonraker webcam records

The bridge now keeps two, and writes either only when the stored record differs
(`camera.sync_webcams`, once at startup, off the request path):

| Name | Service | Stream URL | For |
| --- | --- | --- | --- |
| `K2 Plus camera` | `creality-webrtc` | `http://192.168.1.50:8000/call/webrtc_local` | the fork's own camera card |

This is a Moonraker **configuration** write, not a printer-state write: it changes a row in
Moonraker's database and touches no motion, no heater and no filament path. The comparison ignores
`enabled`, `uid`, `icon` and `aspect_ratio`, because Moonraker 1.0.5 drops those on the way in and
comparing them would rewrite both records on every start.

Both records showed up in the fork side by side and both played.

### 8.5 Measured, 2026-09-13, printer mid-job

| | |
| --- | --- |
| Dashboard camera card | plays, `videoWidth` 1920, `videoHeight` 1080, `currentTime` advancing, `paused` false |
| Camera page (`#/camera`) | same, full width |
| Layout mode | **Cameras** listed with its toggle and drag handle |
| Collapse, persisted | collapsing the camera card and reloading keeps it collapsed |
| Console | no errors, no warnings, across the dashboard, both nav pages and a reload |

So the answer to section 6's question, "can Fluidd's camera card play it", is now: not the
printer's Fluidd, and not any stock Fluidd, but yes, the fork's, natively, as an ordinary camera
card that can be moved, collapsed and hidden with the rest of the dashboard.

### 8.6 What section 7's routes do now

`GET /camera` is unchanged and still the right thing for an embed or a plain browser tab. What moved
is `GET /fluidd`: it was the printer's Fluidd in a frame with two floating cards drawn on top, and
it is now the fork's build, served from `cfsbridge/fluidd/`. The old page is still there at
`GET /fluidd-shell`, because it is the only thing that works in a checkout with no build in it.

Update 2026-09-14: the `K2 Plus camera (bridge page)` iframe record was removed. The stock Fluidd cannot draw a camera card on this Moonraker whatever the service, and in the fork it only produced a second, dead camera tile. `sync_webcams` now deletes that record if it finds it (`STALE_WEBCAMS` in camera.py).
