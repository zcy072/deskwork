import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

vi.mock('@/api/client', () => ({}))
import { useChatStore } from '@/stores/chat'
import ChatView from './ChatView.vue'

function setup() {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const wrapper = mount(ChatView, { attachTo: document.body })
  return { chat, wrapper }
}

const textarea = (w: ReturnType<typeof setup>['wrapper']) => w.find('textarea')

describe('ChatView 输入框', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    HTMLElement.prototype.scrollTo = vi.fn()
  })

  it('Enter 发送;输入法组合中的 Enter(isComposing / keyCode 229)不发送', async () => {
    const { chat, wrapper } = setup()
    const send = vi.spyOn(chat, 'send').mockResolvedValue(true)
    const ta = textarea(wrapper)
    await ta.setValue('你好')

    await ta.trigger('keydown', { key: 'Enter', isComposing: true })
    await ta.trigger('keydown', { key: 'Enter', keyCode: 229 })
    await ta.trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(send).not.toHaveBeenCalled()

    await ta.trigger('keydown', { key: 'Enter' })
    expect(send).toHaveBeenCalledWith('你好', [])
    wrapper.unmount()
  })

  it('生成结束时把焦点还给输入框,但不抢走别处输入框的焦点', async () => {
    const { chat, wrapper } = setup()
    chat.running = true
    await nextTick()
    chat.running = false
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(textarea(wrapper).element)

    const other = document.createElement('input')
    document.body.appendChild(other)
    other.focus()
    chat.running = true
    await nextTick()
    chat.running = false
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(other)
    wrapper.unmount()
  })

  it('拖入文件加入待发送列表;生成中忽略;纯文本拖拽不触发提示', async () => {
    const { chat, wrapper } = setup()
    const f = new File(['x'], 'a.xlsx')
    const section = wrapper.find('section.chat')
    await section.trigger('dragenter', { dataTransfer: { types: ['text/plain'] } })
    expect(wrapper.find('.drop-hint').exists()).toBe(false)
    await section.trigger('dragenter', { dataTransfer: { types: ['Files'] } })
    expect(wrapper.find('.drop-hint').exists()).toBe(true)
    await section.trigger('drop', { dataTransfer: { types: ['Files'], files: [f] } })
    expect(wrapper.find('.drop-hint').exists()).toBe(false)
    expect(wrapper.text()).toContain('a.xlsx')

    chat.running = true
    await section.trigger('drop', { dataTransfer: { types: ['Files'], files: [new File(['y'], 'b.docx')] } })
    expect(wrapper.text()).not.toContain('b.docx')
    wrapper.unmount()
  })

  it('粘贴截图:重命名为带时间戳的文件并阻止默认;纯文本粘贴不拦截', async () => {
    const { wrapper } = setup()
    const ta = textarea(wrapper)
    const shot = new File(['png'], 'image.png', { type: 'image/png' })
    const ev = new Event('paste', { bubbles: true, cancelable: true }) as any
    ev.clipboardData = { files: [shot] }
    ta.element.dispatchEvent(ev)
    await nextTick()
    expect(ev.defaultPrevented).toBe(true)
    expect(wrapper.text()).toMatch(/screenshot-\d{8}\d{6}-1\.png/)

    const text = new Event('paste', { bubbles: true, cancelable: true }) as any
    text.clipboardData = { files: [] }
    ta.element.dispatchEvent(text)
    expect(text.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('发送被拒绝时把输入还回输入框', async () => {
    const { chat, wrapper } = setup()
    vi.spyOn(chat, 'send').mockResolvedValue(false)
    const ta = textarea(wrapper)
    await ta.setValue('别丢了我')
    await ta.trigger('keydown', { key: 'Enter' })
    await vi.waitFor(() => expect(textarea(wrapper).element.value).toBe('别丢了我'))
    wrapper.unmount()
  })

  it('发送被接受后输入框清空;空白内容不发送', async () => {
    const { chat, wrapper } = setup()
    const send = vi.spyOn(chat, 'send').mockResolvedValue(true)
    const ta = textarea(wrapper)
    await ta.setValue('   ')
    await ta.trigger('keydown', { key: 'Enter' })
    expect(send).not.toHaveBeenCalled()
    await ta.setValue('hi')
    await ta.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(textarea(wrapper).element.value).toBe('')
    wrapper.unmount()
  })

  it('加载会话时显示骨架而不是空状态', async () => {
    const { chat, wrapper } = setup()
    expect(wrapper.find('.hero').exists()).toBe(true)
    chat.loadingConversation = true
    await nextTick()
    expect(wrapper.find('.hero').exists()).toBe(false)
    expect(wrapper.find('[role="status"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('切换会话隔离文字与附件,切回来恢复各自的草稿', async () => {
    const { chat, wrapper } = setup()
    const original = chat.sessionId
    await textarea(wrapper).setValue('A 的草稿')
    const f = new File(['a'], 'only-a.txt')
    await wrapper.find('section.chat').trigger('drop', { dataTransfer: { files: [f] } })
    chat.newConversation()
    await nextTick()
    expect(textarea(wrapper).element.value).toBe('')
    expect(wrapper.text()).not.toContain('only-a.txt')
    await textarea(wrapper).setValue('B 的草稿')
    const second = chat.sessionId
    chat.sessionId = original
    await nextTick()
    expect(textarea(wrapper).element.value).toBe('A 的草稿')
    expect(wrapper.text()).toContain('only-a.txt')
    chat.sessionId = second
    await nextTick()
    expect(textarea(wrapper).element.value).toBe('B 的草稿')
    wrapper.unmount()
  })

  it('发送被拒绝时恢复到原会话,不污染期间切换到的会话', async () => {
    const { chat, wrapper } = setup()
    const original = chat.sessionId
    let rejectSend!: (accepted: boolean) => void
    vi.spyOn(chat, 'send').mockReturnValue(new Promise((r) => (rejectSend = r)))
    await textarea(wrapper).setValue('发送失败的 A')
    await wrapper.find('section.chat').trigger('drop', {
      dataTransfer: { files: [new File(['a'], 'a.txt')] },
    })
    await textarea(wrapper).trigger('keydown', { key: 'Enter' })
    chat.newConversation()
    await nextTick()
    await textarea(wrapper).setValue('正在编辑 B')
    rejectSend(false)
    await nextTick()
    expect(textarea(wrapper).element.value).toBe('正在编辑 B')
    expect(wrapper.text()).not.toContain('a.txt')
    chat.sessionId = original
    await nextTick()
    expect(textarea(wrapper).element.value).toBe('发送失败的 A')
    expect(wrapper.text()).toContain('a.txt')
    wrapper.unmount()
  })
})

describe('ChatView 向上滚动加载', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    HTMLElement.prototype.scrollTo = vi.fn()
  })

  it('滚到顶部且有更早消息时自动加载;不在顶部或没有更早消息时不加载', async () => {
    const { chat, wrapper } = setup()
    const load = vi.spyOn(chat, 'loadEarlier').mockResolvedValue()
    const scroller = wrapper.find('.messages')
    const el = scroller.element as HTMLElement

    chat.hasEarlier = true
    el.scrollTop = 300
    await scroller.trigger('scroll')
    expect(load).not.toHaveBeenCalled()

    el.scrollTop = 0
    await scroller.trigger('scroll')
    expect(load).toHaveBeenCalledTimes(1)

    chat.hasEarlier = false
    await scroller.trigger('scroll')
    expect(load).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})

describe('ChatView 待重选附件提示', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    localStorage.clear()
    HTMLElement.prototype.scrollTo = vi.fn()
  })

  it('刷新前的附件名显示为提示,点“知道了”后消失并清出存储', async () => {
    setActivePinia(createPinia())
    const first = useChatStore()
    first.draftFor().files.push(new File(['a'], 'report.pdf'))
    first.persistDraft()

    setActivePinia(createPinia())
    const wrapper = mount(ChatView, { attachTo: document.body })
    expect(wrapper.find('.lost').text()).toContain('report.pdf')

    await wrapper.find('.lost button').trigger('click')
    expect(wrapper.find('.lost').exists()).toBe(false)
    setActivePinia(createPinia())
    expect(useChatStore().draftFor().lostFiles).toEqual([])
    wrapper.unmount()
  })
})
