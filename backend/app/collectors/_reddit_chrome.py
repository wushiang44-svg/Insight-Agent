from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import psutil
import requests

from . import _agent_browser as _ab

_STATE_FILE_NAME = "reddit_collector_state.json"
_LOCAL_STATE_FILE_NAME = "Local State"  # written by Chrome itself on first launch, before any navigation
_LAUNCH_READY_TIMEOUT_SECONDS = 15.0
_SHUTDOWN_POLL_TIMEOUT_SECONDS = 5.0


class ChromeLifecycleError(RuntimeError):
    """Raised for infrastructure failures: Chrome missing, CDP unreachable, or
    process identification too ambiguous to safely act on. Never raised for a
    Reddit challenge/block -- that's a normal collector return, not this."""


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A snapshot of the dedicated Chrome process, captured once and always
    re-verified (pid + create_time together, never pid alone) before any
    action that could affect the OS process -- defends against PID reuse
    after the original process has already died."""

    pid: int
    create_time: float
    cmdline_snapshot: tuple[str, ...]
    cdp_port: int


def locate_chrome_executable() -> Path:
    """Resolves the real Chrome binary. `REDDIT_CHROME_EXECUTABLE` overrides;
    otherwise checks the standard Windows install locations."""
    override = os.environ.get("REDDIT_CHROME_EXECUTABLE", "").strip()
    if override:
        path = Path(override)
        if path.exists():
            return path
        raise ChromeLifecycleError(f"REDDIT_CHROME_EXECUTABLE is set to {override!r}, but that path does not exist.")

    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    checked = ", ".join(str(c) for c in candidates)
    raise ChromeLifecycleError(
        f"Could not locate Chrome. Checked: {checked}. Set REDDIT_CHROME_EXECUTABLE to override."
    )


def _normalize_dir(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _extract_flag_value(cmdline: list[str], flag: str) -> str | None:
    """Chrome accepts `--flag=value` as a single argv token (sometimes
    quoted); this only matches that exact shape, never a loose substring."""
    prefix = f"{flag}="
    for arg in cmdline:
        stripped = arg.strip('"')
        if stripped.startswith(prefix):
            return stripped[len(prefix) :]
    return None


def find_dedicated_instance(profile_dir: Path, cdp_port: int) -> ProcessIdentity | None:
    """Scans running chrome.exe processes for the one exact process matching
    both --remote-debugging-port=<cdp_port> and --user-data-dir=<profile_dir>
    (exact directory match, not a substring). Zero matches -> None. More than
    one match -> raise rather than guess among candidates.

    Chrome's own child processes (renderer/gpu-process/utility/crashpad-handler)
    inherit both of those same flags from the main browser process, so they'd
    otherwise all "match" too and trip the ambiguity guard on every single
    launch -- excluded here via their `--type=...` flag, which only ever
    appears on child processes, never on the main browser process."""
    target_dir = _normalize_dir(str(profile_dir))
    matches: list[ProcessIdentity] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            if (info.get("name") or "").lower() != "chrome.exe":
                continue
            cmdline = info.get("cmdline") or []
            if any(arg.strip('"').startswith("--type=") for arg in cmdline):
                continue  # a child process, not the main browser process
            port_value = _extract_flag_value(cmdline, "--remote-debugging-port")
            dir_value = _extract_flag_value(cmdline, "--user-data-dir")
            if port_value != str(cdp_port):
                continue
            if dir_value is None or _normalize_dir(dir_value) != target_dir:
                continue
            matches.append(
                ProcessIdentity(
                    pid=info["pid"],
                    create_time=info["create_time"],
                    cmdline_snapshot=tuple(cmdline),
                    cdp_port=cdp_port,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not matches:
        return None
    if len(matches) > 1:
        pids = [m.pid for m in matches]
        raise ChromeLifecycleError(
            f"Found {len(matches)} chrome.exe processes matching profile {profile_dir} on port "
            f"{cdp_port} (pids {pids}) -- refusing to guess which one is authoritative. "
            "Manual intervention required."
        )
    return matches[0]


def _cdp_probe(cdp_port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        resp = requests.get(f"http://localhost:{cdp_port}/json/version", timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def ensure_running(profile_dir: Path, cdp_port: int) -> tuple[ProcessIdentity, bool]:
    """Reuses an already-running dedicated instance if one exists and its CDP
    endpoint is live; otherwise launches a fresh, deliberately plain Chrome
    process (no --headless, no automation-marker flags) and waits for CDP
    readiness. Never launches via agent-browser's own open()/--profile path
    -- that's the path proven to trigger Reddit's challenge.

    Returns (identity, reused): reused=True means an already-running
    instance was attached to; False means this call just launched a fresh
    process. `reused` is call-specific (a fact about what THIS call did), not
    a property of the process itself, so it's returned alongside
    ProcessIdentity rather than added as a field on it."""
    existing = find_dedicated_instance(profile_dir, cdp_port)
    if existing is not None and _cdp_probe(cdp_port) is not None:
        return existing, True

    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome = locate_chrome_executable()
    cmd = [
        str(chrome),
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    deadline = time.monotonic() + _LAUNCH_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _cdp_probe(cdp_port) is not None:
            identity = find_dedicated_instance(profile_dir, cdp_port)
            if identity is not None:
                return identity, False
        time.sleep(0.5)
    raise ChromeLifecycleError(
        f"Chrome did not become ready on CDP port {cdp_port} within {_LAUNCH_READY_TIMEOUT_SECONDS}s."
    )


def shutdown(identity: ProcessIdentity) -> bool:
    """Explicit, administrative-only operation -- never called automatically
    per-run (see ensure_running's reuse-by-default behavior). Re-verifies the
    identity against a fresh scan before touching anything; never trusts
    agent-browser's own CDP close output as proof of anything."""
    try:
        proc = psutil.Process(identity.pid)
        if proc.create_time() != identity.create_time:
            return True  # a different process now holds this pid -- original is already gone
    except psutil.NoSuchProcess:
        return True

    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return True

    deadline = time.monotonic() + _SHUTDOWN_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not proc.is_running():
            break
        time.sleep(0.25)

    if proc.is_running():
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        deadline = time.monotonic() + _SHUTDOWN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline and proc.is_running():
            time.sleep(0.25)

    profile_dir_value = _extract_flag_value(list(identity.cmdline_snapshot), "--user-data-dir")
    still_running = proc.is_running()
    still_matched = (
        find_dedicated_instance(Path(profile_dir_value), identity.cdp_port) if profile_dir_value else None
    )
    cdp_alive = _cdp_probe(identity.cdp_port) is not None
    return not still_running and still_matched is None and not cdp_alive


def _parse_eval_result(raw: str) -> Any:
    """`_agent_browser.parse_eval_json` is tuned for JS payloads that already
    JSON.stringify their own return value (an object/array) -- it always
    attempts a second decode, and discards the result as None if that second
    decode fails. That's wrong for a JS expression that returns a plain
    string (e.g. `document.title`, `location.href`): the CLI JSON-encodes it
    once, the second decode attempt then fails (a bare string isn't valid
    JSON on its own), and the real value gets silently thrown away -- this
    caused a real false-positive "challenge detected" in the first live smoke
    test (title/body both collapsed to empty, tripping the empty-title
    defense-in-depth check). Fixed here: decode once; a second decode is a
    bonus only, and on failure the first-decoded value is kept, never
    discarded to None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class CdpSession:
    """Mirrors `_agent_browser.py`'s `AgentBrowserSession.run()` safe-subprocess
    pattern (native-binary resolution, temp-file I/O instead of pipes, retry-on-read
    -- all reused directly from `_agent_browser` rather than reimplemented) but
    attaches via `--cdp <port>` to an externally-managed Chrome instead of letting
    agent-browser launch/own its own browser. `AgentBrowserSession` itself can't be
    reused as-is here: its constructor is hardwired to `--session`/`--profile`
    launch mode, which is exactly the mode proven to trigger Reddit's challenge."""

    def __init__(self, cdp_port: int, request_delay: float = 2.0):
        self.cdp_port = cdp_port
        self.request_delay = request_delay

    def open(self, url: str) -> None:
        self.run("open", url)

    def wait_networkidle(self, timeout: float = 15.0) -> None:
        self.run("wait", "--load", "networkidle", timeout=timeout)

    def eval(self, js: str) -> str:
        return self.run("eval", "--stdin", input_text=js)

    def eval_json(self, js: str) -> Any:
        return _parse_eval_result(self.eval(js))

    def click(self, selector: str) -> str:
        return self.run("click", selector)

    def close(self) -> None:
        self.run("close")

    def run(self, *args: str, input_text: str | None = None, timeout: float = 30.0) -> str:
        binary = _ab.resolve_binary()
        if binary is None:
            raise ChromeLifecycleError("agent-browser binary not found on PATH.")
        if self.request_delay:
            time.sleep(self.request_delay)
        cmd = [binary, "--cdp", str(self.cdp_port), *args]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            tmp = Path(tmp_dir)
            stdout_path = tmp / "stdout.txt"
            stdin_handle = None
            if input_text is not None:
                stdin_path = tmp / "stdin.txt"
                stdin_path.write_text(input_text, encoding="utf-8")
                stdin_handle = open(stdin_path, "r", encoding="utf-8")
            try:
                with open(stdout_path, "w", encoding="utf-8", errors="replace") as stdout_file:
                    subprocess.run(
                        cmd,
                        stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=subprocess.DEVNULL,
                        timeout=timeout,
                    )
            except (subprocess.TimeoutExpired, OSError):
                return ""
            finally:
                if stdin_handle is not None:
                    stdin_handle.close()
            return _ab._read_text_with_retry(stdout_path)


class ProfileStatus(str, Enum):
    NOT_INITIALIZED = "not_initialized"
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    CHALLENGED = "challenged"
    UNAVAILABLE = "unavailable"


def _state_path(profile_dir: Path) -> Path:
    return profile_dir / _STATE_FILE_NAME


def read_profile_state(profile_dir: Path) -> dict[str, Any]:
    path = _state_path(profile_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_profile_state(profile_dir: Path, **updates: Any) -> None:
    """Additive-only: merges `updates` into whatever's already recorded. Our
    own metadata is observed history, never the source of truth for whether
    Reddit session trust actually exists on disk (Chrome's own profile data
    is)."""
    state = read_profile_state(profile_dir)
    state.update(updates)
    _state_path(profile_dir).write_text(json.dumps(state, indent=2), encoding="utf-8")


def profile_status(profile_dir: Path) -> ProfileStatus:
    """NOT_INITIALIZED / UNKNOWN / HEALTHY / CHALLENGED, in that order of
    precedence. UNKNOWN specifically covers a profile with real Chrome state
    (e.g. reddit_cdp_probe_profile) that predates our own metadata file --
    such a profile is adopted via one health attempt, never re-initialized."""
    if not (profile_dir / _LOCAL_STATE_FILE_NAME).exists():
        return ProfileStatus.NOT_INITIALIZED
    state = read_profile_state(profile_dir)
    if not state:
        return ProfileStatus.UNKNOWN
    if state.get("last_challenge_at") and (
        not state.get("last_success_at") or state["last_challenge_at"] > state["last_success_at"]
    ):
        return ProfileStatus.CHALLENGED
    return ProfileStatus.HEALTHY
