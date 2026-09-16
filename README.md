# Cluster Tunnel for VS Code 

VSCode has a remote feature that establishes a connection to a remote computer and presents the VSCode graphical interface as if running on that computer. Three options are provided by default.

1. A direct SSH connection to a specific computer
2. Tunnels registered with Microsoft
3. Tunnels registered with GitHub.

Private computer clusters often provide a login node equipped with a scheduler in order to reserve compute nodes on the cluster for jobs. Cluster users may want to use VSCode to interactively edit, execute, or observe code running on a cluster environment. Since the compute node may vary, a VS Code user would have to reconfigure their SSH connection for every cluster session. Alternatively, they could register a tunnel with Microsoft or Github.

This repository contains a Python script to facilitate a SSH connection from a user's workstation to a compute node.

It is currently customized for use at the Janelia Research Campus of the Howard Hughes Medical Institute. Janelia's cluster uses IBM LSF.

The script performs the following steps.

1. Connects to the login node.
2. Schedules the script as a job on a compute node.
3. On the compute node:
    a. Identifies a free TCP port.
    b. Changes the job description to the free port number.
    c. Starts a `sshd` session bound to that port number.
4. Identifies the compute node and the port number for the client workstation.
5. Fowards stdin and stdout over TCP to the compute node via the login node by invoking `ssh -W $host:$port $login_node`

The script is meant to be used via the OpenSSH's [`ProxyCommand`](https://man.openbsd.org/ssh_config#ProxyCommand) option.

Currently, the script requires manual configuration of SSH, typically via `~/.ssh/config` where `~` indicate the user's home folder.

```
Host hpcx
    ProxyCommand python /path/to/tunnel.py proxy
```

Optionally, the following configuration is recommended to minimize the number of password prompts.

```
Host *
    AddKeysToAgent yes # Minimize number of password prompts
    UseKeychain yes # For MacOS only
    IdentityFile ~/.ssh/id_ed25519 # Use a particular SSH key.
```

It is recommend to use a SSH key. Github has [detailed instructions](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent) on SSH key generation across multiple platforms.

# Installation

1. Clone the repository in order to download the script.
```
git clone https://github.com/JaneliaSciComp/ssh_cluster_tunnel_for_vscode/
```

2. Configure `~/.ssh/config` as detailed above.

3. Add your SSH public key to `~/.ssh/authorized_keys` on the cluster. The `sshd` started on the compute node authenticates against this file.

   The script connects to the login node by its full hostname (`login1.int.janelia.org` by default), so your `User` and `IdentityFile` settings must apply to that name, not just to an alias:

   ```
   Host janelia login1.int.janelia.org
       HostName login1.int.janelia.org
       User your_username
       IdentityFile ~/.ssh/your_key
   ```

4. Ensure that `python` is installed. We recommend installing `python` from conda-forge via `pixi`.

The sshd host key (`~/.ssh/tunnel_key` on the cluster) is generated automatically on first run.

## Configuration

Each setting has a default at the top of `cluster_tunnel/tunnel.py` and can be overridden with an environment variable.

| Variable | Default | Meaning |
|---|---|---|
| `CLUSTER_TUNNEL_PROJECT` | from `lsfgroup` | LSF project to bill (`bsub -P`) |
| `CLUSTER_TUNNEL_NUM_SLOTS` | `1` | Slots to request (`bsub -n`) |
| `CLUSTER_TUNNEL_JOB_TIME` | `8:00` | Wall time (`bsub -W`) |
| `CLUSTER_TUNNEL_JOB_QUEUE` | `local` | Queue (`bsub -q`) |
| `CLUSTER_TUNNEL_LOGIN_NODE` | `login1.int.janelia.org` | Login node |
| `CLUSTER_TUNNEL_JOB_NAME` | `tunnel` | LSF job name |
| `CLUSTER_TUNNEL_HOST_KEY` | `~/.ssh/tunnel_key` | sshd host key path |
| `CLUSTER_TUNNEL_IDLE_TIMEOUT` | `900` | Seconds idle before the job exits. `0` disables. |
| `CLUSTER_TUNNEL_IDLE_CHECK` | `60` | Seconds between idle checks |
| `CLUSTER_TUNNEL_KEEPALIVE_FILE` | `~/.tunnel-keepalive` | If this file exists, the job never exits on idle |

The variables are forwarded to the login node when the job is queued, since the job is submitted from there and ssh does not pass the environment through.

If `CLUSTER_TUNNEL_PROJECT` is unset, the project is taken from `lsfgroup $USER`. If that command is unavailable, `-P` is omitted. Some Janelia groups, including `scicompsoft`, are rejected by esub unless `-P` is given.

## Idle shutdown

`sshd -D` runs until the job's wall clock expires, whether or not anyone is connected. Closing VS Code does not end the job.

The job exits when no client is connected to its port and nothing is running in the session. Reconnecting queues a new job. vscode-server, extensions and credentials are stored in `$HOME`, which all compute nodes mount, so nothing is lost.

The running check walks the process tree under sshd. The following are treated as idle:

- `sshd`, `sshd-session` and `sshd-auth` (OpenSSH 9.8 and later use separate process names for connections)
- processes whose command line contains `.vscode-server` or `.vscode-remote`
- `sleep` (vscode-server runs a `sleep 180` keep-alive loop)
- shells with no child processes

Anything else, such as a running script, `tail -f`, or a notebook kernel, keeps the job alive. Note that a `sleep` in your own terminal will not.

If the connection check cannot run (for example `ss` is missing), the session is assumed to be in use and the job stays up.

`touch ~/.tunnel-keepalive` prevents the job from exiting. `CLUSTER_TUNNEL_IDLE_TIMEOUT=0` disables the feature.

# Command line usage

The script can be invoked with no argument or a single argument. The default action when no argument is provided depends on the host.

* `python -m cluster_tunnel start_job` - Copy the script to `~/tunnel.py` on the login node. Queue the job.
* `python -m cluster_tunnel queue_job` - Queue the job from the login node. Default action on the login node.
* `python -m cluster_tunnel run_job` - Get a free TCP port. Change the job description to the port number. Start `sshd`. Default action on a compute node.
* `python -m cluster_tunnel proxy` - Run `ssh -W $hostname:$port $login_node`, retrieving the variables from the login node. Default action on a workstation that is not the login node or a compute node.
* `python -m cluster_tunnel kill_job` - Terminate the job.

# Pixi tasks

* `pixi run tunnel [action]` can be used to run the script where action is one of `start_job`, `queue_job`, `run_job`, or `kill_job`
* `pixi run ssh` creates a ssh session to the compute node
* `pixi run test` runs pytest

# Testing and Debugging

Tests can be invoked by running `pytest`. The pixi task `pixi run test` can be used to automate the installation and invocation of `pytest`.

Executing `ssh hpcx` should be sufficient to test if the command can establish a connection to the compute node. VSCode parses the SSH config file and should present an option to connect to `hpcx`.

Without modify the SSH config, the script can be used as a ssh proxy via the command line:

```
ssh -o "ProxyCommand python -m cluster_tunnel proxy" hpcx
```

If you continue to have problems with the script, check if the following SSH configuration worksreplacing the `$host`, `$port`, and `$login_node` variables with the known values for the compute node, `sshd` port, and hostname for the login node, respectively.

If the compute nodes can be directly accessed without going through the login node, `$login_node` can be any server running sshd that you have access to, including `localhost`.

```
Host hpcx
    ProxyCommand ssh -W $host:$port $login_node
```

You may also try this on the command line, replacing the variables as described above.
```
$ ssh -o "ProxyCommand ssh -W $host:$port $login_node" hpcx
```

If setting `ProxyCommand` to `ssh -W ...` succeeds, then the problem is in the Python's script ability to retrieve the `$host` and `$port` information.

To examine the functionality of individual functions, the key steps of the Python script can be tested as follows.

```python
import cluster_tunnel

# Logs in to the login node, copies the script there, and queues a job
cluster_tunnel.start_job()

# Return "hostname:port" as a string
cluster_tunnel.get_compute_node_and_port()

# Forward stdin and stdout to the compute node by running `ssh -W hostname:port hpc_login_node`
cluster_tunnel.do_proxy() # Should report the SSH version. Press Ctrl-D to terminate.
```

# Microsoft Windows

Microsoft Windows 10 and Windows 11 offers OpenSSH as an [optional feature](https://learn.microsoft.com/en-us/windows/terminal/tutorials/ssh).

It is also recommended to [enable and use the `ssh-agent` service](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement).

Currently, there is a known issue with Python's `capture_output` keyword to `subprocess.run` causes the script to hang.

# LSF details

The Python script uses the following commands with regard to IBM's LSF scheduler.

1. Retrieve the job description.

```
bjobs -noheader -J tunnel -o 'exec_host description delimiter=":"'
```

The expected output should resemble `e10u22:25561`.

2. Set the job description to the port number

```
bmod -Jd $port $jobid
```

3. The job id is stored in an environment variable called `LSB_JOBID`.

# Inspiration

This Python script is inspired by a script by Haakon Ludvig Langeland Ervik at Caltech:
https://gist.github.com/haakon-e/e444972b99a5cd885ef6b29c86cb388e

# License

[BSD 3-Clause](LICENSE.txt)
