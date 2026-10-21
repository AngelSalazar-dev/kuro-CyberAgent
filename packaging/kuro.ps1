# kuro.ps1 - launcher for Kuro on Windows.
#
# Starts the LiteLLM -> Logfare proxy if it is not already up, then runs the
# harness. Every argument is forwarded verbatim to `python -m kuro`, so
# `kuro.ps1 --help`, `kuro.ps1 -C C:\proj` etc. all work unchanged.
#
# Configure through environment variables only:
#
#   KURO_HOME              harness dotdir            (default %USERPROFILE%\.kuro)
#   LITELLM_ENV_FILE      profile to source         (default $KURO_HOME\logfare\source)
#   LITELLM_PORT          proxy port                (default 4001)
#   LITELLM_SERVICE       proxy manager             (default $KURO_HOME\logfare\start-litellm.ps1)
#   KURO_NO_PROXY=1        never touch the proxy
#   KURO_REQUIRE_PROXY=1   hard-fail if the proxy is not healthy
#
# No key is ever printed or logged.

$ErrorActionPreference = 'Stop'

# Find the checkout. This launcher lives in <root>\packaging\, so when
# KURO_ROOT is not already exported - run.ps1 and the installed shim both
# export it - the checkout is the parent of this file's directory.
$self_dir = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if (-not $env:KURO_ROOT) {
  if ((Split-Path -Leaf $PSScriptRoot) -eq 'packaging') {
    $env:KURO_ROOT = (Split-Path -Parent $PSScriptRoot)
  } else {
    $env:KURO_ROOT = $PSScriptRoot
  }
}

# Resolve the interpreter that carries Kuro's dependencies.
#
# Prefer the checkout's own venv - the one install.ps1 builds - so a global
# install runs with no activation at all. That is what lets `kuro` work
# from any directory without the operator touching a shell. Failing that, fall
# back to an activated venv ($VIRTUAL_ENV), then a python on PATH, which is
# the bring-your-own-venv layout. This script never creates a venv.
$root = $env:KURO_ROOT
$venv_py = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -Path $venv_py -PathType Leaf) {
  $PY = $venv_py
} elseif ($env:VIRTUAL_ENV -and (Test-Path -Path (Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe') -PathType Leaf)) {
  $PY = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $PY = (Get-Command python).Source
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
  $PY = (Get-Command python3).Source
} else {
  $PY = ''
}

$env:KURO_HOME = $env:KURO_HOME
if (-not $env:KURO_HOME) { $env:KURO_HOME = Join-Path $env:USERPROFILE '.kuro' }

# Which upstream to use.
$env_override   = $env:LITELLM_ENV_FILE
$port_override  = $env:LITELLM_PORT
$svc_override   = $env:LITELLM_SERVICE

$current_source = ''
$current_source_file = Join-Path $env:KURO_HOME 'current-source'
if (Test-Path -Path $current_source_file -PathType Leaf) {
  $current_source = (Get-Content -Path $current_source_file -Raw).Trim()
}

# A parked source is not a selection. Token Juice is under construction.
if ($current_source -eq 'tokenjuice') {
  $current_source = ''
}

# An endpoint already in the environment wins over everything below.
$inherited_base_url = $env:ANTHROPIC_BASE_URL
$skip_profile = 0
if ($inherited_base_url -and $inherited_base_url -notlike '*localhost:*' -and $inherited_base_url -notlike '*127.0.0.1:*') {
  $skip_profile = 1
  $env:KURO_NO_PROXY = 1
}

if ($skip_profile -ne 1) {
  switch ($current_source) {
    'tokenharbour' {
      if (Test-Path -Path (Join-Path $env:KURO_HOME 'tokenharbour\source') -PathType Leaf) {
        $env_override = Join-Path $env:KURO_HOME 'tokenharbour\source'
      }
      $env:KURO_NO_PROXY = 1
    }
    'openrouter' {
      if (Test-Path -Path (Join-Path $env:KURO_HOME 'openrouter\source') -PathType Leaf) {
        $env_override = Join-Path $env:KURO_HOME 'openrouter\source'
      }
      $env:KURO_NO_PROXY = 1
    }
  }
}

if ($current_source -eq 'iteracompute') {
  $ic_profile   = Join-Path $env:KURO_HOME 'iteracompute\source'
  $ic_service   = Join-Path $env:KURO_HOME 'iteracompute\start-litellm.ps1'
  if (Test-Path -Path $ic_profile -PathType Leaf -and (Test-Path -Path $ic_service -PathType Leaf)) {
    $env_override  = $ic_profile
    $port_override = 4002
    $svc_override  = $ic_service
    if (-not $env:LITELLM_LOG) { $env:LITELLM_LOG = "$env:TEMP\litellm-itera.log" }
  }
}

if ($current_source -eq 'infronai') {
  $inf_profile  = Join-Path $env:KURO_HOME 'infronai\source'
  $inf_service  = Join-Path $env:KURO_HOME 'infronai\start-litellm.ps1'
  if (Test-Path -Path $inf_profile -PathType Leaf -and (Test-Path -Path $inf_service -PathType Leaf)) {
    $env_override  = $inf_profile
    $port_override = 4003
    $svc_override  = $inf_service
    if (-not $env:LITELLM_LOG) { $env:LITELLM_LOG = "$env:TEMP\litellm-infron.log" }
  }
}

if ($current_source -eq 'tokenjuice') {
  $tj_profile = ''; $tj_service = ''
  foreach ($cand in @(
    (Join-Path $env:KURO_HOME 'tokenjuice\source') + ':' + (Join-Path $env:KURO_HOME 'tokenjuice\start-litellm.ps1'),
    (Join-Path $env:USERPROFILE 'claude-source\deepseek-claude') + ':' + (Join-Path $env:USERPROFILE 'start-litellm.sh')
  )) {
    $p = $cand.Split(':')[0]
    $s = $cand.Split(':')[1]
    if (Test-Path -Path $p -PathType Leaf -and (Test-Path -Path $s -PathType Leaf)) {
      $tj_profile = $p; $tj_service = $s; break
    }
  }
  if ($tj_profile) {
    $env_override  = $tj_profile
    $port_override = 4000
    $svc_override  = $tj_service
    if (-not $env:LITELLM_LOG) { $env:LITELLM_LOG = "$env:TEMP\litellm-tj.log" }
  }
}

if (-not $env_override) { $env_override = Join-Path $env:KURO_HOME 'logfare\source' }
if (-not $port_override) { $port_override = 4001 }
if (-not $env:LITELLM_LOG) { $env:LITELLM_LOG = "$env:TEMP\litellm-zim.log" }
if (-not $svc_override) { $svc_override = Join-Path $env:KURO_HOME 'logfare\start-litellm.ps1' }

$env:LITELLM_ENV_FILE  = $env_override
$env:LITELLM_PORT      = $port_override
$env:LITELLM_LOG       = $env:LITELLM_LOG
$env:LITELLM_SERVICE   = $svc_override

if (-not $env:KURO_NO_PROXY) { $env:KURO_NO_PROXY = '0' }
if (-not $env:KURO_REQUIRE_PROXY) { $env:KURO_REQUIRE_PROXY = '0' }

# Diagnostics
function die  { param($msg) Write-Error "[kuro] $msg" -ErrorAction Stop }
function warn { param($msg) Write-Warning "[kuro] $msg" }
function info { param($msg) Write-Host "[kuro] $msg" }

# Keep the checkout's own venv in step with requirements.txt.
function sync_deps {
  $req  = Join-Path $root 'requirements.txt'
  $stamp = Join-Path $root '.venv\.requirements.sha'
  if ($PY -ne $venv_py -or -not (Test-Path -Path $req -PathType Leaf)) { return 0 }

  $want = ''
  if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
    $want = (Get-FileHash -Path $req -Algorithm SHA256).Hash.ToLower()
  } else {
    return 0
  }
  if ((Test-Path -Path $stamp -PathType Leaf) -and ((Get-Content -Path $stamp -Raw).Trim() -eq $want)) { return 0 }

  info "dependencies changed - refreshing the venv (one-off)..."
  try {
    Push-Location -Path $root -ErrorAction Stop
    & $PY -m pip install --quiet --disable-pip-version-check -r $req 2>$null
    Pop-Location
    Set-Content -Path $stamp -Value $want -Encoding UTF8
    info "venv up to date."
  } catch {
    warn "could not refresh the venv - run $root\install.ps1"
  }
}

# --- preconditions -----------------------------------------------------------

if (-not $PY) {
  die "no python on PATH. Create a venv, activate it, then install the dependencies:
    python -m venv .venv & .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt"
}

sync_deps

# The dependencies must be importable by THIS interpreter.
try {
  Push-Location -Path 'C:\' -ErrorAction Stop
  $null = & $PY @('-c', 'import anthropic, textual, rich, yaml') 2>$null
  Pop-Location
} catch {
  die "Kuro's dependencies are not installed for $PY.
    Create a venv, activate it, then install them:
    python -m venv .venv & .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt"
}

# Fail CLOSED. Without the profile, the harness would silently use whatever
# ANTHROPIC_* the shell already exports.
if ($env:KURO_NO_PROXY -ne '1' -and -not (Test-Path -Path $env:LITELLM_ENV_FILE -PathType Leaf)) {
  die "profile not found: $env:LITELLM_ENV_FILE
    Refusing to run: without it the harness would use whatever ANTHROPIC_*
    variables your shell already exports.
    Fix:  run ./setup.ps1, or KURO_NO_PROXY=1 kuro ... for a direct endpoint."
}

# --- load the profile --------------------------------------------------------

$prev_base_url = $env:ANTHROPIC_BASE_URL
if ($skip_profile -ne 1 -and (Test-Path -Path $env:LITELLM_ENV_FILE -PathType Leaf)) {
  # Dot-source the profile: every `export VAR=value` line becomes a PowerShell
  # assignment. The profile is shell syntax (POSIX), so we translate the
  # `export KEY=VALUE` lines ourselves.
  $profile_lines = Get-Content -Path $env:LITELLM_ENV_FILE -Raw
  foreach ($line in ($profile_lines -split "`n")) {
    $trimmed = $line.Trim()
    if ($trimmed -match '^export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"$') {
      $key = $matches[1]
      $val = $matches[2]
      Set-Item -Path "env:$key" -Value $val
    } elseif ($trimmed -match "^export\s+([A-Za-z_][A-Za-z0-9_]*)='(.*)'$") {
      $key = $matches[1]
      $val = $matches[2]
      Set-Item -Path "env:$key" -Value $val
    }
  }
  if ($prev_base_url -and $prev_base_url -ne $env:ANTHROPIC_BASE_URL) {
    warn "overriding inherited ANTHROPIC_BASE_URL=$prev_base_url -> $env:ANTHROPIC_BASE_URL"
  }
}

# --- only manage a proxy when the profile points at the local one ------------

if ($env:ANTHROPIC_BASE_URL -like "*localhost:$env:LITELLM_PORT*" -or $env:ANTHROPIC_BASE_URL -like "*127.0.0.1:$env:LITELLM_PORT*") {
  # local proxy case, nothing to skip
} else {
  warn "ANTHROPIC_BASE_URL=$env:ANTHROPIC_BASE_URL is not the local proxy"
  warn "on :$env:LITELLM_PORT; skipping the proxy start-up check."
  $env:KURO_NO_PROXY = 1
}

function port_answers {
  try {
    $code = (Invoke-RestMethod -Uri "http://127.0.0.1:$env:LITELLM_PORT/health/liveliness" -Method Get -MaximumTimeoutMilliseconds 2000 -ErrorAction SilentlyContinue).StatusCode
    return $code -eq 200
  } catch {
    return $false
  }
}

function ensure_proxy {
  if ($env:KURO_NO_PROXY -eq '1') { return 0 }
  if (port_answers) { return 0 }

  if (-not (Test-Path -Path $env:LITELLM_SERVICE -PathType Leaf)) {
    warn "proxy not answering on :$env:LITELLM_PORT, and no service manager at"
    warn "$env:LITELLM_SERVICE. Run ./setup.ps1, or set KURO_NO_PROXY=1."
    return 1
  }

  info "LiteLLM proxy not answering on :$env:LITELLM_PORT - starting it..."
  try {
    & $env:LITELLM_SERVICE start
    info "proxy is up."
    return 0
  } catch {
    warn "could not start the proxy - see $env:LITELLM_LOG"
    return 1
  }
}

# Serialize cold starts
$lock_file = Join-Path $env:TEMP 'kuro-proxy.lock'
if ($env:KURO_NO_PROXY -ne '1') {
  $lock_stream = [System.IO.File]::Open($lock_file, 'OpenOrCreate', 'ReadWrite', 'None')
  try {
    $lock_stream.Lock(0, 0)
    ensure_proxy | Out-Null
    $lock_stream.Unlock(0, 0)
  } finally {
    $lock_stream.Close()
    $lock_stream.Dispose()
  }
} else {
  ensure_proxy | Out-Null
}

if ($env:KURO_REQUIRE_PROXY -eq '1' -and -not (port_answers)) {
  die "proxy is not healthy on :$env:LITELLM_PORT and KURO_REQUIRE_PROXY=1."
}

# --- hand off ----------------------------------------------------------------

# Make the package importable regardless of the caller's cwd.
$existing_pythonpath = $env:PYTHONPATH
if ($existing_pythonpath) {
  $env:PYTHONPATH = "$root;$existing_pythonpath"
} else {
  $env:PYTHONPATH = $root
}

# Hand off to the harness.
& $PY -m kuro $args
exit $LASTEXITCODE
