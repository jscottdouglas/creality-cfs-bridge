"""The K2 Plus nozzle camera, as an MJPEG stream the forked Fluidd can show.

The stock firmware keeps the nozzle camera powered off (GPIO 162, driven by
`/usr/bin/nozzle_cam_power.sh`) and only switches it on for its own AI flow
check, during which `cam_sub_app` holds the device. Nothing streams it. The
nozzle LED sits on the camera board and lights whenever the board has power.

So a viewer here costs one SSH session to the printer, which:

  1. notes whether the camera was powered, and powers it on;
  2. stops `cam_sub_app` (the printer's AI flow check pauses while watched);
  3. runs `ffmpeg -c:v copy -bsf:v mjpeg2jpeg -f mjpeg -`: the camera's own
     JPEGs with Huffman tables inserted so browsers decode them, no re-encode.

When the last viewer has been gone for `IDLE_SECONDS`, the session ends and
the printer is put back: camera off if it was off, otherwise `cam_sub_app`
restarted with its stock arguments. Needs root SSH on the printer (the user
enables it on the touchscreen) and paramiko in the bridge's environment.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterator, Optional

log = logging.getLogger("cfsbridge.nozzlecam")

# The K2 Plus factory root password, printed in Creality's own docs. Only used
# when the config carries no `ssh_password` of its own.
FACTORY_PASSWORD = "creality_2024"
DEVICE = "/dev/v4l/by-id/sub-video2"
SIZE = "1280x720"
IDLE_SECONDS = 15.0
FRAME_TIMEOUT = 12.0
SUB_APP_ARGS = "-i %s -t 1 -w 1600 -h 1200 -f 5 -c" % DEVICE

# Runs on the printer. Status lines go to stderr; stdout is the MJPEG stream.
START_SCRIPT = r"""
was=$(cat /sys/class/gpio/gpio162/value 2>/dev/null || echo 1)
echo "SHPID=$$ WAS=$was" >&2
/usr/bin/nozzle_cam_power.sh on
i=0; while [ ! -e %(dev)s ] && [ $i -lt 50 ]; do sleep 0.2; i=$((i+1)); done
[ -e %(dev)s ] || { echo "ERR no nozzle camera device" >&2; exit 2; }
# Hotplug starts cam_sub_app a moment after the device appears; wait for it.
i=0; while [ -z "$(pidof cam_sub_app)" ] && [ $i -lt 15 ]; do sleep 0.2; i=$((i+1)); done
n=0
while [ $n -lt 4 ]; do
  for p in $(pidof cam_sub_app); do kill $p; done
  sleep 0.5
  start=$(date +%%s)
  ffmpeg -hide_banner -loglevel error -f v4l2 -input_format mjpeg -video_size %(size)s \
         -i %(dev)s -c:v copy -bsf:v mjpeg2jpeg -f mjpeg -
  # A run that lasted is a viewer that left (SIGPIPE): stop. A run that died at
  # once lost a race with cam_sub_app for the device: try again.
  [ $(( $(date +%%s) - start )) -ge 3 ] && break
  n=$((n+1))
done
"""

RESTORE_SCRIPT = r"""
kill %(shpid)s 2>/dev/null
for p in $(pidof ffmpeg); do grep -q sub-video2 /proc/$p/cmdline 2>/dev/null && kill $p; done
sleep 0.3
if [ "%(was)s" = "1" ]; then
  /usr/bin/nozzle_cam_power.sh off
else
  [ -n "$(pidof cam_sub_app)" ] || start-stop-daemon -S -b -x /usr/bin/cam_sub_app -- %(args)s
fi
echo restored
"""


# The page the Fluidd tile frames. Nothing streams until Start is pressed, and
# a forgotten stream stops itself, so the AI flow check is never left paused.
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Nozzle camera</title>
<style>html,body{margin:0;height:100%;background:#111;color:#ccc;font:13px sans-serif}
#wrap{position:relative;height:100%;display:flex;align-items:center;justify-content:center}
img{max-width:100%;max-height:100%;display:none}
#bar{position:absolute;top:6px;left:6px;display:flex;gap:6px;align-items:center}
button{background:#2196f3;color:#fff;border:0;border-radius:3px;padding:5px 10px;cursor:pointer}
button.stop{background:#555}#msg{padding:0 8px}</style></head><body><div id="wrap">
<img id="cam" alt="nozzle camera"><div id="bar"><button id="go">Start nozzle view</button>
<button id="stop" class="stop" hidden>Stop</button><span id="msg">Pauses the printer's AI flow check while on; stops by itself after 10 min.</span></div></div>
<script>
var cam=document.getElementById('cam'),go=document.getElementById('go'),st=document.getElementById('stop'),msg=document.getElementById('msg'),t=null;
function stop(){cam.removeAttribute('src');cam.style.display='none';go.hidden=false;st.hidden=true;msg.textContent='Stopped.';if(t){clearTimeout(t);t=null}}
go.onclick=function(){msg.textContent='Powering the nozzle camera...';cam.onload=function(){msg.textContent=''};
cam.onerror=function(){fetch('nozzle/status').then(function(r){return r.json()}).then(function(j){msg.textContent='No picture: '+(j.error||'timed out')});stop()};
cam.src='nozzle/stream?t='+Date.now();cam.style.display='block';go.hidden=true;st.hidden=false;t=setTimeout(stop,600000)};
st.onclick=stop;window.addEventListener('pagehide',stop);
document.addEventListener('visibilitychange',function(){if(document.hidden)stop()});
</script></body></html>"""


def split_jpegs(buf: bytearray) -> list:
    """Pop every complete JPEG (SOI ... EOI) off the front of `buf`."""
    out = []
    while True:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            buf.clear()
            return out
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            if start:
                del buf[:start]
            return out
        out.append(bytes(buf[start:end + 2]))
        del buf[:end + 2]


class NozzleCam:
    """One shared session per bridge; any number of viewers.

    `connect` is the seam the tests replace: it returns an object with
    `exec(command) -> channel` and `run(command) -> str` and `close()`.
    """

    def __init__(self, host_fn: Callable[[], str], user: str = "root",
                 password: str = "", connect: Optional[Callable] = None,
                 idle_seconds: float = IDLE_SECONDS) -> None:
        self.host_fn = host_fn
        self.user = user
        self.password = password or FACTORY_PASSWORD
        self.connect = connect or self._ssh
        self.idle_seconds = idle_seconds
        self.lock = threading.Condition()
        self.frame: Optional[bytes] = None
        self.frame_no = 0
        self.viewers = 0
        self.last_viewer = 0.0
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- the printer side ------------------------------------------------

    def _ssh(self, host: str):
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=self.user, password=self.password, timeout=8,
                       look_for_keys=False, allow_agent=False)

        class Session:
            def exec(self, command):
                channel = client.get_transport().open_session()
                channel.exec_command(command)
                return channel

            def run(self, command, timeout=20.0):
                _, out, _ = client.exec_command(command, timeout=timeout)
                return out.read().decode(errors="replace")

            def close(self):
                client.close()

        return Session()

    def _session(self) -> None:
        host = self.host_fn()
        session = None
        shpid = was = ""
        try:
            session = self.connect(host)
            channel = session.exec(START_SCRIPT % {"dev": DEVICE, "size": SIZE})
            buf, err = bytearray(), b""
            channel.settimeout(1.0)
            idle_since = None
            while not self._stop.is_set():
                if channel.recv_stderr_ready():
                    err += channel.recv_stderr(4096)
                    for line in err.decode(errors="replace").splitlines():
                        if line.startswith("SHPID="):
                            parts = dict(p.split("=", 1) for p in line.split())
                            shpid, was = parts.get("SHPID", ""), parts.get("WAS", "")
                        elif line.startswith("ERR "):
                            raise RuntimeError(line[4:])
                try:
                    data = channel.recv(65536)
                except Exception:
                    data = None
                if data == b"":
                    raise RuntimeError("the nozzle camera stream ended"
                                       + (": " + err.decode(errors="replace").strip()[-200:]
                                          if err.strip() else ""))
                if data:
                    buf += data
                    frames = split_jpegs(buf)
                    if frames:
                        with self.lock:
                            self.frame = frames[-1]
                            self.frame_no += 1
                            self.error = None
                            self.lock.notify_all()
                with self.lock:
                    idle = self.viewers == 0 and time.time() - self.last_viewer > self.idle_seconds
                if idle:
                    break
        except Exception as exc:
            with self.lock:
                self.error = str(exc) or type(exc).__name__
                self.lock.notify_all()
            log.warning("nozzle camera: %s", self.error)
        finally:
            if session is not None:
                try:
                    out = session.run(RESTORE_SCRIPT % {
                        "shpid": shpid or "0", "was": was or "1", "args": SUB_APP_ARGS})
                    log.info("nozzle camera session ended, printer %s", out.strip() or "restore sent")
                except Exception as exc:
                    log.warning("nozzle camera: could not restore the printer: %s", exc)
                session.close()
            with self.lock:
                self.frame = None
                self._thread = None
                self.lock.notify_all()

    # -- the viewer side -------------------------------------------------

    def _ensure_running(self) -> None:
        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.error = None
            self._thread = threading.Thread(target=self._session, daemon=True,
                                            name="nozzle-cam")
            self._thread.start()

    def frames(self) -> Iterator[bytes]:
        """Yield frames for one viewer until the stream fails or it stops reading."""
        with self.lock:
            self.viewers += 1
        try:
            self._ensure_running()
            seen = -1
            while True:
                with self.lock:
                    deadline = time.time() + FRAME_TIMEOUT
                    while self.frame_no == seen and self.error is None:
                        left = deadline - time.time()
                        if left <= 0:
                            return
                        self.lock.wait(left)
                    if self.error is not None and self.frame is None:
                        return
                    seen, frame = self.frame_no, self.frame
                if frame:
                    yield frame
        finally:
            with self.lock:
                self.viewers -= 1
                self.last_viewer = time.time()

    def snapshot(self) -> Optional[bytes]:
        for frame in self.frames():
            return frame
        return None

    def status(self) -> dict:
        with self.lock:
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "viewers": self.viewers, "frames": self.frame_no, "error": self.error}

    def stop(self) -> None:
        self._stop.set()
