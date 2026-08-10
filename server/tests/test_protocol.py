"""Protocol serialisation and validation.

Everything a controller sends is untrusted until it has been through
`decode_controller_message`, so the negative cases matter as much as the happy
path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.turret import protocol as proto
from app.version import PROTOCOL_VERSION


class TestDecoding:
    def test_decodes_a_status_frame(self) -> None:
        message = proto.decode_controller_message(
            json.dumps(
                {
                    "v": 1,
                    "type": "status",
                    "pan_deg": 42.1,
                    "tilt_deg": -17.75,
                    "moving": True,
                    "homed": True,
                }
            )
        )
        assert isinstance(message, proto.Status)
        assert message.pan_deg == 42.1
        assert message.valve_open is False  # default

    def test_decodes_an_ack(self) -> None:
        message = proto.decode_controller_message(
            '{"v":1,"type":"ack","id":7,"ok":false,"code":"NOT_HOMED"}'
        )
        assert isinstance(message, proto.Ack)
        assert message.ok is False
        assert message.code == proto.ErrorCode.NOT_HOMED

    def test_accepts_bytes(self) -> None:
        message = proto.decode_controller_message(b'{"v":1,"type":"hello"}')
        assert isinstance(message, proto.Hello)

    def test_ignores_unknown_fields(self) -> None:
        # Forward compatibility: newer firmware may send fields we do not know.
        message = proto.decode_controller_message(
            '{"v":1,"type":"status","pan_deg":1.0,"future_field":123}'
        )
        assert isinstance(message, proto.Status)

    @pytest.mark.parametrize(
        "payload, message",
        [
            ("not json", "invalid JSON"),
            ('["a"]', "must be a JSON object"),
            ('{"v":1}', "missing 'type'"),
            ('{"v":1,"type":"nonsense"}', "unknown message type"),
            ('{"v":1,"type":"ack"}', "ack:"),
            ('{"v":1,"type":"log","level":"catastrophe","msg":"x"}', "log:"),
        ],
    )
    def test_rejects_bad_frames(self, payload: str, message: str) -> None:
        with pytest.raises(proto.ProtocolError, match=message):
            proto.decode_controller_message(payload)

    def test_rejects_oversized_frames(self) -> None:
        payload = json.dumps({"v": 1, "type": "log", "level": "info", "msg": "x" * 40000})
        with pytest.raises(proto.ProtocolError, match="too large"):
            proto.decode_controller_message(payload)

    def test_rejects_invalid_utf8(self) -> None:
        with pytest.raises(proto.ProtocolError, match="UTF-8"):
            proto.decode_controller_message(b'{"type":"status","x":"\xff\xfe"}')

    def test_server_message_types_are_not_accepted_from_a_controller(self) -> None:
        # A controller must not be able to feed the server its own commands.
        with pytest.raises(proto.ProtocolError, match="unknown message type"):
            proto.decode_controller_message('{"v":1,"type":"move_absolute","id":1}')


class TestEncoding:
    def test_encodes_a_move(self) -> None:
        payload = json.loads(proto.encode(proto.MoveAbsolute(id=12, pan_deg=10.0, tilt_deg=-5.0)))
        assert payload == {
            "v": PROTOCOL_VERSION,
            "type": "move_absolute",
            "id": 12,
            "pan_deg": 10.0,
            "tilt_deg": -5.0,
        }

    def test_none_fields_are_omitted(self) -> None:
        payload = json.loads(proto.encode(proto.HelloAck(accepted=True)))
        assert "reason" not in payload

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"id": 1, "pan_deg": 400.0, "tilt_deg": 0.0},
            {"id": 1, "pan_deg": 0.0, "tilt_deg": -900.0},
        ],
    )
    def test_out_of_range_angles_are_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            proto.MoveAbsolute(**kwargs)

    def test_spray_duration_bounds(self) -> None:
        with pytest.raises(ValueError):
            proto.Spray(id=1, duration_ms=0)
        with pytest.raises(ValueError):
            proto.Spray(id=1, duration_ms=999_999)

    def test_jog_ttl_has_a_floor(self) -> None:
        # A tiny TTL would make the controller stutter; a huge one defeats the
        # failsafe. Both ends are enforced.
        with pytest.raises(ValueError):
            proto.Jog(id=1, pan_rate_deg_s=1.0, tilt_rate_deg_s=0.0, ttl_ms=1)
        with pytest.raises(ValueError):
            proto.Jog(id=1, pan_rate_deg_s=1.0, tilt_rate_deg_s=0.0, ttl_ms=60_000)


class TestGeneratedHeader:
    def test_firmware_header_is_up_to_date(self) -> None:
        """The generated C header must match the Python definition.

        This is the mechanism that keeps two implementations of one protocol
        honest; if it fails, run:
            python server/tools/gen_protocol_header.py
        """
        script = Path(__file__).resolve().parents[1] / "tools" / "gen_protocol_header.py"
        result = subprocess.run(
            [sys.executable, str(script), "--check"], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr or result.stdout

    def test_header_contains_every_message_type(self) -> None:
        header = (
            Path(__file__).resolve().parents[2] / "firmware" / "include" / "protocol_generated.h"
        ).read_text(encoding="utf-8")
        for name in (*proto.SERVER_MESSAGE_TYPES, *proto.CONTROLLER_MESSAGE_TYPES):
            assert f'"{name}"' in header
        for code in proto.ERROR_CODES:
            assert f'"{code}"' in header
