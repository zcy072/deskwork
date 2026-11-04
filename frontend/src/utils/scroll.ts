/** 离底部不到 threshold 像素就算"跟随中";用户往上翻看时不要把他拉回底部。 */
export function isNearBottom(
  el: { scrollHeight: number; scrollTop: number; clientHeight: number },
  threshold = 80,
): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold
}

/** 离顶部不到 threshold 像素就该加载更早的消息。 */
export function isNearTop(el: { scrollTop: number }, threshold = 40): boolean {
  return el.scrollTop < threshold
}
