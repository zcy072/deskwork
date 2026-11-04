"""view_image:未配置视觉模型明确报错;路径/格式/大小/符号链接受限;每轮次数上限;结果走视觉模型。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.config import settings
from app.sandbox.session_paths import SessionPaths
from app.services import image_view
from app.tools import image_tools
from app.tools.image_tools import view_image

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeSession:
    def __init__(self, paths):
        self.paths = paths

    def resolve_virtual_path(self, raw):
        if not raw.startswith("/workspace/"):
            raise ValueError(f"Path '{raw}' escapes workspace directory")
        import posixpath

        return posixpath.normpath(raw)


@pytest.fixture
def session(tmp_path, monkeypatch):
    paths = SessionPaths.__new__(SessionPaths)
    root = tmp_path / "sess"
    for d in ("uploads", "output", "scratchpad"):
        (root / d).mkdir(parents=True)
    monkeypatch.setattr(SessionPaths, "session_root", property(lambda self: root), raising=False)
    monkeypatch.setattr(SessionPaths, "virtual_root", "/workspace", raising=False)
    sess = FakeSession(paths)

    class Manager:
        async def get_or_create(self, _sid):
            return sess

    monkeypatch.setattr(image_view, "CONTAINER_MANAGER", Manager())
    monkeypatch.setattr(settings, "REVIEW_LLM_PROFILE", "vision")
    return root


def test_vision_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LLM_PROFILE", "")
    assert not image_view.vision_enabled()


async def test_load_image_ok(session):
    (session / "uploads" / "a.PNG").write_bytes(PNG)
    data, mime = await image_view.load_image("s", "/workspace/uploads/a.PNG")
    assert data == PNG and mime == "image/png"


@pytest.mark.parametrize(
    ("path", "match"),
    [
        ("/workspace/uploads/a.txt", "不支持的图片格式"),
        ("/workspace/scratchpad/a.png", "只能查看"),
        ("/workspace/uploads/missing.png", "File not found"),
        ("/etc/passwd.png", "escapes"),
    ],
)
async def test_load_image_rejected(session, path, match):
    (session / "scratchpad" / "a.png").write_bytes(PNG)
    with pytest.raises(RuntimeError, match=match):
        await image_view.load_image("s", path)


async def test_load_image_too_large(session, monkeypatch):
    monkeypatch.setattr(settings, "VIEW_IMAGE_MAX_MB", 1)
    (session / "uploads" / "big.png").write_bytes(b"0" * (1024 * 1024 + 10))
    with pytest.raises(RuntimeError, match="超过大小上限"):
        await image_view.load_image("s", "/workspace/uploads/big.png")


async def test_load_image_refuses_symlink(session, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    (session / "uploads" / "link.png").symlink_to(secret)
    with pytest.raises(RuntimeError, match="读取图片失败"):
        await image_view.load_image("s", "/workspace/uploads/link.png")


async def test_describe_image_sends_image_to_vision_model(monkeypatch):
    seen = {}

    class FakeModel:
        async def ainvoke(self, messages):
            seen["content"] = messages[0].content
            return AIMessage(content=[{"type": "text", "text": "一只猫"}])

    monkeypatch.setattr(
        image_view, "get_named_model", lambda name: (SimpleNamespace(model=name), FakeModel())
    )
    monkeypatch.setattr(settings, "REVIEW_LLM_PROFILE", "vision")
    out = await image_view.describe_image(PNG, "image/png", "图里有什么?")
    assert out == "一只猫"
    assert "图里有什么?" in seen["content"][0]["text"]
    assert seen["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_message_text_handles_streamed_blocks():
    # 流式调用时 Anthropic 返回 [裸字符串, thinking 块, 裸字符串],文字不在 {"type": "text"} 里
    blocks = ["", {"type": "thinking", "thinking": "内部思考"}, "DESKWORK-7421"]
    assert image_view.message_text(blocks) == "DESKWORK-7421"
    assert image_view.message_text([{"type": "text", "text": "一只猫"}]) == "一只猫"
    assert image_view.message_text(" 纯文本 ") == "纯文本"
    assert image_view.message_text([{"type": "thinking", "thinking": "只有思考"}]) == ""


def _runtime(messages=()):
    return SimpleNamespace(
        config={"configurable": {"thread_id": "s"}},
        tool_call_id="c1",
        state={"messages": list(messages)},
    )


def _msg(command):
    return command.update["messages"][0]


async def _call(runtime, path="/workspace/uploads/a.png"):
    return await view_image.coroutine(path=path, description="d", runtime=runtime, question="q")


async def test_tool_reports_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LLM_PROFILE", "")
    msg = _msg(await _call(_runtime()))
    assert msg.status == "error" and "未配置视觉模型" in msg.content


async def test_tool_success(session, monkeypatch):
    (session / "uploads" / "a.png").write_bytes(PNG)

    async def fake_describe(data, mime, question):
        return f"saw {len(data)} bytes, q={question}"

    monkeypatch.setattr(image_tools, "describe_image", fake_describe)
    msg = _msg(await _call(_runtime()))
    assert msg.status == "success" and msg.content == f"saw {len(PNG)} bytes, q=q"


async def test_tool_turn_limit_counts_only_current_turn(session, monkeypatch):
    (session / "uploads" / "a.png").write_bytes(PNG)
    monkeypatch.setattr(settings, "VIEW_IMAGE_MAX_PER_TURN", 2)

    async def fake_describe(*_a):
        return "ok"

    monkeypatch.setattr(image_tools, "describe_image", fake_describe)

    def used(*ids):
        calls = [{"id": i, "name": "view_image", "args": {}} for i in ids]
        return [AIMessage("", tool_calls=calls)]

    old_turn = [HumanMessage("old"), *used("a", "b"), HumanMessage("new"), *used("c1")]
    assert _msg(await _call(_runtime(old_turn))).status == "success"  # 新一轮重新计数
    # 同一条回复里并行发起 3 次:第 3 次(c1)超限,哪怕前两次的结果还没写进 state
    msg = _msg(await _call(_runtime([HumanMessage("new"), *used("a", "b", "c1")])))
    assert msg.status == "error" and "上限" in msg.content
    assert (
        _msg(await _call(_runtime([HumanMessage("new"), *used("c1", "a", "b")]))).status
        == "success"
    )


async def test_tool_surfaces_load_errors(session):
    msg = _msg(await _call(_runtime(), path="/workspace/uploads/nope.png"))
    assert msg.status == "error" and "File not found" in msg.content
