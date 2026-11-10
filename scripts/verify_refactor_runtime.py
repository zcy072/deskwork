"""Exercise a random, disposable session against sandbox-manager (no model or DB)."""

import asyncio
import uuid

from app.sandbox import CONTAINER_MANAGER


async def main():
    sid = "refactor-check-" + uuid.uuid4().hex
    session = await CONTAINER_MANAGER.get_or_create(sid)
    try:
        out, failed = await session.execute(
            "printf original > /workspace/output/guard.txt"
        )
        assert not failed, out
        await session.execute_readonly(
            "python3 -c \"open('/workspace/output/guard.txt', 'w').write('changed')\"",
            10,
        )
        assert (session.paths.output / "guard.txt").read_text() == "original"
        out, failed = await session.execute_readonly(
            "python3 -c \"open('/tmp/allowed.txt', 'w').write('ok'); print('tmp-ok')\"",
            10,
        )
        assert "tmp-ok" in out and not failed, out
        out, failed = await session.execute("printf alive")
        assert "alive" in out and not failed, out
        task = asyncio.create_task(session.execute("sleep 30"))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert not session.shell_alive
        print(
            "PASS: independent read-only workspace, private tmp, parent shell retained, cancellation closes shell"
        )
    finally:
        await CONTAINER_MANAGER.purge_session(sid)
        await CONTAINER_MANAGER.close_all()


asyncio.run(main())
