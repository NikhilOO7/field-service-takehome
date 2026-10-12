import { ChevronDown, ClipboardList, LogIn, MessageSquareText, Wrench } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { User } from '../api'
import { AccessChip, Avatar, cx } from './ui'

export function Logo() {
  return (
    <div className="flex items-center gap-2">
      <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-slate-900 text-white"><Wrench className="h-4 w-4" /></span>
      <span className="font-semibold tracking-tight text-slate-900">Field Copilot</span>
    </div>
  )
}

export function SignIn({ users, onPick }: { users: User[]; onPick: (id: string) => void }) {
  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 to-slate-100">
      <div className="mx-auto max-w-3xl px-4 py-12">
        <Logo />
        <h1 className="mt-8 text-3xl font-semibold tracking-tight text-slate-900">Who's working today?</h1>
        <p className="mt-2 text-slate-600">Pick a technician. Answers only use the documents that person is allowed to see.</p>
        <div className="mt-8 grid gap-3 sm:grid-cols-2">
          {users.map((u) => (
            <button key={u.id} onClick={() => onPick(u.id)}
              className="group flex items-start gap-3 rounded-2xl bg-white p-4 text-left shadow-sm ring-1 ring-slate-200 transition hover:-translate-y-0.5 hover:shadow-md hover:ring-indigo-300">
              <Avatar name={u.name} size="lg" />
              <div className="min-w-0 flex-1">
                <p className="font-semibold text-slate-900">{u.name}</p>
                <p className="text-sm text-slate-500">{u.title}</p>
                <div className="mt-2 flex flex-wrap gap-1">{u.groups.map((g) => <AccessChip key={g} group={g} />)}</div>
              </div>
              <LogIn className="h-4 w-4 text-slate-300 group-hover:text-indigo-500" />
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

export function Header({ me, users, tab, onTab, onSwitch }: {
  me: User; users: User[]; tab: 'ask' | 'wo'; onTab: (t: 'ask' | 'wo') => void; onSwitch: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const Tab = ({ id, icon: Icon, label }: { id: 'ask' | 'wo'; icon: typeof Wrench; label: string }) => (
    <button onClick={() => onTab(id)}
      className={cx('inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition',
        tab === id ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100')}>
      <Icon className="h-4 w-4" /><span className="hidden sm:inline">{label}</span>
    </button>
  )

  return (
    <header className="sticky top-0 z-40 border-b border-slate-200/80 bg-white/90 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4">
        <Logo />
        <nav className="ml-2 flex gap-1">
          <Tab id="ask" icon={MessageSquareText} label="Ask" />
          <Tab id="wo" icon={ClipboardList} label="Work orders" />
        </nav>
        <div ref={ref} className="relative ml-auto">
          <button onClick={() => setOpen((o) => !o)} aria-label="Account menu" aria-expanded={open} className="flex items-center gap-2 rounded-xl p-1 pr-2 hover:bg-slate-100">
            <Avatar name={me.name} size="sm" />
            <span className="hidden text-left text-sm leading-tight sm:block">
              <span className="block font-medium text-slate-900">{me.name}</span>
              <span className="block text-xs text-slate-500">{me.title}</span>
            </span>
            <ChevronDown className="h-4 w-4 text-slate-400" />
          </button>
          {open && (
            <div className="absolute right-0 mt-2 w-80 rounded-2xl bg-white p-3 shadow-xl ring-1 ring-slate-200">
              <p className="px-1 text-xs font-semibold text-slate-500">You can access</p>
              <div className="mt-1.5 flex flex-wrap gap-1 px-1">{me.groups.map((g) => <AccessChip key={g} group={g} />)}</div>
              <p className="mt-4 px-1 text-xs font-semibold text-slate-500">Switch technician</p>
              <div className="mt-1 max-h-72 overflow-y-auto">
                {users.filter((u) => u.id !== me.id).map((u) => (
                  <button key={u.id} onClick={() => { setOpen(false); onSwitch(u.id) }}
                    className="flex w-full items-center gap-2 rounded-xl p-2 text-left hover:bg-slate-50">
                    <Avatar name={u.name} size="sm" />
                    <span className="text-sm leading-tight">
                      <span className="block font-medium text-slate-900">{u.name}</span>
                      <span className="block text-xs text-slate-500">{u.title}</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}

export function Toast({ message, tone, onClose }: { message: string; tone: 'error' | 'ok'; onClose: () => void }) {
  useEffect(() => { const t = setTimeout(onClose, 5000); return () => clearTimeout(t) }, [onClose])
  return (
    <div className={cx('fixed bottom-24 left-1/2 z-[60] w-max max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-xl px-4 py-3 text-sm font-medium shadow-lg',
      tone === 'error' ? 'bg-red-600 text-white' : 'bg-slate-900 text-white')}>
      {message}
    </div>
  )
}
