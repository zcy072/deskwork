"""会话持久化:TurnRecorder 的累积规则、repository 读写、/conversations 路由。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from app.config import settings
from app.db import close_db, init_db, repository
from app.services.history import TurnRecorder
from tests.history_seed import seed_assistant_turn, seed_user_turn


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    await close_db()
    await init_db()
    yield
    await close_db()


# ---------- TurnRecorder(纯逻辑) ----------


def test_recorder_merges_consecutive_tokens():
    r = TurnRecorder()
    r.record("ai.token", {"text": "你"})
    r.record("ai.token", {"text": "好"})
    assert r.parts == [{"type": "text", "text": "你好"}]
    assert r.text == "你好"


def test_recorder_keeps_order_across_tool_calls():
    r = TurnRecorder()
    r.record("ai.token", {"text": "先看看"})
    r.record(
        "ai.tool_call",
        {
            "text": None,
            "tool_calls": [{"id": "c1", "name": "bash_tool", "args": {"command": "ls"}}],
        },
    )
    r.record(
        "tool.result", {"id": "c1", "name": "bash_tool", "content": "a.txt", "status": "success"}
    )
    r.record("ai.token", {"text": "有一个文件"})
    assert [p["type"] for p in r.parts] == ["text", "tool_call", "tool_result", "text"]
    assert r.text == "先看看有一个文件"


def test_recorder_ignores_empty_token_and_unknown_events():
    r = TurnRecorder()
    r.record("ai.token", {"text": ""})
    r.record("stream.end", {})
    r.record("error", {"message": "x"})
    assert r.parts == []


def test_recorder_tool_call_text_becomes_text_part():
    r = TurnRecorder()
    r.record(
        "ai.tool_call", {"text": "我来执行", "tool_calls": [{"id": "c", "name": "n", "args": {}}]}
    )
    assert r.parts[0] == {"type": "text", "text": "我来执行"}
    assert r.parts[1]["type"] == "tool_call"


def test_streamed_tool_preamble_matches_live_history():
    r = TurnRecorder()
    r.record("ai.token", {"text": "先读取"})
    r.record("ai.tool_call", {"text": "先读取", "tool_calls": [{"id": "a", "name": "read"}]})
    r.record("tool.result", {"id": "a", "content": "ok"})
    r.record("ai.tool_call", {"text": "再检查", "tool_calls": [{"id": "b", "name": "check"}]})
    assert r.text == "先读取再检查"


# ---------- repository ----------


async def test_title_from_first_query_and_truncation(db):
    await repository.ensure_conversation("s1", "这是一个很长的问题" * 10)
    rows = await repository.list_conversations()
    title = rows[0][0].title
    assert len(title) == repository.TITLE_MAX_CHARS and title.endswith("…")


async def test_roundtrip_messages_in_order(db):
    await seed_user_turn(
        "s1", "p1", "你好", [{"file_name": "a.pdf", "container_path": "/workspace/uploads/a.pdf"}]
    )
    r = TurnRecorder()
    r.record("ai.token", {"text": "嗨"})
    await seed_assistant_turn("p1", r, "complete")

    msgs = await repository.get_messages("s1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "嗨" and msgs[1].status == "complete"


async def test_sources_survive_history_reload(db):
    await seed_user_turn("s1", "p1", "预算多少", [])
    source = {
        "id": "ref1",
        "file_name": "a.txt",
        "container_path": "/workspace/uploads/a.txt",
        "fingerprint": "a" * 64,
        "location": {"kind": "lines", "start": 1, "end": 2},
        "excerpt": "预算 100 元",
    }
    recorder = TurnRecorder()
    recorder.record("sources.presented", {"sources": [source]})
    recorder.record("ai.token", {"text": "预算 100 元 [原文](#source-ref1)"})
    await seed_assistant_turn("p1", recorder, "complete")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        rows = (await client.get("/conversations/s1/messages")).json()
    assert rows[1]["parts"] == recorder.parts


async def test_legacy_delivered_scratchpad_path_points_to_output(db):
    """旧记录的 container_path 在 scratchpad/,读出时改指向 output/ 副本,下载接口才放行。"""
    legacy = {
        "file_name": "r.pdf",
        "container_path": "/workspace/scratchpad/r.pdf",
        "host_path": "/h",
    }
    current = {
        "file_name": "ok.pdf",
        "container_path": "/workspace/output/ok.pdf",
        "sha256": "a" * 64,
    }
    recorder = TurnRecorder()
    recorder.record("files.presented", {"files": [legacy, current]})
    await seed_user_turn("s1", "p1", "hi", [])
    await seed_assistant_turn("p1", recorder, "complete")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        rows = (await client.get("/conversations/s1/messages")).json()
        assets = (await client.get("/conversations/s1/assets")).json()
    paths = [f["container_path"] for f in rows[1]["parts"][0]["files"]]
    assert paths == ["/workspace/output/r.pdf", "/workspace/output/ok.pdf"]
    assert {f["container_path"] for f in assets["delivered"]} == set(paths)
    # 库里的原始数据不改
    stored = (await repository.get_messages("s1"))[1].parts[0]["files"][0]
    assert stored["container_path"] == "/workspace/scratchpad/r.pdf"


async def test_get_messages_unknown_conversation_is_none(db):
    assert await repository.get_messages("nope") is None


async def test_list_sorted_by_recent_update_with_counts(db):
    await seed_user_turn("old", "p", "旧会话", [])
    await seed_user_turn("new", "pn", "新会话", [])
    await seed_user_turn("old", "p2", "又说一句", [])  # old 更新时间变新
    rows = await repository.list_conversations()
    assert [c.id for c, _ in rows] == ["old", "new"]
    assert [n for _, n in rows] == [2, 1]


async def test_delete_cascades(db):
    await seed_user_turn("s1", "p", "hi", [{"file_name": "a", "container_path": "/x/a"}])
    assert await repository.delete_conversation("s1") is True
    assert await repository.get_messages("s1") is None
    assert await repository.delete_conversation("s1") is False


async def test_user_message_and_files_saved_together(db):
    await seed_user_turn("s1", "p", "hi", [{"file_name": "a", "container_path": "/x/a"}])
    msgs = await repository.get_messages("s1")
    files = await repository.get_message_files(msgs[0].id)
    assert [f["file_name"] for f in files] == ["a"]


# ---------- 路由 ----------


@pytest.fixture
async def client(db, monkeypatch):
    # 级联清理(Redis/沙箱/checkpoint)由 test_conversation_cleanup 单独覆盖,这里只测路由与库
    from app.api.routes import conversations

    monkeypatch.setattr(conversations, "delete_conversation_data", repository.delete_conversation)
    # ASGITransport 不触发 lifespan,避免连接 Redis;与 db fixture 共用同一事件循环
    await seed_user_turn("s1", "p1", "你好", [])
    r = TurnRecorder()
    r.record("ai.token", {"text": "嗨"})
    await seed_assistant_turn("p1", r, "complete")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_route_list_and_messages(client):
    listing = (await client.get("/conversations")).json()
    assert listing[0]["id"] == "s1" and listing[0]["message_count"] == 2

    msgs = (await client.get("/conversations/s1/messages")).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["parts"] == [{"type": "text", "text": "嗨"}]


async def test_route_unknown_conversation_404(client):
    assert (await client.get("/conversations/nope/messages")).status_code == 404
    assert (await client.delete("/conversations/nope")).status_code == 404


async def test_route_rename_and_delete(client):
    assert (await client.patch("/conversations/s1", json={"title": "新标题"})).status_code == 200
    assert (await client.get("/conversations")).json()[0]["title"] == "新标题"
    assert (await client.patch("/conversations/s1", json={"title": ""})).status_code == 422
    assert (await client.delete("/conversations/s1")).status_code == 204
    assert (await client.get("/conversations")).json() == []


async def test_route_rejects_bad_conversation_id(client):
    assert (await client.get("/conversations/bad.id/messages")).status_code == 422


async def test_route_timestamps_are_utc_marked(client):
    listing = (await client.get("/conversations")).json()
    assert listing[0]["created_at"].endswith("Z")
    msgs = (await client.get("/conversations/s1/messages")).json()
    assert msgs[0]["created_at"].endswith("Z")


async def test_usage_persisted_and_served(db):
    await seed_user_turn("s1", "p1", "hi", [])
    r = TurnRecorder()
    r.record("ai.token", {"text": "ok"})
    await seed_assistant_turn(
        "p1", r, "complete", {"llm_calls": 1, "input_tokens": 7, "output_tokens": 3}
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.get("/conversations/s1/usage")
        assert res.status_code == 200
        body = res.json()
        assert body["total_tokens"] == 10 and body["turns"] == 1
        assert (await c.get("/conversations/nope/usage")).status_code == 404


async def test_assistant_turn_saved_incrementally_in_one_row(db):
    await seed_user_turn("s1", "p1", "hi", [])
    r = TurnRecorder()
    r.record("ai.token", {"text": "先看看"})
    await seed_assistant_turn("p1", r, "running")
    r.record("tool.result", {"id": "c1", "name": "bash_tool", "content": "ok"})
    await seed_assistant_turn("p1", r, "running")
    r.record("ai.token", {"text": "完成"})
    await seed_assistant_turn("p1", r, "complete", {"total_tokens": 3})

    msgs = await repository.get_messages("s1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].status == "complete" and msgs[1].usage == {"total_tokens": 3}
    assert [p["type"] for p in msgs[1].parts] == ["text", "tool_result", "text"]
