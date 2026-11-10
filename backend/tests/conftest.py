"""测试公共配置。

Settings 在 import 时实例化且要求 LLM_MODEL_NAME,这里先注入占位环境变量,
保证单元测试不依赖真实的 .env、LLM、Redis 或 Docker。
"""

import os

os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_PROVIDER", "openai")
# 显式覆盖会被真实 .env 污染的安全相关配置(环境变量优先于 dotenv 文件)
os.environ["API_KEYS"] = ""
os.environ["REDIS_PASSWORD"] = ""
os.environ["DATABASE_URL"] = ""
os.environ.pop("SANDBOX_MANAGER_URL", None)
# 测试不写日志文件,免得往开发用的 app.log 里灌测试产生的报错
os.environ["LOG_FILE"] = ""


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_session_registry(monkeypatch):
    from app.runtime.sessions import sessions

    monkeypatch.setattr(sessions, "_slots", {})
    monkeypatch.setattr(sessions, "closing", False)
