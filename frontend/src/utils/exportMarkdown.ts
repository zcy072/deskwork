import type { FileRef, MessageRow } from '@/types'
import { groupParts, stepSummary, toolLabel } from '@/utils/steps'
import { sourceLabel } from '@/utils/sources'

const oneLine = (s: string) => s.replace(/\s+/g, ' ').trim()

/** 把一个会话导出为 Markdown:正文、步骤摘要(工具名 + 参数摘要)与文件清单。 */
export function conversationToMarkdown(title: string, rows: MessageRow[]): string {
  const out: string[] = [`# ${oneLine(title) || '未命名对话'}`]
  const uploaded = new Map<string, FileRef>()
  const delivered = new Map<string, FileRef>()
  const exportedSources = new Set<string>()
  const key = (f: FileRef) => f.container_path ?? f.file_name

  for (const m of rows) {
    out.push(m.role === 'user' ? '## 用户' : '## 助手')
    if (m.role === 'user') {
      for (const f of m.attachments ?? []) uploaded.set(key(f), f)
      const names = (m.attachments ?? []).map((f) => oneLine(f.file_name))
      if (names.length) out.push(`附件:${names.join('、')}`)
    }
    let steps: string[] = []
    const flush = () => {
      if (steps.length) out.push(steps.join('\n'))
      steps = []
    }
    for (const item of groupParts(m.parts)) {
      if (item.kind === 'step') {
        const hint = stepSummary(item)
        steps.push(`- ${toolLabel(item.name)}${hint ? `:${oneLine(hint)}` : ''}`)
        continue
      }
      flush()
      if (item.kind === 'text') {
        if (item.text.trim()) out.push(item.text.trim())
      } else if (item.kind === 'files') {
        for (const f of item.files) delivered.set(key(f), f)
        out.push(item.files.map((f) => `- 交付文件:${oneLine(f.file_name)}`).join('\n'))
      } else if (item.kind === 'ask') {
        out.push(`> 向你确认:${oneLine(item.question)}`)
      }
    }
    flush()
    const sources = new Map(m.parts.flatMap((p) => p.type === 'sources' ? p.sources.map((s) => [s.id, s] as const) : []))
    for (const source of sources.values()) {
      if (exportedSources.has(source.id)) continue
      exportedSources.add(source.id)
      out.push(`<a id="source-${source.id}"></a>\n> 原文:${oneLine(sourceLabel(source))}\n>\n${source.excerpt.split('\n').map((line) => `> ${line}`).join('\n')}`)
    }
    if (m.status === 'error') out.push('> (这一轮生成出错)')
    else if (m.status === 'cancelled') out.push('> (这一轮已被中止)')
  }

  if (uploaded.size || delivered.size) {
    out.push('## 文件清单')
    const list = (label: string, files: Map<string, FileRef>) =>
      files.size ? `${label}\n${[...files.values()].map((f) => `- ${oneLine(f.file_name)}`).join('\n')}` : ''
    out.push([list('**我上传的**', uploaded), list('**Agent 产出的**', delivered)].filter(Boolean).join('\n\n'))
  }
  return out.join('\n\n') + '\n'
}

/** 文件名里不能出现的字符换成下划线,过长截断。 */
export function exportFileName(title: string): string {
  const base = oneLine(title).replace(/[\\/:*?"<>|]/g, '_').slice(0, 60) || 'conversation'
  return `${base}.md`
}
