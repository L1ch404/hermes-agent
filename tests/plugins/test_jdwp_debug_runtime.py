from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "jdwp-debug"
PACKAGE_NAME = "hermes_test_jdwp_debug"


def _load_plugin_package():
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin_module = _load_plugin_package()

from hermes_test_jdwp_debug.runtime.base import RuntimeAction, Variable  # noqa: E402
from hermes_test_jdwp_debug.runtime.java.jdwp import (  # noqa: E402
    Cmd,
    EventKind,
    IDSizes,
    JDWPClient,
    Tag,
)
from hermes_test_jdwp_debug.runtime.java.runtime import (  # noqa: E402
    JavaRuntime,
    SuspensionSnapshot,
)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    result = b""
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("socket closed")
        result += chunk
    return result


def test_plugin_runtime_is_isolated_per_hermes_session() -> None:
    plugin_module._runtimes.clear()

    first = json.loads(plugin_module._handle_java_runtime(
        {"action": "status"}, session_id="session-a"
    ))
    second = json.loads(plugin_module._handle_java_runtime(
        {"action": "status"}, session_id="session-b"
    ))

    assert first["process_state"] == "absent"
    assert second["process_state"] == "absent"
    assert set(plugin_module._runtimes) == {"session-a", "session-b"}
    assert plugin_module._runtimes["session-a"] is not plugin_module._runtimes["session-b"]


def test_handler_logs_observable_lifecycle_without_argument_values(caplog) -> None:
    plugin_module._runtimes.clear()
    caplog.set_level(logging.INFO)

    result = json.loads(plugin_module._handle_java_runtime(
        {
            "action": "status",
            "app_args": ["do-not-log-this-value"],
            "vm_args": ["-Dpassword=do-not-log-this-value"],
        },
        session_id="logging-session",
    ))

    assert result["process_state"] == "absent"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "java_runtime.session.created context=logging-session" in messages
    assert "java_runtime.action.start action=status context=logging-session" in messages
    assert "java_runtime.action.finish action=status context=logging-session" in messages
    assert "do-not-log-this-value" not in messages


def test_command_queues_interleaved_breakpoint_event(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client_sock, vm_sock = socket.socketpair()
    client = JDWPClient()
    client._sock = client_sock
    client.ids = IDSizes(8, 8, 8, 8, 8)
    event_data = struct.pack(">BI", 2, 1)
    event_data += struct.pack(">BI", EventKind.BREAKPOINT, 73)
    event_data += (101).to_bytes(8, "big")
    event_data += struct.pack(">B", 1)
    event_data += (202).to_bytes(8, "big")
    event_data += (303).to_bytes(8, "big")
    event_data += struct.pack(">Q", 404)
    event_packet = (
        struct.pack(">IIBBB", 11 + len(event_data), 900, 0, 64, 100)
        + event_data
    )

    def vm() -> None:
        command_header = _recv_exact(vm_sock, 11)
        command_length, command_id, _ = struct.unpack(">IIB", command_header[:9])
        _recv_exact(vm_sock, command_length - 11)
        vm_sock.sendall(event_packet)
        vm_sock.sendall(struct.pack(">IIBH", 11, command_id, 0x80, 0))

    thread = threading.Thread(target=vm)
    thread.start()
    try:
        error, data = client.command(Cmd.VM, 1)
        assert error == 0
        assert data == b""

        composite = client.wait_for_event(0.1)
        assert composite is not None
        assert composite["suspend_policy"] == 2
        assert composite["events"] == [{
            "kind": EventKind.BREAKPOINT,
            "request_id": 73,
            "thread_id": 101,
            "location": {
                "type_tag": 1,
                "class_id": 202,
                "method_id": 303,
                "index": 404,
            },
        }]
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "java_runtime.jdwp.command.send" in messages
        assert "java_runtime.jdwp.event.queued" in messages
        assert "java_runtime.jdwp.command.reply" in messages
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        client.close()
        vm_sock.close()


def test_breakpoint_clear_includes_event_kind() -> None:
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        def __init__(self):
            self.calls = []

        def command(self, command_set, command, data=b""):
            self.calls.append((command_set, command, data))
            return 0, b""

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._breakpoints = {17: {"line": 10}}
    client = FakeClient()
    runtime._connect = lambda: client

    result = runtime.breakpoint(RuntimeAction(action="breakpoint", bp_action="remove"))

    assert result.error == ""
    assert client.calls == [
        (Cmd.EVENT, 2, struct.pack(">BI", EventKind.BREAKPOINT, 17))
    ]


def _runtime_with_fake_variable_response(stack_error: int, stack_data: bytes):
    class FakeProcessManager:
        is_running = True

    class FakeClient:
        ids = IDSizes(8, 8, 8, 8, 8)

        def command(self, command_set, command, data=b""):
            if (command_set, command) == (Cmd.METHOD, 2):
                return 0, b"variable-table"
            if (command_set, command) == (Cmd.STACK, 1):
                return stack_error, stack_data
            raise AssertionError((command_set, command, data))

    runtime = JavaRuntime()
    runtime._proc = FakeProcessManager()
    runtime._active_suspension = SuspensionSnapshot(
        suspension_id="susp_test",
        generation=1,
        request_id=1,
        thread_id=10,
        location={},
        observed_at="2026-07-04T00:00:00+00:00",
    )
    runtime._connect = lambda: FakeClient()
    runtime._resolve_thread_id = lambda jdwp, snapshot, name: 10
    runtime._read_frames = lambda jdwp, thread_id, count, start_index=0: [{
        "index": 0,
        "frame_id": 20,
        "class_id": 30,
        "method_id": 40,
        "location_index": 50,
        "class": "LFixture;",
        "method": "run()V",
        "line": 10,
        "is_native": False,
    }]
    runtime._visible_variables_for_location = lambda data, location: [
        Variable(name="actualNull", type_name="Ljava/lang/String;", slot=1),
        Variable(name="notReturned", type_name="Ljava/lang/String;", slot=2),
    ]
    runtime._thread_name = lambda jdwp, thread_id: "main"
    return runtime


def test_getvalues_error_marks_every_variable_unavailable() -> None:
    runtime = _runtime_with_fake_variable_response(35, b"")

    result = runtime.variables(RuntimeAction(
        action="variables", suspension_id="susp_test"
    ))

    assert result.error == ""
    assert result.data["complete"] is False
    assert result.data["partial"] is True
    for variable in result.data["variables"]:
        assert variable["value_state"] == "unavailable"
        assert "value" not in variable
        assert "GetValues failed" in variable["error"]


def test_real_null_is_observed_but_missing_batch_value_is_unavailable() -> None:
    one_null_value = (
        struct.pack(">I", 1)
        + bytes([Tag.STRING])
        + (0).to_bytes(8, "big")
    )
    runtime = _runtime_with_fake_variable_response(0, one_null_value)

    result = runtime.variables(RuntimeAction(
        action="variables", suspension_id="susp_test"
    ))

    actual_null, not_returned = result.data["variables"]
    assert actual_null["value_state"] == "observed"
    assert actual_null["value"] is None
    assert "error" not in actual_null
    assert not_returned["value_state"] == "unavailable"
    assert "value" not in not_returned
    assert "JVM returned no value" in not_returned["error"]


@pytest.mark.skipif(
    shutil.which("java") is None or shutil.which("javac") is None,
    reason="JDK is required for the real JDWP integration test",
)
def test_real_jvm_breakpoint_snapshot_variables_and_resume(tmp_path: Path) -> None:
    source = """\
import java.nio.file.Files;
import java.nio.file.Path;

public class DebugFixture {
    public static void main(String[] args) throws Exception {
        Path trigger = Path.of(args[0]);
        while (!Files.exists(trigger)) {
            Thread.sleep(20);
        }
        String sex = "男";
        String missing = null;
        int answer = 42;
        System.out.println(sex + answer);
        Thread.sleep(30000);
    }
}
"""
    source_path = tmp_path / "DebugFixture.java"
    source_path.write_text(source, encoding="utf-8")
    subprocess.run(
        ["javac", "-g", str(source_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    breakpoint_line = next(
        index for index, line in enumerate(source.splitlines(), start=1)
        if "System.out.println" in line
    )
    trigger = tmp_path / "trigger"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    runtime = JavaRuntime()
    try:
        started = runtime.run(RuntimeAction(
            action="run",
            classpath=str(tmp_path),
            main_class="DebugFixture",
            app_args=[str(trigger)],
            jdwp_port=port,
        ))
        assert started.error == ""

        breakpoint = runtime.breakpoint(RuntimeAction(
            action="breakpoint",
            bp_action="set",
            class_pattern="DebugFixture",
            line=breakpoint_line,
        ))
        assert breakpoint.error == ""

        trigger.write_text("go", encoding="utf-8")
        hit = runtime.wait_breakpoint(RuntimeAction(
            action="wait_breakpoint",
            timeout=10,
        ))
        assert hit.error == ""
        assert hit.data["status"] == "breakpoint_hit"
        suspension_id = hit.data["suspension_id"]
        assert hit.data["location"]["line"] == breakpoint_line

        status = runtime.status(RuntimeAction(action="status"))
        assert status.data["debug_state"] == "suspended"
        assert status.data["suspension_id"] == suspension_id

        threads = runtime.threads(RuntimeAction(
            action="threads",
            suspension_id=suspension_id,
        ))
        assert threads.error == ""
        assert any(
            thread["name"] == "main" and thread["is_breakpoint_thread"]
            for thread in threads.data["threads"]
        )

        stack = runtime.stack(RuntimeAction(
            action="stack",
            suspension_id=suspension_id,
        ))
        assert stack.error == ""
        assert stack.data["frames"][0]["class"] == "LDebugFixture;"

        variables = runtime.variables(RuntimeAction(
            action="variables",
            suspension_id=suspension_id,
            frame_index=0,
        ))
        assert variables.error == ""
        values = {item["name"]: item["value"] for item in variables.data["variables"]}
        assert values["sex"] == "男"
        assert values["missing"] is None
        assert values["answer"] == 42
        missing = next(
            item for item in variables.data["variables"] if item["name"] == "missing"
        )
        assert missing["value_state"] == "observed"
        assert "error" not in missing
        assert "\\u7537" not in variables.to_json()

        resumed = runtime.resume(RuntimeAction(
            action="resume",
            suspension_id=suspension_id,
        ))
        assert resumed.error == ""
        assert resumed.data["invalidated_suspension_id"] == suspension_id

        stale = runtime.variables(RuntimeAction(
            action="variables",
            suspension_id=suspension_id,
        ))
        assert "No active breakpoint suspension" in stale.error
    finally:
        runtime.stop(RuntimeAction(action="stop"))


@pytest.mark.skipif(
    shutil.which("java") is None or shutil.which("javac") is None,
    reason="JDK is required for the real JDWP integration test",
)
def test_attach_and_detach_leave_external_jvm_running(tmp_path: Path) -> None:
    source_path = tmp_path / "AttachFixture.java"
    source_path.write_text(
        """\
import java.nio.file.Files;
import java.nio.file.Path;

public class AttachFixture {
    public static void main(String[] args) throws Exception {
        while (!Files.exists(Path.of(args[0]))) {
            Thread.sleep(20);
        }
    }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["javac", "-g", str(source_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop_file = tmp_path / "stop"
    process = subprocess.Popen(
        [
            "java",
            f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address={port}",
            "-cp",
            str(tmp_path),
            "AttachFixture",
            str(stop_file),
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    runtime = JavaRuntime()
    try:
        deadline = time.monotonic() + 10
        while True:
            attached = runtime.attach(RuntimeAction(
                action="attach",
                pid=process.pid,
                jdwp_port=port,
                main_class="AttachFixture",
            ))
            if not attached.error:
                break
            if time.monotonic() >= deadline:
                pytest.fail(attached.error)
            time.sleep(0.1)

        status = runtime.status(RuntimeAction(action="status"))
        assert status.data["ownership"] == "attached"

        detached = runtime.detach(RuntimeAction(action="detach"))
        assert detached.error == ""
        assert detached.data == {"status": "detached", "pid": process.pid}
        assert process.poll() is None
    finally:
        stop_file.write_text("stop", encoding="utf-8")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
