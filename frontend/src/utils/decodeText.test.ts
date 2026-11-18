import { describe, expect, it } from 'vitest'
import { decodeText } from './decodeText'

describe('decodeText', () => {
  it('UTF-8 原样解码,去掉 BOM', async () => {
    expect(await decodeText(new Blob([new Uint8Array([0xef, 0xbb, 0xbf]), '地区,销量'] ))).toBe('地区,销量')
  })

  it('GBK 编码的中文 CSV 不再乱码', async () => {
    // "你好,1\n" 的 GBK 字节
    const gbk = new Uint8Array([0xc4, 0xe3, 0xba, 0xc3, 0x2c, 0x31, 0x0a])
    expect(await decodeText(new Blob([gbk]))).toBe('你好,1\n')
  })

  it('UTF-8 在多字节字符中间被截断时仍按 UTF-8 解码', async () => {
    const full = new TextEncoder().encode('中文')
    expect(await decodeText(new Blob([full.slice(0, 4)]))).toBe('中')
  })
})
