"""Federation: are SERVICE targets reachable *from the endpoint* (where the query
actually runs), and how does that compare to reachability from this machine?"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from ..model import CheckResult, Context, Status
from ..transport import SPARQL_JSON, Response, Target, sparql_request
from .common import short

# (regex over error body, classification, hint)
ERROR_PATTERNS = [
    (r"UnknownHost|Name or service not known|nodename nor servname|getaddrinfo|Could not resolve|NXDOMAIN",
     "DNS", "The endpoint's host cannot resolve the SERVICE hostname - check DNS / egress proxy on the server side."),
    (r"Connection refused", "refused", "Target port closed from the endpoint's network."),
    (r"connect timed out|ConnectTimeout|No route to host|Network is unreachable|SocketTimeout|timed out|Timeout",
     "timeout", "Egress from the endpoint's network is probably blocked by a firewall, or requires a proxy."),
    (r"Connection reset|reset by peer", "reset", "Connection reset between endpoint and target - see keep-alive/idle advice."),
    (r"PKIX|SSLHandshake|certificate|unable to find valid certification path", "TLS",
     "The endpoint's JVM/OS trust store does not trust the target's certificate."),
    (r"\b403\b|Forbidden|User-Agent|robot policy", "HTTP 403",
     "Target rejects the endpoint's requests - often a User-Agent policy (Wikidata) or IP block."),
    (r"\b429\b|Too Many Requests", "HTTP 429", "Target rate-limits the endpoint."),
    (r"SERVICE.*(not (allowed|supported|enabled))|federat.*(disabled|not allowed)|Service calls are not allowed",
     "disabled", "Federation is disabled or restricted on this endpoint."),
    (r"Proxy|407", "proxy", "Egress goes through a proxy that refused or needs authentication."),
]


def classify_error(text: str) -> Tuple[str, Optional[str]]:
    for rx, label, hint in ERROR_PATTERNS:
        if re.search(rx, text, re.I):
            return label, hint
    return "error", None


def _bindings(resp: Response) -> Optional[int]:
    try:
        return len(json.loads(resp.body.decode("utf-8"))["results"]["bindings"])
    except Exception:  # noqa: BLE001
        return None


def run(ctx: Context) -> CheckResult:
    r = CheckResult("federation", "Federation (SERVICE) reachability")
    if not ctx.services:
        return r.skip("No federation targets given (use --service URL, repeatable).")
    t = ctx.target
    tbl = r.table("SERVICE target", "From this machine", "From the endpoint", "Diagnosis")
    for svc in ctx.services:
        q = f"SELECT * WHERE {{ SERVICE <{svc}> {{ SELECT * WHERE {{ ?s ?p ?o }} LIMIT 1 }} }}"
        remote = sparql_request(t, q, "POST", accept=SPARQL_JSON, timeout=ctx.service_timeout)
        if remote.status in (405, 415):
            remote = sparql_request(t, q, "GET", accept=SPARQL_JSON, timeout=ctx.service_timeout)
        try:
            local = sparql_request(Target(svc, user_agent=t.user_agent, timeout=min(t.timeout, 30),
                                          insecure=t.insecure), "ASK {}", "GET", accept=SPARQL_JSON)
        except ValueError as e:
            r.add(Status.FAIL, f"Invalid SERVICE URL {svc!r}: {e}")
            continue

        n = _bindings(remote)
        local_txt = f"{local.describe()} ({local.ms} ms)"
        if remote.ok and n:
            remote_txt = f"OK, {n} row ({remote.ms} ms)"
            r.add(Status.PASS, f"{svc} is reachable from the endpoint ({remote.ms} ms).")
            diag = "reachable"
        elif remote.ok:
            remote_txt = f"OK but 0 rows ({remote.ms} ms)"
            r.add(Status.WARN, f"{svc}: SERVICE call returned no rows.",
                  "The target may be empty, or the endpoint swallowed the error (SILENT-like behaviour). "
                  "Check the endpoint's logs.")
            diag = "empty result"
        else:
            body = remote.text(600) if remote.status else (remote.error or "")
            label, hint = classify_error(body) if remote.status else (remote.error_kind or "error", None)
            remote_txt = f"{remote.describe()} ({remote.ms} ms)"
            if remote.error_kind == "timeout":
                label, hint = "timeout", (f"No answer within {ctx.service_timeout:.0f} s. The endpoint may be waiting "
                                          f"on a blocked connection to the target.")
            if local.ok:
                diag = f"{label}: reachable from here, NOT from the endpoint"
                hint = (hint or "") + (" The target works from this machine, so the problem is on the endpoint's side "
                                       "(its network egress, proxy, trust store or outgoing User-Agent).")
            else:
                diag = f"{label}: unreachable from both"
                hint = (hint or "") + f" The target also fails from this machine ({local.describe()})."
            r.add(Status.FAIL, f"{svc}: SERVICE call failed ({label}). Error: {short(body, 220)!r}", hint.strip())
        tbl.rows.append([svc, local_txt, remote_txt, diag])
    return r.finalize()
