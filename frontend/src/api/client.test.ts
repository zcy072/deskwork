import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getApiKey, getThreadStatus, onUnauthorized, sendMessage, setApiKey, streamEvents } from './client'

beforeEach(() => localStorage.clear())
afterEach(() => {
  onUnauthorized(null)
  vi.unstubAllGlobals()
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

  it('连续失败耗尽后查询状态:已结束则抛错', async () => {
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (String(url).includes('thread-status')) return new Response('{"status":"idle","prompt_id":null}')
      throw new TypeError('net')
    })
    vi.stubGlobal('fetch', f)
    await expect(collect(streamEvents('s', 'p', new AbortController().signal, { ...fast, maxFailures: 3 }))).rejects.toThrow('net')
    expect(f.mock.calls.filter((c) => !String(c[0]).includes('thread-status'))).toHaveLength(3)
  })

  it('耗尽后线程仍在运行则继续尝试', async () => {
    let n = 0
    const f = vi.fn().mockImplementation(async (url: string) => {
      if (String(url).includes('thread-status')) return new Response('{"status":"running","prompt_id":"p"}')
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
      if (String(url).includes('thread-status')) return new Response('{"status":"complete","prompt_id":null}')
      return new Response('', { status: 200 })
    })
    vi.stubGlobal('fetch', f)
    await expect(
      collect(streamEvents('s', 'p', new AbortController().signal, { ...fast, maxFailures: 2 })),
    ).rejects.toThrow('未收到结束标记')
    expect(f.mock.calls.filter((c) => !String(c[0]).includes('thread-status'))).toHaveLength(2)
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

  it('不同的发送使用不同的请求 ID', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => ok())
    vi.stubGlobal('fetch', fetchMock)

    await sendMessage('s', 'a', [])
    await sendMessage('s', 'a', [])

    const ids = fetchMock.mock.calls.map((c) => (c[1].body as FormData).get('client_request_id'))
    expect(ids[0]).not.toBe(ids[1])
  })
})
