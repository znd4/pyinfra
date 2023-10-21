from collections import defaultdict
from os import path
from unittest import TestCase
from unittest.mock import mock_open, patch

import pyinfra
from pyinfra.api import (
    BaseStateCallback,
    Config,
    FileDownloadCommand,
    FileUploadCommand,
    OperationError,
    OperationValueError,
    State,
    StringCommand,
)
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.operation import OperationMeta, add_op
from pyinfra.api.operations import run_ops
from pyinfra.api.state import StateOperationMeta
from pyinfra.context import ctx_host, ctx_state
from pyinfra.operations import files, python, server

from ..paramiko_util import FakeBuffer, FakeChannel, PatchSSHTestCase
from ..util import make_inventory


class TestOperationMeta(TestCase):
    def test_operation_meta_repr_no_change(self):
        op_meta = OperationMeta("hash", False)
        assert repr(op_meta) == "OperationMeta(changed=False, hash=hash)"

    def test_operation_meta_repr_changes(self):
        op_meta = OperationMeta("hash", True)
        assert repr(op_meta) == "OperationMeta(changed=True, hash=hash)"


class TestOperationsApi(PatchSSHTestCase):
    def test_op(self):
        inventory = make_inventory()
        somehost = inventory.get_host("somehost")
        anotherhost = inventory.get_host("anotherhost")

        state = State(inventory, Config())
        state.add_callback_handler(BaseStateCallback())

        # Enable printing on this test to catch any exceptions in the formatting
        state.print_output = True
        state.print_input = True
        state.print_fact_info = True
        state.print_noop_info = True

        connect_all(state)

        add_op(
            state,
            files.file,
            "/var/log/pyinfra.log",
            user="pyinfra",
            group="pyinfra",
            mode="644",
            create_remote_dir=False,
            _sudo=True,
            _sudo_user="test_sudo",
            _su_user="test_su",
            _ignore_errors=True,
            _env={
                "TEST": "what",
            },
        )

        op_order = state.get_op_order()

        # Ensure we have an op
        assert len(op_order) == 1

        first_op_hash = op_order[0]

        # Ensure the op name
        assert state.op_meta[first_op_hash].names == {"Files/File"}

        # Ensure the global kwargs (same for both hosts)
        somehost_global_arguments = state.ops[somehost][first_op_hash].global_arguments
        assert somehost_global_arguments["_sudo"] is True
        assert somehost_global_arguments["_sudo_user"] == "test_sudo"
        assert somehost_global_arguments["_su_user"] == "test_su"
        assert somehost_global_arguments["_ignore_errors"] is True

        anotherhost_global_arguments = state.ops[anotherhost][first_op_hash].global_arguments
        assert anotherhost_global_arguments["_sudo"] is True
        assert anotherhost_global_arguments["_sudo_user"] == "test_sudo"
        assert anotherhost_global_arguments["_su_user"] == "test_su"
        assert anotherhost_global_arguments["_ignore_errors"] is True

        # Ensure run ops works
        run_ops(state)

        # Ensure the commands
        assert state.ops[somehost][first_op_hash].operation_meta.commands == [
            StringCommand("touch /var/log/pyinfra.log"),
            StringCommand("chmod 644 /var/log/pyinfra.log"),
            StringCommand("chown pyinfra:pyinfra /var/log/pyinfra.log"),
        ]

        # Ensure ops completed OK
        assert state.results[somehost].success_ops == 1
        assert state.results[somehost].ops == 1
        assert state.results[anotherhost].success_ops == 1
        assert state.results[anotherhost].ops == 1

        # And w/o errors
        assert state.results[somehost].error_ops == 0
        assert state.results[anotherhost].error_ops == 0

        # And with the different modes
        run_ops(state, serial=True)
        run_ops(state, no_wait=True)

        disconnect_all(state)

    @patch("pyinfra.api.util.open", mock_open(read_data="test!"), create=True)
    @patch("pyinfra.operations.files.os.path.isfile", lambda *args, **kwargs: True)
    def test_file_upload_op(self):
        inventory = make_inventory()

        state = State(inventory, Config())
        connect_all(state)

        # Test normal
        add_op(
            state,
            files.put,
            name="First op name",
            src="files/file.txt",
            dest="/home/vagrant/file.txt",
        )

        # And with sudo
        add_op(
            state,
            files.put,
            src="files/file.txt",
            dest="/home/vagrant/file.txt",
            _sudo=True,
            _sudo_user="pyinfra",
        )

        # And with su
        add_op(
            state,
            files.put,
            src="files/file.txt",
            dest="/home/vagrant/file.txt",
            _sudo=True,
            _su_user="pyinfra",
        )

        op_order = state.get_op_order()

        # Ensure we have all ops
        assert len(op_order) == 3

        first_op_hash = op_order[0]
        second_op_hash = op_order[1]

        # Ensure first op is the right one
        assert state.op_meta[first_op_hash].names == {"First op name"}

        somehost = inventory.get_host("somehost")
        anotherhost = inventory.get_host("anotherhost")

        # Ensure second op has sudo/sudo_user
        assert state.ops[somehost][second_op_hash].global_arguments["_sudo"] is True
        assert state.ops[somehost][second_op_hash].global_arguments["_sudo_user"] == "pyinfra"

        # Ensure third has su_user
        assert state.ops[somehost][op_order[2]].global_arguments["_su_user"] == "pyinfra"

        # Check run ops works
        run_ops(state)

        # Ensure first op used the right (upload) command
        assert state.ops[somehost][first_op_hash].operation_meta.commands == [
            StringCommand("mkdir -p /home/vagrant"),
            FileUploadCommand("files/file.txt", "/home/vagrant/file.txt"),
        ]

        # Ensure ops completed OK
        assert state.results[somehost].success_ops == 3
        assert state.results[somehost].ops == 3
        assert state.results[anotherhost].success_ops == 3
        assert state.results[anotherhost].ops == 3

        # And w/o errors
        assert state.results[somehost].error_ops == 0
        assert state.results[anotherhost].error_ops == 0

    def test_file_download_op(self):
        inventory = make_inventory()

        state = State(inventory, Config())
        connect_all(state)

        with patch("pyinfra.operations.files.os.path.isfile", lambda *args, **kwargs: True):
            add_op(
                state,
                files.get,
                name="First op name",
                src="/home/vagrant/file.txt",
                dest="files/file.txt",
            )

        op_order = state.get_op_order()

        assert len(op_order) == 1

        first_op_hash = op_order[0]
        assert state.op_meta[first_op_hash].names == {"First op name"}

        somehost = inventory.get_host("somehost")
        anotherhost = inventory.get_host("anotherhost")

        with patch("pyinfra.api.util.open", mock_open(read_data="test!"), create=True):
            run_ops(state)

        # Ensure first op has the right (upload) command
        assert state.ops[somehost][first_op_hash].operation_meta.commands == [
            FileDownloadCommand("/home/vagrant/file.txt", "files/file.txt"),
        ]

        assert state.results[somehost].success_ops == 1
        assert state.results[somehost].ops == 1
        assert state.results[anotherhost].success_ops == 1
        assert state.results[anotherhost].ops == 1
        assert state.results[somehost].error_ops == 0
        assert state.results[anotherhost].error_ops == 0

    def test_function_call_op(self):
        inventory = make_inventory()
        state = State(inventory, Config())
        connect_all(state)

        is_called = []

        def mocked_function(*args, **kwargs):
            is_called.append(True)
            return None

        # Add op to both hosts
        add_op(state, python.call, mocked_function)

        # Ensure there is one op
        assert len(state.get_op_order()) == 1

        run_ops(state)

        assert is_called

    def test_run_once_serial_op(self):
        inventory = make_inventory()
        state = State(inventory, Config())
        connect_all(state)

        # Add a run once op
        add_op(state, server.shell, 'echo "hi"', _run_once=True, _serial=True)

        # Ensure it's added to op_order
        assert len(state.get_op_order()) == 1

        somehost = inventory.get_host("somehost")
        anotherhost = inventory.get_host("anotherhost")

        # Ensure between the two hosts we only run the one op
        assert len(state.ops[somehost]) + len(state.ops[anotherhost]) == 1

        # Check run works
        run_ops(state)

        assert (state.results[somehost].success_ops + state.results[anotherhost].success_ops) == 1

    @patch("pyinfra.connectors.ssh.SSHConnector.check_can_rsync", lambda _: True)
    def test_rsync_op(self):
        inventory = make_inventory(hosts=("somehost",))
        state = State(inventory, Config())
        connect_all(state)

        add_op(state, files.rsync, "src", "dest", _sudo=True, _sudo_user="root")

        assert len(state.get_op_order()) == 1

        with patch("pyinfra.connectors.ssh.run_local_process") as fake_run_local_process:
            fake_run_local_process.return_value = 0, []
            run_ops(state)

        fake_run_local_process.assert_called_with(
            (
                "rsync -ax --delete --rsh "
                '"ssh -o BatchMode=yes"'
                " --rsync-path 'sudo -u root rsync' src vagrant@somehost:dest"
            ),
            print_output=False,
            print_prefix=inventory.get_host("somehost").print_prefix,
        )

    @patch("pyinfra.connectors.ssh.SSHConnector.check_can_rsync", lambda _: True)
    def test_rsync_op_with_strict_host_key_checking_disabled(self):
        inventory = make_inventory(hosts=(("somehost", {"ssh_strict_host_key_checking": "no"}),))
        state = State(inventory, Config())
        connect_all(state)

        add_op(state, files.rsync, "src", "dest", _sudo=True, _sudo_user="root")

        assert len(state.get_op_order()) == 1

        with patch("pyinfra.connectors.ssh.run_local_process") as fake_run_local_process:
            fake_run_local_process.return_value = 0, []
            run_ops(state)

        fake_run_local_process.assert_called_with(
            (
                "rsync -ax --delete --rsh "
                '"ssh -o BatchMode=yes -o \\"StrictHostKeyChecking=no\\""'
                " --rsync-path 'sudo -u root rsync' src vagrant@somehost:dest"
            ),
            print_output=False,
            print_prefix=inventory.get_host("somehost").print_prefix,
        )

    @patch("pyinfra.connectors.ssh.SSHConnector.check_can_rsync", lambda _: True)
    def test_rsync_op_with_strict_host_key_checking_disabled_and_custom_config_file(self):
        inventory = make_inventory(
            hosts=(
                (
                    "somehost",
                    {
                        "ssh_strict_host_key_checking": "no",
                        "ssh_config_file": "/home/me/ssh_test_config",
                    },
                ),
            )
        )
        state = State(inventory, Config())
        connect_all(state)

        add_op(state, files.rsync, "src", "dest", _sudo=True, _sudo_user="root")

        assert len(state.get_op_order()) == 1

        with patch("pyinfra.connectors.ssh.run_local_process") as fake_run_local_process:
            fake_run_local_process.return_value = 0, []
            run_ops(state)

        fake_run_local_process.assert_called_with(
            (
                "rsync -ax --delete --rsh "
                '"ssh -o BatchMode=yes '
                '-o \\"StrictHostKeyChecking=no\\" -F /home/me/ssh_test_config"'
                " --rsync-path 'sudo -u root rsync' src vagrant@somehost:dest"
            ),
            print_output=False,
            print_prefix=inventory.get_host("somehost").print_prefix,
        )

    @patch("pyinfra.connectors.ssh.SSHConnector.check_can_rsync", lambda _: True)
    def test_rsync_op_with_sanitized_custom_config_file(self):
        inventory = make_inventory(
            hosts=(("somehost", {"ssh_config_file": "/home/me/ssh_test_config && echo hi"}),)
        )
        state = State(inventory, Config())
        connect_all(state)

        add_op(state, files.rsync, "src", "dest", _sudo=True, _sudo_user="root")

        assert len(state.get_op_order()) == 1

        with patch("pyinfra.connectors.ssh.run_local_process") as fake_run_local_process:
            fake_run_local_process.return_value = 0, []
            run_ops(state)

        fake_run_local_process.assert_called_with(
            (
                "rsync -ax --delete --rsh "
                "\"ssh -o BatchMode=yes -F '/home/me/ssh_test_config && echo hi'\""
                " --rsync-path 'sudo -u root rsync' src vagrant@somehost:dest"
            ),
            print_output=False,
            print_prefix=inventory.get_host("somehost").print_prefix,
        )

    def test_rsync_op_failure(self):
        inventory = make_inventory(hosts=("somehost",))
        state = State(inventory, Config())
        connect_all(state)

        with patch("pyinfra.connectors.ssh.find_executable", lambda x: None):
            with self.assertRaises(OperationError) as context:
                add_op(state, files.rsync, "src", "dest")

        assert context.exception.args[0] == "The `rsync` binary is not available on this system."

    def test_op_cannot_change_execution_kwargs(self):
        inventory = make_inventory()

        state = State(inventory, Config())

        class NoSetDefaultDict(defaultdict):
            def setdefault(self, key, _):
                return self[key]

        op_meta_item = StateOperationMeta(tuple())
        op_meta_item.global_arguments = {"_serial": True}
        state.op_meta = NoSetDefaultDict(lambda: op_meta_item)

        connect_all(state)

        with self.assertRaises(OperationValueError) as context:
            add_op(state, files.file, "/var/log/pyinfra.log", _serial=False)

        assert context.exception.args[0] == "Cannot have different values for `_serial`."


class TestNestedOperationsApi(PatchSSHTestCase):
    def test_nested_op_api(self):
        inventory = make_inventory()
        state = State(inventory, Config())

        connect_all(state)

        somehost = inventory.get_host("somehost")

        ctx_state.set(state)
        ctx_host.set(somehost)

        pyinfra.is_cli = True

        try:
            outer_result = server.shell(commands="echo outer")
            assert outer_result.combined_output_lines is None

            def callback():
                inner_result = server.shell(commands="echo inner")
                assert inner_result.combined_output_lines is not None

            python.call(function=callback)

            assert len(state.get_op_order()) == 2

            run_ops(state)

            assert len(state.get_op_order()) == 3
            assert state.results[somehost].success_ops == 3
            assert outer_result.combined_output_lines is not None

            disconnect_all(state)
        finally:
            pyinfra.is_cli = False


class TestOperationFailures(PatchSSHTestCase):
    def test_full_op_fail(self):
        inventory = make_inventory()
        state = State(inventory, Config())
        connect_all(state)

        add_op(state, server.shell, 'echo "hi"')

        with patch("pyinfra.connectors.ssh.SSHConnector.run_shell_command") as fake_run_command:
            fake_channel = FakeChannel(1)
            fake_run_command.return_value = (
                False,
                FakeBuffer("", fake_channel),
            )

            with self.assertRaises(PyinfraError) as e:
                run_ops(state)

            assert e.exception.args[0] == "No hosts remaining!"

            somehost = inventory.get_host("somehost")

            # Ensure the op was not flagged as success
            assert state.results[somehost].success_ops == 0
            # And was flagged asn an error
            assert state.results[somehost].error_ops == 1

    def test_ignore_errors_op_fail(self):
        inventory = make_inventory()
        state = State(inventory, Config())
        connect_all(state)

        add_op(state, server.shell, 'echo "hi"', _ignore_errors=True)

        with patch("pyinfra.connectors.ssh.SSHConnector.run_shell_command") as fake_run_command:
            fake_channel = FakeChannel(1)
            fake_run_command.return_value = (
                False,
                FakeBuffer("", fake_channel),
            )

            # This should run OK
            run_ops(state)

        somehost = inventory.get_host("somehost")

        # Ensure the op was added to results
        assert state.results[somehost].ops == 1
        assert state.results[somehost].ignored_error_ops == 1
        # But not as a success
        assert state.results[somehost].success_ops == 0


class TestOperationOrdering(PatchSSHTestCase):
    # In CLI mode, pyinfra uses *line numbers* to order operations as defined by
    # the user. This makes reasoning about user-written deploys simple and easy
    # to understand.
    def test_cli_op_line_numbers(self):
        inventory = make_inventory()
        state = State(inventory, Config())
        connect_all(state)

        state.current_deploy_filename = __file__

        pyinfra.is_cli = True
        ctx_state.set(state)

        # Add op to both hosts
        for name in ("anotherhost", "somehost"):
            ctx_host.set(inventory.get_host(name))
            server.shell("echo hi")  # note this is called twice but on *the same line*

        # Add op to just the second host - using the context modules such that
        # it replicates a deploy file.
        ctx_host.set(inventory.get_host("anotherhost"))
        first_context_hash = server.user("anotherhost_user").hash

        # Add op to just the first host - using the context modules such that
        # it replicates a deploy file.
        ctx_host.set(inventory.get_host("somehost"))
        second_context_hash = server.user("somehost_user").hash

        ctx_state.reset()
        ctx_host.reset()

        pyinfra.is_cli = False

        # Ensure there are two ops
        op_order = state.get_op_order()
        assert len(op_order) == 3

        # And that the two ops above were called in the expected order
        assert op_order[1] == first_context_hash
        assert op_order[2] == second_context_hash

        # Ensure somehost has two ops and anotherhost only has the one
        assert len(state.ops[inventory.get_host("somehost")]) == 2
        assert len(state.ops[inventory.get_host("anotherhost")]) == 2

    # In API mode, pyinfra *overrides* the line numbers such that whenever an
    # operation or deploy is added it is simply appended. This makes sense as
    # the user writing the API calls has full control over execution order.
    def test_api_op_line_numbers(self):
        inventory = make_inventory()
        state = State(inventory, Config())
        connect_all(state)

        another_host = inventory.get_host("anotherhost")

        def add_another_op():
            return add_op(state, server.shell, "echo second-op")[another_host].hash

        first_op_hash = add_op(state, server.shell, "echo first-op")[another_host].hash
        second_op_hash = add_another_op()  # note `add_op` will be called on an earlier line

        op_order = state.get_op_order()
        assert len(op_order) == 2

        assert op_order[0] == first_op_hash
        assert op_order[1] == second_op_hash


this_filename = path.join("tests", "test_api", "test_api_operations.py")
