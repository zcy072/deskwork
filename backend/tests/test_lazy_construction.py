"""import 阶段不得构造 LLM / Agent;首次调用才构造,且结果被缓存。"""

import importlib
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_import_does_not_build_model_or_agent():
    """在子进程里验证:import 全应用后 get_model / get_agent 的缓存仍是空的。"""
    code = (
        "import os, sys;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "os.environ['LLM_MODEL_NAME']='m'; os.environ['LLM_API_KEY']='k';"
        "import app;"
        "from app.agents import llms, agent;"
        "assert llms.get_model.cache_info().currsize == 0, 'model built at import';"
        "assert agent.get_agent.cache_info().currsize == 0, 'agent built at import'"
    )
    # 在 tmp 目录运行并用 -I,避免当前目录的 .env 影响结果
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
    )
    assert result.returncode == 0, result.stderr


def test_get_model_is_cached(monkeypatch):
    from app.agents import llms

    llms.get_model.cache_clear()
    calls = []
    monkeypatch.setattr(llms, "build_model", lambda *_a: calls.append(1) or object())

    first = llms.get_model()
    second = llms.get_model()

    assert first is second
    assert calls == [1]
    llms.get_model.cache_clear()


def test_legacy_module_level_singletons_are_gone():
    llms = importlib.import_module("app.agents.llms")
    agent = importlib.import_module("app.agents.agent")
    assert not hasattr(llms, "model")
    assert not hasattr(agent, "deskwork_agent")
