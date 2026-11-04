export interface PromptTemplate {
  id: string
  label: string
  text: string
}

export const STORAGE_KEY = 'prompt_templates'
export const MAX_TEMPLATES = 12
const MAX_LABEL = 20
const MAX_TEXT = 2000

export const DEFAULT_TEMPLATES: PromptTemplate[] = [
  { id: 'd-table', label: '分析表格', text: '请分析我上传的表格:概括数据结构,找出关键趋势和异常,并给出结论。' },
  { id: 'd-ppt', label: '文档转 PPT', text: '请把我上传的文档整理成一份 PPT,提炼要点,每页不超过 5 条。' },
  { id: 'd-summary', label: '总结文档', text: '请总结我上传的文档:先给出三句话概述,再按章节列出要点。' },
  { id: 'd-report', label: '生成 Word 报告', text: '请根据我上传的资料写一份结构清晰的 Word 报告,包含摘要、正文和结论。' },
]

function valid(t: unknown): t is PromptTemplate {
  const x = t as PromptTemplate
  return (
    !!x &&
    typeof x.id === 'string' &&
    typeof x.label === 'string' &&
    typeof x.text === 'string' &&
    x.label.trim() !== '' &&
    x.text.trim() !== ''
  )
}

/** 读取已保存的模板;没保存过用默认,保存内容损坏时也退回默认。用户删光后保存的是空数组,不会被默认值覆盖。 */
export function loadTemplates(storage?: Pick<Storage, 'getItem'>): PromptTemplate[] {
  try {
    const raw = (storage ?? localStorage).getItem(STORAGE_KEY)
    if (raw === null) return DEFAULT_TEMPLATES.map((t) => ({ ...t }))
    const data = JSON.parse(raw)
    if (!Array.isArray(data)) throw new Error('not an array')
    return data.filter(valid).slice(0, MAX_TEMPLATES)
  } catch {
    return DEFAULT_TEMPLATES.map((t) => ({ ...t }))
  }
}

export function saveTemplates(list: PromptTemplate[], storage?: Pick<Storage, 'setItem'>): void {
  try {
    ;(storage ?? localStorage).setItem(STORAGE_KEY, JSON.stringify(list))
  } catch {
    /* 存储不可用或配额满:本页内仍可用 */
  }
}

/** 校验并生成一条新模板;不合法返回错误文案。 */
export function makeTemplate(label: string, text: string, existing: PromptTemplate[]): PromptTemplate | string {
  const l = label.trim()
  const t = text.trim()
  if (!l || !t) return '名称和内容都不能为空'
  if (l.length > MAX_LABEL) return `名称最多 ${MAX_LABEL} 个字`
  if (t.length > MAX_TEXT) return `内容最多 ${MAX_TEXT} 个字`
  if (existing.length >= MAX_TEMPLATES) return `最多保存 ${MAX_TEMPLATES} 个模板`
  if (existing.some((e) => e.label === l)) return '已有同名模板'
  return { id: crypto.randomUUID(), label: l, text: t }
}
