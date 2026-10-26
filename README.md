# Reddita

Reddita is a read-only Reddit research CLI and Python library. It searches posts, reads communities and thread trees, records provenance and completeness, and saves deduplicated evidence in a private searchable SQLite corpus.

It works without Reddit developer credentials. Its default `auto` mode tries a verified Reddita-only account session, then a separate warmed anonymous session, then Reddit's public feeds. The output always says which route worked.

## Install

Requires Python 3.12+ and works on macOS, Linux, and Windows.

```bash
git clone https://github.com/iamalwayskay/Reddita.git
cd Reddita
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
patchright install chromium
```

On a clean Mac, install Python first with `brew install python@3.12` or use [python.org](https://www.python.org/downloads/macos/). Account login uses installed Google Chrome; normal research can use Patchright's bundled Chromium.

## Quick start

```bash
reddita search "cat joint supplement" 50 --mode auto --json-meta
reddita subsearch CatAdvice "arthritis supplement" 50 --top --save --json-meta
reddita thread https://www.reddit.com/r/CatAdvice/comments/POST_ID/title/ 99 --save --json-meta
reddita about CatAdvice --json-meta
reddita search "neuropathy product" 1000 --extended --save --json-meta
```

Use `--json` for the stable results-only shape. Prefer `--json-meta` for research: it includes `complete`, typed `gaps`, provenance, the requested/actual access path, and records. `--fresh` bypasses the six-hour feed cache.

## LLM and agent use

LLM callers should use `--agent`, not parse the human-readable output:

```bash
reddita search "neuropathy product" 1000 --extended --save --agent
```

`--agent` emits exactly one compact JSON document and nothing on stderr. Successful responses use schema `reddita.agent.v1` and include `ok`, `complete`, `gaps`, `continuation`, `nextAction`, provenance, and results. Failures use the same schema with a stable error `code`, message, `retryable` boolean, and `nextAction`. Exit code `0` means usable results, including honestly labelled partial results; exit code `2` means no usable result. Agents should consume partial evidence when `nextAction` is `use-partial-results`, inspect `gaps`, and never create a tight retry loop after `rate-limited`.

## Access modes

| Mode | What it does | When to use it |
|---|---|---|
| `auto` | Verified account web → anonymous web → public feed | Default; highest chance of a useful answer |
| `web-account` | Requires a verified username in Reddita's dedicated account profile | Standard Reddit as a logged-in user |
| `web-anon` | Standard `www.reddit.com` with a separate persistent anonymous profile | Logged-out research with live metrics when available |
| `feed` | Public Atom feeds | Fast, robust fallback; scores and comment totals can be `null` |

Web routes try same-origin Reddit JSON first and modern Reddit DOM second. Feed fallback is reported as `feed`/`atom`; it is never mislabeled as a complete scored web capture.

### Dedicated account session

```bash
reddita session login
reddita session status
reddita search "neuropathy foot cream" 50 --mode web-account --json-meta
```

`session login` opens ordinary Chrome. You type into Reddit directly; Reddita never receives a username, password, or cookie value. Login succeeds only when Reddit returns a real username from `/api/me.json`. Reddita never imports or touches your everyday Chrome profile.

Profiles are separated under the platform application-data directory:

- macOS: `~/Library/Application Support/Reddita/profiles/`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/reddita/profiles/`
- Windows: `%LOCALAPPDATA%\Reddita\profiles\`

## Product and avatar research recipe

Good product research uses several queries and keeps both durable demand and recent language:

```bash
reddita search 'cat arthritis supplement' 50 --top --save --json-meta
reddita search 'cat joint supplement side effects' 50 --new --save --json-meta
reddita subsearch CatAdvice 'senior cat mobility' 50 --top --save --json-meta
reddita search 'neuropathy cream stopped working' 50 --top --save --json-meta
reddita search 'neuropathy supplement scam' 50 --new --save --json-meta
```

Then read promising threads, save their comments, and query the corpus for customer language:

```bash
reddita thread POST_URL 99 --save --json-meta
reddita saved "side effects" 50
reddita saved "wish it" 50
reddita export research.jsonl 10000
```

Look for symptoms, failed alternatives, objections, desired outcomes, product forms, brands, and exact phrases people repeat. Every saved item retains its Reddit permalink, first/last seen timestamps, capture count, and normalized source fields. Re-running a query updates an item by stable Reddit identity instead of duplicating it.

## Commands

```text
reddita search QUERY [N] [--new|--top] [--mode MODE] [--save] [--extended]
reddita sub SUBREDDIT [N] [--new|--top] [--mode MODE] [--save] [--extended]
reddita subsearch SUBREDDIT QUERY [N] [--new|--top] [--mode MODE] [--save] [--extended]
reddita thread URL_OR_ID [N] [--mode MODE] [--save] [--extended]
reddita about SUBREDDIT [--mode MODE] [--save]
reddita saved QUERY [N]
reddita export FILE.jsonl [N]
reddita session login|status
```

Add `--agent` to any research command for the stable LLM/tool-calling response contract.

Listing commands return up to 100 posts and thread commands return up to 99 comments by default. `--extended` raises the bounded ceiling to 2,500 posts or 1,000 comments, with a one-hour/1,000-request hard budget. Extended work remains sequential and is intended for saved research runs, not parallel scraping.

## Capacity and rate limiting

Reddita reduces avoidable traffic before increasing ceilings:

- identical feed, anonymous-web, and username-scoped account captures use the private six-hour cache;
- successful JSON pagination keeps one rendered Reddit session instead of reloading the complete site for every page;
- web data requests and feed pages are paced sequentially; extended feed pages wait 6–8 seconds;
- HTTP `429` and transient server failures receive at most two bounded retries;
- `Retry-After` is honored when it fits inside the remaining run deadline;
- an exhausted `429` is reported as `rate-limited` and stops cross-mode fallback instead of multiplying traffic.

`--extended` increases Reddita's local run budget; it does not increase or bypass a limit imposed by Reddit. Large-scale or commercial collection may require Reddit authorization under Reddit's [Data API Terms](https://redditinc.com/policies/data-api-terms).

## Output contract

All records have a stable ID, record type, source index, canonical permalink/source URL, and canonical JSON body. Web posts include available score and comment count. Thread comments include parent ID, depth, top-level status, timestamp, score, and permalink. `null` means Reddit did not expose a value; it never means zero.

| Field | Meaning |
|---|---|
| `complete` | Reddita saw no known uncollected page/comment gap |
| `gaps` | Typed reasons such as `record-limit`, `comment-limit`, challenge, or selector drift |
| `accessMode` | Actual route: `web-account`, `web-anon`, or `feed` |
| `transport` | Actual representation: `json`, `dom`, or `atom` |
| `sessionStatus` | Observed identity state for a web session |
| `acquisitionMode` | Fresh acquisition or cache hit |

Never ignore `complete` and `gaps` in automated research.

## Python API

```python
import json
from pathlib import Path

from reddita import ResearchCorpus, SourceBudgets, SourceRequest, acquire_reddit_research

request = SourceRequest(
    source="reddit", operation="search", locator="cat joint supplement",
    parameters_json=json.dumps({"pageSize": 25, "maxComments": 100, "sort": "top"}),
    max_records=50, max_pages=2,
)
capture = acquire_reddit_research(
    request, budgets=SourceBudgets(timeout_seconds=300),
    settings_digest="0" * 64, mode="auto",
)
with ResearchCorpus(Path.home() / ".local/share/reddita/research.sqlite3") as corpus:
    corpus.save_capture(capture, query_metadata={"project": "cat-supplement"})
    matches = corpus.search("side effects", limit=50)
```

The stable public objects are exported from `reddita`. The CLI remains easiest because it manages private directories, profiles, caching, mode selection, and formatting.

## Safety and reliability

- GET-only browser boundary. No posting, voting, messaging, moderation, or private-community access.
- Separate private, symlink-rejecting, exclusively locked account and anonymous profiles.
- Account mode fails closed unless Reddit verifies a username. Cookie names alone are not proof.
- Bounded pages, records, comments, payload bytes, continuations, storage inputs, and exports.
- Sequential adaptive pacing, finite retries, terminal rate-limit handling, and a real whole-run deadline.
- Private versioned SQLite storage, idempotent upserts, FTS5 when available, and safe fallback search.
- Partial captures preserve evidence and report gaps instead of inventing empty success.
- CAPTCHA and network challenges are detected and reported, never bypassed.

Reddita is not an official Reddit client. Use it responsibly and comply with applicable laws, Reddit's terms, and community expectations.

## Design references

The design was checked against open-source prior art:

- [8TrafficAI/reddit-skills](https://github.com/8TrafficAI/reddit-skills) — dedicated macOS Chrome, CDP, login doctor, same-origin fetch (MIT)
- [feedgrab](https://github.com/iBigQiang/feedgrab) — hybrid acquisition, paging, retries, `morechildren` expansion (MIT)
- [preaverage/reddit-mcp](https://github.com/preaverage/reddit-mcp) — manual browser login and session refresh (MIT)
- [reddhog](https://github.com/c4pi/reddhog) — warmed persistent anonymous profile (MIT)
- [Stash](https://github.com/arrpollo/Stash) — durable offline library and interrupted-sync preservation (MIT)
- [Apify Dataset](https://docs.apify.com/storage/dataset) — dataset/export ergonomics benchmark; Reddita remains local and private

Closed Apify actors were used only to compare output and coverage claims; no closed or unlicensed code was copied.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
REDDITA_BROWSER_SMOKE=1 python -m unittest tests.test_browser_smoke -v
```

Parser tests use trimmed real-shaped JSON/HTML fixtures. CI runs the full suite, real Chromium smoke test, dependency check, CLI check, and package build on Ubuntu 26.04 x64/ARM64, macOS 26 Apple Silicon/Intel, Windows Server 2025 x64, Windows 11 ARM64, and the latest available Python. Account integration is opt-in because CI must never contain a user's Reddit session.

## License

MIT. See [LICENSE](LICENSE).
