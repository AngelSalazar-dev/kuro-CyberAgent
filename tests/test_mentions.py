"""`@`-mention completion: selection, ranking, and what accepting one does.

Offline and app-free. The selection logic lives at module level precisely so it
can be exercised here — a popup that needs a terminal to say what it would have
offered is a popup whose ordering never gets tested, and ordering is the whole
feature.

Run:  python tests/test_mentions.py   (from an activated venv)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.theme import get_palette  # noqa: E402
from zimzilla.ui.complete import (  # noqa: E402
    MAX_FILES,
    CompletionPopup,
    _at_token,
    _is_subsequence,
    _iter_files,
    _iter_under,
    _match_rank,
    _split,
    file_candidates,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _tree(root: Path) -> None:
    """A project shaped like the things that actually break a file picker.

    Two `app.py` at different depths, a directory whose name is a prefix of
    another entry, git internals and dependency trees that must never be
    offered, and a path segment shared by several files.
    """
    files = [
        "alpha.py",
        "app.py",
        "README.md",
        "zimzilla/config.py",
        "zimzilla/ui/app.py",
        "zimzilla/ui/complete.py",
        "zimzilla/ui/rain.py",
        "docs/guide.md",
        "docs/gen_graphics.py",
        ".git/objects/ab/cdef",
        "node_modules/pkg/index.js",
        ".venv/lib/python3.11/site-packages/x.py",
    ]
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")


# ---- the walk -------------------------------------------------------------
def test_walk(root: Path) -> None:
    pool = _iter_files(root)
    check("walk: relative paths, directories flagged with a slash",
          "zimzilla/ui/complete.py" in pool and "zimzilla/" in pool)
    check("walk: skipped directories never appear",
          not any(p.startswith((".git", "node_modules", ".venv")) for p in pool),
          f"{[p for p in pool if p.startswith('.')]}")
    check("walk: sorted for a stable display order", pool == sorted(pool))
    bounded = _iter_files(root, limit=3)
    check("walk: limit is honoured", len(bounded) == 3, f"{bounded}")
    check("walk: an empty directory is an empty list, not an error",
          _iter_files(root / "zimzilla" / "ui" / "rain.py") == [])


# ---- descent --------------------------------------------------------------
def test_descent(root: Path) -> None:
    sub = _iter_under(root, "zimzilla/ui")
    check("descent: paths stay relative to the root, not the subtree",
          "zimzilla/ui/app.py" in sub, f"{sub[:3]}")
    check("descent: bounded by the same limit",
          len(_iter_under(root, "zimzilla", limit=2)) == 2)
    check("descent: a file is not a directory",
          _iter_under(root, "README.md") == [])
    check("descent: the root itself is not a prefix", _iter_under(root, "") == [])
    check("descent: a missing directory offers nothing",
          _iter_under(root, "nope/nothing") == [])

    # Operator-typed text reaches this. Each of these has to resolve to "no
    # such directory here", never to a walk of the real filesystem.
    check("descent: absolute paths refused", _iter_under(root, "/etc") == [])
    check("descent: traversal refused", _iter_under(root, "../..") == [])
    check("descent: traversal mid-path refused",
          _iter_under(root, "docs/../..") == [])
    check("descent: a skip directory stays skipped",
          _iter_under(root, ".git") == [])
    check("descent: a skip directory stays skipped when typed in full",
          _iter_under(root, ".git/objects") == [])
    check("descent: node_modules stays skipped",
          _iter_under(root, "node_modules") == [])

    outside = root.parent / "outside-mentions"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("s\n")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        check("descent: a symlink out of the tree is refused", True, "skipped")
    else:
        check("descent: a symlink out of the tree is refused",
              _iter_under(root, "escape") == [])


# ---- matching -------------------------------------------------------------
def test_matching() -> None:
    check("subsequence: in order", _is_subsequence("cmp", "complete.py"))
    check("subsequence: out of order is not", not _is_subsequence("pmc", "complete.py"))
    check("subsequence: missing character is not",
          not _is_subsequence("cmpz", "complete.py"))
    check("subsequence: empty needle matches anything", _is_subsequence("", "x"))

    name_start = _match_rank("zimzilla/ui/complete.py", "comp")
    name_inside = _match_rank("zimzilla/ui/incomplete.py", "comp")
    path_only = _match_rank("a/comp/x.py", "comp")
    anywhere = _match_rank("a/xcompy.py", "comp")
    fuzzy = _match_rank("zimzilla/ui/complete.py", "zc")
    check("rank: a name starting with the query outranks one containing it",
          name_start < name_inside, f"{name_start} < {name_inside}")
    check("rank: a name match outranks a directory-only match",
          name_inside < path_only, f"{name_inside} < {path_only}")
    check("rank: a match inside the name outranks one only in the directory",
          anywhere[0] < path_only[0], f"{anywhere} < {path_only}")
    check("rank: a subsequence still matches, last", fuzzy is not None and fuzzy[0] == 4,
          f"{fuzzy}")
    check("rank: a query that is not there at all does not match",
          _match_rank("zimzilla/ui/complete.py", "qqqq") is None,
          f"{_match_rank('zimzilla/ui/complete.py', 'qqqq')}")
    check("rank: ties go to the shallower path",
          _match_rank("app.py", "app") < _match_rank("zimzilla/ui/app.py", "app"))
    check("rank: a directory is matched on its name, not its slash",
          _match_rank("zimzilla/ui/", "ui")[0] == 0, f"{_match_rank('zimzilla/ui/', 'ui')}")


def test_at_token() -> None:
    check("token: the @ the cursor is inside",
          _at_token("look at @al", 11) == 8, f"{_at_token('look at @al', 11)}")
    check("token: the mention under a mid-line cursor, not the last one",
          _at_token("@alpha and @beta", 4) == 0,
          f"{_at_token('@alpha and @beta', 4)}")
    check("token: a bare @ with the cursor after it", _at_token("@", 1) == 0)
    check("token: whitespace between the @ and the cursor ends the token",
          _at_token("@a b", 4) is None)
    check("token: no @ anywhere", _at_token("just words", 5) is None)
    check("token: cursor at the start of the line", _at_token("@a", 0) is None)
    check("token: a cursor past the end is clamped",
          _at_token("@al", 99) == 0)


def test_candidates(root: Path) -> None:
    pool = _iter_files(root)
    cands, subject = file_candidates(pool, root, "app")
    check("candidates: a plain query scores the whole flat pool",
          subject == "app" and any(v == "zimzilla/ui/app.py" for v, _ in cands))

    cands, subject = file_candidates(pool, root, "zimzilla/ui/comp")
    check("candidates: a real directory descends and scores only the rest",
          subject == "comp"
          and [v for v, _ in CompletionPopup._rank(cands, subject)]
          == ["zimzilla/ui/complete.py"],
          f"{subject} {[v for v, _ in cands]}")
    check("candidates: the subject is relative to the typed directory",
          all("/" not in s for _, s in cands))

    # `ui/` is a real segment in the tree but not a real directory at the top
    # level, so there is nothing to descend into and the whole query has to be
    # matched against the whole path instead.
    cands, subject = file_candidates(pool, root, "ui/comp")
    check("candidates: a partial segment with no directory behind it still finds it",
          subject == "ui/comp" and any(v.endswith("ui/complete.py") for v, _ in cands),
          f"{subject}")

    cands, subject = file_candidates(pool, root, "zimzilla/")
    check("candidates: a trailing slash lists the directory",
          subject == "" and "zimzilla/config.py" in [v for v, _ in cands]
          and "zimzilla/ui/" in [v for v, _ in cands])


def test_rank() -> None:
    pool = ["docs/", "docs/gen_graphics.py", "zimzilla/", "zimzilla/ui/complete.py"]
    cands = [(v, v) for v in pool]
    check("rank: a bare @ keeps the tree's own order",
          [v for v, _ in CompletionPopup._rank(cands, "")] == pool)
    ranked = CompletionPopup._rank([(v, v) for v in pool], "comp")
    check("rank: only matches survive",
          [v for v, _ in ranked] == ["zimzilla/ui/complete.py"], f"{ranked}")
    ranked = CompletionPopup._rank([(v, v) for v in pool], "docs")
    check("rank: the directory is offered as well as its children",
          ranked[0][0] == "docs/", f"{ranked}")
    check("rank: the row carries the parent directory for display",
          ranked[0] == ("docs/", "") and
          CompletionPopup._rank([("zimzilla/ui/complete.py",
                                 "zimzilla/ui/complete.py")], "complete")[0]
          == ("zimzilla/ui/complete.py", "zimzilla/ui/"))


def test_split() -> None:
    check("split: nested file", _split("zimzilla/ui/complete.py")
          == ("complete.py", "zimzilla/ui/"))
    check("split: directory keeps its slash",
          _split("zimzilla/ui/") == ("ui/", "zimzilla/"))
    check("split: top-level directory has no parent", _split("docs/") == ("docs/", ""))
    check("split: top-level file has no parent",
          _split(".gitignore") == (".gitignore", ""))


# ---- accepting ------------------------------------------------------------
def _popup(items: list[tuple[str, str]], kind: str = "file") -> CompletionPopup:
    """A popup holding *items*, with no app — `accept` only reads its own state."""
    pop = CompletionPopup(get_palette("green"))
    pop.items = items
    pop.kind = kind
    pop.index = 0
    return pop


def test_accept() -> None:
    pop = _popup([("zimzilla/ui/app.py", "")])
    check("accept: a file replaces the mention and ends it",
          pop.accept("@app") == "@zimzilla/ui/app.py ",
          repr(pop.accept("@app")))

    pop = _popup([("zimzilla/ui/", "")])
    check("accept: a directory is left open, so the picker can descend",
          pop.accept("@ui") == "@zimzilla/ui/", repr(pop.accept("@ui")))

    pop = _popup([("zimzilla/ui/app.py", "")])
    check("accept: the rest of the line survives, with no doubled space",
          pop.accept("read @app and fix it", 9) == "read @zimzilla/ui/app.py and fix it",
          repr(pop.accept("read @app and fix it", 9)))

    pop = _popup([("alpha.py", "")])
    check("accept: nothing to accept returns None", pop.accept("no mention") is None)

    pop = _popup([("/mode", "switch mode")], kind="slash")
    check("accept: slash commands are unchanged",
          pop.accept("/mo") == "/mode " and pop.accept("/mode au") == "/mode /mode ")

    pop = _popup([])
    check("accept: an empty popup returns None", pop.accept("@a") is None)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        _tree(root)
        test_walk(root)
        test_descent(root)
        test_candidates(root)
    test_matching()
    test_at_token()
    test_rank()
    test_split()
    test_accept()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
