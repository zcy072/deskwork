/** 输入法正在确认候选词时的回车,不应触发发送/保存。
 * Safari 里 compositionend 先于 keydown,此时 isComposing 已为 false,只能靠 keyCode 229 识别。 */
export function isImeEnter(e: KeyboardEvent): boolean {
  return e.isComposing || e.keyCode === 229
}
