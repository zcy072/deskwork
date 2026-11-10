"""File downloads must keep one safe descriptor through headers, ranges and cancellation."""

import asyncio

import pytest
from fastapi import HTTPException

from app.api.routes import stream
from app.sandbox.session_paths import SessionPaths


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    paths = SessionPaths.create("download", tmp_path / "sessions")
    (paths.output / "nested").mkdir(parents=True)
    target = paths.output / "nested" / "result.txt"
    target.write_bytes(b"original content")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_bytes(b"OUTSIDE_SESSION_SENTINEL")
    opened = []
    original_open = SessionPaths.open_host_file

    def track_open(self, path):
        file = original_open(self, path)
        opened.append(file)
        return file

    async def visible(*args):
        pass

    monkeypatch.setattr(stream, "_require_visible", visible)
    monkeypatch.setattr(SessionPaths, "create", staticmethod(lambda sid: paths))
    monkeypatch.setattr(SessionPaths, "open_host_file", track_open)
    return paths, target, outside, opened


async def response_for(paths):
    return await stream.download_file(None, paths.session_id, "output/nested/result.txt", None)


def scope(headers=(), method="GET"):
    return {
        "type": "http",
        "method": method,
        "headers": list(headers),
        "asgi": {"spec_version": "2.4"},
        "extensions": {"http.response.pathsend": {}},
    }


@pytest.mark.parametrize("replace_parent", [False, True])
async def test_replacement_before_open_cannot_escape(workspace, replace_parent):
    paths, target, outside, opened = workspace
    response = await response_for(paths)
    assert not opened  # No leaked descriptor if the response is never sent.
    if replace_parent:
        target.parent.rename(paths.output / "previous")
        target.parent.symlink_to(outside, target_is_directory=True)
    else:
        target.unlink()
        target.symlink_to(outside / "result.txt")
    sent = []

    async def send(message):
        sent.append(message)

    with pytest.raises(HTTPException) as exc:
        await response(scope(), None, send)
    assert exc.value.status_code in (400, 404)
    assert not sent
    assert not opened


@pytest.mark.parametrize("replace_parent", [False, True])
@pytest.mark.parametrize(
    "headers,expected_status,expected_body",
    [
        ([], 200, b"original content"),
        ([(b"range", b"bytes=0-3")], 206, b"orig"),
        ([(b"range", b"bytes=-7")], 206, b"content"),
        ([(b"range", b"bytes=0-1,9-11")], 206, None),
        ([(b"range", b"bytes=100-200")], 416, b""),
    ],
)
async def test_replacement_after_open_keeps_original_bytes(
    workspace, replace_parent, headers, expected_status, expected_body
):
    paths, target, outside, opened = workspace
    response = await response_for(paths)
    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start":
            if replace_parent:
                target.parent.rename(paths.output / "previous")
                target.parent.symlink_to(outside, target_is_directory=True)
            else:
                target.unlink()
                target.symlink_to(outside / "result.txt")

    await response(scope(headers), None, send)
    assert sent[0]["status"] == expected_status
    assert all(m["type"] != "http.response.pathsend" for m in sent)
    body = b"".join(m.get("body", b"") for m in sent)
    assert b"OUTSIDE_SESSION_SENTINEL" not in body
    if expected_body is None:
        assert b"\r\nor\r\n" in body and b"\r\ncon\r\n" in body
    else:
        assert body == expected_body
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.parametrize("ending", ["head", "disconnect", "send_error", "cancel"])
async def test_download_closes_descriptor_on_every_exit(workspace, ending):
    paths, _, _, opened = workspace
    response = await response_for(paths)
    body_entered = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            body_entered.set()
            if ending == "send_error":
                raise OSError("client disconnected")
            if ending in ("disconnect", "cancel"):
                await asyncio.Event().wait()
            if ending == "head":
                assert message["body"] == b""

    async def receive():
        await body_entered.wait()
        return {"type": "http.disconnect"}

    request_scope = scope(method="HEAD" if ending == "head" else "GET")
    if ending == "disconnect":
        request_scope["asgi"]["spec_version"] = "2.0"
    task = asyncio.create_task(response(request_scope, receive, send))
    if ending == "cancel":
        await body_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif ending == "send_error":
        with pytest.raises(OSError):
            await task
    else:
        await asyncio.wait_for(task, 2)
    assert len(opened) == 1 and opened[0].closed
