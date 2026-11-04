import { Blob as NodeBlob } from 'node:buffer'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SourceRef } from '@/types'

vi.mock('@/api/client', () => ({ fetchFileBlob: vi.fn(), saveBlob: vi.fn() }))
import { fetchFileBlob } from '@/api/client'
import SourcePreview from './SourcePreview.vue'

const source: SourceRef = {
  id: 'ref1', file_name: 'notes.txt', container_path: '/workspace/uploads/notes.txt',
  fingerprint: 'a'.repeat(64), excerpt: '预算 100 元', location: { kind: 'lines', start: 3, end: 3 },
}
const blob = (text: string) => new NodeBlob([text]) as unknown as Blob
function setup(s: SourceRef = source) {
  return mount(SourcePreview, {
    props: { source: s, sessionId: 's1' },
    global: { stubs: { Modal: { template: '<div><slot /></div>' } } },
  })
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:test-pdf')
    static revokeObjectURL = vi.fn()
  })
})
afterEach(() => vi.unstubAllGlobals())

describe('原文预览', () => {
  it('携带指纹获取原文并高亮对应行,摘录只作为文本显示', async () => {
    vi.mocked(fetchFileBlob).mockResolvedValue(blob('标题\n\n预算 100 元\n备注'))
    const wrapper = setup({ ...source, excerpt: '<script>alert(1)</script>' })
    await flushPromises()
    expect(fetchFileBlob).toHaveBeenCalledWith('s1', 'uploads/notes.txt', source.fingerprint)
    expect(wrapper.find('.selected').text()).toContain('3预算 100 元')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('.excerpt').text()).toBe('<script>alert(1)</script>')
    wrapper.unmount()
  })

  it('文件变化时保留原摘录并显示警告,不展示当前原文', async () => {
    vi.mocked(fetchFileBlob).mockRejectedValue(new Error('原文件已变化'))
    const wrapper = setup()
    await flushPromises()
    expect(wrapper.text()).toContain('原文件已变化')
    expect(wrapper.find('.excerpt').text()).toBe('预算 100 元')
    expect(wrapper.find('.context').exists()).toBe(false)
    expect(wrapper.find('button').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('PDF 定位到物理页码,关闭和卸载时释放原始 blob URL', async () => {
    vi.mocked(fetchFileBlob).mockResolvedValue(blob('%PDF'))
    const wrapper = setup({ ...source, file_name: 'a.pdf', location: { kind: 'page', page: 3 } })
    await flushPromises()
    expect(wrapper.find('iframe').attributes('src')).toBe('blob:test-pdf#page=3')
    await wrapper.setProps({ source: null })
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test-pdf')
    wrapper.unmount()
  })

  it('幻灯片引用校验文件后展示摘录和幻灯片号,不渲染 iframe', async () => {
    vi.mocked(fetchFileBlob).mockResolvedValue(blob('PK'))
    const wrapper = setup({ ...source, file_name: 'deck.pptx', location: { kind: 'slide', slide: 3 } })
    await flushPromises()
    expect(fetchFileBlob).toHaveBeenCalledWith('s1', 'uploads/notes.txt', source.fingerprint)
    expect(wrapper.text()).toContain('第 3 张幻灯片')
    expect(wrapper.text()).toContain('预算 100 元')
    expect(wrapper.find('iframe').exists()).toBe(false)
    wrapper.unmount()
  })

  it('Word 段落引用校验文件后展示摘录和段号,不读取正文', async () => {
    const raw = blob('PK')
    const text = vi.spyOn(raw, 'text')
    vi.mocked(fetchFileBlob).mockResolvedValue(raw)
    const wrapper = setup({ ...source, file_name: 'report.docx', location: { kind: 'paragraphs', start: 4, end: 6 } })
    await flushPromises()
    expect(wrapper.text()).toContain('第 4–6 段')
    expect(wrapper.text()).toContain('预算 100 元')
    expect(wrapper.find('.context').exists()).toBe(false)
    expect(text).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('切换引用后旧请求返回不会覆盖新引用', async () => {
    let release!: (b: Blob) => void
    vi.mocked(fetchFileBlob).mockReturnValueOnce(new Promise((r) => { release = r }))
    const wrapper = setup()
    vi.mocked(fetchFileBlob).mockResolvedValueOnce(blob('新的原文'))
    await wrapper.setProps({ source: { ...source, id: 'ref2', location: { kind: 'lines', start: 1, end: 1 } } })
    await flushPromises()
    release(blob('旧的原文'))
    await flushPromises()
    expect(wrapper.find('.context').text()).toContain('新的原文')
    expect(wrapper.find('.context').text()).not.toContain('旧的原文')
    wrapper.unmount()
  })
})
