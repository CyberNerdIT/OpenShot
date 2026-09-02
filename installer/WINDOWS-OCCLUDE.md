# Windows builds of this fork (OpenShot + OCCLUDE)

This fork adds a **"Blur immodest content with OCCLUDE"** checkbox to
OpenShot's Export dialog. Everything it changes lives in plain Python files
under `src/`, and the official OpenShot Windows build ships those files as-is
next to the frozen executable — so a Windows build of this fork is the
official OpenShot 2.6.1 Windows build with this repository's `src/` tree
overlaid on top.

## Downloading a build

Every push to `master` (and to `claude/**` branches) runs the
[Windows Package workflow](../.github/workflows/windows-package.yml). Open the
repository's **Actions** tab, pick the latest successful **Windows Package**
run, and download either artifact:

- **`OpenShot-OCCLUDE-v<version>-win64-installer`** — an unsigned Inno Setup
  installer. It uses its own AppId and install directory, so it installs
  alongside (not over) an official OpenShot installation. Windows SmartScreen
  will warn because the installer is unsigned; choose "More info" → "Run
  anyway".
- **`OpenShot-OCCLUDE-v<version>-win64-portable`** — the application folder
  itself. Unzip anywhere and run `openshot-qt.exe`.

## Enabling the blur feature on Windows

The app runs OCCLUDE as an external command. On the same machine:

1. Install [Python 3.10+](https://www.python.org/downloads/windows/) (check
   "Add python.exe to PATH").
2. `pip install occlude` — this puts `occlude.exe` on your PATH, which the
   Export dialog detects.
3. Install [ffmpeg](https://ffmpeg.org/download.html) and make sure
   `ffmpeg.exe` is on your PATH (OCCLUDE uses it to keep the original audio).
4. Optional: for silhouette-shaped blur,
   `pip install "git+https://github.com/facebookresearch/sam2.git"`.

If `occlude` is installed somewhere unusual, set the `OCCLUDE_COMMAND`
environment variable to the full command line, e.g.
`"C:\Python312\python.exe" -m occlude`.

The checkbox in the Export dialog is greyed out (with an install hint) until
OCCLUDE is found. Note that OCCLUDE is compute-heavy: without an NVIDIA GPU,
blurring a long video can take many times its duration.

## How the packaging works

`windows-package.yml` on a `windows-latest` runner:

1. Downloads the official `OpenShot-v2.6.1-x86_64.exe` release installer and
   verifies its pinned SHA-256.
2. Extracts it with `innoextract` (no installation needed).
3. Copies this repository's `src/` tree over the extracted application
   directory. The frozen executable imports `classes/`, `windows/`, etc. from
   these plain files, so the overlay fully takes effect.
4. Validates the sources with the same Python 3.8 the frozen app embeds, runs
   the wrapper unit tests, and smoke-tests `openshot-qt-cli.exe --version`.
5. Uploads the folder as the portable artifact and compiles
   `installer/windows-installer-ci.iss` (unsigned, CI-only script) into the
   installer artifact.

**Limitation:** `src/launch.py` is frozen inside `openshot-qt.exe` when
OpenShot Studios builds the base package, so changes to `launch.py` itself do
not take effect through the overlay. Every other file under `src/` is loaded
from disk at runtime. A change to `launch.py`, to the bundled Python/Qt, or to
libopenshot would need a real from-source Windows build (OpenShot's MSYS2
build machine), which this workflow deliberately avoids.

## Building locally

The same steps work on any OS for assembling the folder (the smoke test and
installer need Windows):

```bash
curl -LO https://github.com/OpenShot/openshot-qt/releases/download/v2.6.1/OpenShot-v2.6.1-x86_64.exe
innoextract -s -d base OpenShot-v2.6.1-x86_64.exe
cp -r src/* base/app/
# base/app is now the portable Windows build
```
