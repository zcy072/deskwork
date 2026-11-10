"""Convert graph events once, before either realtime or persistent presentation."""

import asyncio
import copy
from collections.abc import Awaitable, Callable

from app.services.history import TurnRecorder


class RunEvents:
    def __init__(self, run_id: str, publish: Callable[[str, str, dict], Awaitable[None]]) -> None:
        self.run_id = run_id
        self.publish = publish
        self.recorder = TurnRecorder()
        self.seq = 0
        self.transport_failed = False
        self._lock = asyncio.Lock()

    async def emit(self, event: str, data: dict) -> None:
        async with self._lock:
            await self._emit(event, data)

    async def _emit(self, event: str, data: dict) -> None:
        data = dict(data)
        if event == "ai.tool_call" and self.recorder.streamed_text:
            data["text"] = None
        self.seq += 1
        self.recorder.record(event, data)
        try:
            await self.publish(
                self.run_id,
                event,
                data | {"run_id": self.run_id, "seq": self.seq, "schema_version": 1},
            )
        except Exception:
            self.transport_failed = True
            raise

    def snapshot(self) -> tuple[list, str, int]:
        return copy.deepcopy(self.recorder.parts), self.recorder.text, self.seq
