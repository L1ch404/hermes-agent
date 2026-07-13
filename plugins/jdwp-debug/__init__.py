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
from pathlib import Path

from .runtime.base import RuntimeAction, Runtime
from .runtime.java.runtime import JavaRuntime

logger = logging.getLogger(__name__)

# ============================================================================
# 1. JSON Schema — 只暴露 Runtime 能力
# ============================================================================

JAVA_RUNTIME_SCHEMA = {
    "name": "java_runtime",
    "description": (
        "An LLM-facing Java debugger for observing and controlling a running Java application "
        "using real execution-time evidence. "
        "Use this tool to investigate bugs, unfamiliar code, actual execution paths, "
        "exceptions, call stacks, variables, object state, data flow, runtime configuration, "
        "permission context, and application behavior when source code, logs, tests, or error "
        "messages are insufficient. "

        "Before performing a runtime investigation with breakpoints, exception watches, "
        "event waiting, threads, stack frames, variables, object inspection, resume, or "
        "debug-state cleanup, first load skill_view(\"java-runtime:observation\") unless that "
        "skill has already been loaded in the current context. The skill defines evidence "
        "boundaries, suspension lifecycles, investigation principles, and safe usage rules. "
        "Lifecycle, connection, and monitoring actions such as run, stop, restart, attach, "
        "detach, status, and logs do not require loading the skill. "

        "Choose and combine Runtime actions dynamically according to the current task; no fixed "
        "debugging workflow is required. Stack frames, variables, and object references remain "
        "valid only while the corresponding suspension and debug connection remain active. "
        "Do not reuse them after resume, cleanup_debug_state, detach, stop, restart, process "
        "replacement, or connection reset. Do not leave application threads suspended "
        "unnecessarily. "

        "Variable entries use value_state='observed' for successfully read values, including "
        "Java null; value_state='partial' when a structured value was only partly read; and "
        "value_state='unavailable' when no reliable value could be read. For partial or unavailable "
        "values, inspect the error on the variable entry or inside its structured value. Object "
        "inspection is shallow by default to avoid flooding results; use include_this=true or "
        "increase max_value_depth only when relevant. "

        "Read error_code, retryable, warnings, nearby_locations, suggested_next_step, "
        "next_action, and suggestions when deciding how to continue or recover. Use the exact "
        "identifiers returned by the Runtime: breakpoint_id for breakpoints, request_id for "
        "exception watches, and suspension_id for active suspensions. These identifiers are not "
        "interchangeable. The model does not need to understand JDWP or other protocol internals."
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
                    "Operation to perform. run launches a Runtime-owned JVM with JDWP enabled; "
                    "if another process is already tracked, the current implementation stops a "
                    "Runtime-owned process or detaches an externally attached process before "
                    "launching the new one. stop terminates a Runtime-owned process, but only "
                    "detaches from an externally attached JVM. restart performs stop followed by "
                    "run and therefore requires the same launch arguments as run; it does not "
                    "restart an externally attached JVM in place. attach requires a positive local "
                    "pid and a reachable local JDWP endpoint. detach resumes any active suspension, "
                    "clears tracked debug requests, and disconnects the debugger without terminating "
                    "the target process. status reports process/debug state and may promote a queued "
                    "breakpoint or exception event into the active suspension. logs reads only output "
                    "captured for a JVM launched by Runtime; it does not capture stdout or stderr from "
                    "an externally attached JVM and must not be used as evidence about that JVM. "
                    "wait_event accepts registered breakpoint and exception "
                    "events; wait_breakpoint is a compatibility action that accepts breakpoint events "
                    "only. threads lists thread state for the active suspension; stack and variables "
                    "inspect it; resume invalidates it. cleanup_debug_state is a best-effort recovery "
                    "action that drains queued events, clears tracked breakpoints and exception "
                    "watches, resumes the active suspension when possible, and issues a final VM resume "
                    "without stopping or detaching the application."
                ),
            },
            "classpath": {
                "type": "string", "default": ".",
                "description": (
                    "Classpath used by run/restart with main_class (java -cp). "
                    "Ignored when jar_path is provided. Default: '.'."
                ),
            },
            "main_class": {
                "type": "string",
                "description": (
                    "For run/restart in classpath mode, the fully qualified main class, for example "
                    "'com.example.DemoApp'. Use either main_class or jar_path, not both. For attach, "
                    "this is an optional descriptive label; when omitted, Runtime uses 'attached'."
                ),
            },
            "jar_path": {
                "type": "string",
                "description": (
                    "Executable JAR path for run/restart with java -jar, including Spring Boot fat "
                    "JARs. Use either jar_path or main_class, not both."
                ),
            },
            "app_args": {
                "type": "array", "items": {"type": "string"},
                "description": "Application command-line arguments used by run/restart.",
            },
            "jdwp_port": {
                "type": "integer", "minimum": 1024, "maximum": 65535, "default": 5005,
                "description": (
                    "JDWP port used by run/restart or attach. The endpoint must be local for attach. "
                    "Default: 5005."
                ),
            },
            "pid": {
                "type": "integer", "minimum": 1,
                "description": "Positive local Java process ID required by attach.",
            },
            "host": {
                "type": "string", "default": "127.0.0.1",
                "description": (
                    "JDWP host for attach. Only IPv4 local endpoints are currently supported: "
                    "localhost or 127.0.0.1. Default: 127.0.0.1."
                ),
            },
            "vm_args": {
                "type": "array", "items": {"type": "string"},
                "description": "Additional JVM arguments used by run/restart.",
            },
            "tail": {
                "type": "integer", "minimum": 1, "maximum": 500, "default": 50,
                "description": (
                    "Number of lines returned from the Runtime-captured launch log by action='logs'. "
                    "An attached JVM's stdout/stderr is not captured, so logs must not be treated as "
                    "evidence about the current attached JVM. Default: 50."
                ),
            },
            "bp_action": {
                "type": "string", "enum": ["set", "remove", "list"],
                "description": (
                    "Breakpoint operation for action='breakpoint'. 'set' requires class_pattern and "
                    "line. 'list' returns tracked breakpoints and their breakpoint_id values. "
                    "'remove' selects targets in this priority order: breakpoint_id, request_id, "
                    "class_pattern and/or line. If all selectors are omitted, the current "
                    "implementation clears every tracked breakpoint; use that behavior intentionally."
                ),
            },
            "breakpoint_id": {
                "type": "string",
                "description": (
                    "Agent-facing breakpoint identifier returned by breakpoint set/list, for example "
                    "bp_001. Prefer it with bp_action='remove' to clear exactly one breakpoint."
                ),
            },
            "request_id": {
                "type": "integer", "minimum": 1,
                "description": (
                    "Numeric debug-event request identifier. It is the primary identifier returned "
                    "for exception watches and may be used by exception_action='remove'. For "
                    "breakpoints, set/list results expose it as a low-level diagnostic value under "
                    "jdwp.request_id, while breakpoint-hit and status-promoted events may also return "
                    "it at the top level. Prefer breakpoint_id for breakpoint management. Use only an "
                    "exact value returned by the Runtime."
                ),
            },
            "exception_action": {
                "type": "string", "enum": ["set", "remove", "list"],
                "description": (
                    "Exception-watch operation for action='exception'. 'set' requires exception_class "
                    "and registers a caught and/or uncaught exception event. 'list' returns tracked "
                    "request_id values. 'remove' selects by request_id first, then exception_class. "
                    "If both selectors are omitted, the current implementation clears every tracked "
                    "exception watch; use that behavior intentionally."
                ),
            },
            "exception_class": {
                "type": "string",
                "description": (
                    "Exception class used by action='exception'. Accepted forms include "
                    "java.lang.NullPointerException, java/lang/NullPointerException, and "
                    "Ljava/lang/NullPointerException;. Known java.lang simple names such as "
                    "NullPointerException and NumberFormatException are also accepted; other simple "
                    "names must be fully qualified. Runtime normalizes the value to a JVM signature. "
                    "For exception_action='set', the class must already be loaded in the target JVM; "
                    "otherwise Runtime returns error_code='exception_class_not_loaded', retryable=true, "
                    "and next_action/suggestions. Broad classes such as java.lang.Exception, "
                    "java.lang.RuntimeException, java.lang.Error, and java.lang.Throwable are refused "
                    "when caught=true unless allow_broad_caught=true is explicitly set. For "
                    "exception_action='remove', exception_class may be used as a selector."
                ),
            },
            "caught": {
                "type": "boolean", "default": True,
                "description": (
                    "For exception_action='set'. Whether to suspend when the exception will be caught. "
                    "Default: true. Broad caught watches are refused unless allow_broad_caught=true."
                ),
            },
            "uncaught": {
                "type": "boolean", "default": True,
                "description": (
                    "For exception_action='set'. Whether to suspend for uncaught throws. At least one "
                    "of caught or uncaught must be true. Default: true."
                ),
            },
            "allow_broad_caught": {
                "type": "boolean", "default": False,
                "description": (
                    "Safety override for exception_action='set'. Set true only when intentionally "
                    "watching caught occurrences of a broad class such as java.lang.Exception; this "
                    "can be very noisy in Spring, MyBatis, and similar frameworks."
                ),
            },
            "class_pattern": {
                "type": "string",
                "description": (
                    "Class selector used by action='breakpoint'. For bp_action='set', Runtime accepts "
                    "a fully qualified Java class name, JVM signature, exact simple class name, package "
                    "suffix, or distinctive substring; fully qualified Java names are preferred. "
                    "Matching is ranked and ambiguous best matches are rejected. Proxy and generated "
                    "classes are excluded by default. For bp_action='remove' without breakpoint_id or "
                    "request_id, this is a raw substring selector over the tracked class signature; "
                    "prefer breakpoint_id for precise removal."
                ),
            },
            "include_proxy": {
                "type": "boolean", "default": False,
                "description": (
                    "Used by breakpoint set. Defaults to false so Spring CGLIB, JDK proxy, ByteBuddy, "
                    "Hibernate proxy, Javassist, and similar proxy classes are skipped during matching."
                ),
            },
            "include_generated": {
                "type": "boolean", "default": False,
                "description": (
                    "Used by breakpoint set. Defaults to false so generated classes such as lambda or "
                    "generated helper classes are skipped during matching."
                ),
            },
            "line": {
                "type": "integer", "minimum": 1,
                "description": (
                    "Source line number. For bp_action='set', Runtime requires an executable location "
                    "at this exact line; when none exists it may return nearby_locations with line, "
                    "method, and code_index candidates. For bp_action='remove' without breakpoint_id "
                    "or request_id, line filters tracked breakpoints and may be combined with "
                    "class_pattern."
                ),
            },
            "thread_name": {
                "type": "string",
                "description": (
                    "Optional thread-name substring used by stack and variables. The selected name must "
                    "match exactly one currently suspended thread; an unsuspended thread cannot provide "
                    "stack frames or variables. With the default EVENT_THREAD policy, normally only the "
                    "event-hit thread is suspended, so omit thread_name unless action='threads' confirms "
                    "the target has suspended=true. When omitted, the event-hit thread is used. The "
                    "threads action lists all threads and does not use this selector."
                ),
            },
            "frame_index": {
                "type": "integer", "minimum": 0, "default": 0,
                "description": "Stack-frame index used by action='variables'. Default: 0 (top frame).",
            },
            "max_frames": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                "description": "Maximum number of frames returned by action='stack'. Default: 20.",
            },
            "include_this": {
                "type": "boolean", "default": False,
                "description": (
                    "Used by action='variables'. Defaults to false to exclude the local variable named "
                    "'this', which often leads to a large Spring bean graph. Set true only when the "
                    "receiver object is relevant."
                ),
            },
            "max_value_depth": {
                "type": "integer", "minimum": 0, "maximum": 5, "default": 1,
                "description": (
                    "Used by action='variables'. Object expansion depth, clamped by Runtime to 0..5. "
                    "Default 1 shows top-level fields. 0 keeps objects as references while primitives, "
                    "strings, and array metadata remain readable."
                ),
            },
            "semantic_collections": {
                "type": "boolean", "default": True,
                "description": (
                    "Used by action='variables'. Defaults to true so Java arrays, ArrayList, "
                    "LinkedList, HashMap, LinkedHashMap, HashSet, LinkedHashSet, and Optional are "
                    "rendered as logical structures instead of JDK internal fields. Set false to "
                    "inspect raw fields such as elementData, table, or map."
                ),
            },
            "item_limit": {
                "type": "integer", "minimum": 0, "maximum": 64, "default": 16,
                "description": (
                    "Used by action='variables' when semantic_collections=true. Maximum array/list/set "
                    "items returned, clamped to 0..64. Set 0 to return collection metadata without "
                    "items. Default: 16."
                ),
            },
            "map_entry_limit": {
                "type": "integer", "minimum": 0, "maximum": 64, "default": 16,
                "description": (
                    "Used by action='variables' when semantic_collections=true. Maximum map entries "
                    "returned, clamped to 0..64. Set 0 to return map metadata without entries. "
                    "Default: 16."
                ),
            },
            "timeout": {
                "type": "number", "minimum": 0.1, "maximum": 300, "default": 30,
                "description": (
                    "Seconds to wait in action='wait_event' or action='wait_breakpoint'. Default: 30."
                ),
            },
            "suspension_id": {
                "type": "string",
                "description": (
                    "Suspension token returned by wait_event, wait_breakpoint, or a status call that "
                    "promotes a queued event. Use it with threads, stack, variables, and resume to "
                    "reject stale observations. The current implementation uses the sole active "
                    "suspension when this field is omitted; when provided, it must exactly match the "
                    "active suspension_id."
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
        emoji="☕️",
        description="Manage and debug a Java application with stateful breakpoint observations.",
    )
    ctx.register_skill(
        "observation",
        Path(__file__).with_name("skills") / "observation" / "SKILL.md",
        (
            "Use live Java Runtime evidence to investigate execution paths, exceptions, "
            "call stacks, variables, object state, data flow, configuration, permission "
            "context, and application behavior."
        ),
    )
    logger.info("java-runtime plugin: registered java_runtime tool (via Runtime framework)")
