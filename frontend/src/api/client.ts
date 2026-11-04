import type {
  Capabilities,
  ConversationAssets,
  ConversationSummary,
  ConversationUsage,
  MemoryItem,
  MessageRow,
  SearchHit,
  StreamPacket,
} from '@/types'
import { parseSseChunk } from '@/utils/sse'

const KEY_STORAGE = 'deskwork_api_key'
// 服务端空闲这么久就结束响应、客户端重连;取后端上限 4s,长时间工具调用时少占请求配额
const STREAM_BLOCK_MS = 4000
const SEND_RETRIES = 2
const SEND_RETRY_DELAY_MS = 500

/** 用户在页面里填的 Key 优先;没填时交给 nginx 注入。 */
// 浏览器禁用存储时访问 localStorage 会抛错;此时 Key 只保存在本页内存里
let memoryKey = ''

export function getApiKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) || ''
  } catch {
    return memoryKey
  }
}

export function setApiKey(key: string): void {
  memoryKey = key
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key)
    else localStorage.removeItem(KEY_STORAGE)
  } catch {
    /* 存储不可用:只在本页内存里生效 */
  }
}

let unauthorizedHandler: (() => void) | null = null

/** 后端返回 401 时回调(Key 缺失或无效),界面据此弹出填写 Key 的对话框。 */
export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const key = getApiKey()
  return key ? { Authorization: `Bearer ${key}`, ...extra } : extra
}

interface ReqInit extends Omit<RequestInit, 'body'> {
  /** 以 JSON 发送并带上 Content-Type */
  json?: unknown
  body?: BodyInit
}

function rawFetch(path: string, init: ReqInit = {}): Promise<Response> {
  const { json, headers: extra, body, ...rest } = init
  const extraHeaders = (extra ?? {}) as Record<string, string>
  // 同源部署:接口由 nginx 代理到后端
  return fetch(path, {
    ...rest,
    headers: headers(json !== undefined ? { 'Content-Type': 'application/json', ...extraHeaders } : extraHeaders),
    body: json !== undefined ? JSON.stringify(json) : body,
  })
}

/** 发请求并带上认证头;非 2xx 抛 ApiError(401 时触发 onUnauthorized)。 */
async function request(path: string, init?: ReqInit): Promise<Response> {
  return ensureOk(await rawFetch(path, init))
}

async function requestJson<T>(path: string, init?: ReqInit): Promise<T> {
  return (await request(path, init)).json() as Promise<T>
}

async function ensureOk(res: Response): Promise<Response> {
  if (res.ok) return res
  if (res.status === 401) unauthorizedHandler?.()
  let detail = res.statusText
  try {
    const body = await res.json()
    if (typeof body.detail === 'string') detail = body.detail
  } catch {
    // 非 JSON 响应,沿用 statusText
  }
  throw new ApiError(res.status, detail)
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

export async function sendMessage(
  sessionId: string,
  text: string,
  files: File[],
  rewindToMessageId?: number,
): Promise<string> {
  const form = new FormData()
  form.append('session_id', sessionId)
  form.append('user_query', text)
  for (const f of files) form.append('files', f)
  if (rewindToMessageId !== undefined) form.append('rewind_to_message_id', String(rewindToMessageId))
  // 同一次发送的重试共用这个 ID:服务端可能已接受请求而响应丢失,凭 ID 返回原 prompt_id,不会再起一轮
  form.append('client_request_id', crypto.randomUUID())
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await requestJson<{ prompt_id: string }>('/send-message', { method: 'POST', body: form })
      return res.prompt_id
    } catch (e) {
      // 只重试没拿到 HTTP 响应的网络错误;服务端明确拒绝(ApiError)原样抛出
      if (e instanceof ApiError || attempt >= SEND_RETRIES) throw e
      await new Promise((r) => setTimeout(r, SEND_RETRY_DELAY_MS * (attempt + 1)))
    }
  }
}

export interface StreamOptions {
  /** 连续失败多少次后查询线程状态(默认 5) */
  maxFailures?: number
  /** 退避起点与上限,毫秒 */
  backoffBaseMs?: number
  backoffMaxMs?: number
  /** 这么久没收到任何字节就主动重连(默认 60s) */
  idleTimeoutMs?: number
  /** 线程仍在运行时,最多再续几轮重试(默认 3) */
  extraRounds?: number
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve()
    const done = () => {
      clearTimeout(t)
      signal.removeEventListener('abort', done)
      resolve()
    }
    const t = setTimeout(done, ms)
    signal.addEventListener('abort', done, { once: true })
  })
}

/** 4xx(除超时/限流)重试没有意义,直接上抛 */
function isFatal(e: unknown): boolean {
  return e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429
}

/**
 * 读取一次运行的全部事件。服务端空闲 block_ms 后会主动结束响应,
 * 这里带着最后的 id 自动重连,直到收到 stream.end 或被 signal 中止。
 * 网络错误/5xx 按退避重试,连续失败过多后查询线程状态决定是继续还是报错;
 * 长时间没有任何字节视为卡死,带着 start_id 重连。
 */
class EmptyStreamError extends Error {}

export async function* streamEvents(
  sessionId: string,
  promptId: string,
  signal: AbortSignal,
  opts: StreamOptions = {},
): AsyncGenerator<StreamPacket> {
  const maxFailures = opts.maxFailures ?? 5
  const baseMs = opts.backoffBaseMs ?? 500
  const maxMs = opts.backoffMaxMs ?? 8000
  const idleMs = opts.idleTimeoutMs ?? 60_000
  let extraLeft = opts.extraRounds ?? 3
  let lastId = '0-0'
  let failures = 0
  let emptyRuns = 0
  while (!signal.aborted) {
    const attempt = new AbortController()
    const onAbort = () => attempt.abort()
    signal.addEventListener('abort', onAbort, { once: true })
    let stalled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const arm = () => {
      clearTimeout(timer)
      timer = setTimeout(() => {
        stalled = true
        attempt.abort()
      }, idleMs)
    }
    let reader: ReadableStreamDefaultReader<string> | undefined
    try {
      arm()
      const res = await request('/stream-events', {
        method: 'POST',
        json: { session_id: sessionId, prompt_id: promptId, start_id: lastId, block_ms: STREAM_BLOCK_MS },
        signal: attempt.signal,
      })
      const startedAt = Date.now()
      let got = 0
      if (!res.body) throw new Error('响应没有内容')
      reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        arm()
        failures = 0
        buffer += value
        const { packets, rest } = parseSseChunk(buffer)
        buffer = rest
        for (const packet of packets) {
          got++
          if (packet.id) lastId = packet.id
          yield packet
          if (packet.event === 'stream.end') return
        }
      }
      // 服务端立刻结束且没有任何事件(例如流已结束却没收到 stream.end):稍等再重连,避免空转打满请求
      if (got > 0) {
        emptyRuns = 0
      } else if (Date.now() - startedAt < 200) {
        // 反复收到空响应:不能无限重连,达到上限后看线程状态,仍在运行就再给几轮,否则报错
        if (++emptyRuns >= maxFailures) {
          const status = await getThreadStatus(sessionId).catch(() => null)
          if (status?.status === 'running' && extraLeft > 0) {
            extraLeft--
            emptyRuns = 0
          } else {
            throw new EmptyStreamError('事件流提前结束,未收到结束标记')
          }
        }
        await sleep(500, signal)
      }
    } catch (e) {
      if (signal.aborted) return
      if (e instanceof EmptyStreamError) throw e
      if (stalled) continue // 卡死:不算失败,直接带着 lastId 重连
      if (isFatal(e)) throw e
      failures++
      if (failures >= maxFailures) {
        const status = await getThreadStatus(sessionId).catch(() => null)
        if (status?.status === 'running' && extraLeft > 0) {
          extraLeft--
          failures = 0
        } else {
          throw e
        }
      }
      await sleep(Math.min(maxMs, baseMs * 2 ** (failures - 1)), signal)
    } finally {
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      await reader?.cancel().catch(() => {})
    }
  }
}

export async function cancelGeneration(sessionId: string): Promise<void> {
  await request(`/thread-cancel/${sessionId}`, { method: 'POST' })
}

/** 会话是否还有进行中的生成;没有记录(404)返回 null。 */
export async function getThreadStatus(
  id: string,
): Promise<{ status: string; prompt_id: string | null } | null> {
  const res = await rawFetch(`/thread-status/${id}`)
  if (res.status === 404) return null
  return (await ensureOk(res)).json()
}

/** 单页上限由后端决定(最多 200);按 offset 翻页。 */
export function listConversations(limit = 100, offset = 0): Promise<ConversationSummary[]> {
  return requestJson(`/conversations?limit=${limit}&offset=${offset}`)
}

export function searchConversations(q: string): Promise<SearchHit[]> {
  return requestJson(`/conversations/search?${new URLSearchParams({ q })}`)
}

/** 不传参数取全部(导出用);limit 取最近 limit 条,beforeId 向前翻页,fromId 取 id >= fromId 的全部。 */
export function getMessages(id: string, limit?: number, beforeId?: number, fromId?: number): Promise<MessageRow[]> {
  const qs = new URLSearchParams()
  if (limit !== undefined) qs.set('limit', String(limit))
  if (beforeId !== undefined) qs.set('before_id', String(beforeId))
  if (fromId !== undefined) qs.set('from_id', String(fromId))
  const q = qs.toString()
  return requestJson(`/conversations/${id}/messages${q ? `?${q}` : ''}`)
}

export function getUsage(id: string): Promise<ConversationUsage> {
  return requestJson(`/conversations/${id}/usage`)
}

export function getConversationAssets(id: string): Promise<ConversationAssets> {
  return requestJson(`/conversations/${id}/assets`)
}

export async function renameConversation(id: string, title: string): Promise<void> {
  await request(`/conversations/${id}`, { method: 'PATCH', json: { title } })
}

/** 用户看过这个会话的最新结果,清除未读标记。 */
export async function markConversationRead(id: string): Promise<void> {
  await request(`/conversations/${id}/read`, { method: 'POST' })
}

export async function deleteConversation(id: string): Promise<void> {
  await request(`/conversations/${id}`, { method: 'DELETE' })
}

export function getCapabilities(): Promise<Capabilities> {
  return requestJson('/capabilities')
}

export function listMemories(): Promise<MemoryItem[]> {
  return requestJson('/memories')
}

/** ttlDays 为 0 表示永不过期,不传则用服务端默认值。 */
export function addMemory(content: string, ttlDays?: number): Promise<MemoryItem> {
  return requestJson('/memories', { method: 'POST', json: { content, ttl_days: ttlDays ?? null } })
}

export async function deleteMemory(id: number): Promise<void> {
  await request(`/memories/${id}`, { method: 'DELETE' })
}

/** 文件接口需要认证头,所以不能直接用 <a href>,统一先取成 Blob。 */
export async function fetchFileBlob(sessionId: string, relativePath: string, fingerprint?: string): Promise<Blob> {
  const qs = new URLSearchParams({ session_id: sessionId, relative_path: relativePath })
  if (fingerprint) qs.set('fingerprint', fingerprint)
  return (await request(`/files/download?${qs}`)).blob()
}

/** 取回某次交付时的内容(按交付记录里的摘要)。版本已被裁剪时抛 ApiError(410)。 */
export async function fetchDeliveredBlob(sessionId: string, fileName: string, sha256: string, size?: number): Promise<Blob> {
  const qs = new URLSearchParams({ session_id: sessionId, file_name: fileName, sha256 })
  if (size !== undefined) qs.set('size', String(size))
  return (await request(`/files/delivered?${qs}`)).blob()
}

/** Office 文件经服务端在沙箱里转成 PDF 后返回;首次转换可能要几十秒。 */
export async function fetchPreviewBlob(sessionId: string, relativePath: string): Promise<Blob> {
  const qs = new URLSearchParams({ session_id: sessionId, relative_path: relativePath })
  return (await request(`/files/preview?${qs}`)).blob()
}

export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  // 立即 revoke 会让部分浏览器取消大文件下载
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

export async function downloadFile(sessionId: string, relativePath: string, name: string): Promise<void> {
  saveBlob(await fetchFileBlob(sessionId, relativePath), name)
}

/** 会话备份包(zip):消息、附件元数据和 uploads/ output/ 里的文件,附 sha256 清单。 */
export async function downloadSessionArchive(sessionId: string): Promise<void> {
  const qs = new URLSearchParams({ session_id: sessionId })
  saveBlob(await (await request(`/files/export?${qs}`)).blob(), `deskwork-${sessionId}.zip`)
}

export interface ImportResult {
  id: string
  title: string
  message_count: number
  file_count: number
  missing: string[]
  /** 备份里带了 checkpoint 且已恢复,可以接着原上下文继续对话 */
  resumable: boolean
  warnings: string[]
}

/** 导入备份包,总是建成一个新会话。 */
export async function importSessionArchive(file: File): Promise<ImportResult> {
  const form = new FormData()
  form.append('file', file)
  return requestJson<ImportResult>('/conversations/import', { method: 'POST', body: form })
}

export interface FileVersion {
  version: string
  size: number
  modified_at: number
}

/** output/ 里同名文件被覆盖前留下的历史版本,新的在前。 */
export async function listFileVersions(sessionId: string, fileName: string): Promise<FileVersion[]> {
  const qs = new URLSearchParams({ session_id: sessionId, file_name: fileName })
  return requestJson<FileVersion[]>(`/files/versions?${qs}`)
}

export async function fetchFileVersionBlob(sessionId: string, fileName: string, version: string): Promise<Blob> {
  const qs = new URLSearchParams({ session_id: sessionId, file_name: fileName, version })
  return (await request(`/files/version?${qs}`)).blob()
}

export interface StorageInfo {
  /** 字节;uploads / output / scratchpad 计入配额,versions / snapshots 只展示 */
  sizes: Record<'uploads' | 'output' | 'scratchpad' | 'versions' | 'snapshots', number>
  quota_used: number
  quota_limit: number
}

export function getStorage(sessionId: string): Promise<StorageInfo> {
  return requestJson<StorageInfo>(`/files/storage?${new URLSearchParams({ session_id: sessionId })}`)
}

/** 删除 uploads/ 或 output/ 下的单个文件,返回释放的字节数。 */
export async function deleteSessionFile(sessionId: string, relativePath: string): Promise<number> {
  const qs = new URLSearchParams({ session_id: sessionId, relative_path: relativePath })
  return ((await (await request(`/files?${qs}`, { method: 'DELETE' })).json()) as { freed: number }).freed
}

/** 清空所有文件的历史版本(保留各文件最近一次交付的副本),返回释放的字节数。 */
export function clearFileVersions(sessionId: string): Promise<number> {
  return clearStorage('versions', sessionId)
}

/** 清空暂存目录(agent 的中间文件),返回释放的字节数。 */
export function clearScratchpad(sessionId: string): Promise<number> {
  return clearStorage('scratchpad', sessionId)
}

async function clearStorage(what: 'versions' | 'scratchpad', sessionId: string): Promise<number> {
  const qs = new URLSearchParams({ session_id: sessionId })
  return ((await (await request(`/files/${what}?${qs}`, { method: 'DELETE' })).json()) as { freed: number }).freed
}

/** uploads/ 与 output/ 里现在还在磁盘上的文件(相对会话根的路径)。 */
export function listSessionFiles(sessionId: string): Promise<string[]> {
  return requestJson<string[]>(`/files/list?${new URLSearchParams({ session_id: sessionId })}`)
}
