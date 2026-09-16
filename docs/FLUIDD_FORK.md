# The Fluidd fork

Date: 2026-09-13/14.

Fluidd has no plugin system. Everything on this page could in principle be a
plugin and is not, so it is a fork instead, kept as small and as legible a diff
against upstream as the work allows: **33 files, 2838 lines added, 22 removed**.

---

## 1. Where it is, and exactly what it is

| | |
| --- | --- |
| Upstream | `https://github.com/fluidd-core/fluidd` |
| Upstream base, **as served today** | **v1.37.5** |
| Upstream base commit | **`281dadab3f3bc079bde05df6af6c452411ff3749`** |
| Fork | `https://github.com/jscottdouglas/fluidd`, branch **`cfs`** |
| Built output, served | `cfsbridge/fluidd/`, 262 files, 12.81 MB |
| Served at | `GET http://127.0.0.1:7126/fluidd/` |

The built bundle is not committed here: the release workflow builds the fork and packs the result
into the installer. `installer/fluidd_release.json` records the fork repository and the commit the
shipped bundle was built from: that is the tip of the fork's `cfs` branch, which sits on top of the
upstream base commit above, never the base commit itself (a build from the base is stock Fluidd
with no CFS card, and the release workflow refuses it).

The fork started on **v1.30.0**, the build the printer itself serves: the
printer's own Moonraker log reports its Fluidd as `1.30.0-f3e4ac3`, and
`git rev-parse v1.30.0` upstream is `f3e4ac3c...`, so "the printer's Fluidd cannot
do this, the fork can" began as a fair comparison rather than a version
difference. The pin moved to **v1.37.5** after a live trial against the printer;
the comparisons in sections 3, 4 and 8 were all made on the v1.30.0 build and
still hold, because none of the fork's changes was affected by the rebase in a
way that altered them. Section 6 is what that rebase cost.

**The toolchain changed with the pin.** v1.30.0 was **npm** (a
`package-lock.json`, no `packageManager` field, `engines.node` `^18 || ^20`,
`.node-version` 20.9.0), and built cleanly on node 22 anyway. v1.37.5 is
**pnpm**: build it with `pnpm install --frozen-lockfile` and `pnpm run build`.
Check `packageManager` in the fork's `package.json` before assuming either.

Build times:

| | |
| --- | --- |
| v1.37.5, `pnpm run build` cold, after `pnpm install --frozen-lockfile` | **86.2 s** |
| v1.30.0, `npm ci` plus a clean upstream `npm run build`, first run | 88 s total |
| v1.30.0, `npm run build` on its own, upstream, no changes | 55.4 s (plus 0.3 s service worker) |
| v1.30.0, `npm run build` with the fork's changes | **39.9 s** (plus 0.3 s service worker) |

A clean upstream build was confirmed working before a single line was changed.

---

## 2. Why a fork at all

Two things, neither of them reachable from outside the app.

**The camera card cannot be drawn on this printer.** Moonraker here reports
`api_version 1.0.5`, which predates the webcam schema this Fluidd expects:
`/server/webcams/list` carries no `enabled` and no `uid`, and the server
silently drops both from anything posted to `/server/webcams/item`. Upstream's
`getEnabledWebcams` filters on `webcam.enabled`, so the enabled list is always
empty, `hasCameras` is always false, and no camera card exists to configure.
Changing the service type does nothing, because the filter runs first. See
docs/CAMERA.md section 7.3 for the measurement.

**And even drawn, no upstream camera type can play it.** The K2 speaks a
Creality-specific signalling envelope on port 8000. The same offer posted three
ways: base64-of-JSON with `Content-Type: plain/text` gets a real answer SDP;
camera-streamer's raw JSON and WHEP's raw SDP both get `{}`. docs/CAMERA.md
section 6.

The CFS is the same story for a different reason: Klipper barely knows it
exists, it is driven over Creality's own port 9999 protocol, and there is no
card, no store module and no route for it anywhere in Fluidd.

---

## 3. Changed files, one line each

### Camera

| File | What changed |
| --- | --- |
| `src/store/webcams/getters.ts` | `getEnabledWebcams` filters on `enabled !== false`, so a record from an older Moonraker that carries no such field counts as enabled. |
| `src/store/webcams/mutations.ts` | `setWebcamsList` normalises every record: a missing `uid` becomes `name:<name>`, which is stable across reloads because Moonraker's webcam names are unique, and a missing `enabled` becomes true. |
| `src/store/webcams/types.ts` | `WebcamService` gains `'creality-webrtc'`. |
| `src/components/widgets/camera/services/CrealityWebrtcCamera.vue` | **New.** The Creality K2 signalling, exactly as `cfsbridge/camera.py` does it: `iceServers: []`, a `sendrecv` transceiver, a non-trickle offer, a single H264 payload type in 96-127, the mDNS `.local` candidate rewritten to the machine's real LAN address, `POST` with `Content-Type: plain/text`, base64-of-JSON in both directions. It asks the bridge for the LAN address on `GET /camera/local_address`, because a page cannot read its own. |
| `src/components/settings/cameras/CameraConfigDialog.vue` | One more entry in the stream-type dropdown. |
| `src/locales/en.yaml` | `camera_type_options.creality_webrtc`, and `general.title.cfs`. |

Component naming is upstream's, not new: `CameraItem` resolves a service to a
component by `startCase(service).replace(/ /g, '') + 'Camera'`, so
`creality-webrtc` finds `CrealityWebrtcCamera` with no registration anywhere.

### CFS

| File | What changed |
| --- | --- |
| `src/store/cfs/types.ts` | **New.** The shapes cfsbridge answers with; nothing invented on this side. |
| `src/store/cfs/state.ts`, `mutations.ts`, `getters.ts`, `index.ts` | **New.** A plain namespaced Vuex module. |
| `src/store/cfs/actions.ts` | **New.** Reference-counted polling of `GET /cfs/card` every 3 s, `GET /cfs/writes`, `GET /updates`, and one `write` action that posts to the bridge and keeps whatever it answered. |
| `src/store/index.ts`, `src/store/types.ts` | Register the module. |
| `src/components/widgets/cfs/CfsCard.vue` | **New.** The dashboard card. A `collapsable-card` with `layout-path="dashboard.cfs-card"` and `draggable`, which is all it takes to appear in layout mode, drag between columns, collapse and hide. |
| `src/components/widgets/cfs/CfsUnits.vue` | **New.** Four units with model, serial, temperature and humidity; sixteen slots with colour swatch, type, vendor/name, remaining, loaded and mapped chips, and Load / Unload / Tag. Shared by the card and the page. |
| `src/components/widgets/cfs/CfsSlotDialog.vue` | **New.** The slot edit dialog: vendor, type and name cascading out of the printer's own filament list, a colour picker with a hex field, min and max temperature, Write to slot and Clear slot. |
| `src/components/widgets/cfs/CfsOptions.vue` | **New.** The printer-wide auto refill, auto feed and self test toggles, plus the read-only auto update filament. |
| `src/components/widgets/cfs/CfsWriteLog.vue` | **New.** Every write the bridge has made, with Undo where the bridge says it is undoable and the bridge's reason where it is not. |
| `src/components/widgets/cfs/CfsForkStatus.vue` | **New.** The fork update check, from `GET /updates`. |
| `src/views/Cfs.vue` | **New.** The CFS page: units, write log, fork status, printer-wide options, bridge facts. |
| `src/views/Camera.vue` | **New.** The camera page: the same `CameraItem` at full width. |
| `src/views/Dashboard.vue` | Register `CfsCard`. |
| `src/store/layout/state.ts` | `cfs-card` in the default `container1`. Upstream's `setInitLayout` already merges new default cards into a stored layout, so an existing dashboard gets it without being reset. |
| `src/router/index.ts` | `/cfs` and `/camera`. Fluidd's existing per-camera fullscreen route keeps its path and is renamed `Fullscreen Camera`, because `/camera` took the name. |
| `src/components/layout/AppNavDrawer.vue` | Two nav items, `$filament` for CFS and `$camera` for Camera; the camera one hides when there is no camera. |
| `src/components/settings/CfsSettings.vue` | **New.** "CFS bridge address", plus whether the bridge is answering. |
| `src/views/Settings.vue`, `src/store/config/types.ts`, `src/store/config/state.ts` | Register that settings section and its `uiSettings.general.cfsBridgeUrl` value. |
| `src/util/cfs-bridge.ts` | **New.** One place that knows where the bridge is: relative when it serves the page, the configured address otherwise. |

No file was deleted, and the eight removed lines are the eight that were
replaced in the six upstream files touched.

---

## 4. Decisions worth knowing about

**Polling, not the Moonraker `box` object.** The printer does publish `box` over
Moonraker, and it does carry live per-slot material and remaining length, so
half this card could come off the websocket Fluidd already holds open. It was
not used. The filament database, the printer-wide box config, the write log,
the refusal rules and every write itself exist only on the bridge, which reads
the CFS over Creality's port 9999 protocol rather than through Klipper. Drawing
half a table from one clock and half from another is a bug waiting to be filed.
So one source, `GET /cfs/card`, every 3 s, and the bridge answers that out of a
cache its own 5 s poller fills, so the printer sees no extra traffic at all.
Polling is reference counted: a dashboard with the card hidden makes no
requests.

**The bridge is the only thing that decides.** Nothing in the fork enforces a
rule about what may be written while a job is on the bed. A disabled button is
a convenience; the guard is `cfsbridge/cfs.py`, server side, and its refusal
text is shown to the user word for word.

**Relative URLs.** The page is normally served by the bridge, so `/cfs/card`
and friends are relative and stay correct whatever port the bridge was started
on. Settings has an address field for the other case, and the bridge sends
`Access-Control-Allow-Origin: *` on its JSON routes so that case works.

**One webcam record.** The bridge maintains a single Moonraker webcam record,
`K2 Plus camera`, on the `creality-webrtc` service, which only the fork can
play. An earlier layout added a second record, `K2 Plus camera (bridge page)`,
on `iframe` pointing at the bridge's own `/camera`; that record is gone, and
the bridge removes it from Moonraker's webcam store when it finds one left
over from an older bridge.

**A uid synthesised from the name.** Fluidd keys the active camera, the
fullscreen route and `getWebcamById` off `uid`. Moonraker 1.0.5 sends none.
Names are unique in Moonraker's webcam store, so `name:<name>` is stable across
reloads, which is what the stored active-camera setting needs.

---

## 5. How to rebuild

In a clone of the fork, on the `cfs` branch. At the current v1.37.5 pin that is pnpm:

```bash
pnpm install --frozen-lockfile   # only after a dependency change
pnpm run build                   # about 86 s cold, output in dist/
```

At the older v1.30.0 tag the same two steps are `npm ci` and `npm run build`.

Then copy `dist/` over `cfsbridge/fluidd/` in the bridge checkout and restart the bridge service.
Build on a native filesystem, not a network or mapped drive: a half-installed build answers 200 with
the wrong bytes.

The build location is not fixed in stone: `cfsbridge serve --fluidd-dist DIR`,
or `CFSBRIDGE_FLUIDD_DIST`, serve a different directory. The default is
`cfsbridge/fluidd` inside the installed package, which is where the installer puts the bundle it
ships.

Nothing printer-specific is baked into the build. Fluidd reads `./config.json`
on startup and takes its Moonraker endpoint from it; the bridge generates that
file per request from its own `--host`, keeping the build's theme presets, and
blacklists `127.0.0.1` and `localhost` so Fluidd does not also race a websocket
against the bridge's own origin, which speaks none.

---

## 6. How to rebase onto a newer Fluidd

First, find out what it would cost. Fetch upstream, then do the rebase in a throwaway worktree beside
the clone and list what conflicts, so your own checkout is never touched:

```bash
git fetch upstream --tags
git worktree add ../dry-rebase cfs
git -C ../dry-rebase rebase <new tag> || git -C ../dry-rebase diff --name-only --diff-filter=U
git -C ../dry-rebase rebase --abort; git worktree remove --force ../dry-rebase
```

Measured when the pin moved from v1.30.0 to **v1.37.5** (805 commits ahead of the old pin on
`develop`), nine files conflicted:

```
src/components/layout/AppNavDrawer.vue
src/router/index.ts
src/store/config/state.ts
src/store/index.ts
src/store/types.ts
src/store/webcams/mutations.ts
src/store/webcams/types.ts
src/views/Dashboard.vue
src/views/Settings.vue
```

All nine are the registration points, and none of the new files conflicts,
because upstream has no `src/store/cfs` and no `src/components/widgets/cfs`. In
other words the fork is cheap to carry, and it was designed that way:
everything substantial lives in a file upstream does not have.

**But do not read "registration point" as "two-line conflict".** When that rebase was actually
performed, only six of the nine were that; three had changed shape underneath, and three files that
did **not** conflict still had to be edited.

Then:

```bash
git fetch origin --tags
git rebase <new tag> cfs
# fix the registration-point conflicts, keeping both sides
pnpm install --frozen-lockfile
pnpm run build
pnpm exec eslint --ext .ts,.js,.vue ./src
```

and then **update the pin**: `forks.json` at the project root carries
`pinned_tag` and `pinned_commit`, and the fork status section on the CFS page
reads it through the bridge. A rebase that does not update it makes the page
lie.

Two things to check by hand after a rebase, because neither has a test:

1. `getEnabledWebcams` may have been rewritten upstream. The fork's change is
   one operator (`webcam.enabled` to `webcam.enabled !== false`); make sure it
   survived.
2. `CameraItem`'s service-to-component mapping. If upstream ever stops deriving
   the component name from the service string, `CrealityWebrtcCamera` needs
   registering explicitly.

Both were checked against v1.37.5 and both still hold.
Add a third to the list, learned the hard way in that trial: **run the built
page against the printer with the websocket instrumented and compare the call
list to the old build's.** The one thing that stopped v1.37.5 being adopted was
invisible in the source and invisible in the build, and showed up only as three
`-32601` replies the old build never asked for.

---

## 7. The fork update check

`GET /updates` on the bridge answers both forks at once: pinned version, latest
upstream release and its date, how many commits behind the default branch, and
the release notes URL. Cached six hours; `?refresh=1` forces. It asks GitHub
anonymously (three GETs per fork, twice a day, far inside the 60-per-hour
limit) and it **never runs git against a clone**, because a web request has no
business touching a tree somebody may be building in.

The registry is `forks.json` at the project root, and `cfsbridge/upstream.py`
reads it. The CFS page draws it in a "Fork status" section with an "Update
available" badge and a "How to update" expander; the CFS card's header carries
a small `fork update` chip when either fork is behind, which links to the page.

The clone-side dry rebase is described in section 6.

The pins as `forks.json` carries them:

| Fork | Tracks | Pinned |
| --- | --- | --- |
| Fluidd | release | v1.37.5, `281dadab3f3bc079bde05df6af6c452411ff3749`, branch `cfs` |
| OrcaSlicer | upstream `main` | `3e1daccd7c567a0a3d2b5721841d30845f21307e`, branch `cfs` |

How far behind each one is, and what the newest upstream release is, is a live question, so this
document does not carry an answer that would rot: ask `GET /updates`, or the "Fork status" section
of the CFS page, which is the same data. The last dry rebase measured for Fluidd is in section 6.

---

## 8. What is verified, and what is not

Verified live in Chromium against the printer while it was printing, 2026-09-13:

* the dashboard loads, every upstream card works, the console is clean;
* layout mode lists **Cameras** and **CFS** with their toggles and drag
  handles; moving `cfs-card` to `container2` and collapsing `camera-card`
  survives a reload, because Fluidd stores the layout in Moonraker's database;
* the camera plays at **1920 x 1080** with `currentTime` advancing, on the
  dashboard card and on the Camera page;
* the CFS card shows all four units with temperature and humidity, and 2B as
  `loaded` and `mapped` with 58 m remaining;
* both nav pages open, and the write log and fork status render;
* opening slot 2B and pressing **Write to slot** is refused with the bridge's
  own words, and no write is recorded.

Not verified, deliberately: every CFS write that the bridge would actually
send. The printer had a 13 hour job on the bed throughout, and the only writes
the bridge allows in that state are drying (impossible on this hardware, every
unit is an `MF003` with no dryer) and editing a slot that is neither loaded nor
in the running job's map, which would have been a real write to a real spool
label. So Load, Unload, Tag, the option toggles and a successful slot edit are
all still untried from the fork. The refusal path is what was exercised.
