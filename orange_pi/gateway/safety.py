"""Strict browser-message validation and testable drive safety state."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
from typing import Callable, Mapping


MAX_LINEAR_MPS = 1.0
MAX_ANGULAR_RPS = 3.0
MAX_SEQUENCE = 0xFFFFFFFF


class MessageValidationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DriveMessage:
    linear: float
    angular: float
    seq: int


@dataclass(frozen=True, slots=True)
class EstopMessage:
    seq: int


@dataclass(frozen=True, slots=True)
class ClearEstopMessage:
    seq: int


ClientMessage = DriveMessage | EstopMessage | ClearEstopMessage


def _reject_json_constant(value: str) -> None:
    raise MessageValidationError("invalid_json_number", f"{value} is not valid JSON")


def _require_exact_keys(message: Mapping[str, object], expected: set[str]) -> None:
    keys = set(message)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise MessageValidationError(
            "invalid_fields", f"missing={missing or []}, extra={extra or []}"
        )


def _parse_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MessageValidationError("invalid_seq", "seq must be a uint32 integer")
    if not 0 <= value <= MAX_SEQUENCE:
        raise MessageValidationError("invalid_seq", "seq must be between 0 and 4294967295")
    return value


def _parse_finite_number(name: str, value: object, limit: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MessageValidationError("invalid_drive", f"{name} must be a JSON number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise MessageValidationError("invalid_drive", f"{name} must be finite")
    if abs(parsed) > limit:
        raise MessageValidationError(
            "drive_out_of_range", f"{name} must be within {-limit}..{limit}"
        )
    return parsed


def parse_client_message(raw: str | bytes | Mapping[str, object]) -> ClientMessage:
    """Parse one message and reject coercions, NaN and unknown fields."""

    if isinstance(raw, Mapping):
        message = raw
    else:
        try:
            message = json.loads(raw, parse_constant=_reject_json_constant)
        except MessageValidationError:
            raise
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise MessageValidationError("invalid_json", "message must be valid JSON") from exc

    if not isinstance(message, Mapping):
        raise MessageValidationError("invalid_message", "message must be a JSON object")
    message_type = message.get("type")

    if message_type == "drive":
        _require_exact_keys(message, {"type", "linear", "angular", "seq"})
        return DriveMessage(
            linear=_parse_finite_number("linear", message["linear"], MAX_LINEAR_MPS),
            angular=_parse_finite_number("angular", message["angular"], MAX_ANGULAR_RPS),
            seq=_parse_sequence(message["seq"]),
        )
    if message_type == "estop":
        _require_exact_keys(message, {"type", "seq"})
        return EstopMessage(seq=_parse_sequence(message["seq"]))
    if message_type == "clear_estop":
        _require_exact_keys(message, {"type", "seq", "confirm"})
        if message["confirm"] is not True:
            raise MessageValidationError(
                "confirmation_required", "clear_estop requires confirm=true"
            )
        return ClearEstopMessage(seq=_parse_sequence(message["seq"]))

    raise MessageValidationError(
        "unsupported_type", "type must be drive, estop, or clear_estop"
    )


@dataclass(frozen=True, slots=True)
class DriveOutput:
    linear: float
    angular: float
    client_seq: int | None
    cause: str


@dataclass(frozen=True, slots=True)
class Decision:
    accepted: bool
    reason: str
    output: DriveOutput | None = None


@dataclass(frozen=True, slots=True)
class WatchdogAction:
    output: DriveOutput
    newly_tripped: bool


class SafetyController:
    """Pure state machine; async and UART concerns live in app.py."""

    def __init__(
        self,
        watchdog_seconds: float = 0.2,
        zero_repeat_seconds: float = 0.1,
        clear_ack_timeout_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < watchdog_seconds <= 0.2:
            raise ValueError("watchdog_seconds must be within 0..0.2")
        if not 0 < zero_repeat_seconds <= watchdog_seconds:
            raise ValueError("zero_repeat_seconds must be within watchdog interval")
        if clear_ack_timeout_seconds <= 0:
            raise ValueError("clear_ack_timeout_seconds must be positive")
        self.watchdog_seconds = watchdog_seconds
        self.zero_repeat_seconds = zero_repeat_seconds
        self.clear_ack_timeout_seconds = clear_ack_timeout_seconds
        self._clock = clock
        self.serial_ready = False
        self.estop_latched = False
        self.hardware_fault_latched = False
        self.hardware_fault_code = 0
        self.clear_estop_pending = False
        self.clear_estop_pending_since: float | None = None
        self.watchdog_tripped = True
        self.last_drive_at: float | None = None
        self.last_zero_at: float | None = None
        self.last_client_seq: int | None = None
        self.current = DriveOutput(0.0, 0.0, None, "startup")

    def begin_client(self) -> None:
        self.last_client_seq = None
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, None, "client_connected")

    def end_client(self) -> DriveOutput:
        self.last_drive_at = None
        self.last_client_seq = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, None, "client_disconnected")
        return self.current

    def set_serial_ready(self, ready: bool) -> DriveOutput:
        self.serial_ready = ready
        # A pending clear belongs to one live UART transaction.  A state change
        # invalidates it; critically, it never clears the latch.
        self.clear_estop_pending = False
        self.clear_estop_pending_since = None
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.last_zero_at = None
        cause = "serial_connected" if ready else "serial_disconnected"
        self.current = DriveOutput(0.0, 0.0, None, cause)
        return self.current

    def _is_newer_sequence(self, candidate: int) -> bool:
        if self.last_client_seq is None:
            return True
        distance = (candidate - self.last_client_seq) & MAX_SEQUENCE
        return 0 < distance < 0x80000000

    def _consume_sequence(self, sequence: int) -> bool:
        if not self._is_newer_sequence(sequence):
            return False
        self.last_client_seq = sequence
        return True

    def accept_drive(self, message: DriveMessage) -> Decision:
        if not self._consume_sequence(message.seq):
            return Decision(False, "stale_seq")
        if not self.serial_ready:
            return Decision(False, "serial_unavailable")
        if self.hardware_fault_latched:
            self.current = DriveOutput(0.0, 0.0, message.seq, "hardware_fault_latched")
            return Decision(False, "hardware_fault_latched", self.current)
        if self.estop_latched:
            self.current = DriveOutput(0.0, 0.0, message.seq, "estop_latched")
            return Decision(False, "estop_latched", self.current)

        now = self._clock()
        self.last_drive_at = now
        self.watchdog_tripped = False
        self.current = DriveOutput(message.linear, message.angular, message.seq, "drive")
        return Decision(True, "accepted", self.current)

    def latch_estop(self, message: EstopMessage) -> Decision:
        # A syntactically valid stop always wins, even if its sequence is a duplicate.
        if self._is_newer_sequence(message.seq):
            self.last_client_seq = message.seq
        self.estop_latched = True
        self.clear_estop_pending = False
        self.clear_estop_pending_since = None
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, message.seq, "estop")
        return Decision(True, "estop_latched", self.current)

    def observe_hardware_estop(self, active: bool) -> Decision:
        """Synchronize STM32 state in the safe direction only.

        A true STATUS bit always locks the gateway, including after a gateway
        restart.  A false bit is only telemetry and can never bypass the
        CLEAR_ESTOP request/ACK transaction.
        """

        if not active:
            return Decision(True, "hardware_estop_false_observed")
        self.estop_latched = True
        # STATUS commonly remains true while CLEAR_ESTOP is awaiting its ACK.
        # Preserve that transaction; V1 does not infer a new physical-button
        # edge from a repeated level=true sample.
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, None, "hardware_estop_latched")
        return Decision(True, "hardware_estop_latched", self.current)

    def observe_hardware_fault(self, fault_code: int) -> Decision:
        """Latch any non-zero STM32 fault; zero telemetry never auto-clears it."""

        if fault_code == 0:
            return Decision(True, "hardware_fault_zero_observed")
        self.hardware_fault_latched = True
        self.hardware_fault_code = int(fault_code)
        # As with periodic estop=true, preserve an in-flight reset transaction.
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, None, "hardware_fault_latched")
        return Decision(True, "hardware_fault_latched", self.current)

    def request_clear_estop(self, message: ClearEstopMessage) -> Decision:
        if not self._consume_sequence(message.seq):
            return Decision(False, "stale_seq")
        if not self.serial_ready:
            return Decision(False, "serial_unavailable")
        if not self.estop_latched and not self.hardware_fault_latched:
            # Do not report a successful hardware reset when the gateway has no
            # latch to clear; the caller gets a terminal, non-pending failure.
            return Decision(False, "estop_not_latched")
        if self.clear_estop_pending:
            return Decision(False, "clear_estop_pending")

        # Real hardware remains latched until app.py matches an STM32 ACK with
        # the exact CLEAR_ESTOP transport sequence and an OK result.
        self.clear_estop_pending = True
        self.clear_estop_pending_since = self._clock()
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, message.seq, "awaiting_stm32_ack")
        return Decision(True, "awaiting_stm32_ack", self.current)

    def confirm_clear_estop(self, reason: str = "estop_cleared") -> Decision:
        if not self.clear_estop_pending or not (
            self.estop_latched or self.hardware_fault_latched
        ):
            return Decision(False, "no_clear_estop_pending")
        self.clear_estop_pending = False
        self.clear_estop_pending_since = None
        self.estop_latched = False
        self.hardware_fault_latched = False
        self.hardware_fault_code = 0
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, None, reason)
        return Decision(True, reason, self.current)

    def reject_clear_estop(self, reason: str) -> Decision:
        self.clear_estop_pending = False
        self.clear_estop_pending_since = None
        if not self.estop_latched and not self.hardware_fault_latched:
            self.estop_latched = True
        self.last_drive_at = None
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, None, reason)
        return Decision(False, reason, self.current)

    def poll_clear_estop_timeout(self) -> Decision | None:
        if not self.clear_estop_pending or self.clear_estop_pending_since is None:
            return None
        elapsed = self._clock() - self.clear_estop_pending_since
        if elapsed + 1e-12 < self.clear_ack_timeout_seconds:
            return None
        return self.reject_clear_estop("stm32_ack_timeout")

    def poll_watchdog(self) -> WatchdogAction | None:
        if not self.serial_ready:
            return None
        now = self._clock()
        # A tiny epsilon prevents an exact 200 ms boundary from becoming
        # 199.999999999 ms due to binary floating-point representation.
        stale = (
            self.last_drive_at is None
            or now - self.last_drive_at + 1e-12 >= self.watchdog_seconds
        )
        if not stale:
            return None

        newly_tripped = not self.watchdog_tripped
        self.watchdog_tripped = True
        self.current = DriveOutput(0.0, 0.0, None, "web_watchdog")
        if (
            self.last_zero_at is not None
            and now - self.last_zero_at + 1e-12 < self.zero_repeat_seconds
        ):
            return None
        self.last_zero_at = now
        return WatchdogAction(self.current, newly_tripped)

    def snapshot(self) -> dict[str, object]:
        now = self._clock()
        age_ms = None
        if self.last_drive_at is not None:
            age_ms = max(0, round((now - self.last_drive_at) * 1000))
        return {
            "serial_ready": self.serial_ready,
            "estop_latched": self.estop_latched,
            "hardware_fault_latched": self.hardware_fault_latched,
            "hardware_fault_code": self.hardware_fault_code,
            "clear_estop_pending": self.clear_estop_pending,
            "watchdog": "tripped" if self.watchdog_tripped else "healthy",
            "last_drive_age_ms": age_ms,
            "command": {
                "linear": self.current.linear,
                "angular": self.current.angular,
                "cause": self.current.cause,
            },
        }
