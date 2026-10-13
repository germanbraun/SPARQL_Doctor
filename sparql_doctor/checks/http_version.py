"""HTTP/1.1 vs HTTP/2 negotiation and behaviour, plus TLS details."""

from __future__ import annotations

import datetime as dt
import ssl
import time

from ..model import CheckResult, Context, Status
from ..transport import SPARQL_JSON, open_connection, raw_request, sparql_request, ssl_context

PROBE = "ASK {}"


def _tls(ctx: Context, r: CheckResult) -> None:
    t = ctx.target
    # 1) verified handshake offering h2 + http/1.1 to learn ALPN, TLS version and certificate
    info = {}
    for verify in ((True, False) if t.insecure else (True,)):
        conn = open_connection(t, timeout=min(t.timeout, 20), context=ssl_context(t, ["h2", "http/1.1"], verify=verify))
        start = time.monotonic()
        try:
            conn.connect()
            s = conn.sock
            info = {
                "alpn": s.selected_alpn_protocol(),
                "tls_version": s.version(),
                "cipher": s.cipher()[0] if s.cipher() else None,
                "handshake_ms": int((time.monotonic() - start) * 1000),
                "verified": verify,
            }
            cert = s.getpeercert() if verify else None
            if cert:
                not_after = dt.datetime.utcfromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]))
                info["cert_not_after"] = not_after.strftime("%Y-%m-%d")
                info["cert_days_left"] = (not_after - dt.datetime.utcnow()).days
                info["cert_issuer"] = dict(x[0] for x in cert.get("issuer", ())).get("organizationName")
            break
        except ssl.SSLCertVerificationError as e:
            r.add(Status.FAIL, f"TLS certificate verification failed: {e.verify_message or e}",
                  "Clients without a custom trust store (Java cacerts, Python certifi) will refuse this endpoint. "
                  "Install the full certificate chain (intermediates!) on the server.")
            if not t.insecure:
                return
        except Exception as e:  # noqa: BLE001
            r.add(Status.FAIL, f"TLS handshake failed: {type(e).__name__}: {e}")
            return
        finally:
            conn.close()
    r.data["tls"] = info
    r.add(Status.PASS if info.get("tls_version") in ("TLSv1.2", "TLSv1.3") else Status.WARN,
          f"TLS: {info.get('tls_version')} ({info.get('cipher')}), handshake {info.get('handshake_ms')} ms",
          None if info.get("tls_version") in ("TLSv1.2", "TLSv1.3") else "TLS < 1.2 is rejected by modern JVMs and browsers.")
    days = info.get("cert_days_left")
    if days is not None:
        if days < 0:
            r.add(Status.FAIL, f"Certificate expired on {info['cert_not_after']}.")
        elif days < 21:
            r.add(Status.WARN, f"Certificate expires in {days} days ({info['cert_not_after']}).")
        else:
            r.add(Status.PASS, f"Certificate valid until {info['cert_not_after']} (issuer: {info.get('cert_issuer')}).")
    alpn = info.get("alpn")
    ctx.notes["alpn"] = alpn
    if alpn == "h2":
        r.add(Status.INFO, "ALPN: server negotiates HTTP/2 (h2) when offered.",
              "Clients that offer h2 (Java 11+ HttpClient, browsers, curl, httpx) will use HTTP/2 here; "
              "older stacks (Apache HttpClient 4.x, RDF4J) always use HTTP/1.1.")
    elif alpn == "http/1.1":
        r.add(Status.INFO, "ALPN: server only offers HTTP/1.1.")
    else:
        r.add(Status.INFO, "ALPN: no protocol negotiated (server ignores ALPN) - HTTP/1.1 will be used.")


def _h2c(ctx: Context, r: CheckResult) -> None:
    t = ctx.target
    resp = raw_request(t, "GET", t.path_with({"query": PROBE}), {
        "Accept": SPARQL_JSON,
        "Connection": "Upgrade, HTTP2-Settings",
        "Upgrade": "h2c",
        "HTTP2-Settings": "AAMAAABkAARAAAAAAAIAAAAA",
    })
    ctx.notes["alpn"] = "h2c" if resp.status == 101 else "http/1.1"
    if resp.status == 101:
        r.add(Status.INFO, "Cleartext HTTP/2 upgrade (h2c) is accepted.")
    elif resp.ok:
        r.add(Status.PASS, "Plain-HTTP endpoint ignores the h2c upgrade and answers over HTTP/1.1 (normal).")
    else:
        r.add(Status.WARN, f"A request carrying an h2c Upgrade header failed ({resp.describe()}).",
              "Some proxies reject Upgrade headers. Java 11+ HttpClient sends 'Upgrade: h2c' on plain http by default; "
              "force HTTP/1.1 in the client (HttpClient.Version.HTTP_1_1) if queries fail from Java.")


def _http2_request(ctx: Context, r: CheckResult) -> None:
    try:
        import h2  # noqa: F401 - httpx needs it for http2=True
        import httpx
    except ImportError:
        r.add(Status.INFO, "Skipped a real HTTP/2 query (install 'sparql-doctor[http2]' to enable).")
        return

    t = ctx.target
    headers = dict(t.headers)
    headers.update({"User-Agent": t.user_agent, "Accept": SPARQL_JSON})
    tbl = r.table("Protocol", "Result", "Negotiated", "Time")
    results = {}
    for label, use_h2 in (("HTTP/1.1", False), ("HTTP/2", True)):
        start = time.monotonic()
        try:
            with httpx.Client(http1=not use_h2, http2=use_h2, verify=not t.insecure, timeout=t.timeout) as c:
                resp = c.get(t.url, params={"query": PROBE}, headers=headers)
                results[label] = (resp.status_code, resp.http_version)
                tbl.rows.append([label, f"{resp.status_code} {resp.reason_phrase}", resp.http_version,
                                 f"{int((time.monotonic() - start) * 1000)} ms"])
        except Exception as e:  # noqa: BLE001
            results[label] = (None, str(e))
            tbl.rows.append([label, f"error: {type(e).__name__}: {e}", "-", f"{int((time.monotonic() - start) * 1000)} ms"])
    s1, s2 = results["HTTP/1.1"][0], results["HTTP/2"][0]
    ok = lambda s: s is not None and 200 <= s < 300  # noqa: E731
    if ok(s1) and not ok(s2):
        r.add(Status.WARN, "Query works over HTTP/1.1 but FAILS over HTTP/2.",
              "Force HTTP/1.1 in the client, or fix the HTTP/2 front end (proxy/LB). "
              "Typical symptoms: stream resets, 'Connection reset', GOAWAY on long-running queries.")
    elif ok(s2) and not ok(s1):
        r.add(Status.WARN, "Query works over HTTP/2 but fails over HTTP/1.1.")
    elif ok(s1) and ok(s2):
        r.add(Status.PASS, f"Real query succeeds over both HTTP/1.1 and HTTP/2 (negotiated {results['HTTP/2'][1]}).")


def run(ctx: Context) -> CheckResult:
    r = CheckResult("http", "HTTP version & TLS")
    t = ctx.target
    if t.is_https:
        _tls(ctx, r)
    else:
        r.add(Status.INFO, "Endpoint uses plain HTTP (no TLS).",
              "Credentials and queries travel unencrypted; some browsers block mixed content from https UIs.")
        _h2c(ctx, r)

    resp = sparql_request(t, PROBE, "GET")
    if resp.version:
        r.data["http1_response_version"] = resp.version
        if resp.version == "HTTP/1.0":
            r.add(Status.WARN, "Server answers HTTP/1.1 requests with HTTP/1.0.",
                  "HTTP/1.0 has no default keep-alive and no chunked encoding; an old proxy is likely in the path.")
    _http2_request(ctx, r)
    return r.finalize()
