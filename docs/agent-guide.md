# Agent integration guide

## Choose an interface

Use the CLI when the calling agent can run commands and only needs JSON:

```bash
reddita search "QUERY" 20 --json
reddita sub SUBREDDIT 20 --new --json
reddita subsearch SUBREDDIT "QUERY" 20 --top --json
reddita thread THREAD_ID_OR_URL 50 --json
reddita about SUBREDDIT --json
```

Use the Python API when the host application needs typed capture metadata, provenance, continuation state, or custom budgets. Import only from `reddita` unless extending the package itself.

## Operational contract

1. Install the package and Chromium once with `patchright install chromium`.
2. Run unauthenticated. Reddita manages its own persistent profile; do not supply a personal profile, cookies, tokens, or passwords.
3. Prefer `--json`; parse stdout only after a zero exit code.
4. The managed profile lives at `~/.local/share/reddita/browser-profile/`; the six-hour cache lives at `~/.cache/reddita/`. Bounded post and thread captures are cached even when Reddit indicates more results exist. Use `--fresh` only when current data matters.
5. Keep requests bounded. Split broad research into explicit, reviewable queries.
6. Report empty results and capture errors honestly; do not invent missing records.
7. Do not reinterpret this tool as authorization to access private content or interact with accounts.
8. Feed results do not expose vote or comment totals. Treat `null` metrics as unavailable; use Reddit's returned order for relevance/top/new ranking.

## Common agent tasks

Research mentions of a product:

```bash
reddita search '"PRODUCT NAME"' 25 --new --json
```

Compare discussions in one community:

```bash
reddita subsearch COMMUNITY "TOPIC" 50 --top --json
```

Read a discussion and its top comments:

```bash
reddita thread THREAD_URL 99 --json
```

## Failure handling

- `invalid-locator` or `invalid-parameters`: fix the request; do not retry unchanged.
- `redirect-denied`: stop; Reddit moved the request away from the requested public feed or community page.
- `network-byte-limit` or continuation gaps: narrow the query or reduce the requested scope.
- `browser-failed`: confirm Chromium is installed, then retry once.
- `403 Blocked`, repeated Reddit errors, or empty HTML: stop and report the upstream limitation. Do not evade it; use the official authenticated API if the user has authorized credentials.

## Extending the project

Keep the CLI stable and add fixture-first tests using trimmed real captures for parser changes. Preserve the managed profile, GET-only browser policy, final Reddit-page validation, capture bounds, and private cache. No feature may add authenticated actions or import a personal browser profile.
