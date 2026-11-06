import paramiko


class KindleSSHClient:

    def __init__(self):
        self.ssh = None
        self.sftp = None

    def connect(self, host, port=2222, username="root", password=None):
        self.close()

        ssh = paramiko.SSHClient()

        # No aceptar claves de host desconocidas automáticamente.
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

        ssh.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )

        self.ssh = ssh
        self.sftp = ssh.open_sftp()

    def execute(self, command):
        if self.ssh is None:
            raise RuntimeError("SSH connection is not established")

        stdin, stdout, stderr = self.ssh.exec_command(command)

        exit_code = stdout.channel.recv_exit_status()

        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")

        if exit_code != 0:
            raise RuntimeError(
                f"Remote command failed ({exit_code}): {error.strip()}"
            )

        return output.strip()

    def download(self, remote_path, local_path):
        if self.sftp is None:
            raise RuntimeError("SFTP connection is not established")

        self.sftp.get(remote_path, str(local_path))

    def upload(self, local_path, remote_path):
        if self.sftp is None:
            raise RuntimeError("SFTP connection is not established")

        self.sftp.put(str(local_path), remote_path)

    def close(self):
        if self.sftp is not None:
            self.sftp.close()
            self.sftp = None

        if self.ssh is not None:
            self.ssh.close()
            self.ssh = None