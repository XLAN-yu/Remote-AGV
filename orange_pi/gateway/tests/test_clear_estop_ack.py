import asyncio
import struct

from app import GatewayRuntime, Settings
from protocol import AckResult, Frame, MessageType
from safety import EstopMessage


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.messages.append(payload)


def make_runtime(send_result: bool = True) -> tuple[GatewayRuntime, FakeWebSocket]:
    settings = Settings(
        serial_port="test-uart",
        baud=115200,
        dry_run=False,
        watchdog_seconds=0.2,
        allowed_origins=("*",),
        web_root=None,
    )
    runtime = GatewayRuntime(settings)
    runtime.link.send = lambda *args, **kwargs: send_result  # type: ignore[method-assign]
    socket = FakeWebSocket()
    runtime._active_client = ("test-client", socket)  # noqa: SLF001
    runtime.safety.set_serial_ready(True)
    runtime.safety.begin_client()
    runtime.safety.latch_estop(EstopMessage(100))
    return runtime, socket


def test_real_clear_waits_for_matching_ok_ack_and_keeps_client_seq() -> None:
    async def scenario() -> None:
        runtime, socket = make_runtime()
        await runtime.handle_message(
            '{"type":"clear_estop","seq":101,"confirm":true}'
        )

        transport_seq = runtime._pending_clear_transport_sequence  # noqa: SLF001
        assert transport_seq is not None
        assert runtime.safety.estop_latched
        assert runtime.safety.clear_estop_pending
        queued_ack = socket.messages[-1]["ack"]
        assert queued_ack["seq"] == 101
        assert queued_ack["stage"] == "gateway"
        assert queued_ack["reason"] == "awaiting_stm32_ack"
        assert queued_ack["applied"] is False

        payload = struct.pack("<BB", MessageType.CLEAR_ESTOP, AckResult.OK)
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.ACK, transport_seq, payload)
        )

        final_ack = socket.messages[-1]["ack"]
        assert final_ack["seq"] == 101
        assert final_ack["stage"] == "stm32"
        assert final_ack["reason"] == "estop_cleared"
        assert final_ack["accepted"] is True
        assert final_ack["applied"] is True
        assert not runtime.safety.estop_latched

    asyncio.run(scenario())


def test_rejected_or_mismatched_clear_ack_keeps_latch() -> None:
    async def scenario(result: AckResult, acked_type: MessageType) -> str:
        runtime, socket = make_runtime()
        await runtime.handle_message(
            '{"type":"clear_estop","seq":101,"confirm":true}'
        )
        transport_seq = runtime._pending_clear_transport_sequence  # noqa: SLF001
        assert transport_seq is not None
        payload = struct.pack("<BB", acked_type, result)
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.ACK, transport_seq, payload)
        )
        final_ack = socket.messages[-1]["ack"]
        assert final_ack["accepted"] is False
        assert final_ack["applied"] is False
        assert runtime.safety.estop_latched
        return str(final_ack["reason"])

    assert asyncio.run(scenario(AckResult.HARDWARE_FAULT, MessageType.CLEAR_ESTOP)) == (
        "stm32_rejected_hardware_fault"
    )
    assert asyncio.run(scenario(AckResult.OK, MessageType.DRIVE)) == "ack_type_mismatch"


def test_clear_queue_failure_is_rejected_and_remains_latched() -> None:
    async def scenario() -> None:
        runtime, socket = make_runtime(send_result=False)
        await runtime.handle_message(
            '{"type":"clear_estop","seq":101,"confirm":true}'
        )

        ack = socket.messages[-1]["ack"]
        assert ack["seq"] == 101
        assert ack["stage"] == "gateway"
        assert ack["reason"] == "serial_queue_failed"
        assert ack["accepted"] is False
        assert ack["applied"] is False
        assert runtime.safety.estop_latched
        assert not runtime.safety.clear_estop_pending

    asyncio.run(scenario())


def test_status_after_gateway_restart_only_synchronizes_estop_toward_latched() -> None:
    async def scenario() -> None:
        settings = Settings(
            serial_port="test-uart",
            baud=115200,
            dry_run=False,
            watchdog_seconds=0.2,
            allowed_origins=("*",),
            web_root=None,
        )
        runtime = GatewayRuntime(settings)
        runtime.link.send = lambda *args, **kwargs: True  # type: ignore[method-assign]
        socket = FakeWebSocket()
        runtime._active_client = ("test-client", socket)  # noqa: SLF001
        runtime.safety.set_serial_ready(True)
        assert not runtime.safety.estop_latched

        asserted_payload = struct.pack(
            "<HHiiiifffBB",
            12400,
            1000,
            0,
            0,
            0,
            0,
            0.0,
            0.0,
            0.0,
            1,
            0,
        )
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.STATUS, 1, asserted_payload)
        )
        assert runtime.safety.estop_latched

        clear_payload = bytearray(asserted_payload)
        clear_payload[-2] = 0
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.STATUS, 2, bytes(clear_payload))
        )
        assert runtime.safety.estop_latched

        await runtime.handle_message(
            '{"type":"clear_estop","seq":1,"confirm":true}'
        )
        assert runtime._pending_clear_transport_sequence is not None  # noqa: SLF001
        assert runtime.safety.estop_latched

    asyncio.run(scenario())


def test_dry_run_clear_is_explicitly_simulated_without_hardware_applied() -> None:
    async def scenario() -> None:
        settings = Settings(
            serial_port="dry-run",
            baud=115200,
            dry_run=True,
            watchdog_seconds=0.2,
            allowed_origins=("*",),
            web_root=None,
        )
        runtime = GatewayRuntime(settings)
        socket = FakeWebSocket()
        runtime._active_client = ("test-client", socket)  # noqa: SLF001
        runtime.safety.set_serial_ready(True)
        runtime.safety.begin_client()
        runtime.safety.latch_estop(EstopMessage(200))

        await runtime.handle_message(
            '{"type":"clear_estop","seq":201,"confirm":true}'
        )

        ack = socket.messages[-1]["ack"]
        assert ack["seq"] == 201
        assert ack["stage"] == "dry_run"
        assert ack["reason"] == "estop_cleared"
        assert ack["accepted"] is True
        assert ack["applied"] is False
        assert ack["simulated"] is True
        assert not runtime.safety.estop_latched

    asyncio.run(scenario())


def test_periodic_estop_true_status_does_not_cancel_matching_clear_ack() -> None:
    async def scenario() -> None:
        runtime, socket = make_runtime()
        await runtime.handle_message(
            '{"type":"clear_estop","seq":101,"confirm":true}'
        )
        transport_seq = runtime._pending_clear_transport_sequence  # noqa: SLF001
        assert transport_seq is not None

        asserted_payload = struct.pack(
            "<HHiiiifffBB",
            12400,
            1000,
            0,
            0,
            0,
            0,
            0.0,
            0.0,
            0.0,
            1,
            0,
        )
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.STATUS, 1, asserted_payload)
        )
        assert runtime.safety.estop_latched
        assert runtime.safety.clear_estop_pending
        assert runtime._pending_clear_transport_sequence == transport_seq  # noqa: SLF001

        ok_payload = struct.pack("<BB", MessageType.CLEAR_ESTOP, AckResult.OK)
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.ACK, transport_seq, ok_payload)
        )
        final_ack = socket.messages[-1]["ack"]
        assert final_ack["seq"] == 101
        assert final_ack["reason"] == "estop_cleared"
        assert final_ack["applied"] is True
        assert not runtime.safety.estop_latched

    asyncio.run(scenario())


def test_hardware_fault_status_stops_drive_and_only_clear_ok_unlocks() -> None:
    async def scenario() -> None:
        settings = Settings(
            serial_port="test-uart",
            baud=115200,
            dry_run=False,
            watchdog_seconds=0.2,
            allowed_origins=("*",),
            web_root=None,
        )
        runtime = GatewayRuntime(settings)
        runtime.link.send = lambda *args, **kwargs: True  # type: ignore[method-assign]
        socket = FakeWebSocket()
        runtime._active_client = ("test-client", socket)  # noqa: SLF001
        runtime.safety.set_serial_ready(True)
        runtime.safety.begin_client()

        fault_payload = struct.pack(
            "<HHiiiifffBB",
            12400,
            1000,
            0,
            0,
            0,
            0,
            0.0,
            0.0,
            0.0,
            0,
            7,
        )
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.STATUS, 1, fault_payload)
        )
        assert runtime.safety.hardware_fault_latched
        assert runtime.safety.hardware_fault_code == 7

        await runtime.handle_message(
            '{"type":"drive","linear":0.3,"angular":0,"seq":1}'
        )
        assert socket.messages[-1]["ack"]["reason"] == "hardware_fault_latched"

        zero_fault_payload = bytearray(fault_payload)
        zero_fault_payload[-1] = 0
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.STATUS, 2, bytes(zero_fault_payload))
        )
        assert runtime.safety.hardware_fault_latched

        await runtime.handle_message(
            '{"type":"clear_estop","seq":2,"confirm":true}'
        )
        transport_seq = runtime._pending_clear_transport_sequence  # noqa: SLF001
        assert transport_seq is not None

        # A repeated non-zero fault level is analogous to estop=true: it keeps
        # the lock but does not cancel the in-flight reset transaction.
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.STATUS, 3, fault_payload)
        )
        assert runtime.safety.clear_estop_pending

        ok_payload = struct.pack("<BB", MessageType.CLEAR_ESTOP, AckResult.OK)
        await runtime._handle_serial_frame(  # noqa: SLF001
            Frame(MessageType.ACK, transport_seq, ok_payload)
        )
        assert not runtime.safety.hardware_fault_latched
        assert runtime.safety.hardware_fault_code == 0
        final_ack = socket.messages[-1]["ack"]
        assert final_ack["reason"] == "estop_cleared"
        assert final_ack["applied"] is True

    asyncio.run(scenario())
