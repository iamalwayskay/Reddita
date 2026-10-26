# State

Reddita is a standalone public repository derived from the original archived Reddit scraper, with personal paths and identifiers removed.

The CLI preserves the original working acquisition model: an unauthenticated persistent browser profile, human-paced pagination, old-Reddit HTML parsing, a six-hour private cache, and JSON output. The profile is Reddita-owned and never imports personal cookies or credentials. Browser writes are blocked.

Verified on 2026-10-22:

- all 10 unit tests pass;
- package compilation and dependency checks pass;
- a fresh live `about python --json` capture succeeds;
- the following cached capture returns the same record.

Current upstream constraint: Reddit redirects some logged-out listing, search, and thread routes to its login page on this network. Reddita rejects that redirect and returns an explicit error rather than a misleading empty result.

Updated on 2026-10-26 after testing a fresh Apple Silicon macOS install:

- browser user agents now match the host OS and Patchright Chromium major version, avoiding a contradictory Linux/Chrome 131 fingerprint on macOS;
- redirect-denied errors for changed Reddit pages include the landed-on URL, which exposes login-wall redirects without bypassing them;
- CLI tests create fixtures below the user's private home directory so the production cache-ancestry guard remains strict while the suite also works under user-owned writable temporary roots.

Live read-only verification on Linux with Chromium 156: `about python` succeeds; `search "bunion pain"` is redirected to `old.reddit.com/login/?reason=lor2` and Reddita reports that destination.
