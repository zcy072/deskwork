import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  announceRun,
  attentionStatus,
  clearTitleAttention,
  disableNotifications,
  enableNotifications,
  installAttentionReset,
  notificationsEnabled,
} from './attention'

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
}

class FakeNotification {
  static permission: NotificationPermission = 'default'
  static requestPermission = vi.fn(async () => FakeNotification.permission)
  static created: { title: string; opts?: NotificationOptions }[] = []
  constructor(title: string, opts?: NotificationOptions) {
    FakeNotification.created.push({ title, opts })
  }
}

beforeEach(() => {
  document.title = 'Deskwork'
  localStorage.clear()
  FakeNotification.permission = 'default'
  FakeNotification.created = []
  FakeNotification.requestPermission.mockClear()
  vi.stubGlobal('Notification', FakeNotification)
  setHidden(false)
})
afterEach(() => {
  clearTitleAttention()
  vi.unstubAllGlobals()
})

describe('attentionStatus', () => {
  it('只对完成、失败、等待回答提醒', () => {
    expect(attentionStatus('complete')).toBe('complete')
    expect(attentionStatus('error')).toBe('error')
    expect(attentionStatus('waiting')).toBe('waiting')
    for (const s of ['cancelled', 'running', null, undefined]) expect(attentionStatus(s)).toBeNull()
  })
})

describe('announceRun', () => {
  it('页面可见时不改标题', () => {
    announceRun('complete', 't')
    expect(document.title).toBe('Deskwork')
  })
  it('页面在后台时加前缀,重复提醒不叠加', () => {
    setHidden(true)
    announceRun('complete', 't')
    expect(document.title).toBe('✓ Deskwork')
    announceRun('waiting', 't')
    expect(document.title).toBe('? Deskwork')
  })
  it('取消不提醒', () => {
    setHidden(true)
    announceRun('cancelled', 't')
    expect(document.title).toBe('Deskwork')
  })
  it('页面重新可见后恢复标题', () => {
    setHidden(true)
    announceRun('error', 't')
    const off = installAttentionReset()
    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(document.title).toBe('Deskwork')
    off()
  })
  it('没开开关或没授权时不发系统通知', () => {
    setHidden(true)
    FakeNotification.permission = 'granted'
    announceRun('complete', 't')
    expect(FakeNotification.created).toHaveLength(0)
  })
  it('开了开关且已授权时发系统通知', async () => {
    FakeNotification.permission = 'granted'
    expect(await enableNotifications()).toBe(true)
    setHidden(true)
    announceRun('waiting', '我的会话')
    expect(FakeNotification.created).toEqual([
      { title: '我的会话', opts: { body: '等待你的回答', tag: 'deskwork-run' } },
    ])
  })
})

describe('notifications switch', () => {
  it('授权被拒绝时不保存开关', async () => {
    FakeNotification.permission = 'default'
    FakeNotification.requestPermission.mockResolvedValueOnce('denied')
    expect(await enableNotifications()).toBe(false)
    expect(localStorage.getItem('notify')).toBeNull()
  })
  it('只在打开开关时才请求授权', async () => {
    announceRun('complete', 't')
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled()
    FakeNotification.requestPermission.mockResolvedValueOnce('granted')
    FakeNotification.permission = 'default'
    await enableNotifications()
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1)
  })
  it('关闭后不再通知', async () => {
    FakeNotification.permission = 'granted'
    await enableNotifications()
    disableNotifications()
    expect(notificationsEnabled()).toBe(false)
  })
})
