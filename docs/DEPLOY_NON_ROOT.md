# 后端以非 root 运行:旧数据迁移

后端镜像现在以 `app` 用户(UID 1000)运行。这个 UID 必须和沙箱镜像里 agent 的 UID 一致:后端要覆盖、删除 agent 在 output / scratchpad 里建的文件。改其中一个时,两边要一起改(`backend/Dockerfile` 的 `APP_UID`、`sandbox/Dockerfile` 的 `UID`)。

镜像里的代码和依赖属 root,`app` 用户只能读。日志写到 `/app/data/app.log`(compose 挂载的宿主目录 `backend/data`,`LOG_FILE` 可覆盖)。

## 新装

宿主用户是 UID 1000 时不需要额外操作:`backend/data` 随仓库带着(只有 `.gitkeep`),属当前用户。其他 UID 的宿主上先执行 `sudo chown 1000:1000 backend/data`。`sandbox_data/sessions` 由后端自己创建。

## 从以 root 运行的旧版本升级

旧版本留下的数据属 root:`backend/data` 里的数据库,以及 `./sandbox_data/sessions`。降权后的后端写不进去,入口脚本会在启动时发现并退出,日志里给出下面的命令。

在项目根目录执行一次(先停掉 backend):

```sh
docker compose stop backend
docker compose build backend
docker compose run --rm --no-deps --user root --entrypoint chown backend \
    -R -h 1000:1000 /app/data /app/sandbox_data/sessions
docker compose up -d
```

- `-h` 只改符号链接本身,不跟随链接(会话目录里有 agent 建的链接)。
- 如果 `./sandbox_data/pending_purge` 存在且属 root,把它也加到命令末尾。
- 迁移后宿主上的 `sandbox_data/sessions` 属 UID 1000。宿主用户正好是 1000 时可以直接读写。

要退回以 root 运行,在 compose 的 backend 下加 `user: root` 即可,数据不需要再改回去。
