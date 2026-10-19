"""Shodan search, wired into the agent loop.

Shodan is a public search engine for internet-connected devices. This module
does two things and nothing else:

* **Keeps the key.** ``/shodan-api-setup`` writes it to
  ``~/.zimzilla/shodan/source`` in the same ``source``-format profile every
  other credential in this project uses — mode 600, never echoed back. The
  environment variable ``SHODAN_API_KEY`` is honoured too, so a shell that
  already exports it needs nothing.

* **Runs a named search.** A *facet* is a named Shodan query with a human
  label (``cam``, ``scada``, ``db`` …). ``search()`` calls the read-only
  ``/shodan/host/search`` endpoint and returns the matches as plain facts —
  never banners. The raw ``data`` blob Shodan returns for a host routinely
  contains credentials, session cookies and personal data lifted from a banner;
  none of it is read, kept or shown. Only the index metadata an operator needs
  to triage (IP, port, org, product, country, tags, CVEs) survives into the
  result, so a Shodan search cannot become an accidental secret dump.

The search result is handed to the agent as a single turn (see
``build_prompt``) whose job is to *analyse* it and propose tasks — not to touch
anything. A Shodan result names hosts this project has no authorisation to
reach; the prompt says so, and the ranking is a list of things to ask about,
not a list of things to do.

Nothing here writes a report, a case directory or a banner. The only file this
module touches is the key.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))

#: The search API. A key is required for every call, including the free plan.
API_BASE = "https://api.shodan.io"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "shodan" / "source"

#: The environment variable a shell profile sets, alongside the file.
ENV_VAR = "SHODAN_API_KEY"

#: How many matches a search asks for unless told otherwise, and the ceiling it
#: will not go past. The free plan bills by the query and caps results per
#: query; keeping the default small also keeps the transcript readable and the
#: turn that follows cheap.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: One HTTP request per search. Short on purpose: a search that hangs is worth
#: abandoning, and the operator can simply run it again.
TIMEOUT = 25.0

#: ``client`` ASNs are Shodan's own scanners and cloud crawlers, not a
#: customer's devices. Every facet hides them, because a page of an ISP's
#: honeypots is a page of nothing worth triaging.
_CLOUD_NOISE = "-org:Shodan -tag:cloud"


# ---------------------------------------------------------------------------
# key and profile — same shape as every other credential in the project
# ---------------------------------------------------------------------------

#: The key ``/shodan-api-setup`` wrote *this session*, if it wrote one. In
#: memory on purpose, for the same reason TokenHarbour keeps one: the operator
#: just overrode the environment explicitly, and a snapshot taken at launch
#: must not win over that for the rest of the process.
_SESSION_KEY: str | None = None


def remember_session_key(key: str) -> None:
    """Make *key* win for the rest of this process."""
    global _SESSION_KEY
    _SESSION_KEY = key.strip()


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
        name, _, value = body.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def load_credentials() -> tuple[str, str] | None:
    """The Shodan key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. A key set **in this session** with ``/shodan-api-setup``.
      2. ZimZilla's own profile, written by that same command.
      3. ``SHODAN_API_KEY`` in the environment.

    Returns ``(key, origin)`` or ``None``. ``origin`` is a path or the word
    "environment" — never the key itself.
    """
    if _SESSION_KEY:
        return _SESSION_KEY, f"{PROFILE} (set this session)"

    token = _read_exports(PROFILE).get(ENV_VAR)
    if token:
        return token, str(PROFILE)

    token = os.environ.get(ENV_VAR, "").strip()
    if token:
        return token, "environment"
    return None


def has_key() -> bool:
    return load_credentials() is not None


def save_key(key: str) -> Path:
    """Write *key* to ZimZilla's own Shodan profile, mode 600.

    The file is the same ``source`` format as every other profile in the
    project, so it can be sourced by a shell and read by ``load_credentials``
    — one credential format, not two.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = f'export {ENV_VAR}="{key.strip()}"\n'
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


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    A Shodan key is 32 hex characters, but the check is deliberately loose —
    Shodan decides what is valid, not us. It is strict enough to catch a pasted
    shell line (``export SHODAN_API_KEY=…``), a quoted literal, or a bare word.
    """
    value = value.strip().strip("'\"")
    if len(value) < 16 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._\-]+", value))


# ---------------------------------------------------------------------------
# facets — the named searches /help and the popup list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facet:
    """One named Shodan search.

    ``query`` is the base Shodan query. ``{country}`` is replaced with a
    ``country:XX`` clause when the operator passes one, and dropped otherwise —
    a search with no country is a global search, which is right for a first
    look.
    """

    name: str
    command: str
    label: str
    blurb: str
    query: str
    country: bool = True
    target_label: str = "[country]"

    def build_query(self, country: str = "", extra: str = "") -> str:
        """The concrete query string for a run of this facet."""
        parts = [self.query]
        if country:
            parts.append(f"country:{country.upper()}")
        extra = extra.strip()
        if extra:
            parts.append(extra)
        parts.append(_CLOUD_NOISE)
        return " ".join(p for p in parts if p).strip()


#: The order they are shown in, and the order the help lists them. The three
#: highest-signal device classes first — a camera with a screenshot, a PLC, and
#: an open database are each a concrete, well-understood finding — then the
#: remote-access and network-edge surfaces, then the two query-taking commands.
FACETS: dict[str, Facet] = {
    f.name: f
    for f in (
        Facet(
            name="cam",
            command="shodan-cam-search",
            label="cameras",
            blurb="exposed IP cameras that show a live screenshot",
            query="tag:webcam has_screenshot:true",
        ),
        Facet(
            name="scada",
            command="shodan-scada-search",
            label="ICS / SCADA",
            blurb="industrial control systems — PLCs, RTUs, HMIs",
            query="tag:ics",
        ),
        Facet(
            name="db",
            command="shodan-db-search",
            label="databases",
            blurb="exposed MongoDB, Elasticsearch, Redis, Postgres, MySQL",
            query="(product:MongoDB OR product:Elasticsearch OR product:Redis"
                  " OR product:PostgreSQL OR product:MySQL)",
        ),
        Facet(
            name="rdp",
            command="shodan-rdp-search",
            label="remote desktop",
            blurb="exposed RDP and VNC services",
            query="(port:3389 OR port:5900)",
        ),
        Facet(
            name="router",
            command="shodan-router-search",
            label="routers and gateways",
            blurb="network edge devices — routers, firewalls, VPNs",
            query="device:router",
        ),
    )
}

#: The two commands that take a free-form query instead of a fixed facet. Kept
#: out of ``FACETS`` because they are not device classes: one is a general
#: vuln filter, the other is the escape hatch that makes every other command
#: optional.
VULN_FACET = Facet(
    name="vuln",
    command="shodan-vuln-search",
    label="hosts with known CVEs",
    blurb="hosts Shodan has flagged with a known CVE",
    query="has_vuln:true",
    country=True,
    target_label="<query…>",
)

RAW_FACET = Facet(
    name="raw",
    command="shodan-search",
    label="raw query",
    blurb="run any Shodan query verbatim",
    query="",
    country=False,
    target_label="<query…>",
)

FACET_ORDER: tuple[str, ...] = tuple(FACETS) + (VULN_FACET.name, RAW_FACET.name)


def facet_for(command: str) -> Facet | None:
    """The facet a slash command maps to, or ``None``."""
    for f in (*FACETS.values(), VULN_FACET, RAW_FACET):
        if f.command == command or f.name == command:
            return f
    return None


# ---------------------------------------------------------------------------
# the search itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Host:
    """One Shodan match, reduced to the fields worth triaging.

    Deliberately not a copy of Shodan's host object: the raw ``data`` banner
    (which can carry credentials) and everything else not listed here is
    dropped at parse time.
    """

    ip: str
    port: int
    org: str = ""
    product: str = ""
    version: str = ""
    os: str = ""
    country: str = ""
    city: str = ""
    hostnames: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    vulns: tuple[str, ...] = ()

    @property
    def endpoint(self) -> str:
        return f"{self.ip}:{self.port}"

    @property
    def identity(self) -> str:
        """What the box calls itself: product and version, else the OS."""
        bits = [b for b in (self.product, self.version) if b]
        if bits:
            return " ".join(bits)
        return self.os


@dataclass
class SearchResult:
    """What a search returned: the query, the total, and the matches kept."""

    facet: str
    query: str
    total: int
    hosts: list[Host] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


#: Errors the API returns in words rather than codes, mapped to advice. The
#: free plan is the common case: searches work, but a query that needs a paid
#: filter comes back 403 with a body that does not say which filter.
_API_ERRORS = {
    401: "key rejected — check it with /shodan-api-setup",
    402: "payment required — this query needs a paid Shodan plan",
    403: "forbidden — this query or filter is not available on your plan",
    429: "rate limited — Shodan is throttling; wait a moment and retry",
}


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """Turn an HTTP error into one line of advice, without leaking the key.

    Shodan puts a human message in the body for some errors; the URL contains
    the key, so the URL is never part of the message.
    """
    known = _API_ERRORS.get(exc.code)
    if known:
        return f"{known} (HTTP {exc.code})"
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        detail = str(body.get("error") or body.get("message") or "")[:160]
    except Exception:  # noqa: BLE001 - a body we cannot parse is not a failure
        detail = ""
    suffix = f" — {detail}" if detail else ""
    return f"Shodan returned HTTP {exc.code}{suffix}"


def _parse_host(match: dict) -> Host | None:
    """One match from the API, or ``None`` if it carries no usable address.

    Every field is read defensively: Shodan's schema varies by plan and by
    host, and a missing key must not abort a page of otherwise-good results.
    """
    ip = str(match.get("ip_str") or "").strip()
    if not ip:
        return None
    try:
        port = int(match.get("port"))
    except (TypeError, ValueError):
        port = 0
    location = match.get("location") or {}
    vulns = match.get("vulns") or {}
    return Host(
        ip=ip,
        port=port,
        org=str(match.get("org") or "").strip(),
        product=str(match.get("product") or "").strip(),
        version=str(match.get("version") or "").strip(),
        os=str(match.get("os") or "").strip(),
        country=str(location.get("country_name") or "").strip(),
        city=str(location.get("city") or "").strip(),
        hostnames=tuple(str(h) for h in (match.get("hostnames") or [])[:4]),
        tags=tuple(str(t) for t in (match.get("tags") or [])[:8]),
        vulns=tuple(sorted(str(v) for v in vulns))[:12],
    )


def search(
    key: str,
    query: str,
    limit: int = DEFAULT_LIMIT,
    timeout: float = TIMEOUT,
) -> tuple[SearchResult | None, str]:
    """Run *query* against Shodan. Returns ``(result, "")`` or ``(None, err)``.

    Read-only: one GET to ``/shodan/host/search``. ``minify`` is not used —
    the response is trimmed here instead, so the fields kept are a decision in
    this file and not a decision in the API.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    params = urllib.parse.urlencode({"key": key, "query": query})
    url = f"{API_BASE}/shodan/host/search?{params}"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return None, _http_error_message(exc)
    except urllib.error.URLError as exc:
        return None, f"network error: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"network error: {exc}"
    except json.JSONDecodeError:
        return None, "Shodan returned a response that was not JSON"

    matches = payload.get("matches")
    if not isinstance(matches, list):
        # The API answers 200 with an ``error`` field for some bad queries.
        msg = str(payload.get("error") or "unexpected response shape")
        return None, f"Shodan: {msg}"

    hosts = [h for h in (_parse_host(m) for m in matches) if h is not None][:limit]
    total = payload.get("total")
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(hosts)
    return SearchResult(facet="", query=query, total=total, hosts=hosts), ""


# ---------------------------------------------------------------------------
# the turn that follows the search
# ---------------------------------------------------------------------------


def _host_lines(result: SearchResult) -> str:
    """The results as compact evidence lines, for the prompt.

    One line per host: ``ip:port org — identity [country] tags vulns``. This is
    the same shape the transcript block shows, so the model reasons over what
    the operator saw.
    """
    lines: list[str] = []
    for h in result.hosts:
        bits = [h.endpoint]
        if h.org:
            bits.append(h.org)
        ident = h.identity
        if ident:
            bits.append(f"[{ident}]")
        place = ", ".join(p for p in (h.city, h.country) if p)
        if place:
            bits.append(f"({place})")
        if h.tags:
            bits.append("tags:" + ",".join(h.tags))
        if h.vulns:
            bits.append("vulns:" + ",".join(h.vulns))
        lines.append("- " + " ".join(bits))
    return "\n".join(lines) if lines else "- (no matches returned)"


def build_prompt(facet: Facet, result: SearchResult, country: str = "") -> str:
    """The briefing handed to the agent after a search.

    The job is analysis and *asking*, not doing. A Shodan result names hosts
    nobody has authorised this session to touch, so the turn is asked to rank
    what it sees and propose tasks — the operator picks one and the normal
    scope rules apply from there, exactly as they do for any other target.
    """
    scope = f"country:{country.upper()}" if country else "global"
    return (
        f"Shodan search — {facet.label} ({scope}).\n"
        f"Query: {result.query}\n"
        f"Total matches Shodan holds for this query: {result.total}. "
        f"{len(result.hosts)} shown below.\n\n"
        "RESULTS\n"
        f"{_host_lines(result)}\n\n"
        "TASK\n"
        "You are analysing a Shodan result set, not attacking it. Nothing in "
        "this list is in scope and nothing here has been authorised — these "
        "are internet-wide index rows, not targets you may reach. Do NOT run "
        "any tool against these addresses. This is a read-only analysis turn.\n\n"
        "Read the results and report back in this shape:\n"
        "1. What the result set is — which product/version/port dominates, "
        "what that says about the population.\n"
        "2. The interesting rows and why: group the hosts into a handful of "
        "classes (e.g. by product, version, or exposed service) and name the "
        "few that stand out and the reason each one does.\n"
        "3. Concrete, testable hypotheses worth asking about — one line each, "
        "phrased as a question the operator can turn into a task, e.g. "
        "\"are these Bosch cameras still on the factory admin password?\" or "
        "\"does this Elasticsearch build predate CVE-xxxx-xxxx?\".\n"
        "4. A short numbered list of tasks you would propose, ranked by likely "
        "value, each naming the host class it applies to — and, for each, what "
        "authorisation would be needed before it is anything but a question.\n\n"
        "End by asking which task, if any, the operator wants to pursue. Do "
        "not start one yourself."
    )


def summary_lines(result: SearchResult) -> list[str]:
    """The result rows as plain strings, for the UI to style.

    Rendering lives in the UI; this only decides *what* is shown, so the
    transcript block and any future export cannot disagree about a host.
    """
    lines: list[str] = []
    for h in result.hosts:
        bits = [h.endpoint]
        if h.org:
            bits.append(h.org)
        ident = h.identity
        if ident:
            bits.append(ident)
        place = ", ".join(p for p in (h.city, h.country) if p)
        if place:
            bits.append(place)
        if h.vulns:
            bits.append("⚠ " + ",".join(h.vulns[:4]))
        lines.append("  ".join(bits))
    return lines
