import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '@/api/client'
import type { ChatMessage, ConversationAssets, ConversationSummary, ConversationUsage, FileRef, MessageRow } from '@/types'
import { toRelativePath } from '@/utils/files'
import { latestTodos } from '@/utils/steps'
import { applyPacket, newTurn } from '@/utils/turn'
import { announceRun } from '@/utils/attention'
import { errMsg } from '@/utils/errors'
import { NEW_CONVERSATION, clearDraft, loadDraft, loadDraftFiles, saveDraft } from '@/utils/draftStorage'

/** 打开会话和向上翻页时每次取的消息条数 */
export const MESSAGE_PAGE_SIZE = 40
// 重试被拒绝后等原请求写下回执:最多约 3 秒
const RECEIPT_POLLS = 6
const RECEIPT_POLL_MS = 500

function newId(): string {
  return crypto.randomUUID()
}

interface ComposerDraft {
  text: string
  files: File[]
  /** 刷新前选好但没发出去的附件名;内容已丢失,提示用户重新选择 */
  lostFiles: string[]
}

function toChatMessage(m: MessageRow): ChatMessage {
  return {
    id: String(m.id),
    role: m.role,
    parts: m.parts,
    runId: m.prompt_id,
    lastEventSeq: m.last_event_seq,
    errorText: m.error_text ?? undefined,
    status: m.status,
    dbId: m.role === 'user' ? m.id : undefined,
    rewindable: m.rewindable,
    attachments: m.attachments?.map((f) => f.file_name),
    attachmentFiles: m.attachments,
  }
}

export const useChatStore = defineStore('chat', () => {
  const sessionId = ref(newId())
  const conversations = ref<ConversationSummary[]>([])
  const messages = ref<ChatMessage[]>([])
  const running = ref(false)
  const cancelling = ref(false)
  const error = ref<string | null>(null)
  /** 正在加载某个历史会话的消息(切换会话时显示骨架/转圈) */
  const loadingConversation = ref(false)
  /** 服务端是否还有比当前最早一条更早的消息 */
  const hasEarlier = ref(false)
  /** 仅在 hasEarlier 时使用:补全未加载消息里的文件与计划 */
  const summary = ref<ConversationAssets | null>(null)
  /** 磁盘上已不存在的文件(容器路径);面板据此隐藏,聊天里的历史卡片保持原样 */
  const removedPaths = ref<Set<string>>(new Set())
  const loadingEarlier = ref(false)
  /** 会话列表加载失败的原因;与 error 分开,否则 401/密钥错误会显示成“还没有对话” */
  const conversationsError = ref<string | null>(null)
  const usage = ref<ConversationUsage | null>(null)
  // 附件保留 File 对象,只在本页内存中;草稿文字和附件名另存 localStorage(见 draftStorage),刷新后恢复文字、提示重选附件。
  const drafts = ref(new Map<string, ComposerDraft>())
  /** 打开过的服务端会话;从搜索打开的旧会话可能不在已加载的列表页里 */
  const openedIds = new Set<string>()
  /** 已有的会话用自己的 id;还没发出第一条消息的新对话共用一个槽位。 */
  function draftSlot(id: string): string {
    return openedIds.has(id) || conversations.value.some((c) => c.id === id) ? id : NEW_CONVERSATION
  }
  let newSlotRestored = false
  function draftFor(id = sessionId.value): ComposerDraft {
    if (!drafts.value.has(id)) {
      const slot = draftSlot(id)
      // 新对话的共享槽位只在页面加载后的第一个新对话里恢复,之后新开的对话从空白开始
      const restore = slot !== NEW_CONVERSATION || !newSlotRestored
      if (slot === NEW_CONVERSATION) newSlotRestored = true
      drafts.value.set(id, {
        text: restore ? loadDraft(slot) : '',
        files: [],
        lostFiles: restore ? loadDraftFiles(slot) : [],
      })
    }
    return drafts.value.get(id)!
  }
  function writeDraft(slot: string, id: string) {
    const d = drafts.value.get(id)
    // 待重选的附件名也写回,直到用户关掉提示或发送;否则没点开就再刷新一次会丢掉提示
    saveDraft(slot, d?.text ?? '', [...(d?.lostFiles ?? []), ...(d?.files.map((f) => f.name) ?? [])])
  }
  /** 把某个会话当前的草稿文字写入本地存储。 */
  function persistDraft(id = sessionId.value) {
    const slot = draftSlot(id)
    if (slot === NEW_CONVERSATION && id !== sessionId.value) return
    pendingDrafts.delete(slot)
    writeDraft(slot, id)
  }
  // 打字时每次都重写整份草稿存储(最多约 1MB)太重,攒一下再写;槽位在排队时确定,写入时取最新文字
  const pendingDrafts = new Map<string, string>()
  let draftTimer: ReturnType<typeof setTimeout> | null = null
  function queueDraftSave(id = sessionId.value) {
    const slot = draftSlot(id)
    if (slot === NEW_CONVERSATION && id !== sessionId.value) return
    pendingDrafts.set(slot, id)
    if (!draftTimer) draftTimer = setTimeout(flushDrafts, 300)
  }
  /** 立即写入排队中的草稿;页面隐藏或关闭前调用,避免丢掉最后几个字。 */
  function flushDrafts() {
    if (draftTimer) clearTimeout(draftTimer)
    draftTimer = null
    for (const [slot, id] of pendingDrafts) writeDraft(slot, id)
    pendingDrafts.clear()
  }
  let abort: AbortController | null = null
  // 历史读取期间即使新一轮已经开始并结束,旧响应也不能覆盖它。
  let runSeq = 0
  let stopTimer: ReturnType<typeof setTimeout> | null = null
  /** 已切走、但服务端仍在生成的会话 id;当前会话的运行状态看 running */
  const backgroundIds = ref<string[]>([])
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  /** 编辑重发被拒绝时的文字,由对应消息取回继续编辑 */
  const rejectedEdit = ref<{ id: string; text: string } | null>(null)
  /** 删除请求还没返回的会话 id */
  const deleting = new Set<string>()
  const POLL_MS = 4000

  /** 侧边栏用:该会话是否还在生成(含当前会话) */
  function isGenerating(id: string): boolean {
    return (id === sessionId.value && running.value) || backgroundIds.value.includes(id)
  }

  const isRemoved = (f: FileRef) => removedPaths.value.has(f.container_path ?? f.file_name)

  /** 会话内所有 files.presented 汇总,供右侧文件面板使用 */
  const sharedFiles = computed<FileRef[]>(() => {
    const seen = new Map<string, FileRef>()
    if (hasEarlier.value) for (const f of summary.value?.delivered ?? []) seen.set(f.container_path ?? f.file_name, f)
    for (const m of messages.value)
      for (const p of m.parts)
        if (p.type === 'files') for (const f of p.files) seen.set(f.container_path ?? f.file_name, f)
    return [...seen.values()].filter((f) => !isRemoved(f))
  })

  /** 用户在各条消息里上传的文件(按沙箱路径去重),供右侧文件面板使用 */
  const uploadedFiles = computed<FileRef[]>(() => {
    const seen = new Map<string, FileRef>()
    if (hasEarlier.value) for (const f of summary.value?.uploaded ?? []) seen.set(f.container_path ?? f.file_name, f)
    for (const m of messages.value)
      for (const f of m.attachmentFiles ?? []) seen.set(f.container_path ?? f.file_name, f)
    return [...seen.values()].filter((f) => !isRemoved(f))
  })

  /** 当前会话最新一次 write_todos 的计划 */
  const todos = computed(() => {
    for (let i = messages.value.length - 1; i >= 0; i--) {
      const t = latestTodos(messages.value[i].parts)
      if (t.length) return t
    }
    return hasEarlier.value ? (summary.value?.todos ?? []) : []
  })

  /** 会话列表分页加载;还有更多时侧栏显示"加载更多" */
  const CONV_PAGE = 100
  const conversationsHasMore = ref(false)
  const loadingMoreConversations = ref(false)
  let listSeq = 0

  /** 重新取列表:至少覆盖已加载的条数,否则刷新后用户翻出来的旧会话会被丢掉。 */
  async function refreshConversations() {
    const mine = ++listSeq
    try {
      const want = Math.max(CONV_PAGE, conversations.value.length)
      const all: ConversationSummary[] = []
      let more = false
      while (all.length < want) {
        const page = await api.listConversations(CONV_PAGE, all.length)
        all.push(...page)
        more = page.length === CONV_PAGE
        if (!more) break
      }
      if (mine !== listSeq) return // 期间有更新的刷新或翻页
      conversations.value = all
      conversationsHasMore.value = more
      conversationsError.value = null
      trackServerState()
    } catch (e) {
      if (mine === listSeq) conversationsError.value = errMsg(e)
    }
  }

  /**
   * 列表里的服务端状态决定本地追踪:刷新页面后仍在生成的会话要接着轮询,
   * 当前正在看的会话不该留着未读。
   */
  function trackServerState() {
    for (const c of conversations.value) {
      if (c.status === 'running' && c.id !== sessionId.value && !backgroundIds.value.includes(c.id)) {
        backgroundIds.value.push(c.id)
      }
    }
    schedulePoll()
    const current = conversations.value.find((c) => c.id === sessionId.value)
    if (current?.unread && !running.value) markRead(current.id)
  }

  /** 清除未读:先在本地去掉,服务端失败不打扰用户(下次刷新列表会再带回来)。 */
  function markRead(id: string) {
    const c = conversations.value.find((x) => x.id === id)
    if (c) c.unread = false
    void api.markConversationRead(id).catch(() => {})
  }

  async function loadMoreConversations() {
    if (loadingMoreConversations.value || !conversationsHasMore.value) return
    loadingMoreConversations.value = true
    const mine = ++listSeq
    try {
      const page = await api.listConversations(CONV_PAGE, conversations.value.length)
      if (mine !== listSeq) return
      // 翻页期间有新会话插到前面时 offset 会错位,按 id 去重
      const seen = new Set(conversations.value.map((c) => c.id))
      conversations.value = [...conversations.value, ...page.filter((c) => !seen.has(c.id))]
      conversationsHasMore.value = page.length === CONV_PAGE
      conversationsError.value = null
    } catch (e) {
      if (mine === listSeq) conversationsError.value = errMsg(e)
    } finally {
      loadingMoreConversations.value = false
    }
  }

  /** 会话摘要(文件、待办)与用量只是辅助信息,取不到(例如新会话还没落库)就当没有,不打扰对话。 */
  async function refreshUsage() {
    const id = sessionId.value
    const [assets, u] = await Promise.allSettled([api.getConversationAssets(id), api.getUsage(id)])
    if (id !== sessionId.value) return
    summary.value = assets.status === 'fulfilled' ? assets.value : null
    usage.value = u.status === 'fulfilled' ? u.value : null
  }

  /** 切走正在生成的会话:只中止本地事件流,服务端继续生成;之后轮询它何时结束。 */
  function detachRunning() {
    if (!running.value) return
    const id = sessionId.value
    abortStream()
    if (!backgroundIds.value.includes(id)) backgroundIds.value.push(id)
    schedulePoll()
  }

  function unmarkBackground(id: string) {
    backgroundIds.value = backgroundIds.value.filter((x) => x !== id)
  }

  function schedulePoll() {
    if (pollTimer || backgroundIds.value.length === 0) return
    pollTimer = setTimeout(() => void pollBackground(), POLL_MS)
  }

  /** 后台会话生成结束(或不存在了)就从标记里去掉,并刷新列表(标题、时间、消息数会变)。 */
  async function pollBackground() {
    pollTimer = null
    let finished = false
    const finishedIds: string[] = []
    await Promise.all(
      backgroundIds.value.map(async (id) => {
        let st: Awaited<ReturnType<typeof api.getThreadStatus>>
        try {
          st = await api.getThreadStatus(id)
        } catch {
          return // 暂时查不到就保留标记,下一轮再查
        }
        if (st?.status !== 'running') {
          unmarkBackground(id)
          finished = true
          finishedIds.push(id)
        }
      }),
    )
    if (finished) {
      await refreshConversations()
      for (const id of finishedIds) announce(id)
      // 打开会话时没查到状态(网络抖动),它仍留在后台标记里;结束后重新载入,补上最终回答
      const sid = sessionId.value
      if (finishedIds.includes(sid) && !running.value) {
        await reloadMessages(sid).catch(() => {})
        void refreshUsage().then(syncMissingFiles)
      }
    }
    schedulePoll()
  }

  /** 页面在后台时,用标题/系统通知告诉用户这个会话有了结果或在等回答。 */
  function announce(id: string) {
    const c = conversations.value.find((x) => x.id === id)
    if (c) announceRun(c.status, c.title)
  }

  function newConversation() {
    detachRunning()
    openSeq++ // 作废还没返回的 openConversation
    loadingConversation.value = false
    sessionId.value = newId()
    messages.value = []
    hasEarlier.value = false
    summary.value = null
    removedPaths.value = new Set()
    usage.value = null
    error.value = null
  }

  let openSeq = 0

  async function openConversation(id: string) {
    if (id === sessionId.value) return
    error.value = null
    const mine = ++openSeq
    loadingConversation.value = true
    try {
      const rows = await api.getMessages(id, MESSAGE_PAGE_SIZE)
      if (mine !== openSeq) return // 已点了别的会话
      openedIds.add(id)
      // 同一时刻切换:先中止本地流再改 sessionId,旧流的收尾看到的已是新会话,不会动当前消息
      detachRunning()
      sessionId.value = id
      removedPaths.value = new Set()
      summary.value = null
      usage.value = null
      messages.value = rows.map(toChatMessage)
      hasEarlier.value = rows.length >= MESSAGE_PAGE_SIZE
      markRead(id)
      loadingConversation.value = false // 接回事件流可能持续很久,转圈只覆盖加载历史这一段
      // 文件是否已删除要对照会话摘要里的文件,摘要到了再查
      void refreshUsage().then(syncMissingFiles)
      // 历史可能是运行中的快照;状态查询时若已结束,还要补读最终历史。
      const active = await api.getThreadStatus(id).catch(() => null)
      if (mine !== openSeq || running.value) return
      if (active?.status === 'running' && active.prompt_id) await attach(active.prompt_id)
      else if (active) {
        await reloadMessages(id)
        if (mine !== openSeq || running.value) return
        unmarkBackground(id)
        void refreshUsage().then(syncMissingFiles)
      }
    } catch (e) {
      if (mine === openSeq) error.value = errMsg(e)
    } finally {
      if (mine === openSeq) loadingConversation.value = false
    }
  }

  /** 读取一次运行的事件流并写入 live;事件从头回放,所以也能接回刷新页面前开始的生成。 */
  async function consume(sid: string, promptId: string, live: ChatMessage, signal: AbortSignal) {
    const turn = newTurn()
    turn.parts = live.parts
    for await (const packet of api.streamEvents(sid, promptId, signal)) {
      if (packet.run_id && packet.run_id !== promptId) continue
      const input = packet.event === 'user.message' ? packet.data :
        packet.event === 'run.snapshot' ? packet.data.input : null
      if (input && sid === sessionId.value && !messages.value.some((m) => m.role === 'user' && m.runId === promptId)) {
        const at = messages.value.indexOf(live)
        if (at >= 0) messages.value.splice(at, 0, {
          id: newId(), role: 'user', runId: promptId,
          parts: input.text ? [{ type: 'text', text: input.text }] : [],
          attachmentFiles: input.files ?? [],
        })
      }
      applyPacket(turn, packet)
      live.parts = turn.parts
      if (turn.error && sid === sessionId.value) error.value = turn.error
    }
    live.status = turn.error ? 'error' : turn.status ?? (cancelling.value || signal.aborted ? 'cancelled' : 'completed')
    live.errorText = turn.error ?? undefined
    live.warningText = turn.warning ?? undefined
  }

  /** 收尾:只有仍是当前这一轮、且会话没被切走时才动全局状态,避免串到别的会话 */
  async function finish(sid: string, mine: AbortController, live: ChatMessage, trimEmpty: boolean) {
    // stopTimer 总是属于当前这一轮(切走时 abortStream 已清掉),旧一轮的收尾不能动它
    if (abort === mine) {
      running.value = false
      cancelling.value = false
      abort = null
      if (stopTimer) {
        clearTimeout(stopTimer)
        stopTimer = null
      }
    }
    if (sid !== sessionId.value || deleting.has(sid)) return
    if (trimEmpty && live.parts.length === 0) messages.value = messages.value.filter((m) => m !== live)
    await Promise.all([
      refreshConversations().then(() => announce(sid)),
      Promise.all([syncUserMessageIds(), refreshUsage()]).then(syncMissingFiles),
    ])
  }

  /** 打开一个仍在生成的会话:补一条空的助手消息并接上事件流。 */
  async function attach(promptId: string) {
    runSeq++
    running.value = true
    unmarkBackground(sessionId.value)
    // 生成中途已落库的部分内容会随事件流从头回放,先丢掉避免重复
    const last = messages.value[messages.value.length - 1]
    messages.value = messages.value.filter((m) => !(m.role === 'assistant' && m.runId === promptId))
    if (last?.role === 'assistant' && !last.runId && last.status === 'running') messages.value.pop()
    messages.value.push({ id: newId(), role: 'assistant', runId: promptId, parts: [] })
    const live = messages.value[messages.value.length - 1]
    const sid = sessionId.value
    const mine = (abort = new AbortController())
    try {
      await consume(sid, promptId, live, mine.signal)
    } catch (e) {
      if (!mine.signal.aborted && sid === sessionId.value) error.value = errMsg(e)
      live.status = 'error'
      live.errorText = errMsg(e)
    } finally {
      await finish(sid, mine, live, true)
    }
  }

  /** 对照磁盘,把已被删除的文件从面板隐藏(刷新页面后同样生效)。查不到时保持原样。 */
  async function syncMissingFiles() {
    const sid = sessionId.value
    try {
      const present = new Set(await api.listSessionFiles(sid))
      if (sid !== sessionId.value) return
      const known: FileRef[] = []
      for (const m of messages.value) {
        for (const p of m.parts) if (p.type === 'files') known.push(...p.files)
        known.push(...(m.attachmentFiles ?? []))
      }
      known.push(...(summary.value?.delivered ?? []), ...(summary.value?.uploaded ?? []))
      const missing = new Set<string>()
      for (const f of known) {
        const path = f.container_path
        if (!path) continue
        const rel = toRelativePath(path)
        if ((rel.startsWith('uploads/') || rel.startsWith('output/')) && !present.has(rel)) missing.add(path)
      }
      removedPaths.value = missing
    } catch {
      // 查不到只是多显示几个已删除的条目
    }
  }

  /** 删除会话里的一个文件(uploads/ 或 output/);成功后从面板隐藏,返回释放的字节数。 */
  async function removeFile(file: FileRef): Promise<number> {
    const sid = sessionId.value
    const freed = await api.deleteSessionFile(sid, toRelativePath(file.container_path ?? ''))
    if (sid === sessionId.value) removedPaths.value = new Set(removedPaths.value).add(file.container_path ?? file.file_name)
    return freed
  }

  /** 本地已加载消息中最早的数据库 id。从服务端载入的消息 id 是数字串;刚发送、未落库的是 UUID,跳过。 */
  function earliestLoadedId(): number | undefined {
    for (const m of messages.value) if (/^\d+$/.test(m.id)) return Number(m.id)
    return undefined
  }

  /** 刚发送的用户消息没有数据库 id;按顺序从服务端补上,之后才能编辑/重新生成。 */
  async function syncUserMessageIds() {
    const id = sessionId.value
    try {
      // 只取本地已加载的这一段:从最早已加载的消息起算,翻出的历史不会让条数对不上
      const from = earliestLoadedId()
      const all = await fetchLoadedSpan(id, from)
      if (id !== sessionId.value) return // 等待期间切换了会话,这批 id 不属于当前消息
      const rows = all.filter((m) => m.role === 'user')
      const local = messages.value.filter((m) => m.role === 'user')
      if (rows.length !== local.length) {
        // 条数对不上:本地状态不可信,按服务端重新载入,否则编辑/重新生成会一直不可用。
        // 服务端一条用户消息都没有(未启用持久化、发送被拒绝)时保持本地原样,别把界面清空
        if (rows.length > 0) await reloadMessages(id)
        return
      }
      local.forEach((m, i) => {
        m.dbId = rows[i].id
        m.rewindable = rows[i].rewindable
        m.attachmentFiles = rows[i].attachments
      })
    } catch {
      // 拿不到只是暂时不能编辑,重新打开会话即可恢复
    }
  }

  /** 取"最早已加载消息"及之后的全部消息。没有已加载的 id 时取最近一页。 */
  function fetchLoadedSpan(id: string, from: number | undefined): Promise<MessageRow[]> {
    return from === undefined ? api.getMessages(id, MESSAGE_PAGE_SIZE) : api.getMessages(id, undefined, undefined, from)
  }

  /** 用服务端的消息替换本地列表(仅在没有进行中的生成、且仍停留在该会话时)。 */
  async function reloadMessages(id: string) {
    if (running.value) return
    const mine = openSeq
    const runAtRead = runSeq
    const keep = Math.max(MESSAGE_PAGE_SIZE, messages.value.length)
    const rows = await api.getMessages(id, keep)
    if (mine !== openSeq || runAtRead !== runSeq || id !== sessionId.value || running.value) return
    messages.value = rows.map(toChatMessage)
    hasEarlier.value = rows.length >= keep
  }

  /** 向上翻一页:取比当前最早一条更早的消息,接到列表前面。 */
  async function loadEarlier() {
    if (loadingEarlier.value || !hasEarlier.value) return
    const id = sessionId.value
    const before = earliestLoadedId()
    if (before === undefined) return
    loadingEarlier.value = true
    try {
      const rows = await api.getMessages(id, MESSAGE_PAGE_SIZE, before)
      if (id !== sessionId.value) return
      const known = new Set(messages.value.map((m) => m.id))
      messages.value = [...rows.map(toChatMessage).filter((m) => !known.has(m.id)), ...messages.value]
      hasEarlier.value = rows.length >= MESSAGE_PAGE_SIZE
    } catch (e) {
      if (id === sessionId.value) error.value = errMsg(e)
    } finally {
      loadingEarlier.value = false
    }
  }

  /** 中止当前事件流并复位运行状态(下一轮 finish 因 abort 已换而不会再动全局状态) */
  function abortStream() {
    abort?.abort()
    abort = null
    running.value = false
    cancelling.value = false
    if (stopTimer) {
      clearTimeout(stopTimer)
      stopTimer = null
    }
  }

  function dispose() {
    abortStream()
    if (pollTimer) clearTimeout(pollTimer)
    pollTimer = null
  }

  /** 删除请求返回前不中止事件流:删除失败时生成照常显示;服务端取消导致的收尾也不再去同步这个会话 */
  async function removeConversation(id: string) {
    deleting.add(id)
    try {
      await api.deleteConversation(id)
    } catch (e) {
      error.value = errMsg(e)
      return
    } finally {
      deleting.delete(id)
    }
    if (id === sessionId.value && running.value) abortStream()
    unmarkBackground(id)
    drafts.value.delete(id)
    clearDraft(id)
    if (id === sessionId.value) {
      openSeq++
      loadingConversation.value = false
      sessionId.value = newId()
      messages.value = []
      hasEarlier.value = false
      usage.value = null
    }
    await refreshConversations()
  }

  async function rename(id: string, title: string) {
    try {
      await api.renameConversation(id, title)
    } catch (e) {
      error.value = errMsg(e)
      return
    }
    await refreshConversations()
  }

  /**
   * 按原 request_id 查持久回执。重试被拒绝说明原请求可能还在准备、尚未写下回执,多等几次。
   */
  async function acceptedReceipt(sid: string, e: api.SendUncertainError) {
    const attempts = e.retryRejected ? RECEIPT_POLLS : 1
    for (let i = 0; ; i++) {
      const recorded = await api.getAcceptedRun(sid, e.requestId)
      if (recorded || i + 1 >= attempts) return recorded
      await new Promise((r) => setTimeout(r, RECEIPT_POLL_MS))
    }
  }

  /**
   * 发送消息。rewindTo 为某条用户消息的数据库 id 时,先让服务端回退到该消息之前再发送:
   * 本地同步丢弃它及之后的消息,请求失败则还原。
   */
  async function send(text: string, files: File[], rewindTo?: number, interruptId?: string): Promise<boolean> {
    if (running.value) return false
    if (rewindTo === undefined && !text.trim() && files.length === 0) return false
    runSeq++
    error.value = null
    running.value = true
    const snapshot = messages.value.slice()
    let attachments = files.map((f) => f.name)
    let attachmentFiles: FileRef[] | undefined
    if (rewindTo !== undefined) {
      const at = messages.value.findIndex((m) => m.dbId === rewindTo)
      if (at < 0) {
        running.value = false
        return false
      }
      // 重新生成沿用原消息的附件,服务端会带上;这里仅用于展示
      // 从服务端载入的消息不带附件名。文本也为空说明原消息只有附件:此时发出的 user_query 为空(服务端允许,
      // 因为回退会带上原附件),界面上标出沿用了附件,别显示成一条空气泡
      if (files.length === 0) {
        attachments = messages.value[at].attachments ?? []
        attachmentFiles = messages.value[at].attachmentFiles
        if (!text.trim() && attachments.length === 0) attachments = ['(沿用原附件)']
      }
      messages.value = messages.value.slice(0, at)
    }
    messages.value.push({
      id: newId(),
      role: 'user',
      parts: text ? [{ type: 'text', text }] : [],
      attachments,
      attachmentFiles,
    })
    const assistant: ChatMessage = { id: newId(), role: 'assistant', parts: [] }
    messages.value.push(assistant)
    // 取响应式代理,后续对 parts 的修改才会触发渲染
    const live = messages.value[messages.value.length - 1]
    const sid = sessionId.value
    const mine = (abort = new AbortController())
    let accepted = false
    let uncertain = false
    let finishedElsewhere = false
    try {
      let promptId: string | null
      try {
        try {
          promptId = interruptId
            ? await api.sendMessage(sid, text, files, rewindTo, { interruptId })
            : await api.sendMessage(sid, text, files, rewindTo)
        } catch (e) {
          if (e instanceof api.ApiError && e.code === 'files_unavailable' && rewindTo !== undefined &&
              window.confirm('文件快照已不可用。是否仅回退对话并保留当前文件?')) {
            promptId = await api.sendMessage(sid, text, files, rewindTo, { conversationOnly: true })
          } else throw e
        }
      } catch (e) {
        if (e instanceof api.ApiError) throw e
        // 没拿到响应的网络错误:服务端可能已接受并开始生成,线程在运行就接回那一轮;
        // 已经跑完的,看服务端是否记下了这条消息,记下了就别把文字还给输入框
        if (!(e instanceof api.SendUncertainError)) throw e
        const recorded = await acceptedReceipt(sid, e)
        if (!recorded) {
          // 重试被拒绝而原请求的回执还查不到:原请求可能仍在准备,不能当作未接受把文字还给输入框,
          // 否则用户再发一次会换新的 request_id,可能重复执行
          if (e.retryRejected) {
            uncertain = true
            throw new Error('无法确认消息是否已发送,请稍后刷新会话查看,确认后再重发')
          }
          throw e
        }
        promptId = ['preparing', 'running', 'finalizing'].includes(recorded.status) ? recorded.id : null
      }
      accepted = true
      if (promptId) {
        live.runId = promptId
        const user = messages.value[messages.value.indexOf(live) - 1]
        if (user?.role === 'user') user.runId = promptId
        await consume(sid, promptId, live, mine.signal)
      }
      else finishedElsewhere = true
    } catch (e) {
      if (!mine.signal.aborted && sid === sessionId.value) error.value = errMsg(e)
      live.status = 'error'
      live.errorText = errMsg(e)
      // 服务端没有接受请求(被限流、有任务在跑、回退被拒绝等),对话没有变化,恢复原样
      if (!accepted && !uncertain && sid === sessionId.value) messages.value = snapshot
    } finally {
      await finish(sid, mine, live, accepted)
    }
    if (finishedElsewhere && sid === sessionId.value) {
      try {
        await reloadMessages(sid) // 那一轮的回答只在服务端
      } catch {
        // 重新打开会话即可看到
      }
    }
    // 结果未知时也算"已交出":调用方不把文字还回输入框,避免换个 request_id 重发
    return accepted || uncertain
  }

  /** 编辑某条用户消息后重发(丢弃它之后的全部对话)。 */
  async function editAndResend(messageId: string, newText: string) {
    const msg = messages.value.find((m) => m.id === messageId)
    if (!msg?.dbId || !msg.rewindable || !newText.trim()) return false
    const sid = sessionId.value
    const ok = await send(newText, [], msg.dbId)
    // 服务端没接受(限流、有任务在跑等):消息已恢复原样,把改过的文字交还给那条消息的编辑框
    if (!ok && sid === sessionId.value && messages.value.some((m) => m.id === messageId)) {
      rejectedEdit.value = { id: messageId, text: newText }
    }
    return ok
  }

  /** 用原问题(含原附件)重新生成某条用户消息之后的回答。 */
  function regenerate(messageId: string) {
    const msg = messages.value.find((m) => m.id === messageId)
    if (!msg?.dbId || !msg.rewindable) return Promise.resolve(false)
    const first = msg.parts[0]
    return send(first?.type === 'text' ? first.text : '', [], msg.dbId)
  }

  async function stop() {
    if (!running.value || cancelling.value) return
    cancelling.value = true
    try {
      await api.cancelGeneration(sessionId.value)
      // 正常情况下服务端会发 stream.end 让流自然结束;宽限期后仍没结束就直接中止
      const mine = abort
      stopTimer = setTimeout(() => {
        if (abort === mine) mine?.abort()
      }, 5000)
    } catch (e) {
      cancelling.value = false
      error.value = errMsg(e)
    }
  }

  return {
    sessionId, backgroundIds, isGenerating, conversations, conversationsHasMore, loadingMoreConversations, conversationsError, loadingConversation, hasEarlier, loadingEarlier, messages, running, cancelling, error, sharedFiles, uploadedFiles, todos, usage, removeFile,
    refreshConversations, loadMoreConversations, newConversation, openConversation, loadEarlier, removeConversation, rename, send, stop,
    editAndResend, rejectedEdit, regenerate, dispose,
    draftFor,
    persistDraft,
    queueDraftSave,
    flushDrafts,
  }
})
