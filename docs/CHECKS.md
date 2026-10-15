# What each check means

This guide explains the eight checks: what they look at, why it matters, what SPARQL Doctor actually does, how to read
the result and how to fix common problems. You don't need to know HTTP internals. Every concept is explained the first
time it appears.

- [Start from your symptom](#start-from-your-symptom)
- [How to read a result](#how-to-read-a-result)
- [1. Connectivity & SPARQL protocol](#1-connectivity--sparql-protocol) · `connectivity`
- [2. HTTP version & TLS](#2-http-version--tls) · `http`
- [3. Keep-alive & idle timeout](#3-keep-alive--idle-timeout) · `keepalive`
- [4. User-Agent policy](#4-user-agent-policy) · `user-agent`
- [5. CORS](#5-cors) · `cors`
- [6. Result formats](#6-result-formats) · `formats`
- [7. Maximum query length](#7-maximum-query-length) · `query-length`
- [8. Federation (SERVICE) reachability](#8-federation-service-reachability) · `federation`

---

## Start from your symptom

| You see… | Read first |
|---|---|
| `Connection reset by peer`, `NoHttpResponseException`, `Broken pipe`, errors that come and go | [Keep-alive & idle timeout](#3-keep-alive--idle-timeout), then [Maximum query length](#7-maximum-query-length) |
| `403 Forbidden` from a public endpoint (e.g. Wikidata), but the same query works in the browser | [User-Agent policy](#4-user-agent-policy) |
| Small queries work, big or generated ones fail (`414`, `413`, `431`, resets) | [Maximum query length](#7-maximum-query-length) |
| A query with `SERVICE <…>` fails or hangs, but the remote endpoint works from your laptop | [Federation](#8-federation-service-reachability) |
| Works from the server, fails in a browser app with a "CORS" error in the console | [CORS](#5-cors) |
| "Cannot parse result", "Unsupported content type", `406 Not Acceptable` | [Result formats](#6-result-formats) |
| Certificate / `PKIX` / `SSLHandshakeException` errors, or only Java 11+ clients fail | [HTTP version & TLS](#2-http-version--tls) |
| Nothing works at all: timeouts, `404`, `401`, `Connection refused` | [Connectivity](#1-connectivity--sparql-protocol) |

## How to read a result

Each check gets one overall status, which is the worst of its individual findings:

| Status | Meaning |
|---|---|
| ✔ **PASS** | Works as expected. |
| ℹ **INFO** | Good to know, not a problem by itself. Example: "CORS not enabled" is fine if no browser app talks to the endpoint directly. |
| ⚠ **WARN** | Some clients or some queries will fail. The line starting with **→** says what to do. |
| ✘ **FAIL** | Broken for typical clients. |
| – **SKIP** | Not run: not requested, no input given (e.g. no `--service`), or the endpoint is unreachable. |

**Results describe the path from the machine where you ran the tool.** A proxy, VPN, firewall or load balancer in
between is part of what gets measured, which is why you should run it on the machine that has the problem.

---

## 1. Connectivity & SPARQL protocol

**Id:** `connectivity` · runs first; if it finds the endpoint unreachable, the other checks are skipped.

### What it is

Before anything SPARQL-specific can work, four basic steps must succeed:

1. **DNS:** the hostname (`kg.example.com`) is translated into an IP address.
2. **TCP connect:** a network connection to that address and port (443 for `https`, 80 for `http`) is opened.
3. **HTTP:** a request is sent and an answer comes back.
4. **SPARQL protocol:** the answer is an actual SPARQL result.

The SPARQL 1.1 Protocol standard defines **three ways to send a query**, and clients pick one:

| Style | How the query travels | Typically used for |
|---|---|---|
| **GET** | In the URL: `…/sparql?query=ASK%20%7B%7D` | Short queries, browser links, caching |
| **POST, form-encoded** | In the request body as `query=…` (`Content-Type: application/x-www-form-urlencoded`) | Long queries; most libraries switch to it automatically |
| **POST, direct** | The raw query is the request body (`Content-Type: application/sparql-query`) | Some libraries and tools |

### Why it matters

An endpoint that only supports GET works until someone writes a long query. The client switches to POST and the query
suddenly fails, and that looks like a bug in the query. **Redirects** are a similar trap. If `http://` redirects to
`https://` with status 301 or 302, many HTTP clients repeat a POST as a GET *without the body*, so the query silently
disappears. Status codes 307 and 308 keep the method and body.

### What the tool does

- Resolves the hostname and lists every IP address (IPv4 and IPv6).
- Opens a TCP connection and measures how long it takes (skipped when you go through a proxy).
- Sends `ASK {}`, the cheapest possible SPARQL query, and follows up to 5 redirects. It reports each redirect and
  continues with the final URL.
- Sends `ASK {}` in each of the three styles and checks the answer is a real SPARQL boolean.
- Checks that the `Content-Type` header matches the body (e.g. a body that is XML but labelled JSON).
- Reads response headers to name the server software (Virtuoso, GraphDB, Fuseki…) and anything in front of it
  (Cloudflare, CloudFront, Varnish, nginx…).

### Typical results

| Finding | Meaning / what to do |
|---|---|
| ✘ DNS cannot resolve | Wrong hostname, or an internal name that needs VPN/internal DNS. If only a proxy can reach it, set `HTTPS_PROXY`. |
| ✘ TCP connect failed | A firewall, security group or wrong port. Test from the network of the failing client. |
| ⚠ Endpoint redirects | Configure clients with the final URL shown. |
| ⚠ POST (form/direct) not supported | Long queries will fail once clients switch to POST. Enable all three styles on the server or proxy. |
| ⚠ Content-Type mismatch | Strict clients cannot parse results. Fix the server or proxy setting that sets the header. |
| ✘ No SPARQL request succeeded + "HTML page" | The URL is probably a web UI or landing page, not the endpoint itself (e.g. `/sparql` vs `/repositories/<id>`). |
| `401` / `403` / `404` | Missing credentials (`-H 'Authorization: …'`), forbidden (see [User-Agent policy](#4-user-agent-policy)), or wrong path. |

---

## 2. HTTP version & TLS

**Id:** `http`

### What it is

- **TLS** is the encryption behind `https://`. During the *handshake*, the server presents a **certificate** that
  proves its identity. The client checks that a trusted authority signed it and that it hasn't expired.
- **HTTP/1.1 and HTTP/2** are two versions of the HTTP protocol. HTTP/1.1 sends one request at a time per connection.
  HTTP/2 sends many in parallel over one connection. Client and server agree on the version during the TLS handshake
  through a mechanism called **ALPN**: the client offers `h2, http/1.1` and the server picks one. On plain `http://`, a
  client can instead *ask to upgrade* to HTTP/2 with an `Upgrade: h2c` header.

### Why it matters

- Expired certificates, or certificates sent without their intermediate chain, break some clients but not others.
  Browsers often repair a missing chain themselves; Java and Python do not. That gives you "works in Chrome, fails in
  the application".
- Newer clients (Java 11+ `HttpClient`, browsers, curl) use HTTP/2 when offered, while older stacks (Apache HttpClient
  4.x, many RDF libraries) always use HTTP/1.1. If a proxy has a buggy HTTP/2 implementation, **only the newer clients
  fail**, which is confusing to debug.
- Some old proxies reject the `Upgrade: h2c` header that Java 11+ sends on plain `http://`.
- A server answering in **HTTP/1.0** means an old proxy is in the path. HTTP/1.0 doesn't keep connections open by
  default, so every query pays for a new connection.

### What the tool does

- Performs a TLS handshake offering HTTP/2 and HTTP/1.1, and records the TLS version, cipher, negotiated protocol and
  certificate expiry. It verifies the certificate exactly like a strict client does.
- On plain `http://`, sends a request with an `Upgrade: h2c` header and compares it with a normal request.
- Checks which HTTP version the server answers with.
- **With the `http2` extra installed and when the server offers HTTP/2:** sends the same query once over HTTP/1.1 and
  once over HTTP/2 and compares the results.

### Typical results

| Finding | Meaning / what to do |
|---|---|
| ✘ Certificate verification failed | Install the full certificate chain (including intermediates) on the server, or add the CA to the client's trust store (Java: `cacerts`). `-k` lets you run the other checks anyway. |
| ⚠ Certificate expires in N days | Renew it. |
| ⚠ TLS older than 1.2 | Modern Java and browsers refuse it. Upgrade the server or proxy. |
| ℹ ALPN: server negotiates HTTP/2 | Informational: tells you which clients will use HTTP/2. |
| ⚠ Works over HTTP/1.1 but fails over HTTP/2 | Force HTTP/1.1 in the client (Java: `HttpClient.Version.HTTP_1_1`) or fix the HTTP/2 front end. |
| ⚠ h2c Upgrade header breaks requests | Force HTTP/1.1 in Java clients, or fix the proxy. |
| ⚠ Answers with HTTP/1.0 | An outdated proxy is in the path. Upgrade or reconfigure it. |

---

## 3. Keep-alive & idle timeout

**Id:** `keepalive` · the most useful check for **"Connection reset by peer"**.

### What it is

Opening a connection costs time (a TCP handshake, plus a TLS handshake for `https`). So HTTP clients keep connections
open after a request and **reuse** them for the next query. This is called **keep-alive**, and the set of open
connections a client holds is its **connection pool**.

Nobody keeps an unused connection open forever. After some idle time, the **idle timeout**, the server closes it. So
can anything in between: a load balancer, reverse proxy, firewall or NAT gateway. Typical values: Rhea 3 s, UniProt
5 s, AWS load balancer 60 s, nginx 75 s, Azure load balancer 4 min, AWS NAT gateway 350 s.

There are three ways a connection can end:

| How it ends | What the client sees when it reuses the connection |
|---|---|
| **Graceful close (FIN)**: the other side says "I'm closing" | A client that checks before reusing reconnects silently. One that doesn't gets `Connection reset by peer` or "closed without response". |
| **Reset (RST)**: the other side aborts | `Connection reset by peer`. |
| **Silent drop**: a NAT or firewall forgets the connection without telling anyone | The query **hangs** until the client's timeout. |

### Why it matters

This is the classic intermittent error. The client pools a connection, nobody uses it for a while, the server or a
load balancer closes it, and the next query on that connection fails. It looks random because it only happens after
quiet periods: overnight, or on a low-traffic system. Restarting the application "fixes" it until the next quiet
period.

### What the tool does

It takes three independent measurements:

1. **Advertised:** the server may say how long it keeps connections (`Keep-Alive: timeout=5`). The tool records it.
2. **Passive watch:** it sends one query, then holds the connection idle and waits until the other side closes it,
   recording when and how (FIN or RST).
3. **Reuse ladder:** this does exactly what a connection pool does. Several connections each send a query, sit idle
   for a different time (2, 5, 10, 20, 30, 45, 58, 65 and 75 seconds by default), then send a second query on the
   *same* connection. The table shows which idle times still work and how the others fail.

It also confirms that keep-alive works at all (three queries on one connection).

This check waits on idle connections, which takes up to `--idle-max` seconds (default 75, `--quick` 20). It runs in
the background while the other checks proceed. Raise `--idle-max` (e.g. `360`) to look for longer timeouts.

### Typical results

| Finding | Meaning / what to do |
|---|---|
| ✔ Pooled connections can be reused after up to N s | No idle timeout below N seconds. Use a larger `--idle-max` to look further. |
| ⚠ Idle connections are closed after ≈N s; reuse gets "Connection reset by peer" | The server closes idle connections. That's a normal choice, but clients must not reuse connections older than that. The hint gives a safe value, about 75 % of N. |
| ⚠ Reusing a connection idle between A and B s fails | Same as above, measured by the ladder. |
| ✘ Reusing … HANGS until timeout | A NAT or firewall silently drops idle connections. Evict idle connections in the client earlier, or enable TCP keep-alive. |
| ⚠ Advertises timeout=X but closed after ≈Y s | Something in between (load balancer, proxy) has a shorter timeout than the server says. |
| ⚠ Server does not keep connections alive | Every query opens a new connection. Not a reset risk, just slower. |
| ⚠ Idle timeout could not be measured | The probe queries themselves failed (endpoint slow or limiting connections). Run again with `--only keepalive`. |

**How to fix connection resets** (pick one):

- Make the client evict idle connections before the measured timeout. Apache HttpClient 4.x:
  `evictIdleConnections(N, SECONDS)` and `setValidateAfterInactivity(…)`. OkHttp: `ConnectionPool` keep-alive
  duration.
- Retry idempotent queries (all read queries) once when a connection reset happens.
- Raise the idle timeout on the load balancer or proxy so it is longer than the client's pool timeout.

---

## 4. User-Agent policy

**Id:** `user-agent`

### What it is

Every HTTP request can carry a **User-Agent** header that says which software is making the request, for example:

| Client | Default User-Agent |
|---|---|
| Java's built-in HTTP client | `Java/17.0.10` |
| Apache HttpClient (used by many Java RDF tools) | `Apache-HttpClient/4.5.14 (Java/17.0.10)` |
| Python `requests` | `python-requests/2.32.3` |
| curl | `curl/8.7.1` |
| A browser | `Mozilla/5.0 (…) Chrome/129.0 …` |
| A well-behaved application | `MyCompanyKG/2.1 (https://example.com; ops@example.com)` |

A **User-Agent policy** is a rule on the endpoint side that rejects requests with a missing or generic User-Agent.
Public endpoints use these policies to tell legitimate tools apart from anonymous scrapers and to know whom to contact
when a client misbehaves.

The best-known example is **Wikidata** (Wikimedia's
[User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy)). It answers
`403 Forbidden` to requests with no User-Agent or with the defaults of Java, Apache HttpClient and python-requests. It
accepts descriptive ones that name the tool and give a contact (URL or email).

### Why it matters

The same query works in the browser (which sends a browser User-Agent) and fails from the application (which sends
the library default). The 403 looks like a permission or IP problem, so people look in the wrong place.

This also affects **federation**. When your endpoint runs `SERVICE <https://query.wikidata.org/sparql>`, it is *your
endpoint* that calls Wikidata, using *its own* User-Agent. Your client's User-Agent doesn't matter there.

### What the tool does

It sends the same `ASK {}` query seven times, changing only the User-Agent: none, Java default, Apache HttpClient
default, python-requests default, curl default, a browser, and a descriptive one (the tool's own, or the one you pass
with `-A`). The table shows which were accepted and the start of each rejection message.

### Typical results

| Finding | Meaning / what to do |
|---|---|
| ✔ No User-Agent policy detected | Any client works. |
| ⚠ Endpoint enforces a User-Agent policy - rejected: … | Configure your client with a descriptive User-Agent: tool name, version and contact. For federated queries, configure the outgoing User-Agent of the *endpoint* that runs the `SERVICE` call. |
| ⚠ Rate limiting (429) observed | The endpoint limits request rates. Slow down and respect the `Retry-After` header. |
| Requests fail regardless of User-Agent | Not a User-Agent issue; look at [Connectivity](#1-connectivity--sparql-protocol). |

To test **your** application's User-Agent:

```bash
sparql-doctor https://query.wikidata.org/sparql --only user-agent -A 'MyCompanyKG/2.1 (https://example.com; ops@example.com)'
```

---

## 5. CORS

**Id:** `cors` · only relevant when **browser** apps call the endpoint directly.

### What it is

Browsers apply a security rule called the **same-origin policy**: JavaScript on `https://app.example.com` may not
read responses from `https://kg.example.com` (a different *origin*) unless that server explicitly allows it. The
mechanism for allowing it is **CORS** (Cross-Origin Resource Sharing). The server adds headers such as
`Access-Control-Allow-Origin: https://app.example.com` (or `*` for everyone).

Browsers distinguish two kinds of requests:

- **Simple requests:** GET, and POST with form encoding. The browser sends them directly and only checks the
  `Access-Control-Allow-Origin` header on the response.
- **Preflighted requests:** anything else, such as a direct POST (`application/sparql-query`) or a request with an
  `Authorization` header. The browser first sends an `OPTIONS` request (the *preflight*) to ask permission, and only
  sends the real request if the answer allows it.

### Why it matters

CORS only affects browsers. Server-side clients (application backends, RDF4J, Jena, Python scripts, curl) ignore it
completely. So "CORS is not enabled" is **not a problem** unless a web page talks to the endpoint directly, for example
YASGUI or a JavaScript app.

When it does matter, a typical failure is "short queries work, long ones fail". The app uses GET for short queries and
switches to POST for long ones, and POST isn't allowed. Another is "anonymous access works, logged-in access fails",
because the `Authorization` header needs a preflight.

### What the tool does

It pretends to be a page on `--origin` (default `https://example.org`) and sends a GET, a form POST and a preflight
`OPTIONS` request for a direct POST. It then checks the `Access-Control-Allow-*` headers on each response.

### Typical results

| Finding | Meaning / what to do |
|---|---|
| ✔ Browser apps on … can query via GET and form POST | Browser apps work. |
| ℹ CORS is not enabled | Fine for server-to-server use. Enable it only if a browser app needs direct access. |
| ⚠ Can use GET but not form POST (or vice versa) | Long queries from browser apps will fail. Enable CORS for both methods. |
| ℹ The preflight fails | Browsers can use GET and form POST, but not direct POST or custom headers. Usually fine. |
| ⚠ The preflight fails, so authenticated browser requests are blocked | Shown when you pass an `Authorization` header. Allow `OPTIONS` and the `Authorization` and `Content-Type` headers in the CORS configuration. |
| ⚠ `*` combined with Allow-Credentials `true` | Browsers reject this combination. Return the specific origin instead of `*`. |

Test with your app's real origin:

```bash
sparql-doctor <your-endpoint-url> --only cors --origin <your-app-origin>
```

---

## 6. Result formats

**Id:** `formats`

### What it is

A SPARQL endpoint can return the same result in several formats. The client says which one it wants with the
**`Accept`** header, and the server answers with a **`Content-Type`** header saying what it actually sent. This is
called **content negotiation**.

There are two families of formats, because there are two kinds of queries:

| Query type | Result | Common formats (`Accept` value) |
|---|---|---|
| `SELECT`, `ASK` | A table of variable bindings, or true/false | SPARQL JSON (`application/sparql-results+json`), SPARQL XML (`application/sparql-results+xml`), CSV (`text/csv`), TSV (`text/tab-separated-values`), RDF4J binary |
| `CONSTRUCT`, `DESCRIBE` | An RDF graph | Turtle (`text/turtle`), N-Triples (`application/n-triples`), RDF/XML (`application/rdf+xml`), JSON-LD (`application/ld+json`), N-Quads, TriG |

### Why it matters

Each client library asks for specific formats. If the endpoint (or a proxy that rewrites headers) can't deliver them,
the client gets `406 Not Acceptable`, or worse, receives a different format than it asked for and fails to parse it.

### What the tool does

It runs a trivial `SELECT` once for each table format and a trivial `CONSTRUCT` once for each graph format. For each,
it checks the status code, whether the returned `Content-Type` is the one asked for, and whether the body parses
(JSON, XML, a CSV header). It also records what the server returns with no `Accept` header and with a nonsense one.

### Typical results

| Finding | Meaning / what to do |
|---|---|
| ✔ Supports N SELECT and M graph result formats | Fine. |
| ⚠ `<format>`: 406 / returned something else instead | A format that common clients rely on (SPARQL JSON/XML, Turtle, N-Triples, RDF/XML) is missing. Configure the client to use a supported one, or check whether a proxy strips or rewrites the `Accept` header. |
| ℹ Optional formats not available | CSV, TSV, binary, JSON-LD, N-Quads or TriG are missing. Only matters if your client asks for them. |
| ℹ Unsupported Accept types fall back to … | The server returns a default format instead of `406`. Harmless. |
| ✘ No standard SELECT result format | No client can read results. Check the endpoint URL and any proxy in front. |

---

## 7. Maximum query length

**Id:** `query-length`

### What it is

There are limits on how big a request can be, and they are set on every server and proxy in the path, usually
without anyone thinking about SPARQL:

- **GET:** the query is part of the URL, and URLs have a maximum length. Common defaults are about **8 KB** (Apache
  httpd, nginx, Tomcat), sometimes less behind a web application firewall (WAF).
- **POST:** the query is in the body, which allows much more, but there is still a limit. Examples: nginx 1 MB by
  default (`client_max_body_size`), Tomcat 2 MB (`maxPostSize`).

### Why it matters

Generated queries can get very large: long `VALUES` blocks, `FILTER (?x IN (…thousands of IRIs…))`, or queries built
by a UI. They work in testing and fail in production with real data volumes. How they fail matters too. A clean
`414 URI Too Long` or `413 Payload Too Large` is easy to diagnose. A **connection reset** or a `403` from a firewall
looks like a network or permission problem instead.

### What the tool does

It sends a trivial `ASK {}` padded with a SPARQL comment (`# xxxx…`) to reach a given size, so the server does no real
work. For **GET**, it doubles the size from 1 KB until something fails (up to `--max-get`, default 128 KB), then
narrows the limit down to about 128 characters. For **POST** (both styles), it steps through 1 KB, 4 KB, 16 KB, 64 KB,
256 KB, 1 MB… up to `--max-post` (default 2 MB). Every failure is repeated once so that a temporary glitch is not
mistaken for a limit.

### Typical results

| Finding | Meaning / what to do |
|---|---|
| ✔ accepts at least N (tested limit) | No limit up to the tested size. Raise `--max-get` / `--max-post` to test further. |
| ℹ / ⚠ GET limit: up to ~N chars work | Make sure clients switch to POST for longer queries (most do automatically above about 4 KB). WARN if the limit is under 4,000 characters. |
| ⚠ POST limit: X works, Y fails | Queries larger than X fail. Raise the limit on the proxy or server, or split large `VALUES` blocks into batches. |
| Failure is a connection reset | The server cuts the connection instead of answering 413/414, so clients see `Connection reset by peer`. |
| Failure is a `403` | Size-based `403`s usually come from a WAF (e.g. AWS WAF body-size rules). Adjust the WAF rule. |

---

## 8. Federation (SERVICE) reachability

**Id:** `federation` · runs only when you pass `--service URL` (repeatable).

### What it is

A **federated query** uses the `SERVICE` keyword to fetch part of its data from *another* endpoint:

```sparql
SELECT * WHERE {
  SERVICE <https://query.wikidata.org/sparql> { ?item wdt:P31 wd:Q5 }
}
```

The key point is that **the endpoint you send the query to makes the `SERVICE` call, not your client.** The network
connection goes from the *endpoint's server* to the remote endpoint. That server has its own DNS, firewall rules,
proxy settings, certificate trust store and User-Agent, which can all differ from your laptop's.

### Why it matters

"I can open Wikidata in my browser, so why does the federated query fail?" Because your browser isn't the one making
the call. Typical causes are a server that has no outbound internet access, needs a proxy, doesn't trust the remote
certificate, or sends a User-Agent the remote endpoint rejects.

### What the tool does

For each `--service` target:

1. **From the endpoint:** it asks your endpoint to run
   `SELECT * WHERE { SERVICE <target> { SELECT * WHERE { ?s ?p ?o } LIMIT 1 } }`, which fetches a single triple.
2. **From this machine:** it sends `ASK {}` directly to the target.
3. It compares the two and classifies the endpoint's error message: DNS, timeout, connection refused, TLS/certificate,
   `403` (often User-Agent), `429` (rate limit), proxy, or federation disabled.

### Typical results

| Diagnosis | Meaning / what to do |
|---|---|
| ✔ reachable | Federation to this target works. |
| ✘ reachable from here, NOT from the endpoint | The problem is on the endpoint's side. Check its outbound firewall and proxy settings, its trust store (Java `cacerts`), or its outgoing User-Agent (see [User-Agent policy](#4-user-agent-policy)). |
| ✘ unreachable from both | The target itself is down or blocks both, or the URL is wrong. |
| ✘ DNS | The endpoint's server cannot resolve the target's hostname. |
| ✘ timeout | Outbound traffic from the endpoint's network is probably blocked or needs a proxy. |
| ✘ TLS | The endpoint doesn't trust the target's certificate. Import the CA into its trust store. |
| ✘ HTTP 403 | The target rejects the endpoint, often because of its User-Agent policy. |
| ✘ disabled | Federation is switched off on the endpoint (a security setting on many servers). |
| ⚠ returned no rows | The call "worked" but returned nothing. The target may be empty, or the endpoint may hide errors. Check the endpoint's logs. |
