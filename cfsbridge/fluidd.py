"""Serve the forked Fluidd build, and tell it where Moonraker is.

`GET /fluidd` used to be the printer's own Fluidd in an iframe with two
floating cards drawn on top, because the printer's Fluidd will not draw either
card (docs/CAMERA.md section 7). The fork removes the reason for the trick: it
has a real CFS card, a real camera card that can play this printer, and two
left-nav pages. So `GET /fluidd/` is now the fork's `dist`, served from disk.

Three things this module is responsible for.

**Where the build is.** `cfsbridge/fluidd/` inside the installed package, found
relative to this module rather than to a working directory, because the service
starts with no working directory worth relying on. The source tree it is built
from is the Fluidd fork's own checkout and is not needed at run time.
`--fluidd-dist` and `CFSBRIDGE_FLUIDD_DIST` override the location.

**`config.json`.** Fluidd reads `./config.json` on startup and uses
`endpoints` to find Moonraker (`src/init.ts`, `getApiConfig`). The file is
generated per request from the bridge's own `--host`, so nothing in the build
is printer specific and a rebuild never has to be re-edited. `blacklist` keeps
Fluidd from also probing the bridge's own origin, which speaks no websocket.

**Everything else is a static file.** Correct MIME types, no path escapes, and
a fallback to `index.html` for anything that is not a file, which matters only
if the router is ever moved off hash mode; today every client route is a
fragment and never reaches the server.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from . import MOONRAKER_PORT

# Named so a reader of the repository can tell at a glance that it is built
# output and not source. docs/FLUIDD_FORK.md says where the source is.
DIST_DIRNAME = "fluidd"

# Vite's `base: './'` means index.html asks for `./assets/...`, so the app has
# to be served from a directory with a trailing slash. `/fluidd` redirects.
MOUNT = "/fluidd"

TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".yaml": "text/yaml; charset=utf-8",
}

# Hashed asset names never change content, so they can be cached hard. Anything
# else is revalidated, because a rebuild reuses the same name.
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


def default_dist() -> str:
    """`cfsbridge/fluidd` next to this module."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DIST_DIRNAME)


def dist_dir(override: Optional[str] = None) -> str:
    """The directory to serve, honouring the flag then the environment."""
    chosen = override or os.environ.get("CFSBRIDGE_FLUIDD_DIST") or default_dist()
    return os.path.abspath(os.path.expanduser(chosen))


def content_type(path: str) -> str:
    return TYPES.get(os.path.splitext(path)[1].lower(),
                     "application/octet-stream")


def cache_control(rel: str) -> str:
    """Vite writes `assets/<name>-<hash>.<ext>`; only those are immutable."""
    return IMMUTABLE if rel.startswith("assets/") else REVALIDATE


def host_config(host: str, moonraker_port: int = MOONRAKER_PORT,
                base: Optional[dict] = None) -> dict:
    """The `config.json` Fluidd fetches on startup.

    `endpoints` is the printer's Moonraker. `blacklist` holds the loopback
    names the bridge answers on, so that `getApiConfig` does not also race a
    websocket against the bridge's own origin, which would never answer.
    `themePresets` is carried through from the build's own file when it has
    one, so the theme picker keeps its entries.
    """
    presets = []
    if isinstance(base, dict):
        presets = base.get("themePresets") or []
    return {
        "endpoints": ["http://%s:%d" % (host, moonraker_port)],
        "blacklist": ["127.0.0.1", "localhost"],
        "hosted": False,
        "themePresets": presets,
    }


class FluiddApp:
    """The static half of `GET /fluidd/...`.

    Answers are `(status, body, content_type, headers)`; the handler does the
    writing. Nothing here reads printer state, so it is cheap to call.
    """

    def __init__(self, host: str, moonraker_port: int = MOONRAKER_PORT,
                 dist: Optional[str] = None) -> None:
        self.host = host
        self.moonraker_port = moonraker_port
        self.dist = dist_dir(dist)

    # -- state -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return os.path.isfile(os.path.join(self.dist, "index.html"))

    def missing_message(self) -> str:
        return (
            "The forked Fluidd build is not in this checkout.\n\n"
            "Expected an index.html in:\n  %s\n\n"
            "Build the Fluidd fork (recipe in docs/FLUIDD_FORK.md) and\n"
            "point --fluidd-dist at its dist folder, or copy dist/ there.\n\n"
            "The stock-Fluidd fallback still works in the meantime:\n"
            "  /camera    the camera on its own\n"
            "  /cfscard   the CFS card on its own\n" % self.dist
        )

    # -- resolving ---------------------------------------------------------

    def resolve(self, rel: str) -> Optional[str]:
        """An absolute path inside the dist, or None.

        `..` and absolute segments are rejected by comparing the resolved path
        against the root, which is the only check that survives symlinks.
        """
        cleaned = rel.lstrip("/")
        if not cleaned:
            cleaned = "index.html"
        full = os.path.abspath(os.path.join(self.dist, cleaned))
        root = self.dist.rstrip(os.sep) + os.sep
        if not (full + os.sep).startswith(root):
            return None
        return full if os.path.isfile(full) else None

    def config_json(self) -> bytes:
        """`config.json`, generated, with the build's theme presets kept."""
        base = None
        packaged = os.path.join(self.dist, "config.json")
        try:
            with open(packaged, "r", encoding="utf-8") as handle:
                base = json.load(handle)
        except (OSError, ValueError):
            base = None
        payload = host_config(self.host, self.moonraker_port, base)
        return json.dumps(payload, indent=2).encode("utf-8")

    # -- serving -----------------------------------------------------------

    def serve(self, rel: str) -> tuple[int, bytes, str, dict]:
        if not self.available:
            return (503, self.missing_message().encode("utf-8"),
                    "text/plain; charset=utf-8", {"Cache-Control": "no-store"})

        cleaned = rel.lstrip("/")
        if cleaned in ("", "index.html"):
            return self._file("index.html", REVALIDATE)
        if cleaned == "config.json":
            body = self.config_json()
            return 200, body, TYPES[".json"], {"Cache-Control": "no-store"}

        found = self.resolve(cleaned)
        if found is not None:
            return self._file(cleaned, cache_control(cleaned))

        # Not a file. A real asset miss should stay a miss, so that a broken
        # build shows up as a 404 rather than as index.html with the wrong
        # content type; only extension-less paths fall back to the app.
        if os.path.splitext(cleaned)[1]:
            return (404, b"not found: /fluidd/" + cleaned.encode("utf-8"),
                    "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
        return self._file("index.html", REVALIDATE)

    def _file(self, rel: str, cache: str) -> tuple[int, bytes, str, dict]:
        full = self.resolve(rel)
        if full is None:
            return (404, b"not found: /fluidd/" + rel.encode("utf-8"),
                    "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
        with open(full, "rb") as handle:
            body = handle.read()
        return 200, body, content_type(rel), {"Cache-Control": cache}
