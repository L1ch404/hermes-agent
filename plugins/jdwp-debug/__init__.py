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
        "Use breakpoint bp_action=list to inspect active breakpoints and "
        "bp_action=remove with request_id to clear one breakpoint. "
        "Use exception to set/list/remove exception events, then wait_event to "
        "wait for either a breakpoint or exception suspension. "
        "Stack frames and variable/object references are valid only while that "
        "suspension is active. "
        "Variable entries use value_state=observed for real values (including "
        "Java null) and value_state=unavailable with an error when reading failed. "
        "variables excludes the local variable named 'this' by default and uses "
        "shallow object expansion so Spring beans do not flood the result; pass "
        "include_this=true or increase max_value_depth when deeper inspection is needed. "
        "The LLM does not need to know about JDWP or any protocol internals."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "run", "stop", "restart", "attach", "detach", "status", "logs", "breakpoint",
                    "exception", "wait_event", "wait_breakpoint", "threads", "stack", "variables", "resume",
                    "cleanup_debug_state",
                ],
                "description": (
                    "Operation to perform. wait_event blocks until a breakpoint or "
                    "exception hit; wait_breakpoint is the compatibility form that "
                    "only accepts breakpoint hits. threads/stack/variables require "
                    "an active debug suspension; "
                    "resume invalidates that suspension. cleanup_debug_state is an "
                    "emergency dogfood recovery action that resumes/clears known debug state."
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
                "type": "string", "enum": ["set", "remove", "list"],
                "description": (
                    "Breakpoint operation for action='breakpoint'. "
                    "'set' requires class_pattern and line and creates a breakpoint. "
                    "'list' returns currently tracked breakpoint_id values. "
                    "'remove' prefers breakpoint_id; if breakpoint_id is omitted, "
                    "class_pattern and/or line are used as selectors; if no selector "
                    "is provided, all breakpoints are cleared for backward compatibility."
                ),
            },
            "breakpoint_id": {
                "type": "string",
                "description": (
                    "Agent-facing breakpoint id returned by breakpoint set/list "
                    "(for example bp_001). Use with bp_action='remove' to clear "
                    "exactly one breakpoint."
                ),
            },
            "request_id": {
                "type": "integer", "minimum": 1,
                "description": (
                    "Legacy JDWP event request id. Prefer breakpoint_id for breakpoint "
                    "remove. Still used by exception_action='remove' for exception events."
                ),
            },
            "exception_action": {
                "type": "string", "enum": ["set", "remove", "list"],
                "description": (
                    "Exception event operation for action='exception'. "
                    "'set' requires exception_class and registers a JDWP exception "
                    "event. 'list' returns tracked exception request_id values. "
                    "'remove' prefers request_id; if no selector is provided, all "
                    "exception event requests are cleared for backward compatibility."
                ),
            },
            "exception_class": {
                "type": "string",
                "description": (
                    "Exception class to watch for action='exception', accepted as "
                    "java.lang.NullPointerException, java/lang/NullPointerException, "
                    "or Ljava/lang/NullPointerException;. Common java.lang simple "
                    "names such as NullPointerException and NumberFormatException "
                    "are also accepted. Runtime normalizes the class to a JVM "
                    "signature. Broad classes such as java.lang.Exception, "
                    "java.lang.RuntimeException, java.lang.Error, and java.lang.Throwable "
                    "are refused when caught=true unless allow_broad_caught=true is "
                    "explicitly set."
                ),
            },
            "caught": {
                "type": "boolean", "default": True,
                "description": (
                    "For action='exception'. Whether to suspend when the exception "
                    "will be caught. Defaults to true for specific exceptions such "
                    "as NullPointerException because web frameworks often catch and "
                    "wrap them. Broad caught exception watches are refused unless "
                    "allow_broad_caught=true."
                ),
            },
            "uncaught": {
                "type": "boolean", "default": True,
                "description": (
                    "For action='exception'. Whether to suspend for uncaught throws. "
                    "At least one of caught or uncaught must be true."
                ),
            },
            "allow_broad_caught": {
                "type": "boolean", "default": False,
                "description": (
                    "Safety override for action='exception'. Set true only when you "
                    "intentionally want caught=true on a broad class such as "
                    "java.lang.Exception; this can be very noisy in Spring/MyBatis/etc."
                ),
            },
            "class_pattern": {
                "type": "string",
                "description": (
                    "Substring match against the JVM internal class signature "
                    "(e.g. Lcom/foo/Bar;). For bp_action='set', this selects the "
                    "target class. For bp_action='remove' without request_id, this "
                    "filters active breakpoints by class. Runtime excludes proxy and "
                    "generated classes by default; use include_proxy/include_generated "
                    "only when you intentionally want those classes."
                ),
            },
            "include_proxy": {
                "type": "boolean", "default": False,
                "description": (
                    "Used by breakpoint set. Defaults to false so CGLIB/JDK/ByteBuddy/"
                    "Hibernate proxy classes are skipped during class_pattern matching."
                ),
            },
            "include_generated": {
                "type": "boolean", "default": False,
                "description": (
                    "Used by breakpoint set. Defaults to false so generated classes "
                    "such as lambda/generated helper classes are skipped during class matching."
                ),
            },
            "line": {
                "type": "integer", "minimum": 1,
                "description": (
                    "Source line number. For bp_action='set', this selects the target "
                    "line. For bp_action='remove' without request_id, this filters "
                    "active breakpoints by line."
                ),
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
            "include_this": {
                "type": "boolean", "default": False,
                "description": (
                    "Used by action='variables'. Defaults to false to skip the local "
                    "variable named 'this', which is often a huge Spring bean graph. "
                    "Set true only when the receiver object itself is important."
                ),
            },
            "max_value_depth": {
                "type": "integer", "minimum": 0, "maximum": 5, "default": 1,
                "description": (
                    "Used by action='variables'. Object expansion depth. Default 1 "
                    "shows useful top-level fields without dumping deep dependency "
                    "graphs. 0 keeps object values as references; primitives, strings, "
                    "and array metadata remain readable."
                ),
            },
            "semantic_collections": {
                "type": "boolean", "default": True,
                "description": (
                    "Used by action='variables'. Defaults to true so Java arrays, "
                    "ArrayList, LinkedList, HashMap, LinkedHashMap, HashSet, "
                    "LinkedHashSet, and Optional are rendered as logical structures "
                    "instead of JDK internal fields. Set false to inspect raw fields "
                    "such as elementData/table/map."
                ),
            },
            "item_limit": {
                "type": "integer", "minimum": 0, "maximum": 64, "default": 16,
                "description": (
                    "Used by action='variables' when semantic_collections=true. "
                    "Maximum array/list/set items returned. Default: 16."
                ),
            },
            "map_entry_limit": {
                "type": "integer", "minimum": 0, "maximum": 64, "default": 16,
                "description": (
                    "Used by action='variables' when semantic_collections=true. "
                    "Maximum map entries returned. Default: 16."
                ),
            },
            "timeout": {
                "type": "number", "minimum": 0.1, "maximum": 300, "default": 30,
                "description": "Seconds to wait for a debug event. Default: 30.",
            },
            "suspension_id": {
                "type": "string",
                "description": (
                    "Suspension token returned by wait_event or wait_breakpoint. Pass it to "
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


def _bool_arg(args: dict, name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


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
        exception_action=args.get("exception_action", "set"),
        breakpoint_id=args.get("breakpoint_id", ""),
        request_id=args.get("request_id", 0),
        class_pattern=args.get("class_pattern", ""),
        include_proxy=_bool_arg(args, "include_proxy", False),
        include_generated=_bool_arg(args, "include_generated", False),
        exception_class=args.get("exception_class", ""),
        caught=_bool_arg(args, "caught", True),
        uncaught=_bool_arg(args, "uncaught", True),
        allow_broad_caught=_bool_arg(args, "allow_broad_caught", False),
        line=args.get("line", 0),
        thread_name=args.get("thread_name", ""),
        frame_index=args.get("frame_index", 0),
        max_frames=args.get("max_frames", 20),
        include_this=_bool_arg(args, "include_this", False),
        max_value_depth=int(args.get("max_value_depth", 1)),
        semantic_collections=_bool_arg(args, "semantic_collections", True),
        item_limit=int(args.get("item_limit", 16)),
        map_entry_limit=int(args.get("map_entry_limit", 16)),
        timeout=float(args.get("timeout", 30)),
        suspension_id=args.get("suspension_id", ""),
    )
    context_key = str(kw.get("session_id") or kw.get("task_id") or "default")
    rt = _get_runtime(context_key)
    logger.info(
        "java_runtime.action.start action=%s context=%s pid=%s main_class=%s jar_path=%s "
        "jdwp=%s:%s breakpoint=%s:%s breakpoint_id=%s exception=%s request_id=%s suspension=%s",
        action.action,
        context_key,
        action.pid or "-",
        action.main_class or "-",
        action.jar_path or "-",
        action.host,
        action.jdwp_port,
        action.class_pattern or "-",
        action.line or "-",
        action.breakpoint_id or "-",
        action.exception_class or "-",
        action.request_id or "-",
        action.suspension_id or "-",
    )

    dispatch = {
        "run": rt.run,
        "stop": rt.stop,
        "restart": rt.restart,
        "attach": rt.attach,
        "detach": rt.detach,
        "status": rt.status,
        "logs": rt.logs,
        "breakpoint": rt.breakpoint,
        "exception": rt.exception,
        "wait_event": rt.wait_event,
        "wait_breakpoint": rt.wait_breakpoint,
        "threads": rt.threads,
        "stack": rt.stack,
        "variables": rt.variables,
        "resume": rt.resume,
        "cleanup_debug_state": rt.cleanup_debug_state,
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
