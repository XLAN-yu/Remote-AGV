from serial_link import OutboundBuffer


def test_nonzero_drive_backlog_is_coalesced_to_latest_value() -> None:
    writes = OutboundBuffer()
    for sequence in range(10_000):
        writes.put_latest_drive(f"drive-{sequence}".encode())

    assert writes.pending_counts() == {"safety": 0, "control": 0, "drive": 1}
    assert writes.pop_next() == b"drive-9999"
    assert writes.pop_next() is None


def test_stop_atomically_discards_queued_drive_and_is_popped_first() -> None:
    writes = OutboundBuffer()
    for sequence in range(10_000):
        writes.put_latest_drive(f"drive-{sequence}".encode())

    writes.put_safety(b"zero", key="zero_speed")

    assert writes.pending_counts() == {"safety": 1, "control": 0, "drive": 0}
    assert writes.pop_next() == b"zero"
    assert writes.pop_next() is None


def test_estop_drops_pending_clear_and_precedes_zero() -> None:
    writes = OutboundBuffer()
    writes.put_control(b"clear-estop")
    writes.put_latest_drive(b"stale-nonzero-drive")

    writes.put_safety(b"estop", key="estop", drop_controls=True)
    writes.put_safety(b"zero", key="zero_speed")

    assert writes.pending_counts() == {"safety": 2, "control": 0, "drive": 0}
    assert writes.pop_next() == b"estop"
    assert writes.pop_next() == b"zero"
    assert writes.pop_next() is None


def test_cancelled_clear_is_dropped_before_priority_zero() -> None:
    writes = OutboundBuffer()
    writes.put_control(b"clear-estop")

    writes.put_safety(b"disconnect-zero", key="zero_speed", drop_controls=True)

    assert writes.pending_counts() == {"safety": 1, "control": 0, "drive": 0}
    assert writes.pop_next() == b"disconnect-zero"
    assert writes.pop_next() is None


def test_periodic_watchdog_zero_preserves_legitimate_pending_clear() -> None:
    writes = OutboundBuffer()
    writes.put_control(b"clear-estop")
    writes.put_latest_drive(b"stale-nonzero-drive")

    writes.put_safety(b"watchdog-zero", key="zero_speed", drop_controls=False)

    assert writes.pending_counts() == {"safety": 1, "control": 1, "drive": 0}
    assert writes.pop_next() == b"watchdog-zero"
    assert writes.pop_next() == b"clear-estop"
    assert writes.pop_next() is None
