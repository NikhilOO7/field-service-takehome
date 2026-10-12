import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { api } from './api'
import { AskPage } from './components/AskPage'
import { Header, SignIn, Toast } from './components/Shell'
import { Spinner } from './components/ui'
import { WorkOrders } from './components/WorkOrders'

export default function App() {
  const qc = useQueryClient()
  const users = useQuery({ queryKey: ['users'], queryFn: api.users })
  const me = useQuery({ queryKey: ['me'], queryFn: api.me, retry: false })
  const [tab, setTab] = useState<'ask' | 'wo'>('ask')
  const [selectedWO, setSelectedWO] = useState<string | null>(null)
  const [toast, setToast] = useState<{ message: string; tone: 'error' | 'ok' } | null>(null)
  const notify = useCallback((message: string, tone: 'error' | 'ok' = 'ok') => setToast({ message, tone }), [])

  const login = useMutation({
    mutationFn: api.login,
    onSuccess: (u) => {
      // Switching technician starts a new session: drop everything cached for the previous one.
      qc.clear()
      qc.setQueryData(['me'], u)
      setSelectedWO(null)
    },
  })

  if (me.isLoading || users.isLoading) {
    return <div className="flex min-h-screen items-center justify-center text-slate-400"><Spinner /></div>
  }
  const user = me.data
  const allUsers = users.data?.users ?? []
  if (!user) return <SignIn users={allUsers} onPick={(id) => login.mutate(id)} />

  return (
    <div className="min-h-screen bg-slate-100">
      <Header me={user} users={allUsers} tab={tab} onTab={setTab} onSwitch={(id) => login.mutate(id)} />
      {tab === 'ask'
        ? <AskPage key={user.id} me={user} users={allUsers} notify={notify}
            onSwitch={async (uid) => { await login.mutateAsync(uid); setTab('ask') }}
            onOpenWO={(id) => { setSelectedWO(id); setTab('wo') }} />
        : <WorkOrders key={user.id} me={user} selected={selectedWO} onSelect={setSelectedWO} notify={notify} />}
      {toast && <Toast message={toast.message} tone={toast.tone} onClose={() => setToast(null)} />}
    </div>
  )
}
