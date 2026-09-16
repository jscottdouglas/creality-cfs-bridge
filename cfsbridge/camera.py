"""K2 Plus camera access.

The printer exposes no JPEG snapshot and no MJPEG stream. Every plain HTTP path
on port 8000 returns a zero-length 200, and Moonraker reports an empty
snapshot_url. The only way to a frame is the WebRTC signalling endpoint. See
docs/CAMERA.md for the live probe results and the CrealityPrint citations.

  POST http://<ip>:8000/call/webrtc_local
  Content-Type: plain/text
  body = base64(json({"type":"offer","sdp":"<offer sdp>"}))
  ->    base64(json({"type":"answer","sdp":"..."}))

That needs a real WebRTC stack, so `snapshot()` requires aiortc and av. Neither
is a dependency of the rest of cfsbridge; install them only if you want the
camera:

  pip install aiortc av

The signalling POST is camera-only and cannot affect a running print, but it is
still a POST, so nothing in this module runs unless you ask for it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
from typing import Optional

from . import CAMERA_PORT

SIGNALLING_PATH = "/call/webrtc_local"
IPV4 = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def signalling_url(host: str, port: int = CAMERA_PORT) -> str:
    return "http://%s:%d%s" % (host, port, SIGNALLING_PATH)


def encode_offer(sdp: str) -> str:
    """The body CrealityPrint posts: base64 of {"type":"offer","sdp":...}."""
    return base64.b64encode(
        json.dumps({"type": "offer", "sdp": sdp}).encode("utf-8")
    ).decode("ascii")


def decode_answer(body: str) -> dict:
    return json.loads(base64.b64decode(body.strip()))


class CameraUnavailable(RuntimeError):
    pass


# -- the Moonraker webcam records ------------------------------------------
#
# Moonraker keeps the webcam list in its own database, and both Fluidds read it
# from there. The forked Fluidd can play this camera, so the printer's entry
# should name the service the fork implements; the stock Fluidd on port 4408
# cannot play it whatever the service says (docs/CAMERA.md section 7.3), so a
# second entry pointing at this bridge's own `/camera` page is kept beside it
# as the fallback for the day that Moonraker is updated.
#
# Writing these is a Moonraker configuration write, not a printer-state write:
# it changes a row in Moonraker's database and touches no motion, no heater and
# no filament path. It is still a POST, so it happens once at startup and only
# when the stored record differs from the wanted one.

CREALITY_SERVICE = "creality-webrtc"
PRIMARY_WEBCAM = "K2 Plus camera"
# An iframe record by this name was written once for the printer's stock Fluidd.
# That Fluidd cannot draw a camera card on this Moonraker whatever the service
# says, and in the fork the record only produced a second, dead camera tile, so
# the bridge now removes it when it finds it.
STALE_WEBCAMS = ("K2 Plus camera (bridge page)",)

# The fields Moonraker 1.0.5 actually round-trips. It silently drops `enabled`,
# `uid`, `icon` and `aspect_ratio` from anything posted to it, which is the
# whole reason the fork has to treat a missing `enabled` as true.
WEBCAM_FIELDS = ("name", "location", "service", "target_fps", "stream_url",
                 "snapshot_url", "flip_horizontal", "flip_vertical", "rotation")


def wanted_webcams(host: str, bridge_url: str,
                   port: int = CAMERA_PORT) -> list[dict]:
    """The one record the bridge would like Moonraker to hold.

    `bridge_url` is kept in the signature for callers and tests; the fork plays
    the printer's stream itself, so no record points back at the bridge.
    """
    return [
        {
            "name": PRIMARY_WEBCAM,
            "location": "printer",
            "service": CREALITY_SERVICE,
            "target_fps": 15,
            "stream_url": signalling_url(host, port),
            "snapshot_url": "",
            "flip_horizontal": False,
            "flip_vertical": False,
            "rotation": 0,
        },
    ]


def webcam_differs(stored: Optional[dict], wanted: dict) -> bool:
    """True when Moonraker's record is not the one the bridge wants.

    Only the fields Moonraker keeps are compared, so an entry is never
    rewritten because of a key the server dropped on the way in.
    """
    if not stored:
        return True
    for key in WEBCAM_FIELDS:
        if str(stored.get(key, "")) != str(wanted.get(key, "")):
            return True
    return False


def read_webcams(host: str, port: int = 7125, timeout: float = 8.0) -> Optional[list]:
    """`GET /server/webcams/list`. Read-only. None when Moonraker is quiet."""
    import requests

    try:
        response = requests.get("http://%s:%d/server/webcams/list" % (host, port),
                                timeout=timeout)
        response.raise_for_status()
        return (response.json().get("result") or {}).get("webcams") or []
    except Exception:
        return None


def sync_webcams(host: str, bridge_url: str, moonraker_port: int = 7125,
                 camera_port: int = CAMERA_PORT, dry_run: bool = False,
                 timeout: float = 8.0) -> dict:
    """Bring Moonraker's webcam records in line, writing only what differs.

    Returns `{"ok", "checked", "written", "skipped", "message"}`. Nothing here
    raises: a printer that is not answering is a log line, not a failed start.
    """
    import requests

    wanted = wanted_webcams(host, bridge_url, camera_port)
    stored = read_webcams(host, moonraker_port, timeout)
    if stored is None:
        return {"ok": False, "checked": False, "written": [], "skipped": [],
                "message": "Moonraker did not answer, so the webcam records "
                           "were left alone."}
    by_name = {str(entry.get("name") or ""): entry for entry in stored}
    written, skipped, failed, removed = [], [], [], []
    for name in STALE_WEBCAMS:
        if name not in by_name:
            continue
        if dry_run:
            removed.append(name)
            continue
        try:
            response = requests.delete(
                "http://%s:%d/server/webcams/item" % (host, moonraker_port),
                params={"name": name}, timeout=timeout)
            response.raise_for_status()
            removed.append(name)
        except Exception as exc:
            failed.append("%s (%s)" % (name, exc))
    for entry in wanted:
        name = entry["name"]
        if not webcam_differs(by_name.get(name), entry):
            skipped.append(name)
            continue
        if dry_run:
            written.append(name)
            continue
        try:
            response = requests.post(
                "http://%s:%d/server/webcams/item" % (host, moonraker_port),
                json=entry, timeout=timeout)
            response.raise_for_status()
            written.append(name)
        except Exception as exc:
            failed.append("%s (%s)" % (name, exc))
    parts = []
    if written:
        parts.append("wrote " + ", ".join(written))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if skipped:
        parts.append("already correct: " + ", ".join(skipped))
    if failed:
        parts.append("failed: " + "; ".join(failed))
    return {"ok": not failed, "checked": True, "written": written,
            "removed": removed, "skipped": skipped, "failed": failed,
            "message": "Moonraker webcams: " + ("; ".join(parts) or "nothing to do")}


async def _grab(host: str, out_path: str, timeout: float) -> str:
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription
    except ImportError as exc:
        raise CameraUnavailable(
            "the camera needs aiortc and av. Install them with:\n"
            "  pip install aiortc av\n"
            "See docs/CAMERA.md for why no simpler path exists."
        ) from exc
    import requests

    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="sendrecv")
    frame_future: asyncio.Future = asyncio.get_event_loop().create_future()

    @pc.on("track")
    def on_track(track):  # pragma: no cover - needs a live camera
        async def pump():
            try:
                frame = await track.recv()
                if not frame_future.done():
                    frame_future.set_result(frame)
            except Exception as exc:
                if not frame_future.done():
                    frame_future.set_exception(exc)

        asyncio.ensure_future(pump())

    await pc.setLocalDescription(await pc.createOffer())
    # CrealityPrint waits for ICE gathering to finish before it posts the offer.
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)

    response = requests.post(
        signalling_url(host),
        data=encode_offer(pc.localDescription.sdp),
        headers={"Content-Type": "plain/text"},
        timeout=15,
    )
    response.raise_for_status()
    answer = decode_answer(response.text)
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
    )

    try:
        frame = await asyncio.wait_for(frame_future, timeout=timeout)
    finally:
        await pc.close()

    image = frame.to_image()
    image.save(out_path, quality=90)
    return out_path


def snapshot(host: str, out_path: str, timeout: float = 20.0) -> str:
    """Grab one frame over WebRTC and write it as a JPEG."""
    return asyncio.run(_grab(host, out_path, timeout))


def mjpeg_note() -> str:
    return (
        "This printer serves no MJPEG stream. A relay would have to decode "
        "WebRTC H264 and re-encode, which is what CrealityPrint does natively on "
        "Linux (HttpServer.cpp:160). cfsbridge does not implement it; see "
        "docs/CAMERA.md."
    )


# -- the embeddable camera page -------------------------------------------
#
# `GET /camera` on the bridge serves this. It is the same signalling the Orca
# fork's generated page does (docs/CAMERA.md sections 1.2 to 1.5), stripped of
# every piece of page furniture so that it can be dropped into Fluidd's
# "iframe" camera card and look like a normal camera tile: the video fills the
# frame, the background is black, and the only text is a small overlay that
# disappears the moment frames arrive.


def same_network(one: str, other: str) -> bool:
    """A /24 comparison, and False for anything that is not two IPv4 literals."""
    if not (IPV4.match(one or "") and IPV4.match(other or "")):
        return False
    return one.split(".")[:3] == other.split(".")[:3]


def route_address(host: str, port: int = CAMERA_PORT) -> str:
    """The address this machine reaches `host` from, or "".

    A connected UDP socket is the same trick CrealityPrint plays in
    DeviceMgrRoutes.cpp:149. No packet is sent: connect() on a datagram socket
    only picks a route.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, port))
        return sock.getsockname()[0] or ""
    except OSError:
        return ""
    finally:
        sock.close()


def windows_address(host: str, timeout: float = 20.0) -> str:
    """The Windows side's own address on `host`'s network, or "".

    When the bridge runs in a different OS or container from the browser that
    plays the camera, the bridge's own NAT address is not one the printer could
    ever send to. Since the address has to be right (see `local_address`), ask
    Windows which address it reaches the printer from. Anything that goes wrong
    here is an empty string, never an exception: this is a convenience, and the
    `lan` query parameter is the reliable override.
    """
    if not IPV4.match(host or ""):
        return ""
    if not os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        return ""
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
               "(Find-NetRoute -RemoteIPAddress %s).IPAddress" % host]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    for match in re.finditer(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", done.stdout or ""):
        found = match.group(0)
        if found != host and same_network(found, host):
            return found
    return ""


def local_address(host: str, port: int = CAMERA_PORT) -> str:
    """The address the *browser* should write into its ICE candidates, or "".

    Chromium hides the real LAN address behind an mDNS `.local` candidate. The
    K2's WebRTC stack resolves nothing: it reads the address out of the offer
    and DTLS-handshakes to it, so with a `.local` in there ICE connects, the
    DTLS ClientHello goes out, and no frame ever arrives. Verified on
    2026-09-13; docs/CAMERA.md section 7.

    So the address matters, and it must be an address of the machine running
    the browser. Two sources, in order:

    1. this machine's own route to the printer, when that lands on the
       printer's network (the bridge and the browser are then the same host);
    2. Windows' route to the printer, when the bridge runs in a different OS or
       container from the browser.

    If neither answers, the page is served with no rewrite and says so. Pass
    `?lan=<address>` to `GET /camera` to settle it by hand.
    """
    mine = route_address(host, port)
    if same_network(mine, host):
        return mine
    return windows_address(host)


CAMERA_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K2 Plus camera</title>
<style>
  :root { color-scheme: dark; --cam-width: 100%; }
  html, body { margin: 0; padding: 0; width: 100%; height: 100%;
               background: #000; }
  /* The page has to survive two very different frames: Fluidd's camera card,
     whose width comes from the dashboard column it sits in, and a full window.
     So nothing here has a fixed size. The video is a percentage of whatever
     the container turns out to be, never wider than it, and free in height. */
  body { position: relative; display: flex; align-items: center;
         justify-content: center; overflow: hidden; cursor: pointer; }
  video { display: block; width: var(--cam-width); max-width: 100%;
          height: auto; max-height: 100%; background: #000; }
  #status { position: absolute; left: 8px; top: 8px; max-width: calc(100% - 120px);
            padding: 3px 8px; border-radius: 3px;
            background: rgba(0, 0, 0, 0.62); color: #d8d8d8;
            font: 12px/1.4 "Segoe UI", system-ui, sans-serif;
            pointer-events: none; transition: opacity 0.4s ease; }
  #status.bad { color: #e0a24a; }
  #status.gone { opacity: 0; }
  #size { position: absolute; right: 8px; top: 8px; display: flex; gap: 3px;
          opacity: 0.28; transition: opacity 0.25s ease; }
  body:hover #size { opacity: 1; }
  #size button { padding: 2px 8px; border: 1px solid #3d3d3d; border-radius: 3px;
                 background: rgba(24, 24, 24, 0.82); color: #d8d8d8; cursor: pointer;
                 font: 11px/1.4 "Segoe UI", system-ui, sans-serif; }
  #size button:hover { background: rgba(56, 56, 56, 0.92); }
  #size button.on { background: #3f5f7f; border-color: #567f9f; color: #fff; }
  #size[hidden] { display: none; }
</style>
</head>
<body title="Click the picture to reconnect">
<video id="video" autoplay muted playsinline></video>
<div id="status">Starting.</div>
<div id="size"__BARE__>
  <button type="button" data-size="45">S</button>
  <button type="button" data-size="70">M</button>
  <button type="button" data-size="100">L</button>
</div>
<script>
(function () {
  "use strict";
  var IP       = "__IP__";
  var LOCAL_IP = "__LOCAL_IP__";
  var SIGNAL   = "__SIGNAL__";

  var video  = document.getElementById("video");
  var status = document.getElementById("status");
  var pc = null, retryTimer = null, frameTimer = null, generation = 0;

  function say(text, kind) {
    status.textContent = text;
    status.className = kind || "";
  }

  function hide() { status.className = status.className.replace(/\s*gone/, "") + " gone"; }

  // Hand the device a single H264 codec, the way CrealityPrint's fr() does.
  // The device echoes the offered payload type straight back into its answer,
  // and an offer with no valid H264 payload type in 96-127 makes it emit
  // garbage. docs/CAMERA.md sections 1.3 and 3.4.
  function singleH264(sdp) {
    var lines = sdp.split(/\r\n|\n/), keep = -1, i, m;
    for (i = 0; i < lines.length; i++) {
      m = /^a=rtpmap:(\d+) H264\/90000/i.exec(lines[i]);
      if (m) {
        var pt = parseInt(m[1], 10);
        if (pt >= 96 && pt <= 127 && (keep < 0 || pt < keep)) keep = pt;
      }
    }
    if (keep < 0) return sdp;
    var out = [], inVideo = false;
    for (i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf("m=") === 0) {
        inVideo = line.indexOf("m=video") === 0;
        if (inVideo) {
          var parts = line.split(" ");
          out.push(parts.slice(0, 3).join(" ") + " " + keep);
          continue;
        }
      }
      if (inVideo) {
        m = /^a=(?:rtpmap|fmtp|rtcp-fb):(\d+)/.exec(line);
        if (m && parseInt(m[1], 10) !== keep) continue;
      }
      out.push(line);
    }
    return out.join("\r\n");
  }

  // Chromium hides the real LAN address behind an mDNS ".local" candidate and
  // the printer cannot resolve it, so put the real address back.
  function realCandidates(sdp) {
    if (!LOCAL_IP) return sdp;
    return sdp.split(/\r\n|\n/).map(function (line) {
      if (line.indexOf("a=candidate:") !== 0) return line;
      var p = line.split(" ");
      if (p.length > 4 && /\.local$/i.test(p[4])) p[4] = LOCAL_IP;
      return p.join(" ");
    }).join("\r\n");
  }

  // Non-trickle: the offer is only sent once gathering is done, which is what
  // the device expects. docs/CAMERA.md section 1.2.
  function iceComplete(peer) {
    if (peer.iceGatheringState === "complete") return Promise.resolve();
    return new Promise(function (resolve) {
      var timer = setTimeout(finish, 3000);
      function finish() {
        clearTimeout(timer);
        peer.removeEventListener("icegatheringstatechange", onChange);
        resolve();
      }
      function onChange() { if (peer.iceGatheringState === "complete") finish(); }
      peer.addEventListener("icegatheringstatechange", onChange);
    });
  }

  function teardown() {
    generation++;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    if (frameTimer) { clearTimeout(frameTimer); frameTimer = null; }
    if (pc) { try { pc.close(); } catch (e) {} pc = null; }
    // Deliberately not clearing video.srcObject: setting it to null restarts
    // the media element's load algorithm against the document URL. The last
    // frame simply stays on screen until the next stream arrives, which also
    // looks better inside a camera card.
  }

  function later(seconds) {
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = setTimeout(start, seconds * 1000);
  }

  function start() {
    teardown();
    var mine = generation;
    say("Connecting to the camera at " + IP + ".");
    if (!window.RTCPeerConnection) {
      say("This browser gives the page no RTCPeerConnection, so the camera "
          + "cannot be played here.", "bad");
      return;
    }
    pc = new RTCPeerConnection({ iceServers: [] });
    // sendrecv, not recvonly: with recvonly this hardware completes signalling
    // and then never sends a frame. docs/CAMERA.md section 1.2.
    pc.addTransceiver("video", { direction: "sendrecv" });
    pc.ontrack = function (event) {
      video.srcObject = event.streams[0];
      video.play().catch(function () {});
    };
    pc.oniceconnectionstatechange = function () {
      if (!pc || mine !== generation) return;
      var state = pc.iceConnectionState;
      if (state === "failed" || state === "disconnected" || state === "closed") {
        say("The camera stream dropped. Reconnecting.", "bad");
        later(5);
      }
    };

    pc.createOffer().then(function (offer) {
      return pc.setLocalDescription(offer);
    }).then(function () {
      return iceComplete(pc);
    }).then(function () {
      var sdp = realCandidates(singleH264(pc.localDescription.sdp));
      return fetch(SIGNAL, {
        method: "POST",
        headers: { "Content-Type": "plain/text" },
        body: btoa(JSON.stringify({ type: "offer", sdp: sdp }))
      });
    }).then(function (response) {
      return response.text();
    }).then(function (text) {
      if (mine !== generation) return;
      var answer;
      try {
        answer = JSON.parse(atob(text.trim()));
      } catch (e) {
        say("The camera did not return an answer (" + text.slice(0, 40) + "). Retrying.", "bad");
        later(5);
        return;
      }
      if (!answer || !answer.sdp) {
        say("The camera returned an empty answer. Retrying.", "bad");
        later(5);
        return;
      }
      return pc.setRemoteDescription(new RTCSessionDescription({ type: "answer", sdp: answer.sdp }))
        .then(function () {
          say("Negotiated. Waiting for the first frame.");
          frameTimer = setTimeout(function () {
            if (mine !== generation || video.videoWidth) return;
            // The one thing that reliably causes this: the printer read an
            // mDNS ".local" address out of the offer and handshook to it.
            say(LOCAL_IP
                ? "Signalling worked but no video arrived. Reconnecting."
                : "Signalling worked but no video arrived, and this page was "
                  + "given no LAN address to put in its ICE candidates. Add "
                  + "?lan=<this machine's address> to the camera URL.", "bad");
            later(LOCAL_IP ? 2 : 20);
          }, 12000);
        });
    }).catch(function (error) {
      if (mine !== generation) return;
      say("Could not reach the camera: " + error + ". Click to retry.", "bad");
      later(5);
    });
  }

  // Size. Three presets, expressed as a percentage of the container, so the
  // card still tracks its column when Fluidd's layout changes. localStorage so
  // the choice survives a reload; a blocked or absent localStorage just means
  // the default, never an error.
  var SIZE_KEY = "cfsbridge.camera.size";
  var sizes = document.getElementById("size");
  var buttons = sizes.querySelectorAll("button");

  function applySize(value, remember) {
    var wanted = String(parseInt(value, 10) || 100);
    document.documentElement.style.setProperty("--cam-width", wanted + "%");
    for (var i = 0; i < buttons.length; i++)
      buttons[i].className = buttons[i].getAttribute("data-size") === wanted ? "on" : "";
    if (remember) { try { localStorage.setItem(SIZE_KEY, wanted); } catch (e) {} }
  }

  var stored = null;
  if (!sizes.hidden) { try { stored = localStorage.getItem(SIZE_KEY); } catch (e) {} }
  applySize(stored || "100", false);

  sizes.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button") : null;
    if (!button) return;
    // The picture is click-to-reconnect; the buttons must not trigger that.
    event.stopPropagation();
    applySize(button.getAttribute("data-size"), true);
  });

  // The overlay is the only other chrome on the page, and it goes away as soon
  // as there are pixels behind it.
  video.addEventListener("resize", function () { if (video.videoWidth) hide(); });
  video.addEventListener("playing", function () { if (video.videoWidth) hide(); });
  document.body.addEventListener("click", function () { start(); });
  window.addEventListener("pagehide", teardown);
  start();
})();
</script>
</body>
</html>
"""


def camera_page(host: str, port: int = CAMERA_PORT,
                local_ip: Optional[str] = None, bare: bool = False) -> str:
    """The `GET /camera` body for one printer address.

    Self contained: no CDN, no build step, no iframe, nothing fetched from
    anywhere except the printer's own signalling endpoint. `bare` drops the
    size buttons, for the case where something outside the page already owns
    the sizing.
    """
    if local_ip is None:
        local_ip = local_address(host, port)
    # It goes straight into a JavaScript string literal, and `?lan=` is a query
    # parameter, so nothing but a dotted quad is ever allowed through.
    if not IPV4.match(local_ip or ""):
        local_ip = ""
    return (CAMERA_PAGE
            .replace("__BARE__", " hidden" if bare else "")
            .replace("__SIGNAL__", signalling_url(host, port))
            .replace("__LOCAL_IP__", local_ip)
            .replace("__IP__", host))


# -- Fluidd with the floating cards ---------------------------------------
#
# The printer's own Fluidd cannot show a camera card at all: its Moonraker is
# old enough that `/server/webcams/list` carries neither `enabled` nor `uid`,
# and this Fluidd build filters on exactly those. So the bridge frames Fluidd
# and puts its own cards on top of it, which gets the same picture in the same
# place without touching either. docs/CAMERA.md section 7.
#
# There are two cards now, the camera and the CFS control panel. They are
# independent in every way that matters: separate size presets, separate
# localStorage keys, separate Hide buttons and separate default corners (the
# camera bottom right, the CFS bottom left), so neither covers the other on
# first load. Both can be dragged by their title strip, and a dragged position
# is remembered too.

FLUIDD_SHELL = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K2 Plus, Fluidd, camera and CFS</title>
<style>
  :root { color-scheme: dark; --card: 32vw; --cfs: 32vw; }
  html, body { margin: 0; padding: 0; width: 100%; height: 100%;
               background: #121212; overflow: hidden; }
  #fluidd { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
  /* Cards, not banners: each floats over Fluidd in a corner with its own
     title strip, and its width is a slice of the viewport, so it behaves on
     any display. The picture keeps 16:9 and the camera card's height follows
     it; the CFS card is given a height in vh and scrolls inside. */
  .card { position: absolute; background: #1a1a1a; border: 1px solid #2f2f2f;
          border-radius: 6px; overflow: hidden;
          box-shadow: 0 6px 22px rgba(0, 0, 0, 0.55);
          display: flex; flex-direction: column; }
  #card { right: 16px; bottom: 16px; width: var(--card);
          min-width: 240px; max-width: calc(100% - 32px); }
  #cfscard { left: 16px; bottom: 16px; width: var(--cfs);
             min-width: 280px; max-width: calc(100% - 32px); }
  #camera { width: 100%; aspect-ratio: 16 / 9; border: 0; display: block;
            background: #000; }
  #cfs { width: 100%; height: var(--cfsheight, 46vh); border: 0; display: block;
         background: #1a1a1a; }
  .bar { display: flex; align-items: center; gap: 3px; padding: 4px 6px;
         background: #202020; border-bottom: 1px solid #2f2f2f;
         cursor: move; user-select: none; }
  .bar .name { flex: 1 1 auto; color: #9a9a9a; padding-left: 2px;
               font: 11px/1.4 "Segoe UI", system-ui, sans-serif; }
  button { padding: 2px 8px; border: 1px solid #3d3d3d; border-radius: 3px;
           background: #262626; color: #d8d8d8; cursor: pointer;
           font: 11px/1.4 "Segoe UI", system-ui, sans-serif; }
  button:hover { background: #383838; }
  button.on { background: #3f5f7f; border-color: #567f9f; color: #fff; }
  #show { position: absolute; right: 16px; bottom: 16px; opacity: 0.45; }
  #showcfs { position: absolute; left: 16px; bottom: 16px; opacity: 0.45; }
  #show:hover, #showcfs:hover { opacity: 1; }
  body.dragging #fluidd, body.dragging iframe { pointer-events: none; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<iframe id="fluidd" src="__FLUIDD__" referrerpolicy="no-referrer"></iframe>
<div id="card" class="card">
  <div class="bar" id="bar">
    <span class="name">Camera</span>
    <button type="button" data-card="24">S</button>
    <button type="button" data-card="32">M</button>
    <button type="button" data-card="46">L</button>
    <button type="button" data-card="off">Hide</button>
  </div>
  <iframe id="camera" src="__CAMERA__" allow="autoplay"></iframe>
</div>
<div id="cfscard" class="card">
  <div class="bar" id="cfsbar">
    <span class="name">CFS</span>
    <button type="button" data-cfs="26">S</button>
    <button type="button" data-cfs="32">M</button>
    <button type="button" data-cfs="46">L</button>
    <button type="button" data-cfs="off">Hide</button>
  </div>
  <iframe id="cfs" src="__CFS__"></iframe>
</div>
<button id="show" type="button" hidden>Camera</button>
<button id="showcfs" type="button" hidden>CFS</button>
<script>
(function () {
  "use strict";

  function get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function put(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }

  // One sizer per card. `sizes` are viewport-width percentages; "off" hides
  // the card and reveals its small corner button instead.
  function sizer(opts) {
    var card = document.getElementById(opts.card);
    var bar = document.getElementById(opts.bar);
    var show = document.getElementById(opts.show);
    var buttons = bar.querySelectorAll("button");
    var last = opts.fallback;

    function apply(value, remember) {
      var wanted = value === "off" ? "off"
                 : String(parseInt(value, 10) || parseInt(opts.fallback, 10));
      card.hidden = wanted === "off";
      show.hidden = !card.hidden;
      if (wanted !== "off") {
        last = wanted;
        document.documentElement.style.setProperty(opts.variable, wanted + "vw");
        if (opts.heightVariable) {
          document.documentElement.style.setProperty(
            opts.heightVariable, Math.round(parseInt(wanted, 10) * 1.45) + "vh");
        }
      }
      for (var i = 0; i < buttons.length; i++)
        buttons[i].className =
          buttons[i].getAttribute(opts.attribute) === wanted ? "on" : "";
      if (remember) { put(opts.key, wanted); }  // privacy-scan: allow
    }

    apply(get(opts.key) || opts.fallback, false);  // privacy-scan: allow
    bar.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("button") : null;
      if (button) { apply(button.getAttribute(opts.attribute), true); }
    });
    show.addEventListener("click", function () { apply(last, true); });
    drag(card, bar, opts.key + ".at");  // privacy-scan: allow
  }

  // Drag by the title strip. The position is stored as a left/top pair in
  // pixels; while a drag is running every iframe stops taking pointer events,
  // because otherwise the Fluidd frame swallows the moves.
  function drag(card, bar, key) {
    var stored = get(key);
    if (stored) {
      try {
        var at = JSON.parse(stored);
        if (at && typeof at.left === "number") { place(at.left, at.top); }
      } catch (e) {}
    }

    function place(left, top) {
      var width = card.offsetWidth || 280, height = card.offsetHeight || 120;
      left = Math.max(0, Math.min(left, window.innerWidth - Math.min(width, 120)));
      top = Math.max(0, Math.min(top, window.innerHeight - 28));
      card.style.left = left + "px";
      card.style.top = top + "px";
      card.style.right = "auto";
      card.style.bottom = "auto";
    }

    bar.addEventListener("pointerdown", function (event) {
      if (event.target.closest && event.target.closest("button")) { return; }
      var box = card.getBoundingClientRect();
      var dx = event.clientX - box.left, dy = event.clientY - box.top;
      document.body.className = "dragging";
      try { bar.setPointerCapture(event.pointerId); } catch (e) {}

      function move(e) { place(e.clientX - dx, e.clientY - dy); }
      function done() {
        document.body.className = "";
        bar.removeEventListener("pointermove", move);
        bar.removeEventListener("pointerup", done);
        bar.removeEventListener("pointercancel", done);
        put(key, JSON.stringify({ left: card.offsetLeft, top: card.offsetTop }));
      }
      bar.addEventListener("pointermove", move);
      bar.addEventListener("pointerup", done);
      bar.addEventListener("pointercancel", done);
      event.preventDefault();
    });
  }

  sizer({ card: "card", bar: "bar", show: "show", key: "cfsbridge.camera.card",
          attribute: "data-card", variable: "--card", fallback: "32" });
  sizer({ card: "cfscard", bar: "cfsbar", show: "showcfs",
          key: "cfsbridge.cfs.card", attribute: "data-cfs", variable: "--cfs",
          heightVariable: "--cfsheight", fallback: "32" });
})();
</script>
</body>
</html>
"""


def fluidd_shell(fluidd_url: str, camera_url: str = "/camera?bare=1",
                 cfs_url: str = "/cfscard?bare=1") -> str:
    """Fluidd in a frame with the camera and CFS cards on top of it."""
    return (FLUIDD_SHELL
            .replace("__FLUIDD__", fluidd_url)
            .replace("__CAMERA__", camera_url)
            .replace("__CFS__", cfs_url))
