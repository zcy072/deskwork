import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({ getStorage: vi.fn() }))
import { getStorage } from '@/api/client'
import { useChatStore } from '@/stores/chat'
import StorageUsage from './StorageUsage.vue'

const MB = 1048576
const info = {
  sizes: { uploads: MB, output: 2 * MB, scratchpad: 512, versions: 3 * MB, snapshots: 0 },
  quota_used: 3 * MB + 512,
  quota_limit: 1024 * MB,
}

function setup(withMessages = true) {
  setActivePinia(createPinia())
  const chat = useChatStore()
  chat.sessionId = 's1'
  if (withMessages) chat.messages = [{ id: 'm', role: 'user', parts: [] }]
  return mount(StorageUsage)
}

beforeEach(() => vi.resetAllMocks())

describe('存储占用', () => {
  it('分类别显示,并说明版本和快照不计入配额', async () => {
    vi.mocked(getStorage).mockResolvedValue(info)
    const w = setup()
    await flushPromises()
    const text = w.text()
    expect(text).toContain('上传')
    expect(text).toContain('历史版本')
    expect(text).toContain('3.0 MB')
    expect(text).toContain('不计入配额')
    expect(getStorage).toHaveBeenCalledWith('s1')
  })

  it('空会话不请求也不显示', async () => {
    const w = setup(false)
    await flushPromises()
    expect(getStorage).not.toHaveBeenCalled()
    expect(w.find('[aria-label="存储占用"]').exists()).toBe(false)
  })

  it('请求失败时不显示', async () => {
    vi.mocked(getStorage).mockRejectedValue(new Error('x'))
    const w = setup()
    await flushPromises()
    expect(w.find('[aria-label="存储占用"]').exists()).toBe(false)
  })

  it('历史版本有内容时可清理并通知父组件', async () => {
    vi.mocked(getStorage).mockResolvedValue(info)
    const w = setup()
    await flushPromises()
    const buttons = w.findAll('button.clear')
    expect(buttons).toHaveLength(2) // 暂存和历史版本都有内容
    await buttons[1].trigger('click') // 行顺序:暂存在前,历史版本在后
    expect(w.emitted('clear')).toEqual([['versions']])
  })

  it('暂存有内容时也能清理', async () => {
    vi.mocked(getStorage).mockResolvedValue({ ...info, sizes: { ...info.sizes, versions: 0 } })
    const w = setup()
    await flushPromises()
    const buttons = w.findAll('button.clear')
    expect(buttons).toHaveLength(1)
    await buttons[0].trigger('click')
    expect(w.emitted('clear')).toEqual([['scratchpad']])
  })

  it('没有历史版本和暂存时不显示清理按钮', async () => {
    vi.mocked(getStorage).mockResolvedValue({ ...info, sizes: { ...info.sizes, versions: 0, scratchpad: 0 } })
    const w = setup()
    await flushPromises()
    expect(w.find('button.clear').exists()).toBe(false)
  })
})
