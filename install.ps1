# install.ps1 — one-line installer for Kuro on Windows.
#
# Clones Kuro into %LOCALAPPDATA%\kuro, builds a virtualenv there and
# installs the dependencies into it, then drops a `kuro` launcher into
# %USERPROFILE%\bin (or a configured bin dir). After this the operator never
# creates, activates or pip-installs anything by hand: `kuro` finds its own
# venv and runs from any directory.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# The venv is built here, on this machine, rather than shipped — a venv is not
# portable (its interpreter symlink, the console-script shebangs and pyvenv.cfg
# all bake in absolute paths and one exact base interpreter), so the only way
# to hand someone a working one is to build it where it will run. That is what
# this does, and it is the same thing pipx and uv do.
#
# Idempotent: re-run it any time to update an existing install.
#
# Environment overrides (all optional):
#   KURO_REPO          git URL to install from
#                          (default https://github.com/AngelSalazar-dev/kuro-CyberAgent.git)
#   KURO_INSTALL_DIR   where the checkout and venv live
#                          (default %LOCALAPPDATA%\kuro)
#   KURO_BIN_DIR       where the `kuro` launcher is written
#                          (default %USERPROFILE%\bin)
#   KURO_SKIP_DEPS=1   lay the files down but do not build the venv
#   KURO_SKIP_SETUP=1  do not run setup.ps1 at the end

$ErrorActionPreference = 'Stop'

$REPO        = $env:KURO_REPO        -or 'https://github.com/AngelSalazar-dev/kuro-CyberAgent.git'
$INSTALL_DIR = $env:KURO_INSTALL_DIR -or (Join-Path $env:LOCALAPPDATA 'kuro')
$BIN_DIR     = $env:KURO_BIN_DIR     -or (Join-Path $env:USERPROFILE 'bin')
$KURO_HOME   = $env:KURO_HOME        -or (Join-Path $env:USERPROFILE '.kuro')
$PROFILE     = Join-Path $KURO_HOME 'logfare\source'

function bold   { param($msg) Write-Host $msg -ForegroundColor White }
function step   { param($msg) Write-Host "`n-> $msg" -ForegroundColor Cyan }
function ok     { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function warn   { param($msg) Write-Warning "  [WARN] $msg" }
function die    { param($msg) Write-Error "  [ERR] $msg" -ErrorAction Stop }
function note   { param($msg) Write-Host "      $msg" -ForegroundColor Cyan }

# --- 0. preconditions --------------------------------------------------------

step "Checking prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  die "git is required."
}
$PY = (Get-Command python -ErrorAction SilentlyContinue) -or (Get-Command python3 -ErrorAction SilentlyContinue)
if (-not $PY) {
  die "python not found — install Python 3.11 or newer."
}
$py_version = & $PY.Source -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'
$py_major_minor = & $PY.Source -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
if ($LASTEXITCODE -ne 0) {
  die "Python 3.11+ is required; $($PY.Source) is $py_version"
}
ok "git and $py_version"

# --- 1. get the source -------------------------------------------------------

step "Fetching Kuro"
$git_dir = Join-Path $INSTALL_DIR '.git'
if (Test-Path -Path $git_dir -PathType Container) {
  # An existing install. Fast-forward only — never merge, never touch local
  # edits; a failed pull is a warning, not a stop, because the launcher's own
  # update check will report the same thing at run time.
  try {
    git -C $INSTALL_DIR pull --ff-only --quiet 2>$null
    ok "updated $INSTALL_DIR"
  } catch {
    warn "could not update $INSTALL_DIR (local changes or offline) — using what is there"
  }
} elseif (Test-Path -Path $INSTALL_DIR -PathType Container) {
  die "$INSTALL_DIR exists but is not a git checkout — move it aside and re-run."
} else {
  $parent = Split-Path -Parent $INSTALL_DIR
  if (-not (Test-Path -Path $parent -PathType Container)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
  # A full clone, not --depth 1: the launcher's update check counts how far
  # behind the checkout is and fast-forwards it, and both want real history.
  git clone --quiet $REPO $INSTALL_DIR 2>$null
  ok "cloned into $INSTALL_DIR"
}

# --- 2. build the venv -------------------------------------------------------

$VENV_PY = Join-Path $INSTALL_DIR '.venv\Scripts\python.exe'

if ($env:KURO_SKIP_DEPS -eq '1') {
  warn "KURO_SKIP_DEPS=1 — skipping the venv (the launcher will not run yet)"
} else {
  step "Building the virtualenv"
  if (Test-Path -Path $VENV_PY -PathType Leaf) {
    ok "venv already present"
  } else {
    & $PY.Source -m venv (Join-Path $INSTALL_DIR '.venv') 2>$null
    ok "venv at $INSTALL_DIR\.venv"
  }

  step "Installing dependencies (a minute or two the first time)"
  function _install_deps {
    & $VENV_PY -m pip install --quiet --disable-pip-version-check --upgrade pip 2>$null
    Push-Location -Path $INSTALL_DIR -ErrorAction Stop
    try {
      & $VENV_PY -m pip install --quiet --disable-pip-version-check -r (Join-Path $INSTALL_DIR 'requirements.txt') 2>$null
    } finally {
      Pop-Location
    }
  }
  try {
    _install_deps
    ok "dependencies installed"
  } catch {
    die "could not install the dependencies — see the pip output above."
  }
  # Stamp the requirements hash so the launcher's incremental refresh sees a
  # matching venv and does nothing on the first launch.
  $hash = (Get-FileHash -Path (Join-Path $INSTALL_DIR 'requirements.txt') -Algorithm SHA256).Hash
  Set-Content -Path (Join-Path $INSTALL_DIR '.venv\.requirements.sha') -Value $hash -Encoding UTF8
}

# --- 3. the launcher on PATH -------------------------------------------------

step "Installing the launcher"
if (-not (Test-Path -Path $BIN_DIR -PathType Container)) {
  New-Item -ItemType Directory -Path $BIN_DIR -Force | Out-Null
}
# A tiny shim, not the launcher itself: it pins KURO_ROOT to the checkout
# so the real launcher (which lives in packaging/) finds the venv and the
# profile no matter where `kuro` is invoked from. Written fresh each run so
# a moved install re-points itself.
$shim = @"
#!/usr/bin/env pwsh
# kuro — installed launcher (written by install.ps1).
# Pins the checkout and hands every argument to the real launcher.
$env:KURO_ROOT = "$INSTALL_DIR"
& "$INSTALL_DIR\packaging\kuro.ps1" @args
"@
Set-Content -Path (Join-Path $BIN_DIR 'kuro.ps1') -Value $shim -Encoding UTF8

# Also create a plain `kuro` shim (without .ps1) for convenience.
$shim2 = @"
#!/usr/bin/env pwsh
$env:KURO_ROOT = "$INSTALL_DIR"
& "$INSTALL_DIR\packaging\kuro.ps1" @args
"@
Set-Content -Path (Join-Path $BIN_DIR 'kuro') -Value $shim2 -Encoding UTF8

ok "$BIN_DIR\kuro"

$path_env = $env:PATH
if ($path_env -split ';' -contains $BIN_DIR) {
  ok "$BIN_DIR is on PATH"
} else {
  warn "$BIN_DIR is not on your PATH — add it, then open a new shell:"
  note "[Environment]::SetEnvironmentVariable('PATH', '$BIN_DIR;' + [Environment]::GetEnvironmentVariable('PATH', 'User'), 'User')"
}

# --- 4. the credential -------------------------------------------------------

if ($env:KURO_SKIP_SETUP -eq '1') {
  warn "KURO_SKIP_SETUP=1 — not configuring the route"
} elseif (Test-Path -Path $PROFILE -PathType Leaf) {
  step "Route already configured"
  ok "$PROFILE exists"
} else {
  # setup.ps1 does not prompt: it seeds the profile from the key shipped in the
  # checkout, so a fresh install finishes unattended. stdin is still pointed at
  # $null rather than left as this script's own, so a future read in setup.ps1
  # cannot eat the rest of a pipeline.
  #
  # A setup.ps1 that fails must not fail the install: a proxy that will not
  # start, an upstream that is down, or a machine with no network all leave a
  # working checkout, and the launcher retries on launch.
  step "Configuring the Logfare route"
  try {
    & "$INSTALL_DIR\setup.ps1" 2>$null
    ok "route configured"
  } catch {
    warn "setup did not finish — run $INSTALL_DIR\setup.ps1 to retry"
  }
}

# --- done --------------------------------------------------------------------

Write-Host ""
bold "+----------------------------------------------+"
bold "|  installed.                                  |"
bold "+----------------------------------------------+"
Write-Host ""
Write-Host "    kuro                 # launch, from anywhere"
Write-Host "    kuro -C C:\proj      # run against another directory"
Write-Host "    kuro --mode zim      # full auto, follows AGENTS.md"
Write-Host ""
Write-Host "  Re-run this installer to update:  $INSTALL_DIR\install.ps1"
Write-Host "  It keeps its own venv, so nothing to activate and nothing to pip-install."
Write-Host ""
