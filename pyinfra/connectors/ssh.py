"""
Connect to hosts over SSH. This is the default connector and all targets default
to this meaning you do not need to specify it - ie the following two commands
are identical:

.. code:: shell

    pyinfra my-host.net ...
    pyinfra @ssh/my-host.net ...
"""

from __future__ import annotations

import shlex
from distutils.spawn import find_executable
from socket import error as socket_error, gaierror
from typing import TYPE_CHECKING, Any, Iterable, Optional, Tuple

import click
from paramiko import AuthenticationException, BadHostKeyException, SFTPClient, SSHException
from typing_extensions import Unpack

from pyinfra import logger
from pyinfra.api.command import QuoteString, StringCommand
from pyinfra.api.exceptions import ConnectError
from pyinfra.api.util import get_file_io, memoize

from .base import BaseConnector, make_keys
from .ssh_util import get_private_key, raise_connect_error
from .sshuserclient import SSHClient
from .util import (
    CommandOutput,
    execute_command_with_sudo_retry,
    make_unix_command_for_host,
    read_output_buffers,
    run_local_process,
    write_stdin,
)

if TYPE_CHECKING:
    from pyinfra.api.arguments import ConnectorArguments


class DataKeys:
    hostname = "SSH hostname"
    port = "SSH port"

    user = "User to SSH as"
    password = "Password to use for authentication"
    key = "Key file to use for authentication"
    key_password = "Key file password"

    allow_agent = "Allow using SSH agent"
    look_for_keys = "Allow looking up users keys"

    forward_agent = "Enable SSH forward agent"
    config_file = "Custom SSH config file"
    known_hosts_file = "Custom SSH known hosts file"
    strict_host_key_checking = "Override strict host keys check setting"

    paramiko_connect_kwargs = (
        "Override keyword arguments passed into paramiko's `SSHClient.connect`"
    )


DATA_KEYS = make_keys("ssh", DataKeys)


class SSHConnector(BaseConnector):
    handles_execution = True
    client: Optional[SSHClient] = None

    def make_names_data(hostname):
        yield "@ssh/{0}".format(hostname), {DATA_KEYS.hostname: hostname}, []

    def make_paramiko_kwargs(self) -> dict[str, Any]:
        kwargs = {
            "allow_agent": False,
            "look_for_keys": False,
            "hostname": self.host.data.get(DATA_KEYS.hostname, self.host.name),
            # Overrides of SSH config via pyinfra host data
            "_pyinfra_ssh_forward_agent": self.host.data.get(DATA_KEYS.forward_agent),
            "_pyinfra_ssh_config_file": self.host.data.get(DATA_KEYS.config_file),
            "_pyinfra_ssh_known_hosts_file": self.host.data.get(DATA_KEYS.known_hosts_file),
            "_pyinfra_ssh_strict_host_key_checking": self.host.data.get(
                DATA_KEYS.strict_host_key_checking
            ),
            "_pyinfra_ssh_paramiko_connect_kwargs": self.host.data.get(
                DATA_KEYS.paramiko_connect_kwargs
            ),
        }

        for key, value in (
            ("username", self.host.data.get(DATA_KEYS.user)),
            ("port", int(self.host.data.get(DATA_KEYS.port, 0))),
            ("timeout", self.state.config.CONNECT_TIMEOUT),
        ):
            if value:
                kwargs[key] = value

        # Password auth (boo!)
        ssh_password = self.host.data.get(DATA_KEYS.password)
        if ssh_password:
            kwargs["password"] = ssh_password

        # Key auth!
        ssh_key = self.host.data.get(DATA_KEYS.key)
        if ssh_key:
            kwargs["pkey"] = get_private_key(
                self.state,
                key_filename=ssh_key,
                key_password=self.host.data.get(DATA_KEYS.key_password),
            )

        # No key or password, so let's have paramiko look for SSH agents and user keys
        # unless disabled by the user.
        else:
            kwargs["allow_agent"] = self.host.data.get(DATA_KEYS.allow_agent, True)
            kwargs["look_for_keys"] = self.host.data.get(DATA_KEYS.look_for_keys, True)

        return kwargs

    def connect(self) -> None:
        """
        Connect to a single host. Returns the SSH client if successful. Stateless by
        design so can be run in parallel.
        """

        kwargs = self.make_paramiko_kwargs()
        logger.debug("Connecting to: %s (%r)", self.host.name, kwargs)
        hostname = kwargs.pop("hostname")

        self.client = SSHClient()

        try:
            self.client.connect(hostname, **kwargs)
        except AuthenticationException as e:
            auth_kwargs = {}

            for key, value in kwargs.items():
                if key in ("username", "password"):
                    auth_kwargs[key] = value
                    continue

                if key == "pkey" and value:
                    auth_kwargs["key"] = self.host.data.get(DATA_KEYS.key)

            auth_args = ", ".join(
                "{0}={1}".format(key, value) for key, value in auth_kwargs.items()
            )

            raise_connect_error(self.host, "Authentication error ({0})".format(auth_args), e)

        except BadHostKeyException as e:
            remove_entry = e.hostname
            port = self.client._ssh_config.get("port", 22)
            if port != 22:
                remove_entry = f"[{e.hostname}]:{port}"

            logger.warning("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!")
            logger.warning(
                ("Someone could be eavesdropping on you right now " "(man-in-the-middle attack)!"),
            )
            logger.warning("If this is expected, you can remove the bad key using:")
            logger.warning(f"    ssh-keygen -R {remove_entry}")

            raise_connect_error(
                self.host,
                "SSH host key error",
                f"Host key for {e.hostname} does not match.",
            )

        except SSHException as e:
            raise_connect_error(self.host, "SSH error", e)

        except gaierror:
            raise_connect_error(self.host, "Could not resolve hostname", hostname)

        except socket_error as e:
            raise_connect_error(self.host, "Could not connect", e)

        except EOFError as e:
            raise_connect_error(self.host, "EOF error", e)

    def run_shell_command(
        self,
        command: StringCommand,
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack["ConnectorArguments"],
    ) -> Tuple[bool, CommandOutput]:
        """
        Execute a command on the specified host.

        Args:
            state (``pyinfra.api.State`` obj): state object for this command
            hostname (string): hostname of the target
            command (string): actual command to execute
            sudo (boolean): whether to wrap the command with sudo
            sudo_user (string): user to sudo to
            get_pty (boolean): whether to get a PTY before executing the command
            env (dict): environment variables to set
            timeout (int): timeout for this command to complete before erroring

        Returns:
            tuple: (exit_code, stdout, stderr)
            stdout and stderr are both lists of strings from each buffer.
        """

        _get_pty = arguments.pop("_get_pty", False)
        _timeout = arguments.pop("_timeout", None)
        _stdin = arguments.pop("_stdin", None)
        _success_exit_codes = arguments.pop("_success_exit_codes", None)

        def execute_command() -> Tuple[int, CommandOutput]:
            unix_command = make_unix_command_for_host(self.state, self.host, command, **arguments)
            actual_command = unix_command.get_raw_value()

            logger.debug(
                "Running command on %s: (pty=%s) %s",
                self.host.name,
                _get_pty,
                unix_command,
            )

            if print_input:
                click.echo("{0}>>> {1}".format(self.host.print_prefix, unix_command), err=True)

            # Run it! Get stdout, stderr & the underlying channel
            assert self.client is not None
            stdin_buffer, stdout_buffer, stderr_buffer = self.client.exec_command(
                actual_command,
                get_pty=_get_pty,
            )

            if _stdin:
                write_stdin(_stdin, stdin_buffer)

            combined_output = read_output_buffers(
                stdout_buffer,
                stderr_buffer,
                timeout=_timeout,
                print_output=print_output,
                print_prefix=self.host.print_prefix,
            )

            logger.debug("Waiting for exit status...")
            exit_status = stdout_buffer.channel.recv_exit_status()
            logger.debug("Command exit status: %i", exit_status)

            return exit_status, combined_output

        return_code, combined_output = execute_command_with_sudo_retry(
            self.host,
            arguments,
            execute_command,
        )

        if _success_exit_codes:
            status = return_code in _success_exit_codes
        else:
            status = return_code == 0

        return status, combined_output

    @memoize
    def get_sftp_connection(self):
        assert self.client is not None
        transport = self.client.get_transport()
        assert transport is not None, "No transport"
        try:
            return SFTPClient.from_transport(transport)
        except SSHException as e:
            raise ConnectError(
                (
                    "Unable to establish SFTP connection. Check that the SFTP subsystem "
                    "for the SSH service at {0} is enabled."
                ).format(self.host),
            ) from e

    def _get_file(self, remote_filename: str, filename_or_io):
        with get_file_io(filename_or_io, "wb") as file_io:
            sftp = self.get_sftp_connection()
            sftp.getfo(remote_filename, file_io)

    def get_file(
        self,
        remote_filename: str,
        filename_or_io,
        remote_temp_filename=None,
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack["ConnectorArguments"],
    ) -> bool:
        """
        Download a file from the remote host using SFTP. Supports download files
        with sudo by copying to a temporary directory with read permissions,
        downloading and then removing the copy.
        """

        _sudo = arguments.get("_sudo", False)
        _su_user = arguments.get("_su_user", None)

        if _sudo or _su_user:
            # Get temp file location
            temp_file = remote_temp_filename or self.state.get_temp_filename(remote_filename)

            # Copy the file to the tempfile location and add read permissions
            command = StringCommand(
                "cp", remote_filename, temp_file, "&&", "chmod", "+r", temp_file
            )

            copy_status, output = self.run_shell_command(
                command,
                print_output=print_output,
                print_input=print_input,
                **arguments,
            )

            if copy_status is False:
                logger.error("File download copy temp error: {0}".format(output.stderr))
                return False

            try:
                self._get_file(temp_file, filename_or_io)

            # Ensure that, even if we encounter an error, we (attempt to) remove the
            # temporary copy of the file.
            finally:
                remove_status, output = self.run_shell_command(
                    StringCommand("rm", "-f", temp_file),
                    print_output=print_output,
                    print_input=print_input,
                    **arguments,
                )

            if remove_status is False:
                logger.error("File download remove temp error: {0}".format(output.stderr))
                return False

        else:
            self._get_file(remote_filename, filename_or_io)

        if print_output:
            click.echo(
                "{0}file downloaded: {1}".format(self.host.print_prefix, remote_filename),
                err=True,
            )

        return True

    def _put_file(self, filename_or_io, remote_location):
        logger.debug("Attempting upload of %s to %s", filename_or_io, remote_location)

        attempts = 0
        last_e = None

        while attempts < 3:
            try:
                with get_file_io(filename_or_io) as file_io:
                    sftp = self.get_sftp_connection()
                    sftp.putfo(file_io, remote_location)
                return
            except OSError as e:
                logger.warning(f"Failed to upload file, retrying: {e}")
                attempts += 1
                last_e = e

        if last_e is not None:
            raise last_e

    def put_file(
        self,
        filename_or_io,
        remote_filename,
        remote_temp_filename=None,
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack["ConnectorArguments"],
    ) -> bool:
        """
        Upload file-ios to the specified host using SFTP. Supports uploading files
        with sudo by uploading to a temporary directory then moving & chowning.
        """

        original_arguments = arguments.copy()

        _sudo = arguments.pop("_sudo", False)
        _sudo_user = arguments.pop("_sudo_user", False)
        _doas = arguments.pop("_doas", False)
        _doas_user = arguments.pop("_doas_user", False)
        _su_user = arguments.pop("_su_user", None)

        # sudo/su are a little more complicated, as you can only sftp with the SSH
        # user connected, so upload to tmp and copy/chown w/sudo and/or su_user
        if _sudo or _doas or _su_user:
            # Get temp file location
            temp_file = remote_temp_filename or self.state.get_temp_filename(remote_filename)
            self._put_file(filename_or_io, temp_file)

            # Make sure our sudo/su user can access the file
            if _su_user:
                command = StringCommand("setfacl", "-m", "u:{0}:r".format(_su_user), temp_file)
            elif _sudo_user:
                command = StringCommand("setfacl", "-m", "u:{0}:r".format(_sudo_user), temp_file)
            elif _doas_user:
                command = StringCommand("setfacl", "-m", "u:{0}:r".format(_doas_user), temp_file)

            if _su_user or _sudo_user or _doas_user:
                status, output = self.run_shell_command(
                    command,
                    print_output=print_output,
                    print_input=print_input,
                    **arguments,
                )

                if status is False:
                    logger.error("Error on handover to sudo/su user: {0}".format(output.stderr))
                    return False

            # Execute run_shell_command w/sudo, etc
            command = StringCommand("cp", temp_file, QuoteString(remote_filename))

            status, output = self.run_shell_command(
                command,
                print_output=print_output,
                print_input=print_input,
                **original_arguments,
            )

            if status is False:
                logger.error("File upload error: {0}".format(output.stderr))
                return False

            # Delete the temporary file now that we've successfully copied it
            command = StringCommand("rm", "-f", temp_file)

            status, output = self.run_shell_command(
                command,
                print_output=print_output,
                print_input=print_input,
                **arguments,
            )

            if status is False:
                logger.error("Unable to remove temporary file: {0}".format(output.stderr))
                return False

        # No sudo and no su_user, so just upload it!
        else:
            self._put_file(filename_or_io, remote_filename)

        if print_output:
            click.echo(
                "{0}file uploaded: {1}".format(self.host.print_prefix, remote_filename),
                err=True,
            )

        return True

    def check_can_rsync(self):
        if self.host.data.get(DATA_KEYS.key_password):
            raise NotImplementedError(
                "Rsync does not currently work with SSH keys needing passwords."
            )

        if self.host.data.get(DATA_KEYS.password):
            raise NotImplementedError("Rsync does not currently work with SSH passwords.")

        if not find_executable("rsync"):
            raise NotImplementedError("The `rsync` binary is not available on this system.")

    def rsync(
        self,
        src: str,
        dest: str,
        flags: Iterable[str],
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack["ConnectorArguments"],
    ):
        _sudo = arguments.pop("_sudo", False)
        _sudo_user = arguments.pop("_sudo_user", False)

        hostname = self.host.data.get(DATA_KEYS.hostname, self.host.name)
        user = self.host.data.get(DATA_KEYS.user, "")
        if user:
            user = "{0}@".format(user)

        ssh_flags = []
        # To avoid asking for interactive input, specify BatchMode=yes
        ssh_flags.append("-o BatchMode=yes")

        known_hosts_file = self.host.data.get(DATA_KEYS.known_hosts_file, "")
        if known_hosts_file:
            ssh_flags.append(
                '-o \\"UserKnownHostsFile={0}\\"'.format(shlex.quote(known_hosts_file))
            )  # never trust users

        strict_host_key_checking = self.host.data.get(DATA_KEYS.strict_host_key_checking, "")
        if strict_host_key_checking:
            ssh_flags.append(
                '-o \\"StrictHostKeyChecking={0}\\"'.format(shlex.quote(strict_host_key_checking))
            )

        ssh_config_file = self.host.data.get(DATA_KEYS.config_file, "")
        if ssh_config_file:
            ssh_flags.append("-F {0}".format(shlex.quote(ssh_config_file)))

        port = self.host.data.get(DATA_KEYS.port)
        if port:
            ssh_flags.append("-p {0}".format(port))

        ssh_key = self.host.data.get(DATA_KEYS.key)
        if ssh_key:
            ssh_flags.append("-i {0}".format(ssh_key))

        remote_rsync_command = "rsync"
        if _sudo:
            remote_rsync_command = "sudo rsync"
            if _sudo_user:
                remote_rsync_command = "sudo -u {0} rsync".format(_sudo_user)

        rsync_command = (
            "rsync {rsync_flags} "
            '--rsh "ssh {ssh_flags}" '
            "--rsync-path '{remote_rsync_command}' "
            "{src} {user}{hostname}:{dest}"
        ).format(
            rsync_flags=" ".join(flags),
            ssh_flags=" ".join(ssh_flags),
            remote_rsync_command=remote_rsync_command,
            user=user,
            hostname=hostname,
            src=src,
            dest=dest,
        )

        if print_input:
            click.echo("{0}>>> {1}".format(self.host.print_prefix, rsync_command), err=True)

        return_code, output = run_local_process(
            rsync_command,
            print_output=print_output,
            print_prefix=self.host.print_prefix,
        )

        status = return_code == 0
        if not status:
            raise IOError(output.stderr)

        return True
