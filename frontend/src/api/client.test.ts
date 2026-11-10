import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, SendUncertainError, getApiKey, getThreadStatus, onUnauthorized, sendMessage, setApiKey, streamEvents } from './client'

beforeEach(() => localStorage.clear())
afterEach(() => {
  onUnauthorized(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('api key', () => {
  it('页面里填的 Key 会随请求发出,清空后不再发送', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    setApiKey('k1')
    expect(getApiKey()).toBe('k1')
    await getThreadStatus('s')
    expect(fetchMock.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer k1' })

    setApiKey('')
    await getThreadStatus('s')
    expect(fetchMock.mock.calls[1][1].headers).toEqual({})
  })

  it('收到 401 时通知界面', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"no"}', { status: 401 })))
    const handler = vi.fn()
    onUnauthorized(handler)

    await expect(getThreadStatus('s')).rejects.toThrow('no')

    expect(handler).toHaveBeenCalledTimes(1)
  })
})

describe('streamEvents', () => {
  const sse = (...ps: object[]) =>
    new Response(ps.map((p) => `data: ${JSON.stringify(p)}\n\n`).join(''), { status: 200 })
  const fast = { backoffBaseMs: 1, backoffMaxMs: 2 }
  async function collect(gen: AsyncGenerator<any>) {
    const out: any[] = []
    for await (const p of gen) out.push(p)
    return out
  }

  it('restores a terminal run when its Redis stream has expired', async () => {
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response(JSON.stringify({
        id: 'p', session_id: 's', status: 'complete', parts: [{ type: 'text', text: 'saved' }],
        last_event_seq: 8,
      }))
      return new Response('{"detail":"expired"}', { status: 410 })
    })
    vi.stubGlobal('fetch', f)
    const out = await collect(streamEvents('s', 'p', new AbortController().signal, fast))
    expect(out.map((p) => p.event)).toEqual(['run.snapshot', 'stream.end'])
    expect(out[0].data.parts[0].text).toBe('saved')
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('失败后退避重试,并带着 lastId 续读', async () => {
    const f = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('net'))
      .mockResolvedValueOnce(sse({ id: '1-0', event: 'ai.token', data: {} }))
      .mockResolvedValueOnce(sse({ id: '2-0', event: 'stream.end', data: {} }))
    vi.stubGlobal('fetch', f)
    const out = await collect(streamEvents('s', 'p', new AbortController().signal, fast))
    expect(out.map((p) => p.id)).toEqual(['1-0', '2-0'])
    expect(JSON.parse(f.mock.calls[2][1].body).start_id).toBe('1-0')
  })

  it('连续失败且无法读取 Run 时保留错误', async () => {
    const f = vi.fn().mockImplementation(async (url: string) => {
      throw new TypeError('net')
    })
    vi.stubGlobal('fetch', f)
    await expect(collect(streamEvents('s', 'p', new AbortController().signal, { ...fast, maxFailures: 3 }))).rejects.toThrow('net')
    expect(f.mock.calls.filter((c) => c[0] === '/stream-events')).toHaveLength(3)
    expect(f.mock.calls.some((c) => c[0] === '/runs/p')).toBe(true)
  })

  it('耗尽后线程仍在运行则继续尝试', async () => {
    let n = 0
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response('{"id":"p","status":"running"}')
      if (++n <= 2) throw new TypeError('net')
      return sse({ id: '1-0', event: 'stream.end', data: {} })
    })
    vi.stubGlobal('fetch', f)
    const out = await collect(streamEvents('s', 'p', new AbortController().signal, { ...fast, maxFailures: 2 }))
    expect(out).toHaveLength(1)
  })

  it('4xx 不重试', async () => {
    const f = vi.fn().mockResolvedValue(new Response('{"detail":"bad"}', { status: 400 }))
    vi.stubGlobal('fetch', f)
    await expect(collect(streamEvents('s', 'p', new AbortController().signal, fast))).rejects.toThrow('bad')
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('中止后静默结束且不再请求', async () => {
    const ac = new AbortController()
    const f = vi.fn().mockImplementation(async () => {
      ac.abort()
      throw new DOMException('aborted', 'AbortError')
    })
    vi.stubGlobal('fetch', f)
    expect(await collect(streamEvents('s', 'p', ac.signal, fast))).toEqual([])
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('长时间没有字节时重连', async () => {
    const f = vi
      .fn()
      .mockImplementationOnce(async (_u: string, init: RequestInit) => {
        // 永不返回数据的响应,直到被中止
        const body = new ReadableStream({
          start(c) {
            init.signal!.addEventListener('abort', () => c.error(new DOMException('a', 'AbortError')))
          },
        })
        return new Response(body)
      })
      .mockResolvedValueOnce(sse({ id: '1-0', event: 'stream.end', data: {} }))
    vi.stubGlobal('fetch', f)
    const out = await collect(streamEvents('s', 'p', new AbortController().signal, { ...fast, idleTimeoutMs: 20 }))
    expect(out).toHaveLength(1)
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('反复收到空响应时有上限,线程已结束则报错', async () => {
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response('{"detail":"unavailable"}', { status: 503 })
      return new Response('', { status: 200 })
    })
    vi.stubGlobal('fetch', f)
    await expect(
      collect(streamEvents('s', 'p', new AbortController().signal, { ...fast, maxFailures: 2, extraRounds: 0 })),
    ).rejects.toThrow('未收到结束标记')
    expect(f.mock.calls.filter((c) => String(c[0]) === '/stream-events')).toHaveLength(2)
  })

  it('uses the saved run when the terminal stream event was lost', async () => {
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response(JSON.stringify({
        id: 'p', session_id: 's', status: 'complete', parts: [{ type: 'text', text: 'finished' }],
        last_event_seq: 7,
      }))
      return new Response('')
    })
    vi.stubGlobal('fetch', f)
    const result = await collect(streamEvents('s', 'p', new AbortController().signal, { ...fast, maxFailures: 2 }))
    expect(result.map((p) => p.event)).toEqual(['run.snapshot', 'stream.end'])
    expect(result[0].data.parts[0].text).toBe('finished')
  })

  it('阻塞后空响应也会读取本次 Run,不被另一个正在运行的任务干扰', async () => {
    let clock = 0
    vi.spyOn(Date, 'now').mockImplementation(() => clock)
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response(JSON.stringify({
        id: 'p', status: 'error', message: '服务重启', parts: [], last_event_seq: 3,
      }))
      if (url.includes('thread-status')) return new Response('{"status":"running","prompt_id":"different-run"}')
      return new Response(new ReadableStream({
        async pull(controller) {
          await new Promise(r => setTimeout(r, 10))
          clock += 4000 // 模拟阻塞 XREAD 的空响应,头已返回而响应体稍后结束。
          controller.close()
        },
      }))
    })
    vi.stubGlobal('fetch', f)
    const ac = new AbortController()
    const deadline = setTimeout(() => ac.abort(), 2000)
    try {
      const result = await collect(streamEvents('s', 'p', ac.signal, { ...fast, maxFailures: 2 }))
      expect(result.map(p => p.event)).toEqual(['run.snapshot', 'stream.end'])
      expect(result[1].data.status).toBe('error')
      expect(f.mock.calls.filter(c => c[0] === '/stream-events')).toHaveLength(2)
      expect(f.mock.calls.some(c => String(c[0]).includes('thread-status'))).toBe(false)
    } finally {
      clearTimeout(deadline)
    }
  })

  it('长工具调用的空响应不会耗尽网络重试额度', async () => {
    let polls = 0
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response(JSON.stringify({ id: 'p', status: 'running' }))
      if (++polls <= 4) return new Response('')
      return sse({ event: 'stream.end', data: { status: 'complete' } })
    })
    vi.stubGlobal('fetch', f)
    const result = await collect(streamEvents('s', 'p', new AbortController().signal,
      { ...fast, maxFailures: 1, extraRounds: 0 }))
    expect(result).toEqual([{ event: 'stream.end', data: { status: 'complete' } }])
    expect(f.mock.calls.filter(c => c[0] === '/runs/p')).toHaveLength(4)
  })

  it('网络重试耗尽后读取具体 Run 的最终快照', async () => {
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response(JSON.stringify({
        id: 'p', status: 'complete', parts: [{ type: 'text', text: 'saved' }], last_event_seq: 4,
      }))
      throw new TypeError('connection lost')
    })
    vi.stubGlobal('fetch', f)
    const result = await collect(streamEvents('s', 'p', new AbortController().signal,
      { ...fast, maxFailures: 1 }))
    expect(result.map(p => p.event)).toEqual(['run.snapshot', 'stream.end'])
    expect(result[0].data.parts).toEqual([{ type: 'text', text: 'saved' }])
  })

  it('流丢失但正在收尾时继续读取,直到终态落库', async () => {
    let reads = 0
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response(JSON.stringify({
        id: 'p', status: ++reads === 1 ? 'running' : 'cancelled',
        parts: [{ type: 'text', text: reads === 1 ? 'partial' : 'final' }], last_event_seq: reads,
      }))
      return new Response('{"detail":"expired"}', { status: 410 })
    })
    vi.stubGlobal('fetch', f)
    const result = await collect(streamEvents('s', 'p', new AbortController().signal, fast))
    expect(result.map(p => p.event)).toEqual(['run.snapshot', 'run.snapshot', 'stream.end'])
    expect(result.at(-1)?.data.status).toBe('cancelled')
  })

  it('取消等待中的终态查询后不再发布快照或发起请求', async () => {
    const ac = new AbortController()
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') {
        ac.abort()
        return new Response(JSON.stringify({ id: 'p', status: 'complete', parts: [] }))
      }
      return new Response('')
    })
    vi.stubGlobal('fetch', f)
    expect(await collect(streamEvents('s', 'p', ac.signal, { ...fast, maxFailures: 1 }))).toEqual([])
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('丢流且始终无法保存终态时停止重试并保留明确错误', async () => {
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (url === '/runs/p') return new Response(JSON.stringify({ id: 'p', status: 'running', parts: [] }))
      return new Response('{"detail":"expired"}', { status: 410 })
    })
    vi.stubGlobal('fetch', f)
    await expect(collect(streamEvents('s', 'p', new AbortController().signal,
      { ...fast, extraRounds: 1 }))).rejects.toThrow('事件流已丢失')
    expect(f.mock.calls.filter(c => c[0] === '/runs/p')).toHaveLength(2)
  })

  it('持续无字节的超时也会查询已保存的终态', async () => {
    const f = vi.fn().mockImplementation(async (url: string, init: RequestInit) => {
      if (url === '/runs/p') return new Response(JSON.stringify({ id: 'p', status: 'complete', parts: [] }))
      return new Response(new ReadableStream({ start(controller) {
        init.signal!.addEventListener('abort', () => controller.error(new DOMException('timeout', 'AbortError')))
      } }))
    })
    vi.stubGlobal('fetch', f)
    const result = await collect(streamEvents('s', 'p', new AbortController().signal,
      { ...fast, maxFailures: 1, idleTimeoutMs: 20 }))
    expect(result.map(p => p.event)).toEqual(['run.snapshot', 'stream.end'])
    expect(f).toHaveBeenCalledTimes(2)
  })
})

describe('sendMessage', () => {
  const ok = () => new Response('{"prompt_id":"p1"}', { status: 200 })

  it('网络错误后用同一个请求 ID 重试', async () => {
    const fetchMock = vi.fn().mockRejectedValueOnce(new TypeError('network')).mockResolvedValueOnce(ok())
    vi.stubGlobal('fetch', fetchMock)

    expect(await sendMessage('s', 'hi', [])).toBe('p1')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    const ids = fetchMock.mock.calls.map((c) => (c[1].body as FormData).get('client_request_id'))
    expect(ids[0]).toBeTruthy()
    expect(ids[1]).toBe(ids[0])
  })

  it('服务端明确拒绝时不重试', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"detail":"busy"}', { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(sendMessage('s', 'hi', [])).rejects.toBeInstanceOf(ApiError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('结果未知后重试被拒绝,不当作原请求被拒绝', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce(new Response('{"detail":"busy"}', { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)

    const error = await sendMessage('s', 'hi', []).catch((e) => e)

    expect(error).toBeInstanceOf(SendUncertainError)
    expect(error.retryRejected).toBe(true)
    expect(error.requestId).toBe((fetchMock.mock.calls[0][1].body as FormData).get('client_request_id'))
  })

  it('不同的发送使用不同的请求 ID', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => ok())
    vi.stubGlobal('fetch', fetchMock)

    await sendMessage('s', 'a', [])
    await sendMessage('s', 'a', [])

    const ids = fetchMock.mock.calls.map((c) => (c[1].body as FormData).get('client_request_id'))
    expect(ids[0]).not.toBe(ids[1])
  })
})
