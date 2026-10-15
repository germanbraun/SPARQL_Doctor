# SPARQL Doctor

Point it at a SPARQL endpoint and get a diagnostic report: HTTP/1.1 vs HTTP/2, keep-alive and idle timeouts,
User-Agent policies, CORS, supported result formats, maximum query length, and whether federation targets are
reachable **from where the query actually runs**.

Attach the report to a support ticket. It saves a lot of back-and-forth on "Connection reset by peer", unexplained
403s, and "works from my laptop but not from the server".

**Installation:** see [ONBOARDING.md](ONBOARDING.md). In short: `pipx install git+https://github.com/germanbraun/SPARQL_Doctor.git`.

---

## Quick start

```bash
sparql-doctor https://query.wikidata.org/sparql
```

A full run takes about 1.5 minutes, mostly spent waiting on idle connections to measure timeouts. Progress appears on
stderr and the report on stdout:

```
✔ PASS  Connectivity & SPARQL protocol  (3.2s)
    ✔ All SPARQL 1.1 request styles work (GET 221 ms).
✔ PASS  Keep-alive & idle timeout  (78.4s)
    ✔ Pooled connections can be reused after up to 75 s idle.
⚠ WARN  User-Agent policy  (1.7s)
    ⚠ Endpoint enforces a User-Agent policy - rejected: (no User-Agent header) (403 Forbidden),
      Java default (403 Forbidden), Apache HttpClient default (403 Forbidden), …
      → Configure the client with a descriptive User-Agent including tool name, version and a contact …
…
Summary: 7 pass, 1 warn, 0 fail, 0 info, 0 skipped
```

## Creating a report for a support ticket

In the examples below, replace `<your-endpoint-url>` with the full URL of your SPARQL endpoint, for example
`http://localhost:7200/repositories/myrepo` (GraphDB), `http://localhost:3030/ds/sparql` (Fuseki) or
`https://query.wikidata.org/sparql`. Replace the `<` and `>` too: they only mark the placeholder.

```bash
sparql-doctor <your-endpoint-url> --markdown sparql-doctor-report.md
```

Attach `sparql-doctor-report.md` to the ticket. It contains the endpoint, the time, the client host and OS, the proxy,
the exact command, an overview table, and every finding with hints and raw measurements. For automated processing use
`--json report.json` instead, or `--json -` to print JSON to stdout.

**Secrets are redacted.** `Authorization`, `Cookie` and API-key headers, and `user:pass@` in URLs, appear as `***` in
every report and in the recorded command line.

**Run it where the problem happens.** A report from your laptop describes your laptop's network path. If
an application server shows the errors, run SPARQL Doctor on that server.

## Common scenarios

### Authenticated endpoints

```bash
# Bearer token / API key
sparql-doctor <your-endpoint-url> -H 'Authorization: Bearer eyJ…'

# Basic auth: either form works
sparql-doctor https://<user>:<password>@<your-endpoint-host>/sparql
sparql-doctor <your-endpoint-url> -H "Authorization: Basic $(printf '<user>:<password>' | base64)"
```

`-H` can be repeated for any additional header.

### "Connection reset by peer" / intermittent failures

```bash
sparql-doctor <your-endpoint-url> --only keepalive --idle-max 360 -v
```

This measures how long the endpoint, or a load balancer or firewall in front of it, keeps an idle connection open, and
what a client sees when it reuses a connection after that point. The usual cause is a client connection pool that
keeps connections longer than the infrastructure does. The report gives the safe eviction time and the settings for
common HTTP clients. Increase `--idle-max` if you suspect timeouts longer than the default 75 s; common values are
60 s (AWS ALB), 240 s (Azure LB) and 350 s (AWS NAT gateway).

### 403 Forbidden from public endpoints (Wikidata and others)

```bash
sparql-doctor https://query.wikidata.org/sparql --only user-agent
```

Shows which User-Agents the endpoint rejects. To check that your own client's User-Agent is accepted, pass it with
`-A`:

```bash
sparql-doctor https://query.wikidata.org/sparql --only user-agent -A 'MyCompanyKG/2.1 (https://example.com; ops@example.com)'
```

### Federated queries (`SERVICE`) failing

```bash
sparql-doctor <your-endpoint-url> \
    --service https://query.wikidata.org/sparql \
    --service https://internal-kg.corp/sparql
```

For each target, the endpoint runs a cheap `SERVICE` query (`LIMIT 1`), and SPARQL Doctor also calls the target
directly from your machine. The verdict, for example *"DNS: reachable from here, NOT from the endpoint"*, tells you
whether to look at the endpoint's network egress, proxy, trust store or outgoing User-Agent.

### Long or generated queries failing

```bash
sparql-doctor <your-endpoint-url> --only query-length --max-post 16777216
```

Finds the largest GET query and POST body the path accepts, and how larger ones fail: a clean `414`/`413`, or a
connection reset that looks like a network problem.

### Browser apps (CORS)

```bash
sparql-doctor <your-endpoint-url> --only cors --origin <your-app-origin>
```

### Quick health check / CI

```bash
sparql-doctor <your-endpoint-url> --quick
echo $?   # 0 = all good, 1 = warnings, 2 = failures
```

`--quick` caps the idle probe at 20 s and uses smaller size limits, for about 30 s in total.

## The checks

New to these topics? **[docs/CHECKS.md](docs/CHECKS.md)** explains each check in plain language: what it is, why it
breaks things, what the tool does, how to read the result and how to fix it. It starts with a
"[which check matches my symptom](docs/CHECKS.md#start-from-your-symptom)" table.

| Id | What it finds |
|---|---|
| [`connectivity`](docs/CHECKS.md#1-connectivity--sparql-protocol) | DNS, TCP, redirects (and whether a redirect turns a POST into a GET, dropping the query), GET / form POST / direct POST support, server and proxy/CDN fingerprint |
| [`http`](docs/CHECKS.md#2-http-version--tls) | TLS version, certificate expiry and verification, ALPN (h2 vs http/1.1), h2c upgrade handling, HTTP/1.0 proxies. With the `http2` extra it also runs a real query over HTTP/2 and HTTP/1.1 and compares them |
| [`keepalive`](docs/CHECKS.md#3-keep-alive--idle-timeout) | Keep-alive support, the advertised `Keep-Alive: timeout`, the **actual** idle timeout (a passive close/reset watch plus a "pooled connection reuse" ladder), silent drops by NAT/firewalls, and advertised vs. real timeout mismatches |
| [`user-agent`](docs/CHECKS.md#4-user-agent-policy) | Missing and library-default User-Agents (Java, Apache HttpClient, python-requests, curl) compared with browser and descriptive ones |
| [`cors`](docs/CHECKS.md#5-cors) | Simple GET and form POST, and the preflight for direct POST, from `--origin` |
| [`formats`](docs/CHECKS.md#6-result-formats) | SELECT (JSON, XML, CSV, TSV, binary) and CONSTRUCT (Turtle, N-Triples, RDF/XML, JSON-LD, N-Quads, TriG) negotiation, the default format, 406 behaviour |
| [`query-length`](docs/CHECKS.md#7-maximum-query-length) | Largest GET query (narrowed down to about 128 chars) and POST body, and the failure mode |
| [`federation`](docs/CHECKS.md#8-federation-service-reachability) | For each `--service URL`: a `SERVICE` call made by the endpoint, compared with a direct call from your machine, with the error classified |

Run a subset with `--only connectivity,keepalive` or exclude checks with `--skip formats`. If the endpoint is
unreachable, the remaining checks are skipped.

## Reading the results

| Status | Meaning |
|---|---|
| ✔ PASS | Works as expected. |
| ℹ INFO | Worth knowing, not a problem by itself (e.g. "CORS not enabled" for a server-to-server setup). |
| ⚠ WARN | Will break some clients or some queries. Read the **→ hint**. |
| ✘ FAIL | Broken for typical clients. |
| – SKIP | Not run (not requested, no `--service` given, or endpoint unreachable). |

Terminal output shows details only for WARN and FAIL checks. Add `-v` to see every finding, hint and table.

## Is it safe to run against production?

Yes. All probe queries are trivial (`ASK {}`, a one-row `SELECT`/`CONSTRUCT` with no data access, and
comment-padded queries for the size tests). Requests are spaced at least 0.15 s apart (`--delay`). The size tests send
up to 128 KB GET and 2 MB POST requests by default; lower them with `--max-get` / `--max-post` or use `--quick`. The
idle probe holds about 10 idle connections open for up to `--idle-max` seconds.

## Public endpoints

SPARQL Doctor works against public endpoints without any configuration. It was verified (October 2026, `--quick`)
against these endpoints:

| Endpoint | Front end detected | Notable findings |
|---|---|---|
| `https://query.wikidata.org/sparql` | nginx + Fastly/Varnish | User-Agent policy: no User-Agent, Java, Apache HttpClient and python-requests defaults get 403 |
| `https://dbpedia.org/sparql` | Virtuoso | none |
| `https://sparql.uniprot.org/sparql` | Apache (SIB) | idle connections closed after ≈5 s; HTTP/1.1 only |
| `https://sparql.rhea-db.org/sparql` | Apache (SIB) | idle connections closed after ≈3 s; HTTP/1.1 only |
| `https://qlever.cs.uni-freiburg.de/api/wikidata` | nginx (QLever) | 308 redirect to `qlever.dev`; idle close ≈5 s; no RDF/XML |
| `https://vocab.getty.edu/sparql` | nginx | XML body labelled as JSON; 403 above ≈2 KB GET / ≈8 KB POST (firewall-style) |
| `https://data.europa.eu/sparql` | Virtuoso + CloudFront | CORS preflight returns 500 (simple GET/POST work) |
| `https://publications.europa.eu/webapi/rdf/sparql` | not disclosed | no CORS; HTTP/1.1 only |
| `https://lindas.admin.ch/query` | nginx | none |

Short idle timeouts like UniProt's are legitimate server choices, not bugs. The warning tells clients how to configure
their connection pools. Public endpoints sometimes have transient errors, so the tool repeats any failed size probe
once and retries keep-alive probes before drawing conclusions. If a result looks surprising, run that check again
(e.g. `--only keepalive`).

## All options

```
sparql-doctor ENDPOINT [options]

  -H, --header 'Name: value'   extra request header (repeatable), e.g. Authorization
  -A, --user-agent UA          User-Agent for normal probes
  --service URL                federation target to test via SERVICE (repeatable)
  --origin URL                 Origin for the CORS check (default https://example.org)
  --only IDS / --skip IDS      comma-separated check ids
  --idle-max SECONDS           longest idle period to probe (default 75)
  --quick                      idle-max 20, max-get 32 KB, max-post 256 KB
  --max-get CHARS              largest GET query to try (default 131072)
  --max-post BYTES             largest POST body to try (default 2097152)
  --timeout SECONDS            per-request timeout (default 30)
  --service-timeout SECONDS    timeout for SERVICE probes (default 60)
  --delay SECONDS              minimum gap between requests (default 0.15)
  -k, --insecure               do not verify TLS certificates
  --no-proxy                   ignore HTTP(S)_PROXY environment variables
  --markdown FILE              write a Markdown report
  --json FILE                  write a JSON report ('-' for stdout)
  -v, --verbose                show all findings, hints and tables
  --no-color                   plain output (also honours NO_COLOR)
```

Exit codes: `0` no problems, `1` warnings, `2` failures.

## Development

See [ONBOARDING.md](ONBOARDING.md#for-contributors) and [CLAUDE.md](CLAUDE.md).
