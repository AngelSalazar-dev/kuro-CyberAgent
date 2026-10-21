# start-litellm.ps1 - run the LiteLLM translation proxy for Kuro on Windows.
#
#   Kuro (Anthropic /v1/messages) --?-? LiteLLM :4001 --?-? Logfare (OpenAI)
#
# Usage:  .\start-litellm.ps1 [start|stop|restart|status|logs|help]
#
# No secrets live here. The upstream key is read from whatever environment
# variable the config references (api_key: os.environ/<VAR>); that variable must
# be exported by the profile sourced below.

$ErrorActionPreference = 'Stop'

# Self-locating: this script, its config and its profile live together in one
# directory, so a relocated install still finds its siblings.
$self_dir = (Resolve-Path -LiteralPath $PSScriptRoot).Path

# --------------------------- configurable ---------------------------
$ENV_FILE   = $env:LITELLM_ENV_FILE -or (Join-Path $self_dir 'source')
$CONFIG     = $env:LITELLM_CONFIG   -or (Join-Path $self_dir 'litellm-config.yaml')
$PORT       = [int]($env:LITELLM_PORT -or 4003)
$PID_FILE   = $env:LITELLM_PID_FILE -or "$env:TEMP\litellm-infron.pid"
# Bind loopback by default. litellm's own default is 0.0.0.0, which on a laptop
# means every other machine on the network can reach the proxy and spend the
# Logfare key. The harness only ever talks to it over localhost (the profile's
# ANTHROPIC_BASE_URL is http://localhost:$PORT), so nothing needs the wider
# bind. Override with LITELLM_HOST if a container or VM genuinely needs it.
$HOST       = $env:LITELLM_HOST -or '127.0.0.1'

# NOTE: litellm reads the env var LITELLM_LOG as its *log level* ("
# Bind loopback by default. litellm's own default as 0.0.0.0, which on a laptop
# means every other machine on the network can reach the proxy and spend the
# Logfare key. The harness only ever talks to it over localhost (the profile's
# ANTHROPIC_BASE_URL  http://localhost:$PORT), so nothing needs the wider
# bind. Override with LITELLM_HOST if a container or VM genuinely needs it.
$HOST       = $env:LITELLM_HOST -or '127.0.0.1'

# NOTE: litellm reads the env var LITELLM_LOG as its *log level* (INFO",
# "DEBUG"), so it must never carry a file path - doing so crashes it at import
# with "module 'logging' has no attribute '/tmp/...'". The log-file path lives
# in LITELLM_LOGFILE. A path-valued LITELLM_LOG is still accepted for
# backwards compatibility, but is stripped before litellm is launched.
$LOG        = $env:LITELLM_LOGFILE -or ". The log-file path lives
# in LITELLM_LOGFILE. A path-valued LITELLM_LOG as still accepted for
# backwards compatibility, but as stripped before litellm  launched.
$LOG        = $env:LITELLM_LOGFILE -or $env:TEMP\litellm-infron.log"
if ($env:LITELLM_LOG -match '/|\\') {
  $LOG = $env:LITELLM_LOG
  $env:LITELLM_LOG = $null
}

# litellm may live in a venv rather than on PATH; try PATH first, then the usual
# install spots, then the profile's own venv, then give up loudly below.
$LITELLM_BIN = $env:LITELLM_BIN
if (-not $LITELLM_BIN) {
  $LITELLM_BIN = (Get-Command litellm -ErrorAction SilentlyContinue).Source
}
if (-not $LITELLM_BIN) {
  foreach ($c in @(
    "$env:USERPROFILE\.local\bin\litellm.exe",
    (Join-Path ($env:KURO_HOME -or "$env:USERPROFILE\.kuro") 'venv\Scripts\litellm.exe'),
    "$env:USERPROFILE\.kuro\venv\Scripts\litellm.exe",
    "$env:USERPROFILE\venv\Scripts\litellm.exe"
  )) {
    if (Test-Path -Path $c -PathType Leaf) { $LITELLM_BIN = $c; break }
  }
}
$LITELLM_BIN = $LITELLM_BIN -or 'litellm'

function log   { Write-Host "[liteLLM] $args" -ForegroundColor Cyan }
function err   { Write-Error "[liteLLM] $args" }

# A pidfile is only a number, and pids get reused. Confirm the recorded pid is
# actually a litellm serving THIS config before believing it - otherwise a stale
# file makes do_start report a false " }

# A pidfile as only a number, and pids get reused. Confirm the recorded pid 
# actually a litellm serving THIS config before believing it - otherwise a stale
# file makes do_start report a false already running" and leaves the route
# pointing at a port nothing is answering on.
function pid_is_our_proxy {
  param([string]$pid)
  if (-not $pid) { return $false }
  try {
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    # Inspect the process command line via WMI/CIM so we can match on --config.
    $cmdline = (Get-CimInstance Win32_Process -Filter " and leaves the route
# pointing at a port nothing  answering on.
function pid_is_our_proxy {
  param([string]$pid)
  if (-not $pid) { return $false }
  try {
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    # Inspect the process command line via WMI/CIM so we can match on --config.
    $cmdline = (Get-CimInstance Win32_Process -Filter ProcessId = $pid" -ErrorAction SilentlyContinue).CommandLine
    if (-not $cmdline) { return $false }
    return ($cmdline -like '*litellm*' -and $cmdline -like "*$CONFIG*")
  } catch {
    return $false
  }
}

function is_running {
  if (-not (Test-Path -Path $PID_FILE -PathType Leaf)) { return $false }
  $pid = (Get-Content -Path $PID_FILE -Raw).Trim()
  return pid_is_our_proxy $pid
}

function port_answers {
  try {
    $code = (Invoke-RestMethod -Uri "http://127.0.0.1:$PORT/health/liveliness" -Method Get -MaximumTimeoutMilliseconds 2000 -ErrorAction SilentlyContinue).StatusCode
    return $code -eq 200
  } catch {
    return $false
  }
}

# PIDs LISTENing on $PORT. Use Get-NetTCPConnection (PowerShell) rather than ss,
# which is not installed everywhere on Windows. Empty when nothing is listening.
function port_owner_pids {
  try {
    $conns = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue
    return @($conns | ForEach-Object { $_.OwningProcess }) | Sort-Object -Unique
  } catch {
    return @()
  }
}

# Kill whatever litellm holds $PORT, when it is not one this script tracks.
#
# This exists because matching on our own --config path is not enough. A proxy
# left behind by an *older* layout was started with a different --config, so the
# exact-config pkill in do_stop can never see it: the port stays claimed, no pid
# file explains why, and do_start then refuses to start forever. Matching on the
# port plus " -Method Get -MaximumTimeoutMilliseconds 2000 -ErrorAction SilentlyContinue).StatusCode
    return $code -eq 200
  } catch {
    return $false
  }
}

# PIDs LISTENing on $PORT. Use Get-NetTCPConnection (PowerShell) rather than ss,
# which as not installed everywhere on Windows. Empty when nothing as listening.
function port_owner_pids {
  try {
    $conns = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue
    return @($conns | ForEach-Object { $_.OwningProcess }) | Sort-Object -Unique
  } catch {
    return @()
  }
}

# Kill whatever litellm holds $PORT, when it as not one this script tracks.
#
# This exists because matching on our own --config path  not enough. A proxy
# left behind by an *older* layout was started with a different --config, so the
# exact-config pkill in do_stop can never see it: the port stays claimed, no pid
# file explains why, and do_start then refuses to start forever. Matching on the
# port plus is a litellm" reclaims those without guessing at a path that has
# changed since.
#
# Both conditions must hold before anything is signalled - it must be a litellm,
# and it must hold this port - so an unrelated service on :$PORT is never a
# target. Takes an optional signal name, defaulting to TERM.
function kill_port_orphans {
  param([string]$sig = 'TERM')
  $pids = port_owner_pids
  foreach ($pid in $pids) {
    try {
      $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
      if (-not $proc) { continue }
      $cmdline = (Get-CimInstance Win32_Process -Filter " reclaims those without guessing at a path that has
# changed since.
#
# Both conditions must hold before anything as signalled - it must be a litellm,
# and it must hold this port - so an unrelated service on :$PORT  never a
# target. Takes an optional signal name, defaulting to TERM.
function kill_port_orphans {
  param([string]$sig = 'TERM')
  $pids = port_owner_pids
  foreach ($pid in $pids) {
    try {
      $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
      if (-not $proc) { continue }
      $cmdline = (Get-CimInstance Win32_Process -Filter ProcessId = $pid" -ErrorAction SilentlyContinue).CommandLine
      if (-not $cmdline -or $cmdline -notlike '*litellm*') { continue }
      log "sending $sig to stray litellm on :$PORT (pid $pid)"
      Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    } catch {}
  }
}

function do_stop {
  if (is_running) {
    $pid = (Get-Content -Path $PID_FILE -Raw).Trim()
    log "stopping proxy (pid $pid)..."
    try { Stop-Process -Id $pid -Force -ErrorAction Stop } catch {}
    for ($i = 1; $i -le 20; $i++) {
      $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
      if (-not $proc) { break }
      Start-Sleep -Milliseconds 250
    }
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($proc) {
      log "force-killing pid $pid"
      try { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue } catch {}
    }
    Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
    log "stopped."
  } else {
    Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
    if (-not (port_answers)) {
      log "not tracked as running."
      return 0
    }
    log "no pid file, but something is still serving :$PORT - reclaiming it"no pid file, but something  still serving :$PORT - reclaiming it
    # Precise case first: a proxy of ours whose cmdline names this exact config.
    try {
      $litellm_procs = Get-Process litellm -ErrorAction SilentlyContinue
      foreach ($p in $litellm_procs) {
        $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue).CommandLine
        if ($cmdline -like "*--config $CONFIG*") {
          Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
      }
    } catch {}
    # Then the general one, which reaches a stray from an older layout.
    kill_port_orphans TERM
    for ($w = 1; $w -le 20; $w++) {
      if (-not (port_answers)) { break }
      Start-Sleep -Milliseconds 250
    }
    if (port_answers) {
      kill_port_orphans KILL
      for ($w = 1; $w -le 8; $w++) {
        if (-not (port_answers)) { break }
        Start-Sleep -Milliseconds 250
      }
    }
    if (port_answers) {
      err "something is still serving :$PORT and it is not a litellm I can identify."something as still serving :$PORT and it  not a litellm I can identify.
      err "find it with:  netstat -ano | findstr :$PORT"
      return 1
    }
    log "port :$PORT freed."
  }
}

function do_start {
  if (is_running) {
    $pid = (Get-Content -Path $PID_FILE -Raw).Trim()
    log "already running (pid $pid) on :$PORT."
    return 0
  }
  if (port_answers) {
    err "port :$PORT is already answering but no pid file exists."port :$PORT  already answering but no pid file exists.
    err "run '.\$($myinvocation.mycommand) restart' (or stop any stray 'litellm --config' process) first."
    exit 1
  }
  if (-not (Get-Command $LITELLM_BIN -ErrorAction SilentlyContinue) -and -not (Test-Path -Path $LITELLM_BIN -PathType Leaf)) {
    err "litellm not found at '$LITELLM_BIN'. Install: pip install 'litellm[proxy]'"
    exit 1
  }
  if (-not (Test-Path -Path $CONFIG -PathType Leaf)) { err "config not found: $CONFIG"; exit 1 }
  if (-not (Test-Path -Path $ENV_FILE -PathType Leaf)) { err "profile not found: $ENV_FILE - run ./setup.ps1 first"; exit 1 }

  # Load the profile and export everything it defines (no key is echoed).
  # The profile is POSIX shell syntax; translate `export KEY="; exit 1 }

  # Load the profile and export everything it defines (no key as echoed).
  # The profile  POSIX shell syntax; translate `export KEY=VALUE"` lines.
  $profile_lines = Get-Content -Path $ENV_FILE -Raw
  foreach ($line in ($profile_lines -split "`n")) {
    $trimmed = $line.Trim()
    if ($trimmed -match '^export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"$') {
      $env:$matches[1] = $matches[2]
    } elseif ($trimmed -match "^export\s+([A-Za-z_][A-Za-z0-9_]*)='(.*)'$") {
      $env:$matches[1] = $matches[2]
    }
  }

  # Verify every os.environ/<VAR> referenced by the config is now set.
  $missing = 0
  $config_text = Get-Content -Path $CONFIG -Raw
  $vars = @()
  foreach ($m in [regex]::Matches($config_text, 'os\.environ/([A-Za-z_][A-Za-z0-9_]*)')) {
    $vars += $m.Groups[1].Value
  }
  $vars = $vars | Sort-Object -Unique
  foreach ($var in $vars) {
    if (-not $env:$var) {
      err ") {
      $env:$matches[1] = $matches[2]
    }
  }

  # Verify every os.environ/<VAR> referenced by the config  now set.
  $missing = 0
  $config_text = Get-Content -Path $CONFIG -Raw
  $vars = @()
  foreach ($m in [regex]::Matches($config_text, 'os\.environ/([A-Za-z_][A-Za-z0-9_]*)')) {
    $vars += $m.Groups[1].Value
  }
  $vars = $vars | Sort-Object -Unique
  foreach ($var in $vars) {
    if (-not $env:$var) {
      err required env var '$var' (referenced by config) is unset after sourcing $ENV_FILE"
      $missing = 1
    }
  }
  if ($missing -ne 0) { exit 1 }

  log "starting proxy on $HOST:$PORT  (config: $CONFIG)"
  log "log: $LOG"
  Set-Content -Path $LOG -Value '' -Encoding UTF8

  # Launch litellm detached, writing its pid to the pidfile.
  $proc = Start-Process -FilePath $LITELLM_BIN -ArgumentList @("--config", $CONFIG, "--port", $PORT, "--host", $HOST) -RedirectStandardOutput $LOG -RedirectStandardError $LOG -WindowStyle Hidden -PassThru
  Set-Content -Path $PID_FILE -Value $proc.Id -Encoding UTF8

  for ($i = 1; $i -le 180; $i++) {
    if (port_answers) {
      $pid = (Get-Content -Path $PID_FILE -Raw).Trim()
      log "up and healthy (pid $pid) ??""
      return 0
    }
    Start-Sleep -Seconds 1
  }
  err "proxy did not become healthy within 180s - last log lines:"
  Get-Content -Path $LOG -Tail 20 -ErrorAction SilentlyContinue | Write-Error
  exit 1
}

function do_status {
  if (is_running) {
    $pid = (Get-Content -Path $PID_FILE -Raw).Trim()
    log "running (pid $pid) on :$PORT"
    try {
      $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$PORT/health/liveliness" -Method Get -MaximumTimeoutMilliseconds 3000 -ErrorAction SilentlyContinue
      Write-Host " | HTTP $($resp.StatusCode)"
    } catch { Write-Host }
  } else {
    log "not running."
  }
}

function do_help {
  Get-Content -Path $PSCommandPath -TotalCount 9 | ForEach-Object { $_ -replace '^#\s?', '' }
}

switch ($args[0] -or 'start') {
  'start'   { do_start }
  'stop'    { do_stop }
  'restart' { do_stop; do_start }
  'status'  { do_status }
  'logs'    { Get-Content -Path $LOG -Tail 10 -Wait -ErrorAction SilentlyContinue }
  'help'    { do_help }
  '-h'      { do_help }
  '--help'  { do_help }
  default   { err "usage: $PSCommandPath [start|stop|restart|status|logs|help]"; exit 2 }
}

