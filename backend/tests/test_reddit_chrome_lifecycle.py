from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.collectors import _reddit_chrome as rc


class FakeIterProcess:
    """Stands in for a psutil.Process yielded by process_iter(attrs=...) -- .info
    is what process_iter's attrs argument actually returns."""

    def __init__(self, pid: int, name: str, cmdline: list[str], create_time: float):
        self.info = {"pid": pid, "name": name, "cmdline": cmdline, "create_time": create_time}


def _chrome_cmdline(port: int, profile_dir: str) -> list[str]:
    return [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
    ]


def test_find_dedicated_instance_exact_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "reddit-profile"
    fake = FakeIterProcess(1234, "chrome.exe", _chrome_cmdline(9222, str(profile_dir)), create_time=1000.0)
    monkeypatch.setattr(rc.psutil, "process_iter", lambda attrs: [fake])

    identity = rc.find_dedicated_instance(profile_dir, 9222)

    assert identity is not None
    assert identity.pid == 1234
    assert identity.create_time == 1000.0
    assert identity.cdp_port == 9222


def test_find_dedicated_instance_no_match_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "reddit-profile"
    other = FakeIterProcess(999, "chrome.exe", _chrome_cmdline(9222, str(tmp_path / "some-other-profile")), 500.0)
    monkeypatch.setattr(rc.psutil, "process_iter", lambda attrs: [other])

    assert rc.find_dedicated_instance(profile_dir, 9222) is None


def test_find_dedicated_instance_ignores_wrong_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "reddit-profile"
    wrong_port = FakeIterProcess(1, "chrome.exe", _chrome_cmdline(9999, str(profile_dir)), 1.0)
    monkeypatch.setattr(rc.psutil, "process_iter", lambda attrs: [wrong_port])

    assert rc.find_dedicated_instance(profile_dir, 9222) is None


def test_find_dedicated_instance_ignores_non_chrome_processes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "reddit-profile"
    not_chrome = FakeIterProcess(1, "notepad.exe", _chrome_cmdline(9222, str(profile_dir)), 1.0)
    monkeypatch.setattr(rc.psutil, "process_iter", lambda attrs: [not_chrome])

    assert rc.find_dedicated_instance(profile_dir, 9222) is None


def test_find_dedicated_instance_ambiguous_matches_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fail safely rather than guess among multiple candidates -- required by
    the plan's process-identification safety design."""
    profile_dir = tmp_path / "reddit-profile"
    dupe_a = FakeIterProcess(11, "chrome.exe", _chrome_cmdline(9222, str(profile_dir)), 1.0)
    dupe_b = FakeIterProcess(22, "chrome.exe", _chrome_cmdline(9222, str(profile_dir)), 2.0)
    monkeypatch.setattr(rc.psutil, "process_iter", lambda attrs: [dupe_a, dupe_b])

    with pytest.raises(rc.ChromeLifecycleError):
        rc.find_dedicated_instance(profile_dir, 9222)


def test_find_dedicated_instance_ignores_child_processes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression test for a real bug caught by the live smoke test: Chrome's
    own child processes (renderer/gpu-process/crashpad-handler/utility)
    inherit the same --user-data-dir and --remote-debugging-port flags as the
    main browser process, so without filtering them out, a single real
    Chrome launch would look like 8 "ambiguous" matches and always raise."""
    profile_dir = tmp_path / "reddit-profile"
    main_process = FakeIterProcess(100, "chrome.exe", _chrome_cmdline(9222, str(profile_dir)), 1.0)
    renderer_child = FakeIterProcess(
        101, "chrome.exe", ["--type=renderer", *_chrome_cmdline(9222, str(profile_dir))], 1.1
    )
    gpu_child = FakeIterProcess(102, "chrome.exe", ["--type=gpu-process", *_chrome_cmdline(9222, str(profile_dir))], 1.2)
    crashpad_child = FakeIterProcess(
        103, "chrome.exe", ["--type=crashpad-handler", *_chrome_cmdline(9222, str(profile_dir))], 1.3
    )
    monkeypatch.setattr(
        rc.psutil, "process_iter", lambda attrs: [main_process, renderer_child, gpu_child, crashpad_child]
    )

    identity = rc.find_dedicated_instance(profile_dir, 9222)

    assert identity is not None
    assert identity.pid == 100


def test_find_dedicated_instance_does_not_substring_match_profile_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A profile dir that is merely a prefix of another must not match --
    exact directory equality only."""
    target = tmp_path / "reddit-profile"
    similar_but_different = tmp_path / "reddit-profile-old"
    decoy = FakeIterProcess(1, "chrome.exe", _chrome_cmdline(9222, str(similar_but_different)), 1.0)
    monkeypatch.setattr(rc.psutil, "process_iter", lambda attrs: [decoy])

    assert rc.find_dedicated_instance(target, 9222) is None


class FakeRunningProcess:
    """Stands in for psutil.Process(pid) -- the object shutdown() operates on
    directly, separate from the process_iter() snapshot used for discovery."""

    def __init__(self, create_time: float, running_for_calls: int = 0):
        self._create_time = create_time
        self.terminate_called = False
        self.kill_called = False
        self._running_calls_left = running_for_calls

    def create_time(self) -> float:
        return self._create_time

    def is_running(self) -> bool:
        if self._running_calls_left > 0:
            self._running_calls_left -= 1
            return True
        return False

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True


def test_shutdown_refuses_to_act_on_reused_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The core stale-PID-reuse defense: if the live process at this pid has a
    different create_time than the one we recorded, the original process is
    already gone and some unrelated process now holds that pid -- must not
    terminate/kill it."""
    profile_dir = tmp_path / "reddit-profile"
    identity = rc.ProcessIdentity(
        pid=555,
        create_time=1000.0,
        cmdline_snapshot=tuple(_chrome_cmdline(9222, str(profile_dir))),
        cdp_port=9222,
    )
    reused_process = FakeRunningProcess(create_time=9999.0)  # different create_time -- pid was recycled
    monkeypatch.setattr(rc.psutil, "Process", lambda pid: reused_process)

    result = rc.shutdown(identity)

    assert result is True
    assert reused_process.terminate_called is False
    assert reused_process.kill_called is False


def test_shutdown_terminates_the_verified_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "reddit-profile"
    identity = rc.ProcessIdentity(
        pid=555,
        create_time=1000.0,
        cmdline_snapshot=tuple(_chrome_cmdline(9222, str(profile_dir))),
        cdp_port=9222,
    )
    matched_process = FakeRunningProcess(create_time=1000.0, running_for_calls=0)
    monkeypatch.setattr(rc.psutil, "Process", lambda pid: matched_process)
    monkeypatch.setattr(rc.psutil, "process_iter", lambda attrs: [])  # nothing left after terminate
    monkeypatch.setattr(rc, "_cdp_probe", lambda port, timeout=2.0: None)

    result = rc.shutdown(identity)

    assert matched_process.terminate_called is True
    assert result is True


def test_profile_status_not_initialized_when_dir_missing(tmp_path: Path) -> None:
    profile_dir = tmp_path / "does-not-exist"
    assert rc.profile_status(profile_dir) == rc.ProfileStatus.NOT_INITIALIZED


def test_profile_status_unknown_for_preexisting_chrome_profile_without_our_metadata(tmp_path: Path) -> None:
    """The exact reddit_cdp_probe_profile scenario: real Chrome state exists
    (Local State file) but our own state file has never been written. Must be
    UNKNOWN, not NOT_INITIALIZED -- this is the bug fix from plan review."""
    profile_dir = tmp_path / "reddit-profile"
    profile_dir.mkdir()
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")

    assert rc.profile_status(profile_dir) == rc.ProfileStatus.UNKNOWN


def test_profile_status_healthy_after_recorded_success(tmp_path: Path) -> None:
    profile_dir = tmp_path / "reddit-profile"
    profile_dir.mkdir()
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")
    rc.write_profile_state(profile_dir, initialized_at="2026-01-01T00:00:00+00:00", last_success_at="2026-01-01T00:00:00+00:00")

    assert rc.profile_status(profile_dir) == rc.ProfileStatus.HEALTHY


def test_profile_status_challenged_after_recorded_challenge(tmp_path: Path) -> None:
    profile_dir = tmp_path / "reddit-profile"
    profile_dir.mkdir()
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")
    rc.write_profile_state(
        profile_dir,
        initialized_at="2026-01-01T00:00:00+00:00",
        last_success_at="2026-01-01T00:00:00+00:00",
        last_challenge_at="2026-01-02T00:00:00+00:00",  # more recent than last_success_at
    )

    assert rc.profile_status(profile_dir) == rc.ProfileStatus.CHALLENGED


def test_profile_status_returns_to_healthy_after_a_later_success(tmp_path: Path) -> None:
    """A profile challenged in one run and later successful in another
    transitions back to HEALTHY -- a profile is never permanently bricked."""
    profile_dir = tmp_path / "reddit-profile"
    profile_dir.mkdir()
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")
    rc.write_profile_state(
        profile_dir,
        last_challenge_at="2026-01-01T00:00:00+00:00",
        last_success_at="2026-01-02T00:00:00+00:00",  # more recent than last_challenge_at
    )

    assert rc.profile_status(profile_dir) == rc.ProfileStatus.HEALTHY


def test_parse_eval_result_preserves_plain_string_values() -> None:
    """Regression test for a real false-positive challenge detection caught
    by the live smoke test: agent-browser's CLI JSON-encodes a plain-string
    eval result once (e.g. `"mechanical keyboard - Reddit Search!"`), and the
    old double-decode-always logic discarded that as None because a second
    decode attempt on a bare string always fails. That collapsed both title
    and body to empty strings, which then falsely tripped the
    empty-title-means-challenge defense-in-depth check on a completely
    normal Reddit page."""
    raw = '"mechanical keyboard - Reddit Search!"'
    assert rc._parse_eval_result(raw) == "mechanical keyboard - Reddit Search!"


def test_parse_eval_result_still_unwraps_stringified_json_objects() -> None:
    """The second-decode path stays useful for JS payloads that themselves
    call JSON.stringify() on an object/array before returning."""
    raw = '"[{\\"a\\": 1}]"'
    assert rc._parse_eval_result(raw) == [{"a": 1}]


def test_parse_eval_result_handles_empty_output() -> None:
    assert rc._parse_eval_result("") is None
    assert rc._parse_eval_result("   ") is None


def test_write_profile_state_is_additive_merge(tmp_path: Path) -> None:
    """write_profile_state must never clobber existing fields with an update
    that doesn't mention them -- our metadata is observed history, appended
    to, not replaced wholesale."""
    profile_dir = tmp_path / "reddit-profile"
    profile_dir.mkdir()
    rc.write_profile_state(profile_dir, initialized_at="2026-01-01T00:00:00+00:00")
    rc.write_profile_state(profile_dir, last_success_at="2026-01-02T00:00:00+00:00")

    state = rc.read_profile_state(profile_dir)
    assert state["initialized_at"] == "2026-01-01T00:00:00+00:00"
    assert state["last_success_at"] == "2026-01-02T00:00:00+00:00"
