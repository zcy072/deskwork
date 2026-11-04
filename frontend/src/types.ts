// 与后端 TurnRecorder / SSE 事件对应的类型
import type { TodoItem } from '@/utils/steps'

export interface FileRef {
  file_name: string
  container_path?: string
  /** 交付时的内容摘要与大小;旧记录没有 */
  sha256?: string
  size?: number
}

export interface SourceRef extends FileRef {
  id: string
  container_path: string
  fingerprint: string
  location:
    | { kind: 'page'; page: number }
    | { kind: 'slide'; slide: number }
    | { kind: 'lines'; start: number; end: number }
    | { kind: 'paragraphs'; start: number; end: number }
  excerpt: string
}

export interface Widget {
  title?: string
  html_content: string
  height?: number
}

export type Part =
  | { type: 'text'; text: string }
  | { type: 'tool_call'; id: string | null; name: string | null; args: unknown }
  | {
      type: 'tool_result'
      id?: string | null
      name?: string | null
      content?: unknown
      status?: string
    }
  | { type: 'files'; files: FileRef[] }
  | { type: 'widgets'; widgets: Widget[] }
  | { type: 'sources'; sources: SourceRef[] }
  | { type: 'ask'; question: string; kind: 'single_select' | 'multi_select' | 'free_text'; options: string[] }

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  parts: Part[]
  status?: string
  /** status 为 error 时的错误信息 */
  errorText?: string
  /** 回答已生成但保存到历史记录失败时的提示;消息本身不算出错 */
  warningText?: string
  /** 用户消息附带的文件名,仅用于展示 */
  attachments?: string[]
  /** 用户消息附带的文件(含沙箱路径);来自服务端或重新生成时沿用,刚发送的消息在同步后才有 */
  attachmentFiles?: FileRef[]
  /** 数据库中的消息 id;仅已落库的用户消息有 */
  dbId?: number
  /** 可回退到这条消息之前重新发送 */
  rewindable?: boolean
}

export interface MessageRow {
  id: number
  role: 'user' | 'assistant'
  parts: Part[]
  status: string
  rewindable: boolean
  /** 仅用户消息有 */
  attachments?: FileRef[]
}

export interface ConversationSummary {
  id: string
  title: string
  message_count: number
  created_at: string
  updated_at: string
  /** 运行中以服务端实时状态为准;否则是最近一轮的终态。旧数据为 null */
  status?: RunStatus | null
  /** 该终态用户还没看过 */
  unread?: boolean
}

export type RunStatus = 'running' | 'complete' | 'waiting' | 'error' | 'cancelled'

export interface SearchHit {
  id: string
  title: string
  /** 正文命中的片段;只有标题命中时为空 */
  snippet: string
  updated_at: string
}

export interface StreamPacket {
  id?: string
  event: string
  data: Record<string, any>
}

export interface ConversationUsage {
  turns: number
  llm_calls: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cache_read_tokens: number
  by_model: Record<string, { input_tokens: number; output_tokens: number }>
}

/** 整个会话的文件与计划,消息分页加载时用来补全未加载的部分。 */
export interface ConversationAssets {
  uploaded: FileRef[]
  delivered: FileRef[]
  todos: TodoItem[]
}

export interface MemoryItem {
  id: number
  content: string
  created_at: string
  last_used_at: string
  /** null = 永不过期 */
  expires_at: string | null
}

export interface Capabilities {
  memory: boolean
  memory_confirm_writes: boolean
  mcp_servers: Record<string, string>
}
