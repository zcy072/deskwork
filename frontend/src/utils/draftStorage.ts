/**
 * 输入框草稿的持久化:刷新页面后恢复文字。附件内容存不下,只记文件名,用来提示用户重新选择。
 *
 * - 按 API Key 的短哈希分命名空间,换身份看不到上一个身份的草稿;不存 Key 本身。
 * - 条数和单条长度有上限,超出时丢弃最久没改过的草稿。
 * - localStorage 不可用(隐私模式、配额满)时静默降级为只在内存里保留。
 */
const PREFIX = 'deskwork_drafts:'
const API_KEY_STORAGE = 'deskwork_api_key' // 与 api/client.ts 保存 Key 的位置一致
export const NEW_CONVERSATION = '__new__' // 还没发出第一条消息的新对话
export const MAX_DRAFTS = 50
export const MAX_DRAFT_CHARS = 20_000
export const MAX_DRAFT_FILES = 20

type Stored = Record<string, { text: string; t: number; files?: string[] }>

function fingerprint(s: string): string {
  let h = 0x811c9dc5 // FNV-1a,只用来分命名空间
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193)
  return (h >>> 0).toString(16)
}

function storageKey(storage: Pick<Storage, 'getItem'>): string {
  let key = ''
  try {
    key = storage.getItem(API_KEY_STORAGE) ?? ''
  } catch {
    /* 读不到就当作没有身份 */
  }
  return PREFIX + (key ? fingerprint(key) : 'default')
}

function read(storage: Storage): Stored {
  try {
    const data = JSON.parse(storage.getItem(storageKey(storage)) ?? '{}')
    if (!data || typeof data !== 'object' || Array.isArray(data)) return {}
    const out: Stored = {}
    for (const [id, v] of Object.entries<any>(data)) {
      if (v && typeof v.text === 'string' && typeof v.t === 'number') {
        const files = Array.isArray(v.files) ? v.files.filter((n: unknown) => typeof n === 'string') : []
        out[id] = { text: v.text, t: v.t, ...(files.length ? { files } : {}) }
      }
    }
    return out
  } catch {
    return {}
  }
}

function write(storage: Storage, data: Stored): void {
  try {
    const entries = Object.entries(data).sort((a, b) => b[1].t - a[1].t).slice(0, MAX_DRAFTS)
    if (entries.length === 0) storage.removeItem(storageKey(storage))
    else storage.setItem(storageKey(storage), JSON.stringify(Object.fromEntries(entries)))
  } catch {
    /* 配额满或不可用:草稿只留在内存 */
  }
}

export function loadDraft(id: string, storage?: Storage): string {
  try {
    return read(storage ?? localStorage)[id]?.text ?? ''
  } catch {
    return ''
  }
}

/** 上次未能保存的附件名(刷新后需要重新选择)。 */
export function loadDraftFiles(id: string, storage?: Storage): string[] {
  try {
    return read(storage ?? localStorage)[id]?.files ?? []
  } catch {
    return []
  }
}

/** 文字为空(只有空白)且没有附件名就删除这条草稿。 */
export function saveDraft(id: string, text: string, files: string[] = [], storage?: Storage): void {
  try {
    storage ??= localStorage
    const data = read(storage)
    const names = files.slice(0, MAX_DRAFT_FILES)
    if (text.trim() === '' && names.length === 0) delete data[id]
    else data[id] = { text: text.slice(0, MAX_DRAFT_CHARS), t: Date.now(), ...(names.length ? { files: names } : {}) }
    write(storage, data)
  } catch {
    /* 同上 */
  }
}

export function clearDraft(id: string, storage?: Storage): void {
  saveDraft(id, '', [], storage)
}
