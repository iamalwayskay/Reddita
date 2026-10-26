# Reddita

A read-only Reddit research tool for humans and AI agents. It searches public posts, reads subreddit listings and threads, and returns community metadata without an API key, Reddit app, or login. It uses Reddit's public Atom feeds for research captures, carries forward the original scraper's private persistent browser profile and polite pacing, and caches repeat captures for six hours.

It is deliberately read-only: the browser permits GET requests and aborts state-changing methods. Main navigation is HTTPS and must end on the requested Reddit page. Posting, voting, messaging, authentication, and private content are unsupported.

## Install

Requires Python 3.12+.

On a clean Mac, install Python first. With Homebrew:

```bash
brew install python@3.12
```

Without Homebrew, use the Python 3.12 macOS installer from [python.org](https://www.python.org/downloads/macos/).

```bash
git clone https://github.com/iamalwayskay/Reddita.git
cd Reddita
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
patchright install chromium
```

## Use

```bash
reddita search "local AI tools" 10
reddita sub python 15 --new
reddita subsearch LocalLLaMA "quantization" 20 --top
reddita thread https://www.reddit.com/r/python/comments/abc123/example/ 25
reddita about python
```

Add `--json` for the stable results-only machine output. For research agents, prefer `--json-meta`: its top level reports completeness, gaps, acquisition time/mode, provenance, and a `results` payload. Always inspect `complete` and `gaps`. Add `--fresh` to bypass the private six-hour cache.

```bash
reddita search "open source OCR" 10 --json-meta > results.json
```

The command exits non-zero on invalid input, a denied network boundary, a browser failure, or an incomplete empty response. Reddit can change its HTML, return `403 Blocked`, or rate-limit unauthenticated requests by network/IP; the tool reports that rather than bypassing it. Treat captures as research evidence, not a guaranteed API.

Listing commands return up to 100 posts and automatically fetch the required 25-item feed pages. Thread commands return the post and up to 99 comments. Atom feeds provide titles, authors, subreddit, timestamps, permalinks, and post/comment text, but not vote or comment totals; those fields are `null` in JSON and `?` in human output. Reddit's chosen sort order is preserved.

## Give it to Claude, GPT, or another coding agent

Share the repository URL and this prompt:

> Install this repository following its README. Use `reddita ... --json-meta` for public Reddit research, inspect `complete` and `gaps`, then analyze `results`. Let Reddita manage its own unauthenticated browser profile; never import a personal profile or add credentials, login automation, posting, voting, or private-content access. When integrating it, prefer the CLI for subprocess use or the documented Python API for in-process use.

The repository also includes [docs/agent-guide.md](docs/agent-guide.md) and an `AGENTS.md` instruction file that coding agents discover automatically.

## Python API

```python
import json
from pathlib import Path

from reddita import SourceBudgets, SourceRequest, acquire_reddit

request = SourceRequest(
    source="reddit",
    operation="search",
    locator="local AI tools",
    parameters_json=json.dumps({
        "pageSize": 10,
        "maxComments": 100,
        "sort": "relevance",
    }),
    max_records=10,
    max_pages=1,
)

capture = acquire_reddit(
    request,
    budgets=SourceBudgets(),
    settings_digest="0" * 64,
    profile_root=Path.home() / ".local" / "share" / "reddita" / "browser-profile",
    native_transport=True,
    allow_stealth=True,
)

for record in capture.result.records:
    print(json.loads(record.data_json))
```

`settings_digest` identifies the caller's effective settings for cache correctness. Use a stable SHA-256 hex digest in production. This minimal Python example is uncached because it does not pass a private cache descriptor. The CLI is the recommended interface because it creates and validates the private profile and cache directories automatically and caches repeat captures for six hours.

## Output model

- `search`, `sub`, and `subsearch` return post records, including available post text.
- `thread` returns one thread record followed by bounded comment records.
- `about` returns one community record.
- Every record carries a stable ID, source index, canonical source URL, and canonical JSON payload.
- Capture metadata records completeness, pages, typed gaps, continuation state, and acquisition time.

## Safety and scope

- Main navigation is restricted to the exact requested public `www.reddit.com` page. Research feeds load no subresources; rendered community pages may load GET-only assets from `www.redditstatic.com`, but not Reddit API, JSON, account, message, or login routes.
- The CLI reuses an unauthenticated profile at `~/.local/share/reddita/browser-profile/`; profile and cache directories are private and reject symlinks.
- The browser may load GET resources referenced by Reddit, but aborts POST and every other state-changing method.
- Captured HTML, pages, records, comments, continuation state, and cache entries are bounded.
- Cache directories and files reject symlinks and unsafe permissions.

This is not an official Reddit client. Reddita uses Reddit's public Atom feeds because logged-out old-Reddit listing/search/thread routes are commonly redirected to login. If Reddit withdraws a public feed or returns an access wall, Reddita reports the failure instead of bypassing it or returning fake empty results.

Use the tool responsibly and comply with applicable laws, Reddit's terms, and community expectations.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

The parser tests use local Atom/HTML fixtures and mocked Reddit acquisition; they do not depend on Reddit being reachable. CI additionally installs and launches real Chromium on Linux and an Apple Silicon macOS runner.

## License

MIT. See [LICENSE](LICENSE).
