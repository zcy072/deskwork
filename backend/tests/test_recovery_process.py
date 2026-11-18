"""Kill a worker between filesystem/SQL steps and recover in a fresh process."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = r"""
import asyncio, json, os, shutil, uuid
from pathlib import Path
from sqlalchemy import update
from app.config import settings
from app.db import init_db, close_db, get_session, repository, runs
from app.db.models import Conversation
from app.sandbox.session_paths import SessionPaths
from app.services.rewind import RewindOperation, recover_operations

root = Path(os.environ["RECOVERY_TEST_ROOT"])
settings.DATA_DIR = str(root / "data")

async def main():
    await init_db()
    paths = SessionPaths.create("s")
    mode = os.environ["RECOVERY_TEST_MODE"]
    if mode == "recover":
        await recover_operations(paths.session_root.parent)
        await runs.recover_interrupted()
        await recover_operations(paths.session_root.parent)
        config = await runs.graph_config("s")
        messages = await repository.get_messages("s")
        result = {
            "files": [(paths.session_root / d / "data.txt").read_text() for d in ("output", "scratchpad")],
            "checkpoint": config["configurable"].get("checkpoint_id"),
            "messages": [m.content for m in messages if m.role == "user"],
            "status": (await runs.latest("s")).status,
            "journals": len(list(paths.session_root.glob("operations/*/journal.json"))),
        }
        (root / "result.json").write_text(json.dumps(result))
        await close_db()
        return
    await repository.ensure_conversation("s", "old question")
    old = await repository.add_user_message_with_files("s", prompt_id="old", content="old question", parts=[], files=[])
    async with get_session() as db:
        await db.execute(update(Conversation).where(Conversation.id == "s").values(graph_checkpoint_id="current-head"))
        await db.commit()
    for name in ("output", "scratchpad"):
        (paths.session_root / name).mkdir(parents=True)
        (paths.session_root / name / "data.txt").write_text("current")
        (root / "snapshot" / name).mkdir(parents=True)
        (root / "snapshot" / name / "data.txt").write_text("target")
    pid = str(uuid.uuid4())
    await runs.accept(pid, "s", "", {"text": "new question", "files": []}, None, "", "trace")
    op = RewindOperation(paths, pid, old.id, {"thread_id": "s", "checkpoint_id": "target-head"}, True)
    op.stage(root / "snapshot")
    if mode == "staged": os._exit(91)
    (op.root / "backup").mkdir()
    paths.output.rename(op.root / "backup" / "output")
    if mode == "old-moved": os._exit(91)
    (op.root / "stage" / "output").rename(paths.output)
    if mode == "first-installed": os._exit(91)
    paths.scratchpad.rename(op.root / "backup" / "scratchpad")
    (op.root / "stage" / "scratchpad").rename(paths.scratchpad)
    if mode == "both-installed": os._exit(91)
    await runs.start(pid, "new question", [], "target-head", old.id, op.graph_ref)
    if mode == "committed": os._exit(91)
    shutil.rmtree(op.root / "backup" / "output")
    os._exit(91)

asyncio.run(main())
"""


@pytest.mark.parametrize(
    "phase", ["staged", "old-moved", "first-installed", "both-installed", "committed", "cleanup"]
)
def test_process_crash_recovers_one_consistent_state(tmp_path, phase):
    env = dict(
        os.environ,
        RECOVERY_TEST_ROOT=str(tmp_path),
        RECOVERY_TEST_MODE=phase,
        SANDBOX_DATA_DIR=str(tmp_path / "sandbox_data"),
        LOG_FILE="",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    crashed = subprocess.run(
        [sys.executable, "-c", WORKER],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert crashed.returncode == 91, crashed.stderr
    recovered = subprocess.run(
        [sys.executable, "-c", WORKER],
        env=env | {"RECOVERY_TEST_MODE": "recover"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads((tmp_path / "result.json").read_text())
    committed = phase in ("committed", "cleanup")
    assert result["files"] == (["target"] * 2 if committed else ["current"] * 2)
    assert result["checkpoint"] == ("target-head" if committed else "current-head")
    assert ("old question" not in result["messages"]) == committed
    assert "new question" in result["messages"]
    assert result["status"] == "error" and result["journals"] == 0
