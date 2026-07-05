"""
Jolink Java Runtime Plugin

Exposes a single ``java_runtime`` tool. The tool depends on the Runtime ABC,
not on JDWP or any protocol detail. JavaRuntime is just the first implementation.

Design:
    LLM → RuntimeAction → Runtime (ABC) → JavaRuntime → JDWP / Process / Log
"""

from __future__ import annotations

import json
import logging

from .runtime.base import RuntimeAction, Runtime
from .runtime.java.runtime import JavaRuntime

logger = logging.getLogger(__name__)

# ============================================================================
# 1. JSON Schema — 只暴露 Runtime 能力
# ============================================================================

JAVA_RUNTIME_SCHEMA = {
    "name": "java_runtime",
    "description": (
        "Manage and observe a running Java application. "
        "Supports lifecycle (run/stop/restart), monitoring (status/logs), "
        "and a stateful debug loop (breakpoint/wait/threads/stack/variables/resume). "
        "Debug workflow: set a breakpoint, trigger the application, call "
        "wait_breakpoint, inspect using the returned suspension_id, then resume. "
        "Stack frames and variable/object references are valid only while that "
        "suspension is active. "
        "Variable entries use value_state=observed for real values (including "
        "Java null) and value_state=unavailable with an error when reading failed. "
        "The LLM does not need to know about JDWP or any protocol internals."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "run", "stop", "restart", "attach", "detach", "status", "logs", "breakpoint",
                    "wait_breakpoint", "threads", "stack", "variables", "resume",
                ],
                "description": (
                    "Operation to perform. wait_breakpoint blocks until a hit or timeout; "
                    "threads/stack/variables require an active breakpoint suspension; "
                    "resume invalidates that suspension."
                ),
            },
            "classpath": {
                "type": "string", "default": ".",
                "description": "Classpath for the Java application. Default: '.'",
            },
            "main_class": {
                "type": "string",
                "description": "Fully-qualified main class name (e.g. 'DemoApp').",
            },
            "app_args": {
                "type": "array", "items": {"type": "string"},
                "description": "Command-line arguments for the application.",
            },
            "jdwp_port": {
                "type": "integer", "minimum": 1024, "maximum": 65535, "default": 5005,
                "description": "JDWP debug port. Default: 5005.",
            },
            "pid": {
                "type": "integer", "minimum": 1,
                "description": "Local Java process ID for attach.",
            },
            "host": {
                "type": "string", "default": "127.0.0.1",
                "description": "JDWP host for attach. Default: 127.0.0.1.",
            },
            "vm_args": {
                "type": "array", "items": {"type": "string"},
                "description": "Additional JVM arguments.",
            },
            "tail": {
                "type": "integer", "minimum": 1, "maximum": 500, "default": 50,
                "description": "Lines of log output. Default: 50.",
            },
            "bp_action": {
                "type": "string", "enum": ["set", "remove"],
                "description": "Breakpoint operation: 'set' or 'remove'.",
            },
            "class_pattern": {
                "type": "string",
                "description": "Class name substring to match.",
            },
            "line": {
                "type": "integer", "minimum": 1,
                "description": "Source line number.",
            },
            "thread_name": {
                "type": "string",
                "description": (
                    "Optional thread-name substring for threads/stack/variables. "
                    "By default the breakpoint-hit thread is used."
                ),
            },
            "frame_index": {
                "type": "integer", "minimum": 0, "default": 0,
                "description": "Stack frame index. Default: 0 (top of stack).",
            },
            "max_frames": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                "description": "Maximum frames returned by stack. Default: 20.",
            },
            "timeout": {
                "type": "number", "minimum": 0.1, "maximum": 300, "default": 30,
                "description": "Seconds to wait for a breakpoint event. Default: 30.",
            },
            "suspension_id": {
                "type": "string",
                "description": (
                    "Suspension token returned by wait_breakpoint. Pass it to "
                    "threads, stack, variables, and resume so stale observations are rejected."
                ),
            },
        },
        "required": ["action"],
    },
}

# ============================================================================
# 2. Handler — Tool → RuntimeAction → Runtime (ABC) → JavaRuntime
# ============================================================================

_runtimes: dict[str, Runtime] = {}


def _get_runtime(context_key: str = "default") -> Runtime:
    """Return the runtime isolated to one Hermes conversation/session."""
    key = context_key or "default"
    runtime = _runtimes.get(key)
    if runtime is None:
        runtime = JavaRuntime()
        _runtimes[key] = runtime
    return runtime


def _handle_java_runtime(args: dict, **kw) -> str:
    action = RuntimeAction(
        action=args.get("action", "status"),
        classpath=args.get("classpath", "."),
        main_class=args.get("main_class", ""),
        app_args=args.get("app_args"),
        jdwp_port=args.get("jdwp_port", 5005),
        vm_args=args.get("vm_args"),
        pid=args.get("pid", 0),
        host=args.get("host", "127.0.0.1"),
        tail=args.get("tail", 50),
        bp_action=args.get("bp_action", "set"),
        class_pattern=args.get("class_pattern", ""),
        line=args.get("line", 0),
        thread_name=args.get("thread_name", ""),
        frame_index=args.get("frame_index", 0),
        max_frames=args.get("max_frames", 20),
        timeout=float(args.get("timeout", 30)),
        suspension_id=args.get("suspension_id", ""),
    )
    logger.info(f"\n{'=' * 60}")
    logger.info(f"[java_runtime] action = {action.action}")
    print(f"\n{'=' * 60}")
    print(f"[java_runtime] action = {action.action}")

    context_key = str(kw.get("session_id") or kw.get("task_id") or "default")
    rt = _get_runtime(context_key)

    dispatch = {
        "run":        rt.run,
        "stop":       rt.stop,
        "restart":    rt.restart,
        "attach":     rt.attach,
        "detach":     rt.detach,
        "status":     rt.status,
        "logs":       rt.logs,
        "breakpoint": rt.breakpoint,
        "wait_breakpoint": rt.wait_breakpoint,
        "threads":    rt.threads,
        "stack":      rt.stack,
        "variables":  rt.variables,
        "resume":     rt.resume,
    }

    handler = dispatch.get(action.action)
    if handler is None:
        result = json.dumps({"error": f"Unknown action: {action.action}"})
    else:
        try:
            rr = handler(action)
            result = rr.to_json()
        except Exception as e:
            result = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)

    print(f"[java_runtime] 返回值: {result[:500]}")
    print(f"{'=' * 60}\n")
    return result


# ============================================================================
# 3. register()
# ============================================================================


def register(ctx) -> None:
    ctx.register_tool(
        name="java_runtime",
        toolset="java",
        schema=JAVA_RUNTIME_SCHEMA,
        handler=_handle_java_runtime,
        emoji="☕",
        description="Manage and debug a Java application with stateful breakpoint observations.",
    )
    logger.info("java-runtime plugin: registered java_runtime tool (via Runtime framework)")
