from protocol import Frame, FrameDecoder, MessageType, crc16_ccitt, encode_frame


def test_crc16_ccitt_false_known_vector() -> None:
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_frame_round_trip_across_partial_reads() -> None:
    expected = Frame(MessageType.DRIVE, 42, b"\x01\x02\x03", flags=7)
    encoded = encode_frame(expected)
    decoder = FrameDecoder()

    frames = decoder.feed_many([encoded[:1], encoded[1:5], encoded[5:-2], encoded[-2:]])

    assert frames == [expected]
    assert decoder.crc_errors == 0


def test_decoder_skips_noise_and_recovers_after_bad_crc() -> None:
    bad = bytearray(encode_frame(Frame(MessageType.DRIVE, 1, b"bad")))
    bad[-1] ^= 0xFF
    good = Frame(MessageType.STATUS, 2, b"status")
    decoder = FrameDecoder()

    frames = decoder.feed(b"line-noise" + bad + encode_frame(good))

    assert frames == [good]
    assert decoder.crc_errors == 1


def test_encoder_rejects_oversized_payload() -> None:
    try:
        encode_frame(Frame(MessageType.STATUS, 1, b"x" * 257))
    except ValueError as exc:
        assert "payload" in str(exc)
    else:
        raise AssertionError("oversized payload was accepted")
