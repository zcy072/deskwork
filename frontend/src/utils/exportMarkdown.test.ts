import { describe, expect, it } from 'vitest'
import type { MessageRow } from '@/types'
import { conversationToMarkdown, exportFileName } from './exportMarkdown'

const rows: MessageRow[] = [
  {
    id: 1,
    role: 'user',
    parts: [{ type: 'text', text: '汇总这张表' }],
    status: 'complete',
    rewindable: true,
    attachments: [{ file_name: 'sales.xlsx', container_path: '/workspace/uploads/sales.xlsx' }],
  },
  {
    id: 2,
    role: 'assistant',
    status: 'complete',
    rewindable: false,
    parts: [
      { type: 'tool_call', id: 't1', name: 'bash_tool', args: { description: '读取表格\n并统计' } },
      { type: 'tool_result', id: 't1', name: 'bash_tool', content: 'ok' },
      { type: 'text', text: '已完成,结果如下。' },
      { type: 'files', files: [{ file_name: 'report.docx', container_path: '/workspace/output/report.docx' }] },
    ],
  },
]

describe('conversationToMarkdown', () => {
  it('引用导出后仍有对应的锚点和摘录', () => {
    const md = conversationToMarkdown('引用', [{ id: 1, role: 'assistant', status: 'complete', rewindable: false, parts: [
      { type: 'text', text: '预算 100 元 [原文](#source-ref1)' },
      { type: 'sources', sources: [{ id: 'ref1', file_name: 'a.txt', container_path: '/workspace/uploads/a.txt', fingerprint: 'a'.repeat(64), location: { kind: 'lines', start: 2, end: 2 }, excerpt: '预算 100 元' }] },
    ] }])
    expect(md).toContain('<a id="source-ref1"></a>')
    expect(md).toContain('a.txt · 第 2 行')
    expect(md).toContain('> 预算 100 元')
  })
  it('包含标题、正文、步骤摘要、附件与文件清单', () => {
    const md = conversationToMarkdown('销售汇总', rows)
    expect(md).toContain('# 销售汇总')
    expect(md).toContain('## 用户\n\n附件:sales.xlsx\n\n汇总这张表')
    expect(md).toContain('- 执行命令:读取表格 并统计')
    expect(md).toContain('已完成,结果如下。')
    expect(md).toContain('- 交付文件:report.docx')
    expect(md).toContain('**我上传的**\n- sales.xlsx')
    expect(md).toContain('**Agent 产出的**\n- report.docx')
  })

  it('没有文件时不输出文件清单;出错的轮次有标注', () => {
    const md = conversationToMarkdown('', [
      { id: 1, role: 'user', parts: [{ type: 'text', text: 'hi' }], status: 'complete', rewindable: true, attachments: [] },
      { id: 2, role: 'assistant', parts: [], status: 'error', rewindable: false },
    ])
    expect(md).toContain('# 未命名对话')
    expect(md).not.toContain('文件清单')
    expect(md).toContain('这一轮生成出错')
  })
})

describe('exportFileName', () => {
  it('替换非法字符并截断', () => {
    expect(exportFileName('a/b:c?')).toBe('a_b_c_.md')
    expect(exportFileName('')).toBe('conversation.md')
    expect(exportFileName('x'.repeat(100))).toHaveLength(63)
  })
})
