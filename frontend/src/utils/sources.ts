import type { SourceRef } from '@/types'

function range(start: number, end: number, unit: string): string {
  return start === end ? `第 ${start} ${unit}` : `第 ${start}–${end} ${unit}`
}

export function locationLabel(loc: SourceRef['location']): string {
  if (loc.kind === 'page') return `第 ${loc.page} 页`
  if (loc.kind === 'slide') return `第 ${loc.slide} 张幻灯片`
  return range(loc.start, loc.end, loc.kind === 'paragraphs' ? '段' : '行')
}

export function sourceLabel(source: SourceRef): string {
  return `${source.file_name} · ${locationLabel(source.location)}`
}
