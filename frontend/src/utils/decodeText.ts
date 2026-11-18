/**
 * 把文件字节解码成文本:先按 UTF-8 严格解码,失败再按 GB18030(兼容 GBK/GB2312)。
 * 中文 Windows 上 Excel 导出的 CSV、记事本存的 txt 常是 GBK,按 UTF-8 读会整片乱码。
 * stream: true 让截断在多字节字符中间的结尾不算错误(预览只读前 1 MB)。
 */
export async function decodeText(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes, { stream: true })
  } catch {
    try {
      return new TextDecoder('gb18030').decode(bytes)
    } catch {
      return new TextDecoder('utf-8').decode(bytes)
    }
  }
}
