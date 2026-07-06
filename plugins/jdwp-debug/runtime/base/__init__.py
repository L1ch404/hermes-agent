"""
Runtime Framework — base classes.

Every language runtime (Java, Python, Go, ...) extends Runtime ABC.
The LLM tool only depends on Runtime, never on concrete implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ============================================================================
# Observation — what the LLM sees (never protocol details)
# ============================================================================


@dataclass
class RuntimeObservation:
    """Structured observation returned by ``status()``."""
    running: bool = False
    pid: int | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StackFrame:
    """A single stack frame."""
    index: int = 0
    class_name: str = ""
    method_name: str = ""
    line: int = 0
    is_native: bool = False


@dataclass
class Variable:
    """A local variable with its value."""
    name: str = ""
    type_name: str = ""
    value: Any = None
    value_observed: bool = False
    error: str = ""
    slot: int = 0


# ============================================================================
# Action — what the LLM requests
# ============================================================================


@dataclass
class RuntimeAction:
    """Parsed action from LLM tool call args."""
    action: str

    # lifecycle
    classpath: str = "."
    main_class: str = ""
    jar_path: str = ""
    app_args: list[str] | None = None
    jdwp_port: int = 5005
    vm_args: list[str] | None = None
    pid: int = 0
    host: str = "127.0.0.1"

    # observation
    tail: int = 50

    # debug
    bp_action: str = "set"
    class_pattern: str = ""
    line: int = 0
    thread_name: str = ""
    frame_index: int = 0
    max_frames: int = 20
    timeout: float = 30.0
    suspension_id: str = ""


# ============================================================================
# RuntimeResult — what the handler turns into JSON for the LLM
# ============================================================================


@dataclass
class RuntimeResult:
    """Structured result for the LLM tool callback."""
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> str:
        import json
        if self.error:
            return json.dumps(
                {"ok": False, "error": self.error, **self.data},
                ensure_ascii=False,
            )
        return json.dumps({"ok": self.ok, **self.data}, ensure_ascii=False)


# ============================================================================
# Runtime — the ABC that every language runtime implements
# ============================================================================


class Runtime(ABC):
    """Agent-facing runtime manager.

    Subclasses implement language-specific lifecycle, observation, and debug.
    The LLM tool only ever calls methods on this interface.
    """

    @abstractmethod
    def run(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def stop(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def restart(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def attach(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def detach(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def status(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def logs(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def wait_breakpoint(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def threads(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def stack(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def variables(self, action: RuntimeAction) -> RuntimeResult:
        ...

    @abstractmethod
    def resume(self, action: RuntimeAction) -> RuntimeResult:
        ...
