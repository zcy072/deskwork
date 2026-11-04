"""高风险 bash 命令确认:中间件在执行前 interrupt,拒绝时不执行。"""

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.middlewares import ToolGuardMiddleware
from app.agents.risk import risky_reason
from app.config import settings
from tests.test_ask_user import ScriptedModel

RAN: list[str] = []


@tool
def bash_tool(command: str) -> str:
    """run"""
    RAN.append(command)
    return "ok"


def _call(cmd):
    return AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "bash_tool", "args": {"command": cmd}}],
    )


def _agent(cmd):
    return create_agent(
        model=ScriptedModel(script=[_call(cmd), AIMessage(content="done")]),
        tools=[bash_tool],
        system_prompt="test",
        checkpointer=InMemorySaver(),
        middleware=[ToolGuardMiddleware()],
    )


@pytest.mark.parametrize(
    "cmd,risky",
    [
        ("rm -f /workspace/uploads/a.docx", True),
        ("find . -name '*.tmp' -delete", True),
        ("echo hi > /workspace/uploads/a.txt", True),
        ("rm -f /workspace/scratchpad/a.js", False),
        ("cd /workspace/scratchpad && rm -f make.js && echo removed", False),
        ("cd /workspace/scratchpad && node x.js && rm -f out.pdf", False),
        ("rm -f make.js", True),
        ("rm -rf /workspace/scratchpad/../uploads", True),
        (
            "ln -s /workspace/output /workspace/scratchpad/o && rm -rf /workspace/scratchpad/o/",
            True,
        ),
        ("cd /workspace/output && rm -f a.docx", True),
        ("rm -f /workspace/scratchpad/a.js /workspace/output/b.docx", True),
        ("rm -f $TARGET", True),
        (
            "cd /workspace/scratchpad && python3 - <<'EOF'\ns=s.replace('a; b','c && d')\nEOF\nnode x.js",
            False,
        ),
        ("cd /workspace/scratchpad && cat <<'EOF'\nrm -rf /\nEOF\nls", False),
        ("cd /workspace/scratchpad && python3 - <<'EOF'\nprint(1)\nEOF\nrm -f t1-*.png", False),
        (
            "cd /workspace/scratchpad && python3 - <<'EOF'\nprint(1)\nEOF\nrm -f /workspace/uploads/a",
            True,
        ),
        ("cd /workspace/scratchpad && python3 -c \"print('a;b')\"; rm -f x.png *.pdf", False),
        ('grep -iE "xx|yy" f; ls; rm -f /workspace/uploads/a', True),
        ("bash -c 'rm -f /workspace/scratchpad/a'", True),
        ("ls -la /workspace", False),
        ("python script.py > /workspace/output/r.txt", False),
    ],
)
def test_risky_reason(cmd, risky):
    assert bool(risky_reason(cmd)) is risky


async def _run(cmd, answer):
    RAN.clear()
    ag = _agent(cmd)
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    snap = await ag.aget_state(cfg)
    assert snap.interrupts
    payload = snap.interrupts[0].value
    await ag.ainvoke(Command(resume=answer), cfg)
    return payload


async def test_allow_runs_command():
    payload = await _run("rm -f /workspace/uploads/a.docx", "允许")
    assert payload["options"] == ["允许", "拒绝"]
    assert RAN == ["rm -f /workspace/uploads/a.docx"]


async def test_deny_skips_command():
    await _run("rm -f /workspace/uploads/a.docx", "拒绝")
    assert RAN == []


async def test_safe_command_no_interrupt():
    RAN.clear()
    ag = _agent("ls")
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert not (await ag.aget_state(cfg)).interrupts
    assert RAN == ["ls"]


async def test_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "CONFIRM_RISKY_COMMANDS", False)
    RAN.clear()
    ag = _agent("rm -f x")
    cfg = {"configurable": {"thread_id": "t"}}
    await ag.ainvoke({"messages": [("user", "go")]}, cfg)
    assert RAN == ["rm -f x"]


def test_bare_cd_and_tilde_leave_scratchpad():
    assert risky_reason("cd /workspace/scratchpad; cd; rm -rf *")
    assert risky_reason("cd /workspace/scratchpad; rm -rf ~/x")
    assert risky_reason("cd /workspace/scratchpad; cd ~; rm -rf x")


@pytest.mark.parametrize(
    "cmd",
    [
        'echo "<<X"; rm -rf /workspace/uploads/*\nX\n',
        "echo '<<EOF'\nrm -rf /workspace/output\nEOF\n",
        "rmdir /workspace/uploads/d",
        "unlink /workspace/uploads/a.csv",
        "echo hi | tee ./uploads/a.csv",
        "echo hi > ./uploads/a.csv",
    ],
)
def test_review_gaps_are_risky(cmd):
    assert risky_reason(cmd) is not None


def test_real_heredoc_body_still_ignored():
    cmd = "cat > /workspace/scratchpad/a.sh <<'EOF'\nrm -rf /tmp/x\nEOF\n"
    assert risky_reason(cmd) is None


def test_rmdir_in_scratchpad_is_fine():
    assert risky_reason("rmdir /workspace/scratchpad/tmp") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "cat notes-rm.md",
        'grep -n "dd of=" log.txt',
        "python3 -c 'print(\"rm is a command\")'",
        "python3 - <<'EOF'\ndef truncate(s, n):\n    return s[:n]\nprint(truncate('abc', 2))\nEOF",
        "python3 build.py --truncate 10",
        "echo 'pip uninstall is blocked'",
        "ls /workspace/output/dd_of=1.txt",
        "chmod +x run.sh",
        "find . -name '*.py' -exec grep -l foo {} +",
    ],
)
def test_words_in_args_and_strings_are_not_risky(cmd):
    assert risky_reason(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "truncate -s 0 /workspace/output/a.docx",
        "shred -u /workspace/output/a.docx",
        "dd if=/dev/zero of=/workspace/output/a.docx",
        "chmod -R 777 /workspace/output",
        "chown -R root /workspace/output",
        "find /workspace/output -exec rm {} \\;",
        "pip uninstall -y openpyxl",
        "python3 -m pip uninstall openpyxl",
        "mv a.csv /workspace/uploads/b.csv",
        "sudo rm -rf /workspace/output",
        "env FOO=1 rm -rf /workspace/output",
        "xargs rm < list.txt",
        "bash -c 'truncate -s 0 /workspace/output/a'",
        "echo $(rm -rf /workspace/output)",
        "FOO=1 /bin/rm -f /workspace/output/a",
        "cd /workspace/output && truncate -s 0 a.docx",
        "echo 'unterminated; rm -rf /workspace/output",
    ],
)
def test_real_dangerous_variants_still_risky(cmd):
    assert risky_reason(cmd) is not None
