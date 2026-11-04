/**
 * 页面在后台时提醒用户:生成结束、失败或需要回答时,给标签页标题加前缀,
 * 并在用户主动开启且浏览器已授权时发系统通知。页面重新可见后恢复标题。
 */
export type AttentionStatus = 'complete' | 'error' | 'waiting'

const PREFIX: Record<AttentionStatus, string> = { complete: '✓ ', error: '! ', waiting: '? ' }
const BODY: Record<AttentionStatus, string> = {
  complete: '已完成,有新结果',
  error: '生成失败',
  waiting: '等待你的回答',
}
const NOTIFY_KEY = 'notify'

let baseTitle: string | null = null

/** 只有这三种终态/等待态需要提醒;取消是用户自己做的,不提醒。 */
export function attentionStatus(status: string | null | undefined): AttentionStatus | null {
  return status === 'complete' || status === 'error' || status === 'waiting' ? status : null
}

export function setTitleAttention(status: AttentionStatus, doc: Document = document) {
  if (!doc.hidden) return
  if (baseTitle === null) baseTitle = doc.title
  doc.title = PREFIX[status] + baseTitle
}

export function clearTitleAttention(doc: Document = document) {
  if (baseTitle === null) return
  doc.title = baseTitle
  baseTitle = null
}

/** 页面重新可见(切回标签页或窗口)时恢复标题;返回卸载函数。 */
export function installAttentionReset(doc: Document = document, win: Window = window): () => void {
  const onVisible = () => {
    if (!doc.hidden) clearTitleAttention(doc)
  }
  doc.addEventListener('visibilitychange', onVisible)
  win.addEventListener('focus', onVisible)
  return () => {
    doc.removeEventListener('visibilitychange', onVisible)
    win.removeEventListener('focus', onVisible)
  }
}

function notificationApi(): typeof Notification | null {
  return typeof Notification === 'undefined' ? null : Notification
}

/** 用户开了开关,且浏览器权限仍是已授权。 */
export function notificationsEnabled(): boolean {
  const api = notificationApi()
  try {
    return !!api && api.permission === 'granted' && localStorage.getItem(NOTIFY_KEY) === '1'
  } catch {
    return false
  }
}

export function notificationsSupported(): boolean {
  return notificationApi() !== null
}

/** 打开开关:此时才向浏览器请求授权;被拒绝返回 false 且不保存。 */
export async function enableNotifications(): Promise<boolean> {
  const api = notificationApi()
  if (!api) return false
  const permission = api.permission === 'default' ? await api.requestPermission() : api.permission
  if (permission !== 'granted') return false
  try {
    localStorage.setItem(NOTIFY_KEY, '1')
  } catch {
    return false
  }
  return true
}

export function disableNotifications() {
  try {
    localStorage.removeItem(NOTIFY_KEY)
  } catch {
    /* 存储不可用时开关本来就关着 */
  }
}

/** 一个会话的运行状态变化了:页面在后台才提醒,在前台时侧栏标记已够用。 */
export function announceRun(status: string | null | undefined, title: string, doc: Document = document) {
  const s = attentionStatus(status)
  if (!s || !doc.hidden) return
  setTitleAttention(s, doc)
  if (notificationsEnabled()) {
    try {
      new Notification(title || 'Deskwork', { body: BODY[s], tag: 'deskwork-run' })
    } catch {
      /* 部分环境(如移动端)不允许直接构造通知,标题提示仍然有效 */
    }
  }
}
