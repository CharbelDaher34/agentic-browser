import { ThemeProvider } from './theme.jsx'
import { AuthProvider, AuthScreen, useAuth } from './auth.jsx'
import Workspace from './Workspace.jsx'

function Gate() {
  const { user, ready } = useAuth()
  if (!ready) return <div className="center muted">Loading…</div>
  if (!user) return <AuthScreen />
  return <Workspace />
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </ThemeProvider>
  )
}
