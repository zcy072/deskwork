export type PreviewKind = 'image' | 'pdf' | 'office' | 'csv' | 'text' | 'none'

const IMAGE = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'])
const OFFICE = new Set(['docx', 'doc', 'pptx', 'ppt', 'xlsx', 'xls', 'odt', 'odp', 'ods'])
const TEXT = new Set(['txt', 'md', 'json', 'log', 'py', 'js', 'ts', 'html', 'css', 'xml', 'yaml', 'yml', 'sh', 'sql'])
const MIME: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
  webp: 'image/webp', svg: 'image/svg+xml', pdf: 'application/pdf',
}

export function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i < 0 ? '' : name.slice(i + 1).toLowerCase()
}

/** office 类文件由服务端转成 PDF 后预览(/files/preview)。 */
export function previewKind(name: string): PreviewKind {
  const e = extOf(name)
  if (IMAGE.has(e)) return 'image'
  if (e === 'pdf') return 'pdf'
  if (OFFICE.has(e)) return 'office'
  if (e === 'csv') return 'csv'
  if (TEXT.has(e)) return 'text'
  return 'none'
}

export function mimeOf(name: string): string {
  return MIME[extOf(name)] ?? 'application/octet-stream'
}

/** 解析 CSV(支持引号、转义引号、引号内换行),最多返回 maxRows 行。 */
export function parseCsv(text: string, maxRows = 200): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false
  for (let i = 0; i < text.length && rows.length < maxRows; i++) {
    const c = text[i]
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++ }
      else if (c === '"') quoted = false
      else cell += c
    } else if (c === '"') quoted = true
    else if (c === ',') { row.push(cell); cell = '' }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++
      row.push(cell); cell = ''
      rows.push(row); row = []
    } else cell += c
  }
  if (rows.length < maxRows && (cell || row.length)) { row.push(cell); rows.push(row) }
  return rows
}
