import { describe, expect, it } from 'vitest'
import { DEFAULT_TEMPLATES, loadTemplates, makeTemplate, MAX_TEMPLATES, saveTemplates, STORAGE_KEY } from './templates'

const store = (value: string | null) => ({ getItem: () => value })

describe('templates', () => {
  it('没保存过用默认;保存内容损坏时退回默认', () => {
    expect(loadTemplates(store(null))).toEqual(DEFAULT_TEMPLATES)
    expect(loadTemplates(store('{oops'))).toEqual(DEFAULT_TEMPLATES)
    expect(loadTemplates(store('{"a":1}'))).toEqual(DEFAULT_TEMPLATES)
  })

  it('保存的空数组不会被默认值覆盖;非法条目被过滤', () => {
    expect(loadTemplates(store('[]'))).toEqual([])
    const raw = JSON.stringify([{ id: '1', label: 'a', text: 'b' }, { id: '2', label: '', text: 'x' }, 7])
    expect(loadTemplates(store(raw))).toEqual([{ id: '1', label: 'a', text: 'b' }])
  })

  it('保存写入 localStorage 约定的键', () => {
    const calls: [string, string][] = []
    saveTemplates([{ id: '1', label: 'a', text: 'b' }], { setItem: (k, v) => calls.push([k, v]) })
    expect(calls).toEqual([[STORAGE_KEY, '[{"id":"1","label":"a","text":"b"}]']])
  })

  it('makeTemplate 校验空值、重名、长度与数量上限', () => {
    expect(makeTemplate(' ', 'x', [])).toMatch('不能为空')
    expect(makeTemplate('a', '  ', [])).toMatch('不能为空')
    expect(makeTemplate('x'.repeat(21), 'x', [])).toMatch('名称最多')
    expect(makeTemplate('a', 'x'.repeat(2001), [])).toMatch('内容最多')
    expect(makeTemplate('a', 'x', [{ id: '1', label: 'a', text: 'y' }])).toBe('已有同名模板')
    const full = Array.from({ length: MAX_TEMPLATES }, (_, i) => ({ id: `${i}`, label: `l${i}`, text: 't' }))
    expect(makeTemplate('new', 'x', full)).toMatch('最多保存')
    const ok = makeTemplate(' 周报 ', ' 写周报 ', [])
    expect(ok).toMatchObject({ label: '周报', text: '写周报' })
  })
})
