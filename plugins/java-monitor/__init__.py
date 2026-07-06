"""
Java Process Monitor Plugin

Adds a ``java_processes`` tool that lists running Java/JVM processes.
Uses ``jps`` (Java Virtual Machine Process Status Tool) when available,
with ``ps`` as fallback.

Tool registration follows the same pattern as built-in tools
(e.g. tools/file_tools.py:1668-1825):

    1. Define a JSON Schema describing the tool to the LLM
    2. Write a handler function that does the real work
    3. Call ctx.register_tool(name, toolset, schema, handler, ...)

Everything exists to reduce uncertainty for the LLM.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================================
# 1. JSON Schema — 这就是发给 LLM 的"工具使用说明书"
#    格式与 read_file、write_file 完全一致
# ============================================================================

JAVA_PROCESSES_SCHEMA = {
    "name": "java_processes",
    "description": (
        "List all running Java/JVM processes on the current machine. "
        "Returns process ID (PID) and the main class or JAR name for each. "
        "Use this to check if a specific Java application has started, "
        "or to find the PID of a running Java service for debugging. "
        "Prefers 'jps' (JDK tool) when JDK is installed; falls back to 'ps'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": (
                    "Optional. Case-insensitive substring to filter results. "
                    "Example: 'Spring' to find Spring Boot apps, "
                    "'hermes' to find the specific JAR/service."
                ),
            },
            "full": {
                "type": "boolean",
                "description": (
                    "When true, returns JVM arguments (e.g. -Xmx, -D props) "
                    "for each process. Default: false (fast, just PID + class name)."
                ),
                "default": False,
            },
        },
        "required": [],
    },
    "output": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "Human-readable summary of the query result. "
                    "Example: 'Found 2 Java process(es)'."
                ),
            },
            "count": {
                "type": "integer",
                "description": (
                    "The total number of Java processes returned."
                ),
            },
            "processes": {
                "type": "array",
                "description": (
                    "List of running Java processes."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "pid": {
                            "type": "integer",
                            "description": (
                                "Operating system process ID."
                            ),
                        },
                        "main_class": {
                            "type": "string",
                            "description": (
                                "Main class name or executable JAR name of the Java process."
                            ),
                        },
                        "runtime": {
                            "type": "string",
                            "description": (
                                "Java runtime implementation or distribution, "
                                "such as 'OpenJDK', 'Oracle JDK', 'Temurin', or other detected JVM."
                            ),
                        },
                    },
                    "required": [
                        "pid",
                        "main_class",
                        "runtime",
                    ],
                },
            },
        },
        "required": [
            "message",
            "count",
            "processes",
        ],
    },
}


# ============================================================================
# 2. Handler 函数 — LLM 调用 tool 时实际执行的 Python 代码
# ============================================================================


def _find_jps() -> str:
    """Locate the ``jps`` binary.

    Priority: JAVA_HOME/bin/jps → PATH → None (fall back to ps).
    """
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        jps_path = os.path.join(java_home, "bin", "jps")
        if os.path.isfile(jps_path):
            return jps_path

    # macOS: /usr/libexec/java_home is a script that prints the JDK path
    if os.path.isfile("/usr/libexec/java_home"):
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home"],
                capture_output=True, text=True, timeout=5,
            )
            jdk_path = result.stdout.strip()
            if jdk_path:
                jps_path = os.path.join(jdk_path, "bin", "jps")
                if os.path.isfile(jps_path):
                    return jps_path
        except Exception:
            pass

    # Check common Homebrew paths
    for prefix in ["/opt/homebrew/opt/openjdk", "/usr/local/opt/openjdk"]:
        jps_path = os.path.join(prefix, "bin", "jps")
        if os.path.isfile(jps_path):
            return jps_path

    # Last resort: "jps" on PATH
    return "jps"


def _detect_runtime() -> str:
    """Detect the Java runtime distribution.

    Runs ``java -version`` once per process and parses the output.
    Typical outputs:

        OpenJDK / Zulu:
          OpenJDK Runtime Environment Zulu11.84+15-CA (build ...)
          → "Zulu 11.0.27"

        Oracle JDK:
          Java(TM) SE Runtime Environment (build 1.8.0_401-b10)
          → "Oracle JDK 1.8.0_401"

        Temurin:
          OpenJDK Runtime Environment Temurin-11.0.27+9 (build ...)
          → "Temurin 11.0.27"

    Result is cached — all processes on the same machine share the same JVM.
    """
    java_bin = os.path.join(
        os.environ.get("JAVA_HOME", _resolve_macos_java_home() or ""),
        "bin", "java",
    ) if os.environ.get("JAVA_HOME") or _resolve_macos_java_home() else "java"

    try:
        result = subprocess.run(
            [java_bin, "-version"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return "Java"

    # java -version writes to stderr on most JDKs
    output = (result.stderr + result.stdout).strip()
    lines = output.split("\n")
    if not lines:
        return "Java"

    first = lines[0].strip()

    # Extract version number: e.g. "1.8.0_401" or "11.0.27"
    import re
    ver_match = re.search(r'version\s+"?([\d._]+)', output)
    version = ver_match.group(1) if ver_match else "?"

    # Detect distribution from the second line (runtime line)
    runtime_line = lines[1].strip() if len(lines) > 1 else ""
    rt_lower = runtime_line.lower()

    if "zulu" in rt_lower:
        return f"Zulu {version}"
    if "temurin" in rt_lower:
        return f"Temurin {version}"
    if "corretto" in rt_lower:
        return f"Corretto {version}"
    if "graalvm" in rt_lower:
        return f"GraalVM {version}"
    if "liberica" in rt_lower:
        return f"Liberica {version}"
    if "sapmachine" in rt_lower:
        return f"SAP Machine {version}"
    if "openjdk" in rt_lower:
        return f"OpenJDK {version}"
    if "java(tm)" in rt_lower:
        return f"Oracle JDK {version}"

    return f"Java {version}"


def _resolve_macos_java_home() -> str | None:
    """Call ``/usr/libexec/java_home`` on macOS to get the JDK path."""
    if os.path.isfile("/usr/libexec/java_home"):
        try:
            r = subprocess.run(
                ["/usr/libexec/java_home"],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() or None
        except Exception:
            pass
    return None


_runtime_cache: str | None = None


def _get_runtime() -> str:
    """Return the detected Java runtime, cached."""
    global _runtime_cache
    if _runtime_cache is None:
        _runtime_cache = _detect_runtime()
    return _runtime_cache


def _run_jps(full: bool) -> list[dict[str, Any]]:
    """Run ``jps -l`` (and optionally ``jps -lv``) to list Java processes."""
    jps_bin = _find_jps()
    runtime = _get_runtime()

    processes: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            [jps_bin, "-lv" if full else "-l"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid = parts[0]
            class_and_args = parts[1].split(None, 1)
            entry: dict[str, Any] = {"pid": int(pid), "main_class": class_and_args[0], "runtime": runtime}
            if full and len(class_and_args) > 1:
                entry["jvm_args"] = class_and_args[1]
            processes.append(entry)
        return processes
    except FileNotFoundError:
        raise  # caller falls back to ps
    except (subprocess.TimeoutExpired, Exception) as e:
        logger.debug("jps failed: %s", e)
        return []


def _run_ps(_full: bool = False) -> list[dict[str, Any]]:
    """Run ``ps aux`` and grep for ``java`` as fallback."""
    runtime = _get_runtime()
    processes: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.split("\n"):
            if "java" not in line.lower() or "grep" in line:
                continue
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            processes.append({"pid": pid, "main_class": parts[10][:200], "runtime": runtime})
        return processes
    except Exception as e:
        logger.debug("ps fallback failed: %s", e)
        return []


def _handle_java_processes(args: dict[str, Any], **kw: Any) -> str:
    """Tool handler — list Java processes, called by ToolRegistry.dispatch()."""
    filter_text: str | None = args.get("filter")
    full: bool = bool(args.get("full", False))

    print(f"\n{'=' * 60}")
    print(f"[java_processes] 被 LLM 调用")
    print(f"[java_processes] 入参 args = {json.dumps(args, ensure_ascii=False, default=str)}")
    print(f"[java_processes] 入参 kw   = {json.dumps({k: str(v)[:80] for k, v in kw.items()}, ensure_ascii=False)}")
    print(f"[java_processes] filter   = {filter_text!r}")
    print(f"[java_processes] full     = {full}")

    try:
        processes = _run_jps(full=full)
    except FileNotFoundError:
        processes = _run_ps()

    # Optional filtering
    if filter_text:
        ft = filter_text.lower()
        processes = [
            p for p in processes
            if ft in p.get("main_class", "").lower()
               or ft == str(p.get("pid", ""))
        ]

    if not processes:
        tail = f" matching '{filter_text}'" if filter_text else ""
        result = json.dumps({
            "message": f"No Java processes found{tail}. Is a JVM running?",
            "processes": [],
            "count": 0,
        }, ensure_ascii=False)
    else:
        tail = f" matching '{filter_text}'" if filter_text else ""
        result = json.dumps({
            "message": f"Found {len(processes)} Java process(es){tail}",
            "processes": processes,
            "count": len(processes),
        }, ensure_ascii=False)

    print(f"[java_processes] 返回值: {result}")
    print(f"{'=' * 60}\n")
    return result


# ============================================================================
# 3. register() — 插件入口，框架加载时调用
#    类比 Java: InitializingBean.afterPropertiesSet()
# ============================================================================


def register(ctx) -> None:
    """Register the ``java_processes`` tool via the plugin context."""
    ctx.register_tool(
        name="java_processes",
        toolset="runtime",
        schema=JAVA_PROCESSES_SCHEMA,
        handler=_handle_java_processes,
        emoji="☕",
        description="List running Java/JVM processes with PID and main class.",
    )
    logger.info("java-monitor plugin: registered java_processes tool")
