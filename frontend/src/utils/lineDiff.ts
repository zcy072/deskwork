export interface DiffLine {
  op: 'same' | 'add' | 'del'
  text: string
}

/** 超过这个行数就不做逐行对比(LCS 是 O(n*m))。 */
export const MAX_DIFF_LINES = 2000

/** 逐行对比,返回 null 表示文件太大。相同的头尾先剥掉,只对中间部分做 LCS。 */
export function diffLines(oldText: string, newText: string): DiffLine[] | null {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  let head = 0
  while (head < a.length && head < b.length && a[head] === b[head]) head++
  let tail = 0
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++
  const ma = a.slice(head, a.length - tail)
  const mb = b.slice(head, b.length - tail)
  if (ma.length > MAX_DIFF_LINES || mb.length > MAX_DIFF_LINES) return null

  const n = ma.length
  const m = mb.length
  const dp: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = ma[i] === mb[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: DiffLine[] = a.slice(0, head).map((text) => ({ op: 'same', text }))
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (ma[i] === mb[j]) { out.push({ op: 'same', text: ma[i] }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ op: 'del', text: ma[i++] })
    else out.push({ op: 'add', text: mb[j++] })
  }
  while (i < n) out.push({ op: 'del', text: ma[i++] })
  while (j < m) out.push({ op: 'add', text: mb[j++] })
  for (const text of a.slice(a.length - tail)) out.push({ op: 'same', text })
  return out
}
