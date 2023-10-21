"""
The ``@docker`` connector allows you to build Docker images, or modify running
Docker containers, using ``pyinfra``. You can pass either an image name or
existing container ID:

+ Image - will create a container from the image, execute operations and save
    into a new image
+ Existing container ID - will simply execute operations against the container,
    leaving it up afterwards


.. code:: shell

    # A Docker base image must be provided
    pyinfra @docker/alpine:3.8 ...

    # pyinfra can run on multiple Docker images in parallel
    pyinfra @docker/alpine:3.8,@docker/ubuntu:bionic ...

    # Execute against a running container
    pyinfra @docker/2beb8c15a1b1 ...
"""

from __future__ import annotations

import json
import os
from tempfile import mkstemp
from typing import TYPE_CHECKING

import click
from typing_extensions import Unpack

from pyinfra import local, logger
from pyinfra.api import QuoteString, StringCommand
from pyinfra.api.exceptions import ConnectError, InventoryError, PyinfraError
from pyinfra.api.util import get_file_io
from pyinfra.progress import progress_spinner

from .base import BaseConnector, make_keys
from .local import LocalConnector
from .util import CommandOutput, extract_control_arguments, make_unix_command_for_host

if TYPE_CHECKING:
    from pyinfra.api.arguments import ConnectorArguments
    from pyinfra.api.host import Host
    from pyinfra.api.state import State


class DataKeys:
    identifier = "ID of container or image to target"
    container_id = "ID of container to target, overrides ``docker_identifier``"


DATA_KEYS = make_keys("docker", DataKeys)


def _find_start_docker_container(container_id):
    docker_info = local.shell("docker container inspect {0}".format(container_id))
    docker_info = json.loads(docker_info)[0]
    if docker_info["State"]["Running"] is False:
        logger.info("Starting stopped container: {0}".format(container_id))
        local.shell("docker container start {0}".format(container_id))


def _start_docker_image(image_name):
    try:
        return local.shell(
            "docker run -d {0} tail -f /dev/null".format(image_name),
            splitlines=True,
        )[
            -1
        ]  # last line is the container ID
    except PyinfraError as e:
        raise ConnectError(e.args[0])


class DockerConnector(BaseConnector):
    handles_execution = True
    data_keys = DATA_KEYS

    local: LocalConnector

    def __init__(self, state: "State", host: "Host"):
        super().__init__(state, host)
        self.local = LocalConnector(state, host)

    @staticmethod
    def make_names_data(identifier=None):
        if not identifier:
            raise InventoryError("No docker base ID provided!")

        yield (
            "@docker/{0}".format(identifier),
            {DATA_KEYS.identifier: identifier},
            ["@docker"],
        )

    def connect(self):
        self.local.connect()

        docker_container_id = self.host.data.get(DATA_KEYS.container_id)
        if docker_container_id:  # user can provide a docker_container_id
            self.host.connector_data["docker_container_no_disconnect"] = True
            self.host.connector_data["docker_container_id"] = docker_container_id
            return True

        docker_identifier = getattr(self.host.data, DATA_KEYS.identifier)
        with progress_spinner({"prepare docker container"}):
            try:
                # Check if the provided @docker/X is an existing container ID
                _find_start_docker_container(docker_identifier)
            except PyinfraError:
                container_id = _start_docker_image(docker_identifier)
            else:
                container_id = docker_identifier
                self.host.connector_data["docker_container_no_disconnect"] = True

        self.host.connector_data["docker_container_id"] = container_id
        return True

    def disconnect(self):
        container_id = self.host.connector_data["docker_container_id"]

        if self.host.connector_data.get("docker_container_no_disconnect"):
            logger.info(
                "{0}docker build complete, container left running: {1}".format(
                    self.host.print_prefix,
                    click.style(container_id, bold=True),
                ),
            )
            return

        with progress_spinner({"docker commit"}):
            image_id = local.shell("docker commit {0}".format(container_id), splitlines=True)[-1][
                7:19
            ]  # last line is the image ID, get sha256:[XXXXXXXXXX]...

        with progress_spinner({"docker rm"}):
            local.shell(
                "docker rm -f {0}".format(container_id),
            )

        logger.info(
            "{0}docker build complete, image ID: {1}".format(
                self.host.print_prefix,
                click.style(image_id, bold=True),
            ),
        )

    def run_shell_command(
        self,
        command: StringCommand,
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack["ConnectorArguments"],
    ) -> tuple[bool, CommandOutput]:
        local_arguments = extract_control_arguments(arguments)

        container_id = self.host.connector_data["docker_container_id"]

        command = make_unix_command_for_host(self.state, self.host, command, **arguments)
        command = StringCommand(QuoteString(command))

        docker_flags = "-it" if local_arguments.get("_get_pty") else "-i"
        docker_command = StringCommand(
            "docker",
            "exec",
            docker_flags,
            container_id,
            "sh",
            "-c",
            command,
        )

        return self.local.run_shell_command(
            docker_command,
            print_output=print_output,
            print_input=print_input,
            **local_arguments,
        )

    def put_file(
        self,
        filename_or_io,
        remote_filename,
        remote_temp_filename=None,  # ignored
        print_output=False,
        print_input=False,
        **kwargs,  # ignored (sudo/etc)
    ) -> bool:
        """
        Upload a file/IO object to the target Docker container by copying it to a
        temporary location and then uploading it into the container using ``docker cp``.
        """

        fd, temp_filename = mkstemp()

        try:
            # Load our file or IO object and write it to the temporary file
            with get_file_io(filename_or_io) as file_io:
                with open(temp_filename, "wb") as temp_f:
                    data = file_io.read()

                    if isinstance(data, str):
                        data = data.encode()

                    temp_f.write(data)

            docker_id = self.host.connector_data["docker_container_id"]
            docker_command = "docker cp {0} {1}:{2}".format(
                temp_filename,
                docker_id,
                remote_filename,
            )

            status, output = self.local.run_shell_command(
                docker_command,
                print_output=print_output,
                print_input=print_input,
            )
        finally:
            os.close(fd)
            os.remove(temp_filename)

        if not status:
            raise IOError(output.stderr)

        if print_output:
            click.echo(
                "{0}file uploaded to container: {1}".format(
                    self.host.print_prefix,
                    remote_filename,
                ),
                err=True,
            )

        return status

    def get_file(
        self,
        remote_filename,
        filename_or_io,
        remote_temp_filename=None,  # ignored
        print_output=False,
        print_input=False,
        **kwargs,  # ignored (sudo/etc)
    ) -> bool:
        """
        Download a file from the target Docker container by copying it to a temporary
        location and then reading that into our final file/IO object.
        """

        fd, temp_filename = mkstemp()

        try:
            docker_id = self.host.connector_data["docker_container_id"]
            docker_command = "docker cp {0}:{1} {2}".format(
                docker_id,
                remote_filename,
                temp_filename,
            )

            status, output = self.local.run_shell_command(
                docker_command,
                print_output=print_output,
                print_input=print_input,
            )

            # Load the temporary file and write it to our file or IO object
            with open(temp_filename, encoding="utf-8") as temp_f:
                with get_file_io(filename_or_io, "wb") as file_io:
                    data = temp_f.read()
                    data_bytes: bytes

                    if isinstance(data, str):
                        data_bytes = data.encode()
                    else:
                        data_bytes = data

                    file_io.write(data_bytes)
        finally:
            os.close(fd)
            os.remove(temp_filename)

        if not status:
            raise IOError(output.stderr)

        if print_output:
            click.echo(
                "{0}file downloaded from container: {1}".format(
                    self.host.print_prefix,
                    remote_filename,
                ),
                err=True,
            )

        return status
