"""Upstream "sources" — the LiteLLM proxies ZimZilla can talk through.

ZimZilla speaks the Anthropic Messages protocol to whatever ``ANTHROPIC_BASE_URL``
points at. Three local LiteLLM proxies front three different upstreams:

    logfare       :4001  →  logfare.ai         (the default, shipped with the repo)
    tokenjuice    :4000  →  api.tokenjuice.ai  (deepseek-claude's endpoint)
    iteracompute  :4002  →  api.iteracompute.com (OpenAI-only; free Qwen tier)

Logfare is a full route, not just the starting point: the shipped profile makes
a fresh clone run with no signup, and ``/zim-logfare`` switches back to it at
any time. Its key is shared by every install, so heavy use can exhaust the
upstream's daily request budget — that arrives as a 429 with a ``Retry-After``,
which the harness reports rather than retrying.

IteraCompute is OpenAI-compatible only, so its proxy is not optional: the
upstream has no Messages endpoint at all. Its free tier is 30M tokens per UTC
calendar month on one model, and ``/iteracompute-api-setup`` stores the key.

Token Juice stays parked (see UNDER_CONSTRUCTION) because its upstream keeps
renaming its model id.

The other upstreams (TokenHarbour, OpenRouter, UnoRouter, AIHubMix, BeatAPI,
AntSeed) are remote or operator-started, and live in their own modules; this one
is the local proxy plumbing and the selection record
(``~/.zimzilla/current-source``).

Nothing here echoes a key. Credentials live in each source's profile file, mode
600, except Logfare's shared one in ``packaging/logfare/source``.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
DEFAULT_HOME = Path.home()
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Source:
    """One upstream: where to reach it, how to start it, what to call the model."""

    key: str            # "logfare" | "tokenjuice" | "iteracompute"
    label: str          # human name, for the transcript
    port: int
    profile: Path       # shell profile to source (holds the key; mode 600)
    service: Path       # start-litellm.sh-compatible manager
    config: Path        # the litellm yaml
    model: str          # default model name for this upstream
    root: Path          # checkout the service manager is started from
    models: tuple[str, ...] = ()   # declared fallback list; the live one is fetched
    #: Non-None while the route is deliberately not usable — the reason to print.
    #: See UNDER_CONSTRUCTION.
    under_construction: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"


# Sources that are installed, still discovered, and still listed by
# `/zim-source` — but deliberately not switchable right now. Key -> the reason
# to print.
#
# Parked rather than removed so the route stays *visible*: an operator who
# cannot see a source cannot tell "not installed on this machine" from
# "installed and deliberately off", and those call for different actions. It is
# also what makes bringing it back a one-line change in here plus the UI, with
# no second copy of the decision to find.
#
# A parked source is never marked active, never started by a switch, and a
# remembered selection of it is refused by `current_key` — so a stale
# `current-source` cannot drag a session back onto it.
#
# Logfare is deliberately NOT parked. It was, while the shipped key looked like
# a starting point only; but the key is the project's own published credential,
# every install is meant to run on it, and `/zim-logfare` has to be a route the
# operator can come back to. What the shared key cannot do is carry unlimited
# traffic — Logfare's free budget is per account, and exhausting it answers 429
# with a Retry-After. That is a runtime condition to report, not a reason to
# hide the route.
UNDER_CONSTRUCTION: dict[str, str] = {
    "tokenjuice": (
        "the upstream keeps renaming its model id, and the last rename took "
        "the alias with it"
    ),
}


# The *fallback* list for each upstream — what to show when the live catalog
# has not been fetched (no network, or /model opened before the startup warm-up
# finished). The real list comes from the upstream's own status API; see
# models_for().
#
# This is a snapshot, so it rots: it was fourteen models, ten of which Logfare
# retired upstream, and /model kept offering them long after every request for
# them 404'd. Treat it as a starting point that /logfare-models corrects, not as
# the truth. Anything not in the live catalog returns "Model not found" (404) at
# request time.
#
# It now mirrors the *whole* live chat catalog rather than a safe subset, so a
# cold cache offers everything the proxy can route. The proxy config carries a
# wildcard, so a model added upstream works here even before this tuple learns
# about it — the tuple only decides what the picker shows before the fetch.
#
# Logfare's catalog also lists image/TTS/STT models (flux-*, sdxl-lightning,
# whisper-large-v3-turbo, aura-2-en, nova-3, phoenix-1.0, lucid-origin); they
# reject chat completions, so they are omitted here.
#
# Token Juice is deliberately a single entry: its whole catalog is one model,
# exposed upstream as a snapshot-suffixed `deepseek-ai/DeepSeek-V4.1-Flash-<n>`
# id and aliased to `deepseek-v4.1-flash` by its litellm config. It publishes no
# status API, so this list is all there is for it. The alias is stable; the
# upstream id is not — packaging/tokenjuice/litellm-config.yaml is the only
# place that name belongs.
LOGFARE_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
    "deepseek-v4-pro-0813",
    "gemma-4-26b",
    "glm-5.3",
    "glm-5.3-flash",
    "kimi-k2.6",
    "claude-haiku-5-5",
    "logfare/auto",
    "qwen-3.8-27b",
)

TOKENJUICE_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
)

#: IteraCompute's declared fallback. Its real catalog is fetched from the public
#: Models endpoint (see ``iteracompute.py``); this is what ``/model`` shows
#: before that fetch. The free model is the only one an account can use without
#: a balance, so it leads and is the one the switch selects.
ITERACOMPUTE_MODELS: tuple[str, ...] = (
    "qwen/qwen3.8-27b",
)

MODELS_BY_KEY: dict[str, tuple[str, ...]] = {
    "logfare": LOGFARE_MODELS,
    "tokenjuice": TOKENJUICE_MODELS,
    "iteracompute": ITERACOMPUTE_MODELS,
}


def catalog_for(base_url: str):
    """The active source's live catalog, or ``None`` if it has none.

    Only Logfare publishes one. Returned as the source module's own ``Model``
    objects so the caller can read health without knowing where it came from.
    Never fetches: this is the cache, and a cold cache is ``None`` — /model runs
    on the UI thread and must not block on the network.
    """
    if active_key(base_url) != "logfare":
        return None
    from . import logfare as lf_mod
    return lf_mod.cached_models()


def models_for(base_url: str) -> tuple[str, ...] | None:
    """The model list for whichever source this endpoint points at.

    None means "not a known source" — the caller should fall back to the
    default registry rather than showing an empty list.

    Logfare's, TokenHarbour's and OpenRouter's lists are fetched, not declared,
    so they come from a catalog cache rather than MODELS_BY_KEY. A cold cache
    yields None, and the caller falls back to the declared list — which is why
    the per-source *-models commands exist to warm it.
    """
    key = active_key(base_url)
    if key is None:
        return None
    if key == "logfare":
        from . import logfare as lf_mod
        cached = lf_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "tokenharbour":
        from . import tokenharbour as th_mod
        cached = th_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "openrouter":
        from . import openrouter as or_mod
        cached = or_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "unorouter":
        from . import unorouter as uno_mod
        cached = uno_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "antseed":
        from . import antseed as as_mod
        cached = as_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "aihubmix":
        from . import aihubmix as ah_mod
        cached = ah_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "beatapi":
        from . import beatapi as bp_mod
        cached = bp_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "iteracompute":
        from . import iteracompute as ic_mod
        cached = ic_mod.cached_ids()
        return tuple(cached) if cached else None
    return MODELS_BY_KEY.get(key) or None


def catalog_models(key: str) -> list | None:
    """The cached catalog of *key*, as that source module's own ``Model`` list.

    ``None`` for a source with no catalog (Logfare's is health-only and carries
    no window) or a cold cache. Kept as a small dispatch rather than a dict of
    importers so the modules stay lazily imported — ``sources`` is imported by
    their callers, and a module-level import would make them import each other.
    """
    if key == "tokenharbour":
        from . import tokenharbour as th_mod
        return th_mod.cached_models()
    if key == "openrouter":
        from . import openrouter as or_mod
        return or_mod.cached_models()
    if key == "unorouter":
        from . import unorouter as uno_mod
        return uno_mod.cached_models()
    if key == "antseed":
        from . import antseed as as_mod
        return as_mod.cached_models()
    if key == "aihubmix":
        from . import aihubmix as ah_mod
        return ah_mod.cached_models()
    if key == "beatapi":
        from . import beatapi as bp_mod
        return bp_mod.cached_models()
    if key == "iteracompute":
        from . import iteracompute as ic_mod
        return ic_mod.cached_models()
    return None


def context_window_for(base_url: str, model: str) -> int | None:
    """The active model's real context window, from its source's live catalog.

    ``None`` when nothing knows — an unknown source, a cold cache, or a catalog
    that simply does not publish a window (Logfare's status API has no such
    field). The caller falls back to ``Config.context_window``, so a ``None``
    here is not a failure: it means "use the default".

    Read from the cache and never fetched, for the same reason ``catalog_for``
    never fetches: this is called on the turn path, and a blocking network read
    there would stall the very loop it is measuring.

    The field is spelled ``context_length`` in every module that has one, but it
    is read by name off whatever the module's own ``Model`` exposes rather than
    by importing four dataclasses to unify them — that keeps each source's
    parsing where it already lives.
    """
    key = active_key(base_url)
    if key is None:
        return None
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if getattr(entry, "id", None) != model:
            continue
        window = getattr(entry, "context_length", None)
        if isinstance(window, int) and window > 0:
            return window
        return None
    return None


def catalog_price_for(base_url: str, model: str) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok from the active source's live catalog, or ``None``.

    The configured estimate table is keyed by this harness's own model names,
    and a gateway's ids are not always the same string: IteraCompute serves
    ``qwen/qwen3.8-27b`` where Logfare serves ``qwen-3.8-27b``, so the table
    misses it and bills a free model at ``DEFAULT_PRICE``. The catalog knows the
    real answer — including that it is free — so it is consulted first.

    ``None`` means "the catalog cannot say", and the caller falls back to the
    estimate. Read from the cache and never fetched, for the same reason
    ``context_window_for`` does: this is on the turn path.
    """
    key = active_key(base_url)
    if key is None:
        return None
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if getattr(entry, "id", None) != model:
            continue
        if getattr(entry, "free", False):
            return (0.0, 0.0)
        # A module that records whether a price was actually read must be
        # believed: its default ``price_in``/``price_out`` are 0.0, so an
        # unpriced entry would otherwise be reported as free. Modules with no
        # such field keep the old behaviour, where the pair is the answer.
        if getattr(entry, "priced", True) is False:
            return None
        price_in = getattr(entry, "price_in", None)
        price_out = getattr(entry, "price_out", None)
        if isinstance(price_in, (int, float)) and isinstance(price_out, (int, float)):
            return (float(price_in), float(price_out))
        return None
    return None


def max_output_for(base_url: str, model: str) -> int | None:
    """The active model's real output ceiling, from its source's live catalog.

    ``None`` when nothing knows, and the caller then keeps the configured
    ``max_tokens``. The distinction matters: this is a **cap**, so a catalog
    that does not publish one must not be read as "zero".

    Only some catalogs publish the field — AIHubMix, UnoRouter, AntSeed and
    IteraCompute do, under ``max_output`` or ``max_output_tokens``; the rest do
    not. Read by name off each module's own ``Model`` for the same reason
    ``context_window_for`` does, and from the cache rather than by fetching,
    because this is called on the turn path.
    """
    key = active_key(base_url)
    if key is None:
        return None
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if getattr(entry, "id", None) != model:
            continue
        for attr in ("max_output", "max_output_tokens"):
            value = getattr(entry, attr, None)
            if isinstance(value, int) and value > 0:
                return value
        return None
    return None


def _first(paths: list[Path]) -> Path | None:
    for p in paths:
        if p and p.is_file():
            return p
    return None


def _service_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "start-litellm.sh",
            DEFAULT_ROOT / "packaging" / "logfare" / "start-litellm.sh",
        ]
    if key == "iteracompute":
        return [
            ZIMZILLA_HOME / "iteracompute" / "start-litellm.sh",
            DEFAULT_ROOT / "packaging" / "iteracompute" / "start-litellm.sh",
        ]
    return [
        ZIMZILLA_HOME / "tokenjuice" / "start-litellm.sh",
        DEFAULT_HOME / "start-litellm.sh",
    ]


def _config_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "litellm-config.yaml",
            DEFAULT_ROOT / "packaging" / "logfare" / "litellm-config.yaml",
        ]
    if key == "iteracompute":
        return [
            ZIMZILLA_HOME / "iteracompute" / "litellm-config.yaml",
            DEFAULT_ROOT / "packaging" / "iteracompute" / "litellm-config.yaml",
        ]
    return [
        ZIMZILLA_HOME / "tokenjuice" / "litellm-config.yaml",
        DEFAULT_HOME / "litellm-config.yaml",
    ]


def _profile_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "source",
            DEFAULT_ROOT / "packaging" / "logfare" / "source",
        ]
    if key == "iteracompute":
        # No shipped profile: the key is the operator's own. setup.sh writes it
        # when $ANTHROPIC_AUTH_TOKEN is set, and /iteracompute-api-setup writes
        # it otherwise, so there is nothing to fall back to in the tree.
        return [
            ZIMZILLA_HOME / "iteracompute" / "source",
        ]
    return [
        ZIMZILLA_HOME / "tokenjuice" / "source",
        DEFAULT_HOME / "claude-source" / "deepseek-claude",
    ]


def _build(key: str, label: str, port: int, model: str) -> Source | None:
    profile = _first(_profile_candidates(key))
    service = _first(_service_candidates(key))
    config = _first(_config_candidates(key))
    if profile is None or service is None or config is None:
        return None
    return Source(
        key=key, label=label, port=port, profile=profile,
        service=service, config=config, model=model, root=DEFAULT_ROOT,
        models=MODELS_BY_KEY.get(key, ()),
        under_construction=UNDER_CONSTRUCTION.get(key),
    )


def discover() -> dict[str, Source]:
    """Every source whose profile, service and config are actually present."""
    found: dict[str, Source] = {}
    logfare = _build("logfare", "Logfare", 4001, "deepseek-v4.1-flash")
    if logfare:
        found["logfare"] = logfare
    tokenjuice = _build("tokenjuice", "Token Juice", 4000, "deepseek-v4.1-flash")
    if tokenjuice:
        found["tokenjuice"] = tokenjuice
    # The service and config ship in the tree, so this is discovered on every
    # install; only the profile is the operator's, and without it the source is
    # not offered rather than offered and broken at switch time.
    iteracompute = _build("iteracompute", "IteraCompute", 4002, "qwen/qwen3.8-27b")
    if iteracompute:
        found["iteracompute"] = iteracompute
    return found


def source_for_port(port: int) -> str | None:
    """Which source key owns a given proxy port, if any."""
    for key, src in discover().items():
        if src.port == port:
            return key
    return None


#: Memo for ``_upstream_id``: (config path, mtime) -> upstream model id. The
#: config is read on the render path, so it is parsed once per change rather
#: than once per frame.
_UPSTREAM_ID_CACHE: dict[tuple[str, float], str] = {}


def _upstream_id(config: Path) -> str:
    """The upstream model id a LiteLLM config forwards to, or ``""``.

    Read from the config rather than declared here, for the same reason the
    alias is: that file is the one place the upstream name belongs, and the
    snapshot suffix in it rotates. Deriving the display name from it means the
    suffix cannot go stale in a second location.
    """
    try:
        mtime = config.stat().st_mtime
    except OSError:
        return ""
    key = (str(config), mtime)
    if key in _UPSTREAM_ID_CACHE:
        return _UPSTREAM_ID_CACHE[key]
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^\s*model:\s*openai/(\S+)", text, re.MULTILINE)
    found = m.group(1) if m else ""
    _UPSTREAM_ID_CACHE[key] = found
    return found


def display_model(base_url: str, model: str) -> str:
    """The model name to SHOW for *model* on this endpoint.

    The name sent and the name worth showing are not always the same string.
    Token Juice's LiteLLM config registers a stable alias
    (``deepseek-v4.1-flash``) and forwards it upstream as a snapshot-suffixed id
    (``deepseek-ai/DeepSeek-V4.1-Flash-1017``). The alias is what must go on the
    wire — it is the only name the proxy knows — but it is also the one name
    that does not say which snapshot is actually answering.

    So the display name is the alias with the upstream snapshot suffix
    appended, lifted from the config rather than hardcoded: the suffix rotates,
    and a literal here would be wrong the next time it does.

    Falls back to *model* unchanged when the endpoint is not Token Juice, the
    config cannot be read, or the upstream id is not simply the alias plus a
    suffix — a different model entirely is shown as-is rather than mangled.
    Never fetches: this is called on the render path.
    """
    if active_key(base_url) != "tokenjuice":
        return model
    src = discover().get("tokenjuice")
    if src is None:
        return model
    upstream = _upstream_id(src.config)
    if not upstream:
        return model
    # `deepseek-ai/DeepSeek-V4.1-Flash-1017` -> `deepseek-v4.1-flash-1017`
    leaf = upstream.rsplit("/", 1)[-1].lower()
    if leaf.startswith(model) and leaf != model:
        return leaf
    return model


# ---------------------------------------------------------------------------
# current selection (persisted so a restart keeps the working source)
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return ZIMZILLA_HOME / "current-source"


#: Sources that are always selectable, whether or not `discover()` finds them
#: on this machine. TokenHarbour, OpenRouter, UnoRouter and AIHubMix are remote:
#: they have no local profile, service or config to discover, so gating them on
#: discover() would silently discard the selection and drop a session back onto
#: Logfare at the next launch. AntSeed is local but equally undiscoverable by
#: `discover()`, which only knows the two LiteLLM proxies — its buyer is started
#: and stopped by the operator, so requiring it to be up at selection time would
#: lose the choice every time the proxy was down.
BUILTIN_KEYS = frozenset({"tokenharbour", "openrouter", "unorouter", "antseed",
                          "aihubmix", "beatapi"})


def current_key() -> str | None:
    """The source the user last selected, if it is still a valid one.

    A discovered source is valid while its files are present. A built-in one is
    valid always — there is nothing on disk that could go missing. A parked one
    is valid never: see UNDER_CONSTRUCTION, which is why this is checked before
    the others rather than after.
    """
    try:
        key = _state_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if key in UNDER_CONSTRUCTION:
        return None
    if key in BUILTIN_KEYS:
        return key
    return key if key in discover() else None


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI.

    A parked source may be written here; it is ``current_key`` that refuses to
    read one back. Keeping the write unconditional means this stays the plain
    "remember the choice" function it reads as, and the parking decision lives
    in exactly one place rather than being half-applied at both ends.
    """
    try:
        ZIMZILLA_HOME.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(key + "\n", encoding="utf-8")
    except OSError:
        pass


def active_key(base_url: str) -> str | None:
    """Best guess at which source the running session is pointed at.

    The local proxies are identified by their port. The hosted gateways are
    remote, so they are identified by host instead — and checked first, because
    a hosted URL has no port to match on and would otherwise fall through to
    ``None``. Each gateway exposes an ``is_<name>`` predicate, so the host
    string lives in exactly one module rather than being spelled again here.
    """
    # Imported locally: sources is imported by these modules' callers, and a
    # module-level import would make them import each other.
    from . import aihubmix as ah_mod
    from . import antseed as as_mod
    from . import beatapi as bp_mod
    from . import iteracompute as ic_mod
    from . import openrouter as or_mod
    from . import tokenharbour as th_mod
    from . import unorouter as uno_mod
    if th_mod.is_tokenharbour(base_url):
        return "tokenharbour"
    if or_mod.is_openrouter(base_url):
        return "openrouter"
    if uno_mod.is_unorouter(base_url):
        return "unorouter"
    if ah_mod.is_aihubmix(base_url):
        return "aihubmix"
    if bp_mod.is_beatapi(base_url):
        return "beatapi"
    # Checked by port before the generic regex for the same reason AntSeed is:
    # it is a local proxy, and identifying it by its own predicate keeps the
    # port in one module rather than spelling it here too. It also means the
    # source is recognised even when `discover()` has no profile to find.
    if ic_mod.is_iteracompute(base_url):
        return "iteracompute"
    # Checked before the port match, and by port rather than by name: the buyer
    # hands out a plain http://localhost:8377 with no "antseed" in it, and the
    # regex below would otherwise find port 8377, miss it in source_for_port()
    # (which only knows the two LiteLLM proxies) and return None.
    if as_mod.is_antseed(base_url):
        return "antseed"
    m = re.search(r":(\d{2,5})(?:/|$)", base_url or "")
    if not m:
        return None
    return source_for_port(int(m.group(1)))


# ---------------------------------------------------------------------------
# profile / proxy
# ---------------------------------------------------------------------------

def load_profile(src: Source) -> dict[str, str]:
    """Read the profile's exports without a shell, so nothing is echoed.

    Parses the ``export VAR="value"`` lines the profiles are written in. A
    profile that uses anything more exotic than that is read by the launcher
    anyway; this is only for the in-TUI switch.
    """
    out: dict[str, str] = {}
    try:
        text = src.profile.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        body = line[len("export "):]
        if "=" not in body:
            continue
        name, _, value = body.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def proxy_healthy(port: int, timeout: float = 2.0) -> bool:
    url = f"http://127.0.0.1:{port}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_proxy(src: Source, timeout: float = 180.0) -> tuple[bool, str]:
    """Start the source's proxy if it is not already answering. Blocking."""
    if proxy_healthy(src.port):
        return True, "already running"
    if not src.service.is_file():
        return False, f"no service manager at {src.service}"

    env = dict(os.environ)
    env.setdefault("ZIMZILLA_ROOT", str(src.root))
    env["LITELLM_ENV_FILE"] = str(src.profile)
    env["LITELLM_CONFIG"] = str(src.config)
    env["LITELLM_PORT"] = str(src.port)
    # Do not wrap this in flock. The launcher already serialises its own
    # cold-start, and wrapping here is how a leaked lock (LiteLLM inheriting
    # the launcher's lock fd) made /zim-tokenjuice hang until timeout while
    # a manual start of the same script finished in seconds. Two sources
    # live on different ports, so they can come up independently.
    argv = [str(src.service), "start"]

    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    except OSError as e:
        return False, f"could not run {src.service}: {e}"

    if proxy_healthy(src.port):
        return True, "started"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else f"exit {proc.returncode}"
    return False, tail
