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

$env:KURO_HOME = $env:KURO_HOME -or (Join-Path $env:USERPROFILE '.kuro')

# Which upstream to use. An explicit environment variable always wins; failing
# that, honour the source last chosen with /zim-logfare or /zim-tokenjuice, so
# restarting lands back on whichever one was working. Absent either, Logfare.
$env_override   = $env:LITELLM_ENV_FILE
$port_override  = $env:LITELLM_PORT
$svc_override   = $env:LITELLM_SERVICE

$current_source = ''
$current_source_file = Join-Path $env:KURO_HOME 'current-source'
if (Test-Path -Path $current_source_file -PathType Leaf) {
  $current_source = (Get-Content -Path $current_source_file -Raw).Trim()
}

# A parked source is not a selection. Token Juice is under construction - the
# authority for that is UNDER_CONSTRUCTION in kuro\sources.py, and the check
# is repeated here only because this script reads current-source itself and
# cannot import Python. Without it, a selection file left over from before the
# park would start that proxy and point the session at it, which is the one
# thing parking it is meant to prevent.
#
# Logfare is NOT parked: `/zim-logfare` is a real switch, and a remembered
# selection of it is honoured like any other. It also happens to be the default
# route, so a cleared selection still lands there - the difference is that an
# explicit one now survives a restart.
if ($current_source -eq 'tokenjuice') {
  $current_source = ''
}

# An endpoint already in the environment wins over everything below.
#
# `source C:\claude-source\haiku-5.5 && kuro` is how the operator points a
# session at a hosted provider, and it has to mean the same thing here as it
# does for Claude Code. The launcher would otherwise source $LITELLM_ENV_FILE,
# whose own ANTHROPIC_BASE_URL would clobber the inherited one - leaving a
# session on the wrong provider with no indication, which is exactly what the
# fail-closed check further down exists to prevent. A *local* inherited URL is
# left on the normal path: that is the ordinary proxy case and still needs the
# profile and the health check.
$inherited_base_url = $env:ANTHROPIC_BASE_URL
$skip_profile = 0
if ($inherited_base_url -and $inherited_base_url -notlike '*localhost:*' -and $inherited_base_url -notlike '*127.0.0.1:*') {
  $skip_profile = 1
  $env:KURO_NO_PROXY = 1
}

if ($skip_profile -ne 1) {
  # The hosted gateways - TokenHarbour, OpenRouter - are remote: there
  # is no proxy to start, and no LiteLLM config to point at one. Kuro's own
  # profile (written by /<name>-api-setup) is sourced when it exists, which sets
  # ANTHROPIC_BASE_URL to the hosted endpoint; the case below then skips the
  # proxy on its own. When it does not exist the key lives in the environment or
  # in ~/claude-source, and the harness resolves it from there - so nothing is
  # sourced and nothing is required, but the fail-closed check must still be told
  # there is no proxy to insist on.
  #
  # Every hosted gateway needs this branch, not just the first one written.
  # Without it a remembered TokenHarbour or OpenRouter selection falls through
  # to the Logfare profile below: the launcher then tries to start LiteLLM for a
  # session that will never use it, and on a machine with no Logfare profile
  # installed it dies fail-closed - "profile not found" - for a gateway that
  # needs no profile at all. That is a session that cannot start, for a reason
  # that is not true.
  switch ($current_source) {
    'tokenharbour' { if (Test-Path -Path (Join-Path $env:KURO_HOME "tokenharbour\source") -PathType Leaf) { $env_override = Join-Path $env:KURO_HOME 'tokenharbour\source' }; $env:KURO_NO_PROXY = 1 }
    'openrouter'   { if (Test-Path -Path (Join-Path $env:KURO_HOME "openrouter\source") -PathType Leaf) { $env_override = Join-Path $env:KURO_HOME 'openrouter\source' }; $env:KURO_NO_PROXY = 1 }
  }
}

if ($current_source -eq 'iteracompute') {
  # A local proxy, like Logfare - but the profile is the operator's own key, so
  # there is nothing shipped to fall back to. When it is missing, fall through
  # to Logfare rather than dying fail-closed on a route the operator has not
  # finished setting up: `/iteracompute-api-setup` is the command that fixes it,
  # and a session that cannot start cannot run it.
  $ic_profile   = Join-Path $env:KURO_HOME 'iteracompute\source'
  $ic_service   = Join-Path $env:KURO_HOME 'iteracompute\start-litellm.ps1'
  if (Test-Path -Path $ic_profile -PathType Leaf -and (Test-Path -Path $ic_service -PathType Leaf)) {
    $env_override  = $ic_profile
    $port_override = 4002
    $svc_override  = $ic_service
    $env:LITELLM_LOG = $env:LITELLM_LOG -or "$env:TEMP\litellm-itera.log"
  }
}

if ($current_source -eq 'infronai') {
  # The same fail-open arrangement as IteraCompute: a local proxy whose profile
  # is the operator's own, so there is nothing shipped to fall back to. When it
  # is missing, fall through to Logfare rather than dying fail-closed on a route
  # that has not been set up - `/infronai-api-setup` is the command that fixes
  # it, and a session that cannot start cannot run it.
  $inf_profile  = Join-Path $env:KURO_HOME 'infronai\source'
  $inf_service  = Join-Path $env:KURO_HOME 'infronai\start-litellm.ps1'
  if (Test-Path -Path $inf_profile -PathType Leaf -and (Test-Path -Path $inf_service -PathType Leaf)) {
    $env_override  = $inf_profile
    $port_override = 4003
    $svc_override  = $inf_service
    $env:LITELLM_LOG = $env:LITELLM_LOG -or "$env:TEMP\litellm-infron.log"
  }
}

if ($current_source -eq 'tokenjuice') {
  # Prefer the installed route, which setup.ps1 populates; fall back to the
  # standalone deepseek-claude layout for a machine that had one before
  # Kuro was installed. Checking the installed copy first is what makes
  # this work on a fresh machine - the old hardcoded $HOME paths only exist on
  # the box where deepseek-claude happened to be installed.
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
  # Only switch if the Token Juice side is actually installed; otherwise fall
  # through to Logfare rather than dying on a missing profile.
  if ($tj_profile) {
    $env_override  = $tj_profile
    $port_override = 4000
    $svc_override  = $tj_service
    $env:LITELLM_LOG = $env:LITELLM_LOG -or "$env:TEMP\litellm-tj.log"
  }
}

$env:LITELLM_ENV_FILE  = $env_override  -or (Join-Path $env:KURO_HOME 'logfare\source')
$env:LITELLM_PORT      = $port_override -or 4001
$env:LITELLM_LOG       = $env:LITELLM_LOG  -or "$env:TEMP\litellm-zim.log"
$env:LITELLM_SERVICE   = $svc_override   -or (Join-Path $env:KURO_HOME 'logfare\start-litellm.ps1')

$env:KURO_NO_PROXY     = $env:KURO_NO_PROXY     -or '0'
$env:KURO_REQUIRE_PROXY= $env:KURO_REQUIRE_PROXY -or '0'

# Diagnostics go to stderr so piped/redirected runs stay parseable.
function die  { Write-Error "[kuro] $args" -ErrorAction Stop }
function warn { Write-Warning "[kuro] $args" }
function info { Write-Host "[kuro] $args" }

# Keep the checkout's own venv in step with requirements.txt.
#
# install.ps1 builds $KURO_ROOT\.venv once. requirements.txt can still move
# under it - a launch-time update pulls a new one, or the operator edits it -
# and a stale venv then fails the dependency check below on a missing import.
# So reinstall only when the file's content differs from what was last
# installed, recorded in a stamp inside the venv: an ordinary launch pays
# nothing but a hash. Only a venv Kuro owns is touched - a bring-your-own
# venv is left exactly as the operator set it up.
function sync_deps {
  $req  = Join-Path $root 'requirements.txt'
  $stamp = Join-Path $root '.venv\.requirements.sha'
  if ($PY -ne $venv_py -or -not (Test-Path -Path $req -PathType Leaf)) { return 0 }

  $want = ''
  if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
    $want = (Get-FileHash -Path $req -Algorithm SHA256).Hash.ToLower()
  } else {
    return 0   # no hasher: leave the venv alone rather than reinstall every launch
  }
  if (Test-Path -Path $stamp -PathType Leaf -and ((Get-Content -Path $stamp -Raw).Trim() -eq $want)) { return 0 }

  info "dependencies changed - refreshing the venv (one-off)..."
  # pip resolves a bare `-e .` against the CURRENT directory, not against the
  # requirements file it appears in - so this must run from the checkout, or
  # the trailing `-e .` in requirements.txt installs whatever project the
  # operator happened to be standing in. Subshell, so the launch's own cwd is
  # left alone.
  try {
    Push-Location -Path $root -ErrorAction Stop
    & $PY -m pip install --quiet --disable-pip-version-check -r $req 
    Pop-Location
    Set-Content -Path $stamp -Value $want -Encoding UTF8
    info "venv up to date."
  } catch {
    warn "could not refresh the venv - run $root\install.ps1"
  }
}

# --- preconditions -----------------------------------------------------------

if (-not $PY) {
  die "no python on PATH.
       Create a venv, activate it, then install the dependencies:
           python -m venv .venv & .\.venv\Scripts\Activate.ps1
           pip install -r requirements.txt"
}

# Bring the bundled venv up to date first, so the import check below tests the
# interpreter as it will actually be after this launch, not as it was.
sync_deps

# The dependencies must be importable by THIS interpreter. Checking here turns
# a confusing ImportError at launch into the one command that fixes it. Run
# from C:\ so a stray checkout on sys.path cannot satisfy the import; kuro
# itself is made importable by the PYTHONPATH export below, so only the
# third-party packages are checked.
try {
  Push-Location -Path 'C:\' -ErrorAction Stop
  $null = & $PY -c 'import anthropic, textual, rich, yaml'
  Pop-Location
} catch {
  die "Kuro's dependencies are not installed for $PY
       Create a venv, activate it, then install them:
           python -m venv .venv & .\.venv\Scripts\Activate.ps1
           pip install -r requirements.txt"
}

# Fail CLOSED. Without the profile, the harness would silently use whatever
# ANTHROPIC_* the shell already exports - a session against the wrong provider
# with no indication anything was off.
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
  # `export KEY="VALUE"` lines ourselves rather than invoking a POSIX shell.
  $profile_lines = Get-Content -Path $env:LITELLM_ENV_FILE -Raw
  foreach ($line in ($profile_lines -split "`n")) {
    $trimmed = $line.Trim()
    if ($trimmed -match '^export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"$') {
      $env:$matches[1] = $matches[2]
    } elseif ($trimmed -match "^export\s+([A-Za-z_][A-Za-z0-9_]*)='(.*)'$") {
      $env:$matches[1] = $matches[2]
    }
  }
  if ($prev_base_url -and $prev_base_url -ne $env:ANTHROPIC_BASE_URL) {
    warn "overriding inherited ANTHROPIC_BASE_URL=$prev_base_url -> $env:ANTHROPIC_BASE_URL"
  }
}

# --- only manage a proxy when the profile points at the local one ------------

switch ($env:ANTHROPIC_BASE_URL) {
  { $_ -like "*localhost:$env:LITELLM_PORT*" -or $_ -like "*127.0.0.1:$env:LITELLM_PORT*" } {}
  default {
    warn "ANTHROPIC_BASE_URL=$env:ANTHROPIC_BASE_URL is not the local proxy"
    warn "on :$env:LITELLM_PORT; skipping the proxy start-up check."
    $env:KURO_NO_PROXY = 1
  }
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
  # Invoke as a subprocess, never dot-source: start-litellm.ps1's trailing
  # switch statement runs on load, and its functions call exit.
  try {
    & $env:LITELLM_SERVICE start
    info "proxy is up."
    return 0
  } catch {
    warn "could not start the proxy - see $env:LITELLM_LOG"
    return 1
  }
}

# Serialize cold starts: do_start() has a check-then-act window, so two racing
# invocations would both spawn litellm and leave an orphan on the port.
#
# Close the lock handle before handing off to the harness. Without that, the
# harness (and any LiteLLM it later starts) inherits the open file and holds the
# lock for the whole session - /zim-tokenjuice then blocks on the lock until
# start_proxy times out, even though a manual start takes a few seconds.
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
$env:PYTHONPATH = "$root" + if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { '' }

# Exported so the harness can find this launcher again. After a launch-time
# update replaces the code, __main__ re-execs *through this script* rather than
# straight to the interpreter, so the dependency sync above runs against the
# requirements.txt the update just pulled. Without the export it would have to
# guess the path.
# (KURO_ROOT is already exported above.)

# Hand off to the harness. No extra PowerShell layer remains: exit status,
# signal delivery and TTY ownership all belong to the harness.
& $PY -m kuro $args
exit $LASTEXITCODE

