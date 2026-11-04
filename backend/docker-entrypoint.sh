#!/bin/sh
# 后端以 app 用户(APP_UID)运行。旧版本以 root 运行时留下的数据属 root,降权后写不进去:
# 这里在启动前检查一遍,不可写就直接退出并给出迁移命令,而不是带病启动、跑到一半才报错。
# 只看前两层(会话目录及其子目录),不遍历 agent 写的整棵树。/app/sandbox_data 本身是宿主目录,不在这里查。
set -e

bad=""
for d in /app/data /app/sandbox_data/sessions /app/sandbox_data/pending_purge; do
    [ -e "$d" ] || continue
    hit=$(find "$d" -maxdepth 2 ! -type l ! -writable -print -quit 2>/dev/null || true)
    [ -n "$hit" ] && bad="$bad $hit"
done

if [ -n "$bad" ]; then
    echo "数据目录对 UID $(id -u) 不可写:$bad" >&2
    echo "多半是旧版本以 root 运行时留下的。在项目根目录执行一次迁移(见 docs/DEPLOY_NON_ROOT.md):" >&2
    echo "  docker compose run --rm --no-deps --user root --entrypoint chown backend -R -h $(id -u):$(id -g) /app/data /app/sandbox_data/sessions" >&2
    exit 1
fi

exec "$@"
