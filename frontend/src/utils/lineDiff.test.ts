import { describe, expect, it } from 'vitest'
import { diffLines, MAX_DIFF_LINES } from './lineDiff'

const fmt = (d: ReturnType<typeof diffLines>) => d!.map((l) => ({ same: ' ', add: '+', del: '-' })[l.op] + l.text)

describe('diffLines', () => {
  it('marks added, removed and unchanged lines', () => {
    expect(fmt(diffLines('a\nb\nc', 'a\nx\nc\nd'))).toEqual([' a', '-b', '+x', ' c', '+d'])
  })
  it('identical text is all same', () => {
    expect(diffLines('a\nb', 'a\nb')!.every((l) => l.op === 'same')).toBe(true)
  })
  it('handles empty old text', () => {
    expect(fmt(diffLines('', 'a'))).toEqual(['-', '+a'])
  })
  it('gives up on huge inputs', () => {
    const big = Array.from({ length: MAX_DIFF_LINES + 1 }, (_, i) => `l${i}`).join('\n')
    expect(diffLines(big, 'other')).toBeNull()
  })
})
