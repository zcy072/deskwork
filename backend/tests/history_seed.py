"""测试用:经生产写入路径(db.runs 的 accept → start → save)造出历史轮次。"""

from app.db import runs as run_store
from app.services.history import TurnRecorder


async def seed_user_turn(
    session_id: str,
    prompt_id: str,
    text: str,
    files: list[dict] | None = None,
    owner: str = "",
    checkpoint_id: str = "",
) -> None:
    files = files or []
    await run_store.accept(
        prompt_id, session_id, owner, {"text": text, "files": files}, None, "", ""
    )
    await run_store.start(prompt_id, text, files, checkpoint_id)


async def seed_assistant_turn(
    prompt_id: str,
    recorder: TurnRecorder | list,
    status: str = "complete",
    usage: dict | None = None,
) -> None:
    parts = recorder.parts if isinstance(recorder, TurnRecorder) else recorder
    text = "".join(p["text"] for p in parts if p.get("type") == "text")
    await run_store.save(prompt_id, parts, text, 0, usage or {}, status)


async def seed_turn(
    session_id: str,
    prompt_id: str,
    question: str,
    answer: str = "ok",
    status: str = "complete",
    owner: str = "",
    files: list[dict] | None = None,
    usage: dict | None = None,
) -> None:
    await seed_user_turn(session_id, prompt_id, question, files, owner)
    await seed_assistant_turn(prompt_id, [{"type": "text", "text": answer}], status, usage)


async def run_turn(
    session_id: str,
    prompt_id: str,
    agent_input: str,
    raw_answer: str = "",
    owner: str = "",
    **input_json,
):
    """像 start_chat 那样先写入运行回执,再在当前任务里直接跑 _run_agent;返回最终的 Run。"""
    from app.services import chat_operations as ops

    run = await run_store.accept(
        prompt_id,
        session_id,
        owner,
        {"text": raw_answer or agent_input, "files": [], **input_json},
        None,
        "",
        "",
    )
    await ops._run_agent(prompt_id, session_id, agent_input, raw_answer, owner, run_record=run)
    return await run_store.get(prompt_id)


async def seed_legacy_turn(
    session_id: str,
    prompt_id: str,
    question: str,
    answer: str | None = None,
    files: list[dict] | None = None,
    owner: str = "",
    last_run_status: str | None = None,
    assistant_status: str = "complete",
) -> int:
    """旧版本留下的数据:只有消息行、没有 runs 回执(迁移与导入要兼容)。返回用户消息 id。"""
    from app.db import get_session, repository
    from app.db.models import Conversation, Message, UploadedFile

    await repository.ensure_conversation(session_id, question, owner)
    async with get_session() as db:
        user = Message(
            conversation_id=session_id,
            prompt_id=prompt_id,
            role="user",
            content=question,
            parts=[{"type": "text", "text": question}],
            status="complete",
        )
        db.add(user)
        await db.flush()
        db.add_all(
            UploadedFile(
                conversation_id=session_id,
                message_id=user.id,
                file_name=f["file_name"],
                container_path=f["container_path"],
            )
            for f in files or []
        )
        if answer is not None:
            db.add(
                Message(
                    conversation_id=session_id,
                    prompt_id=prompt_id,
                    role="assistant",
                    content=answer,
                    parts=[{"type": "text", "text": answer}] if answer else [],
                    status=assistant_status,
                )
            )
        if last_run_status is not None:
            conv = await db.get(Conversation, session_id)
            conv.last_run_status, conv.unread = last_run_status, True
        await db.commit()
        return user.id
