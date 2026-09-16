#!/usr/bin/env python
import getpass
import shlex
import socket
import subprocess
import os
import sys
import time

# Based on slurm version at Caltech:
# https://gist.github.com/haakon-e/e444972b99a5cd885ef6b29c86cb388e


def _env(name, default):
    """Read a CLUSTER_TUNNEL_<name> override, falling back to `default`."""
    return os.environ.get(f"CLUSTER_TUNNEL_{name}", default)


# Configuration. Every value can be overridden with a CLUSTER_TUNNEL_<NAME>
# environment variable, so the script does not need editing per user or site.
NUM_SLOTS = _env("NUM_SLOTS", "1")
JOB_TIME = _env("JOB_TIME", "8:00")
JOB_QUEUE = _env("JOB_QUEUE", "local")
# None means use the account's default project. See get_project_name().
PROJECT_NAME = _env("PROJECT", None)
LOGIN_NODE = _env("LOGIN_NODE", "login1.int.janelia.org")
JOB_NAME = _env("JOB_NAME", "tunnel")
HOST_KEY = _env("HOST_KEY", "~/.ssh/tunnel_key")
JOB_ID_ENV_VAR = "LSB_JOBID"

# Backoff used while waiting for the job to be dispatched. Each check is a
# bjobs call against the LSF master daemon, so a tight loop degrades the
# scheduler for every user on the cluster.
POLL_INITIAL_SECONDS = 1.0
POLL_MAX_SECONDS = 15.0
POLL_TIMEOUT_SECONDS = 300.0

# Idle shutdown. sshd -D runs until the wall clock expires even when nobody is
# connected. Reconnecting only costs the queue wait; vscode-server, extensions
# and credentials are in $HOME. Set CLUSTER_TUNNEL_IDLE_TIMEOUT=0 to disable.
IDLE_TIMEOUT_SECONDS = int(_env("IDLE_TIMEOUT", "900"))
IDLE_CHECK_SECONDS = int(_env("IDLE_CHECK", "60"))
KEEPALIVE_FILE = _env("KEEPALIVE_FILE", "~/.tunnel-keepalive")

# Processes that do not count as work. Everything in the session descends from
# the job's sshd, including the editor server and anything it spawns, so the
# tree has to be classified rather than merely counted.
#
# OpenSSH 9.8 and later name connection processes "sshd-session" and
# "sshd-auth". Matching only "sshd" makes every connection look like work.
INERT_SSHD = frozenset({"sshd", "sshd-session", "sshd-auth"})
INERT_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh"})
# The editor server re-spawns a `sleep` keep-alive forever, and a bare sleep is
# never work worth protecting.
INERT_COMMANDS = frozenset({"sleep"})
INERT_ARGS_MARKERS = (".vscode-server", ".vscode-remote")


def log(message):
    """
    Print a diagnostic.

    Always stderr: in proxy mode this process's stdout is the SSH data
    channel, and anything else written there corrupts the connection.
    """
    print(message, file=sys.stderr, flush=True)

def get_available_port():
    """
    Find an available TCP port
    """
    s = socket.socket()
    s.bind(("",0));
    port = s.getsockname()[1]
    s.close()
    return port

def get_jobid():
    """
    Get the LSF JOBID
    """
    jobid = os.environ.get(JOB_ID_ENV_VAR, None)
    return jobid

def list_jobs_as_dict():
    """
    List available jobs as a dict
    """
    command = ["bjobs", "-noheader", "-o", "jobid job_name description"]
    s = subprocess.run(command, check=True, capture_output=True, text=True)
    output = str(s.stdout)
    dict = {}
    for line in output.splitlines():
        parts = line.split()
        dict[parts[0]] = (parts[1], " ".join(parts[2:]))
    return dict

def is_login_node():
    """
    Check if this is the login node
    """
    return socket.getfqdn() == socket.getfqdn(LOGIN_NODE)

def get_compute_node_and_port():
    """
    Find a compute node with job name "tunnel"
    and get the SSHD port from the description.
    """
    if is_login_node():
        command = [
            "bjobs",
            "-noheader",
            "-J",
            JOB_NAME,
            "-o",
            "exec_host description delimiter=\":\""
        ]
    else:
        command = [
            "ssh",
            LOGIN_NODE,
            f"bjobs -noheader -J {JOB_NAME} -o 'exec_host description delimiter=\":\"'"
        ]
    # input must be provided so that this ssh instance does not read stdin
    s = subprocess.run(command, input="", check=True, capture_output=True, text=True)
    output = str(s.stdout).strip()
    if output:
        output = output.splitlines()[-1].strip()
        parts = output.split(':')
        if len(parts) < 2 or parts[0] == '-' or parts[1] == '-':
            return ""
    return output

def tunnel_job_exists():
    """
    True if a tunnel job exists in any state, including PEND.

    get_compute_node_and_port() only reports a job once it is running and has
    published its port in the description, so guarding on that alone lets a
    second connection queue a duplicate job while the first is still pending.
    Duplicates each hold an allocation and are billed.

    Note bjobs exits 0 whether or not the job exists and writes "is not found"
    to stderr, so presence has to be judged from stdout.
    """
    command = ["bjobs", "-noheader", "-J", JOB_NAME, "-o", "stat"]
    if not is_login_node():
        command = ["ssh", LOGIN_NODE, " ".join(command)]
    s = subprocess.run(command, input="", capture_output=True, text=True)
    return bool(s.stdout.strip())

def get_project_name():
    """
    Resolve the LSF project to bill the tunnel job to.

    Order: the CLUSTER_TUNNEL_PROJECT override, then the account's own default
    project as reported by `lsfgroup`, then None (omit -P and let LSF decide).

    Looking the project up matters for portability: a hardcoded project bills
    someone else's account and, at Janelia, esub refuses jobs outright from
    users whose group requires -P to be stated explicitly.
    """
    if PROJECT_NAME:
        return PROJECT_NAME
    try:
        s = subprocess.run(
            ["lsfgroup", getpass.getuser()],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Could not run lsfgroup to detect the default project: {e}")
        return None
    if s.returncode != 0:
        log(f"lsfgroup exited {s.returncode}; not passing -P")
        return None
    fields = s.stdout.split()
    return fields[0] if fields else None

def remote_command(command):
    """
    Wrap `command` so CLUSTER_TUNNEL_* settings survive the hop to the login node.

    The job is queued by a copy of this script running there, and ssh does not
    forward environment variables. Without this, overrides set on the
    workstation are silently ignored and the job is queued with defaults.
    """
    overrides = {k: v for k, v in os.environ.items() if k.startswith("CLUSTER_TUNNEL_")}
    if not overrides:
        return command
    assignments = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(overrides.items()))
    return f"env {assignments} {command}"

def start_job():
    # capture_output keeps the remote side's chatter off this process's
    # stdout, which in proxy mode is the SSH data channel.
    copy_command = ["scp", __file__, f"{LOGIN_NODE}:~/tunnel.py"]
    s = subprocess.run(copy_command, input="", capture_output=True, text=True)
    if s.returncode != 0:
        raise RuntimeError(f"Could not copy {__file__} to {LOGIN_NODE}: {s.stderr.strip()}")
    queue_command = ["ssh", LOGIN_NODE, remote_command("~/tunnel.py")]
    s = subprocess.run(queue_command, input="", capture_output=True, text=True)
    # Surface the remote side's output even on success: that is where
    # "already queued or running" is reported, and swallowing it makes the
    # command look like it did nothing.
    for stream in (s.stdout, s.stderr):
        if stream.strip():
            log(stream.strip())
    if s.returncode != 0:
        raise RuntimeError(f"Could not queue the tunnel job on {LOGIN_NODE} (see output above)")

def queue_job():
    """
    Acquire a compute node and execute run_job.
    This should run on the login node.
    """
    if tunnel_job_exists():
        log(f"Job with name \"{JOB_NAME}\" is already queued or running")
        return
    log("Queuing bsub job for tunnel")
    command = ["bsub",
               "-n", NUM_SLOTS,
               "-J", JOB_NAME,
               "-q", JOB_QUEUE,
               "-W", JOB_TIME]
    project = get_project_name()
    if project:
        command += ["-P", project]
    command += ["python", __file__]
    s = subprocess.run(command, capture_output=True, text=True)
    if s.stdout.strip():
        log(s.stdout.strip())
    # An esub rejection exits non-zero (255 at Janelia) and explains itself on
    # stdout. Also require the submission line, so a silent no-op is not
    # mistaken for success and left to time out in the wait loop.
    if s.returncode != 0 or "is submitted" not in s.stdout:
        raise RuntimeError(
            "bsub did not queue the tunnel job.\n"
            f"  command: {' '.join(command)}\n"
            f"  stdout: {s.stdout.strip()}\n"
            f"  stderr: {s.stderr.strip()}"
        )

def wait_for_compute_node_and_port(timeout=POLL_TIMEOUT_SECONDS):
    """
    Wait for the tunnel job to be dispatched, backing off between checks.

    Returns "host:port". Raises TimeoutError if the job has not started within
    `timeout` seconds, rather than polling forever against a job that will
    never run.
    """
    target = get_compute_node_and_port()
    if target:
        return target
    deadline = time.monotonic() + timeout
    delay = POLL_INITIAL_SECONDS
    while not target:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Job \"{JOB_NAME}\" was not dispatched within {timeout:.0f}s. "
                f"Check `bjobs -J {JOB_NAME}` on {LOGIN_NODE}."
            )
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, POLL_MAX_SECONDS)
        target = get_compute_node_and_port()
    return target

def do_proxy():
    """
    Forward stdin and stdout to the compute node.
    This should run on the user's computer as a SSH ProxyCommand in ~/.ssh/config
    """
    target = get_compute_node_and_port()
    if not target:
        start_job()
        target = wait_for_compute_node_and_port()
    command = ["ssh", "-W", target, LOGIN_NODE]
    subprocess.run(command)

def ensure_host_key(path=None):
    """
    Return the path to the sshd host key, generating it if absent.

    sshd will not start without one, and nothing else creates it, so a first
    run on a new account fails unless the key is made here.
    """
    path = os.path.expanduser(path or HOST_KEY)
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    log(f"Generating sshd host key at {path}")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", path, "-N", "", "-q",
         "-C", "cluster tunnel sshd host key"],
        check=True,
    )
    return path

def process_table():
    """Every process on this host as (pid, ppid, comm, args) tuples."""
    s = subprocess.run(
        ["ps", "-eo", "pid,ppid,comm,args", "--no-headers"],
        capture_output=True, text=True,
    )
    table = []
    for line in s.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        table.append((pid, ppid, fields[2], fields[3] if len(fields) > 3 else ""))
    return table

def busy_process(sshd_pid, table=None):
    """
    Describe the first real piece of work in the session, or None if idle.

    Everything in the session descends from the job's sshd, so the tree is
    walked and the infrastructure filtered out: sshd itself, the editor server
    and its own machinery, and shells sitting at an empty prompt. A shell
    *running* something has children and is therefore not idle, which is what
    distinguishes `tail -f` or a training script from an open terminal.
    """
    table = process_table() if table is None else table
    info = {pid: (comm, args) for pid, _, comm, args in table}
    children = {}
    for pid, ppid, _, _ in table:
        children.setdefault(ppid, []).append(pid)

    def first_busy(pid):
        """Deepest real process at or below pid, or None."""
        for child in children.get(pid, []):
            deeper = first_busy(child)
            if deeper:
                return deeper
        comm, args = info.get(pid, ("", ""))
        bare = comm.lstrip("-")
        if bare in INERT_SSHD or bare in INERT_COMMANDS:
            return None
        if any(marker in args for marker in INERT_ARGS_MARKERS):
            return None
        # A shell is only idle if nothing beneath it is working; the recursion
        # above has already established that. This is what lets the editor's
        # plain `bash -> sh` bootstrap chain count as idle while a shell
        # actually running your command does not.
        if bare in INERT_SHELLS:
            return None
        return f"pid {pid} ({comm})"

    for child in children.get(sshd_pid, []):
        busy = first_busy(child)
        if busy:
            return busy
    return None

def has_active_connections(port):
    """True if anything is currently connected to the tunnel's sshd port."""
    filt = f"( sport = :{port} )"
    for ss in ("/usr/sbin/ss", "ss"):
        try:
            s = subprocess.run(
                [ss, "-H", "-tn", "state", "established", filt],
                capture_output=True, text=True,
            )
        except OSError:
            continue
        if s.returncode == 0:
            return bool(s.stdout.strip())
    # If ss cannot run, assume someone is connected. Killing an active session
    # is worse than keeping an idle one.
    log("Could not check for active connections; assuming the tunnel is in use")
    return True

def keepalive_requested():
    return os.path.exists(os.path.expanduser(KEEPALIVE_FILE))

def watch_for_idle(sshd_process, port, timeout=None, interval=None):
    """
    Shut the tunnel down once nobody is attached and nothing is running.

    The idle clock only advances while both are true, and any sign of life
    resets it.
    """
    timeout = IDLE_TIMEOUT_SECONDS if timeout is None else timeout
    interval = IDLE_CHECK_SECONDS if interval is None else interval
    if timeout <= 0:
        log("Idle shutdown disabled. The job will run until its wall clock expires.")
        sshd_process.wait()
        return
    log(f"Will shut down after {timeout}s with no client attached and nothing running "
        f"(touch {KEEPALIVE_FILE} to prevent this).")
    idle = 0
    while True:
        if sshd_process.poll() is not None:
            log(f"sshd exited with status {sshd_process.returncode}")
            return
        time.sleep(interval)
        if keepalive_requested():
            reason = f"{KEEPALIVE_FILE} exists"
        elif has_active_connections(port):
            reason = "a client is connected"
        else:
            reason = busy_process(sshd_process.pid)
            if reason:
                reason = f"still running: {reason}"
        if reason:
            if idle:
                log(f"Idle timer reset ({reason})")
            idle = 0
            continue
        idle += interval
        if idle >= timeout:
            log(f"Idle for {idle}s with nothing running - shutting down. Reconnect for a "
                f"fresh tunnel; nothing is lost because $HOME is shared across nodes.")
            sshd_process.terminate()
            try:
                sshd_process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                sshd_process.kill()
            return

def run_job(jobid=get_jobid()):
    """
    Run the job on the compute node.
    1. Sets the description to an available port for sshd
    2. Runs sshd
    3. Shuts down once the session has been idle (see watch_for_idle)
    """
    host_key = ensure_host_key()
    port = get_available_port()
    log(f"Setting description of job id {jobid} to {port} (port number)")
    mod_command = ["bmod", "-Jd", str(port), str(jobid)]
    subprocess.run(mod_command)
    log(f"Running sshd on port {port} with key {host_key}")
    command = ["/usr/sbin/sshd", "-D", "-p", str(port), "-f", "/dev/null", "-h", host_key]
    sshd_process = subprocess.Popen(command)
    watch_for_idle(sshd_process, port)

def kill_job():
    if is_login_node():
        command = ["bkill", "-J", JOB_NAME]
    else:
        command = ["ssh", LOGIN_NODE, f"bkill -J {JOB_NAME}"]
    subprocess.run(command)

def main():
    action = None

    if len(sys.argv) > 1:
        action = sys.argv[1]
    elif is_login_node():
        # Default action on the login node is to queue the job
        action = "queue_job"
    elif jobid := get_jobid():
        # Default action on a compute node is to run sshd
        action = "run_job"
    else:
        # Otherwise default action is to establish the SSH tunnel via ssh -W
        action = "proxy"

    # Map action to function
    if action == "start_job":
        start_job()
    elif action == "queue_job":
        queue_job()
    elif action == "run_job":
        run_job(jobid)
    elif action == "proxy":
        do_proxy()
    elif action == "kill_job":
        kill_job()
    else:
        raise Exception(f"Unknown action: {action}. Usage {__file__} [start_job|queue_job|run_job|kill_job|proxy].")

if __name__ == "__main__":
    main()
