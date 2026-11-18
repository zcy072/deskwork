import type { StreamPacket } from '@/types'

/** 最小形状检查:必须是带字符串 event 的对象;data 缺失或不是对象时补成空对象,id 非字符串则丢弃。 */
export function toPacket(v: unknown): StreamPacket | null {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return null
  const o = v as Record<string, unknown>
  if (typeof o.event !== 'string') return null
  const data = typeof o.data === 'object' && o.data !== null && !Array.isArray(o.data) ? (o.data as Record<string, any>) : {}
  const packet: StreamPacket = { event: o.event, data }
  if (typeof o.id === 'string') packet.id = o.id
  if (typeof o.run_id === 'string' && o.run_id) packet.run_id = o.run_id
  if (typeof o.seq === 'number' && Number.isSafeInteger(o.seq) && o.seq > 0) packet.seq = o.seq
  if (typeof o.schema_version === 'number' && Number.isSafeInteger(o.schema_version) && o.schema_version > 0) {
    packet.schema_version = o.schema_version
  }
  return packet
}

/** 把累积的文本按行切开,返回解析出的事件和尚未结束的残余文本。 */
export function parseSseChunk(buffer: string): { packets: StreamPacket[]; rest: string } {
  const lines = buffer.split('\n')
  const rest = lines.pop() ?? ''
  const packets: StreamPacket[] = []
  for (const raw of lines) {
    const line = raw.replace(/\r$/, '')
    if (!line.startsWith('data:')) continue
    // 规范允许冒号后带一个可选空格
    const payload = line.slice(line[5] === ' ' ? 6 : 5)
    try {
      const packet = toPacket(JSON.parse(payload))
      if (packet) packets.push(packet)
    } catch {
      // 忽略无法解析的行
    }
  }
  return { packets, rest }
}
