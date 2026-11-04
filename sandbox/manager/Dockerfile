FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# 不加 USER:挂载的 docker.sock 属 root:docker(gid 随宿主而变),非 root 用户无法稳定访问,
# 且能访问 docker.sock 本就等同 root。风险由 manager 的固定操作白名单和 compose 里不发布端口来限制。
# 只需要 docker CLI(通过挂载的 docker.sock 访问 daemon)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ]; then DOCKER_ARCH="aarch64"; else DOCKER_ARCH="x86_64"; fi && \
    curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-26.1.4.tgz" | tar -xz -C /usr/local/bin --strip-components=1 docker/docker && \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi==0.136.3 uvicorn==0.49.0 websockets==17.3

WORKDIR /app
COPY main.py .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
