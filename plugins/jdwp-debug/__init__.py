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
import time

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
                "description": (
                    "Classpath used with main_class (java -cp). "
                    "Ignored when jar_path is provided. Default: '.'"
                ),
            },
            "main_class": {
                "type": "string",
                "description": (
                    "Fully-qualified main class for classpath mode (e.g. 'DemoApp'). "
                    "Use either main_class or jar_path, not both."
                ),
            },
            "jar_path": {
                "type": "string",
                "description": (
                    "Executable JAR path for java -jar, including Spring Boot fat JARs. "
                    "Use either jar_path or main_class, not both."
                ),
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


def _log_error_summary(error: str) -> str:
    """Keep logs diagnostic without copying application output into agent.log."""
    if not error:
        return "-"
    return error.splitlines()[0][:240]


def _get_runtime(context_key: str = "default") -> Runtime:
    """Return the runtime isolated to one Hermes conversation/session."""
    key = context_key or "default"
    runtime = _runtimes.get(key)
    if runtime is None:
        runtime = JavaRuntime()
        _runtimes[key] = runtime
        logger.info("java_runtime.session.created context=%s", key)
    return runtime


def _handle_java_runtime(args: dict, **kw) -> str:
    started_at = time.monotonic()
    action = RuntimeAction(
        action=args.get("action", "status"),
        classpath=args.get("classpath", "."),
        main_class=args.get("main_class", ""),
        jar_path=args.get("jar_path", ""),
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
    context_key = str(kw.get("session_id") or kw.get("task_id") or "default")
    rt = _get_runtime(context_key)
    logger.info(
        "java_runtime.action.start action=%s context=%s pid=%s main_class=%s jar_path=%s "
        "jdwp=%s:%s breakpoint=%s:%s suspension=%s",
        action.action,
        context_key,
        action.pid or "-",
        action.main_class or "-",
        action.jar_path or "-",
        action.host,
        action.jdwp_port,
        action.class_pattern or "-",
        action.line or "-",
        action.suspension_id or "-",
    )

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
        error = f"Unknown action: {action.action}"
        logger.warning("java_runtime.action.invalid action=%s context=%s", action.action, context_key)
        logger.warning(
            "java_runtime.action.finish action=%s context=%s ok=False "
            "duration_ms=%.1f error=%s",
            action.action, context_key,
            (time.monotonic() - started_at) * 1000,
            _log_error_summary(error),
        )
        result = json.dumps({"ok": False, "error": error}, ensure_ascii=False)
    else:
        try:
            rr = handler(action)
            result = rr.to_json()
            data = rr.data or {}
            log_finish = logger.warning if rr.error else logger.info
            log_finish(
                "java_runtime.action.finish action=%s context=%s ok=%s duration_ms=%.1f "
                "status=%s process=%s debug=%s pid=%s suspension=%s "
                "threads=%s frames=%s variables=%s complete=%s error=%s",
                action.action,
                context_key,
                not bool(rr.error) and rr.ok,
                (time.monotonic() - started_at) * 1000,
                data.get("status", "-"),
                data.get("process_state", "-"),
                data.get("debug_state", "-"),
                data.get("pid", "-"),
                data.get("suspension_id", data.get("invalidated_suspension_id", "-")),
                data.get("thread_count", "-"),
                data.get("frame_count", "-"),
                data.get("variable_count", "-"),
                data.get("complete", "-"),
                _log_error_summary(rr.error),
            )
        except Exception as e:
            logger.exception(
                "java_runtime.action.crash action=%s context=%s duration_ms=%.1f",
                action.action,
                context_key,
                (time.monotonic() - started_at) * 1000,
            )
            error = f"{type(e).__name__}: {e}"
            logger.error(
                "java_runtime.action.finish action=%s context=%s ok=False "
                "duration_ms=%.1f error=%s",
                action.action, context_key,
                (time.monotonic() - started_at) * 1000,
                _log_error_summary(error),
            )
            result = json.dumps({"ok": False, "error": error}, ensure_ascii=False)
    return result


# ============================================================================
# 3. register()
# ============================================================================


def register(ctx) -> None:
    ctx.register_tool(
        name="java_runtime",
        toolset="runtime",
        schema=JAVA_RUNTIME_SCHEMA,
        handler=_handle_java_runtime,
        emoji="☕",
        description="Manage and debug a Java application with stateful breakpoint observations.",
    )
    logger.info("java-runtime plugin: registered java_runtime tool (via Runtime framework)")
