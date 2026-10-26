"""Owned browser profiles and read-only Reddit session status probes.

This module never accepts Reddit credentials. Account login is completed by the
human in a headed, Reddita-owned Chrome profile and is considered successful
only when Reddit's ``/api/me.json`` returns a username.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import platform
import stat
import time
from typing import Callable, Iterator, Mapping


ME_URL = "https://www.reddit.com/api/me.json"
LOGIN_URL = "https://www.reddit.com/login/"
MAX_PROBE_BODY_BYTES = 262_144


class AccessMode(str, Enum):
    AUTO = "auto"
    ANONYMOUS = "anonymous"
    ACCOUNT = "account"


class ProfileKind(str, Enum):
    ANONYMOUS = "anonymous"
    ACCOUNT = "account"


class SessionStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    ANONYMOUS = "anonymous"
    CHALLENGED = "challenged"
    BLOCKED = "blocked"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SessionState:
    status: SessionStatus
    username: str | None = None
    http_status: int | None = None
    detail: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.status is SessionStatus.AUTHENTICATED and self.username is not None

    @property
    def identity_known(self) -> bool:
        return self.status in {SessionStatus.AUTHENTICATED, SessionStatus.ANONYMOUS}


class ProfileInUseError(RuntimeError):
    """Raised when another browser owns the selected persistent profile."""


class LoginTimeoutError(TimeoutError):
    """Raised when manual login did not produce a verified Reddit username."""


def default_profile_root(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return Reddita's app-owned data directory for the current platform."""

    system = system or platform.system()
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Reddita"
    if system == "Windows":
        base = environ.get("LOCALAPPDATA") or environ.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "Reddita"
    base = environ.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / "reddita"


@dataclass(frozen=True)
class ProfileLayout:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser())

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    @property
    def anonymous(self) -> Path:
        return self.profiles / ProfileKind.ANONYMOUS.value

    @property
    def account(self) -> Path:
        return self.profiles / ProfileKind.ACCOUNT.value

    def profile(self, kind: ProfileKind | AccessMode | str) -> Path:
        try:
            value = kind.value if isinstance(kind, Enum) else str(kind)
            profile_kind = ProfileKind(value)
        except ValueError as error:
            raise ValueError("auto access mode does not identify one browser profile") from error
        return self.anonymous if profile_kind is ProfileKind.ANONYMOUS else self.account

    def ensure(self) -> ProfileLayout:
        for path in (self.root, self.profiles, self.anonymous, self.account):
            _ensure_private_directory(path)
        return self


def profile_paths(root: Path | None = None) -> dict[AccessMode, Path]:
    layout = ProfileLayout(root or default_profile_root()).ensure()
    return {
        AccessMode.ANONYMOUS: layout.anonymous,
        AccessMode.ACCOUNT: layout.account,
    }


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"profile path cannot be a symlink: {path}")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError as error:
        raise ValueError(f"profile path is not a directory: {path}") from error
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError(f"profile path is not a private directory: {path}")
    if os.name != "nt":
        path.chmod(0o700)
        if path.stat(follow_symlinks=False).st_mode & 0o077:
            raise ValueError(f"profile path permissions are not private: {path}")


class ProfileLock:
    """Cross-platform advisory lock held for one browser profile's lifetime."""

    def __init__(self, profile: Path) -> None:
        self.profile = Path(profile)
        self.path = self.profile / ".reddita-profile.lock"
        self._file = None

    def __enter__(self) -> ProfileLock:
        _ensure_private_directory(self.profile)
        if self.path.is_symlink():
            raise ValueError("profile lock cannot be a symlink")
        handle = self.path.open("a+b")
        if os.name != "nt":
            os.fchmod(handle.fileno(), 0o600)
        try:
            _lock_file(handle)
        except (BlockingIOError, OSError) as error:
            handle.close()
            raise ProfileInUseError(f"browser profile is already in use: {self.profile}") from error
        self._file = handle
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._file is None:
            return
        try:
            _unlock_file(self._file)
        finally:
            self._file.close()
            self._file = None


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def classify_me_response(status_code: int, body: str | bytes) -> SessionState:
    """Classify Reddit identity without treating a block as logged-out state."""

    if isinstance(body, bytes):
        raw = body[:MAX_PROBE_BODY_BYTES].decode("utf-8", errors="replace")
    else:
        raw = body.encode("utf-8")[:MAX_PROBE_BODY_BYTES].decode("utf-8", errors="replace")
    lowered = raw.casefold()
    if "prove your humanity" in lowered or "challenge-platform" in lowered:
        return SessionState(SessionStatus.CHALLENGED, http_status=status_code)
    if status_code in {403, 429}:
        return SessionState(SessionStatus.BLOCKED, http_status=status_code)
    if status_code == 401:
        return SessionState(SessionStatus.ANONYMOUS, http_status=status_code)
    if status_code >= 500:
        return SessionState(SessionStatus.UNAVAILABLE, http_status=status_code)
    if status_code != 200:
        return SessionState(SessionStatus.INVALID, http_status=status_code)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return SessionState(SessionStatus.INVALID, http_status=status_code)
    if not isinstance(payload, dict) or "data" not in payload:
        return SessionState(SessionStatus.INVALID, http_status=status_code)
    data = payload["data"]
    if data is None or data == {}:
        return SessionState(SessionStatus.ANONYMOUS, http_status=status_code)
    if not isinstance(data, dict):
        return SessionState(SessionStatus.INVALID, http_status=status_code)
    username = data.get("name")
    if isinstance(username, str) and username.strip() and len(username.strip()) <= 128:
        return SessionState(
            SessionStatus.AUTHENTICATED,
            username=username.strip(),
            http_status=status_code,
        )
    return SessionState(SessionStatus.INVALID, http_status=status_code)


def probe_session(page_or_context, *, timeout_ms: int = 10_000) -> SessionState:
    """Read Reddit's own identity endpoint through a browser cookie jar."""

    evaluate = getattr(page_or_context, "evaluate", None)
    if callable(evaluate):
        try:
            result = evaluate(
                """async () => {
                  const response = await fetch('/api/me.json', {credentials: 'include'});
                  return {status: response.status, body: (await response.text()).slice(0, 262145)};
                }"""
            )
            if isinstance(result, dict) and type(result.get("status")) is int:
                return classify_me_response(result["status"], result.get("body", ""))
        except Exception as error:
            return SessionState(
                SessionStatus.UNAVAILABLE,
                detail=f"in-page identity probe failed: {type(error).__name__}",
            )

    request = getattr(page_or_context, "request", None)
    if request is None:
        return SessionState(SessionStatus.UNAVAILABLE, detail="browser request context unavailable")
    response = None
    try:
        response = request.get(ME_URL, timeout=timeout_ms)
        return classify_me_response(int(response.status), response.text())
    except Exception as error:
        return SessionState(
            SessionStatus.UNAVAILABLE,
            detail=f"identity probe failed: {type(error).__name__}",
        )
    finally:
        dispose = getattr(response, "dispose", None)
        if callable(dispose):
            try:
                dispose()
            except Exception:
                pass


def wait_for_verified_login(
    page_or_context,
    *,
    timeout_seconds: float = 600,
    poll_interval: float = 1,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SessionState:
    """Wait until Reddit verifies a username; anonymous/blocked is never success."""

    if timeout_seconds <= 0 or poll_interval < 0:
        raise ValueError("login timeout and polling interval must be positive")
    deadline = monotonic() + timeout_seconds
    last_state = SessionState(SessionStatus.UNAVAILABLE)
    while True:
        last_state = probe_session(page_or_context)
        if last_state.authenticated:
            return last_state
        if monotonic() >= deadline:
            raise LoginTimeoutError(
                f"Reddit login was not verified before timeout ({last_state.status.value})"
            )
        sleep(poll_interval)


@contextmanager
def open_profile_context(
    playwright,
    profile: Path,
    *,
    headless: bool,
    channel: str | None = None,
) -> Iterator[object]:
    """Open one owned profile while holding its exclusive lock."""

    profile = Path(profile)
    with ProfileLock(profile):
        options: dict[str, object] = {"headless": headless}
        if channel is not None:
            options["channel"] = channel
        if not headless and platform.system() == "Linux" and not os.environ.get("DISPLAY"):
            runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            wayland = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
            if (Path(runtime) / wayland).exists():
                options["args"] = ["--ozone-platform=wayland"]
                options["env"] = {
                    **os.environ, "XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": wayland,
                }
        context = playwright.chromium.launch_persistent_context(str(profile), **options)
        try:
            yield context
        finally:
            context.close()


def manual_login(
    playwright,
    layout: ProfileLayout,
    *,
    timeout_seconds: float = 600,
    poll_interval: float = 1,
) -> SessionState:
    """Open a headed account profile and wait for the human to finish login."""

    layout.ensure()
    return _manual_login_profile(
        playwright,
        layout.account,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )


def _manual_login_profile(
    playwright,
    account_profile: Path,
    *,
    timeout_seconds: float,
    poll_interval: float = 1,
) -> SessionState:
    with open_profile_context(
        playwright, account_profile, headless=False, channel="chrome"
    ) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        return wait_for_verified_login(
            page,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )


def login_account(profile_root: Path, timeout_seconds: float = 600) -> SessionState:
    """Log in through the selected account profile without receiving credentials."""

    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return _manual_login_profile(
            playwright,
            Path(profile_root),
            timeout_seconds=timeout_seconds,
        )


def inspect_profile(profile_root: Path) -> SessionState:
    """Probe one owned profile without exposing or copying session material."""

    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        with open_profile_context(
            playwright, Path(profile_root), headless=True, channel="chrome",
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=60_000)
            return probe_session(page)
