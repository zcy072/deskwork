import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MessageRow, StreamPacket } from '@/types'

vi.mock('@/api/client', () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message)
    }
  },
  SendUncertainError: class SendUncertainError extends Error {
    constructor(public requestId: string, cause: unknown, public retryRejected = false) { super(String(cause)) }
  },
  getAcceptedRun: vi.fn(),
  sendMessage: vi.fn(),
  streamEvents: vi.fn(),
  cancelGeneration: vi.fn(),
  listConversations: vi.fn(),
  getMessages: vi.fn(),
  getUsage: vi.fn(),
  getConversationAssets: vi.fn(),
  getThreadStatus: vi.fn(),
  renameConversation: vi.fn(),
  deleteConversation: vi.fn(),
  markConversationRead: vi.fn(),
  deleteSessionFile: vi.fn(),
  listSessionFiles: vi.fn(),
}))

import * as api from '@/api/client'
import { useChatStore } from './chat'

const m = vi.mocked(api)

function row(id: number, role: 'user' | 'assistant', text: string): MessageRow {
  return { id, role, parts: [{ type: 'text', text }], status: 'complete', rewindable: role === 'user' }
}

async function* packets(...list: StreamPacket[]) {
  for (const p of list) yield p
}

const token = (text: string): StreamPacket => ({ event: 'ai.token', data: { text } })
const end: StreamPacket = { event: 'stream.end', data: {} }

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  m.listConversations.mockResolvedValue([])
  m.getUsage.mockRejectedValue(new Error('no usage'))
  m.getConversationAssets.mockRejectedValue(new Error('no assets'))
  m.getMessages.mockResolvedValue([])
  m.getAcceptedRun.mockResolvedValue(null)
  m.getThreadStatus.mockResolvedValue(null)
  m.markConversationRead.mockResolvedValue(undefined)
  m.listSessionFiles.mockRejectedValue(new Error('no list'))
})

describe('send', () => {
  it('只合并当前 Run 的事件,不会让其他 Run 的大序号吞掉正文', async () => {
    m.sendMessage.mockResolvedValue('own-run')
    m.streamEvents.mockReturnValue(packets(
      { event: 'ai.token', run_id: 'other-run', seq: 100, data: { text: 'foreign' } },
      { event: 'ai.token', run_id: 'own-run', seq: 1, data: { text: 'own answer' } },
      { event: 'stream.end', run_id: 'own-run', seq: 2, data: { status: 'complete' } },
    ))
    const chat = useChatStore()
    await chat.send('question', [])
    expect(chat.messages.at(-1)?.parts).toEqual([{ type: 'text', text: 'own answer' }])
    chat.dispose()
  })
  it('成功删除会话清理草稿,删除失败保留草稿', async () => {
    const chat = useChatStore()
    chat.draftFor('A').text = '保留的草稿'
    chat.draftFor('A').files.push(new File(['a'], 'a.txt'))
    m.deleteConversation.mockRejectedValueOnce(new Error('delete failed'))
    await chat.removeConversation('A')
    expect(chat.draftFor('A').text).toBe('保留的草稿')
    m.deleteConversation.mockResolvedValueOnce(undefined)
    await chat.removeConversation('A')
    expect(chat.draftFor('A')).toEqual({ text: '', files: [], lostFiles: [] })
  })
  it('流式内容写入助手消息并补上用户消息 id', async () => {
    m.sendMessage.mockResolvedValue('p1')
    m.streamEvents.mockReturnValue(packets(token('你'), token('好'), end))
    m.getMessages.mockResolvedValue([row(7, 'user', 'hi'), row(8, 'assistant', '你好')])
    const chat = useChatStore()

    expect(await chat.send('hi', [])).toBe(true)

    expect(chat.running).toBe(false)
    expect(chat.messages).toHaveLength(2)
    expect(chat.messages[1].parts).toEqual([{ type: 'text', text: '你好' }])
    expect(chat.messages[1].status).toBe('completed')
    expect(chat.messages[0].dbId).toBe(7)
  })

  it('服务端拒绝时恢复原对话并返回 false', async () => {
    m.sendMessage.mockRejectedValue(new Error('正在生成'))
    const chat = useChatStore()
    chat.messages = [{ id: 'a', role: 'user', parts: [{ type: 'text', text: '旧' }] }]

    expect(await chat.send('新', [])).toBe(false)

    expect(chat.messages.map((x) => x.id)).toEqual(['a'])
    expect(chat.error).toBe('正在生成')
    expect(chat.running).toBe(false)
  })

  it('网络错误但服务端已在生成时接回那一轮', async () => {
    m.sendMessage.mockRejectedValue(new api.SendUncertainError('request-1', 'Failed to fetch'))
    m.getAcceptedRun.mockResolvedValue({ status: 'running', id: 'p9' })
    m.streamEvents.mockReturnValue(packets(token('回来了'), end))
    const chat = useChatStore()

    expect(await chat.send('问', [])).toBe(true)

    expect(m.streamEvents).toHaveBeenCalledWith(expect.anything(), 'p9', expect.anything())
    expect(chat.messages[1].parts).toEqual([{ type: 'text', text: '回来了' }])
    expect(chat.error).toBeNull()
  })

  it('网络错误且服务端没有在生成时按未接受处理', async () => {
    m.sendMessage.mockRejectedValue(new api.SendUncertainError('request-1', 'Failed to fetch'))
    m.getThreadStatus.mockResolvedValue(null)
    const chat = useChatStore()
    chat.messages = [{ id: 'a', role: 'user', parts: [{ type: 'text', text: '旧' }] }]

    expect(await chat.send('新', [])).toBe(false)

    expect(chat.messages.map((x) => x.id)).toEqual(['a'])
    expect(m.streamEvents).not.toHaveBeenCalled()
  })

  it('网络错误、服务端已记下这条消息并跑完时算已接受', async () => {
    m.sendMessage.mockRejectedValue(new api.SendUncertainError('request-1', 'Failed to fetch'))
    m.getThreadStatus.mockResolvedValue(null)
    m.getAcceptedRun.mockResolvedValue({ id: 'p9', status: 'complete' })
    const rows = [
      { id: 5, role: 'user' as const, parts: [{ type: 'text' as const, text: '旧' }], status: 'done', rewindable: true },
      { id: 6, role: 'assistant' as const, parts: [], status: 'done', rewindable: false },
      { id: 7, role: 'user' as const, parts: [{ type: 'text' as const, text: '新' }], status: 'done', rewindable: true },
      { id: 8, role: 'assistant' as const, parts: [{ type: 'text' as const, text: '答' }], status: 'done', rewindable: false },
    ]
    m.getMessages.mockResolvedValue(rows)
    const chat = useChatStore()
    chat.messages = [
      { id: '5', role: 'user', parts: [{ type: 'text', text: '旧' }], dbId: 5 },
      { id: '6', role: 'assistant', parts: [] },
    ]

    expect(await chat.send('新', [])).toBe(true)

    expect(m.streamEvents).not.toHaveBeenCalled()
    expect(chat.messages.map((x) => x.id)).toEqual(['5', '6', '7', '8'])
    expect(chat.error).toBeNull()
  })

  it('网络错误、服务端最新的用户消息是发送前就有的时按未接受处理', async () => {
    m.sendMessage.mockRejectedValue(new api.SendUncertainError('request-1', 'Failed to fetch'))
    m.getThreadStatus.mockResolvedValue(null)
    m.getMessages.mockResolvedValue([
      { id: 5, role: 'user', parts: [{ type: 'text', text: '新' }], status: 'done', rewindable: true },
    ])
    const chat = useChatStore()
    chat.messages = [{ id: '5', role: 'user', parts: [{ type: 'text', text: '新' }], dbId: 5 }]

    expect(await chat.send('新', [])).toBe(false)
    expect(chat.messages.map((x) => x.id)).toEqual(['5'])
  })

  it('重试被拒绝后按原请求 ID 查回执并接回那一轮', async () => {
    m.sendMessage.mockRejectedValue(new api.SendUncertainError('request-1', new Error('busy'), true))
    m.getAcceptedRun.mockResolvedValueOnce(null).mockResolvedValueOnce({ status: 'running', id: 'p9' })
    m.streamEvents.mockReturnValue(packets(token('回来了'), end))
    const chat = useChatStore()

    expect(await chat.send('问', [])).toBe(true)

    expect(m.getAcceptedRun).toHaveBeenCalledWith(expect.anything(), 'request-1')
    expect(m.streamEvents).toHaveBeenCalledWith(expect.anything(), 'p9', expect.anything())
    expect(chat.error).toBeNull()
  })

  it('重试被拒绝且回执一直查不到时保持未知,不把消息退回', async () => {
    vi.useFakeTimers()
    try {
      m.sendMessage.mockRejectedValue(new api.SendUncertainError('request-1', new Error('busy'), true))
      const chat = useChatStore()
      chat.messages = [{ id: 'a', role: 'user', parts: [{ type: 'text', text: '旧' }] }]

      const sent = chat.send('新', [])
      await vi.runAllTimersAsync()

      expect(await sent).toBe(true)
      expect(m.getAcceptedRun.mock.calls.length).toBeGreaterThan(1)
      expect(chat.messages.map((x) => x.parts)).toContainEqual([{ type: 'text', text: '新' }])
      expect(chat.error).toContain('无法确认')
    } finally {
      vi.useRealTimers()
    }
  })

  it('服务端明确拒绝时不查线程状态', async () => {
    m.sendMessage.mockRejectedValue(new api.ApiError(409, '正在生成'))
    const chat = useChatStore()
    expect(await chat.send('新', [])).toBe(false)
    expect(m.getThreadStatus).not.toHaveBeenCalled()
  })

  it('重新生成被拒绝时被丢弃的消息会还原', async () => {
    m.sendMessage.mockRejectedValue(new Error('不能回退'))
    const chat = useChatStore()
    chat.messages = [
      { id: 'u', role: 'user', parts: [{ type: 'text', text: 'q' }], dbId: 3, rewindable: true },
      { id: 'a', role: 'assistant', parts: [{ type: 'text', text: 'ans' }] },
    ]

    expect(await chat.regenerate('u')).toBe(false)

    expect(chat.messages.map((x) => x.id)).toEqual(['u', 'a'])
  })

  it('生成期间忽略再次发送', async () => {
    let release!: () => void
    m.sendMessage.mockReturnValue(new Promise((r) => (release = () => r('p1'))))
    m.streamEvents.mockReturnValue(packets(end))
    const chat = useChatStore()

    const first = chat.send('a', [])
    expect(await chat.send('b', [])).toBe(false)
    release()
    await first

    expect(m.sendMessage).toHaveBeenCalledTimes(1)
  })

  it('空消息不发送', async () => {
    const chat = useChatStore()
    expect(await chat.send('  ', [])).toBe(false)
    expect(m.sendMessage).not.toHaveBeenCalled()
  })

  it('助手没有任何输出时不留空消息', async () => {
    m.sendMessage.mockResolvedValue('p1')
    m.streamEvents.mockReturnValue(packets(end))
    const chat = useChatStore()

    await chat.send('hi', [])

    expect(chat.messages.map((x) => x.role)).toEqual(['user'])
  })
})

describe('openConversation', () => {
  it.each(['partial', 'no-assistant'])('补齐历史与状态查询之间完成的回答: %s', async (initial) => {
    const user = { ...row(1, 'user', 'q'), prompt_id: 'p' }
    const partial = { ...row(2, 'assistant', 'partial'), prompt_id: 'p', status: 'running' }
    const complete = { ...partial, parts: [{ type: 'text' as const, text: 'complete answer' }], status: 'complete' }
    m.getMessages.mockResolvedValueOnce(initial === 'partial' ? [user, partial] : [user])
      .mockResolvedValue([user, complete])
    m.getThreadStatus.mockResolvedValue({ status: 'complete', prompt_id: null })
    const chat = useChatStore()
    await chat.openConversation('S')
    expect(chat.messages[1].parts).toEqual(complete.parts)
    expect(chat.messages[1].status).toBe('complete')
    expect(chat.running).toBe(false)
    expect(m.getMessages).toHaveBeenCalledTimes(2)
    chat.dispose()
  })

  it('补读最终历史时切换会话,旧回答不覆盖新会话', async () => {
    let release!: (rows: MessageRow[]) => void
    m.getMessages.mockResolvedValueOnce([row(1, 'assistant', 'partial')])
      .mockImplementationOnce(() => new Promise(r => { release = r }))
      .mockResolvedValue([row(3, 'assistant', 'B answer')])
    m.getThreadStatus.mockResolvedValue({ status: 'complete', prompt_id: null })
    const chat = useChatStore()
    const opening = chat.openConversation('A')
    await vi.waitFor(() => expect(release).toBeDefined())
    await chat.openConversation('B')
    release([row(2, 'assistant', 'A complete')])
    await opening
    expect(chat.sessionId).toBe('B')
    expect(chat.messages[0].parts).toEqual([{ type: 'text', text: 'B answer' }])
    chat.dispose()
  })

  it('补读最终历史期间发送新消息,旧快照不覆盖正在生成的回答', async () => {
    let release!: (rows: MessageRow[]) => void
    m.getMessages.mockResolvedValueOnce([row(1, 'user', 'old')])
      .mockImplementationOnce(() => new Promise(r => { release = r }))
      .mockResolvedValue([row(1, 'user', 'old'), row(2, 'user', 'new')])
    m.getThreadStatus.mockResolvedValue({ status: 'complete', prompt_id: null })
    let finishStream!: () => void
    m.sendMessage.mockResolvedValue('new-run')
    m.streamEvents.mockImplementation(async function* () {
      yield token('new answer')
      await new Promise<void>(r => { finishStream = r })
      yield end
    })
    const chat = useChatStore()
    const opening = chat.openConversation('S')
    await vi.waitFor(() => expect(release).toBeDefined())
    const sending = chat.send('new', [])
    await vi.waitFor(() => expect(finishStream).toBeDefined())
    release([row(1, 'assistant', 'old complete')])
    await opening
    expect(chat.running).toBe(true)
    expect(chat.messages.at(-1)?.parts).toEqual([{ type: 'text', text: 'new answer' }])
    finishStream()
    await sending
    chat.dispose()
  })

  it('补读历史期间新一轮已经完成,迟到的旧快照仍不能覆盖它', async () => {
    let release!: (rows: MessageRow[]) => void
    m.getMessages.mockResolvedValueOnce([row(1, 'user', 'old')])
      .mockImplementationOnce(() => new Promise(r => { release = r }))
      .mockResolvedValue([row(1, 'user', 'old'), row(2, 'user', 'new')])
    m.getThreadStatus.mockResolvedValue({ status: 'complete', prompt_id: null })
    m.sendMessage.mockResolvedValue('new-run')
    m.streamEvents.mockReturnValue(packets(token('new answer'), end))
    const chat = useChatStore()
    const opening = chat.openConversation('S')
    await vi.waitFor(() => expect(release).toBeDefined())
    await chat.send('new', [])
    expect(chat.running).toBe(false)
    release([row(1, 'assistant', 'stale answer')])
    await opening
    expect(chat.messages.at(-1)?.parts).toEqual([{ type: 'text', text: 'new answer' }])
    chat.dispose()
  })

  it('较早发出的请求后返回时不会覆盖后点开的会话', async () => {
    let releaseA!: (rows: MessageRow[]) => void
    m.getMessages.mockImplementation((id: string) =>
      id === 'A' ? new Promise((r) => (releaseA = r)) : Promise.resolve([row(1, 'user', 'B 的消息')]),
    )
    const chat = useChatStore()

    const a = chat.openConversation('A')
    await chat.openConversation('B')
    releaseA([row(2, 'user', 'A 的消息')])
    await a

    expect(chat.sessionId).toBe('B')
    expect(chat.messages[0].parts).toEqual([{ type: 'text', text: 'B 的消息' }])
  })

  it('会话仍在生成时接回事件流', async () => {
    m.getMessages.mockResolvedValue([row(1, 'user', '做个表格')])
    m.getThreadStatus.mockResolvedValue({ status: 'running', prompt_id: 'p9' })
    m.streamEvents.mockReturnValue(packets(token('进行中'), end))
    const chat = useChatStore()

    await chat.openConversation('S')

    expect(m.streamEvents).toHaveBeenCalledWith('S', 'p9', expect.anything())
    expect(chat.messages.map((x) => x.role)).toEqual(['user', 'assistant'])
    expect(chat.messages[1].parts).toEqual([{ type: 'text', text: '进行中' }])
    expect(chat.running).toBe(false)
  })

  it('接回时丢弃已落库的半条助手消息,避免和回放重复', async () => {
    m.getMessages.mockResolvedValue([
      row(1, 'user', '做个表格'),
      { ...row(2, 'assistant', '写到一半'), status: 'running' },
    ])
    m.getThreadStatus.mockResolvedValue({ status: 'running', prompt_id: 'p9' })
    m.streamEvents.mockReturnValue(packets(token('写到一半'), token('完成'), end))
    const chat = useChatStore()

    await chat.openConversation('S')

    expect(chat.messages.map((x) => x.role)).toEqual(['user', 'assistant'])
    expect(chat.messages[1].parts).toEqual([{ type: 'text', text: '写到一半完成' }])
  })

  it('会话已结束时不接事件流', async () => {
    m.getMessages.mockResolvedValue([row(1, 'user', 'q'), row(2, 'assistant', 'a')])
    m.getThreadStatus.mockResolvedValue({ status: 'complete', prompt_id: null })
    const chat = useChatStore()

    await chat.openConversation('S')

    expect(m.streamEvents).not.toHaveBeenCalled()
    expect(chat.messages).toHaveLength(2)
  })

  it('查询状态失败不影响打开历史', async () => {
    m.getMessages.mockResolvedValue([row(1, 'user', 'q')])
    m.getThreadStatus.mockRejectedValue(new Error('boom'))
    const chat = useChatStore()

    await chat.openConversation('S')

    expect(chat.messages).toHaveLength(1)
    expect(m.streamEvents).not.toHaveBeenCalled()
  })
})

describe('生成中切换会话', () => {
  /** 发出一条消息并让流停在中途,返回 send 的 promise 与流的 signal */
  async function startHanging(chat: ReturnType<typeof useChatStore>) {
    m.sendMessage.mockResolvedValue('p1')
    let signal!: AbortSignal
    m.streamEvents.mockImplementationOnce((_s, _p, sig) => {
      signal = sig
      return (async function* () {
        yield token('a')
        await new Promise<void>((r) => sig.addEventListener('abort', () => r()))
      })()
    })
    const p = chat.send('hi', [])
    await vi.waitFor(() => expect(chat.messages[1]?.parts.length).toBe(1))
    return { p, signal: () => signal }
  }

  it('切走只中止本地流,不取消服务端生成,并标记为后台生成', async () => {
    const chat = useChatStore()
    const gen = chat.sessionId
    const { p, signal } = await startHanging(chat)
    m.getMessages.mockResolvedValue([row(7, 'user', 'old'), row(8, 'assistant', 'x')])
    m.getThreadStatus.mockResolvedValue({ status: 'idle', prompt_id: null })

    await chat.openConversation('other')
    await p

    expect(signal().aborted).toBe(true)
    expect(m.cancelGeneration).not.toHaveBeenCalled()
    expect(chat.sessionId).toBe('other')
    expect(chat.running).toBe(false)
    expect(chat.isGenerating(gen)).toBe(true)
    expect(chat.isGenerating('other')).toBe(false)
    // 旧流的收尾不能改动当前会话的消息
    expect(chat.messages.map((x) => x.id)).toEqual(['7', '8'])
  })

  it('新对话不再被生成中阻止', async () => {
    const chat = useChatStore()
    const gen = chat.sessionId
    const { p } = await startHanging(chat)
    chat.newConversation()
    await p
    expect(chat.sessionId).not.toBe(gen)
    expect(chat.messages).toEqual([])
    expect(chat.running).toBe(false)
    expect(chat.isGenerating(gen)).toBe(true)
  })

  it('切回仍在生成的会话时接回事件流并清除后台标记', async () => {
    const chat = useChatStore()
    const gen = chat.sessionId
    const { p } = await startHanging(chat)
    m.getMessages.mockResolvedValue([row(7, 'user', 'old')])
    m.getThreadStatus.mockResolvedValue({ status: 'idle', prompt_id: null })
    await chat.openConversation('other')
    await p

    m.getMessages.mockResolvedValue([row(1, 'user', 'hi')])
    m.getThreadStatus.mockResolvedValue({ status: 'running', prompt_id: 'p1' })
    m.streamEvents.mockReturnValue(packets(token('回放'), end))
    await chat.openConversation(gen)

    expect(m.streamEvents).toHaveBeenLastCalledWith(gen, 'p1', expect.anything())
    expect(chat.isGenerating(gen)).toBe(false) // 流已结束
    expect(chat.backgroundIds).toEqual([])
    expect(chat.messages[1].parts).toEqual([{ type: 'text', text: '回放' }])
  })

  it('切回时服务端已结束的会话会清除后台标记', async () => {
    const chat = useChatStore()
    const gen = chat.sessionId
    const { p } = await startHanging(chat)
    m.getMessages.mockResolvedValue([])
    m.getThreadStatus.mockResolvedValue({ status: 'idle', prompt_id: null })
    await chat.openConversation('other')
    await p
    await chat.openConversation(gen)
    expect(chat.backgroundIds).toEqual([])
  })
})

describe('conversation management', () => {
  it('删除失败时保留当前会话并给出错误', async () => {
    m.deleteConversation.mockRejectedValue(new Error('删除失败'))
    const chat = useChatStore()
    const id = chat.sessionId
    chat.messages = [{ id: 'a', role: 'user', parts: [] }]

    await chat.removeConversation(id)

    expect(chat.sessionId).toBe(id)
    expect(chat.messages).toHaveLength(1)
    expect(chat.error).toBe('删除失败')
  })

  it('删除当前会话后切换到新会话', async () => {
    m.deleteConversation.mockResolvedValue()
    const chat = useChatStore()
    const id = chat.sessionId
    chat.messages = [{ id: 'a', role: 'user', parts: [] }]

    await chat.removeConversation(id)

    expect(chat.sessionId).not.toBe(id)
    expect(chat.messages).toEqual([])
  })
})

describe('error status and delete during run', () => {
  it('流里带 error 事件时助手消息为 error 并带错误文本', async () => {
    m.sendMessage.mockResolvedValue('p1')
    m.streamEvents.mockReturnValue(
      packets(token('x'), { event: 'error', data: { message: '炸了' } } as StreamPacket, end),
    )
    const chat = useChatStore()
    await chat.send('hi', [])
    expect(chat.messages[1].status).toBe('error')
    expect(chat.messages[1].errorText).toBe('炸了')
    expect(chat.error).toBe('炸了')
  })

  it('保存历史失败只作为提示,回答本身不算出错', async () => {
    m.sendMessage.mockResolvedValue('p1')
    m.streamEvents.mockReturnValue(
      packets(token('完整回答'), { event: 'error', data: { code: 'history_save_failed', message: '保存失败' } } as StreamPacket, end),
    )
    const chat = useChatStore()
    await chat.send('hi', [])
    expect(chat.messages[1].status).toBe('completed')
    expect(chat.messages[1].warningText).toBe('保存失败')
    expect(chat.messages[1].errorText).toBeUndefined()
    expect(chat.error).toBeNull()
  })

  it('取消且无错误时为 cancelled', async () => {
    m.sendMessage.mockResolvedValue('p1')
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    m.streamEvents.mockReturnValue(
      (async function* () {
        yield token('a')
        await gate
        yield end
      })(),
    )
    m.cancelGeneration.mockResolvedValue()
    const chat = useChatStore()
    const p = chat.send('hi', [])
    await vi.waitFor(() => expect(chat.messages[1]?.parts.length).toBe(1))
    await chat.stop()
    release()
    await p
    expect(chat.messages[1].status).toBe('cancelled')
  })

  it('运行中删除当前会话会中止流、复位状态且不对新会话同步', async () => {
    m.sendMessage.mockResolvedValue('p1')
    m.deleteConversation.mockResolvedValue()
    let signal!: AbortSignal
    m.streamEvents.mockImplementation((_s, _p, sig) => {
      signal = sig
      return (async function* () {
        yield token('a')
        await new Promise<void>((r) => sig.addEventListener('abort', () => r()))
      })()
    })
    const chat = useChatStore()
    const old = chat.sessionId
    const p = chat.send('hi', [])
    await vi.waitFor(() => expect(chat.messages[1]?.parts.length).toBe(1))

    await chat.removeConversation(old)
    expect(signal.aborted).toBe(true)
    expect(chat.running).toBe(false)
    expect(chat.sessionId).not.toBe(old)
    await p
    expect(m.getMessages).not.toHaveBeenCalled()
    expect(m.getUsage).not.toHaveBeenCalled()
    expect(chat.messages).toEqual([])
  })

  it('运行中删除失败时不中止事件流', async () => {
    m.sendMessage.mockResolvedValue('p1')
    m.deleteConversation.mockRejectedValue(new Error('删除失败'))
    let signal!: AbortSignal
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    m.streamEvents.mockImplementation((_s, _p, sig) => {
      signal = sig
      return (async function* () {
        yield token('a')
        await gate
        yield end
      })()
    })
    const chat = useChatStore()
    const id = chat.sessionId
    const p = chat.send('hi', [])
    await vi.waitFor(() => expect(chat.messages[1]?.parts.length).toBe(1))

    await chat.removeConversation(id)
    expect(signal.aborted).toBe(false)
    expect(chat.running).toBe(true)
    expect(chat.sessionId).toBe(id)
    expect(chat.error).toBe('删除失败')
    release()
    await p
    expect(chat.messages[1].status).toBe('completed')
  })

  it('删除请求期间服务端取消导致流结束时,不再同步被删的会话', async () => {
    m.sendMessage.mockResolvedValue('p1')
    let finishDelete!: () => void
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    m.deleteConversation.mockImplementation(() => {
      release() // 服务端先取消生成,事件流随之结束,DELETE 稍后才返回
      return new Promise<void>((r) => (finishDelete = r))
    })
    m.streamEvents.mockReturnValue(
      (async function* () {
        yield token('a')
        await gate
        yield end
      })(),
    )
    const chat = useChatStore()
    const id = chat.sessionId
    const p = chat.send('hi', [])
    await vi.waitFor(() => expect(chat.messages[1]?.parts.length).toBe(1))

    const removing = chat.removeConversation(id)
    await p
    expect(m.getMessages).not.toHaveBeenCalled()
    expect(m.getUsage).not.toHaveBeenCalled()
    finishDelete()
    await removing
    expect(chat.sessionId).not.toBe(id)
  })
})

describe('editAndResend / regenerate', () => {
  async function loaded() {
    m.getMessages.mockResolvedValue([row(1, 'user', 'q1'), row(2, 'assistant', 'a1')])
    const chat = useChatStore()
    await chat.openConversation('c1')
    return chat
  }

  it('编辑重发带上原消息 id 并丢弃之后的内容', async () => {
    const chat = await loaded()
    m.sendMessage.mockResolvedValue('p2')
    m.streamEvents.mockReturnValue(packets(token('新'), end))
    m.getMessages.mockResolvedValue([row(5, 'user', 'q2'), row(6, 'assistant', '新')])
    expect(await chat.editAndResend(chat.messages[0].id, 'q2')).toBe(true)
    expect(m.sendMessage).toHaveBeenCalledWith('c1', 'q2', [], 1)
    expect(chat.messages.map((x) => x.role)).toEqual(['user', 'assistant'])
    expect(chat.messages[0].dbId).toBe(5)
  })

  it('编辑内容为空或消息不可回退时不发送', async () => {
    const chat = await loaded()
    expect(await chat.editAndResend(chat.messages[0].id, '  ')).toBe(false)
    expect(await chat.editAndResend('nope', 'x')).toBe(false)
    expect(m.sendMessage).not.toHaveBeenCalled()
  })

  it('编辑重发被服务端拒绝时恢复原对话,并把改过的文字交还编辑框', async () => {
    const chat = await loaded()
    m.sendMessage.mockRejectedValue(new api.ApiError(429, '太快了'))
    const id = chat.messages[0].id
    expect(await chat.editAndResend(id, '改过的')).toBe(false)
    expect(chat.messages.map((x) => x.id)).toEqual([id, '2'])
    expect(chat.rejectedEdit).toEqual({ id, text: '改过的' })
  })

  it('只有附件的用户消息重新生成:发空 query、沿用原附件,并在界面上标注', async () => {
    m.getMessages.mockResolvedValue([
      { id: 1, role: 'user', parts: [], status: 'complete', rewindable: true },
      row(2, 'assistant', 'a'),
    ])
    const chat = useChatStore()
    await chat.openConversation('c1')
    m.sendMessage.mockResolvedValue('p2')
    m.streamEvents.mockReturnValue(packets(token('x'), end))
    await chat.regenerate(chat.messages[0].id)
    expect(m.sendMessage).toHaveBeenCalledWith('c1', '', [], 1)
    expect(chat.messages[0].attachments).toEqual(['(沿用原附件)'])
  })
})

describe('会话列表分页', () => {
  const conv = (i: number) => ({ id: `c${i}`, title: `t${i}`, message_count: 1, created_at: '', updated_at: '' })
  const page = (from: number, n: number) => Array.from({ length: n }, (_, k) => conv(from + k))

  it('满页时还有更多,翻页追加并按 id 去重', async () => {
    m.listConversations.mockResolvedValueOnce(page(0, 100))
    const chat = useChatStore()
    await chat.refreshConversations()
    expect(chat.conversations).toHaveLength(100)
    expect(chat.conversationsHasMore).toBe(true)

    m.listConversations.mockResolvedValueOnce([conv(99), ...page(100, 5)]) // 99 因插入而重复出现
    await chat.loadMoreConversations()
    expect(m.listConversations).toHaveBeenLastCalledWith(100, 100)
    expect(chat.conversations).toHaveLength(105)
    expect(chat.conversationsHasMore).toBe(false)
  })

  it('刷新时覆盖已翻出的条数,不丢掉旧会话', async () => {
    m.listConversations.mockResolvedValueOnce(page(0, 100))
    const chat = useChatStore()
    await chat.refreshConversations()
    m.listConversations.mockResolvedValueOnce(page(100, 20))
    await chat.loadMoreConversations()
    expect(chat.conversations).toHaveLength(120)

    m.listConversations.mockImplementation(async (_l = 100, o = 0) => page(o, o === 0 ? 100 : 20))
    await chat.refreshConversations()
    expect(chat.conversations).toHaveLength(120)
    expect(chat.conversationsHasMore).toBe(false)
  })
})

describe('附件', () => {
  it('重新打开会话时用户消息带上服务端返回的附件', async () => {
    const files = [{ file_name: 'a.xlsx', container_path: '/workspace/uploads/a.xlsx' }]
    m.getMessages.mockResolvedValue([
      { ...row(1, 'user', 'q'), attachments: files },
      row(2, 'assistant', 'a'),
    ])
    const chat = useChatStore()
    await chat.openConversation('c1')
    expect(chat.messages[0].attachments).toEqual(['a.xlsx'])
    expect(chat.messages[0].attachmentFiles).toEqual(files)
    expect(chat.messages[1].attachmentFiles).toBeUndefined()
    expect(chat.uploadedFiles).toEqual(files)
  })
})

describe('同步用户消息 id', () => {
  it('条数对不上时按服务端重新载入,使编辑/重新生成可用', async () => {
    m.sendMessage.mockResolvedValue('p1')
    m.streamEvents.mockReturnValue(packets(token('a'), end))
    m.getMessages.mockResolvedValue([row(1, 'user', 'old'), row(2, 'assistant', 'x'), row(3, 'user', 'hi'), row(4, 'assistant', 'a')])
    const chat = useChatStore()
    await chat.send('hi', [])
    expect(chat.messages).toHaveLength(4)
    expect(chat.messages.find((x) => x.dbId === 3)?.rewindable).toBe(true)
  })
})

describe('加载状态', () => {
  it('切换会话期间 loadingConversation 为 true,完成后复位;失败也复位', async () => {
    let resolve!: (r: MessageRow[]) => void
    m.getMessages.mockReturnValueOnce(new Promise((r) => (resolve = r)))
    const chat = useChatStore()
    const p = chat.openConversation('c1')
    expect(chat.loadingConversation).toBe(true)
    resolve([])
    await p
    expect(chat.loadingConversation).toBe(false)

    m.getMessages.mockRejectedValueOnce(new Error('boom'))
    await chat.openConversation('c2')
    expect(chat.loadingConversation).toBe(false)
    expect(chat.error).toBe('boom')
  })

  it('会话列表失败记录在 conversationsError,重试成功后清除', async () => {
    m.listConversations.mockRejectedValueOnce(new Error('401 unauthorized'))
    const chat = useChatStore()
    await chat.refreshConversations()
    expect(chat.conversationsError).toBe('401 unauthorized')
    expect(chat.error).toBeNull()
    await chat.refreshConversations()
    expect(chat.conversationsError).toBeNull()
  })
})

describe('stop', () => {
  it('取消请求失败时复位 cancelling 并报错;未在运行时不动作', async () => {
    const chat = useChatStore()
    await chat.stop()
    expect(m.cancelGeneration).not.toHaveBeenCalled()

    m.sendMessage.mockResolvedValue('p1')
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    m.streamEvents.mockReturnValue(
      (async function* () {
        yield token('a')
        await gate
        yield end
      })(),
    )
    m.cancelGeneration.mockRejectedValue(new Error('网络'))
    const p = chat.send('hi', [])
    await vi.waitFor(() => expect(chat.messages[1]?.parts.length).toBe(1))
    await chat.stop()
    expect(chat.cancelling).toBe(false)
    expect(chat.error).toBe('网络')
    release()
    await p
  })
})

describe('后台状态与未读', () => {
  const summary = (id: string, status: any, unread = false) => ({
    id, title: id, message_count: 2, created_at: '', updated_at: '', status, unread,
  })

  it('刷新页面后列表里仍在运行的会话进入后台追踪', async () => {
    m.listConversations.mockResolvedValue([summary('A', 'running'), summary('B', 'complete')])
    const chat = useChatStore()
    await chat.refreshConversations()
    expect(chat.backgroundIds).toEqual(['A'])
    expect(chat.isGenerating('A')).toBe(true)
    expect(chat.isGenerating('B')).toBe(false)
  })

  it('多个后台会话先后结束,各自的标记被清除并刷新列表', async () => {
    vi.useFakeTimers()
    try {
      m.listConversations.mockResolvedValue([summary('A', 'running'), summary('B', 'running')])
      const chat = useChatStore()
      await chat.refreshConversations()
      expect(chat.backgroundIds).toEqual(['A', 'B'])

      m.getThreadStatus.mockImplementation(async (id: string) =>
        id === 'A' ? { status: 'complete', prompt_id: null } : { status: 'running', prompt_id: 'p' },
      )
      m.listConversations.mockResolvedValue([summary('A', 'complete', true), summary('B', 'running')])
      await vi.advanceTimersByTimeAsync(4000)
      expect(chat.backgroundIds).toEqual(['B'])
      expect(chat.conversations.find((c) => c.id === 'A')).toMatchObject({ status: 'complete', unread: true })

      m.getThreadStatus.mockResolvedValue({ status: 'error', prompt_id: null })
      m.listConversations.mockResolvedValue([summary('A', 'complete', true), summary('B', 'error', true)])
      await vi.advanceTimersByTimeAsync(4000)
      expect(chat.backgroundIds).toEqual([])
      expect(chat.conversations.find((c) => c.id === 'B')).toMatchObject({ status: 'error', unread: true })
    } finally {
      vi.useRealTimers()
    }
  })

  it('打开时没查到状态的运行中会话,结束后重新载入消息', async () => {
    vi.useFakeTimers()
    try {
      m.listConversations.mockResolvedValue([summary('A', 'running')])
      const chat = useChatStore()
      await chat.refreshConversations()
      m.getMessages.mockResolvedValue([row(1, 'user', 'q')])
      m.getThreadStatus.mockRejectedValue(new Error('offline'))
      await chat.openConversation('A')
      expect(chat.backgroundIds).toEqual(['A'])
      expect(chat.messages).toHaveLength(1)

      m.getThreadStatus.mockResolvedValue({ status: 'complete', prompt_id: null })
      m.getMessages.mockResolvedValue([row(1, 'user', 'q'), row(2, 'assistant', '答')])
      m.listConversations.mockResolvedValue([summary('A', 'complete')])
      await vi.advanceTimersByTimeAsync(4000)
      expect(chat.backgroundIds).toEqual([])
      expect(chat.messages.map((x) => x.role)).toEqual(['user', 'assistant'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('打开有未读结果的会话时清除未读', async () => {
    m.listConversations.mockResolvedValue([summary('A', 'complete', true)])
    const chat = useChatStore()
    await chat.refreshConversations()
    await chat.openConversation('A')
    expect(m.markConversationRead).toHaveBeenCalledWith('A')
    expect(chat.conversations[0].unread).toBe(false)
  })

  it('标记已读失败不影响打开会话', async () => {
    m.markConversationRead.mockRejectedValue(new Error('offline'))
    const chat = useChatStore()
    await chat.openConversation('A')
    expect(chat.sessionId).toBe('A')
    expect(chat.error).toBeNull()
  })

  it('当前会话刷新列表时若带着未读,也会被清除', async () => {
    const chat = useChatStore()
    await chat.openConversation('A')
    m.markConversationRead.mockClear()
    m.listConversations.mockResolvedValue([summary('A', 'complete', true)])
    await chat.refreshConversations()
    expect(m.markConversationRead).toHaveBeenCalledWith('A')
    expect(chat.conversations[0].unread).toBe(false)
  })
})

describe('草稿持久化', () => {
  beforeEach(() => localStorage.clear())
  const summary = (id: string) => ({ id, title: id, updated_at: '' }) as any

  it('输入后写入存储,新 store(模拟刷新)可恢复', () => {
    const chat = useChatStore()
    chat.draftFor().text = '没发完的话'
    chat.persistDraft()
    setActivePinia(createPinia())
    expect(useChatStore().draftFor().text).toBe('没发完的话')
  })

  it('打字时攒一下再写入;flushDrafts 立即写入', () => {
    vi.useFakeTimers()
    try {
      const chat = useChatStore()
      chat.draftFor().text = 'a'
      chat.queueDraftSave()
      chat.draftFor().text = 'ab'
      chat.queueDraftSave()
      expect(localStorage.length).toBe(0)
      vi.advanceTimersByTime(300)
      setActivePinia(createPinia())
      expect(useChatStore().draftFor().text).toBe('ab')

      setActivePinia(createPinia())
      const again = useChatStore()
      again.draftFor().text = 'abc'
      again.queueDraftSave()
      again.flushDrafts()
      setActivePinia(createPinia())
      expect(useChatStore().draftFor().text).toBe('abc')
    } finally {
      vi.useRealTimers()
    }
  })

  it('附件名随草稿保存,刷新后作为待重选附件返回,内容不恢复', () => {
    const chat = useChatStore()
    const d = chat.draftFor()
    d.text = '看看这个'
    d.files.push(new File(['a'], 'report.pdf'))
    chat.persistDraft()
    setActivePinia(createPinia())
    const fresh = useChatStore().draftFor()
    expect(fresh.text).toBe('看看这个')
    expect(fresh.files).toEqual([])
    expect(fresh.lostFiles).toEqual(['report.pdf'])
  })

  it('已有会话的草稿按会话隔离', async () => {
    m.listConversations.mockResolvedValue([summary('c1'), summary('c2')])
    const chat = useChatStore()
    await chat.refreshConversations()
    chat.draftFor('c1').text = 'one'
    chat.persistDraft('c1')
    setActivePinia(createPinia())
    const fresh = useChatStore()
    await fresh.refreshConversations()
    expect(fresh.draftFor('c1').text).toBe('one')
    expect(fresh.draftFor('c2').text).toBe('')
  })

  it('从搜索打开不在列表页里的会话,草稿不占用新对话的槽位', async () => {
    const chat = useChatStore()
    chat.draftFor().text = '新对话草稿'
    chat.persistDraft()
    await chat.openConversation('old')
    expect(chat.draftFor().text).toBe('')
    chat.draftFor().text = '旧会话草稿'
    chat.persistDraft()
    setActivePinia(createPinia())
    expect(useChatStore().draftFor().text).toBe('新对话草稿')
  })

  it('删除会话时清掉草稿', async () => {
    m.listConversations.mockResolvedValue([summary('c1')])
    m.deleteConversation.mockResolvedValue()
    const chat = useChatStore()
    await chat.refreshConversations()
    chat.draftFor('c1').text = 'x'
    chat.persistDraft('c1')
    await chat.removeConversation('c1')
    setActivePinia(createPinia())
    expect(useChatStore().draftFor('c1').text).toBe('')
  })

  it('存储不可用时仍可用,只是不持久', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    const chat = useChatStore()
    chat.draftFor().text = 'x'
    expect(() => chat.persistDraft()).not.toThrow()
    expect(chat.draftFor().text).toBe('x')
    spy.mockRestore()
  })
})

describe('分页加载', () => {
  const page = (from: number, to: number) =>
    Array.from({ length: to - from + 1 }, (_, i) => row(from + i, i % 2 ? 'assistant' : 'user', `m${from + i}`))

  it('满一页时 hasEarlier 为真,loadEarlier 把更早的消息接到前面', async () => {
    m.getMessages.mockResolvedValueOnce(page(41, 80))
    const chat = useChatStore()
    await chat.openConversation('A')
    expect(chat.hasEarlier).toBe(true)

    m.getMessages.mockResolvedValueOnce(page(1, 40))
    await chat.loadEarlier()

    expect(m.getMessages).toHaveBeenLastCalledWith('A', 40, 41)
    expect(chat.messages).toHaveLength(80)
    expect(chat.messages[0].id).toBe('1')
    expect(chat.hasEarlier).toBe(true)
  })

  it('不足一页说明已到最早', async () => {
    m.getMessages.mockResolvedValueOnce(page(41, 80))
    const chat = useChatStore()
    await chat.openConversation('A')
    m.getMessages.mockResolvedValueOnce(page(38, 40))
    await chat.loadEarlier()
    expect(chat.hasEarlier).toBe(false)
  })

  it('没有更早消息时 loadEarlier 不发请求', async () => {
    m.getMessages.mockResolvedValueOnce([row(1, 'user', 'a')])
    const chat = useChatStore()
    await chat.openConversation('A')
    m.getMessages.mockClear()
    await chat.loadEarlier()
    expect(m.getMessages).not.toHaveBeenCalled()
  })

  it('未加载部分的上传文件、交付文件和计划由 assets 补全', async () => {
    m.getMessages.mockResolvedValueOnce(page(41, 80))
    m.getConversationAssets.mockResolvedValue({
      uploaded: [{ file_name: 'in.pdf', container_path: '/u/in.pdf' }],
      delivered: [{ file_name: 'out.docx', container_path: '/o/out.docx' }],
      todos: [{ content: '第一步', status: 'pending' }],
    })
    const chat = useChatStore()
    await chat.openConversation('A')
    await vi.waitFor(() => expect(chat.sharedFiles.map((f) => f.file_name)).toEqual(['out.docx']))
    expect(chat.uploadedFiles.map((f) => f.file_name)).toEqual(['in.pdf'])
    expect(chat.todos).toHaveLength(1)
  })
})

describe('removeFile', () => {
  const doc = { file_name: 'a.docx', container_path: '/workspace/output/a.docx' }
  const up = { file_name: 'u.csv', container_path: '/workspace/uploads/u.csv' }

  async function open() {
    m.getMessages.mockResolvedValue([
      { id: 1, role: 'user', parts: [], status: 'complete', rewindable: true, attachments: [up] },
      { id: 2, role: 'assistant', parts: [{ type: 'files', files: [doc] }], status: 'complete', rewindable: false },
    ])
    const chat = useChatStore()
    await chat.openConversation('c1')
    return chat
  }

  it('按相对路径删除,成功后从面板隐藏,聊天里的卡片保持', async () => {
    m.deleteSessionFile.mockResolvedValue(2048)
    const chat = await open()
    expect(await chat.removeFile(doc)).toBe(2048)
    expect(m.deleteSessionFile).toHaveBeenCalledWith('c1', 'output/a.docx')
    expect(chat.sharedFiles).toEqual([])
    expect(chat.uploadedFiles).toEqual([up])
    expect(chat.messages[1].parts[0]).toMatchObject({ type: 'files' })
  })

  it('删除失败时文件仍在面板里', async () => {
    m.deleteSessionFile.mockRejectedValue(new Error('会话正在生成'))
    const chat = await open()
    await expect(chat.removeFile(up)).rejects.toThrow('会话正在生成')
    expect(chat.uploadedFiles).toEqual([up])
  })

  it('刷新后按磁盘现状隐藏已删除的文件,没被检查的路径不动', async () => {
    m.getMessages.mockResolvedValue([
      { id: 1, role: 'user', parts: [], status: 'complete', rewindable: true, attachments: [up] },
      { id: 2, role: 'assistant', parts: [{ type: 'files', files: [doc, { file_name: 'x', container_path: '/workspace/scratchpad/x' }] }], status: 'complete', rewindable: false },
    ])
    m.listSessionFiles.mockResolvedValue(['uploads/u.csv'])
    const chat = useChatStore()
    await chat.openConversation('c1')
    await vi.waitFor(() => expect(chat.sharedFiles.map((f) => f.file_name)).toEqual(['x']))
    expect(chat.uploadedFiles).toEqual([up])
  })

  it('切换会话后隐藏记录清空', async () => {
    m.deleteSessionFile.mockResolvedValue(1)
    const chat = await open()
    await chat.removeFile(doc)
    await chat.openConversation('c2')
    expect(chat.sharedFiles).toEqual([doc])
  })
})
