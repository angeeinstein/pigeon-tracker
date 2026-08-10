from __future__ import annotations

from app.services.settings_schema import ControllerSettings, MotionSettings
from app.turret.manager import TurretManager
from app.turret.protocol import Hello


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


async def test_late_detach_cannot_clear_replacement_connection() -> None:
    manager = TurretManager(ControllerSettings(), MotionSettings())
    hello = Hello(protocol_version=1)
    first = FakeConnection()
    second = FakeConnection()

    assert await manager.attach(first, hello)
    assert await manager.attach(second, hello)
    assert first.closed

    # The first WebSocket route reaches its finally block after the second
    # socket has already attached. That stale cleanup must be ignored.
    assert await manager.detach(first) is False
    assert manager.connected

    assert await manager.detach(second) is True
    assert not manager.connected
