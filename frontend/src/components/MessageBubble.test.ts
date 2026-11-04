import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import type { ChatMessage } from '@/types'

vi.mock('@/api/client', () => ({}))
import { useChatStore } from '@/stores/chat'
import MessageBubble from './MessageBubble.vue'

const userMsg = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'u1', role: 'user', parts: [{ type: 'text', text: '原问题' }], dbId: 3, rewindable: true, ...over,
})

function setup(message: ChatMessage) {
  setActivePinia(createPinia())
  const chat = useChatStore()
  chat.messages = [message]
  return { chat, wrapper: mount(MessageBubble, { props: { message }, attachTo: document.body }) }
}

const btn = (w: ReturnType<typeof setup>['wrapper'], label: string) => w.findAll('button').find((b) => b.text() === label)

describe('MessageBubble 复制', () => {
  it('回答中的已登记引用可打开预览,未登记的引用不会打开', async () => {
    setActivePinia(createPinia())
    const source = {
      id: 'known', file_name: 'a.txt', container_path: '/workspace/uploads/a.txt',
      fingerprint: 'a'.repeat(64), excerpt: '预算', location: { kind: 'lines' as const, start: 2, end: 2 },
    }
    const wrapper = mount(MessageBubble, {
      props: { message: { id: 'a', role: 'assistant', parts: [
        { type: 'sources', sources: [source] },
        { type: 'text', text: '[未知](#source-missing) [原文](#source-known)' },
      ] } },
      global: { stubs: { SourcePreview: { name: 'SourcePreviewStub', props: ['source'], template: '<div class="source-dialog">{{ source?.id }}</div>' } } },
    })
    expect(wrapper.findAll('[aria-label="原文来源"] button')).toHaveLength(1)
    await wrapper.find('a[href="#source-missing"]').trigger('click')
    expect(wrapper.find('.source-dialog').exists()).toBe(false)
    await wrapper.find('a[href="#source-known"]').trigger('click')
    expect(wrapper.find('.source-dialog').text()).toBe('known')
    wrapper.findComponent({ name: 'SourcePreviewStub' }).vm.$emit('close')
    await nextTick()
    expect(wrapper.find('.source-dialog').text()).toBe('')
    // 来源只在前一轮的历史消息里时,后续回答引用同一 ID 仍可打开。
    const chat = useChatStore()
    chat.messages = [{ id: 'previous', role: 'assistant', parts: [{ type: 'sources', sources: [source] }] }]
    await wrapper.setProps({ message: { id: 'later', role: 'assistant', parts: [{ type: 'text', text: '[仍用上次来源](#source-known)' }] } })
    await wrapper.find('a[href="#source-known"]').trigger('click')
    expect(wrapper.find('.source-dialog').text()).toBe('known')
    wrapper.unmount()
  })
  it('助手消息可复制正文;代码块带复制按钮并复制代码', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
    const { wrapper } = setup({
      id: 'a',
      role: 'assistant',
      status: 'complete',
      parts: [{ type: 'text', text: '前言\n\n```py\nprint(1)\n```' }],
    })
    const msgBtn = wrapper.findAll('.actions.hover button').find((b) => b.text() === '复制')!
    await msgBtn.trigger('click')
    expect(writeText).toHaveBeenLastCalledWith('前言\n\n```py\nprint(1)\n```')
    const codeBtn = wrapper.find('.md .code-copy')
    expect(codeBtn.exists()).toBe(true)
    await codeBtn.trigger('click')
    expect(writeText).toHaveBeenLastCalledWith('print(1)\n')
    wrapper.unmount()
  })

  it('生成中或没有正文时不显示复制', () => {
    const { wrapper } = setup({ id: 'a', role: 'assistant', parts: [] })
    expect(wrapper.findAll('.actions.hover button').some((b) => b.text() === '复制')).toBe(false)
    wrapper.unmount()
  })
})

describe('MessageBubble 编辑流程', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('编辑:预填原文,发送调用 editAndResend', async () => {
    const { chat, wrapper } = setup(userMsg())
    const edit = vi.spyOn(chat, 'editAndResend').mockResolvedValue(true)
    await btn(wrapper, '编辑')!.trigger('click')
    const ta = wrapper.find('textarea')
    expect(ta.element.value).toBe('原问题')
    await ta.setValue('改后的问题')
    await btn(wrapper, '发送')!.trigger('click')
    expect(edit).toHaveBeenCalledWith('u1', '改后的问题')
    await nextTick()
    expect(wrapper.find('textarea').exists()).toBe(false)
    wrapper.unmount()
  })

  it('编辑重发被拒绝后重新打开编辑框并保留改过的文字', async () => {
    const { chat, wrapper } = setup(userMsg())
    chat.rejectedEdit = { id: 'u1', text: '改后的问题' }
    await nextTick()
    expect(wrapper.find('textarea').element.value).toBe('改后的问题')
    expect(chat.rejectedEdit).toBeNull()
    wrapper.unmount()
  })

  it('取消编辑不发送;内容为空时发送按钮禁用', async () => {
    const { chat, wrapper } = setup(userMsg())
    const edit = vi.spyOn(chat, 'editAndResend').mockResolvedValue(true)
    await btn(wrapper, '编辑')!.trigger('click')
    await wrapper.find('textarea').setValue('  ')
    expect(btn(wrapper, '发送')!.attributes('disabled')).toBeDefined()
    await btn(wrapper, '取消')!.trigger('click')
    expect(edit).not.toHaveBeenCalled()
    expect(wrapper.find('textarea').exists()).toBe(false)
    wrapper.unmount()
  })

  it('重新生成调用 regenerate;未落库或生成中不显示操作', async () => {
    const a = setup(userMsg())
    const regen = vi.spyOn(a.chat, 'regenerate').mockResolvedValue(true)
    await btn(a.wrapper, '重新生成')!.trigger('click')
    expect(regen).toHaveBeenCalledWith('u1')
    a.wrapper.unmount()

    const b = setup(userMsg({ rewindable: false }))
    expect(btn(b.wrapper, '编辑')).toBeUndefined()
    b.wrapper.unmount()

    const c = setup(userMsg())
    c.chat.running = true
    await nextTick()
    expect(btn(c.wrapper, '编辑')).toBeUndefined()
    c.wrapper.unmount()
  })
})
