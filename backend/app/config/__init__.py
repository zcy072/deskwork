import os
from functools import cache
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings


@cache
def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings(BaseSettings):
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""

    # openai | anthropic | deepseek | ollama | vllm | azure
    LLM_PROVIDER: str = "openai"
    LLM_API_KEY: str = "EMPTY"
    LLM_BASE_URL: str = ""
    LLM_MODEL_NAME: str
    LLM_TEMPERATURE: float = 0.0
    # Anthropic 协议必须显式给出单次回复的输出上限;一次写很长的文件或脚本被截断时调大
    LLM_MAX_TOKENS: int = 8192
    # 主模型使用命名配置 LLM_<PROFILE>_*(留空 = 默认的 LLM_*);用于不改 .env 切换模型做对比
    LLM_PROFILE: str = ""
    # 主模型调用失败时按顺序切换的命名配置,逗号分隔,如 "deepseek,anthropic"
    LLM_FALLBACKS: str = ""
    # 产物自检用的多模态模型(命名配置,如 "anthropic");留空 = 不启用 review_document 工具
    REVIEW_LLM_PROFILE: str = ""
    # view_image 工具(同样用 REVIEW_LLM_PROFILE 的视觉模型):单张图片大小上限(MB)和每轮最多调用次数
    VIEW_IMAGE_MAX_MB: int = 5
    VIEW_IMAGE_MAX_PER_TURN: int = 5
    # Only used by the azure provider
    LLM_API_VERSION: str = ""

    # SQLite 等持久化数据的目录;留空则为 backend/data(容器内即 /app/data)
    DATA_DIR: str = ""
    # 留空使用 DATA_DIR 下的 SQLite;可改为 postgresql+asyncpg://...
    DATABASE_URL: str = ""

    # 逗号分隔的 API Key;留空表示关闭认证(仅限本地开发,启动时会告警)
    API_KEYS: str = ""
    # 逗号分隔的允许跨域来源;认证走 Authorization 头而非 Cookie,故不开启 credentials
    CORS_ORIGINS: str = "http://localhost:5173"
    # 每个调用方(API Key 或 IP)每分钟请求上限;send-message 单独更严
    RATE_LIMIT_PER_MINUTE: int = 120
    RATE_LIMIT_SEND_PER_MINUTE: int = 10
    # 后端只能经可信反代(如自带的 nginx)访问时开启:用 X-Real-IP 作为调用方 IP。
    # nginx 给所有人注入同一个 API Key,不开启则整个站点共用一个限流桶。直接暴露在公网时必须保持关闭
    TRUST_PROXY_HEADERS: bool = False

    # 单次 send-message 的上传限制(后端自己兜底,不依赖前面的 nginx;总量与 nginx 的 100m 对齐)
    UPLOAD_MAX_FILES: int = 20
    UPLOAD_MAX_FILE_MB: int = 50
    UPLOAD_MAX_TOTAL_MB: int = 100

    # 沙箱资源配额(按会话),0 表示不限制。
    # 单文件上限用 ulimit -f 在 shell 里强制;会话总存储在上传和每条命令执行前检查,
    # 超限后只放行 rm / ls / du / find 等清理命令。同时运行的会话数超限时新请求返回 429
    SESSION_MAX_STORAGE_MB: int = 1024
    SESSION_MAX_FILE_MB: int = 200
    MAX_CONCURRENT_RUNS: int = 4
    # 每个会话的 bash 常驻一个沙箱进程,并占 sandbox-manager 的一个 spawn 名额(MANAGER_MAX_SPAWN,默认 32)。
    # 空闲超过 SANDBOX_SHELL_IDLE_SEC 秒的 shell 会被关闭,存活数超过 SANDBOX_MAX_SHELLS 时关闭最久未用的;
    # 下次命令自动重启(cwd 回到 scratchpad)。0 表示不限制。SANDBOX_MAX_SHELLS 应小于 MANAGER_MAX_SPAWN
    SANDBOX_SHELL_IDLE_SEC: int = 900
    SANDBOX_MAX_SHELLS: int = 24

    # 回退(编辑重发/重新生成)时恢复沙箱文件:每轮开始前把 output 和 scratchpad 复制一份快照,
    # 只保留最近 SNAPSHOT_KEEP 轮(0 = 不做快照);两个目录合计超过 SNAPSHOT_MAX_MB 就跳过这一轮。
    # 没有快照的回退只回退对话,文件保持现状
    SNAPSHOT_KEEP: int = 3
    SNAPSHOT_MAX_MB: int = 300

    # 本轮结束后后台把新交付的 docx/pptx/xlsx 预先转成 PDF,点预览时直接命中缓存(会占用沙箱 CPU)
    PREVIEW_PREWARM: bool = True

    # 单轮对话预算,0 表示不限制。默认值取评测中观察到的最大消耗(19 次调用 / 81.7 万 token)
    # 并留出余量,目的是拦住死循环,不是限制正常的长任务。
    AGENT_MAX_LLM_CALLS: int = 60
    AGENT_MAX_TOTAL_TOKENS: int = 2_000_000
    AGENT_MAX_REPEATED_CALLS: int = 4
    # 子 Agent 单次委派的模型调用上限(0 = 不限制)。上面的预算只统计主 Agent 的流事件,
    # 子 Agent 内部的循环要靠这个拦住;正常的子任务只需要几次调用。
    SUBAGENT_MAX_LLM_CALLS: int = 15

    # 对话摘要(用主模型):估算 token 或消息数超过任一阈值就把早期消息压缩成摘要,保留最近 N 条原文
    SUMMARY_TRIGGER_TOKENS: int = 72000
    SUMMARY_TRIGGER_MESSAGES: int = 100
    SUMMARY_KEEP_MESSAGES: int = 20

    # 给读取外部内容的工具结果(bash / view_file / search_document / MCP)加 <untrusted_content> 边界标记
    UNTRUSTED_BOUNDARY: bool = True

    # 高风险 bash 命令(删除、覆盖上传文件等)执行前是否向用户确认
    CONFIRM_RISKY_COMMANDS: bool = True

    # 向量检索(search_document 的混合检索):OpenAI 兼容的 /embeddings 接口,例如硅基流动。
    # 留空 = 只用 BM25。启用后被检索文档的段落文本会发送到该服务。
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_MAX_CHUNKS: int = 2000  # 段数超过就不做向量(只用 BM25),避免大文档的嵌入成本失控

    # 多 Agent 分工:开启后注册只读的规划者 / 审查者子 Agent(默认关,尚无评测数据)
    MULTI_AGENT: bool = False

    # Anthropic 模型的 prompt caching(给系统提示和工具定义打 cache_control)。
    # 其他 provider 自动忽略;DeepSeek / OpenAI 的前缀缓存由服务端自动完成。
    PROMPT_CACHING: bool = True

    # 工具权限分级,JSON:工具名 -> allow | confirm | deny,留空 = 不额外限制。
    # confirm 每次调用前向用户确认;deny 直接拒绝(模型会收到拒绝说明)。例:
    # {"bash_tool": "confirm", "display_widget": "deny"}
    TOOL_POLICY: str = ""

    # 长期记忆:默认关闭(开启后注册 remember / recall / forget 三个工具)。
    # 记忆按 owner(API Key)隔离,同一 Key 的所有会话共享;写入默认需要用户确认,防止上传文档里的注入指令污染长期记忆。
    MEMORY_ENABLED: bool = False
    MEMORY_DEFAULT_TTL_DAYS: int = 180  # 0 = 默认永不过期
    MEMORY_MAX_ITEMS: int = 200
    MEMORY_CONFIRM_WRITES: bool = True

    # 外部 MCP 服务器(作为客户端接入),JSON:服务名 -> 连接配置,留空 = 不启用。例:
    # {"files": {"transport": "http", "url": "http://host:8000/mcp", "headers": {"Authorization": "Bearer x"}},
    #  "demo": {"transport": "stdio", "command": "python", "args": ["server.py"]}}
    # stdio 服务在后端容器内启动,命令由部署者配置,视为受信;远程服务返回的内容仍按不受信数据处理。
    MCP_SERVERS: str = ""
    MCP_CONNECT_TIMEOUT: int = 20
    # 外部工具调用前需要用户确认,除非它声明 readOnlyHint=true 且 openWorldHint=false;单个工具可用 TOOL_POLICY 设为 allow 放行
    MCP_CONFIRM_TOOLS: bool = True

    # 联网搜索 / 抓取(web_search / web_fetch),调用 DeepSeek 的 Search & Fetch API,按成功请求计费。
    # 留空 = 不启用;可以和 LLM_DEEPSEEK_API_KEY 用同一个 Key。搜索词和要抓的 URL 会发给 DeepSeek。
    WEB_TOOLS_API_KEY: str = ""
    WEB_TOOLS_BASE_URL: str = "https://api.deepseek.com"
    WEB_TOOLS_TIMEOUT: int = 60
    # 每轮用户消息内搜索 + 抓取的总次数上限
    WEB_MAX_CALLS_PER_TURN: int = 10
    # 单次抓取返回给模型的正文字符数,超出部分保存到 scratchpad
    WEB_FETCH_MAX_CHARS: int = 20000
    # 抓取既不是用户给的、也不在搜索结果里的 URL 前向用户确认(防止注入指令借 URL 外传数据)
    WEB_FETCH_CONFIRM_UNKNOWN: bool = True

    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 5001

    LANGSMITH_TRACING: bool = False
    LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "deskwork"

    @property
    def api_keys(self) -> list[str]:
        return [k.strip() for k in self.API_KEYS.split(",") if k.strip()]

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def data_dir(self) -> Path:
        path = (
            Path(self.DATA_DIR) if self.DATA_DIR else Path(__file__).resolve().parents[2] / "data"
        )
        return _ensure_dir(path)  # 按路径缓存:不必每次访问都 mkdir,DATA_DIR 变了也会重新创建

    @property
    def database_url(self) -> str:
        return self.DATABASE_URL or f"sqlite+aiosqlite:///{self.data_dir / 'deskwork.sqlite'}"

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def redis_url(self) -> str:
        auth = f":{quote(self.REDIS_PASSWORD, safe='')}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def redis_display_url(self) -> str:
        """用于日志,不含密码。"""
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # 从 backend/ 启动时 .env 在上一级;从仓库根或容器内启动时在当前目录(靠后的优先)
    model_config = {
        "env_file": ("../.env", ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()


if settings.LANGSMITH_TRACING:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_ENDPOINT"] = settings.LANGSMITH_ENDPOINT
    os.environ["LANGCHAIN_API_KEY"] = settings.LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"] = settings.LANGSMITH_PROJECT
