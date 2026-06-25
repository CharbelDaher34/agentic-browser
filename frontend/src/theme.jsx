import { createContext, useContext, useEffect, useState } from 'react'

const ThemeCtx = createContext({ theme: 'dark', setTheme: () => {} })
const KEY = 'ab_theme'

export function ThemeProvider({ children }) {
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme || 'dark'
  )
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem(KEY, theme) } catch {}
  }, [theme])
  return <ThemeCtx.Provider value={{ theme, setTheme }}>{children}</ThemeCtx.Provider>
}

export const useTheme = () => useContext(ThemeCtx)

export function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  return (
    <div className="theme-toggle" role="group" aria-label="Theme">
      <button
        className={theme === 'light' ? 'on' : ''}
        onClick={() => setTheme('light')}
        title="Light"
        aria-label="Light theme"
      >
        ☀
      </button>
      <button
        className={theme === 'dark' ? 'on' : ''}
        onClick={() => setTheme('dark')}
        title="Dark"
        aria-label="Dark theme"
      >
        ☾
      </button>
    </div>
  )
}
