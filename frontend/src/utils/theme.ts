export type ThemeMode = 'auto' | 'light' | 'dark'

/** 读取已保存的主题;值不合法(被改坏、旧版本遗留)就回到 auto。 */
export function parseThemeMode(v: string | null): ThemeMode {
  return v === 'light' || v === 'dark' || v === 'auto' ? v : 'auto'
}

interface Palette {
  accent: string
  accentHover: string
  accentPressed: string
  /** 主色按钮上的文字色 */
  onAccent: string
  vars: Record<string, string>
}

// 主色与 CSS 变量的唯一来源:Naive UI 的 themeOverrides 和全局 CSS 变量都从这里派生。
// --muted 在三种背景(应用底/侧栏/卡片)上的对比度:浅色 ≥ 4.6,深色 ≥ 5.9(12px 小字需 4.5)
export const PALETTES: Record<'light' | 'dark', Palette> = {
  light: {
    accent: '#5561e8',
    accentHover: '#6b76f0',
    accentPressed: '#4752cf',
    onAccent: '#ffffff',
    vars: {
      '--app-bg': '#f5f6fa',
      '--panel-bg': '#fbfbfd',
      '--card-bg': '#ffffff',
      '--line': 'rgba(20, 24, 40, 0.09)',
      '--soft': 'rgba(85, 97, 232, 0.08)',
      '--soft-strong': 'rgba(85, 97, 232, 0.14)',
      '--accent-2': '#9b6bff',
      '--muted': 'rgba(20, 24, 40, 0.62)',
      '--shadow': '0 1px 2px rgba(20, 24, 40, 0.05), 0 4px 16px rgba(20, 24, 40, 0.04)',
    },
  },
  dark: {
    accent: '#8f9bff',
    accentHover: '#a6afff',
    accentPressed: '#7783f0',
    onAccent: '#10121c',
    vars: {
      '--app-bg': '#0e0f15',
      '--panel-bg': '#13141b',
      '--card-bg': '#1a1c25',
      '--line': 'rgba(255, 255, 255, 0.08)',
      '--soft': 'rgba(143, 155, 255, 0.1)',
      '--soft-strong': 'rgba(143, 155, 255, 0.18)',
      '--accent-2': '#9b6bff',
      '--muted': 'rgba(255, 255, 255, 0.6)',
      '--shadow': '0 1px 2px rgba(0, 0, 0, 0.3), 0 4px 16px rgba(0, 0, 0, 0.25)',
    },
  },
}

export function cssVars(dark: boolean): Record<string, string> {
  const p = PALETTES[dark ? 'dark' : 'light']
  return { '--accent': p.accent, ...p.vars }
}

export function naiveOverrides(dark: boolean) {
  const p = PALETTES[dark ? 'dark' : 'light']
  return {
    common: {
      primaryColor: p.accent,
      primaryColorHover: p.accentHover,
      primaryColorPressed: p.accentPressed,
      primaryColorSuppl: p.accentHover,
      borderRadius: '10px',
      fontFamily:
        '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif',
    },
    Button: {
      textColorPrimary: p.onAccent,
      textColorHoverPrimary: p.onAccent,
      textColorPressedPrimary: p.onAccent,
      textColorFocusPrimary: p.onAccent,
    },
  }
}
