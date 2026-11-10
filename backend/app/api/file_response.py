"""Download an untrusted workspace path through one safely opened file handle."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import HTTPException
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from app.sandbox.session_paths import SessionPaths


class WorkspaceFileResponse(FileResponse):
    def __init__(self, paths: SessionPaths, path: Path, *, filename: str) -> None:
        super().__init__(
            path,
            filename=filename,
            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
        )
        self.paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Open only when sending, so an abandoned response owns no descriptor.
        # No await between acquiring the descriptor and establishing its cleanup.
        try:
            file = self.paths.open_host_file(Path(self.path))
        except ValueError as exc:
            raise HTTPException(400, "Invalid file path") from exc
        except OSError as exc:
            raise HTTPException(404, "File not found") from exc
        with file:
            self.stat_result = os.fstat(file.fileno())
            self.set_stat_headers(self.stat_result)
            self._file = anyio.wrap_file(file)
            # pathsend would ask the server to reopen an untrusted pathname.
            extensions = dict(scope.get("extensions", {}))
            extensions.pop("http.response.pathsend", None)
            await super().__call__({**scope, "extensions": extensions}, receive, send)

    @asynccontextmanager
    async def _open_file(self):
        # Starlette's simple/single/multipart range responses share this hook.
        # Keep range and cancellation tests when upgrading the locked dependency.
        yield self._file
