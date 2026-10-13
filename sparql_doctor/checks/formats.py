"""Content negotiation for SELECT, ASK and CONSTRUCT results."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from ..model import CheckResult, Context, Status
from ..transport import Response, sparql_request

SELECT = "SELECT ?x WHERE { BIND(1 AS ?x) }"
CONSTRUCT = "CONSTRUCT { <urn:sparql-doctor:s> <urn:sparql-doctor:p> 1 } WHERE {}"

SELECT_FORMATS = [
    ("application/sparql-results+json", True),
    ("application/sparql-results+xml", True),
    ("text/csv", False),
    ("text/tab-separated-values", False),
    ("application/x-binary-rdf-results-table", False),
]
GRAPH_FORMATS = [
    ("text/turtle", True),
    ("application/n-triples", True),
    ("application/rdf+xml", True),
    ("application/ld+json", False),
    ("application/n-quads", False),
    ("application/trig", False),
]

ALIASES = {
    "application/json": "application/sparql-results+json",
    "application/xml": "application/sparql-results+xml",
    "text/xml": "application/sparql-results+xml",
    "application/x-turtle": "text/turtle",
    "text/plain": "application/n-triples",
    "application/json+ld": "application/ld+json",
    "text/tsv": "text/tab-separated-values",
}


def _validate(accept: str, resp: Response) -> str:
    body = resp.body
    try:
        if accept.endswith("json"):
            doc = json.loads(body.decode("utf-8"))
            if accept == "application/sparql-results+json" and "results" not in doc:
                return "JSON without 'results'"
        elif accept.endswith("xml"):
            ET.fromstring(body)
        elif accept == "text/csv" and b"x" not in body.split(b"\n", 1)[0]:
            return "no 'x' header row"
        elif not body.strip():
            return "empty body"
    except Exception as e:  # noqa: BLE001
        return f"unparsable ({type(e).__name__})"
    return ""


def _probe(ctx: Context, query: str, formats, kind: str, r: CheckResult, tbl):
    supported, missing = [], []
    for accept, important in formats:
        resp = sparql_request(ctx.target, query, "GET", accept=accept)
        got = resp.content_type
        norm = ALIASES.get(got, got)
        if not resp.ok:
            verdict = resp.describe()
            missing.append((accept, important, verdict))
        elif norm != accept:
            verdict = f"returned {got or 'no Content-Type'} instead"
            missing.append((accept, important, verdict))
        else:
            problem = _validate(accept, resp)
            verdict = f"OK{' (alias ' + got + ')' if got != accept else ''}" if not problem else f"OK but {problem}"
            (missing.append((accept, important, problem)) if problem else supported.append(accept))
        tbl.rows.append([kind, accept, verdict])
    return supported, missing


def run(ctx: Context) -> CheckResult:
    r = CheckResult("formats", "Result formats")
    tbl = r.table("Query", "Accept", "Result")
    sel_ok, sel_missing = _probe(ctx, SELECT, SELECT_FORMATS, "SELECT", r, tbl)
    gr_ok, gr_missing = _probe(ctx, CONSTRUCT, GRAPH_FORMATS, "CONSTRUCT", r, tbl)

    default = sparql_request(ctx.target, SELECT, "GET", accept=None)
    tbl.rows.append(["SELECT", "(no Accept header)", f"{default.describe()} → {default.content_type or '-'}"])
    bogus = sparql_request(ctx.target, SELECT, "GET", accept="application/x-sparql-doctor-nonsense")
    tbl.rows.append(["SELECT", "unsupported type", f"{bogus.describe()} → {bogus.content_type or '-'}"])
    r.data.update({"select": sel_ok, "graph": gr_ok})

    if not sel_ok:
        r.add(Status.FAIL, "No standard SELECT result format could be negotiated.")
    for accept, important, why in sel_missing + gr_missing:
        if important:
            r.add(Status.WARN, f"{accept}: {why}",
                  "Most RDF libraries (RDF4J, Jena) request SPARQL XML/JSON and Turtle/N-Triples by default. "
                  "If a proxy rewrites or strips the Accept header, fix the proxy.")
    optional = [a for a, imp, _ in sel_missing + gr_missing if not imp]
    if optional:
        r.add(Status.INFO, "Optional formats not available: " + ", ".join(optional))
    if sel_ok and not any(imp for _, imp, _ in sel_missing + gr_missing):
        r.add(Status.PASS, f"Supports {len(sel_ok)} SELECT and {len(gr_ok)} graph result formats.")
    if bogus.ok:
        r.add(Status.INFO, f"Unsupported Accept types fall back to {bogus.content_type or 'unknown'} (no 406).")
    return r.finalize()
