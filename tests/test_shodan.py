"""`/shodan-*` regression suite: the key, the query, the result, the turn.

Run:  python tests/test_shodan.py

Offline and model-free. Four layers:

* **the key** — that `/shodan-api-setup` writes a mode-600 profile every other
  credential in the project can read, that a key set this session beats a stale
  environment, and that a pasted `export` line is refused;
* **the query** — that a facet composes the query it says it does, country and
  limit included, and that the raw facet stays verbatim;
* **the result** — an actual ``urlopen`` stub: parsing, the fields dropped on
  purpose, the limit clamp, and the words each HTTP error turns into;
* **the turn** — that the prompt handed to the model carries the evidence and
  the instruction to *ask* rather than act, and that it never carries a banner.

The one invariant worth stating: **a banner never reaches the result.** Shodan
returns a host's raw ``data`` alongside its metadata, and that blob routinely
holds credentials and personal data. ``_parse_host`` must reduce a match to the
triaging fields and nothing else — a regression here is a secret dump, not a
cosmetic bug, so it is asserted directly.
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

# A throwaway ZIMZILLA_HOME for the whole suite. The module resolves the path at
# import time, so this has to be set before the import below — otherwise the key
# this suite writes lands in the operator's live install.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="zim-test-home-"))
os.environ["ZIMZILLA_HOME"] = str(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from zimzilla import shodan as shodan_mod  # noqa: E402

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

    # The session key wins over the file and the environment — the correction
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
# the query
# ---------------------------------------------------------------------------

def test_facet_queries() -> None:
    cam = shodan_mod.FACETS["cam"]
    q = cam.build_query(country="us", extra="product:Bosch")
    check("query: cam composes country and extra",
          q.startswith("tag:webcam has_screenshot:true")
          and "country:US" in q and "product:Bosch" in q, q)
    check("query: cloud noise is filtered", "-org:Shodan" in q, q)

    bare = cam.build_query()
    check("query: no country means no country clause",
          "country:" not in bare, bare)

    # Every named facet builds a non-empty query, so none of them can send an
    # empty search and return the whole index.
    for name, facet in shodan_mod.FACETS.items():
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


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------

def test_search_parses() -> None:
    payload = {"total": 4321, "matches": [_match(), _match(ip_str="198.51.100.9",
                                                            port=8080)]}
    with mock.patch("urllib.request.urlopen", return_value=_Resp(payload)):
        result, err = shodan_mod.search("k" * 32, "tag:webcam", limit=20)
    check("result: no error", err == "", err)
    check("result: total is kept", result is not None and result.total == 4321)
    check("result: hosts parsed", result is not None and len(result.hosts) == 2)
    h = result.hosts[0]
    check("result: address parsed", h.endpoint == "203.0.113.7:80", h.endpoint)
    check("result: identity is product+version", h.identity == "Hipcam 1.0", h.identity)
    check("result: country parsed", h.country == "Netherlands", h.country)
    check("result: vulns kept as a tuple", h.vulns == ("CVE-2019-0001",), str(h.vulns))
    # The invariant: no banner, no cookie, no secret reaches the Host.
    blob = json.dumps([h.__dict__ for h in result.hosts])
    check("result: the banner never survives parsing",
          "tops3cret" not in blob and "Set-Cookie" not in blob, blob[:80])


def test_search_limit_and_shape() -> None:
    payload = {"total": 3, "matches": [_match(ip_str=f"10.0.0.{i}") for i in range(3)]}
    with mock.patch("urllib.request.urlopen", return_value=_Resp(payload)):
        result, _ = shodan_mod.search("k" * 32, "x", limit=1)
    check("result: limit truncates the page", len(result.hosts) == 1)

    # A match with no address is dropped rather than crashing the page.
    payload = {"total": 2, "matches": [_match(), {"port": 22}]}
    with mock.patch("urllib.request.urlopen", return_value=_Resp(payload)):
        result, err = shodan_mod.search("k" * 32, "x")
    check("result: an addressless match is skipped, the rest survive",
          err == "" and len(result.hosts) == 1, f"{err} {len(result.hosts)}")

    # A 200 with an `error` body is an error, not an empty success.
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp({"error": "Invalid query"})):
        result, err = shodan_mod.search("k" * 32, "x")
    check("result: an error body is an error", result is None and "Invalid query" in err,
          err)


def test_search_errors() -> None:
    def _raise(code: int):
        def _inner(*a, **k):
            raise urllib.error.HTTPError("http://x", code, "nope", {}, None)
        return _inner

    for code, needle in ((401, "key rejected"), (403, "plan"), (429, "rate limited")):
        with mock.patch("urllib.request.urlopen", side_effect=_raise(code)):
            _, err = shodan_mod.search("k" * 32, "x")
        check(f"error: HTTP {code} says something useful",
              needle in err and "k" * 32 not in err, err)

    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("no dns")):
        _, err = shodan_mod.search("k" * 32, "x")
    check("error: a network failure is reported, not raised",
          "network error" in err, err)

    with mock.patch("urllib.request.urlopen", return_value=_Resp("not json")):
        _, err = shodan_mod.search("k" * 32, "x")
    check("error: a non-JSON body is reported", "not JSON" in err, err)


# ---------------------------------------------------------------------------
# the turn
# ---------------------------------------------------------------------------

def test_prompt() -> None:
    payload = {"total": 12, "matches": [_match()]}
    with mock.patch("urllib.request.urlopen", return_value=_Resp(payload)):
        result, _ = shodan_mod.search("k" * 32, "tag:webcam")
    prompt = shodan_mod.build_prompt(shodan_mod.FACETS["cam"], result, country="US")

    check("prompt: carries the query", "tag:webcam" in prompt)
    check("prompt: carries the host endpoint", "203.0.113.7:80" in prompt)
    check("prompt: carries the CVE", "CVE-2019-0001" in prompt)
    check("prompt: says nothing here is in scope",
          "nothing in this list is in scope" in prompt.lower())
    check("prompt: tells the model not to run tools against the hosts",
          "Do NOT run" in prompt)
    check("prompt: asks for proposed tasks", "task" in prompt.lower())
    check("prompt: asks the operator to choose, not act",
          "which task" in prompt.lower())
    check("prompt: carries no banner",
          "tops3cret" not in prompt and "Set-Cookie" not in prompt)

    check("summary: one line per host", len(shodan_mod.summary_lines(result)) == 1)
    check("summary: names the endpoint",
          "203.0.113.7:80" in shodan_mod.summary_lines(result)[0])


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    """Every command is reachable from every surface that lists commands.

    Read from the source rather than importing a copy, so a rename cannot
    silently orphan a command — the same check test_osint and test_crack make.
    ``app.py`` and ``complete.py`` are read as text because importing them needs
    textual, which this suite does not require.
    """
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    complete_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text()

    check("wiring: shodan.py is imported",
          "from .. import shodan as shodan_mod" in app_src)
    check("wiring: _cmd_shodan_key is defined", "def _cmd_shodan_key(" in app_src)
    check("wiring: _cmd_shodan_search is defined", "def _cmd_shodan_search(" in app_src)

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

    # A search starts a turn, so it must not be busy-ok — letting one through
    # the gate would interleave a second turn into a live one. Read the BUSY_OK
    # block out of app.py and assert none of the searches leaked into it.
    busy_block = app_src.split("BUSY_OK = frozenset({", 1)[1].split("})", 1)[0]
    leaked = [c for c in commands if f'"{c}"' in busy_block]
    check("wiring: no search is busy-ok", leaked == [], str(leaked))


def main() -> int:
    test_key_roundtrip()
    test_key_env_fallback()
    test_facet_queries()
    test_search_parses()
    test_search_limit_and_shape()
    test_search_errors()
    test_prompt()
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
