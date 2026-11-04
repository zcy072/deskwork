import type { FileRef, Part, Widget } from '@/types'

export interface TodoItem {
  content: string
  status: 'pending' | 'in_progress' | 'completed' | string
}

export type AskPart = Extract<Part, { type: 'ask' }>
export type ToolResultPart = Extract<Part, { type: 'tool_result' }>

export interface Step {
  kind: 'step'
  id: string | null
  name: string
  args: unknown
  /** 没有结果表示仍在执行 */
  result?: ToolResultPart
}

export type Item =
  | { kind: 'text'; text: string }
  | Step
  | { kind: 'files'; files: FileRef[] }
  | { kind: 'widgets'; widgets: Widget[] }
  | { kind: 'ask'; question: string; askKind: AskPart['kind']; options: string[] }

/** 把 tool_call 与同 id 的 tool_result 配成一个步骤,其余 part 原样保留。 */
export function groupParts(parts: Part[]): Item[] {
  const items: Item[] = []
  const pending = new Map<string, Step>()
  for (const p of parts) {
    // 提问由 AskCard 呈现,不再重复显示成一个"执行中"的步骤
    if ((p.type === 'tool_call' || p.type === 'tool_result') && p.name === 'ask_user_input') continue
    if (p.type === 'text') items.push({ kind: 'text', text: p.text })
    else if (p.type === 'tool_call') {
      const step: Step = { kind: 'step', id: p.id, name: p.name ?? '未知工具', args: p.args }
      items.push(step)
      if (p.id) pending.set(p.id, step)
    } else if (p.type === 'tool_result') {
      const step = p.id ? pending.get(p.id) : undefined
      if (step) step.result = p
      else items.push({ kind: 'step', id: p.id ?? null, name: p.name ?? '未知工具', args: null, result: p })
    } else if (p.type === 'files') items.push({ kind: 'files', files: p.files })
    else if (p.type === 'widgets') items.push({ kind: 'widgets', widgets: p.widgets })
    else if (p.type === 'ask') items.push({ kind: 'ask', question: p.question, askKind: p.kind, options: p.options })
  }
  return items
}

/** 取最后一次 write_todos 调用的参数作为当前计划。 */
export function latestTodos(parts: Part[]): TodoItem[] {
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i]
    if (p.type === 'tool_call' && p.name === 'write_todos') {
      const todos = (p.args as { todos?: TodoItem[] } | null)?.todos
      if (Array.isArray(todos)) return todos
    }
  }
  return []
}

const TOOL_LABELS: Record<string, string> = {
  bash_tool: '执行命令',
  view_file: '查看文件',
  search_document: '检索文档',
  web_search: '联网搜索',
  web_fetch: '抓取网页',
  str_replace: '替换文本',
  create_file: '创建文件',
  insert: '插入内容',
  present_files: '交付文件',
  write_todos: '更新计划',
  read_todos: '读取计划',
  display_widget: '生成可视化',
  task: '委派子任务',
  ask_user_input: '向你确认',
  remember: '写入记忆',
  recall: '查询记忆',
  forget: '删除记忆',
}

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name
}

/** 一句话摘要,让折叠状态下也能看出这一步在做什么。 */
export function stepSummary(step: Step): string {
  const a = (step.args ?? {}) as Record<string, unknown>
  const pick = (a.description ?? a.command ?? a.path ?? a.file_path) as unknown
  if (typeof pick === 'string') return pick.length > 60 ? pick.slice(0, 60) + '…' : pick
  return ''
}
