"""
core_engine/cpp_bridge.py
=========================

Production native-simulation bridge for Riot / God Node.

Design:
    60 Hz Python scheduler
        -> submit(batch)
        -> bounded in-memory queue
        -> persistent native worker process
        -> NDJSON command/response protocol
        -> result registry
        -> scheduler polls completed results

Important:
* No compile-per-task.
* No synchronous subprocess work in the engine tick path.
* No fake physics/simulation output.
* The bridge requires a real native simulation executable/configuration.
* When no native engine is configured, submissions fail closed with an
  explicit UNAVAILABLE result instead of pretending simulation succeeded.
* The public ``execute(batch)`` method is retained for compatibility with
  the current ``main.py`` caller. It is non-blocking and returns a submission
  envelope.
* Toolchain/build discovery is separate from runtime execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


logger = logging.getLogger("GodNode.CPPBridge")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - [CPP-BRIDGE] - %(levelname)s - %(message)s"
        )
    )
    logger.addHandler(handler)
logger.setLevel(os.getenv("RIOT_CPP_BRIDGE_LOG_LEVEL", "INFO").upper())


# ============================================================================
# CONFIGURATION
# ============================================================================

def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.001) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


DEFAULT_QUEUE_CAPACITY = _env_int("RIOT_CPP_QUEUE_CAPACITY", 256)
DEFAULT_RESULT_CAPACITY = _env_int("RIOT_CPP_RESULT_CAPACITY", 512)
DEFAULT_JOB_TTL = _env_float("RIOT_CPP_JOB_TTL_SECONDS", 300.0)
DEFAULT_JOB_TIMEOUT = _env_float("RIOT_CPP_JOB_TIMEOUT_SECONDS", 5.0)
DEFAULT_READ_TIMEOUT = _env_float("RIOT_CPP_WORKER_READ_TIMEOUT_SECONDS", 5.0)
DEFAULT_RESTART_LIMIT = _env_int("RIOT_CPP_WORKER_RESTART_LIMIT", 8)
DEFAULT_RESTART_WINDOW = _env_float("RIOT_CPP_WORKER_RESTART_WINDOW_SECONDS", 300.0)
DEFAULT_BACKOFF_BASE = _env_float("RIOT_CPP_WORKER_BACKOFF_BASE_SECONDS", 0.5)
DEFAULT_BACKOFF_MAX = _env_float("RIOT_CPP_WORKER_BACKOFF_MAX_SECONDS", 20.0)
DEFAULT_MAX_BATCH_BYTES = _env_int(
    "RIOT_CPP_MAX_BATCH_BYTES", 2 * 1024 * 1024
)
DEFAULT_MAX_REGISTRY_ENTRIES = _env_int(
    "RIOT_CPP_MAX_REGISTRY_ENTRIES", 4096
)


# ============================================================================
# CONTRACTS
# ============================================================================

class WorkerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    CRASHED = "crashed"
    STOPPING = "stopping"
    UNAVAILABLE = "unavailable"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


class BackpressurePolicy(str, Enum):
    REJECT = "reject"
    DROP_OLDEST = "drop_oldest"


@dataclass(slots=True, frozen=True)
class SimulationConfig:
    executable: tuple[str, ...] = ()
    queue_capacity: int = DEFAULT_QUEUE_CAPACITY
    result_capacity: int = DEFAULT_RESULT_CAPACITY
    job_ttl_seconds: float = DEFAULT_JOB_TTL
    job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT
    worker_read_timeout_seconds: float = DEFAULT_READ_TIMEOUT
    restart_limit: int = DEFAULT_RESTART_LIMIT
    restart_window_seconds: float = DEFAULT_RESTART_WINDOW
    restart_backoff_base_seconds: float = DEFAULT_BACKOFF_BASE
    restart_backoff_max_seconds: float = DEFAULT_BACKOFF_MAX
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES
    max_registry_entries: int = DEFAULT_MAX_REGISTRY_ENTRIES
    backpressure: BackpressurePolicy = BackpressurePolicy.REJECT
    working_directory: Optional[str] = None
    extra_environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SimulationJob:
    job_id: str
    sequence: int
    submitted_at: float
    deadline_at: float
    batch: Any
    batch_hash: str
    priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SimulationResult:
    job_id: str
    sequence: int
    status: JobStatus
    submitted_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    duration_ms: float = 0.0
    worker_pid: Optional[int] = None
    output: Any = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass(slots=True)
class BridgeStats:
    submitted: int = 0
    accepted: int = 0
    rejected: int = 0
    dropped: int = 0
    completed: int = 0
    failed: int = 0
    timed_out: int = 0
    unavailable: int = 0
    cancelled: int = 0
    worker_restarts: int = 0
    worker_crashes: int = 0
    queue_high_watermark: int = 0
    result_high_watermark: int = 0
    last_error: Optional[str] = None
    last_worker_start: float = 0.0
    last_worker_exit: float = 0.0


# ============================================================================
# ERRORS
# ============================================================================

class CPPBridgeError(RuntimeError):
    """Base bridge error."""


class SimulationUnavailableError(CPPBridgeError):
    """No usable native simulation runtime is configured."""


class SimulationQueueFullError(CPPBridgeError):
    """The bounded job queue cannot accept another job."""


class SimulationProtocolError(CPPBridgeError):
    """Native worker returned an invalid protocol envelope."""


# ============================================================================
# HELPERS
# ============================================================================

def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_request(job: SimulationJob) -> bytes:
    payload = {
        "protocol": "riot.simulation.v1",
        "type": "step",
        "job_id": job.job_id,
        "sequence": job.sequence,
        "submitted_at": job.submitted_at,
        "deadline_at": job.deadline_at,
        "priority": job.priority,
        "batch_hash": job.batch_hash,
        "batch": job.batch,
        "metadata": dict(job.metadata),
    }
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _decode_response(line: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(line.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulationProtocolError(
            f"native worker emitted invalid JSON: {exc}"
        ) from exc

    if not isinstance(payload, Mapping):
        raise SimulationProtocolError("native worker response must be a JSON object")

    return payload


def _command_from_environment() -> tuple[str, ...]:
    raw = os.getenv("RIOT_SIMULATION_BINARY", "").strip()
    if not raw:
        return ()

    try:
        parts = tuple(shlex.split(raw, posix=(os.name != "nt")))
    except ValueError as exc:
        raise SimulationUnavailableError(
            f"RIOT_SIMULATION_BINARY could not be parsed: {exc}"
        ) from exc

    return parts


# ============================================================================
# BOUNDED REGISTRIES
# ============================================================================

class _BoundedJobStore:
    def __init__(self, ttl_seconds: float, max_entries: int):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: dict[str, tuple[float, SimulationResult]] = {}
        self._lock = threading.RLock()

    def put(self, result: SimulationResult) -> None:
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            if result.job_id in self._items:
                self._items.pop(result.job_id, None)
            elif len(self._items) >= self.max_entries:
                oldest = min(self._items.items(), key=lambda item: item[1][0])[0]
                self._items.pop(oldest, None)
            self._items[result.job_id] = (now + self.ttl_seconds, result)

    def get(self, job_id: str) -> Optional[SimulationResult]:
        now = time.time()
        with self._lock:
            item = self._items.get(job_id)
            if item is None:
                return None
            expires_at, result = item
            if expires_at <= now:
                self._items.pop(job_id, None)
                return None
            return result

    def pop(self, job_id: str) -> Optional[SimulationResult]:
        with self._lock:
            item = self._items.pop(job_id, None)
            return item[1] if item else None

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_locked(time.time())

    def _cleanup_locked(self, now: float) -> None:
        expired = [
            job_id
            for job_id, (expires_at, _) in self._items.items()
            if expires_at <= now
        ]
        for job_id in expired:
            self._items.pop(job_id, None)


# ============================================================================
# WORKER SUPERVISOR
# ============================================================================

class NativeWorkerSupervisor:
    """
    Owns one persistent native runtime process.

    The process speaks newline-delimited JSON over stdin/stdout:
        request -> one JSON line
        response -> one JSON line

    The bridge itself never assumes a particular physics engine or vendor.
    """

    def __init__(self, config: SimulationConfig, stats: BridgeStats):
        self.config = config
        self.stats = stats

        self._state = (
            WorkerState.READY
            if config.executable
            else WorkerState.UNAVAILABLE
        )
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._restart_times: list[float] = []
        self._last_start_error: Optional[str] = None

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    def start(self) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            if not self.config.executable:
                self._state = WorkerState.UNAVAILABLE
                return
            if self._process and self._process.poll() is None:
                self._state = WorkerState.READY
                return

            self._state = WorkerState.STARTING
            self.stats.last_worker_start = time.time()

            env = os.environ.copy()
            env.update({str(k): str(v) for k, v in self.config.extra_environment.items()})

            try:
                self._process = subprocess.Popen(
                    list(self.config.executable),
                    cwd=self.config.working_directory,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                )
                self._state = WorkerState.READY
                self._last_start_error = None
                logger.info(
                    "Native simulation worker started pid=%s command=%s",
                    self._process.pid,
                    list(self.config.executable),
                )
            except (OSError, ValueError) as exc:
                self._process = None
                self._state = WorkerState.DEGRADED
                self._last_start_error = str(exc)
                logger.error("Failed to start native simulation worker: %s", exc)

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            self._state = WorkerState.STOPPING
            process = self._process
            self._process = None

        if process is None:
            with self._lock:
                self._state = (
                    WorkerState.UNAVAILABLE
                    if not self.config.executable
                    else WorkerState.STOPPED
                )
            return

        self._terminate_process(process)

        with self._lock:
            self._state = WorkerState.STOPPED

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            if process.poll() is None:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()

                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=2.0)
        except Exception:
            logger.exception("Error terminating native simulation worker")

    def can_restart(self) -> bool:
        now = time.time()
        with self._lock:
            cutoff = now - self.config.restart_window_seconds
            self._restart_times[:] = [
                stamp for stamp in self._restart_times if stamp >= cutoff
            ]
            return len(self._restart_times) < self.config.restart_limit

    def mark_crashed(self) -> None:
        with self._lock:
            self.stats.worker_crashes += 1
            self.stats.last_worker_exit = time.time()
            self._state = WorkerState.CRASHED
            process = self._process
            self._process = None

        if process is not None:
            try:
                process.wait(timeout=0.2)
            except Exception:
                self._terminate_process(process)

    def record_restart(self) -> float:
        now = time.time()
        with self._lock:
            self._restart_times.append(now)
            self.stats.worker_restarts += 1
            count = len(self._restart_times)

        exponent = max(0, count - 1)
        return min(
            self.config.restart_backoff_max_seconds,
            self.config.restart_backoff_base_seconds * (2 ** exponent),
        )

    def transact(self, payload: bytes, timeout: float) -> Mapping[str, Any]:
        self.start()

        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise SimulationUnavailableError(
                    self._last_start_error
                    or "native simulation worker is unavailable"
                )

        if process.stdin is None or process.stdout is None:
            raise SimulationUnavailableError(
                "native simulation worker pipes are unavailable"
            )

        try:
            process.stdin.write(payload)
            process.stdin.flush()

            # readline() itself is blocking. It runs exclusively in the native
            # worker thread and never in the 60Hz asyncio/event-loop caller.
            line = _readline_with_deadline(
                process.stdout,
                timeout=timeout,
            )
            return _decode_response(line)

        except (BrokenPipeError, OSError, SimulationProtocolError) as exc:
            self.mark_crashed()
            raise SimulationProtocolError(str(exc)) from exc
        except TimeoutError:
            self.mark_crashed()
            raise


def _readline_with_deadline(
    stream: Any,
    timeout: float,
) -> bytes:
    """
    Read one protocol line with a deadline.

    The native worker is isolated in the bridge's worker thread, so a blocking
    read cannot stall the application event loop. If the stream never returns,
    the worker process is killed by the supervisor on timeout.
    """
    # Portable baseline: a file.readline() call has no universal timeout.
    # Run it in a helper thread and wait for the bounded deadline.
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            result_queue.put((True, stream.readline()))
        except Exception as exc:  # pragma: no cover - defensive
            result_queue.put((False, exc))

    reader = threading.Thread(
        target=_reader,
        name="riot-cpp-readline",
        daemon=True,
    )
    reader.start()

    try:
        ok, value = result_queue.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError(
            f"native worker response timeout after {timeout:.3f}s"
        ) from exc

    if not ok:
        raise OSError(str(value))

    if not value:
        raise OSError("native worker closed stdout")
    return value


# ============================================================================
# BRIDGE
# ============================================================================

class SimulationCPPAdapter:
    """
    Non-blocking compatibility adapter for the Python simulation scheduler.

    ``execute(batch)`` only validates/enqueues work. It does not compile C++,
    spawn per-task processes, or wait for physics results.

    ``poll_result(job_id)`` / ``get_result(job_id)`` expose completion.
    """

    def __init__(
        self,
        workspace_dir: str = "workspace_cpp",
        *,
        config: Optional[SimulationConfig] = None,
    ) -> None:
        self.workspace_dir = str(Path(workspace_dir).expanduser())
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)

        self.config = config or SimulationConfig(
            executable=_command_from_environment(),
            working_directory=os.getenv("RIOT_SIMULATION_WORKDIR")
            or self.workspace_dir,
        )

        self.stats = BridgeStats()
        self._jobs: dict[str, SimulationJob] = {}
        self._job_lock = threading.RLock()
        self._sequence = 0
        self._stop = threading.Event()

        self._job_queue: queue.Queue[SimulationJob] = queue.Queue(
            maxsize=self.config.queue_capacity
        )
        self._result_queue: queue.Queue[SimulationResult] = queue.Queue(
            maxsize=self.config.result_capacity
        )
        self._results = _BoundedJobStore(
            ttl_seconds=self.config.job_ttl_seconds,
            max_entries=self.config.max_registry_entries,
        )
        self._worker = NativeWorkerSupervisor(self.config, self.stats)

        self._thread = threading.Thread(
            target=self._worker_loop,
            name="riot-native-simulation",
            daemon=True,
        )
        self._thread.start()

        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="riot-cpp-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

        logger.info(
            "SimulationCPPAdapter initialized state=%s queue=%d result=%d",
            self.state.value,
            self.config.queue_capacity,
            self.config.result_capacity,
        )

    # ------------------------------------------------------------------
    # Public state / health
    # ------------------------------------------------------------------
    @property
    def state(self) -> WorkerState:
        return self._worker.state

    def health(self) -> dict[str, Any]:
        with self._job_lock:
            active_jobs = len(self._jobs)

        return {
            "status": "ready" if self.state is WorkerState.READY else self.state.value,
            "worker_state": self.state.value,
            "worker_pid": (
                self._worker._process.pid
                if self._worker._process is not None
                and self._worker._process.poll() is None
                else None
            ),
            "queue_depth": self._job_queue.qsize(),
            "queue_capacity": self.config.queue_capacity,
            "result_depth": self._result_queue.qsize(),
            "result_capacity": self.config.result_capacity,
            "active_jobs": active_jobs,
            "native_configured": bool(self.config.executable),
            "native_command": list(self.config.executable),
            "last_error": self.stats.last_error,
            "stats": asdict(self.stats),
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_simulation": bool(self.config.executable),
            "persistent_worker": True,
            "non_blocking_submit": True,
            "bounded_job_queue": True,
            "bounded_result_queue": True,
            "crash_recovery": bool(self.config.executable),
            "timeouts": True,
            "cancellation": True,
            "protocol": "riot.simulation.v1",
        }

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    def submit(
        self,
        batch: Any,
        *,
        priority: int = 0,
        timeout_seconds: Optional[float] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        now = time.time()
        job_id = f"SIM_{uuid.uuid4().hex}"
        serialized = _canonical_json(batch)
        size = len(serialized.encode("utf-8"))

        with self._job_lock:
            self.stats.submitted += 1

        if size > self.config.max_batch_bytes:
            self.stats.rejected += 1
            self.stats.last_error = (
                f"batch exceeds {self.config.max_batch_bytes} bytes"
            )
            result = SimulationResult(
                job_id=job_id,
                sequence=self._next_sequence(),
                status=JobStatus.REJECTED,
                submitted_at=now,
                finished_at=now,
                error=self.stats.last_error,
            )
            self._store_result(result)
            return result.to_dict()

        # A native engine is not optional for actual simulation. We fail closed.
        if not self.config.executable:
            self.stats.unavailable += 1
            result = SimulationResult(
                job_id=job_id,
                sequence=self._next_sequence(),
                status=JobStatus.UNAVAILABLE,
                submitted_at=now,
                finished_at=now,
                error=(
                    "No native simulation executable configured. "
                    "Set RIOT_SIMULATION_BINARY to a real persistent engine."
                ),
                metadata={
                    "capabilities": self.capabilities(),
                },
            )
            self._store_result(result)
            return result.to_dict()

        timeout = (
            max(0.01, float(timeout_seconds))
            if timeout_seconds is not None
            else self.config.job_timeout_seconds
        )

        sequence = self._next_sequence()
        job = SimulationJob(
            job_id=job_id,
            sequence=sequence,
            submitted_at=now,
            deadline_at=now + timeout,
            batch=batch,
            batch_hash=_sha256_text(serialized),
            priority=int(priority),
            metadata=dict(metadata or {}),
        )

        with self._job_lock:
            if len(self._jobs) >= self.config.max_registry_entries:
                self._cleanup_jobs_locked()
            self._jobs[job_id] = job

        try:
            self._enqueue(job)
        except queue.Full:
            self.stats.rejected += 1
            with self._job_lock:
                self._jobs.pop(job_id, None)

            result = SimulationResult(
                job_id=job_id,
                sequence=sequence,
                status=JobStatus.REJECTED,
                submitted_at=now,
                finished_at=now,
                error="simulation queue is full",
            )
            self._store_result(result)
            return result.to_dict()

        self.stats.accepted += 1
        return {
            "job_id": job_id,
            "sequence": sequence,
            "status": JobStatus.QUEUED.value,
            "submitted_at": now,
            "deadline_at": job.deadline_at,
            "batch_hash": job.batch_hash,
        }

    def execute(self, batch: Any) -> dict[str, Any]:
        """
        Compatibility method used by the current main.py tick loop.

        It is intentionally non-blocking. It queues the batch and returns a
        submission/result envelope immediately.
        """
        return self.submit(batch)

    def _enqueue(self, job: SimulationJob) -> None:
        try:
            self._job_queue.put_nowait(job)
        except queue.Full:
            if self.config.backpressure is BackpressurePolicy.DROP_OLDEST:
                try:
                    dropped = self._job_queue.get_nowait()
                    self.stats.dropped += 1
                    self._drop_job(dropped)
                    self._job_queue.put_nowait(job)
                    return
                except queue.Empty:
                    pass
            raise

        self.stats.queue_high_watermark = max(
            self.stats.queue_high_watermark,
            self._job_queue.qsize(),
        )

    # ------------------------------------------------------------------
    # Results / cancellation
    # ------------------------------------------------------------------
    def poll_result(self, job_id: str) -> Optional[dict[str, Any]]:
        result = self._results.get(job_id)
        return result.to_dict() if result else None

    def get_result(self, job_id: str) -> Optional[dict[str, Any]]:
        return self.poll_result(job_id)

    def wait_result(
        self,
        job_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Optional[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            result = self.poll_result(job_id)
            if result is not None:
                return result
            time.sleep(0.002)
        return self.poll_result(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._job_lock:
            job = self._jobs.pop(job_id, None)

        if job is None:
            return False

        result = SimulationResult(
            job_id=job.job_id,
            sequence=job.sequence,
            status=JobStatus.CANCELLED,
            submitted_at=job.submitted_at,
            finished_at=time.time(),
            error="cancelled before native execution",
        )
        self.stats.cancelled += 1
        self._store_result(result)
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def shutdown(self, wait: bool = True) -> None:
        if self._stop.is_set():
            return

        self._stop.set()
        self._worker.stop()

        if wait:
            self._thread.join(timeout=3.0)
            self._maintenance_thread.join(timeout=1.0)

        with self._job_lock:
            self._jobs.clear()

    close = shutdown

    # ------------------------------------------------------------------
    # Internal worker loop
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._job_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if self._is_cancelled_or_missing(job.job_id):
                    self._job_queue.task_done()
                    continue

                result = self._execute_native_job(job)
                self._store_result(result)

                if result.status is JobStatus.SUCCESS:
                    self.stats.completed += 1
                elif result.status is JobStatus.TIMEOUT:
                    self.stats.timed_out += 1
                    self.stats.failed += 1
                elif result.status is JobStatus.CANCELLED:
                    self.stats.cancelled += 1
                else:
                    self.stats.failed += 1

            except Exception as exc:  # pragma: no cover - defensive
                self.stats.failed += 1
                self.stats.last_error = str(exc)
                logger.exception("Unexpected simulation worker-loop failure")
                result = SimulationResult(
                    job_id=job.job_id,
                    sequence=job.sequence,
                    status=JobStatus.FAILED,
                    submitted_at=job.submitted_at,
                    finished_at=time.time(),
                    error=str(exc),
                )
                self._store_result(result)
            finally:
                self._job_queue.task_done()

    def _execute_native_job(self, job: SimulationJob) -> SimulationResult:
        started = time.time()

        with self._job_lock:
            current = self._jobs.get(job.job_id)
        if current is None:
            return SimulationResult(
                job_id=job.job_id,
                sequence=job.sequence,
                status=JobStatus.CANCELLED,
                submitted_at=job.submitted_at,
                started_at=started,
                finished_at=started,
                error="job removed before execution",
            )

        if time.time() >= job.deadline_at:
            with self._job_lock:
                self._jobs.pop(job.job_id, None)
            return SimulationResult(
                job_id=job.job_id,
                sequence=job.sequence,
                status=JobStatus.TIMEOUT,
                submitted_at=job.submitted_at,
                started_at=started,
                finished_at=time.time(),
                duration_ms=(time.time() - started) * 1000.0,
                error="simulation job expired before worker execution",
            )

        try:
            response = self._worker.transact(
                _encode_request(job),
                timeout=max(
                    0.001,
                    min(
                        self.config.worker_read_timeout_seconds,
                        job.deadline_at - time.time(),
                    ),
                ),
            )

            response_job_id = str(response.get("job_id", job.job_id))
            if response_job_id != job.job_id:
                raise SimulationProtocolError(
                    f"job id mismatch: expected {job.job_id}, got {response_job_id}"
                )

            response_status = str(response.get("status", "success")).lower()

            if response_status in {"success", "ok", "completed"}:
                status = JobStatus.SUCCESS
                output = response.get("output", response.get("result"))
                error = None
            elif response_status in {"cancelled", "canceled"}:
                status = JobStatus.CANCELLED
                output = None
                error = str(response.get("error", "native simulation cancelled"))
            elif response_status in {"timeout", "timed_out"}:
                status = JobStatus.TIMEOUT
                output = None
                error = str(response.get("error", "native simulation timed out"))
            else:
                status = JobStatus.FAILED
                output = response.get("output", response.get("result"))
                error = str(response.get("error", "native simulation failed"))

            finished = time.time()

            with self._job_lock:
                self._jobs.pop(job.job_id, None)

            return SimulationResult(
                job_id=job.job_id,
                sequence=job.sequence,
                status=status,
                submitted_at=job.submitted_at,
                started_at=started,
                finished_at=finished,
                duration_ms=(finished - started) * 1000.0,
                worker_pid=(
                    self._worker._process.pid
                    if self._worker._process is not None
                    else None
                ),
                output=output,
                error=error,
                metadata={
                    "response_protocol": response.get(
                        "protocol", "riot.simulation.v1"
                    ),
                    "batch_hash": job.batch_hash,
                },
            )

        except TimeoutError as exc:
            self.stats.last_error = str(exc)
            self._recover_worker()
            with self._job_lock:
                self._jobs.pop(job.job_id, None)

            finished = time.time()
            return SimulationResult(
                job_id=job.job_id,
                sequence=job.sequence,
                status=JobStatus.TIMEOUT,
                submitted_at=job.submitted_at,
                started_at=started,
                finished_at=finished,
                duration_ms=(finished - started) * 1000.0,
                error=str(exc),
            )
        except SimulationUnavailableError as exc:
            self.stats.last_error = str(exc)
            with self._job_lock:
                self._jobs.pop(job.job_id, None)

            finished = time.time()
            return SimulationResult(
                job_id=job.job_id,
                sequence=job.sequence,
                status=JobStatus.UNAVAILABLE,
                submitted_at=job.submitted_at,
                started_at=started,
                finished_at=finished,
                duration_ms=(finished - started) * 1000.0,
                error=str(exc),
            )
        except Exception as exc:
            self.stats.last_error = str(exc)
            logger.error(
                "Native simulation job %s failed: %s",
                job.job_id,
                exc,
            )
            self._recover_worker()

            with self._job_lock:
                self._jobs.pop(job.job_id, None)

            finished = time.time()
            return SimulationResult(
                job_id=job.job_id,
                sequence=job.sequence,
                status=JobStatus.FAILED,
                submitted_at=job.submitted_at,
                started_at=started,
                finished_at=finished,
                duration_ms=(finished - started) * 1000.0,
                error=str(exc),
            )

    def _recover_worker(self) -> None:
        if self._stop.is_set() or not self.config.executable:
            return

        self._worker.mark_crashed()

        if not self._worker.can_restart():
            self.stats.last_error = (
                "native worker restart limit reached; "
                "runtime remains degraded until manual recovery"
            )
            return

        delay = self._worker.record_restart()
        logger.warning(
            "Restarting native simulation worker after %.2fs",
            delay,
        )
        time.sleep(delay)
        self._worker.start()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def _maintenance_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._results.cleanup()
                self._expire_jobs()
                self._drain_result_queue()
            except Exception:
                logger.exception("CPP bridge maintenance cycle failed")

    def _expire_jobs(self) -> None:
        now = time.time()
        expired: list[SimulationJob] = []

        with self._job_lock:
            for job in list(self._jobs.values()):
                if now >= job.deadline_at:
                    expired.append(job)
            for job in expired:
                self._jobs.pop(job.job_id, None)

        for job in expired:
            result = SimulationResult(
                job_id=job.job_id,
                sequence=job.sequence,
                status=JobStatus.TIMEOUT,
                submitted_at=job.submitted_at,
                finished_at=now,
                duration_ms=max(0.0, (now - job.submitted_at) * 1000.0),
                error="simulation job deadline expired",
            )
            self.stats.timed_out += 1
            self._store_result(result)

    def _store_result(self, result: SimulationResult) -> None:
        self._results.put(result)

        try:
            self._result_queue.put_nowait(result)
        except queue.Full:
            # The canonical result registry remains authoritative even when
            # the notification queue is saturated.
            pass

        self.stats.result_high_watermark = max(
            self.stats.result_high_watermark,
            self._result_queue.qsize(),
        )

    def _drain_result_queue(self) -> None:
        while True:
            try:
                self._result_queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._result_queue.task_done()

    def _drop_job(self, job: SimulationJob) -> None:
        with self._job_lock:
            self._jobs.pop(job.job_id, None)

        result = SimulationResult(
            job_id=job.job_id,
            sequence=job.sequence,
            status=JobStatus.CANCELLED,
            submitted_at=job.submitted_at,
            finished_at=time.time(),
            error="dropped by bridge backpressure policy",
        )
        self.stats.cancelled += 1
        self._store_result(result)

    def _is_cancelled_or_missing(self, job_id: str) -> bool:
        with self._job_lock:
            return job_id not in self._jobs

    def _cleanup_jobs_locked(self) -> None:
        now = time.time()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if now >= job.deadline_at
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)

    def _next_sequence(self) -> int:
        with self._job_lock:
            self._sequence += 1
            return self._sequence


# ============================================================================
# PUBLIC HELPERS
# ============================================================================

def create_simulation_bridge(
    workspace_dir: str = "workspace_cpp",
    **kwargs: Any,
) -> SimulationCPPAdapter:
    """Factory kept separate for dependency injection and future test harnesses."""
    config = kwargs.pop("config", None)
    return SimulationCPPAdapter(workspace_dir=workspace_dir, config=config)


__all__ = [
    "BackpressurePolicy",
    "BridgeStats",
    "CPPBridgeError",
    "JobStatus",
    "NativeWorkerSupervisor",
    "SimulationCPPAdapter",
    "SimulationConfig",
    "SimulationJob",
    "SimulationProtocolError",
    "SimulationQueueFullError",
    "SimulationResult",
    "SimulationUnavailableError",
    "WorkerState",
    "create_simulation_bridge",
]
