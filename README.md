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
Host hpc
    HostName fqdn.to.login.node

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

```
git clone https://github.com/JaneliaSciComp/ssh_cluster_tunnel_for_vscode/
```

# Microsoft Windows

Microsoft Windows 10 and Windows 11 offers OpenSSH as an [optional feature](https://learn.microsoft.com/en-us/windows/terminal/tutorials/ssh).

It is also recommended to [enable and use the `ssh-agent` service](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement).

Currently, there is a known issue with Python's `capture_output` keyword to `subprocess.run` causes the script to hang.

# Inspiration

This Python script is inspired by a script by Haakon Ludvig Langeland Ervik at Caltech:
https://gist.github.com/haakon-e/e444972b99a5cd885ef6b29c86cb388e

# License

[3-Clause Open BSD](LICENSE.txt)
