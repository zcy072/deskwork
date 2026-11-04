import { describe, expect, it } from 'vitest'
import type { ConversationSummary } from '@/types'
import { runBadge } from './runBadge'

const c = (status: any, unread = false): ConversationSummary => ({
  id: 'x', title: 't', message_count: 1, created_at: '', updated_at: '', status, unread,
})

describe('runBadge', () => {
  it('生成中优先,即使是当前会话', () => {
    expect(runBadge(c('complete', true), true, true)?.kind).toBe('running')
  })
  it('当前会话不显示结果标记,等待回答也不重复提示', () => {
    expect(runBadge(c('complete', true), false, true)).toBeNull()
    expect(runBadge(c('waiting'), false, true)).toBeNull()
  })
  it('等待回答不依赖未读', () => {
    expect(runBadge(c('waiting', false), false, false)?.kind).toBe('waiting')
  })
  it('终态只在未读时显示', () => {
    for (const s of ['complete', 'error', 'cancelled']) {
      expect(runBadge(c(s, true), false, false)).not.toBeNull()
      expect(runBadge(c(s, false), false, false)).toBeNull()
    }
  })
  it('旧数据没有状态时不显示', () => {
    expect(runBadge({ ...c(null), status: undefined, unread: undefined }, false, false)).toBeNull()
  })
})
