<#
Installs everything OpenShot's "Blur immodest content with OCCLUDE" feature
needs on Windows: Python, the occlude package, and ffmpeg.

Everything is self-contained under %LOCALAPPDATA%\OpenShot-OCCLUDE (a private
Python environment plus a bin folder) - nothing touches your system Python or
PATH, and OpenShot looks in that folder automatically. Safe to re-run at any
time; steps that are already done are skipped or refreshed in place.

Heads-up: the occlude package pulls in PyTorch and vision-language model
libraries - expect a download of several gigabytes on first run.
#>

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # avoids slow Invoke-WebRequest progress rendering

$Root = Join-Path $env:LOCALAPPDATA "OpenShot-OCCLUDE"
$Venv = Join-Path $Root "venv"
$Bin = Join-Path $Root "bin"

# Where to install occlude from, tried in order. The branch zip carries fixes
# (GUI progress stream, graceful no-sam2 fallback) that the last PyPI release
# (0.0.3) predates; once merged/released the later entries take over.
$OccludeSpecs = @(
    "https://github.com/CyberNerdIT/Occlude/archive/refs/heads/claude/openshot-video-blur-k22569.zip",
    "https://github.com/CyberNerdIT/Occlude/archive/refs/heads/main.zip",
    "occlude"
)

$PythonFallbackUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
$FfmpegFallbackUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

function Write-Step([string] $Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-PythonExe {
    # Prefer the py launcher; validate every candidate by actually running it,
    # which weeds out the Microsoft Store python.exe stub.
    $candidates = @(
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $exe = $null
        try {
            $exe = & $c.Exe @($c.Args) "-c" "import sys; print(sys.executable)" 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -eq 0 -and $exe) { return "$exe".Trim() }
    }
    return $null
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
        [Environment]::GetEnvironmentVariable("Path", "User")
}

# --- Python -----------------------------------------------------------------
Write-Step "Looking for Python"
$python = Get-PythonExe
if ($python) {
    Write-Host "Found: $python"
} else {
    Write-Step "Python not found - installing it"
    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -eq 0) { $installed = $true }
    }
    if (-not $installed) {
        Write-Host "winget unavailable or failed - downloading the python.org installer"
        $dl = Join-Path $env:TEMP "openshot-occlude-python-setup.exe"
        Invoke-WebRequest -Uri $PythonFallbackUrl -OutFile $dl
        Start-Process -FilePath $dl -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1" -Wait
    }
    Update-SessionPath
    $python = Get-PythonExe
    if (-not $python) {
        throw "Python installation did not complete. Install Python from python.org (tick 'Add python.exe to PATH') and run this script again."
    }
    Write-Host "Installed: $python"
}

# --- occlude (in a private environment) -------------------------------------
Write-Step "Installing OCCLUDE into $Venv (large download on first run)"
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$venvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $python -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw "Creating the private Python environment failed."
    }
}
& $venvPython -m pip install --upgrade pip
$occludeInstalled = $false
foreach ($spec in $OccludeSpecs) {
    Write-Host "Trying: pip install $spec"
    # --no-cache-dir: the branch zip keeps the same version number as code
    # changes land, so a cached wheel would silently pin stale code
    & $venvPython -m pip install --upgrade --no-cache-dir $spec
    if ($LASTEXITCODE -eq 0) { $occludeInstalled = $true; break }
    Write-Warning "Install from $spec failed - trying the next source"
}
if (-not $occludeInstalled) {
    throw "Installing the occlude package failed. Check your internet connection and re-run this script."
}

# --- ffmpeg -----------------------------------------------------------------
Write-Step "Checking ffmpeg"
$ffmpegPresent = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
    (Test-Path (Join-Path $Bin "ffmpeg.exe"))
if ($ffmpegPresent) {
    Write-Host "ffmpeg already available"
} else {
    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -eq 0) { $installed = $true }
    }
    if (-not $installed) {
        Write-Host "winget unavailable or failed - downloading ffmpeg directly"
        $zip = Join-Path $env:TEMP "openshot-occlude-ffmpeg.zip"
        $extract = Join-Path $env:TEMP "openshot-occlude-ffmpeg"
        Invoke-WebRequest -Uri $FfmpegFallbackUrl -OutFile $zip
        if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
        Expand-Archive -Path $zip -DestinationPath $extract
        $exe = Get-ChildItem -Path $extract -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
        if (-not $exe) { throw "ffmpeg.exe not found inside the downloaded archive." }
        New-Item -ItemType Directory -Force -Path $Bin | Out-Null
        Copy-Item $exe.FullName (Join-Path $Bin "ffmpeg.exe") -Force
        Remove-Item -Force $zip
        Remove-Item -Recurse -Force $extract
        Write-Host "ffmpeg installed to $Bin"
    }
}

# --- verify -----------------------------------------------------------------
Write-Step "Verifying"
$occludeExe = Join-Path $Venv "Scripts\occlude.exe"
& $occludeExe --help *> $null
if ($LASTEXITCODE -ne 0) { throw "occlude did not run correctly." }

Write-Host ""
Write-Host "All set!" -ForegroundColor Green
Write-Host "Restart OpenShot and the 'Blur immodest content with OCCLUDE' checkbox"
Write-Host "in the Export dialog will be enabled. The first blur also downloads the"
Write-Host "AI models (several GB), so start with a short clip."
if ($Host.Name -eq "ConsoleHost") { Read-Host "Press Enter to close" }
