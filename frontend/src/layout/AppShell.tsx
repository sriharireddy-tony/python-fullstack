import { useCallback, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { SideNav } from './SideNav'
import { TopNav } from './TopNav'

const COLLAPSE_KEY = 'os-tracker.sidebar-collapsed'

export function AppShell() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === 'true'
    } catch {
      return false
    }
  })

  const toggle = useCallback(() => {
    setCollapsed((current) => {
      const next = !current
      try {
        localStorage.setItem(COLLAPSE_KEY, String(next))
      } catch {
        /* storage may be unavailable; the in-memory value still applies */
      }
      return next
    })
  }, [])

  return (
    <div className="os-shell" data-collapsed={collapsed}>
      <SideNav collapsed={collapsed} />
      <TopNav collapsed={collapsed} onToggleSidebar={toggle} />
      <main className="os-main">
        <Outlet />
      </main>
    </div>
  )
}
