import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage, FileRef } from '@/types'

vi.mock('@/api/client', () => {
  class ApiError extends Error {
    constructor(public status: number, message: string) {
      super(message)
    }
  }
  return { ApiError, downloadFile: vi.fn(), fetchDeliveredBlob: vi.fn(), saveBlob: vi.fn() }
})
import { ApiError, downloadFile, fetchDeliveredBlob, saveBlob } from '@/api/client'
import { useChatStore } from '@/stores/chat'
import DeliveredFiles from './DeliveredFiles.vue'

const ref = (sha: string | undefined): FileRef => ({
  file_name: 'a.docx', container_path: '/workspace/output/a.docx', sha256: sha, size: 3,
})
const turn = (id: string, f: FileRef): ChatMessage => ({ id, role: 'assistant', parts: [{ type: 'files', files: [f] }] })

function setup(shown: FileRef, all: FileRef[]) {
  setActivePinia(createPinia())
  const chat = useChatStore()
  chat.messages = all.map((f, i) => turn(`m${i}`, f))
  return mount(DeliveredFiles, { props: { files: [shown] } })
}
const btn = (w: ReturnType<typeof setup>, label: string) => w.findAll('button').find((b) => b.text() === label)

beforeEach(() => vi.resetAllMocks())

describe('历史消息里的交付文件', () => {
  it('最新一次交付只有一个下载按钮', () => {
    const f = ref('a'.repeat(64))
    const w = setup(f, [f])
    expect(w.text()).not.toContain('已有更新版本')
    expect(w.findAll('button').map((b) => b.text())).toEqual(['下载'])
  })

  it('被后来交付取代的记录可下载当时版本,也可下载最新版本', async () => {
    const old = ref('a'.repeat(64))
    const w = setup(old, [old, ref('b'.repeat(64))])
    expect(w.text()).toContain('已有更新版本')
    vi.mocked(fetchDeliveredBlob).mockResolvedValue(new Blob(['x']))
    await btn(w, '下载当时版本')!.trigger('click')
    await flushPromises()
    expect(fetchDeliveredBlob).toHaveBeenCalledWith(expect.any(String), 'a.docx', 'a'.repeat(64), 3)
    expect(saveBlob).toHaveBeenCalled()
    await btn(w, '下载最新版本')!.trigger('click')
    expect(downloadFile).toHaveBeenCalledWith(expect.any(String), 'output/a.docx', 'a.docx')
  })

  it('当时版本已被裁剪时提示并保留最新版本入口', async () => {
    const old = ref('a'.repeat(64))
    const w = setup(old, [old, ref('b'.repeat(64))])
    vi.mocked(fetchDeliveredBlob).mockRejectedValue(new ApiError(410, 'gone'))
    await btn(w, '下载当时版本')!.trigger('click')
    await flushPromises()
    expect(w.find('[role="alert"]').text()).toContain('不在保留范围内')
    expect(saveBlob).not.toHaveBeenCalled()
    expect(btn(w, '下载最新版本')).toBeTruthy()
  })

  it('没有摘要的旧记录只提供下载最新版本', () => {
    const legacy = ref(undefined)
    const w = setup(legacy, [legacy])
    expect(w.findAll('button').map((b) => b.text())).toEqual(['下载最新版本'])
  })
})
