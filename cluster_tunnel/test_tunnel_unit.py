"""
Unit tests that do not require a cluster.

The tests in test_tunnel.py are integration tests: they queue real LSF jobs and
only pass on a login node. These cover the pure logic so the package can be
tested anywhere.
"""
import os

import pytest

from cluster_tunnel import tunnel


def test_log_writes_to_stderr_not_stdout(capsys):
    # In proxy mode stdout is the SSH data channel; a stray byte there breaks
    # the connection. This is the guard against regressing that.
    tunnel.log("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello" in captured.err


def test_ensure_host_key_generates_a_key(tmp_path):
    key = tmp_path / "nested" / "tunnel_key"
    returned = tunnel.ensure_host_key(str(key))
    assert returned == str(key)
    assert key.exists()
    assert key.with_suffix(".pub").exists()


def test_ensure_host_key_is_idempotent(tmp_path):
    key = tmp_path / "tunnel_key"
    tunnel.ensure_host_key(str(key))
    first = key.read_bytes()
    tunnel.ensure_host_key(str(key))
    assert key.read_bytes() == first, "existing host key must not be regenerated"


def test_ensure_host_key_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    returned = tunnel.ensure_host_key("~/.ssh/tunnel_key")
    assert returned == os.path.join(str(tmp_path), ".ssh", "tunnel_key")
    assert os.path.exists(returned)


def test_get_project_name_prefers_explicit_override(monkeypatch):
    monkeypatch.setattr(tunnel, "PROJECT_NAME", "combsensors")
    # lsfgroup must not be consulted when an override is set.
    monkeypatch.setattr(tunnel.subprocess, "run", _fail)
    assert tunnel.get_project_name() == "combsensors"


def test_get_project_name_falls_back_to_lsfgroup(monkeypatch):
    monkeypatch.setattr(tunnel, "PROJECT_NAME", None)
    monkeypatch.setattr(
        tunnel.subprocess, "run",
        lambda *a, **k: _Completed(stdout="scicompsoft\n"),
    )
    assert tunnel.get_project_name() == "scicompsoft"


def test_get_project_name_returns_none_when_lsfgroup_missing(monkeypatch):
    monkeypatch.setattr(tunnel, "PROJECT_NAME", None)
    monkeypatch.setattr(tunnel.subprocess, "run", _raise_oserror)
    # None means "omit -P and let LSF pick", which is better than guessing.
    assert tunnel.get_project_name() is None


def test_wait_returns_immediately_when_job_is_up(monkeypatch):
    monkeypatch.setattr(tunnel, "get_compute_node_and_port", lambda: "h06u12:25561")
    assert tunnel.wait_for_compute_node_and_port(timeout=0.1) == "h06u12:25561"


def test_wait_times_out_instead_of_polling_forever(monkeypatch):
    monkeypatch.setattr(tunnel, "get_compute_node_and_port", lambda: "")
    monkeypatch.setattr(tunnel.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        tunnel.wait_for_compute_node_and_port(timeout=0.01)


def test_wait_backs_off_between_checks(monkeypatch):
    # Every check is a bjobs call against the LSF master daemon, so the
    # interval must grow rather than hammer once a second.
    delays = []
    calls = {"n": 0}

    def fake_target():
        calls["n"] += 1
        return "h06u12:25561" if calls["n"] > 4 else ""

    monkeypatch.setattr(tunnel, "get_compute_node_and_port", fake_target)
    monkeypatch.setattr(tunnel.time, "sleep", delays.append)
    tunnel.wait_for_compute_node_and_port(timeout=600)
    assert delays == sorted(delays), f"delays must be non-decreasing, got {delays}"
    assert delays[-1] > delays[0]
    assert max(delays) <= tunnel.POLL_MAX_SECONDS


def test_remote_command_is_unchanged_without_overrides(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLUSTER_TUNNEL_"):
            monkeypatch.delenv(key)
    assert tunnel.remote_command("~/tunnel.py") == "~/tunnel.py"


def test_remote_command_forwards_overrides(monkeypatch):
    # ssh does not forward environment variables, and the job is queued by the
    # copy of the script on the login node, so overrides must be passed
    # explicitly or they are silently dropped.
    monkeypatch.setenv("CLUSTER_TUNNEL_PROJECT", "combsensors")
    monkeypatch.setenv("CLUSTER_TUNNEL_JOB_TIME", "2:00")
    wrapped = tunnel.remote_command("~/tunnel.py")
    assert wrapped.startswith("env ")
    assert "CLUSTER_TUNNEL_PROJECT=combsensors" in wrapped
    assert "CLUSTER_TUNNEL_JOB_TIME=2:00" in wrapped
    assert wrapped.endswith("~/tunnel.py")


def test_remote_command_quotes_hostile_values(monkeypatch):
    monkeypatch.setenv("CLUSTER_TUNNEL_JOB_NAME", "a; rm -rf /")
    wrapped = tunnel.remote_command("~/tunnel.py")
    assert "; rm -rf /" not in wrapped.replace("'a; rm -rf /'", "")


def test_tunnel_job_exists_is_true_for_a_pending_job(monkeypatch):
    # A PEND job has no port yet, so get_compute_node_and_port() sees nothing.
    # The duplicate guard must still notice it, or a second connection queues
    # another billed job.
    monkeypatch.setattr(tunnel, "is_login_node", lambda: True)
    monkeypatch.setattr(
        tunnel.subprocess, "run", lambda *a, **k: _Completed(stdout="PEND\n")
    )
    assert tunnel.tunnel_job_exists() is True


def test_tunnel_job_exists_is_false_when_bjobs_finds_nothing(monkeypatch):
    # bjobs exits 0 and writes "is not found" to stderr, so the exit code
    # cannot be used to decide this.
    monkeypatch.setattr(tunnel, "is_login_node", lambda: True)
    monkeypatch.setattr(
        tunnel.subprocess, "run",
        lambda *a, **k: _Completed(stdout="", stderr="Job <tunnel> is not found", returncode=0),
    )
    assert tunnel.tunnel_job_exists() is False


# (pid, ppid, comm, args). 100 is the job's sshd in all of these.
# Shapes below are taken from a real session on a Janelia compute node.
_SSHD = (100, 1, "sshd", "/usr/sbin/sshd -D -p 41603")
# OpenSSH >= 9.8 names per-connection processes sshd-session, not sshd.
_SESSION = (101, 100, "sshd-session", "sshd-session: basham [priv]")
_IDLE_SHELL = (102, 101, "bash", "-bash")

# The editor bootstrap: plain bash -> sh whose args do not mention
# .vscode-server, then the server itself, plus a self-respawning keep-alive.
_EDITOR_TREE = [
    (110, 101, "bash", "-bash"),
    (111, 110, "sh", "sh"),
    (112, 111, "code-7debcd0e2a", "/home/u/.vscode-server/code-7debcd0e2a/code serve-web"),
    (113, 112, "MainThread", "/home/u/.vscode-server/cli/servers/Stable-7de/server/node"),
    (114, 111, "sleep", "sleep 180"),
]


def test_sshd_session_is_not_mistaken_for_work():
    # Regression: only "sshd" was treated as inert, so every connection on
    # OpenSSH >= 9.8 looked busy and the tunnel could never be reaped.
    assert tunnel.busy_process(100, [_SSHD, _SESSION, _IDLE_SHELL]) is None


def test_editor_bootstrap_chain_is_idle():
    # Regression: the bootstrap shells have no .vscode-server in their args and
    # the keep-alive sleep respawns forever, so a flat scan called this busy.
    assert tunnel.busy_process(100, [_SSHD, _SESSION] + _EDITOR_TREE) is None


def test_work_alongside_the_editor_is_still_detected():
    table = [_SSHD, _SESSION] + _EDITOR_TREE + [
        (120, 101, "bash", "-bash"),
        (121, 120, "python", "python train.py"),
    ]
    assert "python" in tunnel.busy_process(100, table)


def test_idle_shell_alone_is_not_busy():
    table = [_SSHD, _SESSION, _IDLE_SHELL]
    assert tunnel.busy_process(100, table) is None


def test_shell_running_something_is_busy():
    # A shell with a working child is not an empty prompt.
    table = [_SSHD, _SESSION, _IDLE_SHELL, (103, 102, "python", "python train.py")]
    assert "python" in tunnel.busy_process(100, table)


def test_a_bare_sleep_does_not_hold_the_tunnel_open():
    # Deliberate tradeoff: the editor respawns `sleep` forever as a keep-alive,
    # so treating it as work would mean the tunnel is never reaped. The cost is
    # that a literal `sleep 300` in your terminal will not protect the session.
    table = [_SSHD, _SESSION, _IDLE_SHELL, (103, 102, "sleep", "sleep 300")]
    assert tunnel.busy_process(100, table) is None


def test_tail_f_keeps_the_tunnel_alive():
    table = [_SSHD, _SESSION, _IDLE_SHELL, (103, 102, "tail", "tail -f train.log")]
    assert "tail" in tunnel.busy_process(100, table)


def test_editor_server_alone_is_not_busy():
    # vscode-server deliberately outlives a disconnect, so it must not be
    # mistaken for work or the tunnel would never be reaped.
    table = [
        _SSHD, _SESSION,
        (104, 101, "node", "/home/u/.vscode-server/code-abc/out/server-main.js"),
        (105, 104, "node", "/home/u/.vscode-server/code-abc/bootstrap-fork --type=extensionHost"),
    ]
    assert tunnel.busy_process(100, table) is None


def test_kernel_spawned_by_the_editor_is_busy():
    # ...but its children are real work and must be protected.
    table = [
        _SSHD, _SESSION,
        (104, 101, "node", "/home/u/.vscode-server/code-abc/out/server-main.js"),
        (106, 104, "python", "python -m ipykernel_launcher -f kernel.json"),
    ]
    assert "python" in tunnel.busy_process(100, table)


def test_processes_outside_the_session_are_ignored():
    # Another user's job on the same node must not keep our tunnel alive.
    table = [_SSHD, _SESSION, _IDLE_SHELL, (900, 1, "python", "someone_elses_training.py")]
    assert tunnel.busy_process(100, table) is None


def test_keepalive_file_is_detected(tmp_path, monkeypatch):
    marker = tmp_path / "keep"
    monkeypatch.setattr(tunnel, "KEEPALIVE_FILE", str(marker))
    assert tunnel.keepalive_requested() is False
    marker.write_text("")
    assert tunnel.keepalive_requested() is True


def test_connection_check_fails_safe(monkeypatch):
    # If ss cannot run we must assume someone is connected; reaping an active
    # session is far worse than holding an idle one.
    monkeypatch.setattr(tunnel.subprocess, "run", _raise_oserror)
    assert tunnel.has_active_connections(41603) is True


def test_watchdog_reaps_an_idle_session(monkeypatch):
    monkeypatch.setattr(tunnel, "keepalive_requested", lambda: False)
    monkeypatch.setattr(tunnel, "has_active_connections", lambda port: False)
    monkeypatch.setattr(tunnel, "busy_process", lambda pid, table=None: None)
    monkeypatch.setattr(tunnel.time, "sleep", lambda s: None)
    proc = _FakeProcess()
    tunnel.watch_for_idle(proc, 41603, timeout=120, interval=60)
    assert proc.terminated, "an idle tunnel should be shut down"


def test_watchdog_spares_a_busy_session(monkeypatch):
    monkeypatch.setattr(tunnel, "keepalive_requested", lambda: False)
    monkeypatch.setattr(tunnel, "has_active_connections", lambda port: False)
    monkeypatch.setattr(tunnel, "busy_process", lambda pid, table=None: "pid 1 (tail)")
    monkeypatch.setattr(tunnel.time, "sleep", lambda s: None)
    # Exits on its own after a while so the test cannot hang.
    proc = _FakeProcess(alive_for=50)
    tunnel.watch_for_idle(proc, 41603, timeout=120, interval=60)
    assert not proc.terminated, "work in progress must not be killed"


def test_watchdog_disabled_by_zero_timeout(monkeypatch):
    proc = _FakeProcess()
    tunnel.watch_for_idle(proc, 41603, timeout=0)
    assert proc.waited
    assert not proc.terminated


def _recorder(result):
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        return result

    return calls, run


def test_remote_bjobs_query_quotes_the_job_name(monkeypatch):
    # JOB_NAME comes from the environment and is interpolated into a remote
    # shell string, so it must be quoted or it can run extra commands.
    monkeypatch.setattr(tunnel, "JOB_NAME", "x; touch /tmp/pwned")
    monkeypatch.setattr(tunnel, "is_login_node", lambda: False)
    calls, run = _recorder(_Completed(stdout=""))
    monkeypatch.setattr(tunnel.subprocess, "run", run)
    tunnel.get_compute_node_and_port()
    remote = calls[0][2]
    assert "'x; touch /tmp/pwned'" in remote
    assert "; touch" not in remote.replace("'x; touch /tmp/pwned'", "")


def test_tunnel_job_exists_quotes_the_job_name(monkeypatch):
    monkeypatch.setattr(tunnel, "JOB_NAME", "x; touch /tmp/pwned")
    monkeypatch.setattr(tunnel, "is_login_node", lambda: False)
    calls, run = _recorder(_Completed(stdout=""))
    monkeypatch.setattr(tunnel.subprocess, "run", run)
    tunnel.tunnel_job_exists()
    assert "'x; touch /tmp/pwned'" in calls[0][2]


def test_kill_job_quotes_the_job_name(monkeypatch):
    monkeypatch.setattr(tunnel, "JOB_NAME", "x; touch /tmp/pwned")
    monkeypatch.setattr(tunnel, "is_login_node", lambda: False)
    calls, run = _recorder(_Completed())
    monkeypatch.setattr(tunnel.subprocess, "run", run)
    tunnel.kill_job()
    assert "'x; touch /tmp/pwned'" in calls[0][2]


def test_tunnel_job_exists_fails_closed_when_bjobs_errors(monkeypatch):
    # A failed query must not be read as "no job", or the caller submits a
    # duplicate.
    monkeypatch.setattr(tunnel, "is_login_node", lambda: True)
    monkeypatch.setattr(
        tunnel.subprocess, "run",
        lambda *a, **k: _Completed(stdout="", stderr="lsf down", returncode=255),
    )
    with pytest.raises(RuntimeError):
        tunnel.tunnel_job_exists()


def test_process_table_returns_none_when_ps_cannot_run(monkeypatch):
    monkeypatch.setattr(tunnel.subprocess, "run", _raise_oserror)
    assert tunnel.process_table() is None


def test_process_table_returns_none_when_ps_fails(monkeypatch):
    monkeypatch.setattr(
        tunnel.subprocess, "run",
        lambda *a, **k: _Completed(stdout="", stderr="boom", returncode=1),
    )
    assert tunnel.process_table() is None


def test_unreadable_process_table_counts_as_busy(monkeypatch):
    # Fail safe: if we cannot see the processes, do not conclude the session
    # is idle.
    monkeypatch.setattr(tunnel, "process_table", lambda: None)
    assert tunnel.busy_process(100) is not None


def test_start_job_reports_whether_it_submitted(monkeypatch):
    submitted = iter([
        _Completed(),  # scp
        _Completed(stdout="Job <1> is submitted to queue <local>."),
        _Completed(),  # scp
        _Completed(stderr='Job with name "tunnel" is already queued or running'),
    ])
    monkeypatch.setattr(tunnel.subprocess, "run", lambda *a, **k: next(submitted))
    assert tunnel.start_job() is True
    assert tunnel.start_job() is False


def test_timeout_cancels_only_a_job_this_connection_submitted(monkeypatch):
    killed = []
    monkeypatch.setattr(tunnel, "get_compute_node_and_port", lambda: "")
    monkeypatch.setattr(tunnel, "kill_job", lambda: killed.append(True))

    def timeout():
        raise TimeoutError("no dispatch")

    monkeypatch.setattr(tunnel, "wait_for_compute_node_and_port", timeout)

    monkeypatch.setattr(tunnel, "start_job", lambda: True)
    with pytest.raises(TimeoutError):
        tunnel.do_proxy()
    assert killed == [True]

    killed.clear()
    monkeypatch.setattr(tunnel, "start_job", lambda: False)
    with pytest.raises(TimeoutError):
        tunnel.do_proxy()
    assert killed == [], "must not cancel a job another connection is waiting on"


class _FakeProcess:
    def __init__(self, alive_for=None):
        self.pid = 100
        self.returncode = 0
        self.terminated = False
        self.waited = False
        self._alive_for = alive_for
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._alive_for is not None and self._polls > self._alive_for:
            return 0
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        self.terminated = True


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _fail(*args, **kwargs):
    raise AssertionError("subprocess should not have been called")


def _raise_oserror(*args, **kwargs):
    raise OSError("lsfgroup not found")
