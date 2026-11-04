import { mount } from '@vue/test-utils'
import { NModal } from 'naive-ui'
import { beforeEach, describe, expect, it } from 'vitest'
import { STORAGE_KEY } from '@/utils/templates'
import TemplateChips from './TemplateChips.vue'

describe('TemplateChips', () => {
  beforeEach(() => {
    localStorage.clear()
    document.body.innerHTML = ''
  })

  it('显示默认模板,点击把内容交给父组件', async () => {
    const w = mount(TemplateChips, { props: { draft: '' } })
    const uses = w.findAll('button.use')
    expect(uses.length).toBeGreaterThan(0)
    await uses[0].trigger('click')
    expect(w.emitted('pick')![0][0]).toContain('表格')
    w.unmount()
  })

  it('删除后写入 localStorage,刷新不会恢复默认', async () => {
    const w = mount(TemplateChips, { props: { draft: '' } })
    const n = w.findAll('button.use').length
    await w.find('button.del').trigger('click')
    expect(w.findAll('button.use')).toHaveLength(n - 1)
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toHaveLength(n - 1)
    const again = mount(TemplateChips, { props: { draft: '' } })
    expect(again.findAll('button.use')).toHaveLength(n - 1)
    w.unmount()
    again.unmount()
  })

  it('新建弹窗带入当前输入;校验失败给出提示,成功后保存', async () => {
    const w = mount(TemplateChips, { props: { draft: '写周报' }, attachTo: document.body })
    await w.find('button.add').trigger('click')
    const modal = w.findComponent(NModal)
    expect(modal.props('show')).toBe(true)
    const inputs = document.body.querySelectorAll('.n-modal input, .n-modal textarea')
    expect((inputs[1] as HTMLTextAreaElement).value).toBe('写周报')
    const save = [...document.body.querySelectorAll('.n-modal button')].find((b) => b.textContent === '保存') as HTMLElement
    save.click()
    await w.vm.$nextTick()
    expect(document.body.textContent).toContain('名称和内容都不能为空')
    const name = inputs[0] as HTMLInputElement
    name.value = '周报'
    name.dispatchEvent(new Event('input'))
    await w.vm.$nextTick()
    save.click()
    await w.vm.$nextTick()
    expect(w.findAll('button.use').some((b) => b.text() === '周报')).toBe(true)
    expect(localStorage.getItem(STORAGE_KEY)).toContain('周报')
    w.unmount()
  })
})
