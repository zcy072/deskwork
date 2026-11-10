"""Validate a database copy and an isolated deployment, optionally with the configured LLM.

Run with backend/.venv/bin/python. Existing service data is opened read-only;
all Docker resources created here have a unique name and are removed in finally.
The optional model cases use a fresh database and a fresh API identity.
"""

import argparse
import asyncio
import json
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.strip()}")
    return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def counts(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in (
                "conversations",
                "messages",
                "uploaded_files",
                "memories",
                "runs",
            )
            if name in tables
        }


async def migrate_copy(output):
    from app.agents.agent import get_agent
    from app.agents.checkpointers import close_checkpointer, init_checkpointer
    from app.db import close_db, init_db, repository, runs

    before = counts(output / "legacy" / "deskwork.sqlite")
    await init_db()
    await init_checkpointer()
    try:
        graph = get_agent()
        read = 0
        # Read checkpoint state only; do not invoke the graph or a model.
        from app.db import get_session

        async with get_session() as db:
            from app.db.models import Conversation
            from sqlalchemy import select

            identities = (
                await db.execute(
                    select(Conversation.id, Conversation.owner)
                    .order_by(Conversation.updated_at.desc())
                    .limit(3)
                )
            ).all()
        for sid, owner in identities:
            await graph.aget_state(await runs.graph_config(sid))
            assert await repository.get_messages(sid, owner=owner or "") is not None
            read += 1
    finally:
        get_agent.cache_clear()
        await close_checkpointer()
        await close_db()
    after = counts(output / "legacy" / "deskwork.sqlite")
    assert all(after[name] == value for name, value in before.items())
    await init_db()
    await close_db()
    assert counts(output / "legacy" / "deskwork.sqlite") == after
    (output / "migration.json").write_text(
        json.dumps(
            {
                "before": before,
                "after": after,
                "checkpoint_threads_read": read,
                "idempotent": True,
            },
            indent=2,
        )
    )


def ready(url, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url + "/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Isolated service did not become ready: {url}")


def port(container, target):
    address = docker("port", container, f"{target}/tcp").splitlines()[0]
    return "http://" + address


def send(client, sid, text, **extra):
    request_id = uuid.uuid4().hex
    data = {
        "session_id": sid,
        "user_query": text,
        "client_request_id": request_id,
        **extra,
    }
    result = client.post("/send-message", data=data)
    result.raise_for_status()
    pid = result.json()["prompt_id"]
    duplicate = client.post("/send-message", data=data)
    duplicate.raise_for_status()
    assert duplicate.json()["prompt_id"] == pid
    return pid


def consume(client, sid, pid):
    cursor, seen, packets = "0-0", set(), []
    deadline = time.monotonic() + 240
    disconnected = False
    while time.monotonic() < deadline:
        with client.stream(
            "POST",
            "/stream-events",
            json={
                "session_id": sid,
                "prompt_id": pid,
                "start_id": cursor,
                "block_ms": 4000,
            },
            timeout=30,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                packet = json.loads(line[5:])
                cursor = packet["id"]
                assert cursor not in seen, "Repeated Redis cursor after reconnection"
                seen.add(cursor)
                packets.append(packet)
                if packet["event"] == "stream.end":
                    run = client.get(f"/runs/{pid}")
                    run.raise_for_status()
                    saved = run.json()
                    assert saved["status"] in ("complete", "waiting"), saved.get(
                        "message"
                    )
                    return saved, packets
                if not disconnected:
                    disconnected = True
                    break  # Deliberately detach once, then resume from the cursor.
    client.post(f"/thread-cancel/{sid}")
    raise TimeoutError("Isolated model case exceeded 240 seconds")


def export(client, sid):
    response = client.get("/files/export", params={"session_id": sid}, timeout=60)
    response.raise_for_status()
    return response.content


def import_archive(client, archive):
    response = client.post(
        "/conversations/import", files={"file": ("session.zip", archive)}, timeout=60
    )
    response.raise_for_status()
    result = response.json()
    assert result["resumable"], result.get("warnings")
    return result["id"]


def download(client, sid):
    result = client.get(
        "/files/download",
        params={"session_id": sid, "relative_path": "output/verification.txt"},
    )
    result.raise_for_status()
    return result.content


def model_cases(base, token, restart):
    headers = {"Authorization": f"Bearer {token}"}
    client = httpx.Client(base_url=base, headers=headers, timeout=40)
    sessions_created = []
    try:
        sid = "acceptance-" + uuid.uuid4().hex
        sessions_created.append(sid)
        prompt = (
            "这是隔离验收。先且只调用一次 ask_user_input, type=free_text, question=请输入测试编号。"
            "收到回答后,先用 create_file 创建 /workspace/output/verification.txt,内容精确为"
            " refactor-ok:<用户回答>。create_file 成功后再用 present_files 交付。"
            "不要读其他文件、不要联网、不要委派。最后简短回答。"
        )
        first = send(client, sid, prompt)
        waiting, packets = consume(client, sid, first)
        assert waiting["status"] == "waiting"
        question = next(p["data"] for p in packets if p["event"] == "ask.user")
        waiting_archive = export(client, sid)
        print(
            "PASS: real-model interrupt, request idempotency, SSE reconnect", flush=True
        )

        client.close()
        restart()
        ready(base)
        client = httpx.Client(base_url=base, headers=headers, timeout=40)
        assert client.get(f"/runs/{first}").json()["status"] == "waiting"
        resumed = send(
            client,
            sid,
            "case-001",
            mode="resume",
            interrupt_id=question["interrupt_id"],
        )
        result, packets = consume(client, sid, resumed)
        assert result["status"] == "complete"
        assert download(client, sid) == b"refactor-ok:case-001"
        old = next(
            p["data"]["files"][0] for p in packets if p["event"] == "files.presented"
        )
        print(
            "PASS: restart then resume, real tool execution and artifact delivery",
            flush=True,
        )

        imported_waiting = import_archive(client, waiting_archive)
        sessions_created.append(imported_waiting)
        answer = send(
            client,
            imported_waiting,
            "import-001",
            mode="resume",
            interrupt_id=question["interrupt_id"],
        )
        consume(client, imported_waiting, answer)
        assert download(client, imported_waiting) == b"refactor-ok:import-001"
        print(
            "PASS: imported waiting checkpoint resumes under a new session and run identity",
            flush=True,
        )

        answer_row = next(
            m
            for m in client.get(f"/conversations/{sid}/messages").json()
            if m["role"] == "user" and m["content"] == "case-001"
        )
        changed = send(
            client,
            sid,
            "case-002",
            mode="resume",
            interrupt_id=question["interrupt_id"],
            rewind_to_message_id=str(answer_row["id"]),
        )
        consume(client, sid, changed)
        assert download(client, sid) == b"refactor-ok:case-002"
        print(
            "PASS: editing a prior confirmation uses the new answer, without reusing pending writes",
            flush=True,
        )

        messages = client.get(f"/conversations/{sid}/messages").json()
        first_user = next(m for m in messages if m["role"] == "user")
        replacement = send(
            client,
            sid,
            "不要提问。先用 create_file 创建 /workspace/output/verification.txt,内容精确为 replacement-ok。"
            "成功后调用 present_files 交付,然后结束。不要委派、不要联网。",
            rewind_to_message_id=str(first_user["id"]),
        )
        consume(client, sid, replacement)
        assert download(client, sid) == b"replacement-ok"
        previous = client.get(
            "/files/delivered",
            params={
                "session_id": sid,
                "file_name": old["file_name"],
                "sha256": old["sha256"],
                "size": old["size"],
            },
        )
        previous.raise_for_status()
        assert previous.content == b"refactor-ok:case-001"
        print(
            "PASS: full rewind to empty graph, workspace identity and historical file version",
            flush=True,
        )

        imported = import_archive(client, export(client, sid))
        sessions_created.append(imported)
        assert download(client, imported) == b"replacement-ok"
        continuation = send(
            client,
            imported,
            "调用 view_file 读取 /workspace/output/verification.txt,然后只回答文件内容。不要联网或委派。",
        )
        result, _ = consume(client, imported, continuation)
        assert "replacement-ok" in "".join(
            p.get("text", "") for p in result["parts"] if p["type"] == "text"
        )
        print(
            "PASS: completed-session backup import and model continuation", flush=True
        )
        usage = client.get(f"/conversations/{imported}/usage").json()
        return {"cases": 6, "sessions": sessions_created, "continuation_usage": usage}
    finally:
        client.close()


def main(args):
    output = Path(
        args.output or tempfile.mkdtemp(prefix="deskwork-acceptance-", dir="/tmp")
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    prefix = "deskwork-check-" + uuid.uuid4().hex[:10]
    names, network = [], prefix + "-net"
    token = secrets.token_urlsafe(24)
    env = {
        "API_KEYS": token,
        "API_KEY": token,
        "REDIS_PASSWORD": secrets.token_urlsafe(24),
        "SANDBOX_MANAGER_TOKEN": secrets.token_urlsafe(24),
        "SANDBOX_CONTAINER": prefix + "-sandbox",
        "REDIS_HOST": "redis",
        "REDIS_DB": "0",
        "SANDBOX_MANAGER_URL": "http://sandbox-manager:8000",
        "SANDBOX_DATA_DIR": "/app/sandbox_data",
        "DATA_DIR": "/app/data",
        "DATABASE_URL": "",
        "LOG_FILE": "",
        "MCP_SERVERS": "",
        "MEMORY_ENABLED": "false",
        "LLM_FALLBACKS": "",
        "AGENT_MAX_LLM_CALLS": "12",
        "AGENT_MAX_TOTAL_TOKENS": "250000",
        "SNAPSHOT_KEEP": "6",
        "REVIEW_LLM_PROFILE": "",
        "WEB_TOOLS_API_KEY": "",
        "PREVIEW_PREWARM": "false",
    }
    env_file = output / "isolation.env"
    if args.profile:
        env["LLM_PROFILE"] = args.profile
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in env.items()) + "\n"
    )
    env_file.chmod(0o600)
    report = {
        "output": str(output),
        "real_model": args.model,
        "profile": args.profile,
        "passed": False,
    }
    try:
        legacy = output / "legacy"
        legacy.mkdir(exist_ok=True)
        for name in ("deskwork.sqlite", "checkpoints.sqlite"):
            source = REPO / "backend/data" / name
            with (
                sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src,
                sqlite3.connect(legacy / name) as dst,
            ):
                src.backup(dst)
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/app/.venv/bin/python",
            "-e",
            "PYTHONPATH=/app",
            "-e",
            "LLM_MODEL_NAME=test",
            "-e",
            "LLM_API_KEY=test",
            "-e",
            "LOG_FILE=",
            "-e",
            "DATA_DIR=/audit/legacy",
            "-v",
            f"{REPO}/backend/app:/app/app:ro",
            "-v",
            f"{output}:/audit",
            "-v",
            f"{Path(__file__).resolve()}:/tmp/verify.py:ro",
            "deskwork-backend",
            "/tmp/verify.py",
            "--migrate-copy",
            "/audit",
        )
        report["migration"] = json.loads((output / "migration.json").read_text())
        print(
            "PASS: read-only source backup, copied SQLite migration and checkpoint reads",
            flush=True,
        )
        docker("network", "create", network)

        def start(name, *options, image, command=()):
            full = prefix + "-" + name
            docker("run", "-d", "--name", full, *options, image, *command)
            names.append(full)
            return full

        start(
            "redis",
            "--network",
            network,
            "--network-alias",
            "redis",
            "--env-file",
            str(env_file),
            image="redis:7-alpine",
            command=(
                "sh",
                "-c",
                'exec redis-server --requirepass "$REDIS_PASSWORD" --save "" --appendonly no',
            ),
        )
        sandbox_root = output / "sandbox_data"
        sandbox_root.mkdir(exist_ok=True)
        skills = REPO / "sandbox_data/default_skills"
        start(
            "sandbox",
            "--init",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            f"seccomp={REPO}/sandbox/seccomp.json",
            "--memory",
            "1g",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:size=256m",
            "--tmpfs",
            "/run:size=16m",
            "-v",
            f"{sandbox_root}:/sandbox_data",
            "-v",
            f"{skills}:/sandbox_data/default_skills:ro",
            image="sandbox:latest",
        )
        start(
            "manager",
            "--network",
            network,
            "--network-alias",
            "sandbox-manager",
            "--env-file",
            str(env_file),
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            image="deskwork-sandbox-manager",
        )
        (output / "data").mkdir(exist_ok=True)
        backend = start(
            "backend",
            "--network",
            network,
            "--network-alias",
            "backend",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--env-file",
            str(REPO / ".env"),
            "--env-file",
            str(env_file),
            "-p",
            f"127.0.0.1:{available_port()}:5001",
            "-v",
            f"{REPO}/backend/app:/app/app:ro",
            "-v",
            f"{output}/data:/app/data",
            "-v",
            f"{sandbox_root}:/app/sandbox_data",
            "-v",
            f"{skills}:/app/sandbox_data/default_skills:ro",
            image="deskwork-backend",
        )
        base = port(backend, 5001)
        ready(base)
        web_image = docker("inspect", "deskwork_web", "--format", "{{.Config.Image}}")
        web = start(
            "web",
            "--network",
            network,
            "--env-file",
            str(env_file),
            "-p",
            f"127.0.0.1:{available_port()}:80",
            "-v",
            f"{REPO}/frontend/dist:/usr/share/nginx/html:ro",
            "-v",
            f"{REPO}/frontend/nginx.conf.template:/etc/nginx/templates/default.conf.template:ro",
            image=web_image,
        )
        web_url = port(web, 80)
        ready(web_url)
        proxy = httpx.get(web_url + "/runs/absent", timeout=10)
        assert proxy.status_code == 404 and "application/json" in proxy.headers.get(
            "content-type", ""
        )
        report["proxy"] = "runs route reaches the authenticated backend"
        if args.model:

            def restart_backend():
                docker("restart", backend)
                ready(base)
                docker("restart", web)
                ready(web_url)

            report["model"] = model_cases(base, token, restart_backend)
        if args.browser:
            result = subprocess.run(
                [
                    "node",
                    str(REPO / "scripts/verify_refactor_browser.cjs"),
                    web_url,
                    str(output),
                    args.browser,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            (output / "browser.stdout.log").write_text(result.stdout)
            (output / "browser.stderr.log").write_text(result.stderr)
            if result.returncode:
                raise RuntimeError(
                    f"Browser acceptance failed; see {output / 'browser.stderr.log'}"
                )
            print(result.stdout, flush=True)
            report["browser"] = json.loads((output / "browser.json").read_text())
        report["passed"] = True
    finally:
        for name in reversed(names):
            try:
                logs = docker("logs", name)
                for value in env.values():
                    if len(value) > 15:
                        logs = logs.replace(value, "[REDACTED]")
                (output / f"{name}.log").write_text(logs)
                docker("rm", "-f", name)
            except RuntimeError:
                pass
        subprocess.run(
            ["docker", "network", "rm", network], capture_output=True, check=False
        )
        (output / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False)
        )
        print(f"Acceptance report: {output / 'report.json'}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument(
        "--profile", help="Existing named LLM profile used only by the isolated service"
    )
    parser.add_argument(
        "--model", action="store_true", help="Make bounded real model calls using .env"
    )
    parser.add_argument(
        "--browser",
        help="Path to playwright-core package; also makes one UI model call",
    )
    parser.add_argument("--migrate-copy", help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.migrate_copy:
        asyncio.run(migrate_copy(Path(options.migrate_copy)))
    else:
        sys.exit(main(options))
