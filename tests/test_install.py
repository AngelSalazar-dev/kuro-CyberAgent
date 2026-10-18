"""Regression suite for the one-line installer and the launcher it writes.

Run:  python tests/test_install.py

Hermetic: no network, no real dependencies. It builds a throwaway git repo to
stand in for the upstream, runs install.sh against it with the dependency and
credential steps switched off, and checks the install layout it produces — the
checkout, the launcher shim, and the interpreter the launcher then chooses.

What is being protected is the promise install.sh makes: `zimzilla` on PATH,
running the checkout's own venv, from any directory, with no activation. The
parts that break silently are the two that decide *which* interpreter runs — the
shim's ZIMZILLA_ROOT and the launcher's root derivation — so those are asserted
directly, by reading the path the launcher reports in its failure message, not
merely by exit code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))


def run(argv: list[str], *, env: dict, cwd: Path | str, stdin: str | None = None):
    """Run a command, returning (returncode, stdout, stderr). Never raises."""
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def base_env(home: Path) -> dict:
    """A clean environment: a temp HOME, no inherited venv, real PATH.

    XDG_DATA_HOME is pinned into the temp HOME too. install.sh honours it, so an
    operator who exports it (common on Arch) would otherwise send the test's
    clone into their real ~/.local/share — the test must not depend on the
    ambient environment to stay inside its sandbox.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    for var in ("VIRTUAL_ENV", "ZIMZILLA_ROOT", "ZIMZILLA_INSTALL_DIR",
                "ZIMZILLA_BIN_DIR", "ZIMZILLA_SKIP_DEPS", "ZIMZILLA_SKIP_SETUP"):
        env.pop(var, None)
    return env


def make_source(td: Path) -> Path:
    """A minimal git repo to clone: the launcher, requirements, a setup stub."""
    src = td / "src"
    (src / "packaging").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "zimzilla", src / "packaging" / "zimzilla")
    os.chmod(src / "packaging" / "zimzilla", 0o755)
    # Empty (comment-only) so the launcher's dependency sync, if it ever runs,
    # is a no-op rather than a network fetch.
    (src / "requirements.txt").write_text("# no deps in this test\n")
    (src / "setup.sh").write_text("#!/usr/bin/env bash\necho setup-stub\n")
    os.chmod(src / "setup.sh", 0o755)

    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(["git", *ident, "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", *ident, "-C", str(src), "commit", "-qm", "seed"], check=True)
    return src


# ---------------------------------------------------------------------------
# install.sh — the layout it lays down
# ---------------------------------------------------------------------------

def test_install_layout(td: Path) -> dict:
    home = td / "home"
    home.mkdir()
    src = make_source(td)
    env = base_env(home)
    env["ZIMZILLA_REPO"] = f"file://{src}"
    env["ZIMZILLA_SKIP_DEPS"] = "1"
    env["ZIMZILLA_SKIP_SETUP"] = "1"

    install_dir = home / ".local" / "share" / "zimzilla"
    bin_dir = home / ".local" / "bin"

    # Feed the script on stdin, exactly as `curl … | bash` would, to prove
    # nothing in it reads standard input.
    rc, out, err = run(["bash"], env=env, cwd=ROOT,
                       stdin=(ROOT / "install.sh").read_text())
    check("install: exits 0", rc == 0, err.strip()[-400:])
    check("install: clones the checkout", (install_dir / ".git").is_dir(),
          str(install_dir))
    check("install: lays the launcher on PATH",
          (bin_dir / "zimzilla").is_file(), str(bin_dir / "zimzilla"))
    check("install: the launcher is executable",
          os.access(bin_dir / "zimzilla", os.X_OK))

    shim = (bin_dir / "zimzilla").read_text()
    check("install: the shim pins ZIMZILLA_ROOT to the checkout",
          f'ZIMZILLA_ROOT="{install_dir}"' in shim
          or f"ZIMZILLA_ROOT={install_dir}" in shim, shim.strip())
    check("install: the shim execs the real launcher",
          f"{install_dir}/packaging/zimzilla" in shim, shim.strip())

    return {"home": home, "install_dir": install_dir, "bin_dir": bin_dir, "src": src}


# ---------------------------------------------------------------------------
# The launcher — which interpreter it picks
# ---------------------------------------------------------------------------

def fake_bundled_venv(install_dir: Path) -> Path:
    """A stand-in venv: a python that exists, plus a stamp so sync_deps no-ops.

    The python is the real system interpreter, so the dependency import check
    fails as it would on a fresh machine — which is the point: the failure
    message names the interpreter that was chosen.
    """
    venv_bin = install_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    real = shutil.which("python3")
    assert real is not None
    os.symlink(real, venv_bin / "python")

    req = install_dir / "requirements.txt"
    proc = subprocess.run(["sha256sum", str(req)], capture_output=True, text=True)
    if proc.returncode == 0:
        (install_dir / ".venv" / ".requirements.sha").write_text(
            proc.stdout.split()[0])
    return venv_bin / "python"


def test_launcher_prefers_bundled_venv(ctx: dict) -> None:
    install_dir, bin_dir = ctx["install_dir"], ctx["bin_dir"]
    launcher = install_dir / "packaging" / "zimzilla"
    if not launcher.exists():
        # A failed install should read as a failed check, not a traceback.
        check("launcher: the checkout was installed", False, str(launcher))
        return

    venv_py = fake_bundled_venv(install_dir)
    env = base_env(ctx["home"])

    # Through the installed shim — the way a user runs it.
    rc, out, err = run([str(bin_dir / "zimzilla"), "--version"], env=env, cwd=ctx["home"])
    check("launcher: the shim resolves the checkout's venv",
          str(venv_py) in err, err.strip()[-300:])
    check("launcher: fails closed on missing deps (exit 2)", rc == 2, f"rc={rc}")

    # The launcher directly, with no ZIMZILLA_ROOT and from an unrelated cwd —
    # it must derive the checkout as the parent of packaging/ on its own.
    rc, out, err = run([str(launcher), "--version"], env=env, cwd="/")
    check("launcher: derives the checkout from its own location",
          str(venv_py) in err, err.strip()[-300:])


def test_launcher_falls_back_to_path_python(td: Path) -> None:
    # A checkout with no bundled venv: the bring-your-own-venv layout. With no
    # VIRTUAL_ENV either, it must fall back to python3 on PATH.
    root = td / "byo"
    (root / "packaging").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "zimzilla", root / "packaging" / "zimzilla")
    os.chmod(root / "packaging" / "zimzilla", 0o755)

    home = td / "home2"
    home.mkdir()
    env = base_env(home)
    rc, out, err = run([str(root / "packaging" / "zimzilla"), "--version"], env=env, cwd="/")
    path_py = shutil.which("python3")
    check("launcher: falls back to python3 on PATH",
          path_py is not None and path_py in err, err.strip()[-300:])


def test_setup_seeds_a_key_without_prompting(td: Path) -> None:
    """A fresh install must not stop to ask for a Logfare key.

    The install path is `curl … | bash` and then `zimzilla`. A prompt for a
    third-party signup in the middle of that is the friction the shipped key
    exists to remove, so setup.sh has to find a credential on its own — from the
    profile in the checkout — and finish with no terminal attached.

    Run in a throwaway HOME with stdin closed. Every network step is stubbed
    (the proxy is never started, nothing is fetched): what is asserted is the
    credential decision, not a route.
    """
    home = td / "home-setup"
    home.mkdir()
    zhome = home / ".zimzilla"
    checkout = td / "checkout"
    (checkout / "packaging" / "logfare").mkdir(parents=True)
    # The shipped profile, as it lands in a clone. A recognisable shape rather
    # than the real key: this test must not depend on a live credential.
    (checkout / "packaging" / "logfare" / "source").write_text(
        'export ANTHROPIC_BASE_URL="http://localhost:4001"\n'
        'export ANTHROPIC_AUTH_TOKEN="lfu_test_only_not_a_real_key"\n'
        'export ANTHROPIC_MODEL="deepseek-v4.1-flash"\n'
    )
    shutil.copy(ROOT / "setup.sh", checkout / "setup.sh")
    os.chmod(checkout / "setup.sh", 0o755)
    # setup.sh refuses to configure a proxy for an interpreter that cannot run
    # the harness, so the checkout needs a venv whose python really has the
    # dependencies. A shim that execs the interpreter running this test does
    # that without building a venv or touching the network. A symlink will not
    # do: a bare symlink has no pyvenv.cfg, so sys.prefix stays /usr and the
    # dependencies are not on the path.
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    shim = venv_bin / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    os.chmod(shim, 0o755)

    env = base_env(home)
    env["ZIMZILLA_HOME"] = str(zhome)
    env["ZIMZILLA_ROOT"] = str(checkout)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    # Hermetic: no network, and nothing slow. `litellm` on PATH stops setup.sh
    # from building a proxy venv (a pip install), and a `curl` that always
    # fails stops the pagekite download and makes the health probes read as
    # "nothing answering" — both are warnings, so the script still exits 0.
    stubs = td / "stubs"
    stubs.mkdir()
    (stubs / "litellm").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "curl").write_text("#!/bin/sh\nexit 1\n")
    for stub in stubs.iterdir():
        os.chmod(stub, 0o755)
    env["PATH"] = f"{stubs}:/usr/bin:/bin"

    proc = subprocess.run(
        ["bash", str(checkout / "setup.sh")],
        cwd=str(checkout), env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
    )
    combined = proc.stdout + proc.stderr
    check("setup: finishes with no terminal and no prompt", proc.returncode == 0,
          combined.strip()[-400:])
    check("setup: never asks for a key",
          "Paste your" not in combined and "key:" not in combined,
          combined.strip()[-400:])

    profile = zhome / "logfare" / "source"
    check("setup: writes the profile from the shipped key",
          profile.is_file()
          and "lfu_test_only_not_a_real_key" in profile.read_text(),
          str(profile))
    check("setup: the profile is mode 600",
          profile.is_file() and (profile.stat().st_mode & 0o777) == 0o600,
          oct(profile.stat().st_mode & 0o777) if profile.is_file() else "missing")

    # The shipped key is a fallback, not an override: a personal credential in
    # the environment must still win.
    personal = td / "home-setup2"
    personal.mkdir()
    env2 = base_env(personal)
    env2["ZIMZILLA_HOME"] = str(personal / ".zimzilla")
    env2["ZIMZILLA_ROOT"] = str(checkout)
    env2["ANTHROPIC_AUTH_TOKEN"] = "lfu_personal_key_should_win"
    env2["PATH"] = env["PATH"]
    subprocess.run(["bash", str(checkout / "setup.sh")],
                   cwd=str(checkout), env=env2,
                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=120)
    written = (personal / ".zimzilla" / "logfare" / "source")
    check("setup: a personal key in the environment beats the shipped one",
          written.is_file() and "lfu_personal_key_should_win" in written.read_text(),
          written.read_text()[:120] if written.is_file() else "missing")


def main() -> int:
    with tempfile.TemporaryDirectory() as td_str:
        td = Path(td_str)
        ctx = test_install_layout(td)
        test_launcher_prefers_bundled_venv(ctx)
        test_launcher_falls_back_to_path_python(td)
        test_setup_seeds_a_key_without_prompting(td)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for name, ok, extra in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if not ok and extra else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
