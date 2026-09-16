"""cfsbridge command line.

  cfsbridge slots
  cfsbridge send job.gcode --map T0=2B [--dry-run]
  cfsbridge send job.gcode --auto [--dry-run]
  cfsbridge serve --port 7126 --live --log ~/.cfsbridge/serve.log
  cfsbridge camera --snapshot out.jpg
  cfsbridge preset [--appdata DIR]

`send` refuses to do anything live unless --live is given; --dry-run is the
default, and it prints the exact messages it would have sent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import DEFAULT_LOG, MOONRAKER_PORT, __version__
from .bridge import METHODS, plan_send, read_slots, send_live
from .config import load_config
from .logs import setup_logging
from .mapping import MappingError
from .protocol import CrealityClient, ProtocolError
from .slots import SlotTable

NO_HOST = ("no printer address configured: pass --host, or open "
           "http://127.0.0.1:7126/setup once the bridge is running")

NO_APPDATA = ("no --appdata given and no APPDATA in the environment, so there "
              "is nowhere to write the preset: pass --appdata with the "
              "roaming application data directory")

# The slicer preset points the Device tab at this bridge, not at the printer,
# so the address is the loopback one the service binds and the port is the one
# CrealityCFSBridge.xml hard codes. Reading the bridge's own config file here
# would let the preset disagree with the service that is actually listening.
PRESET_HOST = "127.0.0.1"
PRESET_PORT = 7126


def _add_common(parser: argparse.ArgumentParser,
                moonraker_default: Optional[int] = MOONRAKER_PORT) -> None:
    parser.add_argument("--host", default=None,
                        help="printer IP (default: the address saved on the "
                             "bridge's setup page)")
    # `serve` passes None so that "no flag" is distinguishable from "7125" and
    # the config file's value can win. Every other command has no config file
    # key to fall back to, so it keeps the plain default.
    parser.add_argument("--moonraker-port", type=int, default=moonraker_default)


def _config_host(args: argparse.Namespace) -> str:
    """The printer address, or "" when nothing has been configured yet."""
    if getattr(args, "host", None):
        return args.host
    return load_config()["host"]


def _resolve_host(args: argparse.Namespace) -> str:
    """Like _config_host, but for the commands that cannot work without one."""
    host = _config_host(args)
    if not host:
        raise SystemExit(NO_HOST)
    return host


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cfsbridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="cfsbridge " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_slots = sub.add_parser("slots", help="show the live CFS contents")
    _add_common(p_slots)
    p_slots.add_argument("--json", action="store_true", help="machine readable output")

    p_send = sub.add_parser("send", help="upload a G-code file and start it with a slot mapping")
    _add_common(p_send)
    p_send.add_argument("file", help="the .gcode file to send")
    p_send.add_argument("--map", dest="maps", action="append", default=[],
                        metavar="TOOL=SLOT", help="for example --map T0=2B (repeatable)")
    p_send.add_argument("--auto", action="store_true",
                        help="pick slots from the Orca filament headers")
    p_send.add_argument("--method", choices=METHODS, default="colormatch",
                        help="colormatch sends the mapping over the protocol "
                             "(what Creality Print does); gcode rewrites the tool "
                             "numbers instead (default %(default)s)")
    p_send.add_argument("--name", help="name to store the file under on the printer")
    p_send.add_argument("--self-test", type=int, default=0, choices=(0, 1),
                        help="printer calibration before the job (default 0)")
    p_send.add_argument("--allow-type-mismatch", action="store_true",
                        help="let --auto pick a slot whose material type differs")
    p_send.add_argument("--multi-color", dest="multi", action="store_true", default=None,
                        help="force the CFS multi-colour start message")
    p_send.add_argument("--no-multi-color", dest="multi", action="store_false",
                        help="force the plain start message")
    group = p_send.add_mutually_exclusive_group()
    group.add_argument("--dry-run", dest="live", action="store_false", default=False,
                       help="print what would be sent and send nothing (default)")
    group.add_argument("--live", dest="live", action="store_true",
                       help="really upload and really start the job")

    p_serve = sub.add_parser("serve", help="an OctoPrint-compatible host Orca can print to")
    _add_common(p_serve, moonraker_default=None)
    p_serve.add_argument("--port", type=int, default=None,
                         help="bridge port (default: the port in the config "
                              "file, or 7126)")
    p_serve.add_argument("--bind", default="127.0.0.1")
    p_serve.add_argument("--method", choices=METHODS, default="colormatch")
    serve_live = p_serve.add_mutually_exclusive_group()
    serve_live.add_argument("--live", dest="live", action="store_true",
                            default=None,
                            help="actually start jobs; without this the facade "
                                 "accepts uploads and reports the mapping only "
                                 "(default: the config file's setting)")
    serve_live.add_argument("--dry-run", dest="live", action="store_false",
                            help="accept uploads and report the mapping only, "
                                 "whatever the config file says")
    p_serve.add_argument("--allow-type-mismatch", action="store_true")
    p_serve.add_argument("--log", dest="log_path", default=None, metavar="PATH",
                         help="append to this file, rotating at 1 MB, five files "
                              "kept (for example %s)" % DEFAULT_LOG)
    p_serve.add_argument("--spool-dir", default=None,
                         help="where uploaded files are kept until they start")
    p_serve.add_argument("--poll-interval", type=float, default=5.0,
                         help="seconds between printer reads (default %(default)s)")
    p_serve.add_argument("--fluidd-dist", default=None, metavar="DIR",
                         help="the forked Fluidd build to serve at /fluidd/ "
                              "(default cfsbridge/fluidd in this checkout; "
                              "CFSBRIDGE_FLUIDD_DIST also works)")
    p_serve.add_argument("--no-webcam-sync", dest="sync_webcams",
                         action="store_false", default=True,
                         help="leave Moonraker's webcam records alone instead "
                              "of pointing them at the fork's camera service")

    p_cam = sub.add_parser("camera", help="camera access, see docs/CAMERA.md")
    _add_common(p_cam)
    p_cam.add_argument("--snapshot", metavar="OUT.jpg",
                       help="grab one frame over WebRTC (needs aiortc and av)")
    p_cam.add_argument("--timeout", type=float, default=20.0)

    # The installer runs this as the logged-in user and deliberately passes no
    # --appdata, so that the preset lands in that user's own profile rather
    # than in whichever profile the elevated setup process happens to have.
    # The flag stays available for a manual run and for the tests.
    p_preset = sub.add_parser("preset", help="write the slicer machine preset "
                                             "that points the Device tab here")
    p_preset.add_argument("--appdata", default=os.environ.get("APPDATA", ""),
                          metavar="DIR",
                          help="the roaming application data directory "
                               "(default: APPDATA in the environment)")

    return parser


# -- slots ----------------------------------------------------------------

def format_slots(table: SlotTable) -> str:
    header = "%-5s %-7s %-8s %-11s %-8s %-6s" % (
        "SLOT", "TYPE", "COLOUR", "VENDOR", "REMAIN", "STATE")
    lines = [header, "-" * len(header)]
    for unit in range(1, 5):
        unit_slots = [s for s in table.slots if s.unit == unit]
        if not unit_slots:
            continue
        present = "connected" if unit in table.units_present else "not connected"
        lines.append("CFS %d (T%d, %s)" % (unit, unit, present))
        for s in unit_slots:
            if not s.present or not s.material_type:
                lines.append("  %-3s %-7s %-8s %-11s %-8s %-6s"
                             % (s.label, "-", "-", "-", "-", "empty"))
                continue
            remain = "%.1f m" % s.remain_len if s.remain_len is not None else "-"
            state = []
            if s.loaded:
                state.append("LOADED")
            if s.mapped:
                state.append("mapped")
            if s.edited:
                state.append("manual")
            lines.append(
                "  %-3s %-7s %-8s %-11s %-8s %-6s"
                % (s.label, s.material_type, s.colour or "-", (s.vendor or "-")[:11],
                   remain, ",".join(state) or "ok")
            )
        lines.append("")
    lines.append("Tool numbers: a bare T<n> in G-code means slot index n, so "
                 "T0=1A, T4=2A, T5=2B, T15=4D.")
    if table.gcode_map:
        if table.map_is_identity:
            lines.append("Slot map: identity. T0 would load 1A, T1 would load 1B, and so on.")
        else:
            changed = ["%s -> %s" % (k[1:], v[1:])
                       for k, v in sorted(table.gcode_map.items()) if k != v]
            lines.append("Slot map: NOT identity. The printer currently redirects "
                         + ", ".join(changed) + ".")
            lines.append("           A job's T0 would therefore load %s, not 1A."
                         % (table.resolves_to(0) or "?"))
    return "\n".join(lines)


def cmd_slots(args: argparse.Namespace) -> int:
    host = _resolve_host(args)
    with CrealityClient(host, dry_run=True) as client:
        table = read_slots(client, host, args.moonraker_port)
    if args.json:
        payload = [
            {
                "slot": s.label, "tnn": s.tnn, "tool_index": s.index,
                "unit": s.unit, "material_id": s.slot,
                "type": s.material_type, "colour": s.colour, "vendor": s.vendor,
                "name": s.name, "rfid": s.rfid, "percent": s.percent,
                "remain_len_m": s.remain_len, "loaded": s.loaded,
                "mapped": s.mapped, "present": s.present,
                "manually_set": s.edited,
            }
            for s in table.slots
        ]
        print(json.dumps({
            "units_present": sorted(table.units_present),
            "gcode_slot_map": table.gcode_map,
            "map_is_identity": table.map_is_identity,
            "slots": payload,
        }, indent=2))
    else:
        print(format_slots(table))
    return 0


# -- send -----------------------------------------------------------------

def cmd_send(args: argparse.Namespace) -> int:
    host = _resolve_host(args)
    if args.live:
        plan = send_live(
            host, args.file, args.maps, args.auto, args.method, args.name,
            args.self_test, args.allow_type_mismatch, args.moonraker_port, args.multi,
        )
        print(plan.describe())
        print("")
        print("uploaded : HTTP %s %s" % (plan.upload.status, plan.upload.url))
        print("started  : yes")
        return 0

    with CrealityClient(host, dry_run=True) as client:
        plan = plan_send(
            client, host, args.file, args.maps, args.auto, args.method,
            args.name, args.self_test, args.allow_type_mismatch,
            args.moonraker_port, args.multi,
        )
    print(plan.describe())
    print("")
    print("DRY RUN: nothing was uploaded and nothing was started.")
    print("Re-run with --live to actually send it.")
    if plan.rewritten_path:
        print("Rewritten G-code left at %s for inspection." % plan.rewritten_path)
    return 0


# -- serve ----------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import serve

    setup_logging(args.log_path)
    # The config file is the installed service's only configuration, so every
    # key in it has to be real: an explicit flag wins, and where there is no
    # flag the file decides. serve is also the one command that must start
    # without a printer address: with an empty host it puts up the setup page
    # instead of talking to a printer.
    cfg = load_config()
    return serve(
        host=args.host or cfg["host"], bind=args.bind, method=args.method,
        port=args.port if args.port is not None else cfg["port"],
        live=args.live if args.live is not None else cfg["live"],
        moonraker_port=(args.moonraker_port if args.moonraker_port is not None
                        else cfg["moonraker_port"]),
        allow_type_mismatch=args.allow_type_mismatch, spool_dir=args.spool_dir,
        log_path=args.log_path, poll_interval=args.poll_interval,
        fluidd_dist=args.fluidd_dist, sync_webcams=args.sync_webcams,
    )


# -- camera ---------------------------------------------------------------

def cmd_camera(args: argparse.Namespace) -> int:
    from .camera import snapshot

    if not args.snapshot:
        print("nothing to do; pass --snapshot OUT.jpg. See docs/CAMERA.md.")
        return 2
    path = snapshot(_resolve_host(args), args.snapshot, timeout=args.timeout)
    print("wrote %s" % path)
    return 0


# -- preset ---------------------------------------------------------------

def _preset_writer():
    """Return `write_preset` from installer/write_orca_preset.py.

    That file is data rather than an importable module: it lives in
    `installer/`, which is not a package and is not on sys.path, and making it
    one would publish an installer helper as an importable top-level name. So
    it is loaded from its own path in whichever of the two places it can be.

    In the frozen build the spec's `datas` puts it at `installer/` inside the
    one-folder tree, which is `sys._MEIPASS`. In a source checkout it is
    `installer/` beside the cfsbridge package. Both are tried, frozen first,
    because a frozen exe run out of a checkout should still use its own copy.
    """
    import importlib.util

    roots = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        roots.append(bundled)
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for root in roots:
        path = os.path.join(root, "installer", "write_orca_preset.py")
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location("cfsbridge._write_orca_preset", path)
        if spec is None or spec.loader is None:      # pragma: no cover
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.write_preset

    raise SystemExit("installer/write_orca_preset.py was not found, so the "
                     "preset cannot be written; looked under "
                     + " and ".join(roots))


def cmd_preset(args: argparse.Namespace) -> int:
    if not args.appdata:
        print("error: %s" % NO_APPDATA, file=sys.stderr)
        return 2
    print(_preset_writer()(args.appdata, host=PRESET_HOST, port=PRESET_PORT))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "slots": cmd_slots, "send": cmd_send,
        "serve": cmd_serve, "camera": cmd_camera,
        "preset": cmd_preset,
    }
    try:
        return handlers[args.command](args)
    except (MappingError, ProtocolError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
