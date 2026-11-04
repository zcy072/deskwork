import { beforeEach, describe, expect, it } from 'vitest'
import { MAX_DRAFTS, MAX_DRAFT_CHARS, NEW_CONVERSATION, clearDraft, loadDraft, loadDraftFiles, saveDraft } from './draftStorage'

function memoryStorage(): Storage {
  const m = new Map<string, string>()
  return {
    get length() { return m.size },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, v),
  }
}

describe('draftStorage', () => {
  let st: Storage
  beforeEach(() => { st = memoryStorage() })

  it('保存后可恢复,空白文字会删除草稿', () => {
    saveDraft('a', '你好', [], st)
    expect(loadDraft('a', st)).toBe('你好')
    saveDraft('a', '  \n', [], st)
    expect(loadDraft('a', st)).toBe('')
    expect(st.length).toBe(0)
  })

  it('不同会话互不影响,clearDraft 只删自己的', () => {
    saveDraft('a', 'A', [], st)
    saveDraft(NEW_CONVERSATION, 'N', [], st)
    clearDraft('a', st)
    expect(loadDraft('a', st)).toBe('')
    expect(loadDraft(NEW_CONVERSATION, st)).toBe('N')
  })

  it('不同 API Key 的草稿隔离,且不存 Key 本身', () => {
    st.setItem('deskwork_api_key', 'key-1')
    saveDraft('a', '身份一', [], st)
    st.setItem('deskwork_api_key', 'key-2')
    expect(loadDraft('a', st)).toBe('')
    saveDraft('a', '身份二', [], st)
    st.setItem('deskwork_api_key', 'key-1')
    expect(loadDraft('a', st)).toBe('身份一')
    expect([...Array(st.length).keys()].some((i) => st.key(i)!.includes('key-1'))).toBe(false)
  })

  it('单条长度和总条数有上限,保留最近修改的', () => {
    saveDraft('long', 'x'.repeat(MAX_DRAFT_CHARS + 10), [], st)
    expect(loadDraft('long', st).length).toBe(MAX_DRAFT_CHARS)
    const t0 = Date.now()
    for (let i = 0; i < MAX_DRAFTS + 5; i++) {
      saveDraft(`s${i}`, `t${i}`, [], st)
    }
    expect(loadDraft(`s${MAX_DRAFTS + 4}`, st)).toBe(`t${MAX_DRAFTS + 4}`)
    expect(Date.now() - t0).toBeGreaterThanOrEqual(0)
    const stored = JSON.parse(st.getItem([...Array(st.length).keys()].map((i) => st.key(i)!)[0])!)
    expect(Object.keys(stored).length).toBeLessThanOrEqual(MAX_DRAFTS)
  })

  it('存储不可用或内容损坏时不抛错', () => {
    const broken = {
      getItem: () => { throw new Error('denied') },
      setItem: () => { throw new Error('quota') },
      removeItem: () => { throw new Error('denied') },
    } as unknown as Storage
    expect(() => saveDraft('a', 'x', [], broken)).not.toThrow()
    expect(loadDraft('a', broken)).toBe('')
    const bad = memoryStorage()
    bad.setItem('deskwork_drafts:default', '{oops')
    expect(loadDraft('a', bad)).toBe('')
    bad.setItem('deskwork_drafts:default', '[1]')
    expect(loadDraft('a', bad)).toBe('')
  })

  it('附件名随草稿保存;文字为空但有附件名时草稿仍保留', () => {
    saveDraft('a', '', ['x.pdf', 'y.docx'], st)
    expect(loadDraft('a', st)).toBe('')
    expect(loadDraftFiles('a', st)).toEqual(['x.pdf', 'y.docx'])
    saveDraft('a', '', [], st)
    expect(loadDraftFiles('a', st)).toEqual([])
    expect(st.length).toBe(0)
  })
})
