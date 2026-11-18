import { fmtSize } from './files'

export interface UploadLimits {
  max_files: number
  max_file_mb: number
  max_total_mb: number
}

/** 与后端默认值一致;实际值以 /capabilities 返回的为准 */
export const DEFAULT_UPLOAD_LIMITS: UploadLimits = { max_files: 20, max_file_mb: 50, max_total_mb: 100 }

const MB = 1024 * 1024

/**
 * 把新选的文件并入已选列表:超过单文件上限、数量上限或总量上限的不收,返回原因。
 * 在选文件时就提示,免得整批上传完才被服务端 413 拒绝。
 */
export function admitFiles(current: File[], incoming: File[], limits: UploadLimits): { accepted: File[]; rejected: string[] } {
  const accepted: File[] = []
  const rejected: string[] = []
  let count = current.length
  let total = current.reduce((n, f) => n + f.size, 0)
  for (const f of incoming) {
    if (f.size > limits.max_file_mb * MB) rejected.push(`「${f.name}」${fmtSize(f.size)},超过单个文件 ${limits.max_file_mb} MB 的上限`)
    else if (count >= limits.max_files) rejected.push(`「${f.name}」未添加:一次最多 ${limits.max_files} 个文件`)
    else if (total + f.size > limits.max_total_mb * MB) rejected.push(`「${f.name}」未添加:一次上传总量不能超过 ${limits.max_total_mb} MB`)
    else {
      accepted.push(f)
      count++
      total += f.size
    }
  }
  return { accepted, rejected }
}
