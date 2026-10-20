#!/usr/bin/env bash
# install.sh â€” one-line installer for Kuro.
#
#   curl -fsSL https://raw.githubusercontent.com/Mr-Destroyer/Kuro/main/install.sh | bash
#
# Clones Kuro into ~/.local/share/kuro, builds a virtualenv there and
# installs the dependencies into it, then drops a `kuro` launcher into
# ~/.local/bin. After this the operator never creates, activates or pip-installs
# anything by hand: `kuro` finds its own venv and runs from any directory.
#
# The venv is built here, on this machine, rather than shipped â€” a venv is not
# portable (its interpreter symlink, the console-script shebangs and pyvenv.cfg
# all bake in absolute paths and one exact base interpreter), so the only way
# to hand someone a working one is to build it where it will run. That is what
# this does, and it is the same thing pipx and uv do.
#
# Idempotent: re-run it any time to update an existing install. The launcher it
# writes refreshes the venv itself when requirements.txt changes, so this only
# needs to run once.
#
# Environment overrides (all optional):
#   KURO_REPO          git URL to install from
#                          (default https://github.com/Mr-Destroyer/Kuro.git)
#   KURO_INSTALL_DIR   where the checkout and venv live
#                          (default ${XDG_DATA_HOME:-~/.local/share}/kuro)
#   KURO_BIN_DIR       where the `kuro` launcher is written
#                          (default ~/.local/bin)
#   KURO_SKIP_DEPS=1   lay the files down but do not build the venv
#   KURO_SKIP_SETUP=1  do not run setup.sh at the end
#
# This is meant to be run from a pipe (`curl â€¦ | bash`), where standard input
# is the script itself â€” so nothing here may read stdin. External commands get
# </dev/null. setup.sh does not prompt either: it seeds the profile from the key
# shipped in the checkout, so a fresh install finishes unattended.

set -euo pipefail

REPO="${KURO_REPO:-https://github.com/Mr-Destroyer/Kuro.git}"
INSTALL_DIR="${KURO_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/kuro}"
BIN_DIR="${KURO_BIN_DIR:-$HOME/.local/bin}"
KURO_HOME="${KURO_HOME:-$HOME/.kuro}"
PROFILE="$KURO_HOME/logfare/source"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36mâ–¸\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mâœ”\033[0m %s\n' "$*"; }
warn() { printf '  \033[33mâš \033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mâœ– %s\033[0m\n' "$*" >&2; exit 1; }
note() { printf '      \033[36m%s\033[0m\n' "$*" >&2; }

# Animate while a command runs, then report it exactly like ok() would have.
#
# The command's output is captured and replayed only if it fails, so a spinner
# never interleaves with pip or git mid-line â€” and on failure the real error
# lands right above the âœ– that explains it. When stdout is not a terminal (a
# pipe, a redirect, CI, the test harness) this falls back to plain execution
# and emits no control characters, so the log stays byte-clean.
spin() {
  local msg="$1"; shift
  local rc=0

  if [[ ! -t 1 ]]; then
    "$@" || rc=$?
    if [[ $rc -eq 0 ]]; then ok "$msg"; fi
    return "$rc"
  fi

  if [[ -z "$SPIN_LOG" ]]; then
    SPIN_LOG="$(mktemp "${TMPDIR:-/tmp}/kuro-install.XXXXXX")"
  fi
  : > "$SPIN_LOG"
  "$@" >"$SPIN_LOG" 2>&1 &

  # A braille spinner, plus the elapsed seconds once a step drags â€” the
  # dependency install is a minute or two and wants a heartbeat.
  local -a frames=(â ‹ â ™ â ¹ â ¸ â ¼ â ´ â ¦ â § â ‡ â )
  local pid=$! i=0 start=$SECONDS el
  while kill -0 "$pid" 2>/dev/null; do
    el=$((SECONDS - start))
    printf '\r\033[K  \033[36m%s\033[0m %s' "${frames[i++ % ${#frames[@]}]}" "$msg"
    if (( el > 0 )); then printf ' \033[2m%ds\033[0m' "$el"; fi
    sleep 0.08
  done
  wait "$pid" || rc=$?
  printf '\r\033[K'

  if [[ $rc -eq 0 ]]; then
    ok "$msg"
  elif [[ -s "$SPIN_LOG" ]]; then
    cat "$SPIN_LOG" >&2
  fi
  return "$rc"
}

# The spinner's scratch file, created on first use and removed on any exit.
SPIN_LOG=""
trap 'if [[ -n "$SPIN_LOG" ]]; then rm -f "$SPIN_LOG"; fi' EXIT

# The hash the launcher also uses to decide whether the venv is stale. Same
# algorithm in both places, so the stamp written here is honoured there.
hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    return 1
  fi
}

bold "â•­â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â•®"
bold "â”‚  KURO Â· install                        â”‚"
bold "â•°â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â•¯"

# --- 0. preconditions --------------------------------------------------------

step "Checking prerequisites"
command -v git >/dev/null 2>&1 || die "git is required."

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "python3 not found â€” install Python 3.11 or newer."
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11+ is required; $PY is $("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')."
ok "git and $("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"

# --- 1. get the source -------------------------------------------------------

step "Fetching Kuro"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  # An existing install. Fast-forward only â€” never merge, never touch local
  # edits; a failed pull is a warning, not a stop, because the launcher's own
  # update check will report the same thing at run time.
  spin "updated $INSTALL_DIR" \
    git -C "$INSTALL_DIR" pull --ff-only --quiet </dev/null \
    || warn "could not update $INSTALL_DIR (local changes or offline) â€” using what is there"
elif [[ -e "$INSTALL_DIR" ]]; then
  die "$INSTALL_DIR exists but is not a git checkout â€” move it aside and re-run."
else
  mkdir -p "$(dirname -- "$INSTALL_DIR")"
  # A full clone, not --depth 1: the launcher's update check counts how far
  # behind the checkout is and fast-forwards it, and both want real history.
  # The repository is a couple of MB, so there is nothing to save by shallow.
  spin "cloned into $INSTALL_DIR" \
    git clone --quiet "$REPO" "$INSTALL_DIR" </dev/null \
    || die "could not clone $REPO"
fi

# --- 2. build the venv -------------------------------------------------------

VENV_PY="$INSTALL_DIR/.venv/bin/python"

if [[ "${KURO_SKIP_DEPS:-0}" == "1" ]]; then
  warn "KURO_SKIP_DEPS=1 â€” skipping the venv (the launcher will not run yet)"
else
  step "Building the virtualenv"
  if [[ -x "$VENV_PY" ]]; then
    ok "venv already present"
  else
    spin "venv at $INSTALL_DIR/.venv" \
      "$PY" -m venv "$INSTALL_DIR/.venv" </dev/null \
      || die "could not create the venv"
  fi

  step "Installing dependencies (a minute or two the first time)"
  # One spinner covers the whole step â€” the pip self-upgrade and then
  # requirements.txt â€” because together they are the wait worth animating.
  _install_deps() {
    "$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip </dev/null || true
    # Run from the checkout: requirements.txt ends in `-e .`, and pip resolves
    # that against the current directory rather than the requirements file.
    # Under `curl | bash` the cwd is wherever the operator happened to be.
    ( cd "$INSTALL_DIR" && \
      "$VENV_PY" -m pip install --quiet --disable-pip-version-check \
        -r "$INSTALL_DIR/requirements.txt" </dev/null )
  }
  spin "dependencies installed" _install_deps \
    || die "could not install the dependencies â€” see the pip output above."
  # Stamp the requirements hash so the launcher's incremental refresh sees a
  # matching venv and does nothing on the first launch.
  if _h="$(hash_file "$INSTALL_DIR/requirements.txt")"; then
    printf '%s' "$_h" > "$INSTALL_DIR/.venv/.requirements.sha"
  fi
fi

# --- 3. the launcher on PATH -------------------------------------------------

step "Installing the launcher"
mkdir -p "$BIN_DIR"
# A tiny shim, not the launcher itself: it pins KURO_ROOT to the checkout
# so the real launcher (which lives in packaging/) finds the venv and the
# profile no matter where `kuro` is invoked from. Written fresh each run so
# a moved install re-points itself.
{
  printf '#!/usr/bin/env bash\n'
  printf '# kuro â€” installed launcher (written by install.sh).\n'
  printf '# Pins the checkout and hands every argument to the real launcher.\n'
  printf 'export KURO_ROOT=%q\n' "$INSTALL_DIR"
  printf 'exec %q "$@"\n' "$INSTALL_DIR/packaging/kuro"
} > "$BIN_DIR/kuro"
chmod 755 "$BIN_DIR/kuro"
ok "$BIN_DIR/kuro"

case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
  *)
    warn "$BIN_DIR is not on your PATH â€” add it, then open a new shell:"
    note "echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc"
    ;;
esac

# --- 4. the credential -------------------------------------------------------

if [[ "${KURO_SKIP_SETUP:-0}" == "1" ]]; then
  warn "KURO_SKIP_SETUP=1 â€” not configuring the route"
elif [[ -r "$PROFILE" ]]; then
  step "Route already configured"
  ok "$PROFILE exists"
else
  # setup.sh does not prompt: it seeds the profile from the key shipped in the
  # checkout, so a fresh install needs no signup and no terminal. stdin is
  # still pointed at /dev/null rather than left as this script's own, so a
  # future read in setup.sh cannot eat the rest of a `curl | bash` pipeline.
  #
  # A setup.sh that fails must not fail the install: a proxy that will not
  # start, an upstream that is down, or a machine with no network all leave a
  # working checkout, and the launcher retries on launch. Hence `|| warn`
  # rather than a bare call under `set -e`.
  step "Configuring the Logfare route"
  "$INSTALL_DIR/setup.sh" </dev/null \
    || warn "setup did not finish â€” run $INSTALL_DIR/setup.sh to retry"
fi

# --- done --------------------------------------------------------------------

printf '\n'
bold "â•­â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â•®"
bold "â”‚  installed.                                  â”‚"
bold "â•°â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â•¯"
printf '\n'
printf '    \033[36mkuro\033[0m                 # launch, from anywhere\n'
printf '    \033[36mkuro -C ~/proj\033[0m       # run against another directory\n'
printf '    \033[36mkuro --mode zim\033[0m      # full auto, follows AGENTS.md\n\n'
printf '  Re-run this installer to update:  %s/install.sh\n' "$INSTALL_DIR"
printf '  It keeps its own venv, so nothing to activate and nothing to pip-install.\n\n'

