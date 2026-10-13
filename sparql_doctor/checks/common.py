"""Helpers shared by several checks."""

from __future__ import annotations

import re
from typing import Dict, Optional

from ..transport import Response

STATUS_HINTS = {
    400: "Bad request - the server rejected the query or request format.",
    401: "Authentication required - pass credentials with --header 'Authorization: ...' or user:pass@ in the URL.",
    403: "Forbidden - often an IP allow-list, missing auth, or a User-Agent policy (see the User-Agent check).",
    404: "Not found - the endpoint path is probably wrong (e.g. /sparql vs /repositories/<id> vs /blazegraph/sparql).",
    405: "Method not allowed for this request style.",
    406: "Not acceptable - the server cannot produce the requested result format.",
    413: "Payload too large - the request body exceeds a server or proxy limit.",
    414: "URI too long - use POST for long queries.",
    415: "Unsupported media type for the request body.",
    429: "Rate limited - slow down; respect Retry-After.",
    431: "Request header fields too large.",
    500: "Server error - check the endpoint's logs.",
    502: "Bad gateway - a proxy/load balancer could not reach the SPARQL server.",
    503: "Service unavailable - overloaded, in maintenance, or rate-limited.",
    504: "Gateway timeout - the proxy gave up waiting for the SPARQL server.",
}


def status_hint(status: Optional[int]) -> Optional[str]:
    return STATUS_HINTS.get(status) if status else None


_SERVER_PATTERNS = [
    ("GraphDB", re.compile(r"graphdb", re.I)),
    ("Virtuoso", re.compile(r"virtuoso", re.I)),
    ("Blazegraph", re.compile(r"blazegraph|bigdata", re.I)),
    ("Apache Jena Fuseki", re.compile(r"fuseki|jena", re.I)),
    ("Stardog", re.compile(r"stardog", re.I)),
    ("Eclipse RDF4J", re.compile(r"rdf4j|sesame", re.I)),
    ("Amazon Neptune", re.compile(r"neptune", re.I)),
    ("QLever", re.compile(r"qlever", re.I)),
    ("AllegroGraph", re.compile(r"allegrograph", re.I)),
    ("Oxigraph", re.compile(r"oxigraph", re.I)),
]

_INFRA_HEADERS = {
    "cf-ray": "Cloudflare",
    "x-amz-cf-id": "Amazon CloudFront",
    "x-amzn-requestid": "AWS",
    "x-amzn-trace-id": "AWS load balancer",
    "x-azure-ref": "Azure Front Door",
    "x-served-by": "Fastly/Varnish",
    "x-varnish": "Varnish",
    "x-cache": "caching proxy",
    "akamai-grn": "Akamai",
    "x-envoy-upstream-service-time": "Envoy",
    "x-kong-upstream-latency": "Kong",
}


def fingerprint(resp: Response) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name in ("Server", "X-Powered-By", "Via"):
        v = resp.header(name)
        if v:
            out[name] = v
    haystack = " ".join(f"{k}: {v}" for k, v in resp.headers) + " " + resp.body[:4000].decode("utf-8", "replace")
    products = [name for name, rx in _SERVER_PATTERNS if rx.search(haystack)]
    if products:
        out["product"] = ", ".join(products)
    infra = sorted({label for h, label in _INFRA_HEADERS.items() if resp.header(h)})
    if infra:
        out["intermediaries"] = ", ".join(infra)
    return out


def short(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}".removesuffix(".0") + " KB"
    return f"{n / (1024 * 1024):.1f}".removesuffix(".0") + " MB"
