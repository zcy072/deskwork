import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({ gfm: true, breaks: true })

// 链接一律新标签页打开:在当前页跳走会中断正在进行的流式输出
// 远程图片会在渲染时自动发请求,被提示注入的模型可借此把数据拼进 URL 带出去,所以只保留 data:/blob: 图片
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'IMG') {
    const src = (node.getAttribute('src') ?? '').trim().toLowerCase()
    if (!src.startsWith('data:') && !src.startsWith('blob:')) {
      const note = document.createElement('span')
      note.textContent = `[图片已拦截${node.getAttribute('alt') ? ':' + node.getAttribute('alt') : ''}]`
      node.replaceWith(note)
      return
    }
  }
  // 代码块右上角的复制按钮;点击由 MessageBubble 在 .md 容器上统一处理(按钮随 HTML 缓存,流式重渲不会丢)
  if (node.tagName === 'PRE' && !node.querySelector('.code-copy')) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'code-copy'
    btn.textContent = '复制'
    node.appendChild(btn)
    return
  }
  if (node.tagName === 'A' && node.hasAttribute('href')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

// 除 img[src] 外,这些属性/标签同样会让浏览器在渲染时自动请求外部地址(style 里的 url()、
// srcset、video poster 等),一并禁掉;模型输出的 Markdown 用不到它们
const SANITIZE_CONFIG = {
  FORBID_ATTR: ['style', 'srcset', 'poster'],
  FORBID_TAGS: ['picture', 'source', 'video', 'audio', 'track'],
}

function renderBlock(source: string): string {
  return DOMPurify.sanitize(marked.parse(source, { async: false }) as string, SANITIZE_CONFIG)
}

const LIST_ITEM = /^\s*([-*+]|\d+[.)])\s/

/**
 * 按空行切块,代码围栏内的空行不算分隔。每块独立渲染,所以追加内容只会改动最后一块。
 * 列表(含项与项之间有空行的松散列表)、缩进续行不拆,否则有序列表的编号会每块重新从 1 开始。
 */
export function splitBlocks(source: string): string[] {
  const lines = source.split('\n')
  const blocks: string[] = []
  let cur: string[] = []
  let fence = ''
  let inList = false
  let nextIdx = 0
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const m = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line)
    if (m) {
      if (!fence) {
        if (m[1][0] !== '`' || !m[2].includes('`')) fence = m[1]
      } else if (m[1][0] === fence[0] && m[1].length >= fence.length && !m[2].trim()) fence = ''
    }
    if (!fence && !cur.length) inList = LIST_ITEM.test(line)
    if (!fence && line.trim() === '' && cur.length) {
      // 连续空行共用同一个"下一个非空行":沿用上一次扫描的位置,整体线性
      if (nextIdx <= i) {
        nextIdx = i + 1
        while (nextIdx < lines.length && lines[nextIdx].trim() === '') nextIdx++
      }
      const next = lines[nextIdx] ?? ''
      const continues = inList && (LIST_ITEM.test(next) || /^( {2,}|\t)/.test(next))
      if (!continues) {
        blocks.push(cur.join('\n'))
        cur = []
        continue
      }
    }
    cur.push(line)
  }
  if (cur.length) blocks.push(cur.join('\n'))
  return blocks
}

const CACHE_MAX = 500
const cache = new Map<string, string>()

/**
 * Markdown 转 HTML,并用 DOMPurify 清洗,防止模型输出或上传文档里的脚本注入页面。
 * 流式输出时每个 token 都会重渲整条消息,已经写完的块直接走缓存。
 * 引用式链接定义(`[a]: url`)跨块时不生效,模型输出里很少见,换取不重复解析整篇长文。
 */
export function renderMarkdown(source: string): string {
  return splitBlocks(source)
    .map((block) => {
      let html = cache.get(block)
      if (html === undefined) {
        html = renderBlock(block)
        if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value as string)
        cache.set(block, html)
      }
      return html
    })
    .join('\n')
}
