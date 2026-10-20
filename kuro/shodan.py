"""Shodan, wired into the agent loop â€” on whatever plan the key actually has.

Shodan's API is tiered, and the tier is not visible from the error message:

* ``/shodan/host/search`` â€” returns host records. **Membership and up.** On the
  free ``oss`` plan every query answers ``403 Requires membership or higher``,
  including a bare ``apache``. No filter tuning changes that; it is the endpoint
  that is gated, not the query.
* ``/shodan/host/count`` â€” returns the *size* of a result set and its
  **facets** (country, product, port, org, os, version). Open to every plan,
  including ``oss``. This is the free analytic surface, and it is most of what
  the feature needs: "how many, where, what product, on what ports" is the
  question a first look at a device class is asking.
* ``/shodan/host/<ip>`` â€” a single host record. Open for popular addresses,
  gated for the rest.

So this module does not assume it can search. ``describe()`` runs a count and
asks for facets â€” always available â€” and only reaches for host records when the
plan permits, degrading to the facet answer when it does not. A run never fails
because of the plan; it answers with less detail and says which detail it could
not get.

Two Shodan syntax facts are load-bearing here, both verified against the live
API: ``OR`` is **invalid** (``port:3389 OR port:5900`` is a 400), and multiple
values are comma-joined instead (``port:3389,5900``). And ``tag:webcam`` no
longer exists in Shodan's index â€” it returns 0 â€” so the camera facet keys off
product and screenshot instead of a tag that is gone.

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

KURO_HOME = Path(os.environ.get("KURO_HOME") or (Path.home() / ".kuro"))

#: The API root. A key is required for every call, including the free plan.
API_BASE = "https://api.shodan.io"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = KURO_HOME / "shodan" / "source"

#: The environment variable a shell profile sets, alongside the file.
ENV_VAR = "SHODAN_API_KEY"

#: How many host records a search asks for when the plan allows one, and the
#: ceiling it will not go past. The free plan cannot search at all, so this only
#: applies to a membership key.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: One HTTP request per call. Short on purpose: a call that hangs is worth
#: abandoning, and the operator can simply run it again.
TIMEOUT = 25.0

#: Facets asked for on a count. These six are the ones that turn a number into
#: a picture of a population, and all six are served on the free plan.
FACET_FIELDS = ("country", "product", "port", "org", "os", "version")

#: How many values per facet. Five is enough to see the shape and short enough
#: to read in a terminal.
FACET_SIZE = 5

#: ``client`` ASNs are Shodan's own scanners and cloud crawlers, not a
#: customer's devices. Every facet hides them, because a page of an ISP's
#: honeypots is a page of nothing worth triaging.
_CLOUD_NOISE = "-org:Shodan -tag:cloud"


# ---------------------------------------------------------------------------
# key and profile â€” same shape as every other credential in the project
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
      2. Kuro's own profile, written by that same command.
      3. ``SHODAN_API_KEY`` in the environment.

    Returns ``(key, origin)`` or ``None``. ``origin`` is a path or the word
    "environment" â€” never the key itself.
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
    """Write *key* to Kuro's own Shodan profile, mode 600.

    The file is the same ``source`` format as every other profile in the
    project, so it can be sourced by a shell and read by ``load_credentials``
    â€” one credential format, not two.
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

    A Shodan key is 32 hex characters, but the check is deliberately loose â€”
    Shodan decides what is valid, not us. It is strict enough to catch a pasted
    shell line (``export SHODAN_API_KEY=â€¦``), a quoted literal, or a bare word.
    """
    value = value.strip().strip("'\"")
    if len(value) < 16 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._\-]+", value))


# ---------------------------------------------------------------------------
# plan â€” what this key is allowed to do
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What the key is entitled to, as Shodan reports it.

    ``search`` is the field that decides the shape of a run: without it there
    are no host records, only counts and facets. It is derived from the plan
    name and the query credit balance rather than guessed from a 403, so the UI
    can say "your plan is X" before a search is attempted.
    """

    name: str = "unknown"
    query_credits: int = 0
    scan_credits: int = 0
    unlocked: bool = False

    @property
    def search(self) -> bool:
        """Whether ``/shodan/host/search`` will answer.

        The **balance** is the truth, not the plan label: a membership key with
        every query credit spent answers 403 exactly like a free one, and a
        label-based test would send it into a guaranteed failure. ``oss`` is
        refused by name as well, because Shodan reports it with a zero balance
        but the reason is the tier, not the balance.
        """
        if self.name in ("oss", "free"):
            return False
        return self.query_credits > 0

    @property
    def blurb(self) -> str:
        if self.search:
            return f"{self.name} Â· host records available"
        return (f"{self.name} Â· counts and facets only â€” "
                "host records need membership")


def fetch_plan(key: str, timeout: float = TIMEOUT) -> tuple[Plan | None, str]:
    """Ask Shodan what this key is entitled to. ``/api-info`` is open to all."""
    payload, err = _get("/api-info", {"key": key}, timeout)
    if payload is None:
        return None, err
    limits = payload.get("usage_limits") or {}
    try:
        credits = int(limits.get("query_credits", payload.get("query_credits", 0)))
    except (TypeError, ValueError):
        credits = 0
    return Plan(
        name=str(payload.get("plan") or "unknown"),
        query_credits=credits,
        scan_credits=int(payload.get("scan_credits") or 0),
        unlocked=bool(payload.get("unlocked")),
    ), ""


# ---------------------------------------------------------------------------
# facets â€” the named searches /help and the popup list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facet:
    """One named Shodan query.

    ``query`` is the base query. ``{country}`` is replaced with a
    ``country:XX`` clause when the operator passes one, and dropped otherwise â€”
    a search with no country is a global look, which is right for a first pass.

    ``query`` uses **comma**-joined values for an OR, never ``OR``: Shodan
    rejects ``OR`` as invalid syntax. See the module docstring.
    """

    name: str
    command: str
    label: str
    blurb: str
    query: str
    country: bool = True
    target_label: str = "[country]"
    #: What the facet is really asking, in one sentence â€” handed to the model so
    #: its hypotheses start from the right question rather than from the label.
    question: str = ""

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


#: The named searches, in display order. The device classes that are both
#: well-populated and well-understood come first: a camera population, a PLC
#: population and an open-database population each produce a concrete finding,
#: whereas "all routers" produces a number with no edge to it.
FACETS: dict[str, Facet] = {
    f.name: f
    for f in (
        Facet(
            name="cam",
            command="shodan-cam-search",
            label="IP cameras",
            blurb="cameras and DVRs that expose a web or RTSP interface",
            # `tag:webcam` is gone from Shodan's index (verified: 0 results),
            # so the camera class is identified by the products that make up
            # most of it. `has_screenshot:true` narrows to boxes that answer a
            # picture request, which is the population worth looking at.
            query="has_screenshot:true "
                  "product:Hikvision,Dahua,GoAhead-Webs,Netsurveillance",
            question="which camera and DVR families are exposed, and how many "
                     "of them answer an unauthenticated screenshot request?",
        ),
        Facet(
            name="scada",
            command="shodan-scada-search",
            label="ICS / SCADA",
            blurb="industrial control systems â€” PLCs, RTUs, HMIs",
            query="tag:ics",
            question="which industrial protocols and vendors dominate the "
                     "exposed population, and what does that say about the "
                     "sites running them?",
        ),
        Facet(
            name="db",
            command="shodan-db-search",
            label="databases",
            blurb="exposed MongoDB, Redis, Elasticsearch, Postgres, MySQL",
            query="product:MongoDB,Redis,PostgreSQL,MySQL,Elastic",
            question="which database engines are reachable from the internet, "
                     "and in what proportions?",
        ),
        Facet(
            name="rdp",
            command="shodan-rdp-search",
            label="remote desktop",
            blurb="exposed RDP and VNC services",
            query="port:3389,5900",
            question="how much remote-desktop surface is exposed, and where is "
                     "it concentrated?",
        ),
        Facet(
            name="router",
            command="shodan-router-search",
            label="routers and gateways",
            blurb="network edge devices â€” routers, firewalls, VPNs",
            query="device:router",
            question="which router families are exposed, and on which "
                     "management ports?",
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
    target_label="<queryâ€¦>",
    question="which flagged services dominate, and which CVE families do they "
             "point at?",
)

RAW_FACET = Facet(
    name="raw",
    command="shodan-search",
    label="raw query",
    blurb="run any Shodan query verbatim",
    query="",
    country=False,
    target_label="<queryâ€¦>",
    question="what does this query describe?",
)

FACET_ORDER: tuple[str, ...] = tuple(FACETS) + (VULN_FACET.name, RAW_FACET.name)


def facet_for(command: str) -> Facet | None:
    """The facet a slash command maps to, or ``None``."""
    for f in (*FACETS.values(), VULN_FACET, RAW_FACET):
        if f.command == command or f.name == command:
            return f
    return None


# ---------------------------------------------------------------------------
# the calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Host:
    """One Shodan host record, reduced to the fields worth triaging.

    Deliberately not a copy of Shodan's host object: the raw ``data`` banner
    (which can carry credentials) and everything else not listed here is
    dropped at parse time.
    """

    ip: str
    port: int = 0
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
        return f"{self.ip}:{self.port}" if self.port else self.ip

    @property
    def identity(self) -> str:
        """What the box calls itself: product and version, else the OS."""
        bits = [b for b in (self.product, self.version) if b]
        if bits:
            return " ".join(bits)
        return self.os


@dataclass
class FacetValue:
    """One bucket of a facet: a value and how many hosts carry it."""

    value: str
    count: int

    @property
    def share(self) -> float:
        return 0.0


@dataclass
class Description:
    """The answer to a facet query: a total, its facets, and any host sample.

    ``hosts`` is empty on a plan that cannot search, and that is not a failure:
    ``plan.search`` says why, and the analysis turn is told to work from the
    facet shape instead of inventing rows it was never given.
    """

    facet: str
    query: str
    total: int
    facets: dict[str, list[FacetValue]] = field(default_factory=dict)
    hosts: list[Host] = field(default_factory=list)
    plan: Plan = field(default_factory=Plan)
    search_error: str = ""
    timestamp: float = field(default_factory=time.time)

    def top(self, name: str, n: int = FACET_SIZE) -> list[FacetValue]:
        return self.facets.get(name, [])[:n]


#: Errors the API returns in words rather than codes, mapped to advice.
_API_ERRORS = {
    401: "key rejected â€” check it with /shodan-api-setup",
    402: "payment required â€” this query needs a paid Shodan plan",
    403: "forbidden â€” this endpoint needs a higher plan",
    429: "rate limited â€” Shodan is throttling; wait a moment and retry",
}


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """Turn an HTTP error into one line of advice, without leaking the key.

    Shodan puts a human message in the body for some errors; the URL contains
    the key, so the URL is never part of the message.
    """
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        detail = str(body.get("error") or body.get("message") or "")[:160]
    except Exception:  # noqa: BLE001 - a body we cannot parse is not a failure
        detail = ""
    # The API's own words beat ours when it has any: "Requires membership or
    # higher to access" says more than "forbidden".
    if detail:
        return f"HTTP {exc.code} â€” {detail}"
    known = _API_ERRORS.get(exc.code)
    if known:
        return f"{known} (HTTP {exc.code})"
    return f"Shodan returned HTTP {exc.code}"


def _get(path: str, params: dict, timeout: float) -> tuple[dict | None, str]:
    """One GET against the API. Returns ``(payload, "")`` or ``(None, err)``.

    The key travels in the query string, as the API requires. It is never put
    in an error message â€” see ``_http_error_message``.
    """
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), ""
    except urllib.error.HTTPError as exc:
        return None, _http_error_message(exc)
    except urllib.error.URLError as exc:
        return None, f"network error: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"network error: {exc}"
    except json.JSONDecodeError:
        return None, "Shodan returned a response that was not JSON"


def _parse_facets(raw: dict | None, total: int) -> dict[str, list[FacetValue]]:
    """Shodan's facet payload, normalised and ordered biggest-first.

    Values arrive as ``[{"value": ..., "count": ...}, ...]``; ports arrive as
    integers. Both become ``FacetValue``, sorted descending so the head of the
    list is the answer and the tail is context.
    """
    out: dict[str, list[FacetValue]] = {}
    for name, buckets in (raw or {}).items():
        if not isinstance(buckets, list):
            continue
        values: list[FacetValue] = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            value = str(bucket.get("value", "")).strip()
            try:
                count = int(bucket.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            if value:
                values.append(FacetValue(value=value, count=count))
        values.sort(key=lambda v: v.count, reverse=True)
        out[str(name)] = values
    return out


def _parse_host(match: dict) -> Host | None:
    """One host record, or ``None`` if it carries no usable address.

    Every field is read defensively: Shodan's schema varies by plan and by
    host, and a missing key must not abort a page of otherwise-good results.
    """
    ip = str(match.get("ip_str") or match.get("ip") or "").strip()
    if not ip:
        return None
    try:
        port = int(match.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    location = match.get("location") or {}
    vulns = match.get("vulns") or {}
    if not isinstance(vulns, dict):
        vulns = {}
    return Host(
        ip=ip,
        port=port,
        org=str(match.get("org") or "").strip(),
        product=str(match.get("product") or "").strip(),
        version=str(match.get("version") or "").strip(),
        os=str(match.get("os") or "").strip(),
        country=str(location.get("country_name")
                    or match.get("country_name") or "").strip(),
        city=str(location.get("city") or match.get("city") or "").strip(),
        hostnames=tuple(str(h) for h in (match.get("hostnames") or [])[:4]),
        tags=tuple(str(t) for t in (match.get("tags") or [])[:8]),
        vulns=tuple(sorted(str(v) for v in vulns))[:12],
    )


def describe(
    key: str,
    facet: Facet,
    query: str,
    plan: Plan | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout: float = TIMEOUT,
) -> tuple[Description | None, str]:
    """Describe a query: its total, its facets, and host records if allowed.

    The count is the load-bearing call â€” it is available on every plan and it
    is what makes the run worth anything on a free key. The host search is
    attempted only when ``plan.search`` says it will answer, and its failure is
    recorded rather than propagated: a facet answer is a complete answer at a
    lower resolution, not a broken one.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    plan = plan or Plan()

    payload, err = _get(
        "/shodan/host/count",
        {"key": key, "query": query,
         "facets": ",".join(f"{f}:{FACET_SIZE}" for f in FACET_FIELDS)},
        timeout,
    )
    if payload is None:
        return None, err

    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    # A 200 carrying an `error` is an error, not an empty result set.
    if payload.get("error"):
        return None, f"Shodan: {payload['error']}"

    result = Description(
        facet=facet.name,
        query=query,
        total=total,
        facets=_parse_facets(payload.get("facets"), total),
        plan=plan,
    )

    if plan.search:
        found, search_err = _search_hosts(key, query, limit, timeout)
        if found is None:
            # Keep the facet answer and record why the rows are missing, so the
            # UI and the prompt can both be honest about it.
            result.search_error = search_err
        else:
            result.hosts = found
    return result, ""


def _search_hosts(
    key: str, query: str, limit: int, timeout: float
) -> tuple[list[Host] | None, str]:
    """Host records for *query*, or ``None`` and why not.

    Read-only: one GET to ``/shodan/host/search``. ``minify`` is not used â€” the
    response is trimmed here instead, so the fields kept are a decision in this
    file and not a decision in the API.
    """
    payload, err = _get(
        "/shodan/host/search",
        {"key": key, "query": query, "limit": limit},
        timeout,
    )
    if payload is None:
        return None, err
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return None, str(payload.get("error") or "unexpected response shape")
    return [h for h in (_parse_host(m) for m in matches) if h is not None][:limit], ""


# ---------------------------------------------------------------------------
# the turn that follows the call
# ---------------------------------------------------------------------------


def _facet_lines(result: Description) -> str:
    """The facets as evidence lines, for the prompt and the transcript."""
    lines: list[str] = []
    for name in FACET_FIELDS:
        values = result.top(name)
        if not values:
            continue
        rendered = ", ".join(f"{v.value} ({v.count:,})" for v in values)
        lines.append(f"- {name}: {rendered}")
    return "\n".join(lines) if lines else "- (no facets returned)"


def _host_lines(result: Description) -> str:
    """The host records as compact evidence lines, for the prompt.

    One line per host: ``ip:port org â€” identity [country] tags vulns``. This is
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
    return "\n".join(lines) if lines else "- (no host records on this plan)"


def build_prompt(facet: Facet, result: Description, country: str = "") -> str:
    """The briefing handed to the agent after a Shodan call.

    The job is analysis and *asking*, not doing. A Shodan result names hosts
    nobody has authorised this session to touch, so the turn is asked to rank
    what it sees and propose tasks â€” the operator picks one and the normal
    scope rules apply from there, exactly as they do for any other target.

    The prompt is told which resolution it has. On a free key there are no
    host rows, and a model asked to "list the interesting hosts" with none
    will invent them; told there are none, it works from the facet shape and
    says what it would need to go further.
    """
    scope = f"country:{country.upper()}" if country else "global"
    if result.hosts:
        evidence = (
            f"HOST RECORDS ({len(result.hosts)} shown of {result.total:,})\n"
            f"{_host_lines(result)}\n\n"
        )
        resolution = ("You have host records as well as the facet shape. Cite "
                      "specific addresses when you name one.")
    else:
        evidence = ""
        why = result.search_error or "this plan does not include host records"
        resolution = (
            f"You do NOT have host records â€” {why}. You have the total and "
            "the facet breakdown only. Do not invent addresses, hostnames or "
            "CVE IDs: work from the facet shape, and where a task would need "
            "per-host detail, say that it needs a membership key or a "
            "different query to answer.\n"
        )

    return (
        f"Shodan â€” {facet.label} ({scope}).\n"
        f"Query: {result.query}\n"
        f"Total matches Shodan holds for this query: {result.total:,}.\n"
        f"Plan: {result.plan.name} â€” {result.plan.blurb}.\n\n"
        f"FACET BREAKDOWN\n{_facet_lines(result)}\n\n"
        f"{evidence}"
        f"THE QUESTION THIS SEARCH IS ASKING\n{facet.question or 'what is this population?'}\n\n"
        f"RESOLUTION\n{resolution}\n\n"
        "TASK\n"
        "You are analysing a Shodan result, not attacking it. Nothing here is "
        "in scope and nothing here has been authorised â€” these are "
        "internet-wide index rows, not targets you may reach. Do NOT run any "
        "tool against these addresses. This is a read-only analysis turn.\n\n"
        "Report back in this shape:\n"
        "1. What the population is: which products, ports and countries "
        "dominate, and what that composition says about who runs these boxes "
        "(vendors, hosting providers, regions).\n"
        "2. What is notable in the shape: a concentration in one product or "
        "one ASN, an odd port mix, a version cluster that dates the fleet, a "
        "country that stands out.\n"
        "3. Concrete, testable hypotheses â€” one line each, phrased as a "
        "question the operator can turn into a task, e.g. \"are these "
        "Hikvision boxes still on the factory password?\" or \"does this "
        "Postgres cluster predate the auth bypass fix?\".\n"
        "4. A short numbered list of tasks you would propose, ranked by likely "
        "value, each naming the population it applies to â€” and, for each, what "
        "authorisation would be needed before it is anything but a question.\n\n"
        "End by asking which task, if any, the operator wants to pursue. Do "
        "not start one yourself."
    )


def summary_lines(result: Description) -> list[str]:
    """The result rows as plain strings, for the UI to style.

    Rendering lives in the UI; this only decides *what* is shown, so the
    transcript block and any future export cannot disagree.
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
            bits.append("âš  " + ",".join(h.vulns[:4]))
        lines.append("  ".join(bits))
    return lines

