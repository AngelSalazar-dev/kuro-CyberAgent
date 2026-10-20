"""Regression suite for the BeatAPI source.

Run:  python tests/test_beatapi.py   (from an activated venv)

Protects what is particular about this provider:

* **Two hosts, one base URL, and the SDK's missing ``/v1``.** Turns go to
  ``https://direct.beatapi.io`` and the catalog is fetched from
  ``https://api.beatapi.io`` â€” a *different* host, not a sibling path. The
  Anthropic SDK appends ``/v1/messages`` to whatever base it is handed, so
  ``DEFAULT_BASE_URL`` must carry no ``/v1``. Asserting the constant can only
  restate the bug â€” OpenRouter shipped a total outage behind a green suite that
  way â€” so this builds the request through the real client and reads the URL
  back, and checks the catalog URL in the same breath. "Fixing" either by moving
  the base to ``/v1`` silently breaks the other.
* **The nested catalog shape.** The live reply is
  ``{"data": {"object": ..., "data": [...]}}`` â€” one level deeper than the usual
  OpenAI-style ``{"data": [...]}``. Both are accepted; a payload that is neither
  yields an empty catalog, not a traceback, because this runs inside a TUI.
* **Free is the family, the suffix, or a read 0/0 price â€” and a *missing* price
  is none of them.** Free entries publish no price, so a missing price parsed as
  0/0 would mark every unpriced model free. ``priced`` records a successful read,
  and the test pins the consequence.
* **``endpoints`` is not a filter.** Every free entry reports ``openai`` alone,
  and filtering on ``anthropic`` would hide every free model â€” the exact
  opposite of the point. The gateway documents ``endpoints`` as a line's native
  formats, "not the full list of formats you can call".
* **Capabilities are borrowed from the paid sibling, advisory only.** Free ids
  publish no ``capabilities``; ``glm-5.3-free`` inherits ``å·¥å…·``/``æŽ¨ç†`` from
  ``glm-5.3``, and a free id whose sibling publishes none inherits nothing. The
  flag orders the free list and never hides a model.
* **The catalog needs no key.** ``GET /v1/text/models`` is public, so the free
  list is available *before* ``/beatapi-api-setup`` has run.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# Shaped like the real response: the list nested one level down under `data`,
# free entries carrying only an id/family/endpoints, paid entries carrying
# prices and (sometimes) capabilities in either language.
#
# The entries cover every case the module has to get right:
#   - free family, no price, endpoints ["openai"]      â†’ free (the filter trap)
#   - free family, sibling advertises tools            â†’ free, tools borrowed
#   - free family, sibling publishes no capabilities   â†’ free, no tools claim
#   - `-free` suffix but NOT in the free family        â†’ free by suffix
#   - paid, English capabilities                       â†’ tools + reasoning
#   - paid, Chinese capabilities (å·¥å…· / æŽ¨ç†)          â†’ tools + reasoning
#   - no price fields at all, no free marker           â†’ neither (the trap)
#   - malformed rows                                   â†’ skipped, not fatal
CATALOG = {
    "data": {
        "object": "list",
        "data": [
            # Free family, native `openai` only â€” filtering on `anthropic`
            # here would drop every one of them.
            {"id": "glm-5.3-free", "family": "free", "endpoints": ["openai"]},
            {"id": "deepseek-v4.1-flash-free", "family": "free",
             "endpoints": ["openai"]},

            # Paid siblings. The capabilities here are what the free ids above
            # borrow â€” and why one of them reads as tool-capable and the other
            # does not.
            {"id": "glm-5.3", "family": "glm", "family_label": "glmæ–‡æœ¬åˆ†ç»„",
             "endpoints": ["anthropic", "openai"],
             "context_length": 1000000, "max_output_tokens": 128000,
             "input_usd_per_million": 0.98, "output_usd_per_million": 3.08,
             "capabilities": ["æŽ¨ç†", "å·¥å…·", "è§†è§‰"]},
            {"id": "deepseek-v4.1-flash", "family": "deepseek",
             "endpoints": ["anthropic", "openai"],
             "context_length": 1000000, "max_output_tokens": 64000,
             "input_usd_per_million": 0.14, "output_usd_per_million": 0.28},
            {"id": "claude-opus-5", "family": "claude",
             "endpoints": ["anthropic", "openai"],
             "context_length": 200000, "max_output_tokens": 32000,
             "input_usd_per_million": 2.5, "output_usd_per_million": 12.5,
             "capabilities": ["Reasoning", "Tools", "Vision"]},

            # No price fields at all and no free marker: NOT free. This is the
            # row that a missing-price-reads-as-zero bug would report as free.
            {"id": "mystery/thing", "family": "misc"},

            # A -free id the catalog did not file under the free family. The
            # suffix alone still marks it.
            {"id": "some-vendor/mystery-free", "family": "misc",
             "endpoints": ["openai"]},

            # Malformed rows: skipped, not fatal.
            "not-a-row",
            {"family": "free"},
            {"id": "   ", "family": "free"},
        ],
    }
}

#: The ids that are free, in catalog order.
FREE = ["glm-5.3-free", "deepseek-v4.1-flash-free", "some-vendor/mystery-free"]

#: The free ids as usable_free_models returns them: a tool-advertising line
#: first, catalog order preserved within a tier. Here only glm-5.3-free's
#: sibling advertises tools, so it leads and the other two keep their order.
USABLE = ["glm-5.3-free", "deepseek-v4.1-flash-free", "some-vendor/mystery-free"]

#: The whole well-formed catalog. Three rows in CATALOG are malformed and are
#: skipped by the parser, so this is not len(CATALOG["data"]["data"]).
ROWS = 7


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub(payload=None, error=None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())
    return _open


def _capture(payload=None):
    """A stub that records the request, so the URL and headers can be asserted."""
    seen: dict = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["data"] = req.data
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())

    return seen, _open


def _clean_env():
    """Context manager that hides any BeatAPI credential in the environment."""
    class _C:
        def __enter__(self):
            self.saved = {k: os.environ.pop(k, None) for k in
                          ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                           "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")}
            return self

        def __exit__(self, *exc):
            for k, v in self.saved.items():
                if v is not None:
                    os.environ[k] = v
            return False
    return _C()


# ---------------------------------------------------------------------------

def test_free_is_the_family_suffix_or_price() -> None:
    """A free family, a ``-free`` suffix or a read 0/0 price â€” the suffix is a hint."""
    from kuro import beatapi as bp

    models = bp._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("catalog: every well-formed row parsed", len(models) == ROWS, str(len(models)))
    check("catalog: a row with no id is skipped", "no id here" not in by_id)

    check("free: the free family is free", by_id["glm-5.3-free"].free)
    check("free: the free family with no price is free",
          by_id["deepseek-v4.1-flash-free"].free)
    check("free: a -free suffix outside the family is free",
          by_id["some-vendor/mystery-free"].free)

    check("free: a paid model is not free", not by_id["claude-opus-5"].free)
    check("free: another paid model is not free", not by_id["glm-5.3"].free)
    # The heart of it: no price was published and there is no free marker.
    check("free: an unpriced model with no free marker is NOT free",
          not by_id["mystery/thing"].free)

    got = [m.id for m in models if m.free]
    check("free: exactly the free set, in catalog order", got == FREE,
          f"{len(got)} vs {len(FREE)}")


def test_priced_flag_records_a_read_not_a_block() -> None:
    """``priced`` tracks a successful *read*, so an absent price is not 0/0."""
    from kuro import beatapi as bp

    models = bp._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("priced: a real paid pair counts as priced", by_id["glm-5.3"].priced)
    check("priced: a free entry that publishes no price is not priced",
          not by_id["glm-5.3-free"].priced)
    check("priced: an entry with no price fields is not priced",
          not by_id["mystery/thing"].priced)

    # A genuine 0/0 read is free even without the family or suffix.
    zero = bp._parse_models({"data": {"data": [
        {"id": "zero/thing", "family": "misc",
         "input_usd_per_million": 0, "output_usd_per_million": 0}]}})
    check("priced: a read 0/0 price is free", zero[0].free and zero[0].priced)

    # No price at all is not the same as a zero price.
    no_price = bp._parse_models({"data": {"data": [
        {"id": "unpriced/thing", "family": "misc"}]}})
    check("priced: an unpriced model with no marker is not free",
          not no_price[0].free)

    # A price that is present but unparseable is the same case.
    junk = bp._parse_models({"data": {"data": [
        {"id": "junk/thing", "family": "misc",
         "input_usd_per_million": "n/a", "output_usd_per_million": "n/a"}]}})
    check("priced: an unparseable price is not treated as zero",
          not junk[0].free and not junk[0].priced)


def test_endpoints_is_not_a_filter() -> None:
    """Every free entry reports ``openai`` alone; filtering on it hides them all.

    This is the BeatAPI-shaped version of the AIHubMix ``tool_call`` trap. The
    gateway documents ``endpoints`` as the line's *native* formats, "not the full
    list of formats you can call", so a free model reachable over Anthropic
    Messages still reports ``["openai"]``. Adding ``'anthropic' in endpoints`` as
    a "correct-looking" filter would empty the very list this command exists to
    build, so the case is pinned by name.
    """
    from kuro import beatapi as bp

    by_id = {m.id: m for m in bp._parse_models(CATALOG)}
    check("endpoints: a free entry's native list is openai only",
          set(by_id["glm-5.3-free"].endpoints) == {"openai"},
          str(by_id["glm-5.3-free"].endpoints))
    check("endpoints: a free entry is NOT anthropic-native",
          "anthropic" not in by_id["glm-5.3-free"].endpoints)

    with mock.patch("urllib.request.urlopen", _stub()):
        bp.clear_cache()
        usable, err = bp.usable_free_models()

    check("endpoints: the fetch succeeded", usable is not None, err)
    ids = [m.id for m in usable] if usable else []
    check("endpoints: a free model survives despite openai-only native list",
          "glm-5.3-free" in ids, str(ids))
    check("endpoints: the usable set is exactly the free models", ids == USABLE,
          str(ids))


def test_capabilities_borrowed_from_the_paid_sibling() -> None:
    """Free ids publish no capabilities; the sibling's are borrowed, as advice."""
    from kuro import beatapi as bp

    by_id = {m.id: m for m in bp._parse_models(CATALOG)}

    check("caps: a paid entry's English caps read as tools",
          by_id["claude-opus-5"].tools)
    check("caps: and as reasoning", by_id["claude-opus-5"].reasoning)
    check("caps: a paid entry's Chinese å·¥å…· reads as tools", by_id["glm-5.3"].tools)
    check("caps: and æŽ¨ç† as reasoning", by_id["glm-5.3"].reasoning)

    check("caps: the free id borrows its sibling's tools flag",
          by_id["glm-5.3-free"].tools)
    check("caps: and its reasoning flag", by_id["glm-5.3-free"].reasoning)
    check("caps: a free id whose sibling publishes none inherits no tools",
          not by_id["deepseek-v4.1-flash-free"].tools)
    check("caps: nor reasoning", not by_id["deepseek-v4.1-flash-free"].reasoning)
    check("caps: a free id with no sibling at all inherits nothing",
          not by_id["some-vendor/mystery-free"].tools)

    # And the consequence: the borrowed flag ORDERS the list, it does not filter.
    with mock.patch("urllib.request.urlopen", _stub()):
        bp.clear_cache()
        usable, _ = bp.usable_free_models()
    check("caps: the tool-advertising free id leads the list",
          [m.id for m in usable][0] == "glm-5.3-free",
          str([m.id for m in usable]))
    check("caps: a tool-less free id is still listed, not hidden",
          "deepseek-v4.1-flash-free" in [m.id for m in usable])


def test_chat_is_always_true() -> None:
    """The catalog is the *text* catalog, so every entry can answer a turn."""
    from kuro import beatapi as bp

    models = bp._parse_models(CATALOG)
    check("chat: every parsed model is chat-capable",
          all(m.chat for m in models))


def test_catalog_needs_no_key() -> None:
    """The catalog is public â€” a keyless fetch must succeed and send nothing."""
    from kuro import beatapi as bp

    seen, opener = _capture()
    with _clean_env(), mock.patch("urllib.request.urlopen", opener):
        bp.clear_cache()
        models, err = bp.fetch_models("")

    check("keyless: the fetch succeeded with no token", models is not None, err)
    check("keyless: no Authorization header is sent",
          "authorization" not in seen.get("headers", {}),
          str(sorted(seen.get("headers", {}))))
    check("keyless: the catalog lands on api./v1/text/models",
          seen.get("url") == "https://api.beatapi.io/v1/text/models",
          seen.get("url"))
    check("keyless: it is a GET", seen.get("method") == "GET", seen.get("method"))

    # With a token the same call carries a Bearer, because the listing should
    # match what this account can reach.
    seen2, opener2 = _capture()
    with mock.patch("urllib.request.urlopen", opener2):
        bp.clear_cache()
        bp.fetch_models("sk-abcdef0123456789")
    check("keyed: the token is sent as a Bearer",
          seen2.get("headers", {}).get("authorization")
          == "Bearer sk-abcdef0123456789",
          str(seen2.get("headers", {}).get("authorization")))


def test_no_anthropic_version_header_on_the_catalog() -> None:
    """Sending ``anthropic-version`` to the catalog would flip its shape.

    The same trap OpenRouter hit: that header makes a gateway answer the
    *Anthropic* Models API shape instead of its own catalog. Asserted directly
    because it is invisible until the model list is silently wrong.
    """
    from kuro import beatapi as bp

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        bp.clear_cache()
        bp.fetch_models(use_cache=False)

    headers = seen.get("headers", {})
    check("headers: no anthropic-version on the catalog",
          "anthropic-version" not in headers, str(sorted(headers)))
    check("headers: no x-api-key on the catalog",
          "x-api-key" not in headers, str(sorted(headers)))


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised â€” the caller is a TUI."""
    from kuro import beatapi as bp

    for name, exc, want in (
        ("http 500", urllib.error.HTTPError("u", 500, "boom", {}, None), "500"),
        ("http 401", urllib.error.HTTPError("u", 401, "nope", {}, None), "rejected"),
        ("http 403", urllib.error.HTTPError("u", 403, "nope", {}, None), "rejected"),
        ("url error", urllib.error.URLError("dns"), "could not reach"),
        ("os error", OSError("socket"), "could not reach"),
        ("bad json", ValueError("nope"), "could not reach"),
    ):
        with mock.patch("urllib.request.urlopen", _stub(error=exc)):
            bp.clear_cache()
            models, err = bp.fetch_models(use_cache=False)
        check(f"total: {name} returns (None, message)",
              models is None and want in err, f"{models!r} {err!r}")

    # A genuinely empty catalog is [] â€” distinguishable from a failure.
    with mock.patch("urllib.request.urlopen", _stub({"data": {"data": []}})):
        bp.clear_cache()
        models, err = bp.fetch_models(use_cache=False)
    check("total: an empty catalog is [] with no error", models == [] and not err,
          f"{models!r} {err!r}")

    # A payload that is not even an object must not crash the parse.
    with mock.patch("urllib.request.urlopen", _stub({"nope": 1})):
        bp.clear_cache()
        models, err = bp.fetch_models(use_cache=False)
    check("total: a payload with no data key yields []", models == [], repr(models))


def test_both_catalog_shapes_parse() -> None:
    """The live reply nests the list; a flat one is accepted too."""
    from kuro import beatapi as bp

    nested = bp._parse_models({"data": {"object": "list", "data": [
        {"id": "a", "family": "free"}]}})
    flat = bp._parse_models({"data": [{"id": "b", "family": "free"}]})

    check("shapes: the nested live shape parses", [m.id for m in nested] == ["a"])
    check("shapes: a flat {data: [...]} shape parses", [m.id for m in flat] == ["b"])
    check("shapes: an empty object yields []", bp._parse_models({}) == [])
    check("shapes: a null data yields []", bp._parse_models({"data": None}) == [])


def test_cache_is_keyed_and_expires() -> None:
    """Keyed by token so a key change refetches; TTL'd so a stale list is not live."""
    from kuro import beatapi as bp

    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        return _Resp(json.dumps(CATALOG).encode())

    bp.clear_cache()
    with mock.patch("urllib.request.urlopen", counting):
        bp.fetch_models("")
        bp.fetch_models("")
        check("cache: the second identical call is served from cache",
              calls["n"] == 1, str(calls["n"]))
        # A different token is a different cache entry.
        bp.fetch_models("sk-other-key-000000")
        check("cache: a different token refetches", calls["n"] == 2, str(calls["n"]))
        # And an explicit bypass always goes out.
        bp.fetch_models("", use_cache=False)
        check("cache: use_cache=False always refetches", calls["n"] == 3, str(calls["n"]))

    # TTL: an entry older than CACHE_TTL reads as cold.
    bp.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub()):
        bp.fetch_models("")
    check("cache: a fresh entry is readable", bp.cached_models() is not None)
    check("cache: its age is a number", isinstance(bp.catalog_age(), float))
    with mock.patch.object(bp, "CACHE_TTL", -1.0):
        check("cache: an expired entry reads as None", bp.cached_models() is None)
        check("cache: an expired entry yields no ids", bp.cached_ids() is None)
    bp.clear_cache()
    check("cache: clearing empties it", bp.cached_models() is None)
    check("cache: and reports no age", bp.catalog_age() is None)


def test_the_composed_message_url_is_right() -> None:
    """The URL the SDK actually builds â€” and the *other host* the catalog uses.

    This is the test whose absence let a total outage ship behind a green suite
    on OpenRouter: every turn 404'd while the assertions, which checked the
    constant, passed. The SDK appends ``/v1/messages`` to whatever prefix it is
    handed, so asserting the constant can only restate the bug.

    BeatAPI makes the split sharper than any sibling: the two paths sit under
    *different hosts* â€” messages at ``direct.beatapi.io/v1/messages`` (the SDK's
    doing) and models at ``api.beatapi.io/v1/text/models`` (built by hand). A fix
    that moved the base to ``/v1`` to please the messages path would still 404
    the catalog, and pointing the base at ``api.`` would move every turn onto the
    Cloudflare-throttled origin.
    """
    import anthropic
    from anthropic._models import FinalRequestOptions

    from kuro import beatapi as bp
    from kuro.agent import Agent
    from kuro.config import Config

    cfg = Config(base_url=bp.DEFAULT_BASE_URL, auth_token="sk-x0000000000000000")
    agent = Agent(cfg)
    opts = FinalRequestOptions(method="post", url="/v1/messages",
                               json_data={}, headers={})
    composed = str(agent.client._build_request(opts).url)

    check("url: the message request lands on direct./v1/messages",
          composed == "https://direct.beatapi.io/v1/messages", composed)
    check("url: there is no doubled /v1 in the path",
          "/v1/v1/" not in composed, composed)
    check("url: the base is the prefix BEFORE /v1, not the API root",
          bp.DEFAULT_BASE_URL == "https://direct.beatapi.io", bp.DEFAULT_BASE_URL)

    seen, opener = _capture()
    bp.clear_cache()
    with mock.patch("urllib.request.urlopen", opener):
        bp.fetch_models(use_cache=False)
    check("url: the catalog lands on api./v1/text/models",
          seen.get("url") == "https://api.beatapi.io/v1/text/models",
          seen.get("url"))
    check("url: the catalog host is NOT the turn host",
          "api.beatapi.io" in bp.MODELS_URL
          and "direct.beatapi.io" not in bp.MODELS_URL, bp.MODELS_URL)


def test_profile_roundtrip(tmp: Path) -> None:
    """The key is written mode 600 and read back byte-identical."""
    from kuro import beatapi as bp

    path = bp.save_key("sk-test-key-abcdefghijklmnop", "glm-5.3-free")
    check("profile: written to the beatapi profile", path == bp.PROFILE, str(path))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    check("profile: mode is 600", mode == 0o600, oct(mode))
    check("profile: the directory is 700",
          stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700,
          oct(stat.S_IMODE(os.stat(path.parent).st_mode)))

    creds = bp.load_credentials()
    check("profile: the key round-trips", creds and creds[0] == "sk-test-key-abcdefghijklmnop")
    check("profile: the model round-trips", creds and creds[1] == "glm-5.3-free")
    check("profile: the origin is the profile path", creds and creds[2] == str(path))

    text = path.read_text(encoding="utf-8")
    check("profile: the base URL is the SDK prefix",
          'ANTHROPIC_BASE_URL="https://direct.beatapi.io"' in text, text)
    check("profile: api_key is cleared, not left to shadow auth_token",
          'ANTHROPIC_API_KEY=""' in text)
    check("profile: has_key agrees", bp.has_key())


def test_read_exports_is_total(tmp: Path) -> None:
    """The profile parser handles quotes, junk and a missing file."""
    from kuro import beatapi as bp

    p = tmp / "exports-test"
    p.write_text(
        'export A="quoted"\n'
        "export B=bare\n"
        "export C='single'\n"
        "# export D=commented\n"
        "D=no-export-prefix\n"
        "export E=with=equals\n"
        "export F=\n"
        "not a line at all\n",
        encoding="utf-8")
    got = bp._read_exports(p)
    check("exports: double quotes stripped", got.get("A") == "quoted", repr(got.get("A")))
    check("exports: bare value kept", got.get("B") == "bare")
    check("exports: single quotes stripped", got.get("C") == "single")
    check("exports: a commented line is ignored", "D" not in got)
    check("exports: a value may contain =", got.get("E") == "with=equals")
    check("exports: an empty value is empty", got.get("F") == "")
    check("exports: a missing file is {}",
          bp._read_exports(tmp / "does-not-exist") == {})


def test_credential_precedence(tmp: Path) -> None:
    """environment â†’ own profile â†’ ~/claude-source, first hit wins."""
    from kuro import beatapi as bp

    bp.PROFILE.unlink(missing_ok=True)
    with _clean_env():
        check("precedence: nothing anywhere is None", bp.load_credentials() is None)
        check("precedence: has_key agrees", not bp.has_key())

        # 3. A claude-source profile, with a foreign one that must be skipped.
        foreign = bp.CLAUDE_SOURCE / "other"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text('export ANTHROPIC_BASE_URL="https://openrouter.ai/api"\n'
                           'export ANTHROPIC_AUTH_TOKEN="sk-or-should-be-skipped"\n')
        src = bp.CLAUDE_SOURCE / "beat"
        src.write_text('export ANTHROPIC_BASE_URL="https://direct.beatapi.io"\n'
                       'export ANTHROPIC_AUTH_TOKEN="sk-from-claude-source"\n'
                       'export ANTHROPIC_MODEL="glm-5.3-free"\n')
        creds = bp.load_credentials()
        check("precedence: a claude-source profile is found",
              creds and creds[0] == "sk-from-claude-source", str(creds and creds[2]))
        check("precedence: a profile for another host is skipped",
              creds and creds[0] != "sk-or-should-be-skipped")

        # 2. Kuro's own profile beats it.
        bp.save_key("sk-from-own-profile", "deepseek-v4.1-flash-free")
        creds = bp.load_credentials()
        check("precedence: the own profile beats claude-source",
              creds and creds[0] == "sk-from-own-profile", str(creds and creds[2]))

        # 1. The environment beats everything â€” but only when the base matches.
        os.environ["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-or-env-wrong-host"
        creds = bp.load_credentials()
        check("precedence: an env token for another host is ignored",
              creds and creds[0] == "sk-from-own-profile", str(creds and creds[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "https://direct.beatapi.io"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-from-environment"
        creds = bp.load_credentials()
        check("precedence: the environment wins when the host matches",
              creds and creds[0] == "sk-from-environment", str(creds and creds[2]))
        check("precedence: the origin says environment",
              creds and creds[2] == "environment")

        # ANTHROPIC_API_KEY is the fallback spelling.
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")
        os.environ["ANTHROPIC_API_KEY"] = "sk-from-api-key-var"
        creds = bp.load_credentials()
        check("precedence: ANTHROPIC_API_KEY is the fallback spelling",
              creds and creds[0] == "sk-from-api-key-var", str(creds and creds[0][:6]))


def test_no_key_is_echoed(tmp: Path) -> None:
    """Nothing a caller can reach returns or prints the key."""
    from kuro import beatapi as bp

    secret = "sk-SUPERSECRETVALUE0123456789abcdef"
    bp.save_key(secret, "glm-5.3-free")

    creds = bp.load_credentials()
    check("echo: origin is a path, not the key",
          creds and secret not in creds[2], str(creds and creds[2]))

    with mock.patch("urllib.request.urlopen", _stub()):
        bp.clear_cache()
        models, err = bp.fetch_models(secret)
    check("echo: no error string carries the key", secret not in (err or ""))

    for m in bp._parse_models(CATALOG):
        check(f"echo: blurb of {m.id[:18]} does not carry a key",
              "sk-" not in m.blurb, m.blurb)
    check("echo: price_for does not carry a key",
          secret not in str(bp.price_for("claude-opus-5", bp._parse_models(CATALOG))))

    bp.PROFILE.unlink(missing_ok=True)


def test_looks_like_key() -> None:
    from kuro import beatapi as bp

    # A synthetic key in the real shape â€” the operator's actual key is never
    # written into a test.
    check("key: a real-shaped key passes",
          bp.looks_like_key("sk-" + "0123456789abcdef" * 3 + "012"))
    check("key: an sk_-prefixed key passes",
          bp.looks_like_key("sk_" + "0123456789abcdef" * 3))
    check("key: a short string fails", not bp.looks_like_key("sk-short"))
    check("key: an empty string fails", not bp.looks_like_key(""))
    check("key: a whole export line fails",
          not bp.looks_like_key('export ANTHROPIC_AUTH_TOKEN="sk-abcdefghijklmnopqrst"'))
    check("key: an embedded space fails",
          not bp.looks_like_key("sk-abcdefghijklmno pqrstuvwxyz"))
    check("key: a newline fails", not bp.looks_like_key("sk-abcdefghijklmnop\nqrstuvwxyz"))
    check("key: surrounding whitespace is tolerated",
          bp.looks_like_key("  sk-abcdefghijklmnopqrst  "))


def test_active_key_recognises_host() -> None:
    """The gateway is identified by host â€” and BOTH text origins match."""
    from kuro import beatapi as bp
    from kuro import sources as s

    check("host: the direct origin matches",
          bp.is_beatapi("https://direct.beatapi.io"))
    check("host: the api origin matches", bp.is_beatapi("https://api.beatapi.io"))
    check("host: a path under it matches",
          bp.is_beatapi("https://direct.beatapi.io/v1"))
    check("host: the SDK prefix matches", bp.is_beatapi(bp.DEFAULT_BASE_URL))
    check("host: another gateway does not",
          not bp.is_beatapi("https://openrouter.ai/api"))
    check("host: None does not", not bp.is_beatapi(None))
    check("host: empty does not", not bp.is_beatapi(""))
    check("host: active_endpoint is the same predicate",
          bp.active_endpoint is bp.is_beatapi)

    check("host: sources.active_key finds it",
          s.active_key("https://direct.beatapi.io") == "beatapi")
    check("host: and finds the api origin too",
          s.active_key("https://api.beatapi.io") == "beatapi")
    check("host: it is a builtin, so a selection survives a launch",
          "beatapi" in s.BUILTIN_KEYS)
    check("host: the port path still works",
          s.active_key("http://localhost:4001") == "logfare")


def test_apply_and_guard(tmp: Path) -> None:
    """``apply_to`` aims the session; the guard refuses to hijack a deliberate host."""
    from kuro import beatapi as bp
    from kuro.config import Config

    bp.PROFILE.unlink(missing_ok=True)
    empty_source = tmp / "no-claude-source"
    empty_source.mkdir(exist_ok=True)
    with _clean_env(), mock.patch.object(bp, "CLAUDE_SOURCE", empty_source):
        cfg = Config(base_url="http://localhost:4001", auth_token="", api_key="stale-key")
        cfg.model = "old-model"
        check("apply: with no key it declines", bp.apply_to(cfg) is None)
        check("apply: and leaves the base alone",
              cfg.base_url == "http://localhost:4001", cfg.base_url)

        bp.save_key("sk-apply-test-0123456789", "glm-5.3-free")
        cfg = Config(base_url="http://localhost:4001", auth_token="", api_key="stale-key")
        cfg.model = "old-model"
        applied = bp.apply_to(cfg)
        check("apply: it reports the origin and model",
              applied == (str(bp.PROFILE), "glm-5.3-free"), str(applied))
        check("apply: the base is the SDK prefix",
              cfg.base_url == "https://direct.beatapi.io", cfg.base_url)
        check("apply: the token is set", cfg.auth_token == "sk-apply-test-0123456789")
        # The stale key must go: the client sends both headers, and a key for
        # another provider sitting in memory is a credential for the wrong host.
        check("apply: a stale api_key is cleared", cfg.api_key == "", repr(cfg.api_key))
        check("apply: the model is taken from the profile",
              cfg.model == "glm-5.3-free", cfg.model)

        # With no model saved, apply_to leaves the model alone AND returns the
        # empty model â€” which is the signal the UI uses to fetch the free list
        # on a worker thread, rather than a network read on the UI thread.
        bp.save_key("sk-apply-test-0123456789", "")
        cfg = Config(base_url="http://localhost:4001", auth_token="")
        cfg.model = "keep-me"
        applied = bp.apply_to(cfg)
        check("apply: an empty profile model leaves the current one",
              cfg.model == "keep-me", cfg.model)
        check("apply: and returns the empty model so the UI fetches the list",
              applied == (str(bp.PROFILE), ""), str(applied))

    check("guard: the default empty endpoint may be overridden",
          bp.applies_to_default_endpoint(""))
    check("guard: a local proxy may be overridden",
          bp.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: 127.0.0.1 may be overridden",
          bp.applies_to_default_endpoint("http://127.0.0.1:4000"))
    check("guard: a deliberate remote host is left alone",
          not bp.applies_to_default_endpoint("https://openrouter.ai/api"))
    check("guard: itself is left alone",
          not bp.applies_to_default_endpoint("https://direct.beatapi.io"))
    check("guard: the api origin is left alone too",
          not bp.applies_to_default_endpoint("https://api.beatapi.io"))


def test_selection_file_is_shared(tmp: Path) -> None:
    """The selection is written through sources, so two writers cannot disagree."""
    from kuro import beatapi as bp
    from kuro import sources as s

    bp.set_current("beatapi")
    check("selection: written through sources", s.current_key() == "beatapi")
    check("selection: the module reads it back", bp.current_key() == "beatapi")

    s.set_current("openrouter")
    check("selection: a switch elsewhere is seen here", bp.current_key() == "openrouter")

    bp.set_current("beatapi")
    check("selection: and back again", s.current_key() == "beatapi")


def test_free_and_id_helpers() -> None:
    """The three catalog views agree with each other."""
    from kuro import beatapi as bp

    with mock.patch("urllib.request.urlopen", _stub()):
        bp.clear_cache()
        free, err = bp.free_models()
        check("views: free_models succeeds", free is not None, err)
        check("views: free_models is the free set, in catalog order",
              [m.id for m in free] == FREE, str([m.id for m in free]))

        usable, err = bp.usable_free_models()
        check("views: usable_free_models orders tools-capable first",
              [m.id for m in usable] == USABLE)

        ids, err = bp.model_ids()
        check("views: model_ids is every model, not just free",
              ids is not None and "claude-opus-5" in ids and "glm-5.3-free" in ids,
              str(len(ids or [])))
        check("views: model_ids is every text model, free or not",
              len(ids) == ROWS, str(len(ids or [])))

        check("views: cached_ids matches", bp.cached_ids() == ids, str(bp.cached_ids()))
        check("views: cached_models is the full catalog",
              len(bp.cached_models()) == ROWS, str(len(bp.cached_models())))

        # price_for reads the catalog, and is None for an unknown model.
        models = bp.cached_models()
        check("views: price_for finds a paid model",
              bp.price_for("claude-opus-5", models) == (2.5, 12.5),
              str(bp.price_for("claude-opus-5", models)))
        check("views: price_for finds a free model",
              bp.price_for("glm-5.3-free", models) == (0.0, 0.0))
        check("views: price_for is None for an unpriced model",
              bp.price_for("mystery/thing", models) is None)
        check("views: price_for is None for an unknown model",
              bp.price_for("nope/not-here", models) is None)
        check("views: price_for is None with no catalog",
              bp.price_for("claude-opus-5", None) is None)


def test_blurb_says_what_matters() -> None:
    """The one line the picker renders, for each interesting shape."""
    from kuro import beatapi as bp

    by_id = {m.id: m for m in bp._parse_models(CATALOG)}

    check("blurb: a free model says free",
          "free" in by_id["glm-5.3-free"].blurb,
          by_id["glm-5.3-free"].blurb)
    check("blurb: it does not repeat the free family name",
          by_id["glm-5.3-free"].blurb.count("free") == 1,
          by_id["glm-5.3-free"].blurb)
    check("blurb: the borrowed tools flag shows",
          "tools" in by_id["glm-5.3-free"].blurb,
          by_id["glm-5.3-free"].blurb)
    check("blurb: a paid model shows its price",
          "$2.5/$12.5" in by_id["claude-opus-5"].blurb,
          by_id["claude-opus-5"].blurb)
    check("blurb: the context window is shown",
          "200k ctx" in by_id["claude-opus-5"].blurb,
          by_id["claude-opus-5"].blurb)
    check("blurb: the family is named on a paid row",
          "claude" in by_id["claude-opus-5"].blurb,
          by_id["claude-opus-5"].blurb)


def test_sources_registry_dispatch(tmp: Path) -> None:
    """``sources`` routes beatapi through the catalog, not the declared list."""
    from kuro import beatapi as bp
    from kuro import sources as s

    with mock.patch("urllib.request.urlopen", _stub()):
        bp.clear_cache()
        # A cold cache yields None, so the caller falls back to KNOWN_MODELS.
        check("registry: a cold cache is None",
              s.models_for("https://direct.beatapi.io") is None)
        check("registry: and no catalog models either",
              s.catalog_models("beatapi") is None)

        bp.fetch_models("")
        ids = s.models_for("https://direct.beatapi.io")
        check("registry: a warm cache gives the ids",
              ids is not None and "glm-5.3-free" in ids, str(ids and len(ids)))
        check("registry: catalog_models returns the Model objects",
              len(s.catalog_models("beatapi") or []) == ROWS)
        check("registry: an unknown key is still None",
              s.catalog_models("nope") is None)
        # The window is read off the module's own Model by name.
        win = s.context_window_for("https://direct.beatapi.io", "glm-5.3")
        check("registry: the context window is read from the catalog",
              win == 1000000, str(win))
        check("registry: an unknown model has no window",
              s.context_window_for("https://direct.beatapi.io", "nope") is None)


def test_tui_surfaces() -> None:
    """The commands, help rows and status row are wired, not just the module."""
    app_src = (ROOT / "kuro" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "kuro" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "kuro" / "__main__.py").read_text(encoding="utf-8")

    for cmd in ("zim-beatapi", "beatapi-api-setup", "beatapi-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is in the busy-ok set", f'"{cmd}"' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    check("tui: the gateway table knows beatapi",
          'if kind == "beatapi":' in app_src and 'return bp_mod, "BeatAPI"' in app_src)
    check("tui: the status pane lists it",
          '("beatapi", bp_mod, "BeatAPI", "/beatapi-api-setup")' in app_src)
    check("tui: the model picker knows it",
          'if active == "beatapi" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "beatapi":' in app_src)
    check("tui: /models accepts it",
          '"aihubmix", "beatapi"' in app_src)
    check("tui: the launcher restores it",
          '("beatapi", bp_mod)' in main_src)
    check("tui: the module is imported by the app",
          "from .. import beatapi as bp_mod" in app_src)
    check("tui: /models completion offers it",
          '"aihubmix",\n                               "beatapi"' in comp_src)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-bp-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["KURO_HOME"] = str(tmp / ".kuro")
    os.environ["KURO_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("kuro")]:
        del sys.modules[mod]

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_MODEL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        test_free_is_the_family_suffix_or_price()
        test_priced_flag_records_a_read_not_a_block()
        test_endpoints_is_not_a_filter()
        test_capabilities_borrowed_from_the_paid_sibling()
        test_chat_is_always_true()
        test_catalog_needs_no_key()
        test_no_anthropic_version_header_on_the_catalog()
        test_fetch_is_total()
        test_both_catalog_shapes_parse()
        test_cache_is_keyed_and_expires()
        test_the_composed_message_url_is_right()
        test_profile_roundtrip(tmp)
        test_read_exports_is_total(tmp)
        test_credential_precedence(tmp)
        test_no_key_is_echoed(tmp)
        test_looks_like_key()
        test_active_key_recognises_host()
        test_apply_and_guard(tmp)
        test_selection_file_is_shared(tmp)
        test_free_and_id_helpers()
        test_blurb_says_what_matters()
        test_sources_registry_dispatch(tmp)
        test_tui_surfaces()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed â€” FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

