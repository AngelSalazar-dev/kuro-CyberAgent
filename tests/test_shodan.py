"""`/shodan-*` regression suite: the key, the plan, the query, the facets, the turn.

Run:  python tests/test_shodan.py

Offline and model-free. Five layers:

* **the key** â€” that `/shodan-api-setup` writes a mode-600 profile every other
  credential in the project can read, that a key set this session beats a stale
  environment, and that a pasted `export` line is refused;
* **the plan** â€” that a free key is *known* to be free before a search is
  attempted, so the run degrades to facets instead of failing with a 403;
* **the query** â€” that a facet composes the query it says it does, country and
  extra included, and that no facet uses `OR`, which Shodan rejects;
* **the result** â€” an actual ``urlopen`` stub: totals, facets, the limit clamp,
  the error bodies, and the fields dropped on purpose;
* **the turn** â€” that the prompt carries the evidence and the instruction to
  *ask* rather than act, and that it tells the model when there are no host rows
  so it cannot invent them.

Two invariants worth stating, because they are the ones a regression would
break silently:

* **A banner never reaches the result.** Shodan returns a host's raw ``data``
  alongside its metadata, and that blob routinely holds credentials and
  personal data. ``_parse_host`` must reduce a match to the triaging fields and
  nothing else â€” a regression here is a secret dump, not a cosmetic bug.
* **A free key still answers.** The whole point of the count path is that
  ``/shodan/host/search`` being gated does not make the command useless. A test
  that only ever stubs a successful search would not notice the feature breaking
  for the plan most operators are on.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A throwaway KURO_HOME for the whole suite. The module resolves the path at
# import time, so this has to be set before the import below â€” otherwise the key
# this suite writes lands in the operator's live install.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="zim-test-home-"))
os.environ["KURO_HOME"] = str(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from kuro import shodan as shodan_mod  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


class _Resp:
    """A minimal stand-in for ``urlopen``'s context manager."""

    def __init__(self, payload: dict | str) -> None:
        self._body = (payload if isinstance(payload, str)
                      else json.dumps(payload)).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _match(**kw) -> dict:
    base = {
        "ip_str": "203.0.113.7",
        "port": 80,
        "org": "Example ISP",
        "product": "Hipcam",
        "version": "1.0",
        "location": {"country_name": "Netherlands", "city": "Amsterdam"},
        "hostnames": ["cam.example.net"],
        "tags": ["webcam"],
        "vulns": {"CVE-2019-0001": {}},
        "data": "HTTP/1.1 200 OK\r\nSet-Cookie: admin=tops3cret",
    }
    base.update(kw)
    return base


def _count(total: int, facets: dict | None = None) -> dict:
    return {"matches": [], "total": total, "facets": facets or {}}


#: A realistic free-plan facet payload: every bucket Shodan actually returns.
_FACETS = {
    "country": [{"value": "US", "count": 900}, {"value": "NL", "count": 120}],
    "product": [{"value": "Hikvision IP Camera", "count": 800},
                {"value": "Dahua-based DVR", "count": 90}],
    "port": [{"value": 554, "count": 700}, {"value": 80, "count": 210}],
    "org": [{"value": "Hetzner Online GmbH", "count": 300}],
    "os": [{"value": "Linux", "count": 40}],
    "version": [{"value": "1.0", "count": 12}],
}


# ---------------------------------------------------------------------------
# the key
# ---------------------------------------------------------------------------

def test_key_roundtrip() -> None:
    key = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    path = shodan_mod.save_key(key)
    check("key: written where the module says",
          path == shodan_mod.PROFILE, str(path))
    mode = path.stat().st_mode & 0o777
    check("key: mode 600", mode == 0o600, oct(mode))

    # The file is the same source format as every other profile in the project.
    text = path.read_text()
    check("key: written as a sourceable export",
          text.strip() == f'export SHODAN_API_KEY="{key}"', text.strip())

    # The session key wins over the file and the environment â€” the correction
    # for an environment snapshot taken before the operator typed a new key.
    shodan_mod.remember_session_key("f" * 32)
    creds = shodan_mod.load_credentials()
    check("key: a session key wins", creds is not None and creds[0] == "f" * 32,
          str(creds and creds[1]))

    # Reset the in-memory override so the remaining checks read the file.
    shodan_mod._SESSION_KEY = None
    creds = shodan_mod.load_credentials()
    check("key: the profile round-trips", creds is not None and creds[0] == key,
          str(creds and creds[1]))

    check("key: looks_like_key accepts a hex key",
          shodan_mod.looks_like_key("0" * 32))
    check("key: looks_like_key accepts a quoted key",
          shodan_mod.looks_like_key('"' + "0" * 32 + '"'))
    check("key: looks_like_key rejects a pasted shell line",
          not shodan_mod.looks_like_key('export SHODAN_API_KEY="0"*32'))
    check("key: looks_like_key rejects a short word",
          not shodan_mod.looks_like_key("hunter2"))
    check("key: looks_like_key rejects whitespace",
          not shodan_mod.looks_like_key("a" * 16 + " b"))


def test_key_env_fallback() -> None:
    # No session key, and the file removed: the environment is the last resort.
    shodan_mod._SESSION_KEY = None
    saved = shodan_mod.PROFILE.read_text()
    shodan_mod.PROFILE.unlink()
    try:
        with mock.patch.dict(os.environ, {"SHODAN_API_KEY": "e" * 32}):
            creds = shodan_mod.load_credentials()
        check("key: environment is the fallback",
              creds is not None and creds[0] == "e" * 32 and creds[1] == "environment",
              str(creds))
    finally:
        shodan_mod.PROFILE.write_text(saved)
    shodan_mod.PROFILE.chmod(0o600)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

def test_plan() -> None:
    # The real shape of /api-info on the free plan â€” query_credits is 0 and the
    # plan name is "oss". This is what the operator's key actually returned.
    oss = {"plan": "oss", "query_credits": 0, "scan_credits": 0,
           "unlocked": False,
           "usage_limits": {"query_credits": 0, "scan_credits": 0,
                            "monitored_ips": 0}}
    with mock.patch("urllib.request.urlopen", return_value=_Resp(oss)):
        plan, err = shodan_mod.fetch_plan("k" * 32)
    check("plan: /api-info parsed", plan is not None and err == "", err)
    check("plan: oss is named", plan.name == "oss", plan.name)
    check("plan: oss cannot search", plan.search is False)
    check("plan: the blurb says counts only",
          "counts and facets only" in plan.blurb, plan.blurb)

    member = {"plan": "membership", "query_credits": 100,
              "usage_limits": {"query_credits": 100, "scan_credits": 1}}
    with mock.patch("urllib.request.urlopen", return_value=_Resp(member)):
        plan, _ = shodan_mod.fetch_plan("k" * 32)
    check("plan: membership can search", plan.search is True, plan.name)

    # A plan with no credits left cannot search, whatever it is called â€” the
    # balance is the truth, not the label.
    spent = {"plan": "dev", "query_credits": 0,
             "usage_limits": {"query_credits": 0}}
    with mock.patch("urllib.request.urlopen", return_value=_Resp(spent)):
        plan, _ = shodan_mod.fetch_plan("k" * 32)
    check("plan: an exhausted balance cannot search", plan.search is False,
          f"{plan.name} {plan.query_credits}")


# ---------------------------------------------------------------------------
# the query
# ---------------------------------------------------------------------------

def test_facet_queries() -> None:
    cam = shodan_mod.FACETS["cam"]
    q = cam.build_query(country="us", extra="port:554")
    check("query: cam composes country and extra",
          "has_screenshot:true" in q and "country:US" in q and "port:554" in q, q)
    check("query: cloud noise is filtered", "-org:Shodan" in q, q)

    bare = cam.build_query()
    check("query: no country means no country clause",
          "country:" not in bare, bare)

    # Shodan rejects `OR` as invalid syntax; a facet that uses it would 400 on
    # every plan, including a paid one. Multiple values are comma-joined.
    for name, facet in shodan_mod.FACETS.items():
        check(f"query: {name} never uses OR", " OR " not in facet.query,
              facet.query)
        check(f"query: {name} is non-empty", bool(facet.build_query().strip()),
              facet.build_query())

    check("query: facets have distinct commands",
          len({f.command for f in shodan_mod.FACETS.values()}) == len(shodan_mod.FACETS))
    check("query: there are at least four named searches",
          len(shodan_mod.FACETS) >= 4, str(len(shodan_mod.FACETS)))
    check("query: facet_for maps a command back to its facet",
          shodan_mod.facet_for("shodan-cam-search") is cam)
    check("query: the vuln facet takes a free query",
          shodan_mod.VULN_FACET.build_query(extra="product:Apache").startswith(
              "has_vuln:true"))

    # `tag:webcam` is gone from Shodan's index â€” a camera facet keyed on it
    # would silently return nothing, which is the bug this asserts against.
    check("query: the camera facet does not rely on the dead webcam tag",
          "tag:webcam" not in shodan_mod.FACETS["cam"].query,
          shodan_mod.FACETS["cam"].query)
    check("query: every facet carries the question it is asking",
          all(f.question for f in shodan_mod.FACETS.values()))


# ---------------------------------------------------------------------------
# the result â€” a free key, which is the case that used to fail
# ---------------------------------------------------------------------------

def test_free_plan_still_answers() -> None:
    oss = shodan_mod.Plan(name="oss", query_credits=0)
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(_count(1032, _FACETS))):
        result, err = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"],
                                          "has_screenshot:true", plan=oss)
    check("free: the count answers", err == "" and result is not None, err)
    check("free: the total is kept", result.total == 1032, str(result.total))
    check("free: facets are parsed", bool(result.facets), str(result.facets.keys()))
    check("free: no host rows, and that is not an error", result.hosts == [])
    check("free: the plan is recorded", result.plan.name == "oss")

    top = result.top("product")
    check("free: facets are sorted biggest-first",
          top and top[0].value == "Hikvision IP Camera", str(top))
    check("free: top() honours its limit", len(result.top("country", 1)) == 1)
    # A port facet arrives as an int and must become a string, not "554 " or 554.
    check("free: an integer facet value is normalised",
          result.top("port")[0].value == "554", result.top("port")[0].value)

    # The whole point: the search endpoint is never called on a free plan, so a
    # 403 cannot turn a good facet answer into a failed command.
    calls: list[str] = []
    real = shodan_mod._get

    def _spy(path, params, timeout):
        calls.append(path)
        return real(path, params, timeout)

    with mock.patch.object(shodan_mod, "_get", side_effect=_spy), \
         mock.patch("urllib.request.urlopen",
                    return_value=_Resp(_count(10, _FACETS))):
        shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"], "x", plan=oss)
    check("free: host search is not attempted", "/shodan/host/search" not in calls,
          str(calls))
    check("free: the count is attempted", "/shodan/host/count" in calls, str(calls))


def test_paid_plan_gets_rows() -> None:
    paid = shodan_mod.Plan(name="membership", query_credits=100)
    payloads = [_count(500, _FACETS),
                {"total": 500, "matches": [_match(),
                                           _match(ip_str="198.51.100.9", port=8080)]}]
    with mock.patch("urllib.request.urlopen", side_effect=[_Resp(p) for p in payloads]):
        result, err = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"],
                                          "x", plan=paid, limit=20)
    check("paid: no error", err == "", err)
    check("paid: rows parsed", len(result.hosts) == 2, str(len(result.hosts)))
    h = result.hosts[0]
    check("paid: address parsed", h.endpoint == "203.0.113.7:80", h.endpoint)
    check("paid: identity is product+version", h.identity == "Hipcam 1.0", h.identity)
    check("paid: country parsed", h.country == "Netherlands", h.country)
    check("paid: vulns kept as a tuple", h.vulns == ("CVE-2019-0001",), str(h.vulns))
    check("paid: facets survive alongside rows", result.total == 500)

    # The invariant: no banner, no cookie, no secret reaches the Host.
    blob = json.dumps([h.__dict__ for h in result.hosts])
    check("paid: the banner never survives parsing",
          "tops3cret" not in blob and "Set-Cookie" not in blob, blob[:80])

    # A search that fails after a good count keeps the facet answer and says
    # why the rows are missing â€” a degradation, not a failure. An exception
    # *instance* in a side_effect list is raised; a function would be returned
    # as the response, which is not what a gated endpoint does.
    gated = urllib.error.HTTPError("http://x", 403, "nope", {}, None)
    with mock.patch("urllib.request.urlopen",
                    side_effect=[_Resp(_count(9, _FACETS)), gated]):
        result, err = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"],
                                          "x", plan=paid)
    check("paid: a gated search degrades to facets",
          err == "" and result is not None and result.total == 9, err)
    check("paid: the reason is recorded", "403" in result.search_error,
          result.search_error)


def test_result_errors() -> None:
    # A 200 with an `error` body is an error, not an empty success.
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp({"error": "Invalid query"})):
        result, err = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"], "x")
    check("error: an error body is an error",
          result is None and "Invalid query" in err, err)

    def _raise(code: int):
        def _inner(*a, **k):
            raise urllib.error.HTTPError("http://x", code, "nope", {}, None)
        return _inner

    for code, needle in ((401, "401"), (403, "403"), (429, "429")):
        with mock.patch("urllib.request.urlopen", side_effect=_raise(code)):
            _, err = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"], "x")
        check(f"error: HTTP {code} is reported without the key",
              needle in err and "k" * 32 not in err, err)

    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("no dns")):
        _, err = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"], "x")
    check("error: a network failure is reported, not raised",
          "network error" in err, err)

    with mock.patch("urllib.request.urlopen", return_value=_Resp("not json")):
        _, err = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"], "x")
    check("error: a non-JSON body is reported", "not JSON" in err, err)


# ---------------------------------------------------------------------------
# the turn
# ---------------------------------------------------------------------------

def test_prompt_free() -> None:
    """The free-plan prompt must say there are no rows, or the model invents them."""
    oss = shodan_mod.Plan(name="oss", query_credits=0)
    query = shodan_mod.FACETS["cam"].build_query(country="US")
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(_count(1032, _FACETS))):
        result, _ = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"], query,
                                        plan=oss)
    prompt = shodan_mod.build_prompt(shodan_mod.FACETS["cam"], result, country="US")

    check("prompt: carries the query", "has_screenshot:true" in prompt)
    check("prompt: carries the total", "1,032" in prompt)
    check("prompt: carries the facet shape", "Hikvision IP Camera" in prompt)
    check("prompt: names the plan", "oss" in prompt)
    check("prompt: says there are no host records",
          "You do NOT have host records" in prompt)
    check("prompt: forbids inventing addresses",
          "Do not invent addresses" in prompt)
    check("prompt: says nothing here is in scope",
          "nothing in this list is in scope" in prompt.lower()
          or "nothing here is in scope" in prompt.lower(), prompt[:300])
    check("prompt: tells the model not to run tools against the hosts",
          "Do NOT run" in prompt)
    check("prompt: asks for proposed tasks", "task" in prompt.lower())
    check("prompt: asks the operator to choose, not act",
          "which task" in prompt.lower())
    check("prompt: carries no banner",
          "tops3cret" not in prompt and "Set-Cookie" not in prompt)


def test_prompt_paid() -> None:
    paid = shodan_mod.Plan(name="membership", query_credits=100)
    payloads = [_count(500, _FACETS), {"total": 500, "matches": [_match()]}]
    with mock.patch("urllib.request.urlopen", side_effect=[_Resp(p) for p in payloads]):
        result, _ = shodan_mod.describe("k" * 32, shodan_mod.FACETS["cam"], "x",
                                        plan=paid)
    prompt = shodan_mod.build_prompt(shodan_mod.FACETS["cam"], result)
    check("prompt: a paid plan gets its rows", "203.0.113.7:80" in prompt)
    check("prompt: a paid plan gets the CVE", "CVE-2019-0001" in prompt)
    check("prompt: a paid plan is told to cite addresses",
          "Cite specific addresses" in prompt)
    check("prompt: a paid plan is not told it has no rows",
          "You do NOT have host records" not in prompt)
    check("summary: one line per host", len(shodan_mod.summary_lines(result)) == 1)
    check("summary: names the endpoint",
          "203.0.113.7:80" in shodan_mod.summary_lines(result)[0])


# ---------------------------------------------------------------------------
# the environment note â€” the model must know where the key lives
# ---------------------------------------------------------------------------

def test_environment_note() -> None:
    """The system prompt must name the Shodan profile, in every mode.

    This is the fix for a real failure: asked to run a Shodan search, the model
    replied that there was no SHODAN_API_KEY in the environment and no shodan
    CLI installed, while the key sat on disk and four commands already read it.
    A prompt that does not say where the key is invites that answer back.
    """
    from kuro.agent import Agent
    from kuro.config import Config

    # A key on disk, and a prompt built the way a live session builds it.
    shodan_mod.save_key("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    shodan_mod._SESSION_KEY = None

    cfg = Config.from_env(workdir=Path("/tmp"), api_key="x",
                          base_url="http://localhost:4001", mode="auto")
    prompt = Agent(cfg).system_prompt()

    check("env: the note names the state directory",
          str(shodan_mod.PROFILE.parent.parent) in prompt, "")
    check("env: the note names the shodan profile",
          "/shodan/source" in prompt)
    check("env: the note tells the model the key is set",
          "a key is set" in prompt)
    check("env: the note names the /shodan-* commands",
          "/shodan-cam-search" in prompt)
    check("env: the note warns not to ask for an export",
          "do not ask for the key to be exported" in prompt)
    check("env: the note explains the free-plan gate",
          "403" in prompt and "count" in prompt)
    check("env: the note names the other profiles too",
          "/tokenharbour/source" in prompt and "/ldxhub/source" in prompt)

    # Every mode must carry it, not just the default one â€” the failure could
    # happen in any of them.
    for mode in ("plan", "edits", "zim", "danger", "uncensored"):
        cfg = Config.from_env(workdir=Path("/tmp"), api_key="x",
                              base_url="http://localhost:4001", mode=mode)
        p = Agent(cfg).system_prompt()
        check(f"env: {mode} mode carries the note", "/shodan/source" in p)

    # With the key deleted the note must say so rather than claim one exists â€”
    # stale "a key is set" is worse than no note at all.
    saved = shodan_mod.PROFILE.read_text()
    shodan_mod.PROFILE.unlink()
    try:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHODAN_API_KEY", None)
            cfg = Config.from_env(workdir=Path("/tmp"), api_key="x",
                                  base_url="http://localhost:4001", mode="auto")
            p = Agent(cfg).system_prompt()
        check("env: with no key the note says so", "no key set yet" in p)
    finally:
        shodan_mod.PROFILE.write_text(saved)
        shodan_mod.PROFILE.chmod(0o600)


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    """Every command is reachable from every surface that lists commands.

    Read from the source rather than importing a copy, so a rename cannot
    silently orphan a command â€” the same check test_osint and test_crack make.
    ``app.py`` and ``complete.py`` are read as text because importing them needs
    textual, which this suite does not require.
    """
    app_src = (ROOT / "kuro" / "ui" / "app.py").read_text()
    complete_src = (ROOT / "kuro" / "ui" / "complete.py").read_text()

    check("wiring: shodan.py is imported",
          "from .. import shodan as shodan_mod" in app_src)
    check("wiring: _cmd_shodan_key is defined", "def _cmd_shodan_key(" in app_src)
    check("wiring: _cmd_shodan_search is defined", "def _cmd_shodan_search(" in app_src)
    check("wiring: the plan is fetched on setup",
          "def _check_shodan_plan(" in app_src)
    check("wiring: the plan is cached per session",
          "_shodan_plan" in app_src)

    commands = [f.command for f in shodan_mod.FACETS.values()]
    commands += [shodan_mod.VULN_FACET.command, shodan_mod.RAW_FACET.command]
    for cmd in commands:
        check(f"wiring: dispatch has {cmd}", f'"{cmd}": lambda a:' in app_src)
        check(f"wiring: help lists {cmd}", f'("/{cmd}"' in app_src)
        check(f"wiring: palette lists {cmd}", f'("/{cmd}"' in app_src)
        check(f"wiring: completion lists {cmd}", f'("/{cmd}"' in complete_src)

    check("wiring: setup is in the completion menu",
          '("/shodan-api-setup"' in complete_src)
    check("wiring: setup is allowed while busy",
          '"shodan-api-setup",' in app_src)

    # A search starts a turn, so it must not be busy-ok â€” letting one through
    # the gate would interleave a second turn into a live one. Read the BUSY_OK
    # block out of app.py and assert none of the searches leaked into it.
    busy_block = app_src.split("BUSY_OK = frozenset({", 1)[1].split("})", 1)[0]
    leaked = [c for c in commands if f'"{c}"' in busy_block]
    check("wiring: no search is busy-ok", leaked == [], str(leaked))


def main() -> int:
    test_key_roundtrip()
    test_key_env_fallback()
    test_plan()
    test_facet_queries()
    test_free_plan_still_answers()
    test_paid_plan_gets_rows()
    test_result_errors()
    test_prompt_free()
    test_prompt_paid()
    test_environment_note()
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

