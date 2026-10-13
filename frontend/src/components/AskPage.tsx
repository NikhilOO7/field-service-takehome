import { useQuery } from '@tanstack/react-query'
import { ArrowUp, Check, ClipboardCheck, FileSearch, History, PanelLeft, Plus, ScrollText, Siren, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError, api, askStream, type AskResponse, type User } from '../api'
import { timeAgo } from '../lib/labels'
import { AnswerCard } from './AnswerCard'
import { Card, Spinner, cx } from './ui'

// Marker for an unsaved conversation; never sent to the server.
const NEW = '__new__'

type Turn = { question: string; response?: AskResponse; error?: string; stage?: number }

const STAGES = ['Understanding', 'Searching documents', 'Writing the answer', 'Double-checking facts']
function stageOf(message: string) {
  const m = message.toLowerCase()
  if (m.includes('understanding')) return 0
  if (m.includes('writing')) return 2
  if (m.includes('checking every')) return 3
  return 1
}

// Suggestions tailored to what the technician works on.
const SUGGESTIONS: Record<string, { icon: typeof Siren; title: string; text: string }[]> = {
  'family-pumps': [
    { icon: Siren, title: 'Explain a fault code', text: 'HCP-500 is showing P-07. What does that mean and what should I check first?' },
    { icon: FileSearch, title: 'Look up a spec', text: "What's the impeller-to-casing clearance spec on an HCP-200?" },
  ],
  'family-compressors': [
    { icon: ScrollText, title: 'Walk me through a fix', text: 'TAC-C-0143 tripped on E-31. How do I reset it?' },
    { icon: History, title: 'Check a machine’s history', text: 'TAC-C-0142 tripped E-14 again. Has this happened before and what fixed it?' },
  ],
  'family-chillers': [
    { icon: Siren, title: 'Troubleshoot an alarm', text: 'RTM-H-0007 has an L-03 alarm again, suction at 1.9 bar. What should I do?' },
  ],
  'family-conveyors': [
    { icon: FileSearch, title: 'Find a known fix', text: "Just put a new belt on a BX-12 and it keeps tracking left. I've adjusted the take-up a few times. Any known fix?" },
    { icon: ClipboardCheck, title: 'Warranty question', text: "What's the warranty period on a Belton BX-20?" },
  ],
}

export function AskPage({ me, users, onSwitch, onOpenWO, notify }: {
  me: User; users: User[]; onSwitch: (uid: string) => Promise<void>; onOpenWO: (id: string) => void
  notify: (msg: string, tone?: 'error' | 'ok') => void
}) {
  const convs = useQuery({ queryKey: ['conversations', me.id], queryFn: api.conversations })
  const [convId, setConvId] = useState<string | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [creatingWO, setCreatingWO] = useState(false)
  const [sidebar, setSidebar] = useState(false)
  const bottom = useRef<HTMLDivElement>(null)
  const loadedByUs = useRef<string | null>(null)

  useEffect(() => {
    if (convId === null && convs.data?.conversations.length) setConvId(convs.data.conversations[0].id)
  }, [convs.data, convId])
  useEffect(() => {
    if (!convId || convId === NEW || convId === loadedByUs.current) return
    let stale = false
    api.conversation(convId)
      .then((c) => { if (!stale) setTurns(c.messages.map((m) => ({ question: m.question, response: m.answer }))) })
      .catch(() => { if (!stale) setTurns([]) })
    return () => { stale = true }
  }, [convId])
  // Bring the newest question into view (and again when its answer lands) so reading starts at the headline.
  const lastAnswered = Boolean(turns.at(-1)?.response)
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, [turns.length, lastAnswered])

  // "Ask as another technician" hands over only the question text; it's asked again as a new request.
  useEffect(() => {
    const pending = sessionStorage.getItem('askAgain')
    if (pending) { sessionStorage.removeItem('askAgain'); setConvId(NEW); setTurns([]); void send(pending, true) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function send(q: string, fresh = false) {
    if (!q.trim() || busy) return
    setBusy(true)
    setQuestion('')
    const conversation = fresh || convId === NEW ? null : convId
    setTurns((t) => [...(fresh ? [] : t), { question: q, stage: 0 }])
    try {
      const response = await askStream(q, conversation, (message) =>
        setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, stage: stageOf(message) } : x))))
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { question: q, response } : x)))
      loadedByUs.current = response.conversation_id
      setConvId(response.conversation_id)
      void convs.refetch()
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Something went wrong. Please try again.'
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { question: q, error: msg } : x)))
    } finally {
      setBusy(false)
    }
  }

  async function createWorkOrder(messageId: string) {
    setCreatingWO(true)
    try {
      const wo = await api.createWorkOrder(messageId)
      notify('Work order draft created')
      onOpenWO(wo.id)
    } catch (e) {
      notify(e instanceof ApiError ? e.message : 'Could not create the work order', 'error')
    } finally {
      setCreatingWO(false)
    }
  }

  const suggestions = me.groups.flatMap((g) => SUGGESTIONS[g] ?? []).slice(0, 4)
  const firstName = me.name.split(' ')[0]

  const History_ = (
    <div className="flex h-full flex-col">
      <button onClick={() => { setConvId(NEW); setTurns([]); setSidebar(false) }}
        className="flex items-center justify-center gap-2 rounded-xl bg-slate-900 px-3 py-2.5 text-sm font-semibold text-white hover:bg-slate-800">
        <Plus className="h-4 w-4" />New question
      </button>
      <p className="mt-6 px-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Recent</p>
      <ul className="mt-2 flex-1 space-y-0.5 overflow-y-auto">
        {convs.data?.conversations.length === 0 && <li className="px-2 text-sm text-slate-400">No questions yet</li>}
        {convs.data?.conversations.map((c) => (
          <li key={c.id}>
            <button onClick={() => { loadedByUs.current = null; setConvId(c.id); setSidebar(false) }}
              className={cx('w-full rounded-xl px-3 py-2 text-left transition hover:bg-white',
                convId === c.id && 'bg-white shadow-sm ring-1 ring-slate-200')}>
              <span className="line-clamp-2 text-sm text-slate-800">{c.first_question ?? 'Untitled'}</span>
              <span className="text-xs text-slate-400">{timeAgo(c.created_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )

  return (
    <div className="mx-auto flex max-w-6xl gap-6 px-4">
      <aside className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-64 shrink-0 py-6 lg:block">{History_}</aside>
      {sidebar && (
        <div className="fixed inset-0 z-50 bg-slate-900/30 lg:hidden" onClick={() => setSidebar(false)}>
          <div className="h-full w-72 bg-slate-50 p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <button onClick={() => setSidebar(false)} className="mb-3 rounded-lg p-1 text-slate-500 hover:bg-slate-200"><X className="h-5 w-5" /></button>
            {History_}
          </div>
        </div>
      )}

      <section className="flex min-h-[calc(100vh-3.5rem)] min-w-0 flex-1 flex-col py-6">
        <button onClick={() => setSidebar(true)} className="mb-4 inline-flex w-fit items-center gap-2 rounded-lg px-2 py-1 text-sm text-slate-600 hover:bg-slate-200 lg:hidden">
          <PanelLeft className="h-4 w-4" />Recent questions
        </button>

        <div className="flex-1 space-y-6">
          {turns.length === 0 && (
            <div className="pt-6 sm:pt-12">
              <h1 className="text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl">Hi {firstName}, what are you working on?</h1>
              <p className="mt-2 text-slate-600">Ask about a fault code, a spec, a procedure or a machine's history. Every answer shows where it came from.</p>
              <div className="mt-8 grid gap-3 sm:grid-cols-2">
                {suggestions.map((s) => (
                  <button key={s.text} onClick={() => void send(s.text)}
                    className="group rounded-2xl bg-white p-4 text-left shadow-sm ring-1 ring-slate-200 transition hover:-translate-y-0.5 hover:shadow-md hover:ring-indigo-300">
                    <s.icon className="h-5 w-5 text-indigo-600" />
                    <p className="mt-3 text-sm font-semibold text-slate-900">{s.title}</p>
                    <p className="mt-1 text-sm text-slate-500">{s.text}</p>
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((t, i) => (
            <div key={i} ref={i === turns.length - 1 ? bottom : undefined} className="scroll-mt-20 space-y-3">
              <div className="flex justify-end">
                <p className="max-w-[85%] rounded-2xl rounded-br-md bg-slate-900 px-4 py-2.5 text-sm text-white">{t.question}</p>
              </div>
              {t.stage !== undefined && !t.response && !t.error && <Progress stage={t.stage} />}
              {t.error && (
                <Card className="p-4 text-sm text-red-700">
                  {t.error}
                  <button className="ml-2 font-semibold underline" onClick={() => void send(t.question)}>Try again</button>
                </Card>
              )}
              {t.response && (
                <AnswerCard response={t.response} question={t.question} users={users} creatingWO={creatingWO}
                  onCreateWorkOrder={createWorkOrder}
                  onFollowUp={(text) => void send(text)}
                  onAskAs={async (uid, q) => { sessionStorage.setItem('askAgain', q); await onSwitch(uid) }} />
              )}
            </div>
          ))}
        </div>

        <form className="sticky bottom-0 mt-6 bg-gradient-to-t from-slate-100 via-slate-100 to-transparent pb-4 pt-6"
          onSubmit={(e) => { e.preventDefault(); void send(question) }}>
          <div className="flex items-end gap-2 rounded-2xl bg-white p-2 shadow-lg ring-1 ring-slate-200 focus-within:ring-2 focus-within:ring-indigo-400">
            <textarea value={question} onChange={(e) => setQuestion(e.target.value)} disabled={busy} rows={1}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(question) } }}
              placeholder="Describe the problem, e.g. “TAC-C-0143 tripped on E-31”"
              className="max-h-40 flex-1 resize-none bg-transparent px-3 py-2.5 text-sm outline-none placeholder:text-slate-400" />
            <button disabled={busy || !question.trim()} aria-label="Ask"
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-slate-900 text-white transition hover:bg-slate-800 disabled:bg-slate-300">
              {busy ? <Spinner /> : <ArrowUp className="h-5 w-5" />}
            </button>
          </div>
          <p className="mt-2 text-center text-xs text-slate-400">Each question is answered on its own, so include the machine ID every time (e.g. TAC-C-0143).</p>
        </form>
      </section>
    </div>
  )
}

function Progress({ stage }: { stage: number }) {
  return (
    <Card className="p-5">
      <div className="flex items-center gap-2 text-sm font-medium text-slate-700"><Spinner className="text-indigo-600" />{STAGES[stage]}…</div>
      <ol className="mt-4 grid grid-cols-4 gap-2">
        {STAGES.map((s, i) => (
          <li key={s}>
            <div className={cx('h-1.5 rounded-full transition-all', i < stage ? 'bg-emerald-500' : i === stage ? 'animate-pulse bg-indigo-500' : 'bg-slate-200')} />
            <p className={cx('mt-1.5 hidden items-center gap-1 text-[11px] sm:flex', i <= stage ? 'text-slate-700' : 'text-slate-400')}>
              {i < stage && <Check className="h-3 w-3 text-emerald-600" />}{s}
            </p>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-xs text-slate-400">Answers are checked against the documents before they're shown. This can take up to a minute.</p>
    </Card>
  )
}
