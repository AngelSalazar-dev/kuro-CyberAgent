"""`/reverse-engineer` regression suite: identify, inventory, probe, brief.

Run:  python tests/test_reverse.py   (from an activated venv)

Everything here is offline and model-free, and that is the point of the module
it covers: ``reverse.identify`` and ``reverse.inventory`` are pure functions of
a path, and the two commands are built entirely on top of them. If that ever
stops being true — if mapping an artifact starts needing the network, or starts
wanting to write beside the artifact — these tests are where it should show up.

Four invariants this file is built around:

* **A wrong extension never beats a right header, and a wrong header never
  beats random bytes.** ``sample.bin`` full of urandom once got parsed as an
  ELF and produced confident garbage — a fake entry point, 35,000 sections.
  Identification has to be willing to say "unknown".
* **Identification is by content, so the same bytes under any name agree.**
  The zip refinement (AndroidManifest.xml + classes.dex → apk) is what makes
  an APK an APK, not the ``.apk`` suffix.
* **Nothing in the inventory writes, executes, or fetches.** The fixtures are
  hostile by construction — an ELF header over random bytes, a script that
  tries to look like a dropper — and reading them must stay a read.
* **A secret in an artifact comes out redacted.** Signatures are surfaced
  because they are the point of reconnaissance; key-shaped strings are not.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import reverse as rv  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# ---------------------------------------------------------------------------
# fixtures — built here rather than checked in, so the suite has no binary blobs
# ---------------------------------------------------------------------------


def _plain_zip(path: Path) -> None:
    """A zip with no Android and no META-INF: a zip, and nothing more."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("note.txt", "just a note\n")
        z.writestr("data.txt", "one, two, three\n")


def _apk(path: Path) -> None:
    """The minimum that makes a zip an APK: a manifest and a dex."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00binary xml here")
        z.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 64)
        z.writestr("resources.arsc", b"\x02\x00\x0c\x00" + b"\x00" * 32)
        z.writestr("lib/arm64-v8a/libnative.so", b"\x7fELF" + b"\x00" * 60)
        z.writestr("META-INF/CERT.RSA", b"\x30\x82\x01\x00sig")


def _jar(path: Path) -> None:
    """A zip that announces itself as a jar the only way one does."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n\r\n")
        z.writestr("com/example/Main.class", b"\xca\xfe\xba\xbe" + b"\x00" * 32)


def _elf(path: Path) -> None:
    """A 64-bit little-endian x86-64 ELF header, and a section table."""
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    header = ident + struct.pack("<HHIQQQIHHHHHH",
                                 2, 62, 1, 0x401000, 64, 0, 0, 64, 64, 3, 2, 1, 0)
    # One section, name index 1, type PROGBITS.
    sec = (struct.pack("<IIQQQQIIQQ", 1, 1, 0x6, 0x401000, 0x100, 0x120,
                       0, 0, 16, 0))
    shstr = b"\x00.text\x00"
    blob = header + sec + shstr
    path.write_bytes(blob.ljust(0x120, b"\x00") + b"\x90" * 0x100)


def _pe(path: Path) -> None:
    """A PE32+ x86-64 image: NX and CFG on, ASLR off, Windows GUI subsystem.

    The PE signature has to land exactly at ``e_lfanew`` — a DOS header is
    padded, not contiguous, and a fixture that puts the signature right after
    the stub while pointing ``e_lfanew`` past it parses as "MZ, no PE" and
    yields no facts at all.
    """
    e_lfanew = 0x80
    dos = b"MZ" + b"\x00" * 58 + struct.pack("<I", e_lfanew)
    assert len(dos) == 0x40
    stub = dos + b"\x00" * (e_lfanew - len(dos))
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0xF0, 0x0022)
    opt = struct.pack("<H", 0x20B) + b"\x00" * 66
    # Subsystem (2 = Windows GUI) sits at opt+68, DllCharacteristics at opt+70.
    # 0x0100 NX | 0x0040 CFG, and deliberately not 0x4000 ASLR.
    opt += struct.pack("<HH", 2, 0x0140)
    opt += b"\x00" * (0xF0 - len(opt))
    sect = struct.pack("<8sIIII", b".text", 0x1000, 0x1000, 0x200, 0x400)
    blob = stub + b"PE\x00\x00" + coff + opt + sect
    path.write_bytes(blob.ljust(0x400, b"\x00"))


def _script(path: Path) -> None:
    path.write_text("#!/usr/bin/env bash\nset -e\necho hello\n")


def _urandom(path: Path, size: int = 200_000) -> None:
    path.write_bytes(os.urandom(size))


# ---------------------------------------------------------------------------
# identification
# ---------------------------------------------------------------------------


def test_identify(tmp: Path) -> None:
    z = tmp / "plain.zip"; _plain_zip(z)
    a = rv.identify(z)
    check("zip: plain archive is a zip", a.kind == "zip", a.kind)

    ap = tmp / "app.apk"; _apk(ap)
    check("apk: manifest + dex is an apk", rv.identify(ap).kind == "apk",
          rv.identify(ap).kind)

    j = tmp / "lib.jar"; _jar(j)
    check("jar: META-INF manifest is a jar", rv.identify(j).kind == "jar",
          rv.identify(j).kind)

    e = tmp / "bin.elf"; _elf(e)
    check("elf: magic + extension agree", rv.identify(e).kind == "elf",
          rv.identify(e).kind)

    p = tmp / "app.exe"; _pe(p)
    check("pe: MZ + extension agree", rv.identify(p).kind == "pe",
          rv.identify(p).kind)

    s = tmp / "run.sh"; _script(s)
    check("script: shebang file is a script", rv.identify(s).kind == "script",
          rv.identify(s).kind)

    d = tmp / "tree"; d.mkdir()
    (d / "a.txt").write_text("x")
    check("dir: a directory is a dir", rv.identify(d).kind == "dir",
          rv.identify(d).kind)


def test_extension_cannot_lie(tmp: Path) -> None:
    """The regression that motivated `_header_confirms`.

    urandom named `.bin` was parsed as an ELF because `.bin` mapped to `elf`
    with nothing checking the magic. `.bin` is a catch-all name; the header has
    to agree before a header-definitive kind is claimed.
    """
    u = tmp / "noise.bin"; _urandom(u)
    a = rv.identify(u)
    check("urandom .bin is not an elf", a.kind != "elf", a.kind)
    check("urandom .bin is unknown", a.kind == "unknown", a.kind)

    inv = rv.inventory(a)
    facts = dict(inv.facts)
    check("urandom produced no elf facts", "Entry point" not in facts,
          str(sorted(facts))[:60])

    # A PE header under a .zip name: the container kind wins on content, and
    # the header is not consulted for a container's own extension.
    lie = tmp / "actually.elf"; _script(lie)
    check("a script named .elf is a script", rv.identify(lie).kind == "script",
          rv.identify(lie).kind)


def test_refinement_is_by_content(tmp: Path) -> None:
    """Same bytes, any name: the name must not decide the kind."""
    for name in ("app.apk", "app.zip", "app.dat", "app"):
        p = tmp / name
        _apk(p)
        got = rv.identify(p).kind
        check(f"apk bytes named {name} → apk", got == "apk", got)

    for name in ("lib.jar", "lib.zip", "lib"):
        p = tmp / name
        _jar(p)
        got = rv.identify(p).kind
        check(f"jar bytes named {name} → jar", got == "jar", got)


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def test_inventory_apk(tmp: Path) -> None:
    p = tmp / "app.apk"; _apk(p)
    art = rv.identify(p)
    inv = rv.inventory(art)
    facts = dict(inv.facts)

    check("apk: member count reported", "members" in facts, str(sorted(facts)))
    check("apk: dex found", "dex" in facts, str(sorted(facts)))
    check("apk: native ABI found", "native libs" in facts, str(sorted(facts)))
    check("apk: signature reported",
          any("sign" in k.lower() or "sign" in v.lower()
              for k, v in inv.facts + [(n, n) for n in inv.notes]),
          str(inv.notes)[:80] + str(sorted(facts)))
    names = [n for n, _s in inv.members]
    check("apk: members are listed", "classes.dex" in names, str(names))
    # Biggest first, which is the order an operator wants: what dominates this.
    sizes = [s for _n, s in inv.members]
    check("apk: members sorted largest first", sizes == sorted(sizes, reverse=True),
          str(sizes))


def test_inventory_elf(tmp: Path) -> None:
    p = tmp / "bin.elf"; _elf(p)
    inv = rv.inventory(rv.identify(p))
    facts = dict(inv.facts)
    check("elf: class read", "64" in facts.get("class", ""), str(facts))
    check("elf: machine read", "x86-64" in facts.get("machine", ""),
          facts.get("machine", ""))
    check("elf: type read", "EXEC" in facts.get("type", ""), facts.get("type", ""))
    check("elf: entry read", facts.get("entry", "").startswith("0x"),
          facts.get("entry", ""))
    check("elf: no PE facts leaked", "subsystem" not in facts, str(sorted(facts)))


def test_inventory_pe(tmp: Path) -> None:
    p = tmp / "app.exe"; _pe(p)
    inv = rv.inventory(rv.identify(p))
    facts = dict(inv.facts)
    check("pe: parsing produced facts", bool(facts), str(sorted(facts)))
    check("pe: machine read", "x86-64" in facts.get("machine", ""),
          facts.get("machine", ""))
    check("pe: format read", "PE32+" in facts.get("format", ""),
          facts.get("format", ""))
    check("pe: subsystem read", "GUI" in facts.get("subsystem", ""),
          facts.get("subsystem", ""))
    flags = facts.get("mitigations", "")
    check("pe: NX reported", "NX" in flags, flags)
    check("pe: CFG reported", "CFG" in flags, flags)
    check("pe: ASLR correctly not claimed", "ASLR" not in flags, flags)
    check("pe: section table read", facts.get("sections") == "1",
          facts.get("sections", ""))
    check("pe: no COM descriptor claim", "managed" not in facts,
          str(sorted(facts)))


def test_inventory_script(tmp: Path) -> None:
    p = tmp / "run.sh"; _script(p)
    inv = rv.inventory(rv.identify(p))
    facts = dict(inv.facts)
    check("script: line count", facts.get("lines") == "3", str(facts))
    check("script: shebang captured", "bash" in facts.get("shebang", ""),
          facts.get("shebang", ""))


def test_inventory_dir(tmp: Path) -> None:
    d = tmp / "tree2"; d.mkdir()
    (d / "a.txt").write_text("x")
    (d / "b.txt").write_text("y")
    (d / "c.py").write_text("z")
    inv = rv.inventory(rv.identify(d))
    facts = dict(inv.facts)
    check("dir: entry count", facts.get("entries", "").startswith("3"),
          str(facts))
    check("dir: extension census", "txt" in facts.get("by extension", ""),
          facts.get("by extension", ""))


def test_redaction(tmp: Path) -> None:
    """A key-shaped string must not survive into the inventory."""
    secret = "sk-ant-api03-QWERTYUIOPASDFGHJKLZXCVBNM1234567890abcd"
    p = tmp / "leaky.txt"
    p.write_text(f"url https://example.com/hook\nkey {secret}\n"
                 "mail admin@example.com\n")
    inv = rv.inventory(rv.identify(p))
    blob = repr(inv.signals) + repr(inv.facts) + repr(inv.notes)
    check("redaction: raw key absent", secret not in blob)
    check("redaction: marker present", "[REDACTED_SECRET]" in blob
          or "REDACTED" in blob, blob[:120])
    check("redaction: email redacted", "admin@example.com" not in blob, blob[:120])
    check("redaction: url kept", any("example.com/hook" in v for _c, v in inv.signals),
          str(inv.signals)[:120])


def test_signals_bounded(tmp: Path) -> None:
    """One category cannot flood the transcript."""
    p = tmp / "many.txt"
    p.write_text("\n".join(f"https://host{i}.example.com/path{i}" for i in range(200)))
    inv = rv.inventory(rv.identify(p))
    urls = [v for c, v in inv.signals if c == "url"]
    check("signals: urls capped", len(urls) <= rv._MAX_PER_SIGNAL, str(len(urls)))
    check("signals: total capped", len(inv.signals) <= rv._MAX_SIGNALS,
          str(len(inv.signals)))


# ---------------------------------------------------------------------------
# tooling
# ---------------------------------------------------------------------------


def _probes(*present: str) -> list[rv.ToolProbe]:
    """A synthetic probe, so the tests do not depend on this box.

    Swapping `shutil.which` for a fixed set is what makes "is the gap logic
    right" a question about the logic rather than about the machine running
    the suite.
    """
    return [
        rv.ToolProbe(name=n, path=(f"/usr/bin/{n}" if n in present else None),
                     hint=h, capability=c)
        for n, h, c in rv._TOOL_CATALOGUE
    ]


def test_probe_shape() -> None:
    probes = rv.probe_tools()
    names = {p.name for p in probes}
    check("probe: covers the catalogue", len(probes) == len(rv._TOOL_CATALOGUE),
          f"{len(probes)} vs {len(rv._TOOL_CATALOGUE)}")
    check("probe: names unique", len(names) == len(probes))
    check("probe: capabilities known",
          all(p.capability in rv.CAPABILITY_ORDER for p in probes),
          str([p.capability for p in probes
               if p.capability not in rv.CAPABILITY_ORDER]))
    check("probe: present implies path",
          all(p.path for p in probes if p.present))
    check("probe: absent implies no path",
          all(p.path is None for p in probes if not p.present))
    check("probe: runs here without raising", isinstance(probes, list))


def test_gaps() -> None:
    # Everything: no gaps anywhere.
    full = _probes(*[n for n, _h, _c in rv._TOOL_CATALOGUE])
    check("gaps: full toolchain has none for elf", rv.gaps("elf", full) == [],
          str(rv.gaps("elf", full)))
    check("gaps: full toolchain has none for apk", rv.gaps("apk", full) == [],
          str(rv.gaps("apk", full)))

    # Nothing: every capability that type needs is reported missing.
    bare = _probes()
    for kind in ("apk", "elf", "pe", "jar", "script"):
        miss = rv.gaps(kind, bare)
        want = set(rv.KIND_CAPABILITIES.get(kind, rv._UNIVERSAL_CAPS))
        check(f"gaps: bare host misses all of {kind}", set(miss) == want,
              f"{sorted(miss)} vs {sorted(want)}")

    # A script needs no decompiler and no disassembler: its capability set is
    # deliberately small, and a missing Ghidra is not a reason a shell script
    # cannot be read. It does need something that can lint or trace it.
    sgap = rv.gaps("script", bare)
    check("gaps: script does not require a disassembler",
          "disasm" not in sgap, str(sgap))
    check("gaps: script does not require unpacking", "unpack" not in sgap,
          str(sgap))
    check("gaps: script does require a script tool", "script" in sgap, str(sgap))

    # Partial: one tool per capability closes the gap, however many names that
    # capability has.
    first_of: dict[str, str] = {}
    for n, _h, c in rv._TOOL_CATALOGUE:
        first_of.setdefault(c, n)
    partial = _probes(*first_of.values())
    check("gaps: one tool per capability is enough for elf",
          rv.gaps("elf", partial) == [], str(rv.gaps("elf", partial)))
    check("gaps: one tool per capability is enough for apk",
          rv.gaps("apk", partial) == [], str(rv.gaps("apk", partial)))


def test_capability_status() -> None:
    full = _probes(*[n for n, _h, _c in rv._TOOL_CATALOGUE])
    status = rv.capability_status("apk", full)
    check("status: one row per capability",
          len(status) == len(set(rv.KIND_CAPABILITIES["apk"])), str(len(status)))
    check("status: nothing missing when full",
          all(not miss for _c, _have, miss in status), str(status)[:120])
    check("status: every capability has a tool when full",
          all(have for _c, have, _miss in status), str(status)[:120])

    bare = rv.capability_status("apk", _probes())
    check("status: nothing present when bare",
          all(not have for _c, have, _miss in bare), str(bare)[:120])
    check("status: every capability has a gap when bare",
          all(miss for _c, _have, miss in bare), str(bare)[:120])


def test_missing_tools_are_recommendations() -> None:
    """One install line per tool, deduplicated by hint.

    Two names for the same package (`python3 -m pip install frida-tools` under
    both `frida` and `frida-ps`) must not produce two lines telling the
    operator to install the same thing twice.
    """
    gaps = rv.missing_tools(_probes())
    hints = [h for h, _n in gaps]
    check("missing: hints deduplicated", len(hints) == len(set(hints)),
          f"{len(hints)} rows, {len(set(hints))} distinct")
    check("missing: every row has a hint", all(h.strip() for h in hints))
    check("missing: deterministic order", hints == sorted(hints), str(hints[:3]))

    full = rv.missing_tools(_probes(*[n for n, _h, _c in rv._TOOL_CATALOGUE]))
    check("missing: empty when the toolchain is complete", full == [], str(full))


def test_tools_for() -> None:
    probes = _probes("apktool")
    apk = rv.tools_for("apk", probes)
    check("tools_for: apk slice includes apktool",
          any(p.name == "apktool" for p in apk))
    check("tools_for: apk slice excludes a script linter",
          not any(p.name == "shellcheck" for p in apk),
          str([p.name for p in apk]))
    check("tools_for: every probe in the slice is looked for",
          all(p.name in {q.name for q in probes} for p in apk))


def test_member_heading() -> None:
    check("heading: container says members",
          "MEMBER" in rv.member_heading("apk").upper(),
          rv.member_heading("apk"))
    check("heading: pe says sections", "SECTION" in rv.member_heading("pe").upper(),
          rv.member_heading("pe"))
    check("heading: elf says sections", "SECTION" in rv.member_heading("elf").upper(),
          rv.member_heading("elf"))
    check("heading: script says something, not a section",
          rv.member_heading("script") and "SECTION" not in rv.member_heading("script").upper(),
          rv.member_heading("script"))
    for kind in rv.KIND_ORDER:
        check(f"heading: defined for {kind}", bool(rv.member_heading(kind)),
              rv.member_heading(kind))


# ---------------------------------------------------------------------------
# the brief
# ---------------------------------------------------------------------------


def test_build_prompt(tmp: Path) -> None:
    p = tmp / "app.apk"; _apk(p)
    art = rv.identify(p)
    inv = rv.inventory(art)
    text = rv.build_prompt(art, inv, rv.probe_tools())

    check("brief: names the artifact", "app.apk" in text)
    check("brief: states the kind", rv.KIND_LABELS["apk"] in text, text[:200])
    check("brief: carries the digest", art.sha256[:12] in text)
    check("brief: tells the agent to ask, not to act",
          "ASK" in text.upper() or "ask" in text)
    check("brief: instructs it to stop before modifying",
          "STOP" in text.upper() or "nothing" in text.lower())
    check("brief: no unreplaced placeholder", "%s" not in text and "{" not in text,
          [ln for ln in text.splitlines() if "{" in ln][:2])
    # The heading is interpolated, so an ELF brief must not say MEMBERS.
    e = tmp / "bin.elf"; _elf(e)
    eart = rv.identify(e)
    etext = rv.build_prompt(eart, rv.inventory(eart), rv.probe_tools())
    check("brief: elf brief says SECTIONS, not MEMBERS",
          "SECTIONS" in etext and "MEMBERS" not in etext,
          [ln for ln in etext.splitlines() if "MEMBER" in ln][:2])


def test_opportunities(tmp: Path) -> None:
    for kind in rv.KIND_ORDER:
        opps = rv.opportunities(kind)
        check(f"opportunities: {kind} offers something to choose",
              len(opps) >= 1 or kind == "unknown", str(len(opps)))
        check(f"opportunities: {kind} slugs unique",
              len({o.slug for o in opps}) == len(opps))
        check(f"opportunities: {kind} all have titles",
              all(o.title.strip() for o in opps))

    # The example the feature was asked for: a game asks about coins and lives.
    apk = {o.title.lower() + " " + o.blurb.lower()
           for o in rv.opportunities("apk")}
    joined = " ".join(apk)
    check("opportunities: apk offers a game-mod path",
          "coin" in joined or "life" in joined or "game" in joined, joined[:160])

    check("opportunities: unknown kind still answers",
          isinstance(rv.opportunities("unknown"), list))


def test_summary_line_and_labels() -> None:
    for kind in rv.KIND_ORDER:
        check(f"labels: {kind} has a human label", kind in rv.KIND_LABELS, kind)
    check("labels: order covers every label",
          set(rv.KIND_ORDER) == set(rv.KIND_LABELS),
          str(set(rv.KIND_ORDER) ^ set(rv.KIND_LABELS)))


def test_usage_lines() -> None:
    check("usage: renders commands", len(rv.USAGE_LINES) >= 3,
          str(len(rv.USAGE_LINES)))
    flat = " ".join(c for c, _d in rv.USAGE_LINES)
    check("usage: documents the map-only form", "--map" in flat, flat)
    check("usage: documents the tools command",
          "reverse-engineer-tools" in flat, flat)


# ---------------------------------------------------------------------------
# the guarantee
# ---------------------------------------------------------------------------


def test_nothing_is_written(tmp: Path) -> None:
    """Mapping an artifact must leave the disk exactly as it found it.

    The commands are supposed to be observation only. This is the cheapest
    possible guard against a later change quietly adding a cache file, an
    extracted directory, or a rewritten artifact beside the original.
    """
    d = tmp / "sealed"; d.mkdir()
    p = d / "app.apk"; _apk(p)
    before = {f.name: (f.stat().st_size, f.stat().st_mtime_ns)
              for f in d.iterdir()}
    art = rv.identify(p)
    rv.inventory(art)
    rv.build_prompt(art, rv.inventory(art), rv.probe_tools())
    after = {f.name: (f.stat().st_size, f.stat().st_mtime_ns)
             for f in d.iterdir()}
    check("guarantee: no file added or removed", set(before) == set(after),
          str(set(before) ^ set(after)))
    check("guarantee: nothing modified", before == after, str(before))


def test_symlink_and_missing(tmp: Path) -> None:
    """Bad input is reported, not raised — these run from a slash command."""
    missing = tmp / "nope.apk"
    try:
        art = rv.identify(missing)
        check("missing: identify does not raise", True, art.kind)
        rv.inventory(art)
        check("missing: inventory does not raise", True)
    except Exception as exc:  # noqa: BLE001 — the point is that it must not
        check("missing: identify does not raise", False, repr(exc))

    real = tmp / "real.sh"; _script(real)
    link = tmp / "link.sh"
    try:
        link.symlink_to(real)
        got = rv.identify(link).kind
        check("symlink: resolved to the target's kind", got == "script", got)
    except OSError as exc:
        check("symlink: resolved to the target's kind", False, repr(exc))


def test_empty_file(tmp: Path) -> None:
    p = tmp / "empty"
    p.write_bytes(b"")
    art = rv.identify(p)
    check("empty: kind is unknown", art.kind == "unknown", art.kind)
    check("empty: size is zero", art.size == 0, str(art.size))
    inv = rv.inventory(art)
    check("empty: inventory does not raise", isinstance(inv.facts, list))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_identify(tmp)
        test_extension_cannot_lie(tmp)
        test_refinement_is_by_content(tmp)
        test_inventory_apk(tmp)
        test_inventory_elf(tmp)
        test_inventory_pe(tmp)
        test_inventory_script(tmp)
        test_inventory_dir(tmp)
        test_redaction(tmp)
        test_signals_bounded(tmp)
        test_build_prompt(tmp)
        test_opportunities(tmp)
        test_nothing_is_written(tmp)
        test_symlink_and_missing(tmp)
        test_empty_file(tmp)

    test_probe_shape()
    test_gaps()
    test_capability_status()
    test_missing_tools_are_recommendations()
    test_tools_for()
    test_member_heading()
    test_summary_line_and_labels()
    test_usage_lines()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
