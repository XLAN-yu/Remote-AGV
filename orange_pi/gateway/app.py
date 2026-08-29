"""FastAPI WebSocket-to-UART safety gateway for Rover One V1."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import os
from pathlib import Path
import time
from typing import Any
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from protocol import (
    Frame,
    MessageType,
    decode_ack_payload,
    decode_status_payload,
    encode_drive_payload,
    encode_frame,
)
from safety import (
    ClearEstopMessage,
    DriveMessage,
    EstopMessage,
    MessageValidationError,
    SafetyController,
    parse_client_message,
)
from serial_link import SerialLink, WriteKind


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    serial_port: str
    baud: int
    dry_run: bool
    watchdog_seconds: float
    allowed_origins: tuple[str, ...]
    web_root: Path | None

    @classmethod
    def from_environment(cls) -> "Settings":
        serial_port = os.getenv("ROVER_SERIAL_PORT", "dry-run").strip() or "dry-run"
        dry_run = _truthy(os.getenv("ROVER_DRY_RUN")) or serial_port.lower() in {
            "dry-run",
            "dryrun",
            "mock",
            "none",
        }

        try:
            baud = int(os.getenv("ROVER_BAUD", "115200"))
        except ValueError as exc:
            raise RuntimeError("ROVER_BAUD must be an integer") from exc
        if not 1200 <= baud <= 4_000_000:
            raise RuntimeError("ROVER_BAUD must be between 1200 and 4000000")

        try:
            watchdog_ms = int(os.getenv("ROVER_WATCHDOG_MS", "200"))
        except ValueError as exc:
            raise RuntimeError("ROVER_WATCHDOG_MS must be an integer") from exc
        # Configuration may make stopping faster, never slower than the V1 contract.
        if not 50 <= watchdog_ms <= 200:
            raise RuntimeError("ROVER_WATCHDOG_MS must be between 50 and 200")

        origins_raw = os.getenv("ROVER_ALLOWED_ORIGINS", "*")
        origins = tuple(item.strip() for item in origins_raw.split(",") if item.strip())
        if not origins:
            raise RuntimeError("ROVER_ALLOWED_ORIGINS must contain at least one origin")

        web_root_raw = os.getenv("ROVER_WEB_ROOT", "").strip()
        web_root = Path(web_root_raw).expanduser().resolve() if web_root_raw else None
        if web_root is not None and not web_root.is_dir():
            raise RuntimeError(f"ROVER_WEB_ROOT is not a directory: {web_root}")

        return cls(
            serial_port=serial_port,
            baud=baud,
            dry_run=dry_run,
            watchdog_seconds=watchdog_ms / 1000.0,
            allowed_origins=origins,
            web_root=web_root,
        )


@dataclass(frozen=True, slots=True)
class PendingAck:
    client_seq: int
    message_type: MessageType
    queued_at: float


class GatewayRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.safety = SafetyController(watchdog_seconds=settings.watchdog_seconds)
        self.link = SerialLink(
            port=settings.serial_port,
            baud=settings.baud,
            dry_run=settings.dry_run,
            on_frame=self._on_serial_frame_thread,
            on_state=self._on_serial_state_thread,
        )
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._dry_run_telemetry_task: asyncio.Task[None] | None = None
        self._client_lock = asyncio.Lock()
        self._active_client: tuple[str, WebSocket] | None = None
        self._transport_sequence = 0
        self._pending_acks: dict[int, PendingAck] = {}
        self._pending_drive_transport_sequence: int | None = None
        self._pending_clear_transport_sequence: int | None = None

    async def start(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        # Simulation is ready for protocol/UI testing, but never reports UART connected.
        if self.settings.dry_run:
            self.safety.set_serial_ready(True)
        self.link.start()
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="rover-web-watchdog"
        )
        if self.settings.dry_run:
            self._dry_run_telemetry_task = asyncio.create_task(
                self._dry_run_telemetry_loop(), name="rover-dry-run-telemetry"
            )

    async def stop(self) -> None:
        self._cancel_pending_clear("gateway_shutdown")
        if self.safety.serial_ready:
            self._send_drive(
                0.0,
                0.0,
                None,
                "gateway_shutdown",
                drop_controls=True,
            )
            await asyncio.sleep(0.04)
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        if self._dry_run_telemetry_task:
            self._dry_run_telemetry_task.cancel()
            try:
                await self._dry_run_telemetry_task
            except asyncio.CancelledError:
                pass
            self._dry_run_telemetry_task = None
        self.link.stop()
        self.safety.set_serial_ready(False)

    def _next_transport_sequence(self) -> int:
        self._transport_sequence = (self._transport_sequence + 1) & 0xFFFFFFFF
        return self._transport_sequence

    def _queue_frame(
        self,
        message_type: MessageType,
        payload: bytes,
        client_seq: int | None,
        *,
        delivery: WriteKind = "control",
        safety_key: str = "stop",
        drop_controls: bool = False,
    ) -> int | None:
        transport_seq = self._next_transport_sequence()
        encoded = encode_frame(Frame(message_type, transport_seq, payload))
        queued = self.link.send(
            encoded,
            kind=delivery,
            safety_key=safety_key,
            drop_controls=drop_controls,
        )
        if queued and delivery in {"drive", "safety"}:
            if self._pending_drive_transport_sequence is not None:
                self._pending_acks.pop(self._pending_drive_transport_sequence, None)
            self._pending_drive_transport_sequence = None
        if queued and client_seq is not None and not self.settings.dry_run:
            self._pending_acks[transport_seq] = PendingAck(
                client_seq=client_seq,
                message_type=message_type,
                queued_at=time.monotonic(),
            )
            if message_type == MessageType.DRIVE:
                self._pending_drive_transport_sequence = transport_seq
            if len(self._pending_acks) > 256:
                oldest = min(
                    self._pending_acks,
                    key=lambda key: self._pending_acks[key].queued_at,
                )
                self._pending_acks.pop(oldest, None)
        return transport_seq if queued else None

    def _send_drive(
        self,
        linear: float,
        angular: float,
        client_seq: int | None,
        cause: str,
        *,
        drop_controls: bool = False,
    ) -> bool:
        is_stop = linear == 0.0 and angular == 0.0
        return self._queue_frame(
            MessageType.DRIVE,
            encode_drive_payload(linear, angular),
            client_seq,
            delivery="safety" if is_stop else "drive",
            safety_key="zero_speed",
            drop_controls=drop_controls,
        ) is not None

    def _cancel_pending_clear(self, reason: str) -> PendingAck | None:
        transport_seq = self._pending_clear_transport_sequence
        had_pending = transport_seq is not None or self.safety.clear_estop_pending
        self._pending_clear_transport_sequence = None
        pending = (
            self._pending_acks.pop(transport_seq, None)
            if transport_seq is not None
            else None
        )
        if self.safety.clear_estop_pending:
            self.safety.reject_clear_estop(reason)
        if had_pending:
            # Remove an unsent CLEAR_ESTOP before a late frame can unlock the
            # MCU, then place zero speed ahead of normal traffic.  Periodic
            # watchdog zeroes do not use this drop-controls path.
            self._send_drive(
                0.0,
                0.0,
                None,
                reason,
                drop_controls=True,
            )
        return pending

    def _on_serial_frame_thread(self, frame: Frame) -> None:
        loop = self._event_loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._handle_serial_frame(frame))
            )

    def _on_serial_state_thread(self, connected: bool, error: str | None) -> None:
        loop = self._event_loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._handle_serial_state(connected, error))
            )

    async def _handle_serial_state(self, connected: bool, error: str | None) -> None:
        if self.settings.dry_run:
            return
        cancelled_clear = self._cancel_pending_clear(
            "serial_reconnected" if connected else "serial_disconnected"
        )
        forced_stop = self.safety.set_serial_ready(connected)
        self._pending_acks.clear()
        self._pending_drive_transport_sequence = None
        if connected:
            self._send_drive(0.0, 0.0, None, forced_stop.cause)
        if cancelled_clear is not None:
            await self.send_status(
                "ack",
                ack={
                    "seq": cancelled_clear.client_seq,
                    "accepted": False,
                    "applied": False,
                    "stage": "gateway",
                    "reason": "serial_reconnected" if connected else "serial_disconnected",
                },
            )
        await self.send_status(
            "serial_state",
            serial={"connected": connected, "error": error},
        )

    async def _handle_serial_frame(self, frame: Frame) -> None:
        if frame.message_type == int(MessageType.STATUS):
            try:
                telemetry = decode_status_payload(frame.payload)
            except ValueError as exc:
                await self.send_status("protocol_error", error={"detail": str(exc)})
                return
            if bool(telemetry["estop"]):
                was_latched = self.safety.estop_latched
                self.safety.observe_hardware_estop(True)
                if not was_latched:
                    self._send_drive(0.0, 0.0, None, "hardware_estop_latched")
            else:
                self.safety.observe_hardware_estop(False)
            fault_code = int(telemetry["fault_code"])
            if fault_code:
                was_same_fault = (
                    self.safety.hardware_fault_latched
                    and self.safety.hardware_fault_code == fault_code
                )
                self.safety.observe_hardware_fault(fault_code)
                if not was_same_fault:
                    self._send_drive(0.0, 0.0, None, "hardware_fault_latched")
            else:
                self.safety.observe_hardware_fault(0)
            await self.send_status("telemetry", telemetry=telemetry)
            return

        if frame.message_type == int(MessageType.ACK):
            try:
                ack = decode_ack_payload(frame.payload)
            except ValueError as exc:
                await self.send_status("protocol_error", error={"detail": str(exc)})
                return
            pending = self._pending_acks.pop(frame.sequence, None)
            if self._pending_drive_transport_sequence == frame.sequence:
                self._pending_drive_transport_sequence = None
            if pending is None:
                await self.send_status(
                    "ack",
                    ack={
                        "seq": None,
                        "transport_seq": frame.sequence,
                        "stage": "stm32",
                        "accepted": False,
                        "applied": False,
                        "reason": "unmatched_transport_seq",
                        **{
                            key: value
                            for key, value in ack.items()
                            if key != "accepted"
                        },
                    },
                )
                return

            type_matches = ack["acked_type"] == int(pending.message_type)
            if pending.message_type == MessageType.CLEAR_ESTOP:
                is_current = (
                    self._pending_clear_transport_sequence == frame.sequence
                    and self.safety.clear_estop_pending
                )
                self._pending_clear_transport_sequence = None
                if not is_current:
                    accepted = False
                    applied = False
                    reason = "unmatched_clear_request"
                elif not type_matches:
                    self.safety.reject_clear_estop("ack_type_mismatch")
                    accepted = False
                    applied = False
                    reason = "ack_type_mismatch"
                elif bool(ack["accepted"]):
                    self.safety.confirm_clear_estop("estop_cleared")
                    self._send_drive(
                        0.0,
                        0.0,
                        None,
                        "estop_cleared",
                        drop_controls=True,
                    )
                    accepted = True
                    applied = True
                    reason = "estop_cleared"
                else:
                    reason = f"stm32_rejected_{ack['result']}"
                    self.safety.reject_clear_estop(reason)
                    self._send_drive(
                        0.0,
                        0.0,
                        None,
                        reason,
                        drop_controls=True,
                    )
                    accepted = False
                    applied = False
                await self.send_status(
                    "ack",
                    ack={
                        "seq": pending.client_seq,
                        "transport_seq": frame.sequence,
                        "stage": "stm32",
                        **ack,
                        "accepted": accepted,
                        "applied": applied,
                        "reason": reason,
                    },
                )
                return

            accepted = bool(ack["accepted"]) and type_matches
            await self.send_status(
                "ack",
                ack={
                    "seq": pending.client_seq,
                    "transport_seq": frame.sequence,
                    "stage": "stm32",
                    **ack,
                    "accepted": accepted,
                    "applied": accepted,
                    "reason": ack["result"] if type_matches else "ack_type_mismatch",
                },
            )
            return

        await self.send_status(
            "protocol_error",
            error={"detail": f"unexpected UART message type {frame.message_type}"},
        )

    async def _watchdog_loop(self) -> None:
        interval = min(0.04, self.settings.watchdog_seconds / 4)
        while True:
            await asyncio.sleep(interval)
            clear_timeout = self.safety.poll_clear_estop_timeout()
            if clear_timeout is not None:
                transport_seq = self._pending_clear_transport_sequence
                self._pending_clear_transport_sequence = None
                pending = (
                    self._pending_acks.pop(transport_seq, None)
                    if transport_seq is not None
                    else None
                )
                self._send_drive(
                    0.0,
                    0.0,
                    None,
                    clear_timeout.reason,
                    drop_controls=True,
                )
                await self.send_status(
                    "ack",
                    ack={
                        "seq": pending.client_seq if pending else None,
                        "transport_seq": transport_seq,
                        "accepted": False,
                        "applied": False,
                        "stage": "gateway",
                        "reason": "stm32_ack_timeout",
                    },
                )
            action = self.safety.poll_watchdog()
            if action is None:
                continue
            queued = self._send_drive(0.0, 0.0, None, action.output.cause)
            if not queued and not self.settings.dry_run:
                self.safety.set_serial_ready(False)
            if action.newly_tripped:
                await self.send_status(
                    "watchdog",
                    watchdog={
                        "tripped": True,
                        "timeout_ms": round(self.settings.watchdog_seconds * 1000),
                    },
                )

    async def _dry_run_telemetry_loop(self) -> None:
        """Keep the UI testable without ever implying that hardware responded."""

        while True:
            await asyncio.sleep(0.25)
            await self.send_status(
                "telemetry",
                telemetry={
                    "simulated": True,
                    "uartConnected": False,
                    "serialReady": True,
                    "estop": self.safety.estop_latched,
                    "batteryVoltage": 12.4,
                    "distanceCm": 120.0,
                },
            )

    def origin_allowed(self, websocket: WebSocket) -> bool:
        if "*" in self.settings.allowed_origins:
            return True
        origin = websocket.headers.get("origin")
        return origin is not None and origin in self.settings.allowed_origins

    async def acquire_client(self, websocket: WebSocket) -> str | None:
        async with self._client_lock:
            if self._active_client is not None:
                return None
            client_id = uuid.uuid4().hex
            self._active_client = (client_id, websocket)
            self.safety.begin_client()
            return client_id

    async def release_client(self, client_id: str) -> None:
        async with self._client_lock:
            if self._active_client is None or self._active_client[0] != client_id:
                return
            self._active_client = None
            self._cancel_pending_clear("client_disconnected")
            stop = self.safety.end_client()
            self._send_drive(
                0.0,
                0.0,
                None,
                stop.cause,
                drop_controls=True,
            )

    async def send_status(self, event: str, **fields: Any) -> None:
        active = self._active_client
        if active is None:
            return
        _, websocket = active
        payload: dict[str, Any] = {
            "type": "robot_status",
            "event": event,
            "timestamp_ms": int(time.time() * 1000),
            "gateway": self.gateway_snapshot(),
        }
        payload.update(fields)
        try:
            await websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            pass

    def gateway_snapshot(self) -> dict[str, object]:
        return {
            "mode": "dry_run" if self.settings.dry_run else "uart",
            "simulated": self.settings.dry_run,
            "uart_connected": False if self.settings.dry_run else self.link.connected,
            "serial_port": None if self.settings.dry_run else self.settings.serial_port,
            "serial_error": self.link.last_error,
            "watchdog_timeout_ms": round(self.settings.watchdog_seconds * 1000),
            "control_client": self._active_client is not None,
            **self.safety.snapshot(),
        }

    async def handle_message(self, raw: str) -> None:
        try:
            message = parse_client_message(raw)
        except MessageValidationError as exc:
            await self.send_status(
                "ack",
                ack={
                    "seq": None,
                    "accepted": False,
                    "applied": False,
                    "stage": "gateway",
                    "reason": exc.code,
                    "detail": exc.detail,
                },
            )
            return

        if isinstance(message, DriveMessage):
            decision = self.safety.accept_drive(message)
            queued = False
            if decision.output is not None and (decision.accepted or decision.reason == "estop_latched"):
                queued = self._send_drive(
                    decision.output.linear,
                    decision.output.angular,
                    message.seq if decision.accepted else None,
                    decision.output.cause,
                )
            if decision.accepted and not queued and not self.settings.dry_run:
                # Do not retain a non-zero desired command if it could not enter
                # the UART queue.  A later UART reconnect starts from zero.
                self.safety.set_serial_ready(False)
            if decision.accepted and not queued:
                decision_reason = "dry_run_only" if self.settings.dry_run else "serial_queue_failed"
            else:
                decision_reason = decision.reason
            await self.send_status(
                "ack",
                ack={
                    "seq": message.seq,
                    "accepted": decision.accepted,
                    "applied": False,
                    "stage": "dry_run" if self.settings.dry_run else "gateway",
                    "queued_to_uart": queued and not self.settings.dry_run,
                    "simulated": self.settings.dry_run,
                    "reason": decision_reason,
                },
            )
            return

        if isinstance(message, EstopMessage):
            cancelled_clear = self._cancel_pending_clear("superseded_by_estop")
            if cancelled_clear is not None:
                await self.send_status(
                    "ack",
                    ack={
                        "seq": cancelled_clear.client_seq,
                        "accepted": False,
                        "applied": False,
                        "stage": "gateway",
                        "reason": "superseded_by_estop",
                    },
                )
            decision = self.safety.latch_estop(message)
            transport_seq = self._queue_frame(
                MessageType.ESTOP,
                b"\x01",
                message.seq,
                delivery="safety",
                safety_key="estop",
                drop_controls=True,
            )
            self._send_drive(0.0, 0.0, None, "estop")
            await self.send_status(
                "ack",
                ack={
                    "seq": message.seq,
                    "accepted": decision.accepted,
                    "applied": False,
                    "stage": "dry_run" if self.settings.dry_run else "gateway",
                    "transport_seq": transport_seq,
                    "queued_to_uart": transport_seq is not None and not self.settings.dry_run,
                    "simulated": self.settings.dry_run,
                    "reason": decision.reason,
                },
            )
            return

        if isinstance(message, ClearEstopMessage):
            decision = self.safety.request_clear_estop(message)
            accepted = decision.accepted
            applied = False
            reason = decision.reason
            transport_seq: int | None = None

            if decision.accepted and decision.reason == "awaiting_stm32_ack":
                if self.settings.dry_run:
                    completed = self.safety.confirm_clear_estop("estop_cleared")
                    accepted = completed.accepted
                    reason = completed.reason
                    self._send_drive(0.0, 0.0, None, "estop_cleared")
                else:
                    transport_seq = self._queue_frame(
                        MessageType.CLEAR_ESTOP,
                        b"\xA5",
                        message.seq,
                        delivery="control",
                    )
                    if transport_seq is None:
                        failed = self.safety.reject_clear_estop("serial_queue_failed")
                        accepted = False
                        reason = failed.reason
                    else:
                        self._pending_clear_transport_sequence = transport_seq
                        reason = "awaiting_stm32_ack"

            await self.send_status(
                "ack",
                ack={
                    "seq": message.seq,
                    "transport_seq": transport_seq,
                    "accepted": accepted,
                    "applied": applied,
                    "stage": "dry_run" if self.settings.dry_run else "gateway",
                    "queued_to_uart": transport_seq is not None,
                    "simulated": self.settings.dry_run,
                    "reason": reason,
                },
            )


settings = Settings.from_environment()
runtime = GatewayRuntime(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="Rover One V1 Gateway", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    healthy = settings.dry_run or runtime.link.connected
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "ready_for_hardware": runtime.link.connected and not settings.dry_run,
            "gateway": runtime.gateway_snapshot(),
        },
    )


@app.websocket("/ws")
async def websocket_gateway(websocket: WebSocket) -> None:
    await websocket.accept()
    if not runtime.origin_allowed(websocket):
        await websocket.send_json(
            {
                "type": "robot_status",
                "event": "rejected",
                "ack": {"accepted": False, "reason": "origin_not_allowed"},
            }
        )
        await websocket.close(code=4403, reason="origin not allowed")
        return

    client_id = await runtime.acquire_client(websocket)
    if client_id is None:
        await websocket.send_json(
            {
                "type": "robot_status",
                "event": "rejected",
                "ack": {"accepted": False, "reason": "controller_busy"},
            }
        )
        await websocket.close(code=4409, reason="another controller is active")
        return

    await runtime.send_status(
        "connected",
        control={
            "granted": True,
            "note": "dry-run does not drive hardware"
            if settings.dry_run
            else "UART commands require matching STM32 firmware",
        },
    )
    try:
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                break
            raw = packet.get("text")
            if raw is None:
                await runtime.send_status(
                    "ack",
                    ack={
                        "seq": None,
                        "accepted": False,
                        "applied": False,
                        "stage": "gateway",
                        "reason": "text_json_required",
                    },
                )
                continue
            await runtime.handle_message(raw)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # Starlette raises RuntimeError for already-disconnected sockets.
        pass
    finally:
        await runtime.release_client(client_id)


if settings.web_root is not None:
    app.mount("/", StaticFiles(directory=str(settings.web_root), html=True), name="web")
else:

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "service": "Rover One V1 Gateway",
            "websocket": "/ws",
            "health": "/health",
            "mode": "dry_run" if settings.dry_run else "uart",
        }
