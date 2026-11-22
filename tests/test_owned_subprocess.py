"""Real launcher/grandchild lifetime regressions; no CAD tool is required."""

import ctypes
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

import pytest

from circuit_weaver import _owned_subprocess
from circuit_weaver._owned_subprocess import run_captured


def test_capture_uses_closed_stdin_and_preserves_output_and_exit(tmp_path):
    result = run_captured(
        [sys.executable, "-c",
         "import sys; assert sys.stdin.read() == ''; print('out'); print('err', file=sys.stderr)"],
        cwd=tmp_path, env=os.environ.copy(), timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


@pytest.mark.skipif(os.name != "nt", reason="Windows virtualenv launcher and Job Object regression")
@pytest.mark.skip_category("platform")
def test_timeout_kills_real_grandchild_and_leaves_unrelated_process_alive(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(120)\n",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\ntime.sleep(120)\n",
    )
    # Use the base interpreter for the unrelated control so its cleanup needs
    # no launcher traversal; the owned tree deliberately uses the venv launcher.
    unrelated = subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(120)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    child_handle = None
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                run_captured, [sys.executable, str(parent), str(grandchild), str(pid_file)],
                cwd=tmp_path, env=os.environ.copy(), timeout=2,
            )
            deadline = time.monotonic() + 1.8
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pid_file.exists(), "owned grandchild must actually start before the timeout"
            child_handle = kernel.OpenProcess(0x00100000, False, int(pid_file.read_text()))  # SYNCHRONIZE
            assert child_handle, "hold the exact grandchild process object across cleanup"
            with pytest.raises(subprocess.TimeoutExpired):
                future.result(timeout=10)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
        assert unrelated.poll() is None
    finally:
        if child_handle:
            kernel.CloseHandle(child_handle)
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_launch_error_is_reported_without_running_a_child(tmp_path):
    with pytest.raises(OSError):
        run_captured([str(tmp_path / "missing-executable")], cwd=tmp_path, env=os.environ.copy(), timeout=1)


@pytest.mark.skipif(os.name != "nt", reason="Windows child must stay suspended until assigned")
@pytest.mark.skip_category("platform")
def test_assignment_failure_reaps_suspended_child_without_starting_it(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-run"

    def fail_assignment(self, pid):
        raise OSError("assignment denied")

    monkeypatch.setattr(_owned_subprocess._WindowsJob, "assign_and_resume", fail_assignment)
    with pytest.raises(OSError, match="assignment denied"):
        run_captured(
            [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)],
            cwd=tmp_path, env=os.environ.copy(), timeout=1,
        )
    assert not marker.exists()
