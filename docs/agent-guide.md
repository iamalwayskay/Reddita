# Agent integration guide

## Choose an interface

Use the CLI when the calling agent can run commands and only needs JSON:

```bash
reddita search "QUERY" 20 --json-meta
reddita sub SUBREDDIT 20 --new --json-meta
reddita subsearch SUBREDDIT "QUERY" 20 --top --json-meta
reddita thread THREAD_ID_OR_URL 50 --json-meta
reddita about SUBREDDIT --json-meta
reddita saved "CUSTOMER LANGUAGE" 50
```

Use the Python API when the host application needs typed capture metadata, provenance, continuation state, or custom budgets. Import only from `reddita` unless extending the package itself.

## Operational contract

1. Install the package and Chromium once with `patchright install chromium`.
2. Use `--mode auto` unless the task specifically needs account or logged-out behaviour. Reddita manages separate account and anonymous profiles; never supply a personal profile, cookies, tokens, or passwords. A human may establish the dedicated account profile with `reddita session login`.
3. Prefer `--json-meta`; parse stdout only after a zero exit code, then inspect top-level `complete` and `gaps` before using `results`. Plain `--json` retains the original results-only shape for compatibility.
4. Profiles live under the platform application-data directory; the six-hour feed cache lives at `~/.cache/reddita/`. Account and anonymous identities never share a profile or cache identity.
5. Keep requests bounded. Split broad research into explicit, reviewable queries.
6. Report empty results and capture errors honestly; do not invent missing records.
7. Do not reinterpret this tool as authorization to access private content or interact with accounts.
8. Inspect `accessMode`, `transport`, and `sessionStatus`. Feed results do not expose vote or comment totals; treat `null` as unavailable.
9. Link posts expose their outbound destination as `url`; `permalink` remains the Reddit evidence URL. Thread comments include IDs, timestamps, and permalinks for citation and deduplication.
10. Add `--save` when evidence should persist. Use `reddita saved QUERY` for local full-text retrieval and `reddita export FILE.jsonl` for an interoperable dataset.

## Common agent tasks

Research mentions of a product:

```bash
reddita search '"PRODUCT NAME"' 25 --new --json-meta
```

Compare discussions in one community:

```bash
reddita subsearch COMMUNITY "TOPIC" 50 --top --json-meta
```

Read a discussion and its top comments:

```bash
reddita thread THREAD_URL 99 --json-meta
```

## Failure handling

- `invalid-locator` or `invalid-parameters`: fix the request; do not retry unchanged.
- `account-not-verified`: run `reddita session login` or choose `web-anon`/`auto`.
- `redirect-denied`: stop; Reddit moved the request away from the requested public page.
- `coverage-unknown`: use `auto` so the public feed can fill a short rendered listing, or narrow the query.
- `network-byte-limit` or continuation gaps: narrow the query or reduce the requested scope.
- `browser-failed`: confirm Chromium is installed, then retry once.
- `403 Blocked`, repeated Reddit errors, or empty HTML: let `auto` use the next declared transport. Never evade a challenge.

## Extending the project

Keep the CLI stable and add fixture-first tests using trimmed real captures for parser changes. Preserve separate managed profiles, verified identity, GET-only browser policy, final-page validation, capture bounds, private storage, and honest gaps. Authentication enables read-only research only; no feature may add account actions or import a personal browser profile.
