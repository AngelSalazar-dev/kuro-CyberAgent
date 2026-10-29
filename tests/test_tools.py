"""The tool suite: every agent tool, executed — plus the harness wiring around it.

Run:  python tests/test_tools.py   (from the checkout's venv)

Two kinds of check. The offline ones (wiring, mode gating, the shell, the
filesystem, argument validation) always run. The ``LIVE`` ones push real
traffic through `search_web` and `web_fetch`, because a tool that parses
arguments and then cannot reach the network is not usable — that is exactly
what the missing-CA-bundle bug looked like: every https call failed with
CERTIFICATE_VERIFY_FAILED before a byte was sent. Live failures count in the
exit code, so this suite cannot go green with a broken web tool.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A throwaway KURO_HOME for the whole suite, set before the imports below: the
# package resolves it at import time (config.DEFAULT_HOME, ldxhub.PROFILE), and
# without it the ldxhub assertions would read the operator's real key — which
# would turn a check into a paid job against a live gateway.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="zim-test-tools-"))
os.environ["KURO_HOME"] = str(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from kuro import tools  # noqa: E402
from kuro.agent import Agent  # noqa: E402
from kuro.config import Config, MODES  # noqa: E402
from kuro.scope import Scope  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
LIVE: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def check_live(name: str, ok: bool, extra: str = "") -> None:
    LIVE.append((name, ok, extra))
    print(f"{'LIVE PASS' if ok else 'LIVE FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


# ---- stub blocks (the same shape test_phase4 drives run_turn with) ---------

class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


# ---------------------------------------------------------------------------
# Wiring: what is advertised, what can run, and whether https can work at all
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    advertised = {t["name"] for t in tools.TOOL_SCHEMAS} | {
        t["name"] for t in tools.PEER_SCHEMAS}
    dispatched = set(tools._DISPATCH)

    check("every advertised tool has an implementation",
          advertised == dispatched,
          f"missing={sorted(advertised - dispatched)} "
          f"orphan={sorted(dispatched - advertised)}")
    check("the nine core tools are all advertised",
          {"bash", "read_file", "write_file", "edit_file", "glob", "grep",
           "list_dir", "search_web", "web_fetch"} <= advertised)

    for schema in tools.TOOL_SCHEMAS + tools.PEER_SCHEMAS:
        body = schema.get("input_schema") or {}
        ok = (bool(schema.get("name")) and bool(schema.get("description"))
              and body.get("type") == "object")
        if not ok:
            check(f"schema {schema.get('name')} is well formed", False,
                  str(schema)[:80])
    check("every schema carries a name, a description and an object schema",
          all((t.get("name") and t.get("description")
               and (t.get("input_schema") or {}).get("type") == "object")
              for t in tools.TOOL_SCHEMAS + tools.PEER_SCHEMAS))

    check("the gated tools are all real tools",
          tools.GATED_TOOLS <= dispatched, str(sorted(tools.GATED_TOOLS)))
    # A label needs real arguments — with {} the path/label branches are empty
    # by construction. One probe dict carrying every field the branches read.
    probe = {"command": "ls -la", "path": "pkg/mod.py", "pattern": "*.py",
             "query": "how to", "url": "https://example.com", "title": "finding",
             "file_path": "doc.pdf", "service": "list", "prompt": "summarise",
             "to": "peer", "body": "hi", "severity": "high"}
    unlabeled = [n for n in sorted(dispatched)
                 if not tools.summarise_call(n, probe, None)]
    check("summarise_call labels every tool", not unlabeled, str(unlabeled))


def test_ca_bundle() -> None:
    """Pin the CA fix: without a bundle every https tool call dies pre-flight.

    The platform's own bundle (a cafile, or a capath directory) counts, and so
    does an SSL_CERT_FILE the operator pointed at a real file — the harness
    only ever fills a hole, it never replaces a working trust store.
    """
    paths = ssl.get_default_verify_paths()
    cafile = os.environ.get(paths.openssl_cafile_env) or paths.cafile
    capath = os.environ.get(paths.openssl_capath_env) or paths.capath
    usable = bool((cafile and os.path.exists(cafile))
                  or (capath and os.path.isdir(capath)))
    check("harness left the standard library a usable CA bundle",
          usable, f"cafile={cafile!r} capath={capath!r}")


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

def test_bash(wd: Path) -> None:
    cfg = _cfg(wd)

    ok = tools.execute("bash", {"command": "echo tool-harness-ok"}, cfg)
    check("bash runs a command", not ok.is_error
          and "tool-harness-ok" in ok.output, ok.output[:60])
    check("bash reports a zero exit", ok.meta.get("exit") == 0, str(ok.meta))

    bad = tools.execute("bash", {"command": "exit 3"}, cfg)
    check("bash reports a failing exit as an error",
          bad.is_error and bad.meta.get("exit") == 3, str(bad.meta))

    empty = tools.execute("bash", {"command": "   "}, cfg)
    check("bash refuses an empty command", empty.is_error, empty.output[:40])

    bad_timeout = tools.execute("bash", {"command": "echo x", "timeout": "soon"}, cfg)
    check("bash refuses a non-integer timeout", bad_timeout.is_error,
          bad_timeout.output[:50])
    # ``timeout or cfg.bash_timeout`` means 0 arrives as "use the default" —
    # what matters is that it can never mean *no* timeout, which would leave
    # subprocess.run unbounded. A negative number does reach the guard.
    zero = tools.execute("bash", {"command": "echo x", "timeout": 0}, cfg)
    check("bash never reads timeout 0 as 'no timeout'",
          not zero.is_error and zero.meta.get("timeout") is False, str(zero.meta))
    negative = tools.execute("bash", {"command": "echo x", "timeout": -1}, cfg)
    check("bash refuses a negative timeout", negative.is_error,
          negative.output[:50])

    slow = tools.execute("bash", {"command": _py_command("import time; time.sleep(5)"),
                                  "timeout": 1}, cfg)
    check("bash enforces its timeout instead of hanging",
          slow.is_error and slow.meta.get("timeout") is True,
          f"{slow.output[:60]} {slow.meta}")


def _py_command(code: str) -> str:
    """`python -c <code>` quoted for whichever shell subprocess.run will use."""
    if os.name == "nt":
        return f'"{sys.executable}" -c "{code}"'
    import shlex

    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


# ---------------------------------------------------------------------------
# read_file / write_file / edit_file
# ---------------------------------------------------------------------------

def test_files(wd: Path) -> None:
    cfg = _cfg(wd)
    src = wd / "sample.txt"
    src.write_text("alpha\nbeta\ngamma\n")

    # ---- read_file
    got = tools.execute("read_file", {"path": "sample.txt"}, cfg)
    check("read_file returns the content with line numbers",
          not got.is_error and "alpha" in got.output and "gamma" in got.output,
          got.output[:70])
    check("read_file counts the lines", got.meta.get("lines") == 3, str(got.meta))

    part = tools.execute("read_file", {"path": "sample.txt", "offset": 2, "limit": 1}, cfg)
    check("read_file honours offset and limit",
          not part.is_error and "beta" in part.output and "alpha" not in part.output,
          part.output[:70])

    missing = tools.execute("read_file", {"path": "nope.txt"}, cfg)
    check("read_file reports a missing file", missing.is_error, missing.output[:60])

    as_dir = tools.execute("read_file", {"path": "."}, cfg)
    check("read_file refuses a directory", as_dir.is_error, as_dir.output[:60])

    # ---- write_file
    made = tools.execute("write_file", {"path": "sub/deep/new.txt",
                                        "content": "written by kuro\n"}, cfg)
    check("write_file creates a file and its parent dirs",
          not made.is_error and (wd / "sub/deep/new.txt").read_text()
          == "written by kuro\n" and made.meta.get("created") is True,
          made.output[:70])

    again = tools.execute("write_file", {"path": "sub/deep/new.txt",
                                         "content": "second\n"}, cfg)
    check("write_file overwrites and says so",
          not again.is_error and again.meta.get("created") is False
          and "overwrote" in again.output, again.output[:70])

    escape = tools.execute("write_file", {"path": "../escaped.txt",
                                          "content": "x"}, cfg)
    check("write_file cannot leave the jail", escape.is_error
          and "sandbox" in escape.output, escape.output[:70])
    check("the jail really is next door", not (wd.parent / "escaped.txt").exists())

    # ---- edit_file
    target = wd / "edit-me.txt"
    target.write_text("one two one\n")

    edited = tools.execute("edit_file", {"path": "edit-me.txt",
                                         "old_string": "two", "new_string": "2"}, cfg)
    check("edit_file replaces an exact string",
          not edited.is_error and target.read_text() == "one 2 one\n"
          and "1 replacement" in edited.output, edited.output[:70])

    ambiguous = tools.execute("edit_file", {"path": "edit-me.txt",
                                            "old_string": "one",
                                            "new_string": "1"}, cfg)
    check("edit_file refuses an ambiguous match", ambiguous.is_error
          and "2 times" in ambiguous.output, ambiguous.output[:70])

    everything = tools.execute("edit_file", {"path": "edit-me.txt",
                                             "old_string": "one", "new_string": "1",
                                             "replace_all": True}, cfg)
    check("edit_file replace_all takes every occurrence",
          not everything.is_error and target.read_text() == "1 2 1\n",
          everything.output[:70])

    nope = tools.execute("edit_file", {"path": "edit-me.txt",
                                       "old_string": "absent", "new_string": "x"}, cfg)
    check("edit_file reports a string it cannot find", nope.is_error,
          nope.output[:60])

    blank = tools.execute("edit_file", {"path": "edit-me.txt",
                                        "old_string": "", "new_string": "x"}, cfg)
    check("edit_file refuses an empty old_string", blank.is_error,
          blank.output[:60])


# ---------------------------------------------------------------------------
# glob / grep / list_dir
# ---------------------------------------------------------------------------

def test_indexing(wd: Path) -> None:
    cfg = _cfg(wd)
    (wd / "alpha.py").write_text("print('a')\nNEEDLE here\n")
    (wd / "beta.md").write_text("# beta\n")
    (wd / ".git").mkdir(exist_ok=True)
    (wd / ".git" / "hidden.txt").write_text("not yours\n")

    found = tools.execute("glob", {"pattern": "*.txt"}, cfg)
    check("glob finds files", not found.is_error
          and "sample.txt" in found.output, found.output[:70])

    py = tools.execute("glob", {"pattern": "**/*.py"}, cfg)
    check("glob walks the tree", "alpha.py" in py.output, py.output[:70])

    hidden = tools.execute("glob", {"pattern": "**/*.txt"}, cfg)
    check("glob skips .git", ".git" not in hidden.output, hidden.output[:70])

    escape = tools.execute("glob", {"pattern": "../*.py"}, cfg)
    check("glob refuses a pattern that escapes the jail", escape.is_error,
          escape.output[:70])

    nothing = tools.execute("glob", {"pattern": "*.nothing"}, cfg)
    check("glob says so when nothing matches",
          not nothing.is_error and "(no matches)" in nothing.output,
          nothing.output[:50])

    # ---- grep
    hit = tools.execute("grep", {"pattern": "NEEDLE", "path": "."}, cfg)
    check("grep returns file, line and content",
          not hit.is_error and "alpha.py:2:" in hit.output, hit.output[:70])
    check("grep counts its hits", hit.meta.get("count") == 1, str(hit.meta))

    filtered = tools.execute("grep", {"pattern": "beta", "path": ".",
                                      "glob": "*.md"}, cfg)
    check("grep filters on a glob", "beta.md" in filtered.output
          and "alpha.py" not in filtered.output, filtered.output[:70])

    insensitive = tools.execute("grep", {"pattern": "needle", "path": ".",
                                         "ignore_case": True}, cfg)
    check("grep honours ignore_case", "alpha.py" in insensitive.output,
          insensitive.output[:70])

    exact = tools.execute("grep", {"pattern": "needle", "path": "."}, cfg)
    check("grep is case-sensitive by default",
          exact.meta.get("count") == 0, str(exact.meta))

    bad = tools.execute("grep", {"pattern": "[unclosed", "path": "."}, cfg)
    check("grep reports a bad regex", bad.is_error, bad.output[:60])

    blank = tools.execute("grep", {"pattern": "", "path": "."}, cfg)
    check("grep refuses an empty pattern", blank.is_error, blank.output[:50])

    # ---- list_dir
    listed = tools.execute("list_dir", {"path": "."}, cfg)
    check("list_dir lists files and directories",
          not listed.is_error and "sample.txt" in listed.output
          and "sub/" in listed.output, listed.output[:70])
    check("list_dir skips .git", ".git" not in listed.output, listed.output[:70])

    file_as_dir = tools.execute("list_dir", {"path": "sample.txt"}, cfg)
    check("list_dir refuses a file", file_as_dir.is_error, file_as_dir.output[:60])

    gone = tools.execute("list_dir", {"path": "nowhere"}, cfg)
    check("list_dir reports a missing directory", gone.is_error,
          gone.output[:60])


# ---------------------------------------------------------------------------
# search_web / web_fetch
# ---------------------------------------------------------------------------

def test_web(wd: Path) -> None:
    cfg = _cfg(wd)

    # ---- validation and the scope guard, offline --------------------------
    no_query = tools.execute("search_web", {"query": "  "}, cfg)
    check("search_web refuses an empty query", no_query.is_error,
          no_query.output[:40])
    no_url = tools.execute("web_fetch", {"url": ""}, cfg)
    check("web_fetch refuses an empty url", no_url.is_error, no_url.output[:40])

    bad_max = tools.execute("search_web", {"query": "x", "max_results": "lots"}, cfg)
    check("search_web refuses a non-integer max_results", bad_max.is_error,
          bad_max.output[:50])

    scratch = Path(tempfile.mkdtemp())
    deny = scratch / "out-of-scope.yaml"
    deny.write_text("domain:\n  - evil.example.com\n")
    scoped = _cfg(wd)
    scoped._scope = Scope.load(None, deny)

    blocked = tools.execute("web_fetch", {"url": "https://evil.example.com/x"}, scoped)
    check("web_fetch obeys the deny-list",
          blocked.is_error and blocked.meta.get("blocked") is True,
          blocked.output[:70])
    # search_web names no host, so scope never blocks it — the operator's own
    # search endpoint is not a target.
    search_scoped = tools.execute("search_web", {"query": "anything"}, scoped)
    check("search_web is never scope-blocked",
          not (search_scoped.is_error and search_scoped.meta.get("blocked") is True),
          search_scoped.output[:60])

    # ---- live traffic ------------------------------------------------------
    page = tools.execute("web_fetch", {"url": "https://example.com/"}, cfg)
    check_live("web_fetch reaches a live https page",
               not page.is_error and len(page.output) > 50,
               f"{page.output[:80]!r}")

    results = tools.execute("search_web", {"query": "python urllib docs",
                                           "max_results": 3}, cfg)
    check_live("search_web returns live results with URLs",
               not results.is_error and "result(s)" in results.output
               and "http" in results.output, results.output[:100])


# ---------------------------------------------------------------------------
# The conditional tools: hunt, campaign read, panes, LDXHub
# ---------------------------------------------------------------------------

def test_conditional(wd: Path) -> None:
    cfg = _cfg(wd)
    cfg.state_dir = Path(tempfile.mkdtemp()) / "state"

    # ---- report_finding
    reported = tools.execute("report_finding", {
        "title": "SQL injection in ?id", "severity": "high",
        "asset": "app.example.com", "evidence": "GET /x?id=1' -> 500",
        "remediation": "parameterise the query"}, cfg)
    finding = (reported.meta or {}).get("finding") or {}
    check("report_finding carries the finding out on meta",
          not reported.is_error and finding.get("severity") == "high"
          and "SQL injection" in finding.get("title", ""),
          reported.output[:70])

    no_evidence = tools.execute("report_finding", {"title": "x", "severity": "high"}, cfg)
    check("report_finding demands evidence", no_evidence.is_error
          and "evidence" in no_evidence.output, no_evidence.output[:70])

    no_title = tools.execute("report_finding", {"severity": "high",
                                                "evidence": "GET /x -> 500"}, cfg)
    check("report_finding demands a title", no_title.is_error,
          no_title.output[:60])

    # ---- hunt_findings (a state dir with no campaign in it)
    none_yet = tools.execute("hunt_findings", {}, cfg)
    check("hunt_findings reports an empty state instead of raising",
          not none_yet.is_error and "no /bug-hunt campaign" in none_yet.output,
          none_yet.output[:70])

    bad_include = tools.execute("hunt_findings", {"include": "everything"}, cfg)
    check("hunt_findings rejects an unknown include", bad_include.is_error,
          bad_include.output[:70])

    # ---- pane messaging (outside tmux, all three must fail cleanly)
    for name, args in (("agent_peers", {}),
                       ("agent_send", {"to": "peer", "body": "hi"}),
                       ("agent_inbox", {})):
        res = tools.execute(name, args, cfg)
        check(f"{name} explains it needs a Kuro pane",
              res.is_error and "tmux pane" in res.output, res.output[:70])

    # ---- LDXHub (no key in the throwaway KURO_HOME: no job can be started)
    bad_service = tools.execute("ldxhub_list", {"service": "nope"}, cfg)
    check("ldxhub_list rejects an unknown service", bad_service.is_error,
          bad_service.output[:70])

    job = tools.execute("ldxhub_structflow", {"prompt": "make a doc"}, cfg)
    check("an LDXHub job without a key says how to get one",
          job.is_error and "ldxhub-api-setup" in job.output, job.output[:70])


# ---------------------------------------------------------------------------
# The harness: what it advertises, what it refuses, and how it previews
# ---------------------------------------------------------------------------

def _names(agent: Agent) -> set[str]:
    return {t["name"] for t in agent.tool_schemas()}


def test_harness_wiring(wd: Path) -> None:
    core = {"bash", "read_file", "write_file", "edit_file", "glob", "grep",
            "list_dir", "search_web", "web_fetch"}

    for mode in MODES:
        agent = Agent(_cfg(wd, mode=mode))
        names = _names(agent)
        check(f"{mode}: every advertised tool can actually run",
              names <= set(tools._DISPATCH),
              str(sorted(names - set(tools._DISPATCH))))
        check(f"{mode}: the read tools are always available",
              {"read_file", "glob", "grep", "list_dir"} <= names,
              str(sorted(names)))

    check("auto offers every core tool", core <= _names(Agent(_cfg(wd))))
    plan = _names(Agent(_cfg(wd, mode="plan")))
    check("plan withholds every writing tool",
          not ({"bash", "write_file", "edit_file"} & plan), str(sorted(plan)))
    edits = _names(Agent(_cfg(wd, mode="edits")))
    check("edits withholds bash only",
          "bash" not in edits and {"write_file", "edit_file"} <= edits,
          str(sorted(edits)))

    # The conditional tools follow their flags, not the mode.
    main_agent = Agent(_cfg(wd))
    check("an ordinary session cannot report into a hunt",
          "report_finding" not in _names(main_agent))
    check("an ordinary session can read a campaign",
          "hunt_findings" in _names(main_agent))
    check("a session outside tmux gets no pane tools",
          not (tools.PEER_TOOLS & _names(main_agent)))

    hunter = Agent(_cfg(wd), can_report=True, can_hunt_read=False)
    check("a hunt agent gets report_finding",
          "report_finding" in _names(hunter))
    check("a hunt agent cannot read the whole campaign",
          "hunt_findings" not in _names(hunter))

    panes = Agent(_cfg(wd), can_peer=True)
    check("a pane session gets the peer tools",
          tools.PEER_TOOLS <= _names(panes), str(sorted(_names(panes))))

    check("LDXHub job tools are withheld without a key",
          not (tools.LDXHUB_TOOLS & _names(main_agent)),
          str(sorted(tools.LDXHUB_TOOLS & _names(main_agent))))
    check("LDXHub browsing stays available without a key",
          "ldxhub_list" in _names(main_agent))

    # _denied is the second line of defence when a model calls a withheld tool.
    denied = Agent(_cfg(wd, mode="plan"))._denied("bash")
    check("plan refuses bash by name",
          bool(denied) and "PLAN" in denied, str(denied))
    check("plan still allows reading",
          Agent(_cfg(wd, mode="plan"))._denied("read_file") is None)
    check("a session outside a hunt refuses report_finding",
          bool(main_agent._denied("report_finding"))
          and "hunt" in main_agent._denied("report_finding"))

    # The permission gate, and the previews it shows.
    check("the gate covers exactly the writing tools",
          {n for n in tools._DISPATCH if tools.needs_permission(n)}
          == tools.GATED_TOOLS,
          str(sorted(n for n in tools._DISPATCH if tools.needs_permission(n))))
    check("reads never ask", not any(tools.needs_permission(n) for n in
                                     ("read_file", "glob", "grep", "list_dir",
                                      "search_web", "web_fetch")))

    bash_preview = tools.build_preview("bash", {"command": "rm -rf /"}, None)
    check("bash previews the exact command",
          bash_preview.kind == "command" and bash_preview.body == "rm -rf /",
          bash_preview.body[:40])

    (wd / "preview.txt").write_text("before\n")
    write_preview = tools.build_preview("write_file",
                                        {"path": "preview.txt", "content": "after\n"},
                                        _cfg(wd))
    check("write previews a diff of the real file",
          write_preview.kind == "diff" and "before" in write_preview.body
          and "after" in write_preview.body, write_preview.body[:70])

    edit_preview = tools.build_preview("edit_file", {"path": "preview.txt",
                                                     "old_string": "before",
                                                     "new_string": "after"}, None)
    check("edit previews a diff", edit_preview.kind == "diff",
          edit_preview.body[:40])


# ---------------------------------------------------------------------------
# The loop itself: seven tools through one real turn
# ---------------------------------------------------------------------------

async def test_harness_turn(wd: Path) -> None:
    """Drive run_turn with a model that calls every core local tool at once.

    This is the check that the harness—not just the tool layer—can use them:
    previews are built, the gate answers, execution happens off the event loop,
    and every tool_use comes back with a matching tool_result.
    """
    (wd / "turn-input.txt").write_text("alpha\nbeta\n")

    calls = [
        _Blk(type="tool_use", id="c1", name="bash",
             input={"command": "echo TURN_OK"}),
        _Blk(type="tool_use", id="c2", name="read_file",
             input={"path": "turn-input.txt"}),
        _Blk(type="tool_use", id="c3", name="list_dir", input={"path": "."}),
        _Blk(type="tool_use", id="c4", name="glob", input={"pattern": "turn-*"}),
        _Blk(type="tool_use", id="c5", name="grep",
             input={"pattern": "alpha", "path": "."}),
        _Blk(type="tool_use", id="c6", name="write_file",
             input={"path": "out/from-turn.txt", "content": "the harness wrote this\n"}),
        _Blk(type="tool_use", id="c7", name="edit_file",
             input={"path": "out/from-turn.txt", "old_string": "harness",
                    "new_string": "loop"}),
    ]

    async def handler(name, args, preview):
        return "yes"

    agent = Agent(_cfg(wd, mode="auto"), permission_handler=handler)
    state = {"n": 0}

    async def fake_stream(max_tokens=None):
        state["n"] += 1
        if state["n"] == 1:
            yield ({"type": "text_delta", "text": "working"}, None)
            yield (None, _Msg([_Blk(type="text", text="working"), *calls]))
            return
        yield (None, _Msg([_Blk(type="text", text="done")]))

    agent._stream_once = fake_stream

    events = [ev async for ev in agent.run_turn("use every tool")]
    results = {e["name"]: e for e in events if e["type"] == "tool_result"}
    ordered = [e["name"] for e in events if e["type"] == "tool_result"]

    check("every tool call came back with a result",
          len(ordered) == len(calls), f"{ordered}")
    check("no tool failed in the turn",
          not [n for n, e in results.items() if e["is_error"]],
          str({n: e["output"][:60] for n, e in results.items() if e["is_error"]}))
    check("the writes happened in order (write before edit)",
          ordered.index("write_file") < ordered.index("edit_file"), str(ordered))
    check("bash ran through the loop",
          "TURN_OK" in results.get("bash", {}).get("output", ""))
    check("read_file ran through the loop",
          "alpha" in results.get("read_file", {}).get("output", ""))
    check("glob ran through the loop",
          "turn-input.txt" in results.get("glob", {}).get("output", ""))
    check("grep ran through the loop",
          "turn-input.txt:1:" in results.get("grep", {}).get("output", ""))
    check("list_dir ran through the loop",
          "turn-input.txt" in results.get("list_dir", {}).get("output", ""))
    check("write_file ran through the loop",
          (wd / "out/from-turn.txt").exists(), str(results.get("write_file", {}).get("output", "")))
    check("edit_file ran through the loop",
          "the loop wrote this" in (wd / "out/from-turn.txt").read_text(),
          str(results.get("edit_file", {}).get("output", "")))
    check("the turn finished normally",
          events[-1]["type"] == "turn_end"
          and not any(e["type"] == "iteration_cap" for e in events),
          events[-1]["type"])

    # The permission gate itself. No shipped mode leaves a tool merely gated
    # (each lists it in `auto` or `deny`, which is why the gate looks
    # unreachable), so register a mode that does neither — that is the state
    # the modal exists for — and take it out again afterwards.
    MODES["gate_probe"] = {"label": "GATE PROBE", "blurb": "leaves tools gated",
                           "auto": set(), "deny": set()}
    try:
        gated = Agent(_cfg(wd, mode="gate_probe"), permission_handler=None)
        asked: list[str] = []

        async def watch(name, args, preview):
            asked.append(name)
            return "no"

        gated.permission_handler = watch
        state2 = {"n": 0}

        async def deny_stream(max_tokens=None):
            state2["n"] += 1
            if state2["n"] == 1:
                yield (None, _Msg([
                    _Blk(type="tool_use", id="d1", name="bash",
                         input={"command": "echo never"}),
                    _Blk(type="tool_use", id="d2", name="write_file",
                         input={"path": "denied.txt", "content": "x"})]))
                return
            yield (None, _Msg([_Blk(type="text", text="done")]))

        gated._stream_once = deny_stream
        gated_events = [ev async for ev in gated.run_turn("go on")]
        gated_calls = [e for e in gated_events if e["type"] == "tool_call"]
        gated_results = [e for e in gated_events if e["type"] == "tool_result"]

        check("the gate is raised for a tool that is neither auto nor denied",
              [e["name"] for e in gated_calls if e["gated"]] == ["bash", "write_file"],
              str([(e["name"], e["gated"]) for e in gated_calls]))
        check("the operator's 'no' reaches both tools",
              asked == ["bash", "write_file"], str(asked))
        check("a denied permission stops the write",
              any(e["name"] == "write_file" and e["meta"].get("denied")
                  for e in gated_results)
              and not (wd / "denied.txt").exists(),
              str([e["name"] for e in gated_results]))
        check("a denied permission stops the command",
              any(e["name"] == "bash" and e["meta"].get("denied")
                  for e in gated_results)
              and "TURN_OK" not in " ".join(e["output"] for e in gated_results),
              str([(e["name"], e["output"][:40]) for e in gated_results]))
    finally:
        del MODES["gate_probe"]


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "sample.txt").write_text("alpha\nbeta\ngamma\n")

        test_wiring()
        test_ca_bundle()
        test_bash(wd)
        test_files(wd)
        test_indexing(wd)
        test_web(wd)
        test_conditional(wd)
        test_harness_wiring(wd)
        await test_harness_turn(wd)

    failed = [n for n, ok, _ in RESULTS if not ok]
    live_failed = [n for n, ok, _ in LIVE if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed, "
          f"{len(LIVE) - len(live_failed)}/{len(LIVE)} live")
    for name in failed:
        print(f"  FAILED: {name}")
    for name in live_failed:
        print(f"  LIVE FAILED: {name}")
    return 1 if (failed or live_failed) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
