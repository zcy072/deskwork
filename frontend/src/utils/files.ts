/** /workspace/output/a.pdf -> output/a.pdf(后端下载接口只接受 uploads/ 与 output/ 前缀) */
export function toRelativePath(containerPath: string): string {
  if (containerPath.startsWith('/workspace/')) return containerPath.slice('/workspace/'.length)
  if (containerPath.startsWith('/workspace')) return containerPath.slice('/workspace'.length).replace(/^\/+/, '')
  return containerPath.replace(/^\/+/, '')
}

export const fmtSize = (n: number) =>
  n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`
