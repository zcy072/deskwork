# Deskwork 系统复核与未通过的边界场景

日期：2026-11-10。审查对象为当前完整工作区，包含前两轮 R1～R9 的修复及原有未提交改造。编号延续[前两轮审查记录](deskwork-architecture-refactor-review-2026-11-10.md)。

**结论：当前不能认定架构改造已完成验收。** 本轮确认 7 类未解决问题，其中 3 类为 P1。12 个新增保护断言失败，另有 1 个子图流对照用例通过。现有后端 940 项及前端 217 项全量测试仍通过，说明现有覆盖与这些边界场景之间存在缺口。

本轮没有修改业务代码、没有部署。复现使用临时文件、临时 SQLite、脚本模型与受控远程进程替身，没有读取真实敏感文件或调用付费模型。

## 1. 本轮覆盖范围

本轮按调用链检查，不以某个辅助函数已使用锁、shield 或 O_NOFOLLOW 作为整条链路安全的充分证据。

| 边界 | 核查内容 | 结果 |
|---|---|---|
| 请求接受与幂等 | API → start_chat → Run 回执 → 前端重试与回执查询 | 确认 R15 |
| 准备、取消与收尾 | 未进入协程的 Run、已运行图、重复取消、清理失败、占用释放 | 确认 R12、R14 |
| 远程命令生命周期 | WebSocket 关闭、exit 回执、终止超时、manager 终止路径、命令计时分支 | 确认 R12；真实 Docker 故障组合尚未执行 |
| 文件读取与交付 | 下载、view_file、文本检索、编辑、交付、归档与路径替换 | 下载的 R1 修复仍通过；其他入口确认 R10、R11 |
| 文件回退协议 | staging、backup、journal、提交凭据与启动恢复 | 现有真实文件/子进程恢复用例通过；本轮未新增已确认缺陷 |
| 图回退与人工确认 | 普通回退、单个确认、并行问题、已完成兄弟任务、子图恢复 | 确认 R13；原有普通兄弟任务与子图确认测试通过 |
| 备份导入导出 | 空图兼容、提交后取消、解压线程、清理重试、旧格式与类型白名单 | R6～R8 正式回归随本轮全量通过；本轮未新增已确认缺陷 |
| Agent 与预算 | RunContext、共享 usage、模型异常、内部视觉工具、委派流 | R9 正式回归通过；新增子图流对照通过；确认异常脱敏遗漏 R16 |
| SSE 与历史 | 解析字段、序号、快照替换、丢终态、刷新/切换与新运行交叉 | R3～R5 正式回归通过；另确认发送重试边界 R15 |
| 归属与接口 | 会话/Run/文件/Memory 的 owner 校验、接口限流与认证 | 代码核查及现有 owner/security 测试通过；未新增已确认越权入口 |
| 外部与浏览器内容 | MCP 确认、陌生 URL 确认、工具结果边界、Markdown/Widget 及 nginx CSP | 静态核查与已有测试；未进行新的真实浏览器攻击验证 |
| 存储与部署约束 | 迁移、清理 marker、保留上限、单进程约束与资源关闭 | 保留上限和单进程属于既有边界；没有将其重新计作 bug |

“未新增已确认缺陷”仅描述本轮证据，不是无缺陷认证。两个文件竞态采用确定性故障注入扩大替换窗口；沙箱故障使用受控替身，未宣称已在实际 Docker 实例中复现残留进程。

## 2. 集中问题清单

| 编号 | 严重级别 | 根因 | 状态 |
|---|---|---|---|
| R10 | P1 | Agent 文件读取把可变父目录当成可信根，仍能越界读取 | 已修复（逐段 fd 打开，含 skills 根） |
| R11 | P1 | 交付临时文件关闭后按路径重开，可被替换为链接造成越界写入 | 已修复（临时文件 O_EXCL 句柄写入，发布前后核对 inode） |
| R12 | P1 | 沙箱停止状态、清理任务归属和 Run 放行之间没有可靠契约 | 已修复（manager 回执确认 stopped；未确认则会话停在 recovery_required） |
| R13 | P2 | 回退某个并行答案时清除同一暂停点的其他已回答任务 | 已修复（Run 记录所答 interrupt，回退只清目标及之后的答案） |
| R14 | P2 | Run 尚未进入执行协程时，持久取消收尾仍依附于请求等待者 | 已修复（等待与收尾放进独立任务排空） |
| R15 | P2 | 首次发送结果未知后，将后续重试的拒绝误当作首次请求未被接受 | 已修复（重试被拒按原 request_id 查回执，查不到保持未知） |
| R16 | P2 | 部分工具自身先把未知异常文本转成反馈，绕过统一脱敏 | 已修复（只有 ToolFeedbackError/路径错误原文透出） |

### R10：文件读取的可信根没有传递到最底层

位置：`backend/app/sandbox/bash_handler.py::read_container_file`，审查时约第 314 行；`backend/app/sandbox/host_files.py::read_regular_file` 第 43 行。

`read_container_file` 只传入解析后的目标路径，没有传可信根。`read_regular_file` 因而以 `host.parent` 调用 `parent_fd`。当目标是 `scratchpad/a/b/data.txt` 时，这相当于直接打开 `scratchpad/a/b`；`O_NOFOLLOW` 只保护这次 open 的末段 `b`，不能防止中间的 `a` 被替换成链接。

复现先完成路径解析，再在底层 open 前将 `a` 替换为指向会话外测试目录的链接，最终读取结果是 `OUTSIDE_SESSION_SENTINEL`。复现调用实际 `BashSession.read_container_file`，没有使用真实敏感文件。

此读取链用于 Agent 文件工具和文本检索。view_file 也未持有工作区锁，不能依靠 shell 的串行执行消除窗口。即使补锁，也不能把可写目录当作可信根。

修复要求：沿会话根或明确允许的全局 skills 根逐段打开目录，读取使用已验证句柄；不得由目标路径的可变父目录推导信任边界。回归覆盖文件、父目录和更上级目录替换，以及 view_file、文本检索和 skills 读取入口。

### R11：交付的临时文件仍会被不安全地重新打开

位置：`backend/app/services/file_versions.py::copy_file_atomic` 第 67～72 行；调用方包括 `tools/executor.py::_copy_local`。

`NamedTemporaryFile` 创建临时文件后立刻退出上下文，随后 `shutil.copy2(source, temporary)` 按文件名重新打开它。目标为 output 时，该临时文件位于 Agent 可写目录。创建与重新打开之间，它可被替换为符号链接。

复现只改变两次操作之间的临时文件：将它换为指向会话外测试文件的链接后，调用原始 `copy2`。结果会话外文件从 `must be preserved` 变为 `delivery bytes`，最终 output 还是一个符号链接。随机文件名与最终 rename 不能保护重新打开阶段。

工作区锁只协调本进程参与锁的操作，不能作为对沙箱后台进程的安全保证。现有 bwrap 设计允许进程留在同一命名空间，命令校验也明确是辅助检查。

修复要求：源与临时目标均使用可信根下安全打开的文件描述符；复制过程中保持临时描述符，不按可变文件名重新打开。发布阶段也要核对目录/文件身份。统一检查交付与历史版本复制，覆盖源文件替换、临时文件替换和父目录替换。

### R12：沙箱“已停止”的事实没有贯通到 Run finalizer

涉及位置：

- `backend/app/sandbox/manager_client.py::RemoteProcess.terminate` 第 96～107 行及 `_receive_loop`。
- `backend/app/sandbox/bash_handler.py::_cleanup_on_cancel` 第 209 行、`_stop` 第 565 行附近和 `_read_until_sentinel`。
- `backend/app/tools/file_tools.py::bash_tool` 的宽泛异常捕获。
- `sandbox/manager/main.py::kill_in_container` 与 `/spawn` 结束回执。

本轮复现了同一生命周期契约下的四个失败场景：

| 场景 | 观察结果 |
|---|---|
| 终止请求没有收到 exit 回执 | `terminate` 吞掉等待超时，正常返回；传输结束被记作 returncode=-1，不能证明远程命名空间已消失 |
| 首次取消正在等待 `_stop`，此时再次取消 | `asyncio.shield` 保护了后台 stop，但等待者提前退出并释放工作区锁；受控进程仍处于运行态 |
| manager 的 terminate 抛错 | `_stop` 仍清空 `_process`，工具将错误转为普通反馈；真实图取消后会话 gate 已释放，受控进程仍未停止，没有保留 recovery_required |
| deadline 在 while 条件之后、计算 remaining 之前跨过 | `remaining <= 0` 走 break，跳过 while-else 的超时清理，返回 `('', False, None)`，被视为非超时结果 |

manager 端静态核查还发现：`kill_in_container` 不检查 `pkill` 的返回码，随后结束的是 docker exec 客户端。因此修复不能只要求“收到 WebSocket 文本回执”，还必须保证回执对应容器内进程的实际停止。

修复要求：为停止成功、停止失败、结果未知建立明确的资源状态；等待者取消必须排空其拥有的清理任务；清理失败不得被普通工具错误包装吞掉；任何无法确认停止的情况都要阻止新运行和文件变更。超时、用户取消、重置、空闲回收、删除和 shutdown 应共用这套契约。验收需补真实 manager/Docker 的断连、终止失败及超时场景。

### R13：编辑第二个并行答案会丢掉第一个答案

位置：`backend/app/agents/recovery.py::fork_answered_interrupt` 第 22 行及第 58～63 行。

当前逻辑把所有带 interrupts 的 task 都放进清理集合，未区分已经回答且应保留的兄弟任务；在选中的 head 上又统一移除 `__resume__` 和这些任务的结果。并行问题的多个答案可能共享同一个物理 Checkpoint，单凭 checkpoint_id 不足以表示“第二次回答之前”。

真实 SQLite 图复现：

1. 同时询问颜色和格式。
2. 用户依次回答“红”“Word”，图完成。
3. 编辑第二个答案为“PDF”。
4. 结果只有 `c2 = User answered: PDF`，第一个答案“红”消失，Run 重新 waiting 并再次询问颜色；展示历史却保留“红”。

修复要求：记录逻辑回答边界及有效 interrupt 身份，按目标回答选择性清理 pending writes；保留目标之前已经完成的确认。覆盖两个/多个并行问题、编辑第一个/中间/最后一个答案、嵌套确认及已完成副作用任务。

### R14：未进入协程的 Run 仍可能卡在 preparing

位置：`backend/app/services/chat_operations.py::cancel_chat` 第 385～398 行。

`handle.started == False` 的特殊收尾直接在取消请求协程中读写数据库，没有像正常 `_run_agent` finalizer 那样独立执行并排空。取消请求自身在持久保存期间被取消，会跳过保存、结束通知和 gate 释放。

复现结果：Run 仍为 `preparing`，worker 已结束且从未进入协程，会话 gate 仍被持有。它不会自行收敛，需再次取消或服务恢复。

修复要求：把“已接受但未进入 worker”的收尾纳入同一套有明确归属的 finalizer，验证重复取消、等待者断开、数据库提交窗口、删除和 shutdown 交叉。

### R15：发送重试把结果未知降级成了确定拒绝

位置：`frontend/src/api/client.ts::sendMessage` 第 145～147 行，以及 `frontend/src/stores/chat.ts::send` 的 ApiError 分支。

触发顺序为：第一次发送的响应丢失，原请求仍在准备；同一 request_id 重试收到会话忙 409；原请求随后被接受。当前 client 将重试的 ApiError 直接抛出，store 又据此认定请求未被接受，完全绕过持久回执查询。

使用实际 client/store、只替换 fetch 的复现结果：发送返回 `false`，只请求两次 send，没有访问 `/requests/{request_id}`，本地消息恢复为空；测试中的原请求回执已经可以读取。用户再次发送会生成新的 request_id，可能重复执行。

修复要求：首次发送出现不确定结果后，不能用后续请求的 409/限流等响应证明原请求未被接受。保留原 request_id，依据它的持久回执收敛；原请求仍在准备而暂时查不到回执时也需要明确的不确定状态，不能靠“最新 Run”猜测。

### R16：未知异常脱敏没有覆盖工具内部捕获

位置：`backend/app/tools/image_tools.py` 第 61 行附近、`review_tools.py` 第 48 行附近、`search_tools.py` 第 55～57 行。

三种工具自身都将未知异常的 `str(exc)` 写入 ToolMessage；外层 ToolGuard 看见的是正常返回值，无法再执行脱敏。这与架构文档“未知异常只返回安全提示和 trace id”的描述不一致。

对三种工具分别注入包含 `PRIVATE_INTERNAL_SENTINEL` 的 RuntimeError，三次都在返回给模型的反馈里得到原始标记。实际暴露内容取决于底层异常，可能包含内部路径、连接信息或供应商错误内容；本轮未使用真实凭据验证。

修复要求：区分允许公开的业务异常与未知异常。未知异常仅留服务端日志，对模型/前端统一返回安全提示和 trace id；同时保留 R9 已修复的预算与图控制流透传。

## 3. 复现与验证证据

| 检查 | 本轮结果 |
|---|---|
| 后端全量测试 | 940 passed，102.55 秒 |
| 前端全量测试 | 217 passed，19 个文件 |
| TypeScript 与生产构建 | 通过 |
| 本轮后端隔离复现 | 11 failed、1 passed，5.08 秒；失败均为保护断言，不是测试环境异常 |
| 本轮前端发送重试复现 | 1 failed，实际 client/store 被错误判为未接受 |
| 子 Agent 流正向对照 | 通过，仅显示 PUBLIC_FINAL_ANSWER，未混入子模型文本 |

后端负向复现有两条 BashToolInput 的 RunContext 类型警告，未将其单独计作功能缺陷。时钟复现最初曾影响 asyncio 的全局时钟，已改为只替换 bash_handler 的时钟对象；上表记录的是修正后集中重跑的结果。

材料已保留到仓库，避免仅依赖 /tmp：

- [后端复现脚本](repros/deep-review-backend-2026-11-10.py)。在 backend 目录执行：`uv run pytest -c pyproject.toml -q -s ../docs/architecture/repros/deep-review-backend-2026-11-10.py`。
- [后端集中复现日志](repros/deep-review-backend-2026-11-10.log)。全部使用测试标记和临时目录，无真实凭据。
- [前端复现用例](repros/deep-review-frontend-2026-11-10.test.ts)。运行时放入 `frontend/src/architecture-audit.temp.test.ts`，执行 `npm test -- src/architecture-audit.temp.test.ts`；本轮运行后已移出正式测试目录。

这些是审查证据，负向断言当前应失败，不属于正式测试集的“通过项”。修复时应将对应场景纳入正式回归，并增加相邻组合，不能只让某一个复现转绿。

## 4. 为何前几轮没有收敛

前两轮核对了具体问题及其邻近路径，但没有覆盖所有跨层组合，特别是：

- 正常图 finalizer 有保护，不代表未进入协程的取消路径也有保护。
- Python task 已结束，不代表远程命名空间已停止；普通工具异常包装可能掩盖资源状态错误。
- 辅助函数支持可信根目录，不代表调用方传入了可信根。
- 临时文件创建安全，不代表关闭后重新按名字打开仍安全。
- 保留普通已完成工具，不代表保留同一个 Checkpoint 上此前已回答的 interrupt。
- 每次重试使用同一 ID，不代表前端在所有响应组合下都会核对该 ID。

后续应按上面的边界契约修复和验收，优先 R10～R12，再处理逻辑回退、请求状态和异常边界。当前结论是“已列明并复现这些未解决问题”，不是“除此之外保证没有 bug”。

## 修复记录（2026-11-10）

R10～R16 已修复，正式回归测试见：

- R10/R11：`test_file_read_isolation.py`、`test_present_symlink.py`、`test_file_versions.py`
- R12/R14：`test_stop_confirmation.py`、`test_sandbox_limits.py`、`test_sandbox_manager.py`
- R13：`test_rewind_parallel_answers.py`（编辑第 1/2/3 个并行答案）
- R15：`frontend/src/api/client.test.ts`、`frontend/src/stores/chat.test.ts`
- R16：`test_tool_error_feedback.py`

已知限制：
- R12 未在真实 Docker 实例里复现残留进程，验证基于受控替身。停止未确认时会话一直停在 recovery_required，要重启服务才能恢复。
- R13 只对新记录了 `answered_interrupt` 的 Run 生效。修复前的旧 Run 回退时，仍会清空同一暂停点的全部答案。
- 嵌套子图中的并行确认没有单独补测试。
