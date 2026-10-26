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

Live read-only verification on Linux with Chromium 156 initially confirmed that `about python` succeeded while old-Reddit research routes landed on `login/?reason=lor2`.

The 2026-10-26 research verification replaced those blocked old-Reddit routes with Reddit's public Atom feeds:

- `search`, `sub`, `subsearch`, `thread`, and `about` all complete live without login;
- a fresh 60-post product query returned 60 unique records with 60 post bodies in 10.97 seconds;
- product search, top-subreddit research, within-subreddit avatar research, and a 10-comment pain-point thread all returned usable JSON;
- identical bounded research captures are cached despite their honest `record-limit` or `comment-limit` gap: a five-post repeat fell from 2.691 seconds to 0.123 seconds with identical output;
- navigation is validated before loading, research captures abort all subresources, and community pages can load only GET assets from `www.redditstatic.com`;
- malformed or incomplete feeds fail closed, a thread feed must begin with its requested post, and a saturated feed is reported as partial rather than falsely complete;
- Atom feeds do not provide vote or comment totals, so Reddita returns those metrics as unavailable rather than inventing zeroes.
- 26 local tests pass, including malformed XML, invalid second pages, comment-only threads, feed saturation, URL boundaries, cache reuse, and strict CLI input.
