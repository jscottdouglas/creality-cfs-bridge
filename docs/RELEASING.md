# Releasing the bridge

This is the maintainer's page. It covers what the release workflow does, the
three pins it reads, the rule about the slicer checksum, what the smoke test
proves, and how to run the same two build scripts on your own Windows machine.

Everything here happens in this repository. There is nothing to configure on
the runner: the workflow installs its own Python, Node, pnpm and Inno Setup.

---

## 1. What a release is

One file: `CrealityCFSBridge_Setup_<version>.exe`, attached to a GitHub
release. It carries

* `cfsbridge.exe`, the bridge, built by PyInstaller as a one folder bundle;
* `fluidd/`, the built Fluidd fork, which the bridge serves at
  `http://127.0.0.1:7126/fluidd/`;
* `CrealityCFSBridge.exe` and `CrealityCFSBridge.xml`, the WinSW service host
  and its service definition;
* `installer/write_orca_preset.py`, which the installer runs to write the
  slicer preset.

It registers the service `CrealityCFSBridge`, automatic start, running as
`NT AUTHORITY\LocalService`, bound to `127.0.0.1:7126`. It ships with **no
printer address**: the setup page at `http://127.0.0.1:7126/setup` writes one
into `%ProgramData%\CrealityCFSBridge\config.json` after the install. A
published package that already knew an address would be a package that had
somebody's network in it.

---

## 2. The three pins

All three live in `installer/` and are plain text, so a release is reproducible
from the tag alone.

| File | What it pins | Who reads it |
| --- | --- | --- |
| `installer/version.txt` | the release version, three numbers such as `0.3.0` | `installer/build_installer.ps1` |
| `installer/fluidd_release.json` | the Fluidd fork repository and the commit to build | `.github/workflows/bridge_release.yml` |
| `installer/orca_release.json` | the slicer release the installer can chain: tag, asset name, URL, SHA-256 | `installer/build_installer.ps1` |

### Updating `installer/version.txt`

Write the new version, nothing else. It has to match `^\d+\.\d+\.\d+$`:
`build_installer.ps1` checks the shape before it starts, because the same
string becomes the output filename, the Inno `AppVersion` and the exe's
`VersionInfoVersion`, and the last of those only accepts a numeric version.
The tag you push should be the same version with a `v` in front.

### Updating `installer/fluidd_release.json`

The full key set:

```json
{
  "repo": "<owner>/fluidd",
  "branch": "cfs",
  "commit": "<40 hex characters, the tip of that branch>",
  "upstream_base": "v1.37.5",
  "upstream_base_commit": "<40 hex characters, the upstream tag the fork sits on>"
}
```

The workflow reads two of these, `repo` and `commit`, and checks out that
commit. `branch`, `upstream_base` and `upstream_base_commit` are there so that
a reader can tell at a glance which branch the commit is supposed to be the tip
of and how far the fork has moved from upstream; nothing in the build depends on
them.

**`commit` must be the tip of the branch named in `branch`, never
`upstream_base_commit`.** Both are reachable from the fork, both build, both
package and both install. The upstream one simply has no CFS card and no
Creality camera service in it, so nothing later in the pipeline would fail and
the mistake would ship. The workflow therefore checks the built bundle for two
independent strings that only the fork puts there, one from the camera service
and one from the CFS card, and stops if **both** are missing. If that check
fires, `commit` is almost certainly `upstream_base_commit` by accident. If only
one of the two is missing the step warns instead: that is a rename inside the
fork, and the step needs updating rather than the pin.

To read the tip off a local clone of the fork:

```bash
git -C <clone> rev-parse cfs
```

See `docs/FLUIDD_FORK.md` for what the fork changes and which toolchain the pin
needs. The workflow takes the Node version from the fork's own `.node-version`
and the pnpm version from the fork's own `packageManager` field, so bumping the
pin to a commit with a different toolchain needs no workflow change.

### Updating `installer/orca_release.json`

```json
{
  "tag": "v2.5.0-cfs.1",
  "asset": "<the installer asset's filename>",
  "sha256": "<64 hex characters>",
  "url": "https://github.com/<owner>/OrcaSlicer/releases/download/<tag>/<asset>"
}
```

Fill in `sha256` from the asset you actually published, for example with

```powershell
Get-FileHash -Algorithm SHA256 .\<asset> | Format-List
```

Order of operations: publish the slicer release first, take its checksum, put
it here, then tag the bridge. The other way round cannot work, and section 3
explains why the build refuses to pretend otherwise.

---

## 3. The placeholder checksum rule

The setup exe offers to download and run the slicer installer, about 200 MB,
from the URL in `installer/orca_release.json`. Inno Setup verifies the
SHA-256 before it runs anything, so that checksum is the only thing standing
between a user and whatever happens to be at that URL on the day.

`installer/build_installer.ps1` therefore **refuses to compile** when `sha256`
is not 64 hex characters, unless it is passed `-AllowPlaceholderSha`. A setup
exe built with that switch cannot install the slicer at all, because Inno will
reject the download, so it exists only to prove the script still compiles.

In CI:

* a tag build never passes the switch;
* a push to `main` never passes the switch;
* a manual run (Actions, "Run workflow") passes it only if you tick
  `allow_placeholder_sha`.

While `installer/orca_release.json` still holds a placeholder value, pushes to
`main` and tag builds fail at the installer step. **That is the intended
behaviour, not a broken workflow.** The fix is to publish the slicer release
and fill in the real checksum. A manual run with the box ticked exercises
everything else in the meantime.

### A manual run can never publish

"Run workflow" lets you pick any ref, including a `v*` tag, so it is worth
being exact about what happens if you start a manual run against a tag with
`allow_placeholder_sha` ticked. The answer is that it builds, it smoke tests,
it uploads the setup exe as a **workflow artifact**, and the release step does
not run at all. Its condition requires three things:

* the event is a `push`, which a manual run is not, whatever ref it was started
  against;
* the ref is a `v*` tag;
* the placeholder switch is not active.

Any one of those failing is enough, and a manual run fails the first and, with
the box ticked, the third as well. So there is no combination of ref and input
that publishes a setup exe compiled against a placeholder checksum. Such an exe
cannot install the slicer at all, because Inno rejects the download, which is
why the door has two locks on it.

What the artifact is for: downloading and installing by hand, to check the
installer, the service and the setup page. Not for sending to anyone.

---

## 4. Cutting a release

1. Make sure `main` is what you want to ship, and that the three pins in
   section 2 are current.
2. Bump `installer/version.txt` and commit it.
3. Tag and push:

   ```bash
   git tag v0.3.0
   git push origin main --tags
   ```

4. Watch the `bridge release` run in the Actions tab. On success it attaches
   `CrealityCFSBridge_Setup_0.3.0.exe` to a new release for the tag, with
   generated release notes.

The workflow runs on every push to `main` too, which is how you find out that
the pipeline still works without cutting a release. The release step is the
only one gated on the tag; everything before it, including the smoke test, runs
either way.

Workflow permissions: the job asks for `contents: write`, which is what the
release step needs to create the release and upload the asset. No other
credential is involved.

---

## 5. What the smoke test proves

After compiling the setup exe, the runner installs it the way a user would and
then takes it apart again:

```
/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=
```

`/TASKS=` with an empty list unticks every task, which here means the chained
slicer install. The runner is not going to install a slicer.

Then, in order:

1. Wait up to 60 s for the service `CrealityCFSBridge` to reach `Running`. The
   installer registers and starts it, and a cold first start of a PyInstaller
   bundle under WinSW is not instant, so the check waits for the state instead
   of sleeping for a guessed number of seconds.
2. Wait up to 60 s for `GET http://127.0.0.1:7126/api/setup` to answer.
   `Running` is WinSW's opinion of its own child process; answering on the port
   is the bridge's, and only the second one means the service works.
3. Require `"configured": false` in that answer. This is the check that the
   published package ships with no printer address in it.
4. Run `%ProgramFiles%\CrealityCFSBridge\unins000.exe` silently, then wait up
   to 60 s and require that the service is gone. Inno's uninstaller hands the
   work to a copy of itself in a temporary folder, and the first process can
   return before that copy has finished, so the deregistration is waited for
   rather than assumed.

On any failure the step prints the tail of every `.log` file in
`%ProgramData%\CrealityCFSBridge\logs` (the bridge's own `serve.log` and the
WinSW wrapper, output and error logs all land there) and in the package folder,
before it throws. A red smoke test in the Actions log should already tell you
why.

The setup exe's `/HELP` is deliberately never used anywhere in CI: Inno shows
it in a modal message box, and on a runner with no desktop that hangs until the
job times out.

---

## 6. Running the build locally, on Windows

Both scripts are written for Windows PowerShell 5.1 and up, and both can be run
from the repository root. Run them in this order: the second one packages what
the first one leaves behind.

Prerequisites: Python 3.12 on `PATH`, Node and pnpm at the versions the fork
pins, and Inno Setup 6.3 or newer.

### The Fluidd bundle

The built bundle is not committed. Build it from the fork, at the commit in
`installer/fluidd_release.json`, and copy the result into place:

```powershell
git clone https://github.com/<owner>/fluidd.git fluidd-src
git -C fluidd-src checkout <commit>
Set-Location fluidd-src
pnpm install --frozen-lockfile
pnpm run build
Set-Location ..
New-Item -ItemType Directory -Force -Path cfsbridge\fluidd | Out-Null
Copy-Item -Recurse -Force fluidd-src\dist\* cfsbridge\fluidd\
```

### `installer\build_package.ps1`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_package.ps1
```

It installs PyInstaller and the bridge's runtime dependencies, refuses to start
if `cfsbridge\fluidd\index.html` is missing, runs `installer\cfsbridge.spec`,
downloads the pinned WinSW binary and **verifies its SHA-256** before using it,
copies `installer\winsw.xml` in as `CrealityCFSBridge.xml`, and then names any
of the four required pieces that is missing rather than leaving a half built
folder for the installer to find. Output: `dist\CrealityCFSBridge\`.

### `installer\build_installer.ps1`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

It reads `installer\version.txt` and `installer\orca_release.json`, checks the
checksum (section 3), checks that the package folder and the root `LICENSE` are
there, finds `ISCC.exe`, and compiles `installer\CrealityCFSBridge.iss`.
Output: `dist\CrealityCFSBridge_Setup_<version>.exe`.

To find `ISCC.exe` it looks at `INNO_DIR` first, then at
`C:\Program Files (x86)\Inno Setup 6`. Set `INNO_DIR` to the folder holding
`ISCC.exe` if you unpacked Inno Setup somewhere instead of installing it. CI
installs it with `choco install innosetup`, which uses the second path, so CI
does not set `INNO_DIR`.

Add `-AllowPlaceholderSha` only to check that the script compiles while the
slicer checksum is still a placeholder, and throw away what it produces.

### Trying the result

```powershell
Start-Process -Wait -FilePath .\dist\CrealityCFSBridge_Setup_0.3.0.exe `
  -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/TASKS='
Get-Service CrealityCFSBridge
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:7126/api/setup
```

Then open `http://127.0.0.1:7126/setup`, enter the printer address, for example
`192.168.1.50`, and press Test before Save. Uninstall with

```powershell
Start-Process -Wait -FilePath "$env:ProgramFiles\CrealityCFSBridge\unins000.exe" `
  -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES'
```

---

## 7. Checklist

- [ ] Slicer release published, and its real SHA-256 is in
      `installer/orca_release.json`.
- [ ] `installer/fluidd_release.json` points at the fork branch tip.
- [ ] `installer/version.txt` bumped, and it matches the tag you are about to
      push.
- [ ] `main` is green.
- [ ] Tag pushed, `bridge release` green, the setup exe is attached to the
      release.
- [ ] The attached exe installs on a clean Windows machine, the setup page
      accepts a printer address, and the slicer's Device tab shows Fluidd with
      the CFS card.
