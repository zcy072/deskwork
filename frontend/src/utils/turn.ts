import type { Part, StreamPacket } from '@/types'

export interface TurnState {
  parts: Part[]
  /** 自上一次工具调用以来是否已经流式输出过正文 */
  streamedText: boolean
  error: string | null
  /** 回答已完整生成但没存进历史记录;不算生成失败 */
  warning: string | null
  lastSeq: number
  status?: string
}

export function newTurn(): TurnState {
  return { parts: [], streamedText: false, error: null, warning: null, lastSeq: 0 }
}

function appendText(turn: TurnState, text: string): void {
  if (!text) return
  const last = turn.parts[turn.parts.length - 1]
  if (last && last.type === 'text') last.text += text
  else turn.parts.push({ type: 'text', text })
}

/** 把一个 SSE 事件应用到当前轮次(与后端 TurnRecorder 的口径一致)。 */
export function applyPacket(turn: TurnState, packet: StreamPacket): void {
  const { event, data } = packet
  if (event === 'run.snapshot') {
    turn.parts = data.parts ?? []
    turn.lastSeq = data.last_event_seq ?? 0
    turn.status = data.status
    turn.error = data.status === 'error' ? data.message ?? '本次生成已中断' : null
    return
  }
  if (packet.seq !== undefined) {
    if (packet.seq <= turn.lastSeq) return
    turn.lastSeq = packet.seq
  }
  switch (event) {
    case 'ai.token':
      appendText(turn, data.text ?? '')
      turn.streamedText = true
      break
    case 'ai.tool_call':
      // 正文已经通过 ai.token 流式输出过,不再重复
      if (!turn.streamedText) appendText(turn, data.text ?? '')
      for (const call of data.tool_calls ?? []) {
        turn.parts.push({ type: 'tool_call', id: call.id, name: call.name, args: call.args })
      }
      turn.streamedText = false
      break
    case 'tool.result':
      turn.parts.push({ type: 'tool_result', ...data })
      break
    case 'files.presented':
      turn.parts.push({ type: 'files', files: data.files ?? [] })
      break
    case 'widget.presented':
      turn.parts.push({ type: 'widgets', widgets: data.widgets ?? [] })
      break
    case 'sources.presented':
      turn.parts.push({ type: 'sources', sources: data.sources ?? [] })
      break
    case 'ask.user':
      turn.parts.push({
        type: 'ask',
        question: data.question ?? '',
        kind: data.kind ?? 'single_select',
        options: data.options ?? [],
        interrupt_id: data.interrupt_id,
      })
      break
    case 'error':
      if (data.code === 'history_save_failed') turn.warning = data.message ?? '保存到历史记录失败'
      else turn.error = data.message ?? '未知错误'
      break
    case 'stream.end':
      turn.status = data.status
      break
  }
}
