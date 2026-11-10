import { createPinia, setActivePinia } from 'pinia'
import { afterEach, expect, it, vi } from 'vitest'
import { useChatStore } from './stores/chat'

afterEach(() => vi.unstubAllGlobals())

it('reconciles an uncertain first send when its retry receives busy', async () => {
  setActivePinia(createPinia())
  let attempts = 0
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    requests.push(path)
    if (path === '/send-message') {
      if (++attempts === 1) throw new TypeError('first response lost, original request still preparing')
      return new Response('{"detail":"session is busy"}', { status: 409 })
    }
    if (path.includes('/requests/')) return new Response('{"id":"original-run","status":"complete"}')
    if (path === '/conversations?limit=100&offset=0') return new Response('[]')
    if (path.includes('/messages')) return new Response('[]')
    if (path.startsWith('/thread-status/')) return new Response('{"status":"running","prompt_id":"original-run"}')
    return new Response('{}')
  }))
  const chat = useChatStore()
  const accepted = await chat.send('question', [])
  console.log({accepted, attempts, receiptRead:requests.some(p=>p.includes('/requests/')), displayedMessages:chat.messages.length})
  chat.dispose()
  expect(accepted).toBe(true)
})
