import { describe, expect, it } from 'vitest'
import { applyPacket, newTurn } from './turn'
import { parseSseChunk, toPacket } from './sse'

describe('run event projection', () => {
  it('keeps wire metadata and deduplicates parsed replay after a snapshot', () => {
    const wire = (seq: number, event: string, data: object) =>
      `data: ${JSON.stringify({ id: `10-${seq}`, run_id: 'run-a', seq, schema_version: 1, event, data })}\n\n`
    const raw = wire(1, 'ai.token', { text: 'hello' }) +
      wire(1, 'ai.token', { text: 'hello' }) +
      wire(2, 'ai.tool_call', { tool_calls: [{ id: 'tool', name: 'view_file', args: {} }] })
    const { packets } = parseSseChunk(raw)
    expect(packets[0]).toMatchObject({ run_id: 'run-a', seq: 1, schema_version: 1 })
    const turn = newTurn()
    for (const packet of packets) applyPacket(turn, packet)
    expect(turn.parts).toEqual([
      { type: 'text', text: 'hello' },
      { type: 'tool_call', id: 'tool', name: 'view_file', args: {} },
    ])
    applyPacket(turn, { event: 'run.snapshot', data: {
      parts: [{ type: 'text', text: 'saved' }], last_event_seq: 4, status: 'running',
    } })
    for (const packet of parseSseChunk(wire(3, 'ai.token', { text: 'old' }) +
      wire(5, 'ai.token', { text: ' new' })).packets) applyPacket(turn, packet)
    expect(turn.parts).toEqual([{ type: 'text', text: 'saved new' }])
    expect(turn.lastSeq).toBe(5)
  })

  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, '2', null, Infinity])(
    'does not use an invalid sequence %s to suppress subsequent events', (seq) => {
      const packet = toPacket({ event: 'ai.token', seq, run_id: 12, schema_version: seq, data: { text: 'legacy' } })!
      expect(packet.seq).toBeUndefined()
      expect(packet.run_id).toBeUndefined()
      expect(packet.schema_version).toBeUndefined()
      const turn = newTurn()
      applyPacket(turn, packet)
      applyPacket(turn, { event: 'ai.token', seq: 1, data: { text: ' next' } })
      expect(turn.parts).toEqual([{ type: 'text', text: 'legacy next' }])
    },
  )

  it('deduplicates replayed events and the streamed tool preamble', () => {
    const turn = newTurn()
    const token = { event: 'ai.token', seq: 1, data: { text: '读取文件' } }
    applyPacket(turn, token)
    applyPacket(turn, token)
    applyPacket(turn, { event: 'ai.tool_call', seq: 2, data: {
      text: '读取文件', tool_calls: [{ id: 'c', name: 'view_file', args: {} }],
    } })
    expect(turn.parts).toEqual([
      { type: 'text', text: '读取文件' },
      { type: 'tool_call', id: 'c', name: 'view_file', args: {} },
    ])
  })

  it('replaces partial presentation with a persisted run snapshot', () => {
    const turn = newTurn()
    applyPacket(turn, { event: 'ai.token', seq: 1, data: { text: 'partial' } })
    applyPacket(turn, { event: 'run.snapshot', data: {
      parts: [{ type: 'text', text: 'saved' }], last_event_seq: 4, status: 'cancelled',
    } })
    applyPacket(turn, { event: 'ai.token', seq: 3, data: { text: 'old replay' } })
    expect(turn.parts).toEqual([{ type: 'text', text: 'saved' }])
    expect(turn.status).toBe('cancelled')
  })

  it('retains the interrupt identity for an explicit answer', () => {
    const turn = newTurn()
    applyPacket(turn, { event: 'ask.user', seq: 1, data: {
      question: '允许?', kind: 'single_select', options: ['允许', '拒绝'], interrupt_id: 'interrupt-1',
    } })
    expect(turn.parts[0]).toMatchObject({ type: 'ask', interrupt_id: 'interrupt-1' })
  })
})
