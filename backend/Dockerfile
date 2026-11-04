# Use an official lightweight Python image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1

# 运行用户的 UID 必须与沙箱镜像里 agent 的 UID 一致(sandbox/Dockerfile 的 UID):
# 后端要覆盖、删除 agent 在 output / scratchpad 里建的文件(agent 的 umask 是 0002)。
ARG APP_UID=1000
RUN groupadd --gid ${APP_UID} app \
    && useradd --uid ${APP_UID} --gid ${APP_UID} --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-install-project --no-dev

# 代码与依赖属 root,app 用户只读:即使进程被诱导去写文件,也改不了自己的代码
COPY . .

# 数据目录属 app:不挂载宿主目录直接运行镜像时也能写
RUN mkdir -p /app/data /app/sandbox_data && chown app:app /app/data /app/sandbox_data

# /app 不可写,日志放进数据卷;.env 里设置 LOG_FILE 可覆盖
ENV LOG_FILE=/app/data/app.log

ENV PATH="/app/.venv/bin:$PATH"

USER app

EXPOSE 5001

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "main.py"]
