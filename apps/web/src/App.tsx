import { Component, type ReactNode } from 'react'
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import { LoginPage } from './auth/LoginPage'
import { Protected } from './auth/Protected'
import { AppShell } from './layout/AppShell'
import { UsersPage } from './pages/UsersPage'
import { ContractorsPage } from './pages/ContractorsPage'
import { InspectionPage } from './pages/InspectionPage'
import { ActionsPage } from './pages/ActionsPage'
import { FieldCapturePage } from './pages/FieldCapturePage'
import './App.css'

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[ErrorBoundary]', error, info)
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{
          padding: 24, color: '#fecaca', background: '#0b0f17',
          fontFamily: 'ui-monospace, monospace', minHeight: '100vh', whiteSpace: 'pre-wrap',
        }}>
          <h2 style={{ color: '#ef4444' }}>App crashed</h2>
          <div><strong>{this.state.error.name}:</strong> {this.state.error.message}</div>
          <pre style={{ marginTop: 12, fontSize: 12, color: '#9ca3af' }}>{this.state.error.stack}</pre>
          <button
            onClick={() => this.setState({ error: null })}
            style={{ marginTop: 16, padding: '8px 16px', background: '#2563eb', color: 'white', border: 0, borderRadius: 4, cursor: 'pointer' }}
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <HashRouter>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/app" element={<Protected><AppShell /></Protected>}>
              <Route path="users" element={<Protected roles={['admin']}><UsersPage /></Protected>} />
              <Route path="contractors" element={<ContractorsPage />} />
              <Route path="inspection" element={<InspectionPage />} />
              <Route path="field-captures" element={<FieldCapturePage />} />
              <Route path="actions" element={<ActionsPage />} />
              <Route index element={<Navigate to="inspection" replace />} />
            </Route>
            <Route path="*" element={<Navigate to="/app/inspection" replace />} />
          </Routes>
        </HashRouter>
      </AuthProvider>
    </ErrorBoundary>
  )
}
