"""
Java process lifecycle manager — pure subprocess, no JDWP dependency.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import time
from typing import Optional


logger = logging.getLogger(__name__)


class ProcessInfo:
    """Snapshot of a running Java process."""
    def __init__(
        self,
        proc: subprocess.Popen | None,
        jdwp_port: int,
        main_class: str,
        *,
        pid: int | None = None,
        owned: bool = True,
    ):
        self.proc = proc
        self._pid = proc.pid if proc is not None else int(pid or 0)
        self.jdwp_port = jdwp_port
        self.main_class = main_class
        self.owned = owned

    @property
    def pid(self) -> int:
        return self._pid

    def is_alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self._pid <= 0:
            return False
        try:
            os.kill(self._pid, 0)
            return True
        except OSError:
            return False

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll() if self.proc is not None else None


class ProcessManager:
    """Start, stop, and monitor a Java process."""

    JDWP_HANDSHAKE = b"JDWP-Handshake"

    def __init__(self, host: str = "localhost"):
        self._host = host
        self._process: Optional[ProcessInfo] = None

    # -- helpers --

    @staticmethod
    def _read_log_tail(log_file: str | None, n: int = 20) -> str:
        """Read last N lines of a log file. Returns empty string on failure."""
        if not log_file:
            return ""
        try:
            with open(log_file, "r") as f:
                lines = f.readlines()
            return "".join(lines[-n:])
        except Exception:
            return ""

    @staticmethod
    def _check_jdwp_port(host: str, port: int, timeout: float = 0.5) -> bool:
        """Check if a JDWP port is accepting connections AND replies with handshake."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall(ProcessManager.JDWP_HANDSHAKE)
            reply = sock.recv(14)
            sock.close()
            return reply == ProcessManager.JDWP_HANDSHAKE
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False

    # -- lifecycle --

    def start(
        self,
        classpath: str,
        main_class: str,
        *,
        app_args: list[str] | None = None,
        jdwp_port: int = 5005,
        vm_args: list[str] | None = None,
        log_file: str | None = None,
        startup_timeout: float = 30.0,
    ) -> ProcessInfo:
        """Launch a Java process with JDWP enabled, return ProcessInfo.

        Waits up to startup_timeout seconds for the process to confirm ready
        (JDWP handshake verified + process survives 2s after). Raises
        RuntimeError with log tail on failure.
        """
        started_at = time.monotonic()
        logger.info(
            "java_runtime.process.start.request main_class=%s classpath=%s "
            "jdwp_port=%s app_args_count=%s vm_args_count=%s startup_timeout=%s",
            main_class or "-", classpath, jdwp_port,
            len(app_args or []), len(vm_args or []), startup_timeout,
        )
        # Auto-restart: stop old process first
        if self._process and self._process.is_alive():
            logger.info(
                "java_runtime.process.start.replacing pid=%s",
                self._process.pid,
            )
            self.stop()

        log_fp = None
        try:
            if log_file:
                log_fp = open(log_file, "w")

            cmd = [
                "java",
                f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address={jdwp_port}",
            ]
            if vm_args:
                cmd.extend(vm_args)
            cmd.extend(["-cp", classpath, main_class])
            if app_args:
                cmd.extend(app_args)

            proc = subprocess.Popen(
                cmd, stdout=log_fp or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            logger.info(
                "java_runtime.process.spawned pid=%s main_class=%s jdwp_port=%s",
                proc.pid, main_class or "-", jdwp_port,
            )
        except Exception as exc:
            if log_fp:
                log_fp.close()
            logger.error(
                "java_runtime.process.spawn.failed main_class=%s jdwp_port=%s "
                "error_type=%s error=%s",
                main_class or "-", jdwp_port, type(exc).__name__,
                str(exc).splitlines()[0] if str(exc) else "-",
            )
            raise

        # Wait for process to confirm ready (JDWP handshake verified)
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                if log_fp:
                    log_fp.close()
                log_tail = self._read_log_tail(log_file)
                logger.warning(
                    "java_runtime.process.start.exited pid=%s exit_code=%s "
                    "elapsed_ms=%.1f captured_log_chars=%s",
                    proc.pid, proc.returncode,
                    (time.monotonic() - started_at) * 1000, len(log_tail),
                )
                raise RuntimeError(
                    f"Process exited with code {proc.returncode}. "
                    f"Last log lines:\n{log_tail}"
                )

            if self._check_jdwp_port("127.0.0.1", jdwp_port):
                # JDWP handshake verified — wait 2s and confirm process stayed alive
                time.sleep(2.0)
                if proc.poll() is not None:
                    if log_fp:
                        log_fp.close()
                    log_tail = self._read_log_tail(log_file)
                    logger.warning(
                        "java_runtime.process.start.unstable pid=%s exit_code=%s "
                        "elapsed_ms=%.1f captured_log_chars=%s",
                        proc.pid, proc.returncode,
                        (time.monotonic() - started_at) * 1000, len(log_tail),
                    )
                    raise RuntimeError(
                        f"Process exited with code {proc.returncode} shortly after startup. "
                        f"Last log lines:\n{log_tail}"
                    )
                break

            time.sleep(0.5)
        else:
            # Timeout: JDWP never ready
            if log_fp:
                log_fp.close()
            log_tail = self._read_log_tail(log_file)
            try:
                proc.kill()
            except Exception:
                pass
            logger.warning(
                "java_runtime.process.start.timeout pid=%s jdwp_port=%s "
                "timeout_seconds=%s captured_log_chars=%s",
                proc.pid, jdwp_port, startup_timeout, len(log_tail),
            )
            raise RuntimeError(
                f"Startup timed out after {startup_timeout}s. "
                f"Last log lines:\n{log_tail}"
            )

        if log_fp:
            log_fp.close()
        self._process = ProcessInfo(proc, jdwp_port, main_class)
        logger.info(
            "java_runtime.process.start.ready pid=%s main_class=%s jdwp_port=%s "
            "elapsed_ms=%.1f log_file=%s",
            proc.pid, main_class or "-", jdwp_port,
            (time.monotonic() - started_at) * 1000, log_file or "-",
        )
        return self._process

    def attach(
        self,
        pid: int,
        jdwp_port: int,
        main_class: str = "attached",
        host: str | None = None,
    ) -> ProcessInfo:
        """Track an existing local JVM after verifying its process and JDWP port."""
        target_host = host or self._host
        logger.info(
            "java_runtime.process.attach.request pid=%s jdwp=%s:%s main_class=%s",
            pid, target_host, jdwp_port, main_class or "-",
        )
        if pid <= 0:
            raise RuntimeError("attach requires a positive pid")
        try:
            os.kill(pid, 0)
        except OSError as exc:
            raise RuntimeError(f"Java process {pid} is not running or not accessible") from exc
        if not self._check_jdwp_port(target_host, jdwp_port, timeout=2.0):
            raise RuntimeError(
                f"Process {pid} is running, but {target_host}:{jdwp_port} "
                "did not complete a JDWP handshake"
            )
        self._process = ProcessInfo(
            None,
            jdwp_port,
            main_class,
            pid=pid,
            owned=False,
        )
        logger.info(
            "java_runtime.process.attach.ready pid=%s jdwp=%s:%s main_class=%s",
            pid, target_host, jdwp_port, main_class or "-",
        )
        return self._process

    def detach(self) -> dict:
        """Forget an attached process without terminating it."""
        if self._process is None:
            logger.info("java_runtime.process.detach.skipped reason=not_attached")
            return {"status": "not_attached"}
        pid = self._process.pid
        self._process = None
        logger.info("java_runtime.process.detached pid=%s", pid)
        return {"status": "detached", "pid": pid}

    def stop(self) -> dict:
        """Stop the process. Returns {'status': ..., 'pid': ...}."""
        if self._process is None or not self._process.is_alive():
            logger.info("java_runtime.process.stop.skipped reason=not_running")
            return {"status": "not_running"}

        if not self._process.owned:
            return self.detach()

        pid = self._process.pid
        proc = self._process.proc
        if proc is None:
            return self.detach()
        try:
            logger.info("java_runtime.process.stop.signal pid=%s signal=SIGTERM", pid)
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning("java_runtime.process.stop.escalate pid=%s signal=SIGKILL", pid)
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        self._process = None
        logger.info(
            "java_runtime.process.stop.finish pid=%s exit_code=%s",
            pid, proc.poll(),
        )
        return {"status": "stopped", "pid": pid}

    # -- query --

    @property
    def current(self) -> Optional[ProcessInfo]:
        return self._process

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_alive()
