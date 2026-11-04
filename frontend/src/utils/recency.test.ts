import { describe, expect, it } from 'vitest'
import { groupByRecency } from './recency'

const now = new Date(2026, 9, 30, 15, 0, 0) // 本地 10-30 15:00
const at = (d: Date) => ({ updated_at: d.toISOString() })

describe('groupByRecency', () => {
  it('按本地日历日分成 今天 / 近 7 天 / 更早,保持顺序,空组不返回', () => {
    const items = [
      { id: 'a', ...at(new Date(2026, 9, 30, 0, 5)) }, // 今天 00:05
      { id: 'b', ...at(new Date(2026, 9, 29, 23, 59)) }, // 昨天
      { id: 'c', ...at(new Date(2026, 9, 24, 12)) }, // 6 天前,仍在近 7 天
      { id: 'd', ...at(new Date(2026, 9, 23, 12)) }, // 7 天前
      { id: 'e', updated_at: '' }, // 无法解析
    ]
    const g = groupByRecency(items, now)
    expect(g.map((x) => x.label)).toEqual(['今天', '近 7 天', '更早'])
    expect(g.map((x) => x.items.map((i) => i.id))).toEqual([['a'], ['b', 'c'], ['d', 'e']])
  })

  it('只有一个分组时不返回其它组', () => {
    expect(groupByRecency([{ updated_at: new Date(2026, 9, 30, 9).toISOString() }], now)).toHaveLength(1)
    expect(groupByRecency([], now)).toEqual([])
  })
})
