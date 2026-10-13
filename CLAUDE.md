# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Project: sparql-doctor

A Python CLI that points at a SPARQL endpoint and produces a diagnostic report customers can attach to support tickets
(HTTP/1.1 vs HTTP/2, keep-alive & idle timeouts, User-Agent policy, CORS, result formats, max query length, federation).
See [README.md](README.md) for usage and what each check reports.

**Commands:**
- `pip install -e ".[dev]"`: install (`.[http2]` adds a real HTTP/2 query via httpx)
- `pytest`: run all tests (they start a local fake endpoint, no network needed)
- `python tests/fake_endpoint.py --idle 4 --block-ua --max-url 4096`: misbehaving endpoint for manual runs
- `sparql-doctor <url> --quick`: fast run (idle probe capped at 20 s)

**Layout:** `transport.py` (http.client plumbing) → `checks/<id>.py` (one `run(ctx) -> CheckResult` each, registered in
`checks/__init__.py`) → `report.py` (text / Markdown / JSON). `cli.py` runs connectivity first, keepalive in the background.

**Rules:**
- Standard library only at runtime. Optional extras must degrade to an INFO finding when missing.
- Code must run on Python 3.10.
- Never let a network error raise out of a check: `raw_request` returns a `Response` with `error_kind` instead.
- Every WARN/FAIL finding needs an actionable `hint`; reports are read by customers and support, not by us.
- Never write secrets into reports: headers go through `redact_headers`, URLs through `redact_url`.
- Probe queries must be cheap (`ASK {}`, comment padding, `LIMIT 1` inside SERVICE); be polite to public endpoints.
- New check behaviour gets a fake-endpoint switch in `tests/fake_endpoint.py` and a test in `tests/test_checks.py`.
- Keep `requirements.txt` in sync with the dependencies in `pyproject.toml`.
- Installation docs live in `ONBOARDING.md`, usage docs in `README.md`; update them when CLI options change.
