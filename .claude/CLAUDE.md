# Reddita repository instructions

This is a standalone, read-only public Reddit research scraper.

- Preserve the public command: `reddita`.
- Preserve the supported operations: `search`, `sub`, `subsearch`, `thread`, and `about`.
- Preserve separate Reddita-owned anonymous and account profiles. The account profile may be authenticated only by the human in a headed browser; never accept credentials, import an everyday browser profile, or expose cookies/session material.
- Support explicit `auto`, `web-account`, `web-anon`, and `feed` research modes. Verify account identity before account-mode capture, keep cache identities separate, and report the transport actually used.
- Login exists only to establish the dedicated read-only research session. Never add posting, voting, messaging, moderation, or private-content access.
- Never weaken the GET-only browser policy, final Reddit-page validation, private profile/cache checks, or finite capture limits.
- Add local fixture tests for parser or output changes; tests must not require live Reddit.
- Keep stdout machine-readable under `--json`; diagnostics belong on stderr with a non-zero exit.
- Do not add dependencies unless the existing standard library, Pydantic, Beautiful Soup, lxml, or Patchright cannot solve the problem.
- Run `python -m unittest discover -s tests -v` before claiming completion.

Start with `README.md`, then `docs/agent-guide.md`. The package lives in `src/reddita/`.
