"""DNS, TCP, redirects and the three SPARQL 1.1 protocol request styles."""

from __future__ import annotations

import socket
import time
import urllib.parse

from ..model import CheckResult, Context, Status
from ..transport import SPARQL_JSON, ask_boolean, body_syntax, sparql_request
from .common import fingerprint, status_hint

PROBE = "ASK {}"


def run(ctx: Context) -> CheckResult:
    r = CheckResult("connectivity", "Connectivity & SPARQL protocol")
    t = ctx.target
    proxy = t.proxy()
    if proxy:
        r.add(Status.INFO, f"Requests go through proxy {proxy[0]}:{proxy[1]} (from environment).")
        r.data["proxy"] = f"{proxy[0]}:{proxy[1]}"

    # DNS
    start = time.monotonic()
    try:
        infos = socket.getaddrinfo(t.host, t.port, type=socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos})
        r.data["dns"] = {"addresses": addrs, "ms": int((time.monotonic() - start) * 1000)}
        r.add(Status.PASS, f"DNS: {t.host} → {', '.join(addrs[:6])}{' …' if len(addrs) > 6 else ''}")
    except socket.gaierror as e:
        if proxy:
            r.add(Status.INFO, f"DNS: {t.host} does not resolve locally ({e}); relying on the proxy.")
        else:
            r.add(Status.FAIL, f"DNS: cannot resolve {t.host} ({e}).",
                  "Check the hostname, VPN or internal DNS. If the endpoint is only reachable via a proxy, set HTTPS_PROXY.")
            ctx.notes["unreachable"] = True
            return r.finalize()

    # TCP connect (direct only - through a proxy this would measure the proxy)
    if not proxy:
        start = time.monotonic()
        try:
            with socket.create_connection((t.host, t.port), timeout=min(t.timeout, 15)) as s:
                peer = s.getpeername()[0]
            ms = int((time.monotonic() - start) * 1000)
            r.data["tcp"] = {"peer": peer, "connect_ms": ms}
            r.add(Status.PASS, f"TCP connect to {peer}:{t.port} in {ms} ms")
        except OSError as e:
            r.add(Status.FAIL, f"TCP connect to {t.host}:{t.port} failed: {e}",
                  "Firewall, security group or wrong port. Try from the same network as the failing client.")
            ctx.notes["unreachable"] = True
            return r.finalize()

    # Redirects (followed for the rest of the run, but reported because POST + 301/302 loses the body)
    for _ in range(5):
        resp = sparql_request(ctx.target, PROBE, "GET")
        loc = resp.header("Location")
        if resp.status in (301, 302, 303, 307, 308) and loc:
            new = urllib.parse.urljoin(ctx.target.url, loc)
            # keep only the endpoint part; drop our probe query string
            p = urllib.parse.urlsplit(new)
            qs = "&".join(x for x in p.query.split("&") if x and not x.startswith("query="))
            new = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, qs, ""))
            hint = ("Configure clients with the final URL. Many HTTP clients turn a POST into a GET on 301/302 "
                    "and silently drop the query." if resp.status in (301, 302, 303) else
                    "Configure clients with the final URL to save a round trip per query.")
            r.add(Status.WARN, f"Endpoint redirects ({resp.status}) {ctx.target.safe_url} → {new}", hint)
            ctx.target = ctx.target.with_url(new)
            ctx.notes["redirected_to"] = new
        else:
            break

    tbl = r.table("Request style", "Result", "Content-Type", "Time", "Answer")
    results = {}
    for method, label in (("GET", "GET ?query="), ("POST", "POST form-encoded"),
                          ("POST-DIRECT", "POST application/sparql-query")):
        resp = sparql_request(ctx.target, PROBE, method, accept=SPARQL_JSON)
        results[method] = resp
        val = ask_boolean(resp)
        tbl.rows.append([label, resp.describe(), resp.content_type or "-", f"{resp.ms} ms",
                         "-" if val is None else str(val).lower()])

    ok = [m for m, x in results.items() if x.ok]
    if not ok:
        first = results["GET"]
        if all(x.error for x in results.values()):
            ctx.notes["unreachable"] = True
            r.add(Status.FAIL, f"No SPARQL request succeeded: {first.describe()}",
                  "The endpoint accepts TCP connections but no HTTP request completed. "
                  "Check TLS settings, proxies and server logs.")
        else:
            ctx.notes["no_sparql"] = True
            hint = status_hint(first.status) or ""
            if body_syntax(first) == "html":
                hint += (" The server answered with an HTML page: check that this is the SPARQL endpoint URL "
                         "and not a web UI or landing page.")
            r.add(Status.FAIL, f"No SPARQL request succeeded (GET: {first.describe()}). Body: {first.text(200)!r}",
                  hint.strip() or None)
    else:
        get = results["GET"]
        declared = "json" if "json" in get.content_type else "xml" if "xml" in get.content_type else None
        actual = body_syntax(get)
        if get.ok and ask_boolean(get) is None:
            r.add(Status.WARN, f"GET succeeded but the ASK result is not parsable SPARQL JSON/XML "
                               f"(Content-Type: {get.content_type or 'none'}).",
                  "Is the URL really the SPARQL endpoint and not a UI page?")
        elif get.ok and declared and actual in ("json", "xml") and declared != actual:
            r.add(Status.WARN, f"Content-Type mismatch: response is labelled {get.content_type} but the body is "
                               f"{actual.upper()}.",
                  "Clients that trust the Content-Type (RDF4J, Jena, browsers) will fail to parse results. "
                  "Fix the server or proxy configuration that sets the Content-Type.")
        for m, resp in results.items():
            if not resp.ok:
                label = {"GET": "GET", "POST": "POST (form)", "POST-DIRECT": "POST (direct)"}[m]
                r.add(Status.WARN, f"{label} is not supported: {resp.describe()}",
                      "SPARQL 1.1 Protocol requires GET, form POST and direct POST. Clients switch to POST "
                      "for long queries, so a missing POST variant breaks large queries only." if m != "GET"
                      else status_hint(resp.status))
        if len(ok) == 3:
            r.add(Status.PASS, f"All SPARQL 1.1 request styles work (GET {results['GET'].ms} ms).")

    good = next((results[m] for m in ("GET", "POST", "POST-DIRECT") if results[m].status), None)
    if good is not None:
        fp = fingerprint(good)
        r.data["server"] = fp
        if fp:
            r.add(Status.INFO, "Server/infrastructure hints: " + "; ".join(f"{k}: {v}" for k, v in fp.items()))
    return r.finalize()
