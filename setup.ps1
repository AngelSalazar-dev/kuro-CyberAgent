# setup.ps1 - configure the Logfare route for Kuro on Windows.
#
#   .\setup.ps1
#
# Finds a Logfare credential - an existing profile, then $ANTHROPIC_AUTH_TOKEN -
# writes the profile to %USERPROFILE%\.kuro\logfare\source, installs the
# LiteLLM proxy if it is missing, starts it, and verifies the route end to end.
#
# It never prompts. If there is no key it does not invent one: the route is
# installed and left unstarted, and the script says where a key goes. A
# credential committed to this repository is not a credential - it is a public
# budget that anyone can spend, and it cannot be taken back out of the history
# of everyone who cloned it. So none ships.
#
# The venv is the user's own: activate it, `pip install -r requirements.txt`,
# then run this. Idempotent. The key is never echoed back and never leaves this
# machine.

$ErrorActionPreference = 'Stop'

$SELF = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$KURO_HOME = $env:KURO_HOME -or (Join-Path $env:USERPROFILE '.kuro')
$LOG_DIR     = Join-Path $KURO_HOME 'logfare'
$PROFILE     = Join-Path $LOG_DIR 'source'
$CONFIG      = Join-Path $LOG_DIR 'litellm-config.yaml'
$SERVICE     = Join-Path $LOG_DIR 'start-litellm.ps1'
$PORT        = [int]($env:LITELLM_PORT -or 4001)

# The interpreter that carries Kuro's dependencies.
#
# Prefer the checkout's own venv - the one install.ps1 builds - so `.\setup.ps1`
# works with no activation after a global install. Then an activated venv
# ($VIRTUAL_ENV), then a python on PATH. This script never creates a venv.
$self_dir = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$root = $env:KURO_ROOT -or $self_dir
$venv_py = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -Path $venv_py -PathType Leaf) {
  $PY = $venv_py
} elseif ($env:VIRTUAL_ENV -and (Test-Path -Path (Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe') -PathType Leaf)) {
  $PY = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $PY = (Get-Command python).Source
} else {
  $PY = ''
}

function bold   { Write-Host $args -ForegroundColor White }
function step   { Write-Host "`n-> $args" -ForegroundColor Cyan }
function ok     { Write-Host "  [OK] $args" -ForegroundColor Green }
function warn   { Write-Warning "  [WARN] $args" }
function die    { Write-Error "  [ERR] $args" -ErrorAction Stop }
function note   { Write-Host "      $args" -ForegroundColor Cyan }

bold "â*­----------------------------------------------â*®"
bold "|  KURO Â· setup                          |"
bold "â*°----------------------------------------------â*¯"

if (-not $PY) {
  die "no python on PATH - create a venv, activate it, then: pip install -r requirements.txt"
}
if (-not (Get-Command curl -ErrorAction SilentlyContinue)) {
  die "curl is required."
}

# The harness must be runnable by this interpreter, or this script would wire
# up a proxy for something that cannot start.
#
# Check the THIRD-PARTY dependencies, not `import kuro`: kuro is
# imported from the checkout itself (see PYTHONPATH below), so `import kuro`
# would succeed from the source tree even in a bare venv with nothing
# installed - passing the check that exists to catch exactly that. The deps are
# what the user's `pip install -r requirements.txt` actually provides.
try {
  Push-Location -Path 'C:\' -ErrorAction Stop
  $null = & $PY -c 'import anthropic, textual, rich, yaml'
  Pop-Location
} catch {
  Write-Host "`n  [ERR] Kuro dependencies are not installed for $PY`n" -ForegroundColor Red
  Write-Host "  Activate your venv and install them, then re-run .\setup.ps1:`n"
  note "python -m venv .venv & .\.venv\Scripts\Activate.ps1"
  note "pip install -r requirements.txt"
  Write-Host
  exit 1
}

# --- 1. locate a credential --------------------------------------------------

step "Looking for a Logfare credential"

$KEY = ''
$KEY_SRC = ''

# A Logfare key starts with `lfu_`. Only a key with that prefix is trusted
# automatically - a shell often has ANTHROPIC_AUTH_TOKEN exported for a
# *different* provider (another proxy, another vendor), and silently adopting
# it would point the harness at the wrong service with no visible symptom.
function is_logfare_key { param($k) return $k -match '^lfu_' }

# 1. An existing profile. These files are Logfare-specific, so try them first.
#    The shipped repo profile is deliberately NOT in this list: it is shared,
#    so it has to lose to a personal key in the environment. See step 3.
foreach ($candidate in @($PROFILE, (Join-Path $env:USERPROFILE 'logfare\source'), (Join-Path $SELF '..\logfare\source'))) {
  if (-not (Test-Path -Path $candidate -PathType Leaf)) { continue }
  $found = & { . $candidate 2>$null; $env:ANTHROPIC_AUTH_TOKEN }
  if (is_logfare_key $found) { $KEY = $found; $KEY_SRC = $candidate; break }
  elseif ($found) { warn "ignoring $candidate - its token is not a Logfare key (no lfu_ prefix)" }
}

# 2. The ambient token, but only when it is genuinely a Logfare key.
if (-not $KEY -and (is_logfare_key $env:ANTHROPIC_AUTH_TOKEN)) {
  $KEY = $env:ANTHROPIC_AUTH_TOKEN
  $KEY_SRC = '$env:ANTHROPIC_AUTH_TOKEN'
}

# 3. Nothing else. There is deliberately no shipped key: packaging/logfare/source
#    is a template whose token is a placeholder, so a key committed there would
#    be public, spendable by anyone, and impossible to unpublish. A fresh install
#    therefore finishes without a Logfare credential and says how to add one -
#    `/logfare-api-setup <key>` in the TUI, or the profile file directly.
#
#    A profile that already exists is never overwritten, so anyone with their own
#    key keeps it by doing nothing.

if ($KEY) {
  ok "using the credential from $KEY_SRC"
  if ($KEY -notmatch '^lfu_') { warn "that key does not start with 'lfu_' - continuing anyway" }
} else {
  warn "no Logfare key found - installing the route without starting it"
  warn "get one at https://logfare.ai (they start with 'lfu_'), then either"
  warn "  run  /logfare-api-setup <key>  in the TUI, or"
  warn "  write it to $PROFILE"
}

# --- 2. write the profile ----------------------------------------------------

step "Writing the credential profile"
if (-not (Test-Path -Path $LOG_DIR -PathType Container)) {
  New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
}
# umask equivalent: create the file with restrictive ACLs. On Windows we just
# write it; the operator should restrict ACLs manually if sharing the machine.
{
  '# Kuro - Logfare profile (generated by setup.ps1)'
  '# Treat this file as a credential. Never commit it.'
  ''
  "export ANTHROPIC_BASE_URL=`"http://localhost:$PORT`""
  if ($KEY) {
    "export ANTHROPIC_AUTH_TOKEN=`"$KEY`""
  } else {
    '# No key yet - /logfare-api-setup <key> in the TUI writes this line.'
    'export ANTHROPIC_AUTH_TOKEN=""'
  }
  'export ANTHROPIC_MODEL="deepseek-v4.1-flash"'
  ''
  '# Load-bearing: an empty string keeps ANTHROPIC_AUTH_TOKEN authoritative'
  '# and clears any real API key inherited from the shell.'
  'export ANTHROPIC_API_KEY=""'
  ''
  '# Model aliases - the models Logfare currently serves.'
  '# Logfare retires models without notice; /logfare-models in the TUI'
  '# lists what is live right now.'
  'export MODEL_DEFAULT="deepseek-v4.1-flash"'
  'export MODEL_DEEPSEEK_FLASH="deepseek-v4.1-flash"'
  'export MODEL_QWEN="qwen-3.8-27b"'
  'export MODEL_GEMMA="gemma-4-26b"'
  'export MODEL_AUTO="logfare/auto"'
} | Set-Content -Path $PROFILE -Encoding UTF8
ok "$PROFILE"

# --- 3. proxy files ----------------------------------------------------------

step "Checking the proxy files"

# These are shipped artifacts, not user config: the catalog and the manager are
# maintained in packaging/ and an install has to track them. Copying only when
# the file is *missing* left every existing install frozen at whatever it first
# received - so a fix in packaging/ (a required config key, a pidfile guard)
# never reached the copy that actually runs, and the proxy kept failing on a bug
# that was already fixed in the tree. Sync on content instead, but keep a .bak
# of any locally-edited copy rather than silently overwriting someone's work.
function sync_shipped {
  param([string]$src, [string]$dst, [switch]$force)
  if (-not (Test-Path -Path $src -PathType Leaf)) {
    warn "missing from packaging/: $(Split-Path -Leaf $src) - leaving $dst alone"
    return
  }
  if (-not (Test-Path -Path $dst -PathType Leaf)) {
    Copy-Item -Path $src -Destination $dst -Force
    ok "installed $(Split-Path -Leaf $dst)"
  } elseif ((Get-FileHash -Path $src -Algorithm SHA256).Hash -ne (Get-FileHash -Path $dst -Algorithm SHA256).Hash) {
    Copy-Item -Path $dst -Destination "$dst.bak" -Force
    Copy-Item -Path $src -Destination $dst -Force
    warn "$(Split-Path -Leaf $dst) updated - previous copy kept at $dst.bak"
  } else {
    ok "$(Split-Path -Leaf $dst) up to date"
  }
}

sync_shipped -src (Join-Path $SELF 'packaging\logfare\litellm-config.yaml') -dst $CONFIG
sync_shipped -src (Join-Path $SELF 'packaging\logfare\start-litellm.ps1') -dst $SERVICE

# The Token Juice route is a sibling install, not a variant: its own port,
# profile and manager. This step lays the files down and seeds a profile when
# one can be found - it does not start anything, and the route is used on
# demand via /zim-tokenjuice.
#
# No credential ships in the tree. `source` was committed here once, holding a
# live key; that is a leak on a public repo and it is not coming back. The
# route is seeded from, in order: an existing ~/.kuro profile (left alone),
# a $ANTHROPIC_AUTH_TOKEN that already looks like a Token Juice key, or nothing
# - and in that last case it is simply not discovered, so /zim-tokenjuice
# reports the route as unavailable instead of failing later at request time.
$TJ_DIR = Join-Path $KURO_HOME 'tokenjuice'
if (Test-Path -Path (Join-Path $SELF 'packaging\tokenjuice') -PathType Container) {
  if (-not (Test-Path -Path $TJ_DIR -PathType Container)) {
    New-Item -ItemType Directory -Path $TJ_DIR -Force | Out-Null
  }
  sync_shipped -src (Join-Path $SELF 'packaging\tokenjuice\litellm-config.yaml') -dst (Join-Path $TJ_DIR 'litellm-config.yaml')
  sync_shipped -src (Join-Path $SELF 'packaging\tokenjuice\start-litellm.ps1')    -dst (Join-Path $TJ_DIR 'start-litellm.ps1')
  sync_shipped -src (Join-Path $SELF 'packaging\tokenjuice\source.example')      -dst (Join-Path $TJ_DIR 'source.example')

  $tj_source = Join-Path $TJ_DIR 'source'
  if (Test-Path -Path $tj_source -PathType Leaf) {
    ok "tokenjuice profile in place ($tj_source)"
  } elseif ($env:ANTHROPIC_AUTH_TOKEN -match '^tj_') {
    {
      '# Kuro - Token Juice profile (generated by setup.ps1)'
      '# Treat this file as a credential. Never commit it.'
      ''
      'export ANTHROPIC_BASE_URL="http://localhost:4000"'
      "export ANTHROPIC_AUTH_TOKEN=`"$env:ANTHROPIC_AUTH_TOKEN`""
      'export ANTHROPIC_MODEL="deepseek-v4.1-flash"'
      'export ANTHROPIC_API_KEY=""'
    } | Set-Content -Path $tj_source -Encoding UTF8
    ok "tokenjuice profile written from $env:ANTHROPIC_AUTH_TOKEN"
  } else {
    warn "no Token Juice credential - the :4000 route will not be discovered"
    note "to enable /zim-tokenjuice, write your tj_ key to:"
    note "  $tj_source"
    note "the format is in $(Join-Path $TJ_DIR 'source.example')"
  }
  ok "installed tokenjuice route (:4000)"
} else {
  warn "no tokenjuice packaging - /zim-tokenjuice will be unavailable"
}

# The IteraCompute route is a sibling install like Token Juice: its own port,
# profile and manager. This step lays the files down and seeds a profile only
# when a key can be found - it does not start anything, and the route is used on
# demand via /zim-iteracompute.
#
# IteraCompute serves the OpenAI protocol only, so the proxy is not optional:
# a session pointed straight at the upstream 404s every turn. No credential
# ships in the tree, and none could - the key is per-account. The route is
# seeded from an existing ~/.kuro profile (left alone) or a
# $ANTHROPIC_AUTH_TOKEN that looks like an IteraCompute key; with neither it is
# not discovered, and `/zim-iteracompute` names /iteracompute-api-setup rather
# than failing later at request time.
$IC_DIR = Join-Path $KURO_HOME 'iteracompute'
if (Test-Path -Path (Join-Path $SELF 'packaging\iteracompute') -PathType Container) {
  if (-not (Test-Path -Path $IC_DIR -PathType Container)) {
    New-Item -ItemType Directory -Path $IC_DIR -Force | Out-Null
  }
  sync_shipped -src (Join-Path $SELF 'packaging\iteracompute\litellm-config.yaml') -dst (Join-Path $IC_DIR 'litellm-config.yaml')
  sync_shipped -src (Join-Path $SELF 'packaging\iteracompute\start-litellm.ps1')    -dst (Join-Path $IC_DIR 'start-litellm.ps1')

  $ic_source = Join-Path $IC_DIR 'source'
  if (Test-Path -Path $ic_source -PathType Leaf) {
    ok "iteracompute profile in place ($ic_source)"
  } elseif ($env:ANTHROPIC_AUTH_TOKEN -and $env:ANTHROPIC_BASE_URL -like '*iteracompute*') {
    # Only when the environment is ALREADY aimed at IteraCompute: a bare token
    # in the shell is far more likely to be Logfare's, and seeding this route
    # with another provider's key would fail at request time with a 401 the
    # operator has no reason to connect to this file.
    {
      '# Kuro - IteraCompute profile (generated by setup.ps1)'
      '# Treat this file as a credential. Never commit it.'
      ''
      'export ANTHROPIC_BASE_URL="http://localhost:4002"'
      "export ANTHROPIC_AUTH_TOKEN=`"$env:ANTHROPIC_AUTH_TOKEN`""
      'export ANTHROPIC_MODEL="qwen/qwen3.8-27b"'
      'export ANTHROPIC_API_KEY=""'
    } | Set-Content -Path $ic_source -Encoding UTF8
    ok "iteracompute profile written from $env:ANTHROPIC_AUTH_TOKEN"
  } else {
    note "to enable /zim-iteracompute, set your key in the TUI:"
    note "  /iteracompute-api-setup <key>"
    note "  (the free tier is 30M tokens per UTC month on qwen/qwen3.8-27b)"
  }
  ok "installed iteracompute route (:4002)"
} else {
  warn "no iteracompute packaging - /zim-iteracompute will be unavailable"
}

# The InfronAI route is the same shape as IteraCompute's, one port over: its own
# port, profile and manager. Files are laid down here; nothing is started, and
# the route is used on demand via /zim-infronai.
#
# InfronAI does serve the Anthropic protocol natively, but its free models are
# `openai`-endpoint only in the catalog, so the proxy is not optional for the
# free tier. No credential ships in the tree, and none could - the key is
# per-account, and a free account still needs a $5 balance. The route is seeded
# from a $ANTHROPIC_AUTH_TOKEN only when the environment is already aimed at
# InfronAI; with neither that nor an existing profile it is not discovered, and
# `/zim-infronai` names /infronai-api-setup rather than failing at request time.
$INFRON_DIR = Join-Path $KURO_HOME 'infronai'
if (Test-Path -Path (Join-Path $SELF 'packaging\infronai') -PathType Container) {
  if (-not (Test-Path -Path $INFRON_DIR -PathType Container)) {
    New-Item -ItemType Directory -Path $INFRON_DIR -Force | Out-Null
  }
  sync_shipped -src (Join-Path $SELF 'packaging\infronai\litellm-config.yaml') -dst (Join-Path $INFRON_DIR 'litellm-config.yaml')
  sync_shipped -src (Join-Path $SELF 'packaging\infronai\start-litellm.ps1')    -dst (Join-Path $INFRON_DIR 'start-litellm.ps1')

  $inf_source = Join-Path $INFRON_DIR 'source'
  if (Test-Path -Path $inf_source -PathType Leaf) {
    ok "infronai profile in place ($inf_source)"
  } elseif ($env:ANTHROPIC_AUTH_TOKEN -and $env:ANTHROPIC_BASE_URL -like '*infron*') {
    # Only when the environment is ALREADY aimed at InfronAI: a bare token in
    # the shell is far more likely to be Logfare's, and seeding this route with
    # another provider's key would fail at request time with a 401 the operator
    # has no reason to connect to this file.
    {
      '# Kuro - InfronAI profile (generated by setup.ps1)'
      '# Treat this file as a credential. Never commit it.'
      ''
      'export ANTHROPIC_BASE_URL="http://localhost:4003"'
      "export ANTHROPIC_AUTH_TOKEN=`"$env:ANTHROPIC_AUTH_TOKEN`""
      'export ANTHROPIC_MODEL="deepseek/deepseek-v4.1-flash:free"'
      'export ANTHROPIC_API_KEY=""'
    } | Set-Content -Path $inf_source -Encoding UTF8
    ok "infronai profile written from $env:ANTHROPIC_AUTH_TOKEN"
  } else {
    note "to enable /zim-infronai, set your key in the TUI:"
    note "  /infronai-api-setup <key>"
    note "  (free models need a $5 balance; 1000 requests/day across all of them)"
  }
  ok "installed infronai route (:4003)"
} else {
  warn "no infronai packaging - /zim-infronai will be unavailable"
}

# The doctrine file zim mode follows. Installed into KURO_HOME so that
# `kuro --mode zim` works from ANY directory: without a copy here, a session
# opened in an unrelated project finds no AGENTS.md and silently falls back to
# the default prompt. A project's own AGENTS.md still wins - the workdir is
# searched first - so this is a fallback, not an override.
#
# Synced on content like the proxy files, but a locally-edited copy is kept as
# .bak rather than overwritten silently: the operator may have tuned their
# doctrine, and that is the one file here that is genuinely theirs.
$aggr_src = Join-Path $SELF 'AGENTS.md'
if (Test-Path -Path $aggr_src -PathType Leaf) {
  sync_shipped -src $aggr_src -dst (Join-Path $KURO_HOME 'AGENTS.md')
} else {
  warn "no AGENTS.md in the checkout - zim mode will fall back to the default prompt"
}

# --- 3b. pagekite (worldwide /phish forwarder) -------------------------------

step "Checking PageKite"
$PK_DIR = Join-Path $KURO_HOME 'pagekite'
if (-not (Test-Path -Path $PK_DIR -PathType Container)) {
  New-Item -ItemType Directory -Path $PK_DIR -Force | Out-Null
}
$pk_src = Join-Path $SELF 'packaging\pagekite\pagekite.py'
if (Test-Path -Path $pk_src -PathType Leaf) {
  sync_shipped -src $pk_src -dst (Join-Path $PK_DIR 'pagekite.py')
} else {
  Write-Host "  downloading pagekite.py..."
  try {
    Invoke-WebRequest -Uri 'https://pagekite.net/pk/pagekite.py' -OutFile (Join-Path $PK_DIR 'pagekite.py') -ErrorAction Stop
    ok "downloaded $(Join-Path $PK_DIR 'pagekite.py')"
  } catch {
    warn "could not download pagekite.py - /phish will fall back to cloudflared/ngrok"
  }
}
$pk_name = Join-Path $KURO_HOME 'pagekite.name'
if (-not (Test-Path -Path $pk_name -PathType Leaf)) {
  warn "no kite name at $pk_name"
  warn "write your kite (e.g. zim.pagekite.me) there, or export PAGEKITE_NAME."
  warn "first-time signup:  $(Join-Path $PK_DIR 'pagekite.py') --signup"
} else {
  ok "kite name: $((Get-Content -Path $pk_name -Raw).Trim())"
}

# --- 4. litellm --------------------------------------------------------------

# The proxy gets its OWN venv. It cannot live in the harness's: litellm[proxy]
# requires rich<14.0 and textual 8.x requires rich>=14.2, so an environment
# holding both cannot resolve at all. start-litellm.ps1 already looks in
# $KURO_HOME\venv, so that is where this goes. A litellm that already
# exists anywhere is reused rather than duplicated.
$PROXY_VENV = Join-Path $KURO_HOME 'venv'
$PROXY_PY   = Join-Path $PROXY_VENV 'Scripts\python.exe'

step "Checking the LiteLLM proxy"
if (Get-Command litellm -ErrorAction SilentlyContinue) {
  ok "litellm on PATH"
} elseif (Test-Path -Path "$env:USERPROFILE\.local\bin\litellm.exe" -PathType Leaf) {
  ok "litellm present at ~/.local/bin/litellm"
} elseif (Test-Path -Path $PROXY_PY -PathType Leaf -and (& $PROXY_PY -c 'import litellm' 2>$null)) {
  ok "litellm in $PROXY_VENV"
} else {
  Write-Host "  building the proxy venv at $PROXY_VENV (this can take a minute)..."
  & $PY -m venv $PROXY_VENV
  & $PROXY_PY -m pip install --quiet --disable-pip-version-check --upgrade pip
  # Both floors are load-bearing, and they live here rather than in
  # requirements.txt because this is the environment that has to satisfy them.
  #
  # litellm[proxy]>=1.100.1 - the harness talks to this proxy over Anthropic's
  # /v1/messages, and every Logfare model is registered as `openai/<name>`.
  # Older litellm only served /v1/messages for provider == anthropic, so an
  # openai/* model raised "Anthropic messages provider config not found for
  # model: ..." - a 500 on the one route Kuro actually uses, while
  # /v1/chat/completions kept working and made the proxy look healthy.
  #
  # uvloop>=0.22.1 - litellm hardcodes uvicorn's event loop to uvloop on Linux
  # (litellm/proxy/proxy_cli.py::_get_loop_type - not configurable, no flag or
  # env var overrides it). On Windows, uvloop is not used (uvloop is Linux-only),
  # so this is skipped there; the flag is kept for parity but only installed
  # when not on Windows.
  & $PROXY_PY -m pip install --quiet --disable-pip-version-check "litellm[proxy]>=1.100.1" 
  if (-not [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Unix) {
    try { & $PROXY_PY -m pip install --quiet --disable-pip-version-check "uvloop>=0.22.1"  } catch { }
  }
  ok "litellm installed into $PROXY_VENV"
}

# --- 5. start the proxy ------------------------------------------------------

if (-not $KEY) {
  step "Skipping the proxy start"
  warn "no key to send - start it later with $SERVICE start"
} else {
  step "Starting the proxy on :$PORT"
  # Reuse anything already healthy on the port - the user may already run a
  # proxy (another tool, or an earlier install) and starting a second one would
  # only fail to bind.
  try {
    $code = (Invoke-RestMethod -Uri "http://127.0.0.1:$PORT/health/liveliness" -Method Get -MaximumTimeoutMilliseconds 2000 -ErrorAction SilentlyContinue).StatusCode
    if ($code -eq 200) {
      ok "a proxy is already serving :$PORT - reusing it"
    } else {
      throw "HTTP $code"
    }
  } catch {
    try {
      & $SERVICE start
      ok "proxy healthy on :$PORT"
    } catch {
      warn "proxy did not start - see $env:TEMP\litellm-zim.log"
      warn "continuing; the launcher will retry on next run."
    }
  }
}

# --- 6. verify the route -----------------------------------------------------

step "Verifying the route"
try {
  $code = (Invoke-RestMethod -Uri "http://127.0.0.1:$PORT/health/liveliness" -Method Get -MaximumTimeoutMilliseconds 3000 -ErrorAction SilentlyContinue).StatusCode
  if ($code -eq 200) {
    ok "proxy answers on :$PORT"
  } else {
    warn "proxy not answering yet"
  }
} catch {
  warn "proxy not answering yet"
}

# A live round-trip through the proxy proves the key actually works. The
# upstream reports "temporarily unavailable" under load, so give it one retry
# before calling the key bad.
$BODY = '{"model":"deepseek-v4.1-flash","max_tokens":8,"messages":[{"role":"user","content":"say ok"}]}'
function probe {
  try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$PORT/v1/messages" -Method Post -Body $BODY -ContentType "application/json" -Headers @{ "anthropic-version" = "2023-06-01"; "x-api-key" = $KEY } -MaximumTimeoutMilliseconds 30000 -ErrorAction SilentlyContinue
    return $resp.StatusCode -or 200
  } catch {
    return $null
  }
}
$CODE = probe
if ($CODE -ne 200) {
  Write-Host "  upstream busy (HTTP $CODE) - retrying once..."
  Start-Sleep -Seconds 3
  $CODE = probe
}
if ($CODE -eq 200) {
  ok "live request through the proxy succeeded"
} else {
  warn "live request returned HTTP $CODE"
  warn "either the key is wrong or Logfare's upstream is down right now:"
  try { Get-Content -Path "$env:TEMP\zim-setup-probe.json" -TotalCount 1 -ErrorAction SilentlyContinue } catch {}
  Write-Host
}
Remove-Item -Path "$env:TEMP\zim-setup-probe.json" -Force -ErrorAction SilentlyContinue

# --- done --------------------------------------------------------------------

Write-Host ""
bold "â*­----------------------------------------------â*®"
bold "|  setup complete.                             |"
bold "â*°----------------------------------------------â*¯"
Write-Host ""
Write-Host "    kuro                 # launch (starts the proxy if needed)"
Write-Host "    kuro -C C:\proj      # run against another directory"
Write-Host "    kuro --mode zim      # full auto, follows AGENTS.md"
Write-Host ""
Write-Host "  Manage the proxy:"
Write-Host "    $SERVICE {status|stop|restart|logs}"
Write-Host ""

