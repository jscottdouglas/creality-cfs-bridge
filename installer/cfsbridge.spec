# installer/cfsbridge.spec  (PyInstaller, one folder)
#
# Build with `python -m PyInstaller --clean --noconfirm installer/cfsbridge.spec`
# from the project root; installer/build_package.ps1 does that and then drops
# WinSW beside the result.
#
# Paths are resolved against SPECPATH, the spec file's own directory, rather
# than against the working directory, so the build does not depend on where it
# was started from.
#
# `datas` carries the built Fluidd bundle as a top-level `fluidd/` folder in
# the one-folder output, which is the path CrealityCFSBridge.xml passes to
# `serve --fluidd-dist`. It also carries write_orca_preset.py as
# `installer/write_orca_preset.py`, which is where `cfsbridge preset` looks
# for it under sys._MEIPASS; it is data rather than a hidden import because
# `installer/` is not a package and the CLI loads the file by path.
# `excludes` keeps tkinter out of the frozen tree: the bridge never imports
# it, and a stray hook pulling it in would grow the installer for nothing.
#
# There is no `block_cipher` and no `cipher=`: PyInstaller 6 removed bytecode
# encryption, so both, and the `a.zipped_data` that used to be handed to PYZ
# alongside it, are dead arguments that only make the spec look like it does
# something it does not.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis([os.path.join(ROOT, "cfsbridge", "__main__.py")], pathex=[ROOT],
             binaries=[],
             datas=[(os.path.join(ROOT, "cfsbridge", "fluidd"), "fluidd"),
                    (os.path.join(ROOT, "installer", "write_orca_preset.py"),
                     "installer")],
             hiddenimports=["websockets.legacy", "websockets.legacy.client"],
             excludes=["tkinter"])
pyz = PYZ(a.pure)
# `contents_directory="."` keeps the one-folder layout flat. PyInstaller 6
# moved everything except the launcher into an `_internal` subfolder, which
# would put the Fluidd bundle at `_internal\fluidd` and make the service
# definition depend on a PyInstaller implementation detail. Flat, the package
# is exactly what the interface promises: cfsbridge.exe and fluidd\ side by
# side, so CrealityCFSBridge.xml can say `%BASE%\fluidd`. The option belongs on
# EXE; COLLECT reads it back off the EXE it is given, and passing it to COLLECT
# instead is silently ignored.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="cfsbridge",
          console=True, icon=None, contents_directory=".")
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="CrealityCFSBridge")
