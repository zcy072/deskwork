/** catch 里拿到的是 unknown;统一取出可展示的错误信息。 */
export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message
  if (typeof e === 'string') return e
  return '未知错误'
}
