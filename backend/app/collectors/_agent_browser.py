from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

_BINARY = "agent-browser"
_SUBPROCESS_TIMEOUT_SECONDS = 30.0


def parse_eval_json(raw: str) -> Any:
    """`agent-browser eval` prints the JS return value JSON-encoded; since callers'
    scripts already return a JSON.stringify'd string, the CLI output is a JSON
    string literal *containing* JSON — hence the double decode."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def resolve_binary() -> str | None:
    """Resolves the actual native agent-browser binary, bypassing the Windows
    .cmd shim on PATH: subprocess can't exec a .cmd directly without
    shell=True, and shell=True left a hung grandchild process in testing (the
    shim spawns the real .exe as a child of cmd.exe; killing a timed-out
    cmd.exe doesn't kill that grandchild, so the pipe read never returns). On
    Unix, `which` already resolves straight to the native binary (agent-browser's
    own postinstall step optimizes the symlink), so no extra resolution is needed.
    """
    shim = shutil.which(_BINARY)
    if shim is None:
        return None
    if os.name != "nt":
        return shim
    native = Path(shim).parent / "node_modules" / "agent-browser" / "bin" / "agent-browser-win32-x64.exe"
    return str(native) if native.exists() else None


def _read_text_with_retry(path: Path, attempts: int = 6, delay: float = 0.25) -> str:
    """The daemon (see class docstring below) can still hold stdout.txt open
    for a brief moment after the CLI process we ran has exited, which surfaces
    as a Windows sharing violation (WinError 32). It's a transient race, not a
    permanent lock, so a short retry clears it."""
    for _ in range(attempts):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            time.sleep(delay)
    return ""


class AgentBrowserSession:
    """Thin subprocess wrapper around the `agent-browser` CLI, pinned to one
    `--profile`/`--session` pair. Shared by every collector that automates a
    real browser (Amazon, YouTube, ...) instead of calling a data API.

    `--session` isolates concurrent callers onto separate browser
    tabs/daemons — without a distinct session per caller, two collectors
    running at once share one live tab, and one's `eval` can silently read
    the *other's* page. Pin `session` to something caller-unique (e.g. a
    run_id) rather than leaving it at the default.

    `profile` controls login state: a persistent `--profile <path>` directory
    keeps cookies across restarts (needed for Amazon; not needed for YouTube,
    whose comments are public). Pass `None` for an ephemeral, logged-out
    profile.
    """

    def __init__(self, profile: str | None, session: str, request_delay: float = 2.0):
        self.profile = profile
        self.session = session
        self.request_delay = request_delay
        self._browser_opened = False

    def available(self) -> bool:
        return resolve_binary() is not None

    def open(self, url: str) -> None:
        self.run("open", url)
        self._browser_opened = True

    def eval(self, js: str) -> str:
        return self.run("eval", "--stdin", input_text=js)

    def scroll_down(self, pixels: int = 2000) -> None:
        self.run("scroll", "down", str(pixels))

    def press(self, key: str) -> None:
        self.run("press", key)

    def close(self) -> None:
        if self._browser_opened:
            self.run("close")
            self._browser_opened = False

    def run(self, *args: str, input_text: str | None = None) -> str:
        # Deliberately not using capture_output/PIPE for stdout+stderr: agent-browser's
        # first invocation in a session spawns a long-lived background daemon that
        # inherits the pipe write-ends. The daemon outlives the CLI wrapper process,
        # so the pipe's write-end never closes and Python's subprocess.run hangs
        # forever waiting for EOF on it — even past `timeout`, since CPython's
        # timeout handler tries to drain remaining output (with no timeout of its
        # own) before re-raising. Redirecting to real temp files sidesteps this:
        # a file handle held open by the daemon doesn't block a read of the file
        # after the CLI process has exited.
        binary = resolve_binary()
        if binary is None:
            return ""
        if self.request_delay:
            time.sleep(self.request_delay)
        cmd = [binary]
        if self.profile is not None:
            cmd += ["--profile", self.profile]
        cmd += ["--session", self.session, *args]
        # ignore_cleanup_errors: for the same reason as above, the daemon can still
        # hold stdout.txt open when this directory gets torn down, which would
        # otherwise raise WinError 32 out of the `with` block on Windows.
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
                        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                    )
            except (subprocess.TimeoutExpired, OSError):
                return ""
            finally:
                if stdin_handle is not None:
                    stdin_handle.close()
            return _read_text_with_retry(stdout_path)
