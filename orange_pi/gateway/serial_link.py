"""Single-owner pyserial worker used by the FastAPI event loop."""

from __future__ import annotations

from collections import deque
import threading
from typing import Callable, Literal

from protocol import Frame, FrameDecoder

try:
    import serial  # type: ignore[import-untyped]
except ImportError:  # Tests for the pure protocol/safety modules need no pyserial.
    serial = None


FrameCallback = Callable[[Frame], None]
StateCallback = Callable[[bool, str | None], None]
WriteKind = Literal["drive", "control", "safety"]


class OutboundBuffer:
    """Thread-safe priority buffer with latest-value drive coalescing.

    Non-zero DRIVE commands never form a FIFO backlog.  A safety insertion
    atomically removes the unsent drive and is always popped before control and
    drive traffic.  The one frame already inside ``serial.write`` cannot be
    recalled, but no older queued velocity can follow the safety frame.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._safety: deque[tuple[str, bytes]] = deque()
        self._control: deque[bytes] = deque()
        self._latest_drive: bytes | None = None

    def put_latest_drive(self, data: bytes) -> None:
        with self._lock:
            self._latest_drive = bytes(data)

    def put_control(self, data: bytes) -> None:
        with self._lock:
            self._control.append(bytes(data))

    def put_safety(self, data: bytes, key: str, drop_controls: bool = False) -> None:
        with self._lock:
            self._latest_drive = None
            if drop_controls:
                self._control.clear()
            # Coalesce repeated watchdog/disconnect zero frames while preserving
            # the FIFO order between distinct safety actions such as ESTOP→zero.
            self._safety = deque(item for item in self._safety if item[0] != key)
            self._safety.append((key, bytes(data)))

    def pop_next(self) -> bytes | None:
        with self._lock:
            if self._safety:
                return self._safety.popleft()[1]
            if self._control:
                return self._control.popleft()
            if self._latest_drive is not None:
                data = self._latest_drive
                self._latest_drive = None
                return data
            return None

    def clear(self) -> None:
        with self._lock:
            self._safety.clear()
            self._control.clear()
            self._latest_drive = None

    def pending_counts(self) -> dict[str, int]:
        """Return a test/diagnostic snapshot without exposing frame contents."""

        with self._lock:
            return {
                "safety": len(self._safety),
                "control": len(self._control),
                "drive": int(self._latest_drive is not None),
            }


class SerialLink:
    """Own a serial port in one background thread and never replay stale writes."""

    def __init__(
        self,
        port: str,
        baud: int,
        dry_run: bool,
        on_frame: FrameCallback,
        on_state: StateCallback,
        reconnect_seconds: float = 1.0,
    ) -> None:
        self.port = port
        self.baud = baud
        self.dry_run = dry_run
        self.on_frame = on_frame
        self.on_state = on_state
        self.reconnect_seconds = reconnect_seconds
        self._writes = OutboundBuffer()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._connected = False
        self._last_error: str | None = None

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    @property
    def last_error(self) -> str | None:
        with self._state_lock:
            return self._last_error

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rover-uart", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._set_state(False, "gateway_stopped")
        self._discard_writes()

    def send(
        self,
        frame_bytes: bytes,
        *,
        kind: WriteKind = "control",
        safety_key: str = "stop",
        drop_controls: bool = False,
    ) -> bool:
        if self.dry_run:
            # This acknowledges only that the gateway simulation accepted bytes.
            return True
        if not self.connected:
            return False
        if kind == "drive":
            self._writes.put_latest_drive(frame_bytes)
        elif kind == "safety":
            self._writes.put_safety(frame_bytes, safety_key, drop_controls)
        elif kind == "control":
            self._writes.put_control(frame_bytes)
        else:  # Defensive guard for untyped callers.
            raise ValueError(f"unsupported write kind: {kind}")
        return True

    def _set_state(self, connected: bool, error: str | None) -> None:
        with self._state_lock:
            changed = connected != self._connected or error != self._last_error
            self._connected = connected
            self._last_error = error
        if changed:
            self.on_state(connected, error)

    def _discard_writes(self) -> None:
        self._writes.clear()

    def _run(self) -> None:
        if self.dry_run:
            self._set_state(False, None)
            self._stop.wait()
            return
        if serial is None:
            self._set_state(False, "pyserial is not installed")
            self._stop.wait()
            return

        while not self._stop.is_set():
            port_handle = None
            try:
                port_handle = serial.Serial(
                    self.port,
                    self.baud,
                    timeout=0.02,
                    write_timeout=0.1,
                )
                port_handle.reset_input_buffer()
                port_handle.reset_output_buffer()
                decoder = FrameDecoder()
                self._discard_writes()
                self._set_state(True, None)

                while not self._stop.is_set():
                    outgoing = self._writes.pop_next()
                    if outgoing is not None:
                        port_handle.write(outgoing)
                        port_handle.flush()

                    incoming = port_handle.read(256)
                    for frame in decoder.feed(incoming):
                        self.on_frame(frame)
            except Exception as exc:  # pyserial exposes platform-specific exception types.
                self._discard_writes()
                self._set_state(False, f"{type(exc).__name__}: {exc}")
            finally:
                if port_handle is not None:
                    try:
                        port_handle.close()
                    except Exception:
                        pass

            self._stop.wait(self.reconnect_seconds)

        self._set_state(False, "gateway_stopped")
