#!/usr/bin/env bash
# 切换后端使用的模型(不改 .env 里的默认配置),用于多模型对比评测。
#   ./evals/switch_model.sh deepseek      # 主模型换成 LLM_DEEPSEEK_* 命名配置
#   ./evals/switch_model.sh               # 恢复 .env 里的默认模型
# 原理:docker-compose.yml 把 EVAL_LLM_PROFILE 映射为容器内的 LLM_PROFILE。
set -euo pipefail
cd "$(dirname "$0")/.."
EVAL_LLM_PROFILE="${1:-}" docker compose up -d backend >/dev/null
for _ in $(seq 1 30); do curl -sf localhost:5001/health >/dev/null && break; sleep 2; done
echo "backend profile=[${1:-default}] model=$(docker exec deskwork_backend sh -c 'echo ${LLM_PROFILE:-default}')"
