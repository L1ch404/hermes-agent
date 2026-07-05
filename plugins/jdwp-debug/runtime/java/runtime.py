"""
JavaRuntime — Agent-facing Java runtime manager.

Implements the Runtime ABC. Internally composits:
  - JDWPClient  (pure protocol transport)
  - ProcessManager (lifecycle)
  - LogManager    (console output)

LLM never sees JDWP, thread IDs, or protocol details.
"""

from __future__ import annotations

import struct
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..base import (
    Runtime, RuntimeAction, RuntimeResult,
    Variable,
)
from .jdwp import JDWPClient, JDWPError, Cmd, EventKind, Tag
from .process import ProcessManager
from .log import LogManager


@dataclass
class SuspensionSnapshot:
    """A VM suspension generation whose frame/object ids are still valid."""

    suspension_id: str
    generation: int
    request_id: int
    thread_id: int
    location: dict[str, int]
    observed_at: str
    valid: bool = True


class JavaRuntime(Runtime):
    """Agent-facing Java runtime. One instance manages one application."""

    def __init__(self, host: str = "localhost"):
        self._host = host
        self._proc = ProcessManager(host)
        self._log = LogManager()
        self._breakpoints: dict[int, dict[str, Any]] = {}
        self._jdwp: JDWPClient | None = None  # persistent debugger connection
        self._active_suspension: SuspensionSnapshot | None = None
        self._suspension_generation = 0
        self._max_array_elements = 64

    # ── Lifecycle ──────────────────────────────────────

    def run(self, action: RuntimeAction) -> RuntimeResult:
        try:
            self._reset_debug_state()
            self._host = "127.0.0.1"
            log_file = self._log.create(action.main_class)
            info = self._proc.start(
                classpath=action.classpath,
                main_class=action.main_class,
                app_args=action.app_args,
                jdwp_port=action.jdwp_port,
                vm_args=action.vm_args,
                log_file=log_file,
            )
            return RuntimeResult(ok=True, data={
                "status": "started",
                "pid": info.pid,
                "jdwp_port": info.jdwp_port,
                "log_file": log_file,
                "main_class": info.main_class,
            })
        except Exception as e:
            return RuntimeResult(ok=False, error=str(e))

    def stop(self, action: RuntimeAction) -> RuntimeResult:
        self._disconnect()
        self._breakpoints.clear()
        self._invalidate_suspension()
        data = self._proc.stop()
        return RuntimeResult(ok=True, data=data)

    def restart(self, action: RuntimeAction) -> RuntimeResult:
        self.stop(action)
        time.sleep(1)
        return self.run(action)

    def attach(self, action: RuntimeAction) -> RuntimeResult:
        current = self._proc.current
        if current is not None and current.is_alive():
            return RuntimeResult(
                ok=False,
                error=(
                    f"Runtime already manages process {current.pid}; "
                    "stop or detach it before attaching another process"
                ),
            )
        try:
            self._reset_debug_state()
            self._host = action.host or "127.0.0.1"
            if self._host not in {"localhost", "127.0.0.1", "::1"}:
                return RuntimeResult(
                    ok=False,
                    error="Remote attach is not supported yet; use a local JDWP endpoint",
                )
            info = self._proc.attach(
                pid=action.pid,
                jdwp_port=action.jdwp_port,
                main_class=action.main_class or "attached",
                host=self._host,
            )
            self._connect()
            return RuntimeResult(ok=True, data={
                "status": "attached",
                "pid": info.pid,
                "jdwp_host": self._host,
                "jdwp_port": info.jdwp_port,
                "main_class": info.main_class,
            })
        except Exception as e:
            self._disconnect()
            self._proc.detach()
            return RuntimeResult(ok=False, error=str(e))

    def detach(self, action: RuntimeAction) -> RuntimeResult:
        if self._active_suspension is not None and self._jdwp is not None:
            try:
                err, _ = self._jdwp.command(Cmd.VM, 9)
                if err:
                    return RuntimeResult(ok=False, error=f"VM resume before detach failed (err {err})")
            except Exception as e:
                return RuntimeResult(ok=False, error=f"VM resume before detach failed: {e}")
        self._invalidate_suspension()
        self._breakpoints.clear()
        self._disconnect()
        current = self._proc.current
        if current is not None and current.owned and current.is_alive():
            data = {
                "status": "debugger_detached",
                "pid": current.pid,
                "process_state": "running",
            }
        else:
            data = self._proc.detach()
        self._host = "127.0.0.1"
        return RuntimeResult(ok=True, data=data)

    # ── Observation ────────────────────────────────────

    def status(self, action: RuntimeAction) -> RuntimeResult:
        proc = self._proc.current
        if proc is None:
            return RuntimeResult(ok=True, data={
                "process_state": "absent",
                "debug_state": "detached",
                "running": False,
                "message": "No application is managed by this runtime",
            })
        if not proc.is_alive():
            self._invalidate_suspension()
            return RuntimeResult(ok=True, data={
                "process_state": "exited",
                "debug_state": "detached",
                "running": False,
                "pid": proc.pid,
                "exit_code": proc.exit_code,
                "message": "Managed application has exited",
            })

        info: dict[str, Any] = {
            "process_state": "running",
            "debug_state": (
                "suspended" if self._active_suspension is not None
                else "attached" if self._jdwp is not None
                else "detached"
            ),
            "running": True,
            "pid": proc.pid,
            "jdwp_port": proc.jdwp_port,
            "main_class": proc.main_class,
            "ownership": "launched" if proc.owned else "attached",
            "log_file": self._log.path,
            "suspension_id": (
                self._active_suspension.suspension_id
                if self._active_suspension is not None else None
            ),
        }

        # Try JDWP for extra info
        try:
            jdwp = self._connect()
            info["debug_state"] = (
                "suspended" if self._active_suspension is not None else "attached"
            )
            err, data = jdwp.command(Cmd.VM, 1)  # Version
            if err == 0:
                offset = 0
                desc_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
                offset += desc_len
                offset += 8  # jdwpMajor, jdwpMinor
                vm_ver_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
                vm_ver = data[offset:offset+vm_ver_len].decode("utf-8", errors="replace"); offset += vm_ver_len
                vm_name_len = struct.unpack_from(">I", data, offset)[0]; offset += 4
                vm_name = data[offset:offset+vm_name_len].decode("utf-8", errors="replace")
                info["jvm"] = f"{vm_name} {vm_ver}"
            # Keep connection alive (now managed by _connect / _disconnect)
        except Exception:
            info["jvm"] = "unreachable"

        return RuntimeResult(ok=True, data=info)

    def logs(self, action: RuntimeAction) -> RuntimeResult:
        data = self._log.tail(action.tail)
        if "error" in data:
            return RuntimeResult(ok=False, error=data["error"])
        return RuntimeResult(ok=True, data=data)

    # ── Debug ──────────────────────────────────────────

    def breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        if not self._proc.is_running:
            return RuntimeResult(ok=False, error="No application running")

        try:
            jdwp = self._connect()

            if action.bp_action == "set":
                ids = jdwp.ids
                # ── Step 1: find class ──
                err, data = jdwp.command(Cmd.VM, 3)  # AllClasses
                if err:
                    return RuntimeResult(ok=False, error=f"AllClasses failed (err {err})")

                count = struct.unpack_from(">I", data, 0)[0]
                offset = 4
                found_cid = None
                found_sig = ""
                found_tag = 0
                for _ in range(count):
                    tag = data[offset]; offset += 1
                    cid = int.from_bytes(data[offset:offset+ids.reference_type_id_size], "big")
                    offset += ids.reference_type_id_size
                    slen = struct.unpack_from(">I", data, offset)[0]; offset += 4
                    sig = data[offset:offset+slen].decode("utf-8"); offset += slen
                    offset += 4  # status
                    if action.class_pattern.lower() in sig.lower():
                        found_cid = cid
                        found_sig = sig
                        found_tag = tag
                        break

                if found_cid is None:
                    return RuntimeResult(ok=False, error=f"Class matching '{action.class_pattern}' not found")

                # ── Step 2: list methods ──
                err, data = jdwp.command(Cmd.REF_TYPE, 5, ids.pack_ref(found_cid))
                if err:
                    return RuntimeResult(ok=False, error=f"Methods failed (err {err})")

                method_count = struct.unpack_from(">I", data, 0)[0]
                offset = 4
                methods = []
                for _ in range(method_count):
                    mid = int.from_bytes(data[offset:offset+ids.method_id_size], "big")
                    offset += ids.method_id_size
                    nlen = struct.unpack_from(">I", data, offset)[0]; offset += 4
                    mname = data[offset:offset+nlen].decode("utf-8"); offset += nlen
                    slen = struct.unpack_from(">I", data, offset)[0]; offset += 4
                    msig = data[offset:offset+slen].decode("utf-8"); offset += slen
                    offset += 4  # modBits
                    methods.append((mid, mname, msig))

                # ── Step 3: find method containing the target line ──
                found_mid = None
                found_mname = ""
                found_msig = ""
                found_code_idx = 0
                for mid, mname, msig in methods:
                    err, lt_data = jdwp.command(
                        Cmd.METHOD, 1,
                        ids.pack_ref(found_cid) + ids.pack_obj(mid)
                    )
                    if err:
                        continue  # abstract / native methods have no line table
                    # LineTable: start(8) + end(8) + lines_count(4) + [lineCodeIndex(8) + lineNumber(4)]*
                    line_count = struct.unpack_from(">I", lt_data, 16)[0]
                    lt_offset = 20
                    for _ in range(line_count):
                        code_idx = struct.unpack_from(">Q", lt_data, lt_offset)[0]
                        lt_offset += 8
                        line_num = struct.unpack_from(">I", lt_data, lt_offset)[0]
                        lt_offset += 4
                        if line_num == action.line:
                            found_mid = mid
                            found_mname = mname
                            found_msig = msig
                            found_code_idx = code_idx
                            break
                    if found_mid:
                        break

                if found_mid is None:
                    return RuntimeResult(
                        ok=False,
                        error=f"Line {action.line} not found in any method of {found_sig}",
                        data={"class": found_sig, "line": action.line},
                    )

                # ── Step 4: set breakpoint (EventRequest/Set, eventKind=BREAKPOINT) ──
                # eventKind=2 (BREAKPOINT), suspendPolicy=2 (SUSPEND_ALL), modifiers=1
                # modifier: modKind=7 (LocationOnly), typeTag from JVM, classID, methodID, codeIndex
                bp_payload = struct.pack(">BBI", 2, 2, 1)  # eventKind, suspendPolicy, modifier count
                bp_payload += struct.pack(">B", 7)  # modKind = LocationOnly
                bp_payload += struct.pack(">B", found_tag)  # typeTag from JVM (1=class, 2=interface, etc.)
                bp_payload += ids.pack_ref(found_cid)
                bp_payload += ids.pack_obj(found_mid)
                bp_payload += struct.pack(">Q", found_code_idx)  # jlocation = code index, NOT line number

                err, bp_data = jdwp.command(Cmd.EVENT, 1, bp_payload)
                if err:
                    return RuntimeResult(ok=False, error=f"Set breakpoint failed (err {err})")

                request_id = struct.unpack_from(">I", bp_data, 0)[0]
                self._breakpoints[request_id] = {
                    "class": found_sig,
                    "method": f"{found_mname}{found_msig}",
                    "line": action.line,
                }

                return RuntimeResult(ok=True, data={
                    "bp_action": "set",
                    "request_id": request_id,
                    "class": found_sig,
                    "method": f"{found_mname}{found_msig}",
                    "line": action.line,
                })

            elif action.bp_action == "remove":
                if not self._breakpoints:
                    return RuntimeResult(ok=False, error="No breakpoints set")

                removed = []
                for rid in list(self._breakpoints):
                    # EventRequest/Clear: eventKind (BREAKPOINT) + requestID.
                    payload = struct.pack(">BI", EventKind.BREAKPOINT, rid)
                    err, _ = jdwp.command(Cmd.EVENT, 2, payload)
                    if err == 0:
                        self._breakpoints.pop(rid, None)
                        removed.append(rid)

                if not removed:
                    return RuntimeResult(ok=False, error="Failed to clear any breakpoints")
                return RuntimeResult(ok=True, data={"bp_action": "remove", "cleared_ids": removed})

            else:
                return RuntimeResult(ok=False, error=f"Unknown bp_action: {action.bp_action}")

        except JDWPError as e:
            return RuntimeResult(ok=False, error=str(e))

    def wait_breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        if not self._proc.is_running:
            return RuntimeResult(ok=False, error="No application running")
        if not self._breakpoints:
            return RuntimeResult(ok=False, error="No breakpoints set")
        if self._active_suspension is not None:
            return RuntimeResult(ok=True, data={
                "status": "already_suspended",
                **self._snapshot_context(self._active_suspension),
            })

        try:
            jdwp = self._connect()
            deadline = time.monotonic() + max(action.timeout, 0.1)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._breakpoint_timeout(action.timeout)
                composite = jdwp.wait_for_event(remaining)
                if composite is None:
                    return self._breakpoint_timeout(action.timeout)
                for event in composite.get("events", []):
                    if event.get("kind") in {
                        EventKind.VM_DEATH, EventKind.VM_DISCONNECTED,
                    }:
                        self._invalidate_suspension()
                        return RuntimeResult(
                            ok=False,
                            error="Target VM exited while waiting for breakpoint",
                        )
                    request_id = int(event.get("request_id", 0))
                    if (
                        event.get("kind") != EventKind.BREAKPOINT
                        or request_id not in self._breakpoints
                    ):
                        continue

                    self._suspension_generation += 1
                    snapshot = SuspensionSnapshot(
                        suspension_id=f"susp_{uuid.uuid4().hex[:12]}",
                        generation=self._suspension_generation,
                        request_id=request_id,
                        thread_id=int(event["thread_id"]),
                        location=event.get("location") or {},
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self._active_suspension = snapshot
                    return RuntimeResult(ok=True, data={
                        "status": "breakpoint_hit",
                        **self._snapshot_context(snapshot),
                        "breakpoint": self._breakpoints[request_id],
                        "thread": {
                            "name": self._thread_name(jdwp, snapshot.thread_id),
                        },
                        "location": self._describe_location(jdwp, snapshot.location),
                    })
        except (JDWPError, OSError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def threads(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            rows = []
            for thread_id in self._all_thread_ids(jdwp):
                err, status_data = jdwp.command(
                    Cmd.THREAD, 4, jdwp.ids.pack_obj(thread_id)
                )
                row: dict[str, Any] = {
                    "name": self._thread_name(jdwp, thread_id),
                    "is_breakpoint_thread": thread_id == snapshot.thread_id,
                }
                if err == 0 and len(status_data) >= 8:
                    thread_status, suspend_status = struct.unpack(">II", status_data[:8])
                    row["state"] = self._thread_status_name(thread_status)
                    row["suspended"] = bool(suspend_status & 1)
                else:
                    row["state"] = "unknown"
                    row["suspended"] = None
                rows.append(row)
            return RuntimeResult(ok=True, data={
                **self._snapshot_context(snapshot),
                "thread_count": len(rows),
                "threads": rows,
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def stack(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            thread_id = self._resolve_thread_id(jdwp, snapshot, action.thread_name)
            frames = self._read_frames(jdwp, thread_id, action.max_frames)
            return RuntimeResult(ok=True, data={
                **self._snapshot_context(snapshot),
                "thread": {"name": self._thread_name(jdwp, thread_id)},
                "frame_count": len(frames),
                "frames": [self._public_frame(frame) for frame in frames],
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def variables(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            ids = jdwp.ids
            thread_id = self._resolve_thread_id(jdwp, snapshot, action.thread_name)
            frames = self._read_frames(
                jdwp, thread_id, 1, start_index=action.frame_index
            )
            if not frames:
                return RuntimeResult(
                    ok=False,
                    error=f"Frame index {action.frame_index} does not exist",
                )
            frame = frames[0]
            if frame["is_native"]:
                return RuntimeResult(
                    ok=False,
                    error=f"Frame index {action.frame_index} is native and has no local variables",
                    data={"frame": self._public_frame(frame)},
                )

            payload = ids.pack_ref(frame["class_id"]) + ids.pack_obj(frame["method_id"])
            err, variable_data = jdwp.command(Cmd.METHOD, 2, payload)
            if err:
                return RuntimeResult(
                    ok=False,
                    error=(
                        f"VariableTable failed (err {err}); compile the class "
                        "with debug variable information (-g)"
                    ),
                    data={"frame": self._public_frame(frame)},
                )
            variables = self._visible_variables_for_location(
                variable_data, frame["location_index"]
            )

            getvalues_error = None
            if variables:
                values_payload = ids.pack_obj(thread_id) + ids.pack_obj(frame["frame_id"])
                values_payload += struct.pack(">I", len(variables))
                for variable in variables:
                    values_payload += struct.pack(">I", variable.slot)
                    values_payload += struct.pack(">B", Tag.from_sig(variable.type_name))
                err, values_data = jdwp.command(Cmd.STACK, 1, values_payload)
                if err == 0:
                    value_count = struct.unpack_from(">I", values_data, 0)[0]
                    offset = 4
                    for index in range(min(value_count, len(variables))):
                        try:
                            tag = values_data[offset]
                            offset += 1
                            variables[index].value, offset = self._read_value(
                                jdwp, ids, tag, values_data, offset, visited=set()
                            )
                            variables[index].value_observed = True
                        except Exception as exc:
                            variables[index].error = (
                                f"Failed to decode JVM value: {type(exc).__name__}: {exc}"
                            )
                            for remaining in variables[index + 1:]:
                                remaining.error = (
                                    "Value was not decoded because an earlier value "
                                    "made the JDWP response boundary unreliable"
                                )
                            break
                    if value_count < len(variables):
                        for variable in variables[value_count:]:
                            if not variable.error:
                                variable.error = (
                                    "JVM returned no value for this variable "
                                    f"({value_count} value(s) for {len(variables)} variable(s))"
                                )
                else:
                    getvalues_error = f"StackFrame/GetValues failed (err {err})"

                    for variable in variables:
                        variable.error = getvalues_error

            variable_results = [
                self._variable_observation(variable) for variable in variables
            ]
            complete = all(variable.value_observed for variable in variables)

            return RuntimeResult(ok=True, data={
                **self._snapshot_context(snapshot),
                "thread": {"name": self._thread_name(jdwp, thread_id)},
                "frame": self._public_frame(frame),
                "variable_count": len(variables),
                "complete": complete,
                "partial": not complete,
                "variables": variable_results,
                "getvalues_error": getvalues_error,
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    def resume(self, action: RuntimeAction) -> RuntimeResult:
        try:
            snapshot = self._require_suspension(action)
            jdwp = self._connect()
            err, _ = jdwp.command(Cmd.VM, 9)
            if err:
                return RuntimeResult(ok=False, error=f"VM resume failed (err {err})")
            suspension_id = snapshot.suspension_id
            self._invalidate_suspension()
            return RuntimeResult(ok=True, data={
                "status": "resumed",
                "invalidated_suspension_id": suspension_id,
                "process_state": "running",
                "debug_state": "attached",
            })
        except (JDWPError, RuntimeError) as e:
            return RuntimeResult(ok=False, error=str(e))

    # ── Internal ───────────────────────────────────────

    def _breakpoint_timeout(self, timeout: float) -> RuntimeResult:
        return RuntimeResult(ok=True, data={
            "status": "timeout",
            "timeout_seconds": timeout,
            "process_state": "running",
            "debug_state": "attached",
        })

    def _reset_debug_state(self) -> None:
        self._disconnect()
        self._breakpoints.clear()
        self._invalidate_suspension()

    def _invalidate_suspension(self) -> None:
        if self._active_suspension is not None:
            self._active_suspension.valid = False
        self._active_suspension = None

    def _require_suspension(self, action: RuntimeAction) -> SuspensionSnapshot:
        snapshot = self._active_suspension
        if snapshot is None or not snapshot.valid:
            raise RuntimeError(
                "No active breakpoint suspension. Call wait_breakpoint after triggering the breakpoint."
            )
        if action.suspension_id and action.suspension_id != snapshot.suspension_id:
            raise RuntimeError(
                f"Stale suspension_id '{action.suspension_id}'. "
                f"The active suspension is '{snapshot.suspension_id}'."
            )
        if not self._proc.is_running:
            self._invalidate_suspension()
            raise RuntimeError("Target process exited; the suspension is no longer valid")
        return snapshot

    def _snapshot_context(self, snapshot: SuspensionSnapshot) -> dict[str, Any]:
        return {
            "suspension_id": snapshot.suspension_id,
            "generation": snapshot.generation,
            "observed_at": snapshot.observed_at,
            "valid_while_suspended": True,
            "process_state": "running",
            "debug_state": "suspended",
        }

    def _variable_observation(self, variable: Variable) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": variable.name,
            "type": variable.type_name,
            "slot": variable.slot,
        }
        if variable.value_observed:
            result["value_state"] = "observed"
            result["value"] = variable.value
        else:
            result["value_state"] = "unavailable"
            result["error"] = variable.error or "Variable value was not returned by the JVM"
        return result

    def _all_thread_ids(self, jdwp: JDWPClient) -> list[int]:
        ids = jdwp.ids
        err, data = jdwp.command(Cmd.VM, 4)
        if err:
            raise JDWPError(err, "VirtualMachine/AllThreads failed")
        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        thread_ids = []
        for _ in range(count):
            thread_ids.append(int.from_bytes(
                data[offset:offset + ids.object_id_size], "big"
            ))
            offset += ids.object_id_size
        return thread_ids

    def _thread_name(self, jdwp: JDWPClient, thread_id: int) -> str:
        err, data = jdwp.command(Cmd.THREAD, 1, jdwp.ids.pack_obj(thread_id))
        if err or len(data) < 4:
            return "<unknown>"
        length = struct.unpack_from(">I", data, 0)[0]
        return data[4:4 + length].decode("utf-8", errors="replace")

    def _resolve_thread_id(
        self,
        jdwp: JDWPClient,
        snapshot: SuspensionSnapshot,
        thread_name: str,
    ) -> int:
        if not thread_name:
            return snapshot.thread_id
        matches = [
            thread_id for thread_id in self._all_thread_ids(jdwp)
            if thread_name.lower() in self._thread_name(jdwp, thread_id).lower()
        ]
        if not matches:
            raise RuntimeError(f"Thread matching '{thread_name}' not found")
        if len(matches) > 1:
            names = [self._thread_name(jdwp, thread_id) for thread_id in matches[:5]]
            raise RuntimeError(
                f"Thread name '{thread_name}' is ambiguous; matches: {names}"
            )
        return matches[0]

    def _read_frames(
        self,
        jdwp: JDWPClient,
        thread_id: int,
        max_frames: int,
        *,
        start_index: int = 0,
    ) -> list[dict[str, Any]]:
        ids = jdwp.ids
        thread_bytes = ids.pack_obj(thread_id)
        err, count_data = jdwp.command(Cmd.THREAD, 7, thread_bytes)
        if err:
            raise JDWPError(err, "ThreadReference/FrameCount failed")
        total_frames = struct.unpack_from(">I", count_data, 0)[0]
        if start_index >= total_frames:
            return []
        requested_frames = min(max(1, max_frames), total_frames - start_index)
        payload = ids.pack_obj(thread_id) + struct.pack(
            ">II", max(start_index, 0), requested_frames
        )
        err, data = jdwp.command(Cmd.THREAD, 6, payload)
        if err:
            raise JDWPError(err, "ThreadReference/Frames failed")
        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        frames = []
        for relative_index in range(count):
            frame_id = int.from_bytes(data[offset:offset + ids.frame_id_size], "big")
            offset += ids.frame_id_size
            type_tag = data[offset]
            offset += 1
            class_id = int.from_bytes(
                data[offset:offset + ids.reference_type_id_size], "big"
            )
            offset += ids.reference_type_id_size
            method_id = int.from_bytes(
                data[offset:offset + ids.method_id_size], "big"
            )
            offset += ids.method_id_size
            location_index = struct.unpack_from(">Q", data, offset)[0]
            offset += 8
            is_native = location_index == 0xFFFFFFFFFFFFFFFF
            frames.append({
                "index": start_index + relative_index,
                "frame_id": frame_id,
                "type_tag": type_tag,
                "class_id": class_id,
                "method_id": method_id,
                "location_index": location_index,
                "class": self._class_signature(jdwp, class_id),
                "method": self._method_name(jdwp, class_id, method_id),
                "line": None if is_native else self._source_line_for_location(
                    jdwp, ids, class_id, method_id, location_index
                ),
                "is_native": is_native,
            })
        return frames

    def _public_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": frame["index"],
            "class": frame["class"],
            "method": frame["method"],
            "line": frame["line"],
            "is_native": frame["is_native"],
        }

    def _class_signature(self, jdwp: JDWPClient, class_id: int) -> str:
        err, data = jdwp.command(Cmd.REF_TYPE, 1, jdwp.ids.pack_ref(class_id))
        if err or len(data) < 4:
            return "unknown"
        length = struct.unpack_from(">I", data, 0)[0]
        return data[4:4 + length].decode("utf-8", errors="replace")

    def _method_name(self, jdwp: JDWPClient, class_id: int, method_id: int) -> str:
        ids = jdwp.ids
        err, data = jdwp.command(Cmd.REF_TYPE, 5, ids.pack_ref(class_id))
        if err or len(data) < 4:
            return "unknown"
        count = struct.unpack_from(">I", data, 0)[0]
        offset = 4
        for _ in range(count):
            current_id = int.from_bytes(data[offset:offset + ids.method_id_size], "big")
            offset += ids.method_id_size
            name_length = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            name = data[offset:offset + name_length].decode("utf-8", errors="replace")
            offset += name_length
            signature_length = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            signature = data[offset:offset + signature_length].decode(
                "utf-8", errors="replace"
            )
            offset += signature_length + 4
            if current_id == method_id:
                return f"{name}{signature}"
        return "unknown"

    def _describe_location(
        self, jdwp: JDWPClient, location: dict[str, int]
    ) -> dict[str, Any]:
        class_id = int(location.get("class_id", 0))
        method_id = int(location.get("method_id", 0))
        index = int(location.get("index", 0))
        return {
            "class": self._class_signature(jdwp, class_id),
            "method": self._method_name(jdwp, class_id, method_id),
            "line": self._source_line_for_location(
                jdwp, jdwp.ids, class_id, method_id, index
            ),
        }

    def _thread_status_name(self, status: int) -> str:
        return {
            0: "zombie",
            1: "running",
            2: "sleeping",
            3: "monitor",
            4: "waiting",
        }.get(status, "unknown")

    def _source_line_for_location(
        self,
        jdwp: JDWPClient,
        ids,
        class_id: int,
        method_id: int,
        location_index: int,
    ) -> int | None:
        """Map a JDWP location index back to the closest source line."""
        err, data = jdwp.command(
            Cmd.METHOD,
            1,
            ids.pack_ref(class_id) + ids.pack_obj(method_id),
        )
        if err:
            return None

        line_count = struct.unpack_from(">I", data, 16)[0]
        offset = 20
        first_line = None
        resolved_line = None

        for _ in range(line_count):
            code_idx = struct.unpack_from(">Q", data, offset)[0]
            offset += 8
            line_num = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            if first_line is None:
                first_line = line_num
            if code_idx > location_index:
                break
            resolved_line = line_num

        return resolved_line if resolved_line is not None else first_line

    def _visible_variables_for_location(self, variable_table: bytes, location_index: int) -> list[Variable]:
        """Return only the variables visible at the current frame location."""
        slot_count = struct.unpack_from(">I", variable_table, 4)[0]
        offset = 8
        entries: list[dict[str, int | str]] = []

        for order in range(slot_count):
            code_index = struct.unpack_from(">Q", variable_table, offset)[0]
            offset += 8
            nlen = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            vname = variable_table[offset:offset+nlen].decode("utf-8")
            offset += nlen
            slen = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            vsig = variable_table[offset:offset+slen].decode("utf-8")
            offset += slen
            scope_length = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            slot = struct.unpack_from(">I", variable_table, offset)[0]
            offset += 4
            entries.append({
                "order": order,
                "code_index": code_index,
                "scope_length": scope_length,
                "name": vname,
                "type_name": vsig,
                "slot": slot,
            })

        visible_by_slot: dict[int, dict[str, int | str]] = {}
        for entry in entries:
            scope_start = int(entry["code_index"])
            scope_length = int(entry["scope_length"])
            if not self._is_variable_visible(scope_start, scope_length, location_index):
                continue

            slot = int(entry["slot"])
            prev = visible_by_slot.get(slot)
            if prev is None or int(entry["code_index"]) >= int(prev["code_index"]):
                visible_by_slot[slot] = entry

        visible_entries = sorted(
            visible_by_slot.values(),
            key=lambda entry: int(entry["order"]),
        )
        return [
            Variable(
                name=str(entry["name"]),
                type_name=str(entry["type_name"]),
                slot=int(entry["slot"]),
                value=None,
            )
            for entry in visible_entries
        ]

    def _is_variable_visible(self, scope_start: int, scope_length: int, location_index: int) -> bool:
        if scope_length <= 0:
            return location_index >= scope_start
        return scope_start <= location_index < scope_start + scope_length

    def _reference_value(self, obj_id: int, kind: str) -> dict[str, str]:
        return {"_ref": f"0x{obj_id:x}", "_kind": kind}

    def _expanded_value_base(self, obj_id: int, kind: str) -> dict[str, str]:
        return self._reference_value(obj_id, kind)

    def _object_error_value(
        self,
        obj_id: int,
        *,
        error: str,
        class_name: str | None = None,
    ) -> dict[str, str]:
        result = self._reference_value(obj_id, "object")
        result["_error"] = error
        if class_name is not None:
            result["_class"] = class_name
        return result

    def _read_value(
        self,
        jdwp: JDWPClient,
        ids,
        tag: int,
        data: bytes,
        offset: int,
        depth: int = 3,
        visited: set[int] | None = None,
    ):
        """Read a single JDWP tagged value. Returns (value, new_offset).
        depth: remaining object-expansion budget. Arrays and strings do not consume it."""
        if visited is None:
            visited = set()

        if tag == Tag.DOUBLE:
            val = struct.unpack_from(">d", data, offset)[0]
            return val, offset + 8
        if tag == Tag.FLOAT:
            val = struct.unpack_from(">f", data, offset)[0]
            return val, offset + 4
        if tag == Tag.LONG:
            val = struct.unpack_from(">q", data, offset)[0]
            return val, offset + 8
        if tag == Tag.BYTE:
            val = struct.unpack_from(">b", data, offset)[0]
            return val, offset + 1
        if tag == Tag.BOOLEAN:
            return data[offset] != 0, offset + 1
        if tag == Tag.CHAR:
            code_unit = struct.unpack_from(">H", data, offset)[0]
            return chr(code_unit), offset + 2
        if tag == Tag.SHORT:
            val = struct.unpack_from(">h", data, offset)[0]
            return val, offset + 2
        if tag == Tag.INT:
            val = struct.unpack_from(">i", data, offset)[0]
            return val, offset + 4

        # Object / Array / String — need object_id
        obj_id = int.from_bytes(data[offset:offset + ids.object_id_size], "big")
        offset += ids.object_id_size

        if obj_id == 0:
            return None, offset

        if tag == Tag.STRING:
            err, sv = jdwp.command(Cmd.STRING_REF, 1, ids.pack_obj(obj_id))
            if err == 0:
                slen = struct.unpack_from(">I", sv, 0)[0]
                return sv[4:4 + slen].decode("utf-8"), offset
            return f"<String 0x{obj_id:x}>", offset

        if tag == Tag.ARRAY:
            if obj_id in visited:
                return self._reference_value(obj_id, "array"), offset
            visited.add(obj_id)
            return self._read_array(jdwp, ids, obj_id, depth, visited), offset

        if depth <= 0:
            return self._reference_value(obj_id, "object"), offset

        if obj_id in visited:
            return self._reference_value(obj_id, "object"), offset

        visited.add(obj_id)

        # tag == Tag.OBJECT (or anything else)
        return self._read_object(jdwp, ids, obj_id, depth - 1, visited), offset

    def _read_object(
        self,
        jdwp: JDWPClient,
        ids,
        obj_id: int,
        depth: int = 3,
        visited: set[int] | None = None,
    ) -> dict[str, Any]:
        """Read an object's class name and field values. Returns a structured dict."""
        try:
            if visited is None:
                visited = set()

            # Get the object's reference type
            err, rt_data = jdwp.command(Cmd.OBJ_REF, 1, ids.pack_obj(obj_id))
            if err:
                return self._object_error_value(obj_id, error=f"ObjectReference.ReferenceType failed (err {err})")
            ref_type_id = int.from_bytes(rt_data[1:1+ids.reference_type_id_size], "big")

            # Get class signature
            err, sig_data = jdwp.command(Cmd.REF_TYPE, 1, ids.pack_ref(ref_type_id))
            if err:
                return self._object_error_value(obj_id, error=f"ReferenceType.Signature failed (err {err})")
            slen = struct.unpack_from(">I", sig_data, 0)[0]
            sig = sig_data[4:4+slen].decode("utf-8")

            # Get fields
            err, f_data = jdwp.command(Cmd.REF_TYPE, 4, ids.pack_ref(ref_type_id))
            if err or struct.unpack_from(">I", f_data, 0)[0] == 0:
                result: dict[str, Any] = self._expanded_value_base(obj_id, "object")
                result["_class"] = sig
                if err:
                    result["_error"] = f"ReferenceType.Fields failed (err {err})"
                return result

            field_count = struct.unpack_from(">I", f_data, 0)[0]
            offset = 4
            fields = []
            for _ in range(field_count):
                fid = int.from_bytes(f_data[offset:offset+ids.field_id_size], "big")
                offset += ids.field_id_size
                nlen = struct.unpack_from(">I", f_data, offset)[0]; offset += 4
                fname = f_data[offset:offset+nlen].decode("utf-8"); offset += nlen
                slen2 = struct.unpack_from(">I", f_data, offset)[0]; offset += 4
                fsig = f_data[offset:offset+slen2].decode("utf-8"); offset += slen2
                mod_bits = struct.unpack_from(">I", f_data, offset)[0]; offset += 4
                if self._is_instance_field(mod_bits):
                    fields.append((fid, fname, fsig))

            if not fields:
                return {"_class": sig}

            # Read field values
            gv_payload = ids.pack_obj(obj_id)
            gv_payload += struct.pack(">I", len(fields))
            for fid, _, _ in fields:
                gv_payload += fid.to_bytes(ids.field_id_size, "big")  # fieldID uses field_id_size

            err, gv_data = jdwp.command(Cmd.OBJ_REF, 2, gv_payload)
            if err:
                result = self._expanded_value_base(obj_id, "object")
                result["_class"] = sig
                result["_error"] = f"ObjectReference.GetValues failed (err {err})"
                return result

            value_count = struct.unpack_from(">I", gv_data, 0)[0]
            gv_offset = 4
            result: dict[str, Any] = self._expanded_value_base(obj_id, "object")
            result["_class"] = sig
            for i in range(value_count):
                if i >= len(fields):
                    break
                _, fname, _fsig = fields[i]
                tag = gv_data[gv_offset]; gv_offset += 1
                val, gv_offset = self._read_value(jdwp, ids, tag, gv_data, gv_offset, depth, visited)
                result[fname] = val
            return result
        except Exception as e:
            return self._object_error_value(obj_id, error=str(e))

    def _read_array(
        self,
        jdwp: JDWPClient,
        ids,
        arr_id: int,
        depth: int = 3,
        visited: set[int] | None = None,
    ) -> dict:
        """Read an array's length and elements. Returns dict with _length and elements list."""
        try:
            if visited is None:
                visited = set()

            # Get length
            err, len_data = jdwp.command(Cmd.ARRAY, 1, ids.pack_obj(arr_id))
            if err:
                result: dict[str, Any] = self._expanded_value_base(arr_id, "array")
                result["_length"] = "?"
                result["_error"] = f"length failed (err {err})"
                return result
            total_len = struct.unpack_from(">I", len_data, 0)[0]

            if total_len == 0:
                result: dict[str, Any] = self._expanded_value_base(arr_id, "array")
                result["_length"] = 0
                result["elements"] = []
                return result

            requested_len = min(total_len, self._max_array_elements)

            # Read all elements (JDWP GetValues: firstIndex=0, length=arr_len)
            payload = ids.pack_obj(arr_id) + struct.pack(">II", 0, requested_len)
            err, ev_data = jdwp.command(Cmd.ARRAY, 2, payload)
            if err:
                result: dict[str, Any] = self._expanded_value_base(arr_id, "array")
                result["_length"] = total_len
                result["_error"] = f"getValues failed (err {err})"
                return result

            if len(ev_data) < 5:
                result: dict[str, Any] = self._expanded_value_base(arr_id, "array")
                result["_length"] = total_len
                result["_error"] = "arrayregion reply too short"
                return result

            element_tag = ev_data[0]
            returned_count = struct.unpack_from(">I", ev_data, 1)[0]
            gv_offset = 5
            read_len = min(returned_count, requested_len)

            elements = []
            for _ in range(read_len):
                if self._array_elements_are_tagged(element_tag):
                    tag = ev_data[gv_offset]
                    gv_offset += 1
                else:
                    tag = element_tag
                val, gv_offset = self._read_value(jdwp, ids, tag, ev_data, gv_offset, depth, visited)
                elements.append(val)

            result: dict[str, Any] = self._expanded_value_base(arr_id, "array")
            result["_length"] = total_len
            result["elements"] = elements
            if total_len > requested_len:
                result["_truncated"] = True
                result["_remaining_count"] = total_len - requested_len
            if returned_count != requested_len:
                result["_warning"] = (
                    f"arrayregion returned {returned_count} values (expected {requested_len})"
                )
            return result
        except Exception as e:
            result = self._expanded_value_base(arr_id, "array")
            result["_length"] = "?"
            result["_error"] = str(e)
            return result

    def _is_instance_field(self, mod_bits: int) -> bool:
        return (mod_bits & 0x0008) == 0

    def _array_elements_are_tagged(self, element_tag: int) -> bool:
        return element_tag in {
            Tag.ARRAY,
            Tag.OBJECT,
            Tag.STRING,
            Tag.THREAD,
            Tag.THREAD_GROUP,
            Tag.CLASS_LOADER,
            Tag.CLASS_OBJECT,
        }

    def _connect(self, timeout: float = 5.0) -> JDWPClient:
        """Get a JDWP connection. Reuses persistent connection if alive."""
        if self._jdwp is not None:
            try:
                # ``command`` multiplexes interleaved events into its event
                # queue, so probing the connection cannot discard a hit.
                self._jdwp.command(Cmd.VM, 1)  # Version
                return self._jdwp
            except Exception:
                try:
                    self._jdwp.close()
                except Exception:
                    pass
                self._jdwp = None
        jdwp = JDWPClient()
        proc = self._proc.current
        if proc is None or not proc.is_alive():
            raise RuntimeError("No application running — cannot connect debugger")
        jdwp.connect(self._host, proc.jdwp_port, timeout)
        self._jdwp = jdwp
        return jdwp

    def _disconnect(self) -> None:
        """Close the persistent JDWP connection."""
        if self._jdwp is not None:
            try:
                self._jdwp.close()
            except Exception:
                pass
            self._jdwp = None
