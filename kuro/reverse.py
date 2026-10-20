"""`/reverse-engineer` — map an artifact, then ask what to change.

Two questions, answered by two different means.

*What is this, and what is in it* is answered deterministically and offline:
:func:`identify` sniffs the format from its own bytes and :func:`inventory`
reads the headers and the member table. No model, no network, and no execution
of the artifact — a binary that is never run cannot misbehave, which matters
because this command is pointed at files that arrived from anywhere.

*What could I do with it here* is answered by :func:`probe_tools`, which asks
which reverse-engineering tools this host actually has. Same reason ``ad.py``
probes before it writes a brief: a plan built around a tool that is not
installed wastes the whole turn, and the operator would rather learn that
before the run than during it.

*What do you want to change* is deliberately not answered here. A catalogue of
modifications exists (:func:`opportunities`) so the briefing has somewhere to
start, but the question that matters is about the *product*, not the format —
a Flappy Bird clone wants unlimited coins and no ad breaks; an obfuscated SDK
wants its network calls mapped. That is a judgement about what the artifact
*is*, and it belongs to the model reading the map rather than to a table of
file extensions. So the command maps, renders, and hands the map to the agent
in a turn whose only job is to make the operator choose before it touches
anything.

Nothing in this module writes, executes, or fetches. The turn that follows
does the work, under the operator's own mode and scope rules.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# kinds
# ---------------------------------------------------------------------------

#: Every kind this module can name, most specific first. ``unknown`` is a real
#: answer, not a failure: an artifact that is none of the others still gets
#: hashed, sized and handed to the agent with its header bytes described.
KIND_ORDER: tuple[str, ...] = (
    "apk", "jar", "zip", "dex", "pe", "elf", "macho", "script", "dir", "unknown",
)

KIND_LABELS: dict[str, str] = {
    "apk": "Android package",
    "jar": "Java archive",
    "zip": "archive",
    "dex": "Dalvik bytecode",
    "pe": "Windows PE",
    "elf": "ELF binary",
    "macho": "Mach-O binary",
    "script": "script",
    "dir": "directory",
    "unknown": "unrecognised",
}

#: Signature → kind, checked against the first bytes of the file. Zip is
#: handled apart from this table because its signature only says "container";
#: what is *inside* decides whether it is an APK, a jar or a plain archive.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x7fELF", "elf"),
    (b"MZ", "pe"),
    (b"dex\n", "dex"),
    (b"\xca\xfe\xba\xbe", "macho"),   # fat, big-endian
    (b"\xbe\xba\xfe\xca", "macho"),   # fat, little-endian
    (b"\xcf\xfa\xed\xfe", "macho"),   # 64-bit LE
    (b"\xce\xfa\xed\xfe", "macho"),   # 32-bit LE
    (b"\xfe\xed\xfa\xcf", "macho"),   # 64-bit BE
    (b"\xfe\xed\xfa\xce", "macho"),   # 32-bit BE
)

#: Extension → kind. Only consulted when the bytes say nothing, because a
#: ``.zip`` that is really a jar, or a ``.bin`` that is really an ELF, is
#: common and the extension is the weaker signal of the two.
#:
#: ``.bin`` is deliberately absent. It is a catch-all name rather than a
#: format — half the binaries people hand you are called that, and so is half
#: the firmware, and a raw blob named ``.bin`` is neither an ELF nor anything
#: else. Naming it from the header or not at all is the honest answer.
_EXTENSIONS: dict[str, str] = {
    ".apk": "apk", ".apks": "zip", ".xapk": "zip", ".aab": "zip",
    ".jar": "jar", ".war": "zip", ".ear": "zip", ".aar": "zip",
    ".zip": "zip", ".tar": "zip", ".gz": "zip", ".xz": "zip", ".7z": "zip",
    ".dex": "dex", ".odex": "dex", ".vdex": "dex",
    ".exe": "pe", ".dll": "pe", ".sys": "pe", ".scr": "pe",
    ".elf": "elf", ".so": "elf", ".o": "elf", ".ko": "elf",
    ".dylib": "macho", ".macho": "macho",
    ".sh": "script", ".bash": "script", ".zsh": "script", ".fish": "script",
    ".py": "script", ".rb": "script", ".pl": "script", ".js": "script",
    ".ps1": "script", ".bat": "script", ".cmd": "script",
}

#: Kinds whose header is definitive. An extension may name one of these, but
#: only the bytes may confirm it: a random 200 KB file called ``sample.bin``
#: is not an ELF, and reporting its first four bytes as an ``e_ident`` and a
#: garbage ``e_type`` is worse than reporting that the format is unknown.
_HEADER_KINDS: frozenset[str] = frozenset({"elf", "pe", "dex", "macho"})

#: Kinds that are really zip files underneath, and so are worth opening to see
#: which one they are.
_CONTAINER_KINDS: frozenset[str] = frozenset({"zip", "apk", "jar"})

_SNIFF_BYTES = 4096

#: How much of a file the string scan will read. A 200 MB binary would
#: otherwise be walked in full on the render path; the interesting strings in
#: a real artifact are overwhelmingly in its first few megabytes.
_MAX_SCAN = 4 * 1024 * 1024

#: How many members of a container to list. The point is the shape of the
#: archive, not an exhaustive index — an APK routinely holds thousands of
#: resource files and the agent needs the thirteen that carry logic.
_MAX_MEMBERS = 40


def human_size(n: int) -> str:
    """Bytes as a short, readable string."""
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < step or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TiB"


# ---------------------------------------------------------------------------
# identification
# ---------------------------------------------------------------------------


@dataclass
class Artifact:
    """One file, described. Everything here is read-only fact."""

    path: Path
    kind: str
    size: int
    sha256: str
    #: The signature bytes that named the kind, for the case where the guess
    #: is wrong and the operator wants to see what it was based on.
    magic: str = ""
    #: Container members: name and uncompressed size, capped.
    entries: list[tuple[str, int]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_container(self) -> bool:
        return self.kind in ("apk", "jar", "zip")


def _sha256(path: Path, cap: int = 512 * 1024 * 1024) -> str:
    """Digest of the file, streamed.

    Streamed rather than read whole because this runs before anything else and
    an artifact can be arbitrarily large; capped so a multi-gigabyte image does
    not stall the command. A capped digest is marked as such by the caller
    seeing ``partial`` in the notes, not silently.
    """
    h = hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as fh:
            while read < cap:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
    except OSError:
        # An unreadable path has no digest. Returning "" rather than raising is
        # what lets `identify` keep the promise in its docstring: this call is
        # unconditional, so a raise here is a raise for every caller — and a
        # file that cannot be read is exactly the case the caller is trying to
        # describe, not an exception to it.
        return ""
    return h.hexdigest()


def _sniff(head: bytes) -> tuple[str, str]:
    """(kind, magic) from the first bytes. ``("", "")`` when nothing matches."""
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip", "PK (zip container)"
    for sig, kind in _MAGIC:
        if head.startswith(sig):
            shown = "".join(chr(b) if 32 <= b < 127 else "." for b in sig)
            return kind, shown
    if head.startswith(b"#!"):
        first = head.split(b"\n", 1)[0][:60].decode("utf-8", "replace")
        return "script", first
    return "", ""


def _header_confirms(head: bytes, kind: str) -> bool:
    """Whether the leading bytes really match *kind*'s signature."""
    return any(k == kind and head.startswith(sig) for sig, k in _MAGIC)


def _refine_container(path: Path, kind: str) -> str:
    """Split the zip family: APK, jar, or a plain archive.

    The signature only says "zip". What separates an APK from a jar from a
    distribution archive is the member list, so the member list is what gets
    asked — ``AndroidManifest.xml`` (or a bare ``classes*.dex``) means Android;
    ``META-INF/MANIFEST.MF`` without those means Java. Anything else is a
    plain archive, and a container that will not open stays whatever it was
    called rather than being promoted on a name alone.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return kind
    if "AndroidManifest.xml" in names or any(
            n.endswith(".dex") and "/" not in n for n in names):
        return "apk"
    if "META-INF/MANIFEST.MF" in names:
        return "jar"
    return "zip" if kind in _CONTAINER_KINDS else kind


def identify(path: Path) -> Artifact:
    """Describe *path*: what kind of thing it is, and its basic facts.

    Never raises on a file it cannot parse — an unreadable header yields
    ``kind="unknown"`` with whatever was read, because "I could not name this"
    is information the operator wants, and a traceback is not.
    """
    path = Path(path)
    if path.is_dir():
        return Artifact(path=path, kind="dir", size=0, sha256="")

    # stat and open are guarded together: they fail for the same reasons — a
    # missing file, a permission denied, a path that is a socket — and a raise
    # from the line above the handler would defeat the contract this docstring
    # makes. A path that cannot be read is "unknown, size 0", not a traceback.
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(_SNIFF_BYTES)
    except OSError:
        size, head = 0, b""

    kind, magic = _sniff(head)
    if kind:
        if kind in _CONTAINER_KINDS:
            kind = _refine_container(path, kind)
    else:
        # Nothing in the bytes. The extension gets a say, but only a confirmed
        # one: see _HEADER_KINDS.
        ext_kind = _EXTENSIONS.get(path.suffix.lower(), "")
        if ext_kind in _HEADER_KINDS and not _header_confirms(head, ext_kind):
            kind, magic = "unknown", ""
        else:
            kind = ext_kind or "unknown"
            magic = f"{path.suffix} extension" if kind != "unknown" else ""
            if kind in _CONTAINER_KINDS:
                kind = _refine_container(path, kind)

    art = Artifact(
        path=path, kind=kind, size=size, sha256=_sha256(path), magic=magic,
    )
    if art.is_container:
        art.entries = _container_members(path)
    return art


def _container_members(path: Path) -> list[tuple[str, int]]:
    """The member list, capped and ordered by size.

    Ordered by uncompressed size because that is what surfaces the logic: in
    an APK the multi-megabyte entries are ``classes.dex`` and the native
    libraries, while the thousands of small entries are icons.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            items = [(i.filename, i.file_size) for i in zf.infolist()
                     if not i.is_dir()]
    except (zipfile.BadZipFile, OSError):
        return []
    items.sort(key=lambda kv: kv[1], reverse=True)
    return items[:_MAX_MEMBERS]


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


@dataclass
class Inventory:
    """What reading the artifact produced. Facts, then interpretation."""

    facts: list[tuple[str, str]] = field(default_factory=list)
    members: list[tuple[str, int]] = field(default_factory=list)
    #: (category, value) for strings worth surfacing. Secret-shaped values are
    #: redacted here rather than downstream, so they cannot leak into a report
    #: by someone rendering this list without thinking about it.
    signals: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


#: What a string has to look like to be worth showing. Order matters: the
#: first match wins, so the more specific patterns are checked first.
_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{4,120}")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("KEYLIKE", re.compile(
        r"\b(?:sk|pk|rk|ghp|gho|AKIA|AIza|tj|lfu)[_-][A-Za-z0-9_-]{16,}\b")),
    ("host", re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ai|dev|app)\b")),
    ("path", re.compile(r"(?:^|[\"'])(?:/[A-Za-z0-9._-]+){2,}/?")),
    ("android", re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,}\b")),
)

#: Cap on each signal category and on the total, so a binary with ten thousand
#: URLs does not fill the transcript.
_MAX_PER_SIGNAL = 8
_MAX_SIGNALS = 32

_PRINTABLE = re.compile(rb"[\x20-\x7e]{6,}")


def _strings(path: Path, limit: int = _MAX_SCAN) -> tuple[list[str], bool]:
    """Printable runs from the start of the file. Returns (strings, truncated)."""
    try:
        with path.open("rb") as fh:
            blob = fh.read(limit)
            truncated = len(blob) == limit and path.stat().st_size > limit
    except OSError:
        return [], False
    out: list[str] = []
    # Bounded: a blob of printable bytes could be large, and this is a sample.
    for m in _PRINTABLE.finditer(blob):
        out.append(m.group(0).decode("ascii", "replace"))
        if len(out) >= 20000:
            break
    return out, truncated


def _collect_signals(strings: list[str]) -> list[tuple[str, str]]:
    """Categorise interesting strings, redacting anything secret-shaped.

    Redaction happens here, at collection, rather than at render: a category
    named ``KEYLIKE`` is one somebody will eventually print, and the value it
    matched is exactly the value that must not be printed when they do.
    """
    seen: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for s in strings:
        for category, pattern in _SIGNAL_PATTERNS:
            m = pattern.search(s)
            if not m:
                continue
            value = m.group(0)
            if category == "KEYLIKE":
                value = "[REDACTED_SECRET]"
            elif category == "email":
                value = "[REDACTED_EMAIL]"
            key = (category, value)
            if key in seen:
                continue
            if counts.get(category, 0) >= _MAX_PER_SIGNAL:
                continue
            seen.add(key)
            counts[category] = counts.get(category, 0) + 1
            out.append((category, value))
            break
        if len(out) >= _MAX_SIGNALS:
            break
    return out


def _inventory_archive(art: Artifact, inv: Inventory) -> None:
    """APK / jar / zip: the member table, and what it says about the build."""
    try:
        with zipfile.ZipFile(art.path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, OSError) as exc:
        inv.notes.append(f"could not read the archive: {exc}")
        return

    names = [i.filename for i in infos]
    total = sum(i.file_size for i in infos)
    inv.facts.append(("members", f"{len(infos)} files, {human_size(total)} unpacked"))

    dexes = [n for n in names if n.endswith(".dex") and "/" not in n]
    if dexes:
        inv.facts.append(("dex", ", ".join(sorted(dexes)[:8])))
    so = [n for n in names if n.startswith("lib/") and n.endswith(".so")]
    if so:
        abis = sorted({n.split("/")[1] for n in so if len(n.split("/")) > 2})
        inv.facts.append(
            ("native libs",
             f"{len(so)} across {', '.join(abis) or 'unknown ABIs'}"))

    encrypted = [i.filename for i in infos if i.flag_bits & 0x1]
    if encrypted:
        inv.notes.append(
            f"{len(encrypted)} entries are encrypted — most tools will not "
            "read them without the password")

    # Signature scheme. v1 lives in META-INF; v2/v3 live in a signing block
    # whose magic sits near the end of the file, outside the zip directory —
    # which is why an APK can look unsigned to `unzip -l` and still be signed.
    v1 = [n for n in names if n.startswith("META-INF/")
          and n.upper().endswith((".RSA", ".DSA", ".EC", ".SF"))]
    schemes = []
    if v1:
        schemes.append("v1 (META-INF)")
    try:
        tail = art.path.open("rb")
        tail.seek(max(0, art.size - 131072))
        if b"APK Sig Block 42" in tail.read():
            schemes.append("v2/v3 (signing block)")
        tail.close()
    except OSError:
        pass
    inv.facts.append(("signature", ", ".join(schemes) if schemes else "unsigned"))

    inv.members = [(n, s) for n, s in art.entries]

    if "AndroidManifest.xml" in names:
        inv.notes.append(
            "the manifest is binary AXML — readable with apktool or aapt, "
            "not with a text editor")
    if "resources.arsc" in names:
        inv.notes.append("resources.arsc carries the compiled resource table")
    if any(n.startswith("assets/") for n in names):
        assets = [n for n in names if n.startswith("assets/")]
        inv.notes.append(
            f"{len(assets)} files under assets/ — where games and apps keep "
            "data that is not compiled, and the easiest thing to edit")
    if any(n.startswith("META-INF/") and n.endswith(".version") for n in names):
        inv.notes.append("built with the Android Gradle plugin (source map present)")


def _elf_facts(art: Artifact, inv: Inventory) -> None:
    """ELF: class, machine, type, and whether symbols survived."""
    try:
        with art.path.open("rb") as fh:
            blob = fh.read(4096)
    except OSError:
        return
    if len(blob) < 20:
        inv.notes.append("truncated before the ELF header finished")
        return

    bits = 64 if blob[4] == 2 else 32
    little = blob[5] == 1
    order = "<" if little else ">"
    inv.facts.append(("class", f"ELF{bits}, {'little' if little else 'big'}-endian"))
    inv.facts.append(("abi", f"OSABI {blob[7]}"))

    try:
        if bits == 64:
            e_type, e_machine = struct.unpack_from(order + "HH", blob, 16)
            e_entry, = struct.unpack_from(order + "Q", blob, 24)
            e_shoff, = struct.unpack_from(order + "Q", blob, 40)
            e_shnum, = struct.unpack_from(order + "H", blob, 60)
        else:
            e_type, e_machine = struct.unpack_from(order + "HH", blob, 16)
            e_entry, = struct.unpack_from(order + "I", blob, 24)
            e_shoff, = struct.unpack_from(order + "I", blob, 32)
            e_shnum, = struct.unpack_from(order + "H", blob, 48)
    except struct.error:
        return

    inv.facts.append(("type", ELF_TYPES.get(e_type, f"type {e_type}")))
    inv.facts.append(("machine", ELF_MACHINES.get(e_machine, f"machine {e_machine}")))
    inv.facts.append(("entry", f"0x{e_entry:x}"))
    if e_type == 4:
        inv.notes.append(
            "ET_DYN — a shared object or a PIE executable; the two are told "
            "apart by the presence of an interpreter, not by this header")
    if e_shnum == 0:
        inv.notes.append("no section headers — the binary has been stripped hard")
    else:
        inv.facts.append(("sections", str(e_shnum)))


#: e_type values that are worth naming.
ELF_TYPES: dict[int, str] = {
    0: "ET_NONE", 1: "ET_REL (object)", 2: "ET_EXEC (fixed-address)",
    3: "ET_DYN (shared / PIE)", 4: "ET_CORE",
}

#: The machines that actually show up in artifacts people reverse.
ELF_MACHINES: dict[int, str] = {
    3: "x86", 8: "MIPS", 20: "PowerPC", 40: "ARM", 62: "x86-64",
    183: "AArch64", 243: "RISC-V",
}


def _pe_facts(art: Artifact, inv: Inventory) -> None:
    """PE: machine, subsystem, mitigations, and the section table."""
    try:
        with art.path.open("rb") as fh:
            blob = fh.read(8192)
    except OSError:
        return
    if len(blob) < 0x40:
        inv.notes.append("too short to hold a DOS header")
        return

    try:
        e_lfanew, = struct.unpack_from("<I", blob, 0x3C)
    except struct.error:
        return
    if blob[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        inv.notes.append(
            f"MZ header present but no PE signature at 0x{e_lfanew:x} — a DOS "
            "stub or a self-extracting archive rather than a real PE")
        return

    coff = e_lfanew + 4
    try:
        machine, nsec, _stamp, _psym, _nsym, opt_size, _chars = struct.unpack_from(
            "<HHIIIHH", blob, coff)
    except struct.error:
        return
    inv.facts.append(("machine", PE_MACHINES.get(machine, f"machine 0x{machine:x}")))
    inv.facts.append(("sections", str(nsec)))

    opt = coff + 20
    magic, = struct.unpack_from("<H", blob, opt)
    inv.facts.append((
        "format",
        {0x10b: "PE32", 0x20b: "PE32+ (64-bit)"}.get(magic, f"magic 0x{magic:x}")))

    try:
        subsystem, dllchars = struct.unpack_from("<HH", blob, opt + 68)
        inv.facts.append((
            "subsystem", PE_SUBSYSTEMS.get(subsystem, f"subsystem {subsystem}")))
        guards = []
        if dllchars & 0x0100:
            guards.append("NX")
        if dllchars & 0x0040:
            guards.append("CFG")
        if dllchars & 0x4000:
            guards.append("ASLR")
        inv.facts.append(("mitigations", ", ".join(guards) if guards else "none"))
    except struct.error:
        pass

    # Section table: name, virtual size, virtual address, raw size, raw offset.
    sec = opt + opt_size
    rows = []
    for i in range(min(nsec, 24)):
        off = sec + i * 40
        try:
            name, vsize, vaddr, rsize, _raw = struct.unpack_from(
                "<8sIIII", blob, off)
        except struct.error:
            break
        rows.append((name.rstrip(b"\0").decode("ascii", "replace"),
                     vaddr, rsize))
    if rows:
        inv.members = [(f"{n}  0x{v:x}", s) for n, v, s in rows]

    if ".NET" in "".join(n for n, _, _ in rows) or _has_com_descriptor(blob, opt, opt_size):
        inv.notes.append(
            "managed .NET assembly — decompiles to near-original source with "
            "ilspycmd or dnSpy, which is a different problem to native code")


def _has_com_descriptor(blob: bytes, opt: int, opt_size: int) -> bool:
    """Whether the PE declares a CLR header (data directory 14)."""
    # The data directories sit at the end of the optional header; entry 14 is
    # the CLR runtime header and is non-zero only for managed assemblies.
    dirs = opt + (96 if struct.unpack_from("<H", blob, opt)[0] == 0x10b else 112)
    try:
        rva, size = struct.unpack_from("<II", blob, dirs + 14 * 8)
    except struct.error:
        return False
    return rva != 0 and size != 0


PE_MACHINES: dict[int, str] = {
    0x14c: "x86", 0x8664: "x86-64", 0x1c0: "ARM", 0x1c4: "ARMv7",
    0xaa64: "ARM64", 0x200: "IA64",
}

PE_SUBSYSTEMS: dict[int, str] = {
    1: "native", 2: "Windows GUI", 3: "Windows console", 9: "Windows CE",
    10: "EFI application", 14: "Xbox", 16: "Windows boot",
}


def _inventory_script(art: Artifact, inv: Inventory) -> None:
    """A script: shebang, size, and syntax-checkability, not execution."""
    try:
        text = art.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.splitlines()
    inv.facts.append(("lines", f"{len(lines):,}"))
    shebang = lines[0] if lines and lines[0].startswith("#!") else ""
    if shebang:
        inv.facts.append(("shebang", shebang[:80]))
    interpreter = shebang.split("/")[-1].split()[0] if shebang else art.path.suffix
    if interpreter in ("bash", "sh", "dash", "zsh"):
        inv.notes.append(f"`{interpreter} -n {art.path.name}` checks the syntax "
                         "without running a line of it")
    elif interpreter in ("python", "python3"):
        inv.notes.append(
            f"`python3 -m py_compile {art.path.name}` checks it without "
            "executing it; `-I` if you ever do run it")
    if re.search(r"(?i)\b(rm\s+-rf|curl\s+[^|]*\|\s*(?:ba)?sh|base64\s+-d|"
                 r"eval\s|chmod\s+777|/dev/tcp/)", text):
        inv.notes.append(
            "contains shell patterns that are also common in droppers "
            "(piped shells, base64 decoding, eval) — read it before running it")


def _inventory_dir(art: Artifact, inv: Inventory) -> None:
    """A directory: the shape of the tree, and the artifacts inside it."""
    try:
        kids = sorted(art.path.iterdir())
    except OSError:
        return
    files = [k for k in kids if k.is_file()]
    dirs = [k for k in kids if k.is_dir()]
    inv.facts.append(("entries", f"{len(files)} files, {len(dirs)} directories"))
    # The literal extension, not the mapped artifact kind. A census answers
    # "what is this tree made of", and `_EXTENSIONS` is lossy on purpose for a
    # single file — it collapses `.md`, `.json` and `.log` into `unknown`
    # because none of them needs a different tool. Tallying by kind would
    # report a documentation tree as "unknown×400" and tell the operator
    # nothing, which is the opposite of what a census is for.
    kinds: dict[str, int] = {}
    for f in files:
        ext = f.suffix.lower().lstrip(".") or "(no extension)"
        kinds[ext] = kinds.get(ext, 0) + 1
    if kinds:
        inv.facts.append(("by extension", ", ".join(
            f"{k}×{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))))
    inv.members = [(f.name, f.stat().st_size) for f in files[:20]]


def inventory(art: Artifact) -> Inventory:
    """Read the artifact and describe what is in it. Writes nothing."""
    inv = Inventory()
    if art.kind == "dir":
        _inventory_dir(art, inv)
        return inv

    if art.is_container:
        _inventory_archive(art, inv)
    elif art.kind == "elf":
        _elf_facts(art, inv)
    elif art.kind == "pe":
        _pe_facts(art, inv)
    elif art.kind == "script":
        _inventory_script(art, inv)
    elif art.kind == "dex":
        inv.notes.append(
            "raw Dalvik bytecode — `baksmali d` disassembles it, `jadx` goes "
            "straight to readable Java")

    strings, truncated = _strings(art.path)
    inv.signals = _collect_signals(strings)
    if truncated:
        inv.notes.append(
            f"string scan read the first {human_size(_MAX_SCAN)} of "
            f"{human_size(art.size)}")
    if art.kind == "unknown":
        inv.notes.append(
            "format not recognised from the bytes or the extension — the "
            "header bytes are the place to start")
    return inv


# ---------------------------------------------------------------------------
# what could be changed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Opportunity:
    """One class of modification, as a conversation-starter for the agent."""

    slug: str
    title: str
    blurb: str


#: Candidates per kind. These are *offers*, not a plan: the briefing tells the
#: agent to keep the ones that apply to the product in front of it and drop the
#: rest. A game has coins; a banking app does not, and a menu that lists
#: "unlimited coins" for a banking app is a menu nobody trusts.
_OPPORTUNITIES: dict[str, tuple[Opportunity, ...]] = {
    "apk": (
        Opportunity("manifest", "AndroidManifest.xml",
                    "permissions, min/target SDK, package name, exported "
                    "components, network security config path"),
        Opportunity("smali", "the app's own code",
                    "rebuild with apktool and edit smali directly — checks, "
                    "limits, counters, licence gates"),
        Opportunity("resources", "resources",
                    "App name, strings, icons, colours; resources.arsc holds "
                    "the compiled table"),
        Opportunity("assets", "assets/",
                    "uncompiled app and game data: level files, save games, "
                    "JSON config, tuning constants"),
        Opportunity("libs", "native libraries",
                    "lib/<abi>/*.so when the logic is compiled rather than "
                    "in dex — a different skill set and a different toolchain"),
        Opportunity("ads", "advertising and telemetry",
                    "drop the SDKs, the permissions they pull in, and the "
                    "hostnames they call"),
        Opportunity("detect", "protection checks",
                    "root, emulator, tamper, signature and licence checks, and "
                    "what each one gates"),
        Opportunity("resign", "repackage and re-sign",
                    "zipalign plus apksigner; an edited APK will not install "
                    "until it is signed"),
    ),
    "dex": (
        Opportunity("decompile", "decompile to Java",
                    "readable output before deciding anything"),
        Opportunity("smali", "edit bytecode",
                    "baksmali, edit, smali, rebuild"),
    ),
    "jar": (
        Opportunity("decompile", "decompile the classes",
                    "jadx or a Java decompiler over the class files"),
        Opportunity("resources", "edit resources",
                    "MANIFEST.MF, properties files, bundled config"),
        Opportunity("code", "patch bytecode",
                    "edit under a debugger or rewrite the class outright"),
    ),
    "zip": (
        Opportunity("extract", "extract and inspect",
                    "the contents decide everything else"),
        Opportunity("modify", "modify and repack",
                    "edit members in place, preserving the archive layout"),
    ),
    "elf": (
        Opportunity("strings", "strings and symbols",
                    "the cheapest map of a stripped binary"),
        Opportunity("disasm", "disassemble",
                    "radare2/rizin or Ghidra for structure, objdump for a dump"),
        Opportunity("checks", "find the checks",
                    "licence, trial, integrity and environment tests, and the "
                    "branch that gates each one"),
        Opportunity("patch", "patch a branch",
                    "invert or NOP the conditional once it is located"),
        Opportunity("unpack", "unpack",
                    "UPX and other packers, before the disassembly is worth "
                    "anything"),
        Opportunity("debug", "debug it",
                    "gdb or ltrace against a copy, in a sandbox"),
    ),
    "pe": (
        Opportunity("strings", "strings and imports",
                    "imports say what the program can do far faster than the "
                    "disassembly does"),
        Opportunity("disasm", "disassemble",
                    "Ghidra, or a .NET decompiler when it is managed"),
        Opportunity("checks", "find the checks",
                    "licence, trial, integrity and environment tests"),
        Opportunity("patch", "patch a branch",
                    "invert or NOP the conditional once it is located"),
        Opportunity("unpack", "unpack",
                    "UPX and friends, and the resource section"),
        Opportunity("debug", "debug it",
                    "a debugger or a sandboxed run"),
    ),
    "macho": (
        Opportunity("strings", "strings and symbols", "the first pass"),
        Opportunity("disasm", "disassemble", "Ghidra, Hopper or otool"),
        Opportunity("patch", "patch", "once the target branch is located"),
    ),
    "script": (
        Opportunity("read", "read it end to end",
                    "a script is source — there is nothing to reverse"),
        Opportunity("check", "syntax-check without running",
                    "bash -n / py_compile, so a hostile script is not executed "
                    "to find out what it does"),
        Opportunity("edit", "edit behaviour",
                    "constants, endpoints, paths, guards"),
    ),
    "dir": (
        Opportunity("triage", "triage the contents",
                    "identify the artifacts in the tree before choosing one"),
    ),
    "unknown": (
        Opportunity("header", "identify the format",
                    "the leading bytes are the place to start"),
        Opportunity("read", "read-only first pass",
                    "strings, entropy, and the header, before any tool is "
                    "pointed at it"),
    ),
}


def opportunities(kind: str) -> list[Opportunity]:
    """The candidate modifications for *kind*, or the generic set."""
    return list(_OPPORTUNITIES.get(kind, _OPPORTUNITIES["unknown"]))


# ---------------------------------------------------------------------------
# tooling probe
# ---------------------------------------------------------------------------

#: Capabilities, in the order the report shows them. A capability is a *kind of
#: work*, not a program: `unpack` is satisfied by any one of unzip, 7z or
#: binwalk, and the report cares whether the work is possible, not which of the
#: three is installed.
CAPABILITY_ORDER: tuple[str, ...] = (
    "detect", "unpack", "archive", "package", "sign", "decompile", "dex",
    "native", "disasm", "debug", "hook", "proxy", "script", "hex", "emulate",
)

CAPABILITY_LABELS: dict[str, str] = {
    "detect": "identify formats",
    "unpack": "unpack / decompress",
    "archive": "read and write archives",
    "package": "unpack and repack Android packages",
    "sign": "sign Android packages",
    "decompile": "decompile",
    "dex": "disassemble Dalvik bytecode",
    "native": "read native binaries",
    "disasm": "disassemble",
    "debug": "debug",
    "hook": "instrument a running app",
    "proxy": "intercept traffic",
    "script": "read and check scripts",
    "hex": "inspect bytes",
    "emulate": "run it isolated",
}

#: (command, install hint, capability). ``shutil.which`` decides presence, so a
#: name here has to be the thing that actually resolves on PATH.
_TOOL_CATALOGUE: tuple[tuple[str, str, str], ...] = (
    # --- the cheap universal pass
    ("file", "file", "detect"),
    ("exiftool", "libimage-exiftool-perl", "detect"),
    ("die", "detect-it-easy", "detect"),
    ("binwalk", "binwalk", "unpack"),
    ("strings", "binutils", "native"),
    ("floss", "flare-floss", "native"),
    ("xxd", "xxd / vim-common", "hex"),
    ("hexdump", "bsdmainutils / util-linux", "hex"),
    # --- archives
    ("unzip", "unzip", "archive"),
    ("zip", "zip", "archive"),
    ("7z", "p7zip-full", "archive"),
    ("zipinfo", "unzip", "archive"),
    ("upx", "upx-ucl", "unpack"),
    ("zipalign", "Android build-tools", "sign"),
    # --- android
    ("apktool", "apktool", "package"),
    ("aapt", "Android build-tools", "package"),
    ("aapt2", "Android build-tools", "package"),
    ("apksigner", "Android build-tools", "sign"),
    ("keytool", "openjdk", "sign"),
    ("uber-apk-signer", "uber-apk-signer", "sign"),
    ("jadx", "jadx", "decompile"),
    ("jadx-gui", "jadx", "decompile"),
    ("dex2jar", "dex2jar", "decompile"),
    ("d2j-dex2jar", "dex2jar", "decompile"),
    ("procyon", "procyon-decompiler", "decompile"),
    ("baksmali", "smali / baksmali", "dex"),
    ("smali", "smali / baksmali", "dex"),
    ("d8", "Android build-tools", "dex"),
    # --- native
    ("readelf", "binutils", "native"),
    ("objdump", "binutils", "native"),
    ("nm", "binutils", "native"),
    ("ldd", "libc-bin", "native"),
    ("otool", "Xcode command line tools", "native"),
    ("radare2", "radare2", "disasm"),
    ("r2", "radare2", "disasm"),
    ("rizin", "rizin", "disasm"),
    ("ghidra", "ghidra", "disasm"),
    ("analyzeHeadless", "ghidra", "disasm"),
    ("objconv", "objconv", "disasm"),
    # --- debug and instrument
    ("gdb", "gdb", "debug"),
    ("lldb", "lldb", "debug"),
    ("strace", "strace", "debug"),
    ("ltrace", "ltrace", "debug"),
    ("frida", "frida-tools", "hook"),
    ("frida-ps", "frida-tools", "hook"),
    ("objection", "objection", "hook"),
    ("mitmproxy", "mitmproxy", "proxy"),
    ("mitmdump", "mitmproxy", "proxy"),
    ("tshark", "tshark", "proxy"),
    # --- scripts
    ("shellcheck", "shellcheck", "script"),
    ("shfmt", "shfmt", "script"),
    ("bash", "bash", "script"),
    ("python3", "python3", "script"),
    # --- running it safely
    ("qemu-x86_64", "qemu-user", "emulate"),
    ("qemu-arm", "qemu-user", "emulate"),
    ("qemu-aarch64", "qemu-user", "emulate"),
    ("wine", "wine", "emulate"),
    ("docker", "docker", "emulate"),
    ("firejail", "firejail", "emulate"),
    ("bwrap", "bubblewrap", "emulate"),
)

#: Which capabilities matter for which kind. A kind that is absent falls back
#: to the universal set, so an unrecognised artifact still gets a useful probe
#: rather than an empty one.
_UNIVERSAL_CAPS: tuple[str, ...] = ("detect", "unpack", "hex", "native")
KIND_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "apk": ("detect", "archive", "package", "sign", "decompile", "dex",
            "native", "hook", "proxy", "unpack"),
    "dex": ("decompile", "dex", "detect"),
    "jar": ("detect", "archive", "decompile", "debug"),
    "zip": ("detect", "archive", "unpack"),
    "elf": ("detect", "native", "disasm", "debug", "unpack", "hex", "emulate"),
    "pe": ("detect", "native", "disasm", "debug", "unpack", "hex", "emulate"),
    "macho": ("detect", "native", "disasm", "debug", "hex"),
    "script": ("detect", "script", "hex"),
    "dir": _UNIVERSAL_CAPS,
    "unknown": _UNIVERSAL_CAPS,
}


@dataclass(frozen=True)
class ToolProbe:
    """One tool, and whether this host actually has it."""

    name: str
    path: str | None
    hint: str
    capability: str

    @property
    def present(self) -> bool:
        return self.path is not None


def probe_tools() -> list[ToolProbe]:
    """Every tool in the catalogue, resolved against this host.

    ``shutil.which`` rather than an import: the question is whether the
    *command* resolves, and half of these — apktool, jadx, ghidra — are not
    Python packages at all.
    """
    return [
        ToolProbe(name=n, path=shutil.which(n), hint=h, capability=c)
        for n, h, c in _TOOL_CATALOGUE
    ]


def tools_for(kind: str, probes: list[ToolProbe] | None = None) -> list[ToolProbe]:
    """The probes that matter for *kind*."""
    caps = KIND_CAPABILITIES.get(kind, _UNIVERSAL_CAPS)
    return [p for p in (probes or probe_tools()) if p.capability in caps]


def capability_status(
    kind: str, probes: list[ToolProbe] | None = None,
) -> list[tuple[str, list[str], list[tuple[str, str]]]]:
    """(capability, present tools, missing tools) for *kind*.

    Reported per capability rather than per tool because that is the question
    the operator is asking: three decompilers and none of them installed is a
    gap, and one installed radare2 out of four disassemblers is not.
    """
    caps = KIND_CAPABILITIES.get(kind, _UNIVERSAL_CAPS)
    probes = tools_for(kind, probes)
    out = []
    for cap in CAPABILITY_ORDER:
        if cap not in caps:
            continue
        group = [p for p in probes if p.capability == cap]
        have = [p.name for p in group if p.present]
        miss = [(p.name, p.hint) for p in group if not p.present]
        out.append((cap, have, miss))
    return out


def gaps(kind: str, probes: list[ToolProbe] | None = None) -> list[str]:
    """Capabilities for *kind* that no installed tool satisfies.

    This is the list the briefing hands the agent, so a playbook is never
    written around a step the host cannot take.
    """
    return [cap for cap, have, _ in capability_status(kind, probes) if not have]


def render_tools(kind: str, probes: list[ToolProbe] | None = None) -> str:
    """The tool situation as plain text, for the prompt rather than the screen."""
    lines = []
    for cap, have, miss in capability_status(kind, probes):
        label = CAPABILITY_LABELS.get(cap, cap)
        if have:
            lines.append(f"  {label:<38} available: {', '.join(have)}")
        else:
            would = "; ".join(f"{n} ({h})" for n, h in miss[:3])
            lines.append(f"  {label:<38} NOT AVAILABLE — would need: {would}")
    return "\n".join(lines) or "  (no tools relevant to this artifact type)"


# ---------------------------------------------------------------------------
# the briefing
# ---------------------------------------------------------------------------


def missing_tools(
    probes: list[ToolProbe] | None = None,
) -> list[tuple[str, str]]:
    """Every absent tool as (name, install hint), deduplicated and sorted.

    The hints are package names for a Debian/Ubuntu-style package manager where
    one exists, and upstream project names where the tool only ships as a
    release archive (jadx, Ghidra, apktool are all in the second group). Both
    are honest answers to "what do I have to go and get"; pretending they are
    all one `apt install` would not be.
    """
    seen: dict[str, str] = {}
    for p in (probes or probe_tools()):
        if not p.present:
            seen.setdefault(p.hint, p.name)
    return sorted(seen.items())


def member_heading(kind: str) -> str:
    """What the member list actually is for this kind.

    An archive has members, a PE or ELF has sections, a directory has entries.
    Labelling all three "MEMBERS" invites the agent to reason about an ELF as
    though it were a zip, which is the kind of small wrongness that produces a
    confidently wrong plan.
    """
    if kind in _CONTAINER_KINDS:
        return "MEMBERS (largest first)"
    if kind in ("pe", "elf", "macho"):
        return "SECTIONS"
    return "ENTRIES"


def summary_line(art: Artifact) -> str:
    """One line naming the artifact, for the transcript header."""
    return (f"{art.path.name} · {art.label} · {human_size(art.size)}"
            + (f" · sha256 {art.sha256[:12]}…" if art.sha256 else ""))


def build_prompt(art: Artifact, inv: Inventory, probes: list[ToolProbe]) -> str:
    """The turn that follows the map: identify the product, then ask.

    The prompt is explicit about the ordering — map, *ask*, and only then
    touch — because the whole value of the command is that the operator is
    offered a choice that fits the artifact in front of them rather than a
    generic menu. An agent that patches a binary before asking has produced
    exactly the thing the operator did not want.
    """
    facts = "\n".join(f"  {k:<16} {v}" for k, v in inv.facts) or "  (none)"
    members = "\n".join(
        f"  {human_size(s):>10}  {n}" for n, s in inv.members) or "  (none)"
    heading = member_heading(art.kind)
    signals = "\n".join(
        f"  {c:<10} {v}" for c, v in inv.signals) or "  (none)"
    notes = "\n".join(f"  - {n}" for n in inv.notes) or "  (none)"
    offers = "\n".join(f"  {o.slug:<10} {o.title}: {o.blurb}"
                       for o in opportunities(art.kind))
    missing = gaps(art.kind, probes)
    missing_line = (
        "\n".join(f"  - {CAPABILITY_LABELS.get(c, c)}" for c in missing)
        or "  (every capability this kind needs is available)")

    return f"""REVERSE ENGINEERING — INTAKE

You are mapping an artifact the operator supplied. Nothing has been modified:
the map below is the product of a read-only pass (format sniff, header parse,
member table, string scan). No part of the artifact has been executed.

ARTIFACT
  path            {art.path}
  kind            {art.kind} — {art.label}
  size            {human_size(art.size)}
  sha256          {art.sha256 or "(directory)"}
  signature       {art.magic or "(none)"}

FACTS
{facts}

{heading}
{members}

STRINGS WORTH NOTING
{signals}

NOTES
{notes}

TOOLING ON THIS HOST
{render_tools(art.kind, probes)}

CAPABILITIES THIS KIND NEEDS AND THIS HOST LACKS
{missing_line}

STARTING POINTS FOR WHAT COULD BE CHANGED
These are candidates for this *file format*. Most will not apply. Keep the ones
that fit the actual product and drop the rest without comment:
{offers}

WHAT TO DO NOW

1. Work out what this artifact IS as a product — what it does, who it is for,
   what its author cared about. The facts above are the evidence; the format is
   not the answer. "An Android game, a Flappy Bird clone with an ad-supported
   free tier" is the answer, and it decides everything that follows.

2. Read its NATURE as far as the map allows: the members that carry logic, the
   strings that reveal what it talks to, where the numbers that matter are
   kept. Say plainly which parts are readable now and which are opaque without
   a tool this host does not have.

3. THEN ASK THE OPERATOR WHAT THEY WANT. Not a generic menu — a menu for THIS
   artifact. If it is a game, the offer is the game's own knobs (unlimited
   currency, invulnerability, unlocked levels, no ad breaks). If it is an SDK,
   the offer is its observable behaviour (map the endpoints, trace the checks,
   see what it collects). Give each option a short name, say what it would
   involve, and say how confident you are that this host can actually do it
   with the tooling listed above. Number them so the operator can answer
   "1 and 4".

4. STOP. Do not modify, rebuild, repack, sign, write or execute anything yet —
   not even a copy, not even to check. The operator's choice is the next input.
   End the turn after the question, and keep the question under about fifteen
   lines so the answer is easy to give.

RULES FOR THE WORK THAT FOLLOWS THE CHOICE (state them, do not perform them)
  - Never modify the original in place. Write a new file beside it and keep
    the original untouched.
  - Prefer the least destructive method that answers the question.
  - An artifact the operator did not write is somebody's product: this is for
    artifacts they own or are authorised to take apart.
  - When a step needs a capability this host lacks, say which tool is missing
    and what it would take, rather than improvising around it.
"""


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

USAGE_LINES: tuple[tuple[str, str], ...] = (
    ("/reverse-engineer <path>",
     "map an artifact, then choose what to change"),
    ("/reverse-engineer <path> --map",
     "map it only — no model turn, nothing sent anywhere"),
    ("/reverse-engineer-tools [kind]",
     "what is installed, and what is missing, for reverse engineering"),
)
