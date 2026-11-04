import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FileRef } from '@/types'

vi.mock('@/api/client', () => ({
  downloadFile: vi.fn(), fetchFileBlob: vi.fn(), fetchPreviewBlob: vi.fn(), saveBlob: vi.fn(),
}))
import { fetchFileBlob } from '@/api/client'
import PreviewDrawer from './PreviewDrawer.vue'

const file: FileRef = { file_name: 'a.txt', container_path: '/workspace/output/a.txt' }
let wrapper: ReturnType<typeof mount> | null = null

async function setup(busy = false) {
  wrapper = mount(PreviewDrawer, { props: { file, sessionId: 's1', busy }, attachTo: document.body })
  await flushPromises()
  return wrapper
}
const textarea = () => document.querySelector('textarea') as HTMLTextAreaElement
const submit = () =>
  [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === '提交修改') as HTMLButtonElement

async function type(value: string) {
  textarea().value = value
  textarea().dispatchEvent(new Event('input'))
  await flushPromises()
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(fetchFileBlob).mockResolvedValue(new Blob(['x']))
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('异步预览生命周期', () => {
  it('同一个文件引用切换会话时重新加载', async () => {
    const w = await setup()
    await w.setProps({ sessionId: 's2' })
    await flushPromises()
    expect(fetchFileBlob).toHaveBeenLastCalledWith('s2', 'output/a.txt')
  })

  it('卸载后返回的图片不再创建 Blob URL', async () => {
    let finish!: (blob: Blob) => void
    vi.mocked(fetchFileBlob).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const create = vi.fn(() => 'blob:preview')
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: vi.fn() })
    wrapper = mount(PreviewDrawer, { props: {
      file: { file_name: 'a.png', container_path: '/workspace/output/a.png' }, sessionId: 's1',
    } })
    wrapper.unmount()
    wrapper = null
    finish(new Blob(['png']))
    await flushPromises()
    expect(create).not.toHaveBeenCalled()
  })

  it('旧文本解码完成后不会覆盖新文件', async () => {
    let finish!: (text: string) => void
    const pending = new Promise<string>((resolve) => { finish = resolve })
    vi.mocked(fetchFileBlob).mockResolvedValueOnce({
      size: 3, slice: () => ({ text: () => pending }),
    } as unknown as Blob)
    const w = await setup()
    vi.mocked(fetchFileBlob).mockResolvedValueOnce({
      size: 3, slice: () => ({ text: async () => 'new content' }),
    } as unknown as Blob)
    await w.setProps({ file: { file_name: 'b.txt', container_path: '/workspace/output/b.txt' } })
    await flushPromises()
    finish('stale content')
    await flushPromises()
    expect(document.querySelector('.txt')?.textContent).toBe('new content')
  })
})

describe('预览旁提出修改', () => {
  it('提交修改要求并清空输入', async () => {
    const w = await setup()
    await type('  标题改成红色 ')
    submit().click()
    await flushPromises()
    expect(w.emitted('revise')).toEqual([['标题改成红色']])
    expect(textarea().value).toBe('')
  })

  it('空输入不能提交', async () => {
    const w = await setup()
    expect(submit().disabled).toBe(true)
    await type('   ')
    expect(submit().disabled).toBe(true)
    submit().click()
    expect(w.emitted('revise')).toBeUndefined()
  })

  it('生成中禁用输入与提交', async () => {
    await setup(true)
    expect(textarea().disabled).toBe(true)
    expect(submit().disabled).toBe(true)
  })
})
