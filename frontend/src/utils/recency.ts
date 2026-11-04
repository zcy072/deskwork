export interface RecencyGroup<T> {
  label: string
  items: T[]
}

const DAY = 24 * 60 * 60 * 1000

/**
 * 按更新时间分成 今天 / 近 7 天 / 更早(按本地日历日算),保持输入顺序,空组不返回。
 * 时间无法解析的归入"更早"。
 */
export function groupByRecency<T extends { updated_at: string }>(items: T[], now = new Date()): RecencyGroup<T>[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const groups: RecencyGroup<T>[] = [
    { label: '今天', items: [] },
    { label: '近 7 天', items: [] },
    { label: '更早', items: [] },
  ]
  for (const it of items) {
    const t = Date.parse(it.updated_at)
    if (Number.isNaN(t)) groups[2].items.push(it)
    else if (t >= startOfToday) groups[0].items.push(it)
    else if (t >= startOfToday - 6 * DAY) groups[1].items.push(it)
    else groups[2].items.push(it)
  }
  return groups.filter((g) => g.items.length > 0)
}
