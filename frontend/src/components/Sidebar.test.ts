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

/** 打开第 i 个会话的“更多”菜单并点其中一项 */
async function pickAction(wrapper: ReturnType<typeof mount>, i: number, label: string) {
  await wrapper.findAll('.actions .more')[i].trigger('click')
  await nextTick()
  const opt = await vi.waitFor(() => {
    const o = [...document.querySelectorAll('.n-dropdown-option')].find((el) => el.textContent?.trim() === label)
    if (!o) throw new Error(`菜单项 ${label} 未出现`)
    return o as HTMLElement
  })
  opt.querySelector<HTMLElement>('.n-dropdown-option-body')!.click()
  await nextTick()
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
    expect(wrapper.findAll('.actions .more').length).toBe(2)
    await pickAction(wrapper, 1, '备份(zip)')
    expect(downloadSessionArchive).toHaveBeenCalledWith('c2', '会话二')
  })

  it('导入备份:上传所选文件并打开新会话', async () => {
    const { chat, wrapper } = setup()
    vi.mocked(importSessionArchive).mockResolvedValue({
      id: 'new1', title: '旧会话', message_count: 2, file_count: 0, missing: [],
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

  it('操作收在“更多”按钮菜单里;重命名 Enter 提交', async () => {
    const { chat, wrapper } = setup()
    const rename = vi.spyOn(chat, 'rename').mockResolvedValue()
    const more = wrapper.findAll('.actions button')
    expect(more.length).toBe(2) // 每个会话只有一个“更多”按钮,标题不被挤占
    expect(more[0].attributes('aria-label')).toBe('更多操作')

    await pickAction(wrapper, 0, '重命名')
    const input = wrapper.find('.item input')
    expect(input.exists()).toBe(true)
    await input.setValue('新名字')
    await input.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(rename).toHaveBeenCalledWith('c1', '新名字')
    wrapper.unmount()
  })

  it('改名时按 Esc 放弃,不提交;没改动也不提交', async () => {
    const { chat, wrapper } = setup()
    const rename = vi.spyOn(chat, 'rename').mockResolvedValue()
    await pickAction(wrapper, 0, '重命名')
    const input = wrapper.find('.item input')
    await input.setValue('不要的名字')
    await input.trigger('keydown', { key: 'Escape' })
    await input.trigger('blur')
    await nextTick()
    expect(wrapper.find('.item input').exists()).toBe(false)

    await pickAction(wrapper, 0, '重命名')
    await wrapper.find('.item input').trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(rename).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('标题为空的会话显示“未命名对话”', async () => {
    const { chat, wrapper } = setup()
    chat.conversations = [{ id: 'c3', title: '', message_count: 1, created_at: '', updated_at: '' }]
    await nextTick()
    expect(wrapper.find('.item .title').text()).toBe('未命名对话')
    wrapper.unmount()
  })

  it('点删除先弹确认,确认后才删除', async () => {
    const { chat, wrapper } = setup()
    const remove = vi.spyOn(chat, 'removeConversation').mockResolvedValue()
    await pickAction(wrapper, 0, '删除')
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
