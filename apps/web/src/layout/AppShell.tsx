import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { listActions } from '../lib/erpApi'

export function AppShell() {
  const { user, logout, hasRole } = useAuth()
  const [openActions, setOpenActions] = useState<number | null>(null)

  // Lightweight badge: count open actions, refresh on mount + every 60s.
  useEffect(() => {
    let live = true
    async function refresh() {
      try {
        const actions = await listActions('open')
        if (live) setOpenActions(actions.length)
      } catch { /* not fatal */ }
    }
    refresh()
    const t = setInterval(refresh, 60_000)
    return () => { live = false; clearInterval(t) }
  }, [])

  const tab = (to: string, label: string, badge?: number | null) => (
    <NavLink to={to} className={({ isActive }) => `shell-tab${isActive ? ' active' : ''}`}>
      {label}
      {badge != null && badge > 0 && <span className="tab-badge">{badge}</span>}
    </NavLink>
  )

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <span className="brand-logo" aria-hidden>◈</span>
          <div className="brand-text">
            <span className="brand-title">PatchGuard ERP</span>
            <span className="brand-sub">Road maintenance management</span>
          </div>
        </div>
        <nav className="shell-tabs">
          {hasRole('admin') && tab('/app/users', 'Users & Accounts')}
          {tab('/app/contractors', 'Contractors')}
          {tab('/app/inspection', 'Inspection')}
          {tab('/app/field-captures', 'Field Captures')}
          {tab('/app/actions', 'Actions', openActions)}
        </nav>
        <div className="header-user">
          <span className="header-user-name">{user?.full_name}</span>
          <span className={`role-pill role-${user?.role}`}>{user?.role}</span>
          <button className="logout-btn" onClick={logout} type="button">Sign out</button>
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
