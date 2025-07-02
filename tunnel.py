#!/usr/bin/env python
import socket
import subprocess
import os
import sys
import time

# Based on slurm version at Caltech:
# https://gist.github.com/haakon-e/e444972b99a5cd885ef6b29c86cb388e

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
    jobid = os.environ.get("LSB_JOBID", None)
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
    return socket.getfqdn() == socket.getfqdn("login1")

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
            "tunnel",
            "-o",
            "exec_host description delimiter=\":\""
        ]
    else:
        command = [
            "ssh",
            "login1",
            "bjobs -noheader -J tunnel -o 'exec_host description delimiter=\":\"'"
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

def start_job():
    copy_command = ["scp", __file__, "login1:~/tunnel.py"]
    subprocess.run(copy_command, input="")
    queue_command = ["ssh", "login1", "~/tunnel.py"]
    subprocess.run(queue_command, input="")

def queue_job():
    """
    Acquire a compute node and execute run_job.
    This should run on the login node.
    """
    if get_compute_node_and_port():
        print("Job with name \"tunnel\" is already running")
    else:
        print("Queuing bsub job for tunnel")
        command = ["bsub", "-n", "1", "-P", "scicompsoft", "-J", "tunnel", "python", __file__]
        subprocess.run(command)

def do_proxy():
    """
    Forward stdin and stdout to the compute node.
    This should run on the user's computer as a SSH ProxyCommand in ~/.ssh/config
    """
    target = get_compute_node_and_port()
    if not target:
        start_job()
        while not target:
            time.sleep(1)
            target = get_compute_node_and_port()
    command = ["ssh", "-W", target, "hpc"]
    subprocess.run(command)

def run_job(jobid=get_jobid()):
    """
    Run the job on the compute node.
    1. Sets the description to an available port for sshd
    2. Runs sshd
    """
    HOME = os.environ["HOME"]
    port = get_available_port()
    print(f"Setting description of job id {jobid} to {port} (port number)")
    mod_command = ["bmod", "-Jd", str(port), str(jobid)]
    subprocess.run(mod_command)
    print(f"Running sshd on port {port} with key {HOME}/.ssh/tunnel_key")
    command = ["/usr/sbin/sshd", "-D", "-p", str(port), "-f", "/dev/null", "-h", f"{HOME}/.ssh/tunnel_key"]
    subprocess.run(command)

def kill_job():
    if is_login_node():
        command = ["bkill", "-J", "tunnel"]
    else:
        command = ["ssh", "login1", "bkill -J tunnel"]
    subprocess.run(command)

def main():
    action = None
    if len(sys.argv) > 1:
        action = sys.argv[1]
    elif is_login_node():
        action = "queue_job"
    elif jobid := get_jobid():
        action = "run_job"
    else:
        action = "proxy"

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
