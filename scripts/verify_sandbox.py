"""真实沙箱生命周期验收,只操作随机 accept-* 会话并在 finally 清理。

运行: docker compose exec -T -e LOG_LEVEL=WARNING backend python - < scripts/verify_sandbox.py
仅修改本测试进程中的设置,不改变服务配置。需已启动 Compose。
"""

import asyncio
import json
import uuid

from app.config import settings
from app.sandbox.bash_handler import BashSessionsManager


async def main():
    mgr = BashSessionsManager()
    prefix = "accept-" + uuid.uuid4().hex[:10]
    ids = [f"{prefix}-{i}" for i in range(8)]
    results = {}
    try:
        settings.SANDBOX_MAX_SHELLS = 0
        sessions = await asyncio.gather(*(mgr.get_or_create(s) for s in ids))
        a, b = await asyncio.gather(mgr.get_or_create(ids[0]), mgr.get_or_create(ids[0]))
        assert a is b

        async def work(i, s):
            out, err = await s.execute(
                f"echo {i} > /workspace/output/check.txt; sleep 0.2; cat /workspace/output/check.txt"
            )
            assert not err and str(i) in out, (i, out)

        for _ in range(25):
            await asyncio.gather(*(work(i, s) for i, s in enumerate(sessions)))
        results["parallel_commands"] = 200
        settings.SANDBOX_MAX_SHELLS = 3
        await mgr.reap_idle_shells(keep=ids[-1])
        assert sum(s.shell_alive for s in sessions) == 3
        results["lru_alive"] = 3
        settings.SANDBOX_MAX_SHELLS = 0
        settings.SANDBOX_SHELL_IDLE_SEC = 1
        await asyncio.sleep(1.2)
        await mgr.reap_idle_shells()
        assert not any(s.shell_alive for s in sessions)
        results["idle_reap"] = True
        s = await mgr.get_or_create(ids[0])
        out, err = await s.execute("cat /workspace/output/check.txt")
        assert not err and "0" in out
        results["restart_preserves_files"] = True
        task = asyncio.create_task(s.execute("sleep 30", timeout=40))
        await asyncio.sleep(1)
        s.last_used -= 10
        await mgr.reap_idle_shells()
        assert s.shell_alive
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not s.shell_alive
        results["busy_not_reaped_cancel_closes"] = True
        out, err = await s.execute("echo recovered")
        assert not err and "recovered" in out
        await s._process._ws.close()
        await asyncio.sleep(1)
        assert not s.shell_alive
        out, err = await s.execute("echo reconnected")
        assert not err and "reconnected" in out
        results["disconnect_reconnect"] = True
        out, err = await s.execute("sleep 5", timeout=1)
        assert err and "timed out" in out
        out, err = await s.execute("echo after_timeout")
        assert not err and "after_timeout" in out
        results["timeout_recovery"] = True
        for _ in range(5):
            t = asyncio.create_task(s.execute("sleep 30", timeout=40))
            await asyncio.sleep(0.2)
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            assert not s.shell_alive
        results["repeated_cancellation"] = 5
    finally:
        await mgr.close_all()
        for sid in ids:
            await mgr.purge_session(sid)
        remaining = await mgr._docker_client.run_exec(mgr.container_name, "ps -eo stat,args")
        assert remaining.returncode == 0
        assert not any(prefix in line and "bwrap" in line for line in remaining.stdout.splitlines())
        results["test_processes_remaining"] = 0
        await mgr._docker_client.http.aclose()
    print("STABILITY_RESULT", json.dumps(results), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
