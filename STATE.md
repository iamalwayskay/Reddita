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
- 33 local tests pass with the browser smoke enabled, including malformed XML, invalid and HTTP-failing second pages, comment-only threads, feed saturation, external-link extraction, provenance, URL boundaries, cache reuse, and strict CLI input.

The 2026-10-26 adversarial Mac/Claude pass added Apple Silicon CI with a real persistent Chromium launch, made the README executable on a clean Mac, added `--json-meta` completeness and typed gaps without breaking legacy `--json`, preserved outbound link-post URLs and thread-comment provenance, and stopped converting unavailable community activity into zero.

The 2026-10-26 session-aware research release replaces the RSS-only product boundary with four explicit routes: `auto`, `web-account`, `web-anon`, and `feed`. Account and anonymous browsers have separate private, locked Reddita-owned profiles. Account login is human-driven in headed Chrome and is accepted only after `/api/me.json` returns a username; credentials and cookie values never enter Reddita.

Standard-domain capture now tries same-origin JSON, recursively parses comments, expands bounded `morechildren` batches, and falls back to current Shreddit DOM (including the live `search-sdui-post` cards). `auto` uses feeds when rendered coverage is shorter, and metadata reports requested mode, actual access mode, transport, session state, completeness, and typed gaps.

A versioned private SQLite research corpus now provides idempotent stable-identity upserts, capture/query provenance, FTS5 with a portable fallback, local search, and atomic JSONL export. CLI commands are `--save`, `saved`, and `export`; the same APIs are public from `reddita`.

Verification on 2026-10-26:

- 82 unit/integration tests pass, plus the opt-in real persistent Chromium smoke test;
- live logged-out standard-domain search for `cat joint supplement` returned seven scored posts through modern DOM;
- its selected CatAdvice thread returned the post and 15 nested comments with parent/depth relationships and an honest comment-coverage gap;
- live `auto` research for `neuropathy foot cream` selected Atom after short web coverage, returned and saved 10 posts, found six saved records for `cream`, and exported 10 private JSONL rows;
- the Linux headed-login path now selects native Wayland rather than requiring Xorg; macOS continues to use installed Chrome.

Open acceptance item: an authenticated live capture requires the human to complete the one-time login in the dedicated Chrome window. The code and tests fail closed until Reddit verifies the username.

The final adversarial pass moved identity and JSON acquisition into the loaded page's same-origin `fetch`, fixed structured timestamps, enforces one time/request/byte/cancellation budget across every fallback, queues newly exposed `morechildren` generations, uses installed Chrome's native identity consistently for the account profile, makes feed fallback ephemeral and lock-safe, validates invariant landing-page query parameters, migrates corpus schemas v1/v2→v3, and exports append-only query provenance without overwriting repeated research contexts.

Cross-platform verification on 2026-10-26 now runs seven clean GitHub-hosted environments: Ubuntu 26.04 x64 and ARM64, macOS 26 on Apple Silicon and Intel, Windows Server 2025 x64, Windows 11 ARM64, and Ubuntu 26.04 with the latest available Python 3.x. Every job installs Reddita and Chromium from scratch, runs all 82 tests with the real browser smoke enabled, checks dependencies and CLI startup, and builds both wheel and source distributions.

The first Windows runs exposed and the verified fix closes POSIX-only cache-descriptor assumptions, SQLite handles left open by tests, host-specific path separators and permission-bit assertions, and implicit fixture decoding. Windows keeps the same private-cache guarantees available on that platform by rejecting symlinks and junctions before using the cache path; POSIX retains descriptor-pinned cache access and strict mode-bit validation.

The 2026-10-26 capacity pass adds an explicit bounded `--extended` mode for up to 2,500 listing posts or 1,000 thread comments. It improves sustained throughput by caching web captures (with account caches namespaced by the identity verified during capture), reusing one rendered session across successful JSON pagination, pacing web data requests and extended feeds, applying at most two bounded transient retries, honoring `Retry-After`, enforcing a whole-run feed deadline, and treating exhausted HTTP 429 responses as terminal `rate-limited` evidence rather than triggering more access modes. Live testing progressed from 25 to 50 records under an already active Reddit throttle, then exposed and fixed a separate live-pagination bug so malformed later Atom pages preserve earlier evidence instead of aborting it. The expanded suite contains 101 tests including the real Chromium smoke test.

The LLM-facing contract is `--agent`: one compact `reddita.agent.v1` JSON document, quiet stderr, explicit `ok`/`complete`/`nextAction`, typed gaps, and structured retryable or terminal errors. This is the intended automation path; agents do not parse the human CLI presentation.
