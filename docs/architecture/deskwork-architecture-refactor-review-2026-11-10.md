# Deskwork 架构改造审查记录

审查日期：2026-11-10  
审查对象：[deskwork-architecture-refactor.md](deskwork-architecture-refactor.md) 及当时的工作区实现（包括未提交的修改和新增文件）。  
当前状态：R1～R9 均已修复并增加正式回归测试，修改尚未提交、未部署。后续系统复核确认 R10～R16 尚未解决，详见[系统复核报告](deskwork-architecture-deep-review-2026-11-10.md)，当前不能认定完成整体验收。第 1～4 节保留首轮审查证据，第 5 节记录首轮修复及验证，第 6 节记录第二轮发现，第 7 节记录第二轮修复及验证。

## 1. 结论与完成情况

主要架构改造已经落地。初次审查确认实现中仍有五项遗漏，本轮已逐项修复，验收记录见第 5 节。代码中已有持久 Run、统一会话占用、集中 finalizer、回退 journal、事件投影、共享预算和 v4 备份实现，不是只有设计文档。

| 改造方向 | 本次审查结论 |
|---|---|
| Run 持久化、请求幂等、终态事务 | 已有实现，相关现有测试通过 |
| 会话互斥、取消与 shutdown | 已有实现；审查发现的删除取消占用问题 R2 已修复 |
| SSE、快照与历史一致性 | 已有实现；序号解析、终态收敛和刷新竞态 R3～R5 已修复 |
| 文件与图历史回退 | staging/backup/journal、图选择及确认重答已有实现，相关恢复测试通过 |
| 文件并发与安全 | 已增加工作区锁、原子写及安全文件打开；普通下载的路径竞态 R1 已修复 |
| Agent 依赖与预算 | 显式构造、RunContext、调用计数及共享 usage callback 已有实现 |
| 迁移、备份与部署兼容 | 编号迁移、v4 备份、Run 查询及 nginx 路由已有实现；本次未重新执行真实模型和部署验收 |

“已有实现”和“现有测试通过”不意味着不存在其他缺陷。本次重点核查运行状态、取消、回退、文件隔离、备份及前端续流边界，不作穷尽性安全保证。

## 2. 问题清单

P1 表示应优先修复的安全或并发正确性问题；P2 表示需要修复的功能或一致性问题。

| 编号 | 级别 | 问题 | 状态 |
|---|---|---|---|
| R1 | P1 | 普通文件下载存在符号链接替换竞态 | 已修复，回归测试通过 |
| R2 | P1 | 删除协程取消后提前释放仍在收尾的会话 | 已修复，回归测试通过 |
| R3 | P2 | SSE 解析器丢弃序号，应用层去重没有实际生效 | 已修复，回归测试通过 |
| R4 | P2 | 打开会话遇到运行结束竞态，停留在部分回答 | 已修复，回归测试通过 |
| R5 | P2 | 较慢的空响应绕过续流终态查询 | 已修复，回归测试通过 |

以下各问题的原因和复现结果描述修复前的实现，行号也以初次审查时为准。

### R1：普通文件下载存在符号链接替换竞态

- 位置：[backend/app/api/routes/stream.py](../../backend/app/api/routes/stream.py)，`download_file`，审查时第 254～297 行，重点为第 290 行的 `FileResponse`。
- 触发条件：请求不带 `fingerprint`；路径检查通过后、响应实际打开文件之前，会话内可写文件或其父目录被替换为符号链接。
- 原因：路径解析、范围检查和 `is_file()` 与 `FileResponse` 的实际打开是分离操作。将已校验的路径传给响应对象，不能保证稍后打开的是同一个文件。
- 影响：可读取后端进程有权限读取的会话外文件，破坏文件隔离边界。此处是遗留未补齐的安全问题，不据此认定为本次重构新增漏洞。

隔离复现步骤：

1. 在临时会话的 `output/result.txt` 写入普通内容，在会话外的临时文件写入 `OUTSIDE_SESSION_SENTINEL`。
2. 调用下载路由，取得尚未发送的 `FileResponse`。
3. 将 `result.txt` 替换为指向会话外测试文件的符号链接。
4. 执行响应，实际响应体为 `OUTSIDE_SESSION_SENTINEL`。

复现只使用临时测试文件，没有读取真实敏感文件。

修复建议：从可信会话根目录逐段安全打开文件，响应使用同一个已打开的文件句柄或已读取的字节，避免再按路径重新打开。覆盖末段文件替换、父目录替换和正常下载行为。

### R2：删除协程取消后提前释放会话占用

- 位置：[backend/app/services/conversation_cleanup.py](../../backend/app/services/conversation_cleanup.py)，`delete_conversation_data`，审查时第 71～82 行；关联 [chat_operations.py](../../backend/app/services/chat_operations.py) 的 `cancel_chat`，第 380～384 行。
- 触发条件：删除正在运行的会话，旧 Run 已收到取消并开始异步收尾；等待删除的协程此时又被取消。
- 原因：`cancel_chat` 使用 `shield` 保护旧任务，但等待者被取消且旧任务未结束时会向外传播取消。外层删除函数的 `finally` 只判断 `recovery_required`，对仍为 `finalizing` 的 handle 直接执行 `sessions.release`。
- 影响：旧任务尚未停止或完成收尾，会话已允许新操作进入。新运行、文件操作可能与旧清理交叉执行，违反统一互斥的约束。

隔离复现使用可控的异步任务模拟 Run 收尾：删除流程取消旧任务，旧任务进入 `finalizing` 后阻塞；再取消删除协程。结果为：

```text
gate_retained: false
run_still_finalizing: true
```

修复建议：取消删除等待者时，继续排空其拥有的取消/清理任务，或把 handle 的释放责任归还旧 Run；确认旧任务停止后才能释放占用。无法确认停止时保留恢复阻塞状态。补充删除等待者取消、重复取消以及紧接着启动新操作的回归测试。

### R3：SSE 解析器丢弃序号

- 位置：[frontend/src/utils/sse.ts](../../frontend/src/utils/sse.ts)，`toPacket`，审查时第 9 行；关联 [turn.ts](../../frontend/src/utils/turn.ts) 的 `applyPacket`。
- 触发条件：后端发送带 `run_id/seq/schema_version` 的正常 SSE 事件，经前端解析器进入事件投影。
- 原因：`toPacket` 重建对象时只保留 `id/event/data`，丢弃新增字段。因此 `applyPacket` 中的 `packet.seq !== undefined` 分支不会处理真实传输来的序号。
- 影响：文档声明的应用层序号去重没有接通。发生同序号事件重复投递或回放时，正文和工具事件可能重复追加。Redis 游标仍有作用，本问题不表示每次正常重连都会重复内容。

复现使用真实格式的 SSE 文本，先调用 `parseSseChunk`，再将同一解析结果应用两次：

```text
输入：run_id=p, seq=1, event=ai.token, text=hello
解析结果：仅保留 id/event/data
重复应用后的正文：hellohello
```

现有 `turn.test.ts` 直接构造带 seq 的事件，绕过解析层，因此无法发现这个缺口。

修复建议：解析并校验保留这些字段；增加“原始 SSE 文本 → 解析器 → 事件投影”的集成测试，验证重复序号、快照后的旧序号及正常递增事件。

### R4：打开会话时可能停留在部分回答

- 位置：[frontend/src/stores/chat.ts](../../frontend/src/stores/chat.ts)，`openConversation`，审查时第 315～334 行。
- 触发条件：首次历史请求返回运行中的部分快照；随后查询线程状态之前，Run 正好完成。
- 原因：状态已结束时仅执行 `unmarkBackground`，既不接回事件流，也不重新读取最终消息。重构后运行中的 assistant 快照会落库，先前“运行中回答尚未落库”的假设不再成立。
- 影响：页面显示半截回答或过期状态，全局运行标记却已结束，且不再轮询补齐，需重新打开会话才能恢复。

隔离复现让第一次 `getMessages` 返回 `partial/running`，让 `getThreadStatus` 返回 `complete`，后续历史读取可返回完整答案。实际只读取了一次历史：

```text
history_reads: 1
message_status: running
global_running: false
visible_text: partial
```

修复建议：读取历史后发现运行已结束，应核对并获取相关 Run 的最终快照或重新读取历史，同时保留现有会话切换保护。增加运行结束发生在两个请求之间的测试。

### R5：较慢的空响应绕过续流终态查询

- 位置：[frontend/src/api/client.ts](../../frontend/src/api/client.ts)，`streamEvents`，审查时第 249 行附近。
- 触发条件：缺少 `stream.end`，而连续 SSE 响应没有事件；响应头到达后，响应体结束耗时不少于 200ms。例如服务重启后 Redis 流仍存在，但没有补齐结束事件，XREAD 等待后返回空结果。
- 原因：只有 `Date.now() - startedAt < 200` 的空响应才增加 `emptyRuns` 并触发 `getRun`。较慢的空响应不算失败，也不查询持久终态。
- 影响：客户端可能一直表现为生成中并持续重连，即使业务库已保存终态。若流最终过期，410 分支仍可能恢复；不能把这一兜底视为及时收敛。

模拟复现将空响应体延迟 250ms 关闭，设置 `maxFailures=2`，并准备可返回终态的 `/runs/p`：连续六次请求后只访问 `/stream-events`，没有访问 `/runs/p`；测试主动中止循环。

修复建议：按连续无事件次数或经过时间周期性查询具体 Run 的持久状态，不以响应速度决定是否收敛。保留对正常长工具运行的支持，补充阻塞后空响应、服务重启及结束通知丢失的测试。

## 3. 修复前审查执行的验证

| 检查 | 结果 |
|---|---|
| 后端全量测试 `uv run pytest -q` | 893 项通过，146.33 秒 |
| 前端全量测试 `npm test` | 196 项通过，19 个测试文件 |
| 前端 `npm run build` | TypeScript 检查及生产构建通过 |
| Ruff 检查与格式校验 | 通过，166 个文件格式符合要求 |
| `git diff --check` | 通过 |
| 针对 R1、R2 的临时隔离测试 | 安全/互斥预期断言失败，确认上述行为 |
| 针对 R4 的临时前端测试 | 最终答案预期断言失败，确认上述行为 |
| 针对 R3、R5 的临时脚本 | 确认字段丢失、重复拼接及不查询终态 |

后端测试初次在受限沙箱内停滞，随后在沙箱外重新执行完整测试并通过。不能将首次停滞归为项目测试失败。

本次没有重新运行真实模型、Docker/bwrap 部署验收或真实浏览器验收；原架构文档中的这部分结论仍属于此前的验证记录。

## 4. 复现材料与后续工作

临时复现材料保存在：

- `/tmp/deskwork_architecture_review_test.py`：R1、R2。可在 `backend/` 运行 `uv run pytest -c pyproject.toml -q -s /tmp/deskwork_architecture_review_test.py`。
- `/tmp/deskwork_sse_review.mjs`：R3、R5。使用当前工作区 TypeScript 依赖转译并调用实际前端实现；脚本包含当前工作区绝对路径。
- `/tmp/deskwork-chat-review.test.ts`：R4。执行时曾临时放入前端测试目录，结束后已移出；再次运行需要放回项目测试目录以使用项目配置和依赖。

临时目录可能被清理，以上材料不是已提交的回归测试。本记录已保留触发条件和观察结果，以便重新构建测试。

原审查建议先修复 R1、R2，再处理 R3～R5；现已完成对应代码修改及正式回归测试，见下节。旧临时脚本记录的是当时的复现过程，修复后的验收以正式测试为准。

初次审查未修改业务代码。后续修复已修改业务代码，但未部署变更。

## 5. 修复记录（2026-11-10）

### 实际修改

| 编号 | 实现 | 正式测试 |
|---|---|---|
| R1 | 新增 `WorkspaceFileResponse`，发送响应时安全打开一次文件，使用同一文件句柄取得大小并传输；禁用会让服务器重新按路径打开文件的 pathsend。`SessionPaths.open_host_file` 沿原已校验路径逐段打开目录，不使用再次解析出来的父目录替代路径 | `backend/tests/test_workspace_download.py`：打开前/后替换文件或父目录、正常下载、单段/后缀/多段 Range、416、HEAD、断连、发送失败和协程取消；相关路由、归属和响应头测试 |
| R2 | 整个删除操作作为所属任务执行，通过 `complete_before_cancelling` 等待完成后才传播请求取消并释放占用；确认停止失败时保留 `recovery_required` | `backend/tests/test_conversation_cleanup.py`：Run 收尾、数据库提交和资源清理三个阶段的重复取消；等待期间拒绝新操作；失败时保留恢复占用 |
| R3 | 解析器保留并校验 `run_id/seq/schema_version`，只接受正安全整数序号；聊天投影过滤其他 Run 的事件，接回流的消息也带 Run 标识 | `frontend/src/utils/turn.test.ts`：原始 SSE 文本解析后的重复事件、快照后旧事件与新事件、非法序号；`stores/chat.test.ts`：不同 Run 的事件隔离 |
| R4 | 打开历史后若查询到终态，补读最终历史；补读响应检查会话打开序号和运行序号，避免切换会话或启动新一轮之后被旧响应覆盖 | `frontend/src/stores/chat.test.ts`：部分回答、尚无 assistant 消息、补读时切换会话、新运行仍在执行或已经完成 |
| R5 | 所有连续空响应都计数并查询具体 Run，200ms 判断仅用于短响应的退避；网络失败和无字节超时也查询该 Run。已确认仍在运行的正常空流不消耗异常重试额度；410 后有限重试读取最终快照，无法收敛时明确报错。状态查询带取消信号和超时 | `frontend/src/api/client.test.ts`：阻塞空响应、长工具调用、网络失败、不同的最新 Run、410 收尾窗口、有限重试、取消和无字节超时 |

### 验证与维护边界

- 前端最终全量测试：**217 项通过，19 个测试文件**；TypeScript 检查和生产构建通过。
- 后端最终全量测试：**913 项通过，101.59 秒**，比修复前新增 20 项回归用例。全量复核时发现原响应头测试使用的路径替身不具备安全打开接口，已改用真实 `SessionPaths`，单项及最终全量复核均通过。
- Ruff 检查和格式校验通过（168 个文件），`git diff --check` 通过。
- 修复复核覆盖了权限边界、文件资源释放、删除任务归属、事件解析到投影的完整链路，以及历史读取与新运行交叉时的状态一致性。
- 文件响应复用锁定版本 Starlette 的 `_open_file` 扩展点，保留原有 Range 和响应头行为。升级 Starlette 时需运行这些传输回归测试，不能仅验证正常 GET。
- 安全文件句柄阻止路径替换后读取其他文件，不保证另一个进程原地改写同一文件 inode 时下载内容不变化；带摘要的引用下载仍沿用原有字节校验语义。
- 本轮没有调用真实模型，没有修改运行实例或执行部署。需部署新 backend/web 后，实际服务才会使用这些修复。

## 6. 第二轮审查（2026-11-10）

第二轮审查复核了上轮修改，并继续检查导入事务、线程取消、备份往返和内部模型预算。审查阶段没有修改业务代码，没有运行真实模型或部署。以下原因与复现描述修复前行为；随后完成的修复见第 7 节。目前没有证据将这些问题归因于上轮 R1～R5 的修复。

| 编号 | 级别 | 问题 | 状态 |
|---|---|---|---|
| R6 | P1 | 导入在业务库提交后被取消，清理逻辑仍删除新会话的文件和 Checkpoint | 已修复，回归测试通过 |
| R7 | P2 | 导入取消不等待解压线程停止，清理后仍可重建孤儿文件 | 已修复，回归测试通过 |
| R8 | P2 | 无 Checkpoint 会话导出空 checkpoint.json，自己的导入器拒绝该备份 | 已修复，回归测试通过 |
| R9 | P2 | 图片查看、文档检查吞掉预算中止异常，继续产生模型调用 | 已修复，回归测试通过 |

### R6：导入提交之后仍执行破坏性回滚

- 位置：[conversations.py](../../backend/app/api/routes/conversations.py) 的 `import_conversation`，审查时第 159～172 行，以及 `_discard_import` 第 188～198 行；业务提交位于 [repository.py](../../backend/app/db/repository.py) 第 295 行。
- 触发：业务 SQLite 已提交，但导入协程尚未正常返回时收到取消或发生异常。
- 原因：统一的 `except BaseException` 无条件清理 Checkpoint 和文件，不核对新会话是否已经持久提交，也不回滚已经提交的会话和消息记录。
- 影响：新导入的会话和消息仍显示在列表中，但附件和恢复上下文已经被删除，形成无法继续正常使用的半成品。此问题删除的是新导入副本，不是原会话或源备份。

使用真实临时业务库和 SQLite saver，在 `repository.import_conversation` 已成功执行后注入取消，观察结果：

```text
conversation_committed = true
files_exist = false
checkpoint_exists = false
```

修复方向：明确导入操作的持久提交凭据；提交后完成收尾，不能继续按未提交状态清理。取消与提交的窗口需要排空并核对结果，覆盖提交前、提交后及收尾失败。

### R7：导入取消后，解压线程继续写入

- 位置：[conversations.py](../../backend/app/api/routes/conversations.py) 第 155 行的 `asyncio.to_thread(session_import.extract_files, ...)`，以及第 170～176 行的清理。
- 触发：解压尚未结束，等待它的导入协程被取消。
- 原因：取消 `to_thread` 的等待者不会停止底层线程；路由却立即进入 `_discard_import` 删除工作区，并删除临时备份包。
- 影响：后台线程可能与删除并发，继续创建目录和文件，产生没有数据库归属的孤儿文件。导入也没有纳入会话操作登记，不能依靠现有 `shutdown_runs` 的会话 gate 来覆盖其生命周期。

复现使用真实 ZIP 解压：在第二个文件的目录创建前暂停线程，取消导入、等待其清理返回，再恢复线程，观察到：

```text
cleanup_before_writer_finished = true
orphan_files_recreated = 1
```

修复方向：将导入纳入可追踪操作，使用可排空的线程 IO，确认解压停止后再执行回滚；检查重复取消及 shutdown 交叉时的资源归属。

### R8：没有图历史的会话无法完成备份往返

- 位置：[checkpoint_transfer.py](../../backend/app/services/checkpoint_transfer.py) 的 `export_thread` 第 83～95 行及 `parse_payload` 第 206～208 行。
- 触发：会话尚未产生 Checkpoint，例如首次任务在进入图执行前失败，或导入了没有执行上下文的会话后再次导出。
- 原因：导出在零记录时仍返回非空 payload 对象，备份生成器据此包含 `checkpoint.json`；导入校验却明确拒绝 `checkpoints=[]`。
- 影响：由应用自己生成的备份，在同一应用内导入时失败，即使其中消息和文件本身有效。

复现使用实际 `export_session` 路由生成备份，然后调用 `inspect_archive`，得到：

```text
payload = {"payload_version": 1, "checkpoints": [], "writes": []}
ImportRejected: checkpoint 内容为空或格式不对
```

修复方向：明确区分“没有执行上下文”与“损坏的 Checkpoint”。无图历史时省略 Checkpoint 并保留消息和文件；同时考虑兼容已经导出的合法空 payload，不放宽不安全类型的校验。

### R9：内部模型预算中止被工具错误处理吞掉

- 位置：[image_tools.py](../../backend/app/tools/image_tools.py) 第 57～59 行；[review_tools.py](../../backend/app/tools/review_tools.py) 第 44～49 行。
- 触发：内部视觉模型调用结束，`RunUsageCallback` 检测到共享预算超限并抛出 `BudgetExceeded`。
- 原因：工具自身先捕获了 `Exception` 并转换为普通 `ToolMessage`，外层 `ToolGuardMiddleware` 对预算异常的透传无法生效。文档检查甚至会提示模型继续执行。
- 影响：图没有立即中止，主模型还会继续调用并消耗额外预算。后续共享 callback 仍可能停止运行，因此这不是无限预算绕过。

两种工具的直接调用均确认吞掉 `BudgetExceeded`。另使用真实 LangGraph 执行、SQLite Checkpointer、脚本主模型和脚本视觉模型验证：预算设为 1，第一次主模型调用后，视觉模型结束时已触发超限，但主模型仍被再次调用：

```text
main_model_calls = 2
vision_calls = 1
usage_calls = 3
run_status = error
```

修复方向：工具层在普通异常处理之前透传预算及图控制流异常；保留可恢复业务错误的明确反馈。增加通过真实图运行的内部模型预算回归测试，验证异常发生后不再调用主模型。

### 验证材料

- 现有相关测试重新运行：`test_workspace_download.py`、`test_conversation_cleanup.py`、`test_session_import.py`、`test_doc_review.py`、`test_view_image.py`，**78 项通过**。这也复核了上轮下载和删除取消的正式回归用例。
- 临时文件 `/tmp/deskwork_review_round2_test.py` 包含 6 个用例：R6、R7、R8 各 1 个，R9 的两个工具直接调用和真实图执行共 3 个。分批运行均触发了预期保护断言失败或实际导入拒绝，确认上述 4 项缺口。
- 可在 `backend/` 运行：`uv run pytest -c pyproject.toml -q -s /tmp/deskwork_review_round2_test.py`。临时目录可能被清理；这些复现尚未加入正式测试集。
- 本轮未重跑全量测试；上一轮的 913/217 全量通过记录属于第 5 节，不代表已覆盖这些新增场景。

## 7. 第二轮修复记录（2026-11-10）

### 实际修改

- **R6：以会话记录核对提交结果。** 数据库事务及 Checkpoint 导入通过 `complete_before_cancelling` 完成后，才允许取消传播。失败收尾复用 `conversation_cleanup` 的持久清理标记：确认会话不存在才删除资源；已提交则保留文件和 Checkpoint。数据库暂不可读时保留资源及标记，由启动恢复再次核对，避免将“结果未知”当作“未提交”。清理失败也保留逐项重试记录。
- **R7：完整登记并排空导入操作。** 从上传暂存开始，导入就占用 `SessionRegistry` 的 import 操作；服务关闭拒绝新导入，并等待已有导入释放占用。上传读写、备份检查、解压使用 `file_io`，取消等待者后先等线程结束；准备工作区、Checkpoint 导入、业务提交及失败收尾也等待所属任务完成。提交前取消会在当前 IO 结束后回滚；提交已经开始时则等待其结果，再按持久事实处理。重复取消不会越过这些边界。
- **R8：区分没有执行上下文与损坏数据。** 空线程导出不再附带空 `checkpoint.json`，只保留消息和文件。兼容旧版确实生成的完整空结构：整数 `payload_version=1`、`checkpoints=[]`、`writes=[]`，且不允许声称存在选中的 Checkpoint。含写入记录、未知版本、缺失/异常字段的空包仍拒绝；非空 Checkpoint 的原类型白名单保持有效。
- **R9：控制信号透传。** `view_image` 和 `review_document` 在普通异常处理前重新抛出 `BudgetExceeded` 与 `GraphBubbleUp`，让图立即中止或处理暂停；同时将两种工具的运行上下文显式标注为 `ToolRuntime[RunContext]`，消除真实图测试中的上下文序列化类型警告。

### 正式回归覆盖

| 文件 | 新增覆盖 |
|---|---|
| `backend/tests/test_session_import.py` | 提交前后异常与取消、重复取消提交等待者、解压未结束时取消与 shutdown、上传阶段 shutdown、新导入拒绝、数据库无法核对时保留资源并恢复、失败清理重试、Checkpoint 写入阶段取消、回滚期间再次取消、空线程真实导出导入往返、旧空包兼容及非法结构拒绝 |
| `backend/tests/test_tool_exception.py` | 两种工具的预算异常与图控制信号透传 |
| `backend/tests/test_run_recovery.py` | 两种内部视觉模型均通过真实 LangGraph 和 SQLite Checkpointer 验证：触发预算异常后主模型不再调用，usage 和错误终态正确保存，会话占用释放 |

针对导入、运行恢复和工具异常的最终定向测试 **84 项通过**，无上下文类型警告。最终完整验证结果：

- 后端全量测试 **940 项通过，107.15 秒**；比上一轮新增 27 项回归用例。
- 前端全量测试 **217 项通过，19 个测试文件**。
- TypeScript 检查、前端生产构建、Ruff 检查与格式校验（168 个文件）、`git diff --check` 均通过。

### 行为与维护边界

- 接受导入后，请求取消不等于数据库回滚：事务已提交时会保留成功导入的新会话，客户端可从会话列表重新打开。
- 失败收尾的持久标记复用已有启动清理流程；本轮没有新增一套导入调度器或数据库迁移。
- 预算仍在模型响应结束时检查；修复的是超限异常被吞后继续调用的问题，不改变已有的预算计数时点。
- 本轮未调用真实模型、未部署，也未改动现有真实会话数据。
