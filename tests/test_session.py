from __future__ import annotations

import json
import inspect
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from reddita.session import (
    AccessMode,
    LoginTimeoutError,
    ProfileInUseError,
    ProfileKind,
    ProfileLayout,
    ProfileLock,
    SessionStatus,
    classify_me_response,
    default_profile_root,
    manual_login,
    login_account,
    open_profile_context,
    profile_paths,
    probe_session,
    wait_for_verified_login,
)


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def text(self) -> str:
        return self._body


class _Request:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class _Context:
    def __init__(self, responses: list[_Response]) -> None:
        self.request = _Request(responses)
        self.pages = []
        self.closed = False

    def new_page(self):
        page = _Page(self.request)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _Page:
    def __init__(self, request=None) -> None:
        self.request = request
        self.visited: list[tuple[str, dict]] = []

    def goto(self, url: str, **kwargs) -> None:
        self.visited.append((url, kwargs))


class _Chromium:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.launches: list[tuple[str, dict]] = []

    def launch_persistent_context(self, profile: str, **kwargs):
        self.launches.append((profile, kwargs))
        return self.context


class _Playwright:
    def __init__(self, context: _Context) -> None:
        self.chromium = _Chromium(context)


class _PlaywrightManager:
    def __init__(self, playwright: _Playwright) -> None:
        self.playwright = playwright

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class SessionTests(unittest.TestCase):
    def test_default_profile_root_is_cross_platform(self):
        home = Path("/Users/kay")
        self.assertEqual(
            default_profile_root(system="Darwin", environ={}, home=home),
            home / "Library" / "Application Support" / "Reddita",
        )
        self.assertEqual(
            default_profile_root(
                system="Windows", environ={"LOCALAPPDATA": "C:/Users/kay/AppData/Local"},
                home=Path("C:/Users/kay"),
            ),
            Path("C:/Users/kay/AppData/Local") / "Reddita",
        )
        self.assertEqual(
            default_profile_root(
                system="Linux", environ={"XDG_DATA_HOME": "/data/kay"},
                home=Path("/home/kay"),
            ),
            Path("/data/kay") / "reddita",
        )

    def test_layout_creates_separate_private_owned_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = ProfileLayout(Path(directory) / "state").ensure()

            self.assertEqual(layout.profile(ProfileKind.ANONYMOUS), layout.root / "profiles" / "anonymous")
            self.assertEqual(layout.profile(ProfileKind.ACCOUNT), layout.root / "profiles" / "account")
            self.assertNotEqual(layout.anonymous, layout.account)
            for path in (layout.root, layout.profiles, layout.anonymous, layout.account):
                self.assertTrue(path.is_dir())
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_profile_paths_supports_access_modes_without_sharing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = profile_paths(Path(directory) / "state")

            self.assertEqual(paths[AccessMode.ANONYMOUS].name, "anonymous")
            self.assertEqual(paths[AccessMode.ACCOUNT].name, "account")
            self.assertNotIn(AccessMode.AUTO, paths)

    def test_layout_rejects_symlinked_profile_component(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir()
            root = base / "state"
            root.mkdir()
            (root / "profiles").symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                ProfileLayout(root).ensure()

    def test_profile_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = ProfileLayout(Path(directory) / "state").ensure().anonymous

            with ProfileLock(profile):
                with self.assertRaises(ProfileInUseError):
                    with ProfileLock(profile):
                        self.fail("second owner entered the same profile")

            with ProfileLock(profile):
                self.assertTrue((profile / ".reddita-profile.lock").is_file())

    def test_me_response_classifies_verified_account(self):
        state = classify_me_response(200, json.dumps({"data": {"name": "researcher"}}))

        self.assertEqual(state.status, SessionStatus.AUTHENTICATED)
        self.assertEqual(state.username, "researcher")
        self.assertTrue(state.authenticated)

    def test_me_response_classifies_anonymous_without_inventing_identity(self):
        state = classify_me_response(200, json.dumps({"data": None}))

        self.assertEqual(state.status, SessionStatus.ANONYMOUS)
        self.assertIsNone(state.username)
        self.assertFalse(state.authenticated)

    def test_me_response_distinguishes_challenge_block_and_invalid_json(self):
        challenge = classify_me_response(200, "<title>Reddit - Prove your humanity</title>")
        blocked = classify_me_response(403, "Blocked")
        invalid = classify_me_response(200, "not json")

        self.assertEqual(challenge.status, SessionStatus.CHALLENGED)
        self.assertEqual(blocked.status, SessionStatus.BLOCKED)
        self.assertEqual(invalid.status, SessionStatus.INVALID)

    def test_probe_uses_only_standard_domain_me_endpoint(self):
        context = _Context([_Response(200, '{"data":null}')])

        state = probe_session(context, timeout_ms=4321)

        self.assertEqual(state.status, SessionStatus.ANONYMOUS)
        self.assertEqual(
            context.request.calls,
            [("https://www.reddit.com/api/me.json", {"timeout": 4321})],
        )

    def test_probe_prefers_same_origin_in_page_identity_fetch(self):
        class BrowserPage:
            def evaluate(self, script):
                self.script = script
                return {"status": 200, "body": '{"data":{"name":"browser-user"}}'}

        page = BrowserPage()
        state = probe_session(page)
        self.assertEqual(state.username, "browser-user")
        self.assertIn("fetch('/api/me.json'", page.script)

    def test_probe_reports_transport_failure_without_claiming_anonymous(self):
        class BrokenRequest:
            def get(self, url: str, **kwargs):
                raise RuntimeError("offline")

        context = _Context([])
        context.request = BrokenRequest()

        state = probe_session(context)

        self.assertEqual(state.status, SessionStatus.UNAVAILABLE)
        self.assertFalse(state.authenticated)

    def test_wait_for_login_returns_only_verified_username(self):
        context = _Context([
            _Response(200, '{"data":null}'),
            _Response(200, '{"data":{"name":"kay-test"}}'),
        ])
        sleeps: list[float] = []

        state = wait_for_verified_login(
            context,
            timeout_seconds=5,
            poll_interval=0.25,
            monotonic=iter([0.0, 0.1, 0.2]).__next__,
            sleep=sleeps.append,
        )

        self.assertEqual(state.username, "kay-test")
        self.assertEqual(sleeps, [0.25])

    def test_wait_for_login_times_out_while_anonymous(self):
        context = _Context([
            _Response(200, '{"data":null}'),
            _Response(200, '{"data":null}'),
        ])

        with self.assertRaises(LoginTimeoutError):
            wait_for_verified_login(
                context,
                timeout_seconds=1,
                poll_interval=0,
                monotonic=iter([0.0, 0.5, 1.1]).__next__,
                sleep=lambda _: None,
            )

    def test_manual_login_is_headed_dedicated_and_never_accepts_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = ProfileLayout(Path(directory) / "state").ensure()
            context = _Context([_Response(200, '{"data":{"name":"verified-user"}}')])
            playwright = _Playwright(context)

            state = manual_login(playwright, layout, timeout_seconds=1, poll_interval=0)

            self.assertEqual(state.username, "verified-user")
            profile, options = playwright.chromium.launches[0]
            self.assertEqual(profile, str(layout.account))
            self.assertIs(options["headless"], False)
            self.assertEqual(options["channel"], "chrome")
            self.assertNotIn("storage_state", options)
            self.assertEqual(
                context.pages[0].visited[0][0], "https://www.reddit.com/login/"
            )
            self.assertTrue(context.closed)

    def test_profile_context_holds_lock_until_browser_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = ProfileLayout(Path(directory) / "state").ensure().anonymous
            context = _Context([])
            playwright = _Playwright(context)

            with open_profile_context(playwright, profile, headless=True) as opened:
                self.assertIs(opened, context)
                with self.assertRaises(ProfileInUseError):
                    with ProfileLock(profile):
                        self.fail("profile lock was not held")

            self.assertTrue(context.closed)
            self.assertIs(playwright.chromium.launches[0][1]["headless"], True)

    def test_login_account_public_api_has_no_credential_parameters(self):
        parameters = inspect.signature(login_account).parameters

        self.assertEqual(list(parameters)[:2], ["profile_root", "timeout_seconds"])
        self.assertNotIn("username", parameters)
        self.assertNotIn("password", parameters)
        self.assertNotIn("cookies", parameters)

    def test_login_account_uses_the_selected_account_profile_path_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            account_profile = Path(directory) / "profiles" / "account"
            context = _Context([_Response(200, '{"data":{"name":"verified-user"}}')])
            playwright = _Playwright(context)
            sync_api = ModuleType("patchright.sync_api")
            sync_api.sync_playwright = lambda: _PlaywrightManager(playwright)

            with patch.dict(sys.modules, {"patchright.sync_api": sync_api}):
                state = login_account(account_profile, 1)

            self.assertEqual(state.username, "verified-user")
            self.assertEqual(playwright.chromium.launches[0][0], str(account_profile))


if __name__ == "__main__":
    unittest.main()
