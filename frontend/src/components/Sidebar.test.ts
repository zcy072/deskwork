import { mount } from '@vue/test-utils'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick } from 'vue'

vi.mock('@/api/client', () => ({
  getCapabilities: vi.fn().mockResolvedValue({ memory: false }),
  listConversations: vi.fn(),
  searchConversations: vi.fn(),
  downloadSessionArchive: vi.fn().mockResolvedValue(undefined),
  importSessionArchive: vi.fn(),
}))
import { downloadSessionArchive, importSessionArchive, searchConversations } from '@/api/client'
import { useChatStore } from '@/stores/chat'
import Sidebar from './Sidebar.vue'

function setup() {
  setActivePinia(createPinia())
  const chat = useChatStore()
  chat.conversations = [
    { id: 'c1', title: '会话一', message_count: 1, created_at: '', updated_at: '' },
    { id: 'c2', title: '会话二', message_count: 1, created_at: '', updated_at: '' },
  ]
  const Host = defineComponent({
    render: () =>
      h(NMessageProvider, null, {
        default: () => h(NDialogProvider, null, { default: () => h(Sidebar, { themeLabel: '跟随系统' }) }),
      }),
  })
  return { chat, wrapper: mount(Host, { attachTo: document.body }) }
}

describe('Sidebar', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('会话标题是原生 button(可聚焦、Enter 可打开),并标出当前会话', async () => {
    const { chat, wrapper } = setup()
    const open = vi.spyOn(chat, 'openConversation').mockResolvedValue()
    const titles = wrapper.findAll('button.title')
    expect(titles.map((t) => t.text())).toEqual(['会话一', '会话二'])
    expect(titles[0].attributes('type')).toBe('button')
    await titles[1].trigger('click')
    expect(open).toHaveBeenCalledWith('c2')
    wrapper.unmount()
  })

  it('按状态显示标记:后台完成未读、失败、等待回答、取消;已读和当前会话不显示', async () => {
    const { chat, wrapper } = setup()
    chat.sessionId = 'cur'
    chat.conversations = [
      { id: 'a', title: 'A', message_count: 1, created_at: '', updated_at: '', status: 'complete', unread: true },
      { id: 'b', title: 'B', message_count: 1, created_at: '', updated_at: '', status: 'error', unread: true },
      { id: 'c', title: 'C', message_count: 1, created_at: '', updated_at: '', status: 'waiting', unread: false },
      { id: 'd', title: 'D', message_count: 1, created_at: '', updated_at: '', status: 'cancelled', unread: true },
      { id: 'e', title: 'E', message_count: 1, created_at: '', updated_at: '', status: 'complete', unread: false },
      { id: 'cur', title: 'F', message_count: 1, created_at: '', updated_at: '', status: 'complete', unread: true },
      { id: 'g', title: 'G', message_count: 1, created_at: '', updated_at: '', status: 'running', unread: false },
    ]
    chat.backgroundIds = ['g']
    await nextTick()
    const labels = wrapper.findAll('.item').map((i) => i.find('.dot').exists() ? i.find('.dot').attributes('aria-label') : null)
    expect(labels).toEqual(['已完成,有新结果', '生成失败', '等待你的回答', '已取消', null, null, '正在生成'])
    wrapper.unmount()
  })

  it('输入后防抖搜索并显示命中片段,点击打开,清空恢复列表', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(searchConversations).mockResolvedValue([
        { id: 'c2', title: '会话二', snippet: '…销售数据…', updated_at: '' },
      ])
      const { chat, wrapper } = setup()
      const open = vi.spyOn(chat, 'openConversation').mockResolvedValue()
      const input = wrapper.find('.search input')
      await input.setValue('销售')
      await input.setValue('销售数')
      await vi.advanceTimersByTimeAsync(350)
      expect(searchConversations).toHaveBeenCalledTimes(1)
      expect(searchConversations).toHaveBeenCalledWith('销售数')
      const hit = wrapper.find('.hit button.title')
      expect(hit.text()).toContain('销售数据')
      await hit.trigger('click')
      expect(open).toHaveBeenCalledWith('c2')
      await input.setValue('')
      expect(wrapper.findAll('.hit')).toHaveLength(0)
      expect(wrapper.findAll('button.title')).toHaveLength(2)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('备份按钮下载对应会话的 zip', async () => {
    const { wrapper } = setup()
    const backup = wrapper.findAll('.actions button').filter((b) => b.text() === '备份')
    expect(backup.length).toBe(2)
    await backup[1].trigger('click')
    expect(downloadSessionArchive).toHaveBeenCalledWith('c2')
  })

  it('导入备份:上传所选文件并打开新会话', async () => {
    const { chat, wrapper } = setup()
    vi.mocked(importSessionArchive).mockResolvedValue({
      id: 'new1', title: '旧会话', message_count: 2, file_count: 0, missing: [], resumable: true, warnings: [],
    })
    const refresh = vi.spyOn(chat, 'refreshConversations').mockResolvedValue()
    const open = vi.spyOn(chat, 'openConversation').mockResolvedValue()
    const input = wrapper.find('input[type=file]')
    const file = new File(['x'], 'b.zip', { type: 'application/zip' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith('new1'))
    expect(importSessionArchive).toHaveBeenCalledWith(file)
    expect(refresh).toHaveBeenCalled()
  })

  it('重命名和删除是按钮,键盘可达;重命名 Enter 提交', async () => {
    const { chat, wrapper } = setup()
    const rename = vi.spyOn(chat, 'rename').mockResolvedValue()
    const actions = wrapper.findAll('.actions button')
    expect(actions.length).toBe(8) // 每个会话 重命名 + 删除 + 导出 + 备份
    expect(actions[0].text()).toBe('重命名')
    expect(actions[1].text()).toBe('删除')

    await actions[0].trigger('click')
    const input = wrapper.find('.item input')
    expect(input.exists()).toBe(true)
    await input.setValue('新名字')
    await input.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(rename).toHaveBeenCalledWith('c1', '新名字')
    wrapper.unmount()
  })

  it('点删除先弹确认,确认后才删除', async () => {
    const { chat, wrapper } = setup()
    const remove = vi.spyOn(chat, 'removeConversation').mockResolvedValue()
    await wrapper.findAll('.actions button')[1].trigger('click')
    await nextTick()
    expect(remove).not.toHaveBeenCalled()
    const confirm = [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === '删除' && b.closest('.n-dialog'))
    expect(confirm).toBeTruthy()
    confirm!.click()
    await vi.waitFor(() => expect(remove).toHaveBeenCalledWith('c1'))
    wrapper.unmount()
  })

  it('列表加载失败显示错误和重试,而不是“还没有对话”', async () => {
    const { chat, wrapper } = setup()
    chat.conversations = []
    chat.conversationsError = 'Unauthorized'
    await nextTick()
    expect(wrapper.text()).toContain('会话列表加载失败:Unauthorized')
    expect(wrapper.text()).not.toContain('还没有对话')
    const refresh = vi.spyOn(chat, 'refreshConversations').mockResolvedValue()
    await wrapper.find('.list-error button').trigger('click')
    expect(refresh).toHaveBeenCalled()
    wrapper.unmount()
  })
})
