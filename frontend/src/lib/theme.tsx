/**
 * Theme and density preferences.
 *
 * Stored in two places deliberately (docs/07-frontend.md):
 *
 *   - `users.preferences` on the server : the source of truth, so the choice
 *     follows the person to another machine.
 *   - `localStorage` : read synchronously by the inline script in index.html so
 *     there is no flash of the wrong theme before React mounts. A cache only.
 *
 * The server value wins once the session resolves. Writes go to both, and a
 * failed server write is not fatal — the local choice still applies.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useUpdatePreferences } from '@/api/hooks/useAuth'
import { useSession } from './session'

export type ThemePreference = 'light' | 'dark' | 'system'
export type Density = 'comfortable' | 'compact'

const THEME_KEY = 'os-tracker.theme'
const DENSITY_KEY = 'os-tracker.density'

interface ThemeContextValue {
  theme: ThemePreference
  /** What is actually rendered once 'system' is resolved. */
  resolvedTheme: 'light' | 'dark'
  density: Density
  setTheme: (t: ThemePreference) => void
  setDensity: (d: Density) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

function readStored<T extends string>(key: string, fallback: T): T {
  try {
    return (localStorage.getItem(key) as T) ?? fallback
  } catch {
    return fallback
  }
}

function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Private browsing or blocked storage — the in-memory value still applies.
  }
}

function systemPrefersDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemePreference>(() =>
    readStored<ThemePreference>(THEME_KEY, 'system'),
  )
  const [density, setDensityState] = useState<Density>(() =>
    readStored<Density>(DENSITY_KEY, 'comfortable'),
  )
  const [systemDark, setSystemDark] = useState(systemPrefersDark)

  const { user } = useSession()
  const updatePreferences = useUpdatePreferences()
  const hydratedFromServer = useRef(false)

  // Adopt the server's stored preference once, on first load. Subsequent local
  // changes must not be overwritten by a refetch, hence the ref.
  useEffect(() => {
    if (!user || hydratedFromServer.current) return
    hydratedFromServer.current = true

    const serverTheme = user.preferences?.theme as ThemePreference | undefined
    const serverDensity = user.preferences?.density as Density | undefined

    if (serverTheme && serverTheme !== theme) {
      setThemeState(serverTheme)
      writeStored(THEME_KEY, serverTheme)
    }
    if (serverDensity && serverDensity !== density) {
      setDensityState(serverDensity)
      writeStored(DENSITY_KEY, serverDensity)
    }
  }, [user, theme, density])

  // Follow the OS setting live while the preference is 'system'.
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const resolvedTheme: 'light' | 'dark' =
    theme === 'system' ? (systemDark ? 'dark' : 'light') : theme

  useEffect(() => {
    document.documentElement.setAttribute('data-bs-theme', resolvedTheme)
  }, [resolvedTheme])

  useEffect(() => {
    document.documentElement.setAttribute('data-density', density)
  }, [density])

  const persist = useCallback(
    (payload: { theme?: string; density?: string }) => {
      if (!user) return
      // Fire and forget: the preference already applied locally, and a failed
      // sync is not worth interrupting the user for.
      updatePreferences.mutate(payload)
    },
    [user, updatePreferences],
  )

  const setTheme = useCallback(
    (next: ThemePreference) => {
      setThemeState(next)
      writeStored(THEME_KEY, next)
      persist({ theme: next })
    },
    [persist],
  )

  const setDensity = useCallback(
    (next: Density) => {
      setDensityState(next)
      writeStored(DENSITY_KEY, next)
      persist({ density: next })
    },
    [persist],
  )

  const value = useMemo(
    () => ({ theme, resolvedTheme, density, setTheme, setDensity }),
    [theme, resolvedTheme, density, setTheme, setDensity],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used inside <ThemeProvider>')
  return ctx
}
