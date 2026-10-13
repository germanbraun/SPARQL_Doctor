# SPARQL Doctor

Point it at a SPARQL endpoint, get a diagnostic report. Customers can attach the Markdown or JSON output to support
tickets, which cuts the back-and-forth on "Connection reset by peer", 403s and "works in the browser but not from the
server" issues.

```bash
pip install git+https://github.com/germanbraun/SPARQL_Doctor.git
sparql-doctor https://query.wikidata.org/sparql --markdown report.md
```

It has no runtime dependencies (Python ≥ 3.10 standard library). `pip install "sparql-doctor[http2]"` adds a real
HTTP/2 query on top of the ALPN probe.

## What it checks

| Check (`--only` id) | What it finds |
|---|---|
| `connectivity` | DNS, TCP, redirects (POST→GET body loss), GET / form POST / direct POST support, server & proxy/CDN fingerprint |
| `http` | TLS version, certificate expiry/verification, ALPN (h2 vs http/1.1), h2c upgrade handling, HTTP/1.0 proxies, optional real HTTP/2 vs HTTP/1.1 comparison |
| `keepalive` | Keep-alive support, advertised `Keep-Alive: timeout`, the **actual idle timeout** (passive FIN/RST watch plus a "pooled connection reuse" ladder), silent drops by NAT/firewalls, and advertised vs. real timeout mismatches |
| `user-agent` | Whether missing or library-default User-Agents (Java, Apache HttpClient, python-requests, curl) are rejected, e.g. the Wikidata/Wikimedia policy |
| `cors` | Simple GET and POST preflight from `--origin` |
| `formats` | SELECT (JSON, XML, CSV, TSV, binary) and CONSTRUCT (Turtle, N-Triples, RDF/XML, JSON-LD, N-Quads, TriG) negotiation, default format, 406 behaviour |
| `query-length` | Largest GET query (bisected to about 128 chars) and POST body, plus *how* oversized requests fail (414 vs. a connection reset) |
| `federation` | For each `--service URL`: a `SERVICE` call executed **by the endpoint**, compared with reachability from your machine, with the error classified (DNS, timeout, TLS trust store, 403/User-Agent, disabled…) |

## Usage

```
sparql-doctor ENDPOINT [-H 'Authorization: Bearer …'] [--service URL]… [--markdown FILE] [--json FILE]
              [--quick] [--only ids] [--skip ids] [--idle-max S] [--max-get N] [--max-post N]
              [-A USER_AGENT] [--origin URL] [-k] [--no-proxy] [-v]
```

* By default the idle-timeout probe waits up to 75 s, which covers the common 60 s load-balancer timeouts. It runs in
  the background while the other checks proceed. Use `--quick` (20 s) for a fast look or `--idle-max 360` to hunt for
  longer timeouts.
* `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` are honoured. When a proxy is used, timeouts describe the client↔proxy leg.
* Exit code: `0` all good, `1` warnings, `2` failures.
* Credentials (`Authorization`, `Cookie`, `user:pass@` in URLs) are redacted from every report.

## Development

```bash
pip install -e ".[dev]"
pytest                                   # uses a local fake endpoint, no network
python tests/fake_endpoint.py --idle 4 --idle-rst --block-ua --max-url 4096 --cors
```
