"""Single-process session ownership. Redis never grants permission to mutate files."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


class ActiveRunError(RuntimeError):
    """The session already belongs to another operation."""


class TooManyRunsError(RuntimeError):
    """The application's concurrent run limit has been reached."""


@dataclass
class RunHandle:
    session_id: str
    prompt_id: str | None = None
    operation: str = "run"
    phase: str = "preparing"
    task: asyncio.Task | None = None
    started: bool = False
    cancel_requested: bool = False
    stream_started: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)


class SessionRegistry:
    def __init__(self) -> None:
        self._slots: dict[str, RunHandle] = {}
        self.closing = False

    def get(self, session_id: str) -> RunHandle | None:
        return self._slots.get(session_id)

    def reserve(self, session_id: str, operation: str = "run", limit: int = 0) -> RunHandle:
        # No await between checking and reserving: atomic in one asyncio loop.
        if self.closing or session_id in self._slots:
            raise ActiveRunError(f"Session '{session_id}' is busy")
        if operation == "run" and limit:
            count = sum(h.operation == "run" for h in self._slots.values())
            if count >= limit:
                raise TooManyRunsError(f"同时运行的会话已达上限({limit}),请稍后再试")
        handle = RunHandle(session_id, operation=operation)
        self._slots[session_id] = handle
        return handle

    def release(self, handle: RunHandle) -> None:
        if self._slots.get(handle.session_id) is handle:
            self._slots.pop(handle.session_id)
        handle.ready.set()
        handle.released.set()

    @contextmanager
    def operation(self, session_id: str, name: str) -> Iterator[RunHandle]:
        handle = self.reserve(session_id, name)
        try:
            yield handle
        finally:
            self.release(handle)

    def begin_delete(self, session_id: str) -> RunHandle:
        handle = self.get(session_id)
        if handle is None:
            return self.reserve(session_id, "delete")
        if handle.operation != "run" or handle.phase in ("preparing", "recovery_required"):
            raise ActiveRunError(f"Session '{session_id}' is busy")
        handle.operation = "delete"
        return handle

    def active(self) -> list[RunHandle]:
        return list(self._slots.values())


sessions = SessionRegistry()


async def complete_before_cancelling(task: asyncio.Task):
    """Drain owned IO/cleanup even if its waiter receives repeated cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def file_io(fn, *args, **kwargs):
    """A cancelled waiter must not release its file lock before the thread stops."""
    return await complete_before_cancelling(
        asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    )
