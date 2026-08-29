"""Binary UART protocol shared by the Orange Pi gateway and STM32 firmware.

The wire format is documented in README.md.  This module deliberately contains
no serial-port code, which keeps framing and CRC behaviour easy to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
import struct
from typing import Iterable


SYNC = b"\xA5\x5A"
VERSION = 1
MAX_PAYLOAD = 256

# sync, version, message type, flags, payload length, transport sequence
_HEADER = struct.Struct("<2sBBBHI")
_CRC = struct.Struct("<H")
_DRIVE = struct.Struct("<ff")
_ACK = struct.Struct("<BB")
_STATUS = struct.Struct("<HHiiiifffBB")


class MessageType(IntEnum):
    """Protocol message identifiers."""

    DRIVE = 0x01
    ESTOP = 0x02
    CLEAR_ESTOP = 0x03
    STATUS = 0x80
    ACK = 0x81


class AckResult(IntEnum):
    OK = 0
    INVALID_PAYLOAD = 1
    ESTOP_LATCHED = 2
    HARDWARE_FAULT = 3
    UNSUPPORTED = 4


ACK_RESULT_NAMES = {
    AckResult.OK: "ok",
    AckResult.INVALID_PAYLOAD: "invalid_payload",
    AckResult.ESTOP_LATCHED: "estop_latched",
    AckResult.HARDWARE_FAULT: "hardware_fault",
    AckResult.UNSUPPORTED: "unsupported",
}


@dataclass(frozen=True, slots=True)
class Frame:
    message_type: int
    sequence: int
    payload: bytes = b""
    flags: int = 0


def crc16_ccitt(data: bytes | bytearray | memoryview) -> int:
    """Return CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF)."""

    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def encode_frame(frame: Frame) -> bytes:
    payload = bytes(frame.payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD} bytes")
    if not 0 <= int(frame.message_type) <= 0xFF:
        raise ValueError("message_type must fit uint8")
    if not 0 <= frame.flags <= 0xFF:
        raise ValueError("flags must fit uint8")
    if not 0 <= frame.sequence <= 0xFFFFFFFF:
        raise ValueError("sequence must fit uint32")

    header = _HEADER.pack(
        SYNC,
        VERSION,
        int(frame.message_type),
        frame.flags,
        len(payload),
        frame.sequence,
    )
    # CRC excludes the two sync bytes so a decoder can discard line noise.
    protected = header[2:] + payload
    return header + payload + _CRC.pack(crc16_ccitt(protected))


def encode_drive_payload(linear_mps: float, angular_rps: float) -> bytes:
    if not math.isfinite(linear_mps) or not math.isfinite(angular_rps):
        raise ValueError("drive values must be finite")
    return _DRIVE.pack(linear_mps, angular_rps)


def decode_ack_payload(payload: bytes) -> dict[str, int | str | bool]:
    if len(payload) != _ACK.size:
        raise ValueError(f"ACK payload must be {_ACK.size} bytes")
    acked_type, result_value = _ACK.unpack(payload)
    try:
        result = AckResult(result_value)
        result_name = ACK_RESULT_NAMES[result]
    except ValueError:
        result_name = f"unknown_{result_value}"
    return {
        "acked_type": acked_type,
        "result_code": result_value,
        "result": result_name,
        "accepted": result_value == int(AckResult.OK),
    }


def decode_status_payload(payload: bytes) -> dict[str, object]:
    """Decode the fixed STATUS payload described in README.md."""

    if len(payload) != _STATUS.size:
        raise ValueError(f"STATUS payload must be {_STATUS.size} bytes")
    (
        battery_mv,
        ultrasonic_mm,
        encoder_fl,
        encoder_fr,
        encoder_rl,
        encoder_rr,
        linear_mps,
        angular_rps,
        yaw_rad,
        estop,
        fault_code,
    ) = _STATUS.unpack(payload)
    return {
        "battery_v": round(battery_mv / 1000.0, 3),
        "ultrasonic_m": None
        if ultrasonic_mm == 0xFFFF
        else round(ultrasonic_mm / 1000.0, 3),
        "encoders": {
            "front_left": encoder_fl,
            "front_right": encoder_fr,
            "rear_left": encoder_rl,
            "rear_right": encoder_rr,
        },
        "measured_linear": round(linear_mps, 4),
        "measured_angular": round(angular_rps, 4),
        "imu_yaw": round(yaw_rad, 4),
        "estop": bool(estop),
        "fault_code": fault_code,
    }


class FrameDecoder:
    """Incremental, noise-tolerant UART frame decoder."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.crc_errors = 0
        self.format_errors = 0

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes | bytearray | memoryview) -> list[Frame]:
        if data:
            self._buffer.extend(data)
        frames: list[Frame] = []

        while True:
            sync_at = self._buffer.find(SYNC)
            if sync_at < 0:
                # Preserve a possible first sync byte split across reads.
                keep = self._buffer[-1:] if self._buffer[-1:] == SYNC[:1] else b""
                self._buffer.clear()
                self._buffer.extend(keep)
                break
            if sync_at:
                del self._buffer[:sync_at]
            if len(self._buffer) < _HEADER.size:
                break

            _, version, message_type, flags, payload_len, sequence = _HEADER.unpack_from(
                self._buffer
            )
            if version != VERSION or payload_len > MAX_PAYLOAD:
                self.format_errors += 1
                del self._buffer[0]
                continue

            frame_len = _HEADER.size + payload_len + _CRC.size
            if len(self._buffer) < frame_len:
                break

            payload_end = _HEADER.size + payload_len
            expected_crc = _CRC.unpack_from(self._buffer, payload_end)[0]
            actual_crc = crc16_ccitt(self._buffer[2:payload_end])
            if actual_crc != expected_crc:
                self.crc_errors += 1
                del self._buffer[0]
                continue

            frames.append(
                Frame(
                    message_type=message_type,
                    sequence=sequence,
                    payload=bytes(self._buffer[_HEADER.size:payload_end]),
                    flags=flags,
                )
            )
            del self._buffer[:frame_len]

        return frames

    def feed_many(self, chunks: Iterable[bytes]) -> list[Frame]:
        frames: list[Frame] = []
        for chunk in chunks:
            frames.extend(self.feed(chunk))
        return frames
