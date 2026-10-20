"""Regression suite for upstream source discovery and proxy start.

Run:  python tests/test_sources.py   (from an activated venv)

Protects the /zim-tokenjuice switch: the installed ~/.kuro/tokenjuice
route must win over a leftover $HOME/start-litellm.sh from an older
deepseek-claude layout, and start_proxy must not wrap the service in flock.
A leaked flock on /tmp/kuro-proxy.lock is what made the in-TUI switch
hang until timeout while a manual start of the same script finished in
seconds.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kuro import sources  # noqa: E402
# Imported for the ``switchable`` suite: ``sources`` reaches for AntSeed inside
# the function rather than at module level (to avoid a circular import), so the
# module has to be patched where it actually lives.
from kuro import antseed  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _touch(path: Path, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_tokenjuice_prefers_installed_route() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "kuro"
        # Both layouts exist â€” the leftover $HOME copy must lose.
        _touch(home / "start-litellm.sh", 0o755)
        _touch(home / "litellm-config.yaml")
        _touch(home / "claude-source" / "deepseek-claude")
        installed_svc = _touch(zhome / "tokenjuice" / "start-litellm.sh", 0o755)
        installed_cfg = _touch(zhome / "tokenjuice" / "litellm-config.yaml")
        installed_src = _touch(zhome / "tokenjuice" / "source")

        with mock.patch.object(sources, "DEFAULT_HOME", home), \
             mock.patch.object(sources, "KURO_HOME", zhome):
            found = sources.discover()

        tj = found.get("tokenjuice")
        check("tokenjuice is discovered when both layouts exist", tj is not None)
        if tj is None:
            return
        check("tokenjuice service is the installed copy",
              tj.service == installed_svc, str(tj.service))
        check("tokenjuice config is the installed copy",
              tj.config == installed_cfg, str(tj.config))
        check("tokenjuice profile is the installed copy",
              tj.profile == installed_src, str(tj.profile))


def test_tokenjuice_falls_back_to_home_layout() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "kuro"
        zhome.mkdir()
        home_svc = _touch(home / "start-litellm.sh", 0o755)
        home_cfg = _touch(home / "litellm-config.yaml")
        home_src = _touch(home / "claude-source" / "deepseek-claude")

        with mock.patch.object(sources, "DEFAULT_HOME", home), \
             mock.patch.object(sources, "KURO_HOME", zhome):
            found = sources.discover()

        tj = found.get("tokenjuice")
        check("tokenjuice falls back to $HOME when not installed", tj is not None)
        if tj is None:
            return
        check("fallback service is $HOME/start-litellm.sh",
              tj.service == home_svc, str(tj.service))
        check("fallback config is $HOME/litellm-config.yaml",
              tj.config == home_cfg, str(tj.config))
        check("fallback profile is $HOME/claude-source/deepseek-claude",
              tj.profile == home_src, str(tj.profile))


def test_start_proxy_does_not_flock() -> None:
    with tempfile.TemporaryDirectory() as td:
        svc = _touch(Path(td) / "start-litellm.sh", 0o755)
        src = sources.Source(
            key="tokenjuice",
            label="Token Juice",
            port=3999,
            profile=Path(td) / "source",
            service=svc,
            config=Path(td) / "config.yaml",
            model="deepseek-v4.1-flash",
            root=Path(td),
        )
        captured: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)

            class _P:
                returncode = 0
                stdout = ""
                stderr = "boom"

            return _P()

        with mock.patch.object(sources, "proxy_healthy", return_value=False), \
             mock.patch.object(sources.subprocess, "run", side_effect=fake_run):
            ok, detail = sources.start_proxy(src, timeout=5)

        argv = captured.get("argv") or []
        check("start_proxy invokes the service directly",
              argv[:2] == [str(svc), "start"], str(argv))
        check("start_proxy does not wrap the start in flock",
              "flock" not in argv, str(argv))
        check("start_proxy reports the service failure, not a hang",
              ok is False and detail == "boom", f"{ok!r} {detail!r}")


def test_remote_gateways_are_identified_by_host() -> None:
    """The hosted gateways have no port to match on.

    Logfare and Token Juice are local proxies, found by the port in their URL.
    TokenHarbour and OpenRouter are remote, so ``active_key`` has to recognise
    them by host instead â€” and it must check them *before* the port regex, or a
    hosted URL falls through to ``None`` and the session is reported as pointing
    at nothing.
    """
    check("host: tokenharbour is recognised",
          sources.active_key("https://tokenharbor.ai") == "tokenharbour")
    check("host: openrouter is recognised",
          sources.active_key("https://openrouter.ai/api/v1") == "openrouter")

    # The two must not be confused for one another, whatever order the checks
    # happen to run in.
    keys = {
        sources.active_key("https://tokenharbor.ai"),
        sources.active_key("https://openrouter.ai/api/v1"),
    }
    check("host: the two gateways are distinct", len(keys) == 2, str(sorted(keys)))

    check("host: an unknown host is still unknown",
          sources.active_key("https://api.example.com") is None)
    check("host: an empty endpoint is still unknown",
          sources.active_key("") is None)


def test_builtin_keys_survive_a_restart() -> None:
    """A remote selection must not be discarded for having nothing on disk.

    ``current_key`` validates a *discovered* source by checking its files are
    present. The remote gateways have no profile, service or config to discover
    â€” so gating them the same way would silently drop the selection and put the
    next launch back on Logfare.
    """
    check("builtin: both remote gateways are listed",
          {"tokenharbour", "openrouter"} <= sources.BUILTIN_KEYS,
          str(sorted(sources.BUILTIN_KEYS)))

    with tempfile.TemporaryDirectory() as td:
        zhome = Path(td) / "kuro"
        zhome.mkdir()
        # BOTH homes are pinned to the empty temp tree. Patching only
        # KURO_HOME would leave the real $HOME in play, and a box with the
        # legacy $HOME/start-litellm.sh layout would legitimately discover
        # tokenjuice â€” making the "absent source is discarded" check below fail
        # for a reason that has nothing to do with the code under test.
        empty_home = Path(td) / "home"
        empty_home.mkdir()
        with mock.patch.object(sources, "KURO_HOME", zhome), \
             mock.patch.object(sources, "DEFAULT_HOME", empty_home):
            for key in ("tokenharbour", "openrouter"):
                sources.set_current(key)
                check(f"builtin: {key} survives a restart",
                      sources.current_key() == key, str(sources.current_key()))

            # A discovered source that is NOT present must still be dropped â€”
            # the builtin exemption must not have loosened that.
            sources.set_current("tokenjuice")
            check("builtin: an absent local source is still discarded",
                  sources.current_key() is None, str(sources.current_key()))


def test_models_for_reads_the_remote_catalogs() -> None:
    """models_for returns None on a cold cache, never an empty tuple.

    ``None`` means "no live catalog, use your fallback"; ``()`` would mean
    "this source serves nothing", and the two must not be confused or /model
    would show an empty list on a cold start instead of the declared one.
    """
    from kuro import openrouter as or_mod
    from kuro import tokenharbour as th_mod

    for key, mod, url in (
        ("tokenharbour", th_mod, "https://tokenharbor.ai"),
        ("openrouter", or_mod, "https://openrouter.ai/api/v1"),
    ):
        mod.clear_cache()
        check(f"models_for: {key} yields None on a cold cache",
              sources.models_for(url) is None, repr(sources.models_for(url)))

        with mock.patch.object(mod, "cached_ids", return_value=["m-one", "m-two"]):
            check(f"models_for: {key} reads its module's cache",
                  sources.models_for(url) == ("m-one", "m-two"),
                  repr(sources.models_for(url)))

    check("models_for: an unknown source is still None",
          sources.models_for("https://api.example.com") is None)


def test_logfare_is_a_switchable_route() -> None:
    """Logfare is a full route: switchable to, remembered, and shipped.

    It used to be parked while the shipped key looked like a starting point
    only. That made ``/zim-logfare`` refuse the one thing it exists to do. The
    shared key still limits *how much* traffic it can carry, but that is a 429
    the turn path reports, not a reason to hide the route.
    """
    check("logfare: it is not parked",
          "logfare" not in sources.UNDER_CONSTRUCTION,
          str(sorted(sources.UNDER_CONSTRUCTION)))

    # A selection of it must survive a restart, like any other discovered
    # source. This is the exact case the old park refused.
    with tempfile.TemporaryDirectory() as td:
        zhome = Path(td) / "kuro"
        zhome.mkdir()
        empty_home = Path(td) / "home"
        empty_home.mkdir()
        with mock.patch.object(sources, "KURO_HOME", zhome), \
             mock.patch.object(sources, "DEFAULT_HOME", empty_home):
            sources.set_current("logfare")
            check("logfare: a remembered selection is honoured",
                  sources.current_key() == "logfare", str(sources.current_key()))

            # Token Juice is still parked, and must still be dropped.
            sources.set_current("tokenjuice")
            check("park: tokenjuice is still refused",
                  sources.current_key() is None, str(sources.current_key()))

    # The shipped profile is what makes the route work out of the box, so it
    # has to be in the tree â€” not gitignored, and carrying a usable key.
    shipped = ROOT / "packaging" / "logfare" / "source"
    check("logfare: the shared profile ships in the tree",
          shipped.is_file(), str(shipped))
    if shipped.is_file():
        text = shipped.read_text()
        check("logfare: the shipped profile carries an lfu_ token",
              'ANTHROPIC_AUTH_TOKEN="lfu_' in text)
        check("logfare: the shipped profile points at the local proxy",
              'ANTHROPIC_BASE_URL="http://localhost:4001"' in text)


def test_logfare_offers_every_chat_model() -> None:
    """The fallback list and the proxy config must both cover the whole catalog.

    ``/model`` reads the fetched catalog first, so the declared list is what a
    cold cache shows. Leaving a served model out of it hid that model for the
    first seconds of every session. The proxy config is the other half: a name
    it does not register is rejected before the upstream is ever asked, so a
    model missing there cannot be used at all â€” the wildcard is what keeps that
    from happening for a model Logfare adds later.
    """
    from kuro.config import KNOWN_MODELS

    # The nine chat models Logfare served on 2026-10-19. The non-chat entries
    # (flux-*, whisper-*, aura-*, nova-*, phoenix-*, lucid-origin, sdxl-*) must
    # stay out: they reject a Messages turn.
    served = {
        "claude-haiku-5-5", "deepseek-v4-pro-0813", "deepseek-v4.1-flash",
        "gemma-4-26b", "glm-5.3", "glm-5.3-flash", "kimi-k2.6",
        "logfare/auto", "qwen-3.8-27b",
    }
    check("catalog: the fallback lists every chat model",
          served <= set(sources.LOGFARE_MODELS),
          f"missing {sorted(served - set(sources.LOGFARE_MODELS))}")
    check("catalog: KNOWN_MODELS agrees with the fallback",
          served <= set(KNOWN_MODELS),
          f"missing {sorted(served - set(KNOWN_MODELS))}")
    for non_chat in ("whisper-large-v3-turbo", "flux-2-dev", "aura-2-en",
                     "nova-3", "phoenix-1.0", "lucid-origin", "sdxl-lightning"):
        check(f"catalog: {non_chat} (non-chat) is not offered",
              non_chat not in sources.LOGFARE_MODELS)

    # The proxy config is the real ceiling. Every named chat model must be
    # registered, and the wildcard must be present so a future one routes.
    config = (ROOT / "packaging" / "logfare" / "litellm-config.yaml").read_text()
    for model in served:
        check(f"proxy: {model} is registered", f"model_name: {model}" in config)
    check("proxy: a wildcard route exists so new models need no release",
          'model_name: "*"' in config, "no wildcard in litellm-config.yaml")


def test_switchable_names_only_what_would_work() -> None:
    """``switchable`` answers "where can this session go next?" honestly.

    It exists because the 429 message used to hardcode four hosted gateways.
    On a box with none of them configured, that sentence moved the operator
    from one error to the next; on this box it never mentioned the local
    proxies, which were the sources actually set up. The caller is a *failure*
    message, so a wrong answer is worse than a short one.

    Fully stubbed: ``discover`` and the gateway modules are replaced, so this
    asserts the policy without reading the real ~/.kuro or the network.
    """
    class _Cred:
        """Stands in for a gateway module; ``key`` is what it holds, if any."""

        def __init__(self, key: str | None):
            self.key = key

        def load_credentials(self):
            return None if self.key is None else (self.key, "m", "test")

    def run(found: dict, creds: dict, exclude: str | None = None) -> list[str]:
        """``switchable`` minus logfare, which is asserted separately.

        Logfare ships a profile, so it is offered on every machine and would
        otherwise appear in every expectation below. Dropping it here keeps
        each check about the rule it names.
        """
        def fake_module(name: str):
            return _Cred(creds.get(name))
        with mock.patch.object(sources, "discover", lambda: found), \
             mock.patch.object(sources, "_gateway_module", fake_module), \
             mock.patch.object(antseed, "reachable", lambda: False):
            return [k for k in sources.switchable(exclude=exclude)
                    if k != "logfare"]

    # Nothing configured anywhere: the message must not invent a source. The
    # empty list is what makes _friendly_error fall back to /zim-source.
    check("switchable: an empty machine offers nothing",
          run({}, {}) == [], str(run({}, {})))

    # A gateway with no key is not offered â€” this is the bug the function was
    # written for.
    check("switchable: a gateway with no credential is omitted",
          run({}, {"openrouter": None}) == [], str(run({}, {"openrouter": None})))
    check("switchable: a gateway with a credential is offered",
          run({}, {"openrouter": "sk-x"}) == ["openrouter"],
          str(run({}, {"openrouter": "sk-x"})))

    # The source that just refused must never be recommended again.
    check("switchable: exclude drops the failing source",
          run({}, {"openrouter": "sk-x", "unorouter": "u"}, exclude="openrouter")
          == ["unorouter"],
          str(run({}, {"openrouter": "sk-x", "unorouter": "u"},
                  exclude="openrouter")))

    # A discovered local proxy is offered without asking any module: its
    # files are the proof it can be started.
    fake = sources.Source(
        key="infronai", label="InfronAI", port=4003,
        profile=Path("/nope"), service=Path("/nope"), config=Path("/nope"),
        model="m", root=Path("/nope"), models=(), under_construction=None)
    check("switchable: a discovered local proxy is offered",
          "infronai" in run({"infronai": fake}, {}),
          str(run({"infronai": fake}, {})))

    # Parked sources are never offered, discovered or not â€” /zim-tokenjuice
    # refuses them, so naming it would send the operator into a refusal.
    # Parked *openrouter* here rather than using tokenjuice: tokenjuice is
    # already absent from the preference list, so offering it proves nothing
    # about the guard. This parks a key the loop does reach.
    with mock.patch.dict(sources.UNDER_CONSTRUCTION, {"openrouter": "test"}):
        check("switchable: a parked source is never offered",
              "openrouter" not in run({}, {"openrouter": "sk-x"}),
              str(run({}, {"openrouter": "sk-x"})))

    # A source whose module raises is dropped, not propagated: this runs
    # inside an error handler, and a raise here would replace the real 429
    # with a traceback about the hint.
    class _Boom:
        def load_credentials(self):
            raise RuntimeError("unreadable profile")

    with mock.patch.object(sources, "discover", lambda: {}), \
         mock.patch.object(sources, "_gateway_module", lambda n: _Boom()):
        try:
            got = sources.switchable()
            ok = "openrouter" not in got
        except Exception as exc:  # noqa: BLE001
            ok, got = False, f"raised {exc!r}"
    check("switchable: a gateway that raises is dropped", ok, str(got))

    # Logfare always answers: its profile ships, so it is a real option on a
    # fresh clone even when nothing else is set up. Asserted on the raw call,
    # since ``run`` deliberately strips it.
    with mock.patch.object(sources, "discover", lambda: {}), \
         mock.patch.object(sources, "_gateway_module", lambda n: _Cred(None)), \
         mock.patch.object(antseed, "reachable", lambda: False):
        raw = sources.switchable()
    check("switchable: logfare is offered on an empty machine",
          raw == ["logfare"], str(raw))


def main() -> int:
    test_tokenjuice_prefers_installed_route()
    test_tokenjuice_falls_back_to_home_layout()
    test_start_proxy_does_not_flock()
    test_remote_gateways_are_identified_by_host()
    test_builtin_keys_survive_a_restart()
    test_models_for_reads_the_remote_catalogs()
    test_logfare_is_a_switchable_route()
    test_logfare_offers_every_chat_model()
    test_switchable_names_only_what_would_work()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

