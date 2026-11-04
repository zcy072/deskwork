import type { ConversationSummary } from '@/types'

export type RunBadge = { kind: 'running' | 'unread' | 'waiting' | 'error' | 'cancelled'; label: string }

/**
 * 侧边栏每个会话旁的状态标记。
 * - 正在生成:总是显示。
 * - 等待回答:需要用户动作,只要还在等就显示。
 * - 完成/失败/取消:只在未读时显示,看过之后不再占位。
 * - 正在查看的会话不显示(结果就在眼前)。
 */
export function runBadge(c: ConversationSummary, generating: boolean, isCurrent: boolean): RunBadge | null {
  if (generating) return { kind: 'running', label: '正在生成' }
  if (isCurrent) return null
  if (c.status === 'waiting') return { kind: 'waiting', label: '等待你的回答' }
  if (!c.unread) return null
  if (c.status === 'complete') return { kind: 'unread', label: '已完成,有新结果' }
  if (c.status === 'error') return { kind: 'error', label: '生成失败' }
  if (c.status === 'cancelled') return { kind: 'cancelled', label: '已取消' }
  return null
}
