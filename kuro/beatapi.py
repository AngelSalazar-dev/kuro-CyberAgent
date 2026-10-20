"""BeatAPI â€” a hosted Anthropic-protocol gateway, as a Kuro source.

BeatAPI is a remote capability layer (``https://beatapi.io``) that fronts text,
image and video models, social-data actions and web tools behind one key. This
module wires up the **text** half: one endpoint that speaks the Anthropic
Messages protocol natively, so there is no translation layer to write and none
is written.

Measured on this box, not assumed:

* ``POST https://direct.beatapi.io/v1/messages`` exists and answers an
  Anthropic/BeatAPI-shaped 401 when no key is sent â€” ``{"error": {"code":
  "missing_api_key", ...}}``. The path is real, which is what makes this a
  genuine source rather than a tool surface.
* ``GET https://api.beatapi.io/v1/text/models`` answers **with no key at all**
  (44 text models when this was written: the Claude/GPT/Gemini/Grok/DeepSeek/
  GLM/Qwen/Kimi/MiniMax/MiMo/Hunyuan/Muse lines plus a ``free`` family). The
  catalog is public, so the free list can be shown, and a model chosen, *before*
  the operator has set anything.
* The response nests the list one level deep: ``{"data": {"object": ...,
  "data": [...]}}``. That is unusual enough that it is parsed defensively â€”
  a flat ``{"data": [...]}`` is also accepted â€” rather than assumed.

**The base URL carries no ``/v1``.** The Anthropic SDK appends ``/v1/messages``
to whatever base it is handed, so :data:`DEFAULT_BASE_URL` is the origin alone:
``https://direct.beatapi.io`` composes to ``/v1/messages``. Passing
``https://direct.beatapi.io/v1`` would compose to ``/v1/v1/messages`` and 404 on
every turn while the catalog still worked â€” the same trap ``openrouter.py`` and
``unorouter.py`` document.

BeatAPI publishes two text origins. ``https://direct.beatapi.io`` is the
recommended one â€” it is not behind Cloudflare's 100-second limit, so a long
non-streaming request is not cut off â€” and is what :data:`DEFAULT_BASE_URL`
names. ``https://api.beatapi.io`` is the documented fallback and the only origin
that serves the model catalog and the image/video APIs.

**Free models are ``-free`` ids in a ``free`` family.** They publish no price,
no context window and no capabilities â€” the catalog says only an id, a family
and a native ``endpoints`` list. That shapes this module in two ways:

* The free list cannot be *ranked* from what the gateway publishes, so
  :func:`usable_free_models` orders it by a cross-reference the catalog *does*
  support: each free id is the free version of a paid sibling, and the sibling's
  capabilities say whether that line advertises tool calling. It is a
  catalogue-derived ordering, not a measurement â€” this module has not driven
  these models, and the docstring says so rather than implying a sweep that
  never ran. The flag is used to *order* the list, never to hide a model.
* ``endpoints`` is **not** a filter. Every free entry reports ``openai`` alone,
  and filtering on it would hide every free model â€” the exact opposite of the
  point. The gateway documents ``endpoints`` as the model line's *native*
  formats, "not the full list of formats you can call", so it is recorded for
  display and never used to exclude.

Free ids are a limited-time offer, rate limited per account, and can be
withdrawn without notice â€” which is why the list is fetched live rather than
baked in, and why the same id can work at noon and 429 by evening.

Nothing here echoes a key.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

KURO_HOME = Path(os.environ.get("KURO_HOME") or (Path.home() / ".kuro"))
#: Where the operator keeps their per-provider shell profiles â€” the same
#: ``~/claude-source/<name>`` files a plain ``source`` loads for Claude Code.
CLAUDE_SOURCE = Path(os.environ.get("KURO_CLAUDE_SOURCE")
                     or (Path.home() / "claude-source"))

#: The canonical host. Matching is by this substring, so both text origins â€”
#: ``direct.beatapi.io`` and ``api.beatapi.io`` â€” count as the same source.
HOST = "beatapi.io"

#: The SDK's ``base_url`` â€” the origin BEFORE ``/v1``, not the API root. The
#: Anthropic SDK appends ``/v1/messages``, composing to
#: ``https://direct.beatapi.io/v1/messages``, which is where BeatAPI serves the
#: Messages protocol (measured: a real ``missing_api_key`` 401, not a 404).
#: ``direct.`` is chosen over ``api.`` because it is the documented text origin
#: and is not subject to the 100-second edge limit; ``api.`` stays reachable as
#: the operator's manual fallback.
DEFAULT_BASE_URL = "https://direct.beatapi.io"

#: The origin that serves the public model catalog. Deliberately NOT
#: :data:`DEFAULT_BASE_URL`: the docs are explicit that ``direct.beatapi.io``
#: "serves model calls, not this list", so the catalog is fetched from ``api.``
#: while turns go to ``direct.``. Spelling both out is what stops one being
#: "fixed" into breaking the other.
API_BASE_URL = "https://api.beatapi.io"

#: The public text-model catalog. No key required â€” which is why the free list
#: works before ``/beatapi-api-setup`` has run.
MODELS_URL = f"{API_BASE_URL}/v1/text/models"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = KURO_HOME / "beatapi" / "source"

#: A fetched catalog older than this is refetched rather than trusted. The free
#: tier is a limited-time offer that turns over, so a stale list is exactly the
#: wrong list this picker exists to avoid.
CACHE_TTL = 300.0

#: The suffix that marks a free model id.
FREE_SUFFIX = "-free"

#: The family the catalog files free models under.
FREE_FAMILY = "free"


@dataclass(frozen=True)
class Model:
    """One entry from BeatAPI's text-model catalog."""

    id: str
    label: str = ""
    context_length: int | None = None
    max_output: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    #: The catalog's routing/billing group: ``claude``, ``codex``, ``deepseek``,
    #: ``gemini``, ``gemini-flash``, ``free``, â€¦ A key must be able to reach the
    #: family to call the model.
    family: str = ""
    #: The model line's *native* formats (``openai``, ``anthropic``,
    #: ``openai-response``). Display only â€” see the module docstring for why it
    #: is never a filter.
    endpoints: tuple[str, ...] = ()
    #: Whether a price was actually **read**. Free entries publish none, so
    #: without this flag a missing price would read as 0/0 and mark every entry
    #: free â€” the failure the flag exists to prevent.
    priced: bool = False
    #: Advisory tool/reasoning flags. For a paid entry these are the catalog's
    #: own ``capabilities``; for a free entry the catalog publishes none and
    #: they are borrowed from the paid sibling of the same line, which is a
    #: cross-reference rather than a guarantee â€” see the module docstring.
    tools: bool = False
    reasoning: bool = False

    @property
    def free(self) -> bool:
        """Whether this model costs nothing.

        True when the catalog files it under the ``free`` family or suffixes
        its id â€” both are how BeatAPI marks a giveaway â€” or when a read price
        came back exactly 0/0. The price is the one signal the gateway is
        explicit about ("a model ID that ends in ``-free`` is free"), so the
        three agreeing is corroboration rather than redundancy.
        """
        if self.family == FREE_FAMILY or self.id.endswith(FREE_SUFFIX):
            return True
        return self.priced and self.price_in == 0.0 and self.price_out == 0.0

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn.

        Always true: ``/v1/text/models`` contains text models only, so the
        modality filter the other gateway modules carry has nothing to drop
        here. The image and video catalogs are separate endpoints this module
        never reads.
        """
        return True

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        elif self.priced and (self.price_in or self.price_out):
            bits.append(f"${self.price_in:g}/${self.price_out:g}")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if self.reasoning:
            bits.append("reasoning")
        if self.tools:
            bits.append("tools")
        if self.family and self.family != FREE_FAMILY:
            bits.append(self.family)
        return " Â· ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def current_key() -> str | None:
    """The source key the operator last selected, if any.

    Delegates to :mod:`kuro.sources`, which owns ``current-source``. Two
    writers of one selection file is how a switch made here and a switch made
    with another gateway's command end up disagreeing about what is live.
    """
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    """Record the selection. Never fatal â€” a read-only home must not break the TUI."""
    from . import sources as sources_mod
    sources_mod.set_current(key)


def is_beatapi(base_url: str | None) -> bool:
    """Whether *base_url* points at BeatAPI (either text origin)."""
    return HOST in (base_url or "")


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds. See ``_cmd_gateway_key`` in ui/app.py.
active_endpoint = is_beatapi


def _read_exports(path: Path) -> dict[str, str]:
    """Parse ``export VAR="value"`` lines from a profile, without a shell."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
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
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def _key_from_claude_source() -> tuple[str, str, str] | None:
    """A BeatAPI key already sitting in ``~/claude-source/<name>``.

    This is the ``source ~/claude-source/<name>`` idiom: the operator has
    already written a profile for Claude Code, so Kuro should use it rather
    than asking for the key again. Returns ``(key, model, path)`` for the first
    profile that points at BeatAPI and carries a token.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_beatapi(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The BeatAPI key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at BeatAPI â€” i.e. the operator
         sourced a profile before launching.
      2. Kuro's own profile, written by ``/beatapi-api-setup``.
      3. Any BeatAPI profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged or
    returned to the transcript â€” only ``origin`` (a path, or "environment") is.
    """
    if is_beatapi(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "")
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"

    exports = _read_exports(PROFILE)
    token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)

    found = _key_from_claude_source()
    if found:
        return found
    return None


def save_key(token: str, model: str = "") -> Path:
    """Write the key to Kuro's own BeatAPI profile, mode 600.

    The file is the same ``source`` format as every other profile â€” it can be
    sourced by a shell and read by ``load_credentials`` â€” so there is one
    credential format in the project, not six.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        f'export ANTHROPIC_BASE_URL="{DEFAULT_BASE_URL}"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
        f'export ANTHROPIC_MODEL="{model}"\n'
        'export ANTHROPIC_API_KEY=""\n'
    )
    # Create with 600 from the outset, then rewrite in place: never a window in
    # which the key sits in a world-readable file.
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(PROFILE, 0o600)
    except OSError:
        pass
    return PROFILE


def has_key() -> bool:
    return load_credentials() is not None


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at BeatAPI. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-beatapi`` and the launch-time restore in ``__main__`` â€” so a session
    started by the command and one started from the remembered source end up
    configured identically.

    ``api_key`` is cleared rather than left as-is: the client sends
    ``auth_token`` when it is set, and a stale ``api_key`` from another provider
    would be a credential for the wrong service sitting in memory.

    With no model named by the profile the model is left exactly as the caller
    had it, and the empty model is returned. That is deliberate rather than an
    omission: the caller (``_cmd_zim_gateway``) reads an empty model as "no
    preference saved" and fetches the free list **on a worker thread**. Naming a
    default here instead would mean a network fetch on the UI thread â€” this runs
    on every switch â€” which is the stall ``_fetch_models`` exists to avoid. So a
    fresh switch aims the session at BeatAPI and shows the operator every free
    model on offer, and they pick one with ``/model``.

    Returns ``None`` when no key can be found anywhere, and leaves *cfg* alone.
    """
    creds = load_credentials()
    if creds is None:
        return None
    token, model, origin = creds
    cfg.base_url = DEFAULT_BASE_URL
    cfg.auth_token = token
    cfg.api_key = ""
    if model:
        cfg.model = model
    return origin, model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered BeatAPI choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate â€” this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no BeatAPI profile to source. This is what keeps a stale ``current-source``
    from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_beatapi(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. The catalog is
#: public and identical for every caller, so the keyless fetch is cached under
#: "" and a keyed one under its token â€” a key change refetches rather than
#: serving a list fetched under a different account.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _capabilities(entry: dict) -> set[str]:
    """The entry's ``capabilities`` labels, lowercased. Advisory, never a filter."""
    caps = entry.get("capabilities")
    if not isinstance(caps, list):
        return set()
    return {str(c).strip().lower() for c in caps if isinstance(c, str)}


def _has_tool(caps: set[str]) -> bool:
    """Whether a capability label names tool calling â€” in either language.

    BeatAPI labels are not consistent: some entries are English
    (``Tools``, ``Reasoning``) and some Chinese (``å·¥å…·`` = tools,
    ``æŽ¨ç†`` = reasoning). Both are checked so a family that describes its
    capabilities in Chinese is not silently read as tool-less.
    """
    return "tools" in caps or "å·¥å…·" in caps


def _has_reasoning(caps: set[str]) -> bool:
    return "reasoning" in caps or "æŽ¨ç†" in caps


def _entries(payload: dict) -> list[dict]:
    """The catalog rows, however the reply nested them.

    The live shape is ``{"data": {"object": ..., "data": [...]}}`` â€” one level
    deeper than the usual OpenAI-style ``{"data": [...]}``. Both are accepted so
    a gateway that flattens its own envelope does not take the picker down, and
    a malformed payload yields an empty catalog rather than an exception: this
    runs inside a TUI command.
    """
    data = payload.get("data")
    if isinstance(data, dict):
        inner = data.get("data")
        return [e for e in inner if isinstance(e, dict)] if isinstance(inner, list) else []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the text-model payload, in catalog order.

    Free entries are resolved against their paid sibling for the two advisory
    flags only. The catalog publishes no capabilities, context or price for a
    ``-free`` id, and the siblings' context/output values are explicitly *not*
    promised for the free id (per the gateway's own docs), so nothing else is
    borrowed.
    """
    rows = _entries(payload)
    # First pass: a capability map keyed by id, so the free entries in the
    # second pass can cross-reference their sibling in one lookup.
    caps_by_id: dict[str, set[str]] = {}
    for entry in rows:
        mid = str(entry.get("id") or "").strip()
        if mid:
            caps_by_id[mid] = _capabilities(entry)

    out: list[Model] = []
    for entry in rows:
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        family = str(entry.get("family") or "").strip()
        endpoints_raw = entry.get("endpoints")
        endpoints = (tuple(str(e) for e in endpoints_raw if isinstance(e, str))
                     if isinstance(endpoints_raw, list) else ())

        price_in = _as_float(entry.get("input_usd_per_million"))
        price_out = _as_float(entry.get("output_usd_per_million"))
        priced = price_in is not None and price_out is not None
        if not priced:
            price_in = price_out = 0.0

        # Capabilities: the entry's own, or â€” for a free id that publishes
        # none â€” its paid sibling's, as an advisory cross-reference.
        caps = caps_by_id.get(mid, set())
        if not caps and (family == FREE_FAMILY or mid.endswith(FREE_SUFFIX)):
            sibling = mid[:-len(FREE_SUFFIX)] if mid.endswith(FREE_SUFFIX) else ""
            caps = caps_by_id.get(sibling, set())

        out.append(Model(
            id=mid,
            label=str(entry.get("description") or "")[:120],
            context_length=_as_int(entry.get("context_length")),
            max_output=_as_int(entry.get("max_output_tokens")),
            price_in=price_in or 0.0,
            price_out=price_out or 0.0,
            family=family,
            endpoints=endpoints,
            priced=priced,
            tools=_has_tool(caps),
            reasoning=_has_reasoning(caps),
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch BeatAPI's text-model catalog. Returns ``(models, error)``.

    **No key required** â€” the catalog is public, so ``token`` defaults to empty
    and the free list is available before ``/beatapi-api-setup`` has run. A key
    is sent when one is supplied, so the listing matches what this account can
    actually reach.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach BeatAPI" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog â€”
    the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    headers = {"accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(MODELS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) â€” check it with /beatapi-api-setup"
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models BeatAPI is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can drive, tool-capable line first.

    Free **and** a text model â€” which, since the catalog is the text one, means
    every free entry. There is deliberately **no** tool filter here: free
    entries publish no capabilities, so filtering on them would return an empty
    list and hide the very models this command exists to surface. The advisory
    flag, borrowed from each free id's paid sibling, is instead used to *order*
    the list â€” a free id whose line advertises tool calling leads, because a
    model that cannot call a tool cannot drive an agent loop. The sort is
    stable, so within a tier catalog order is preserved.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    return sorted(models, key=lambda m: not m.tools), ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every text model id, catalog order preserved â€” what ``/model`` offers.

    All of them, not only the free ones: an operator who names a paid
    model deliberately should not have it refused as "not served by beatapi"
    against a list filtered to the free tier.
    """
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    """Seconds since the newest cache entry was filled, or ``None`` if none was."""
    if not _CACHE:
        return None
    newest = max(entry[1] for entry in _CACHE.values())
    return time.monotonic() - newest


def cached_models() -> list[Model] | None:
    """The last-fetched catalog as :class:`Model` objects, or ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last fetch left behind.

    **The ``CACHE_TTL`` check is load-bearing**, exactly as in the other
    gateway modules. Without it a catalog fetched once reads as live for the
    rest of the session, and a free tier turns over, so a stale list is the
    wrong list this picker exists to avoid. An expired entry reads as ``None``,
    which callers already handle as "no catalog".
    """
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    if newest is None or (time.monotonic() - newest[1]) >= CACHE_TTL:
        return None
    return list(newest[0])


def cached_ids() -> list[str] | None:
    """The last-fetched text model ids, without touching the network."""
    models = cached_models()
    return [m.id for m in models] if models is not None else None


def clear_cache() -> None:
    """Drop every cached catalog. Called when the key changes."""
    _CACHE.clear()


def model_for(model: str, models: list[Model] | None) -> Model | None:
    """The catalog's entry for *model*, if the catalog knows it."""
    if not models:
        return None
    base = model.strip()
    for m in models:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog can answer.

    The catalog publishes dollars per million tokens directly, so this is the
    gateway's own rate rather than a local estimate â€” for both tiers. A free
    model returns ``(0.0, 0.0)``, which is exact and is what makes the note read
    ``free`` rather than ``$0/$0``. An id the catalog does not know returns
    ``None``, so the caller falls back to this harness's labelled estimate.
    """
    found = model_for(model, models)
    if found is None:
        return None
    if found.free:
        return (0.0, 0.0)
    if not found.priced:
        return None
    return (found.price_in, found.price_out)


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose â€” the gateway decides what is valid, not us â€” but strict
    enough to catch a pasted shell line or a bare word. BeatAPI keys work with
    or without an ``sk_`` prefix, so no prefix is required; only length, no
    whitespace, and the character set a token is drawn from.
    """
    value = value.strip()
    if len(value) < 12 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))

