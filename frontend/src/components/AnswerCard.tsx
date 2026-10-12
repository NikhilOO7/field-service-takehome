import {
  AlertTriangle, BookOpen, Check, CheckCircle2, ChevronDown, ClipboardList, Copy, FileWarning, HelpCircle,
  History, Info, Lightbulb, Lock, MoreHorizontal, Scale, ShieldAlert, UserRound,
} from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import type { AskResponse, User } from '../api'
import { CERT_PLAIN, REASON_ACTION } from '../lib/labels'
import { SourceDrawer } from './SourceDrawer'
import { Avatar, Button, Card, Spinner, cx } from './ui'

const STATUS = {
  answered: { label: 'Answer', icon: CheckCircle2, cls: 'bg-emerald-50 text-emerald-700 ring-emerald-200' },
  partial: { label: 'Partial answer', icon: Info, cls: 'bg-amber-50 text-amber-800 ring-amber-200' },
  restricted: { label: 'Certification required', icon: Lock, cls: 'bg-orange-50 text-orange-800 ring-orange-200' },
  not_covered: { label: 'Not in your documents', icon: FileWarning, cls: 'bg-slate-100 text-slate-700 ring-slate-200' },
  clarify: { label: 'Which machine?', icon: HelpCircle, cls: 'bg-sky-50 text-sky-800 ring-sky-200' },
} as const

type Props = {
  response: AskResponse
  question: string
  users: User[]
  creatingWO?: boolean
  onCreateWorkOrder: (messageId: string) => void
  onAskAs: (userId: string, question: string) => void
  onFollowUp: (text: string) => void
}

export function AnswerCard({ response, question, users, creatingWO, onCreateWorkOrder, onAskAs, onFollowUp }: Props) {
  const a = response.answer
  const [openSource, setOpenSource] = useState<string | null>(null)
  const [done, setDone] = useState<Set<number>>(new Set())
  const [menu, setMenu] = useState(false)
  const [copied, setCopied] = useState(false)

  const steps = a.blocks.filter((b) => b.type === 'step')
  const warnings = a.blocks.filter((b) => b.type === 'warning')
  const notes = a.blocks.filter((b) => b.type === 'fact' || b.type === 'note')
  const history = a.blocks.filter((b) => b.type === 'history')
  const status = STATUS[a.status] ?? STATUS.answered
  const certs = response.restricted?.required_certifications ?? []

  // Number citations in reading order: ① ② ③ … instead of internal ids.
  const numbers = useMemo(() => {
    const order: string[] = []
    const add = (ids: string[]) => ids.forEach((id) => { if (!order.includes(id)) order.push(id) })
    add(a.headline.citations)
    ;[...steps, ...warnings].forEach((b) => add(b.citations))
    a.restricted_notice?.what_you_can_do.forEach((w) => add(w.citations))
    ;[...notes, ...history].forEach((b) => add(b.citations))
    a.conflicts.forEach((c) => { c.positions.forEach((p) => add(p.citations)); add(c.resolution.citations) })
    return new Map(order.map((id, i) => [id, i + 1]))
  }, [a]) // eslint-disable-line react-hooks/exhaustive-deps

  const Cite = ({ ids }: { ids: string[] }) => (
    <span className="ml-1 inline-flex gap-0.5 align-super">
      {[...new Set(ids)].filter((id) => numbers.has(id)).map((id) => (
        <button key={id} onClick={() => setOpenSource(id)} title="Show source"
          className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-indigo-50 px-1 text-[10px] font-bold text-indigo-700 ring-1 ring-indigo-200 hover:bg-indigo-600 hover:text-white">
          {numbers.get(id)}
        </button>
      ))}
    </span>
  )

  const primaryReason = a.follow_up.reasons[0]
  const ctaLabel = a.status === 'restricted' && certs.length
    ? `Request a ${CERT_PLAIN[certs[0]] ?? ''} certified technician`
    : (primaryReason && REASON_ACTION[primaryReason]) || 'Create work order'

  const copyText = () => {
    const lines = [a.headline.text, ...steps.map((s, i) => `${i + 1}. ${s.text}`), ...warnings.map((w) => `! ${w.text}`)]
    void navigator.clipboard?.writeText(lines.join('\n'))
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const sourceList = [...numbers.entries()].map(([id, n]) => {
    const s = response.sources.find((x) => x.id === id)
    const f = response.facts.find((x) => x.id === id)
    return { id, n, label: s?.label ?? f?.label ?? id, outdated: s && s.doc_status !== 'current', system: !!f }
  })

  return (
    <Card className="overflow-visible">
      <div className="space-y-5 p-5 sm:p-6">
        {/* status + machine */}
        <div className="flex flex-wrap items-center gap-2">
          <span className={cx('inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ring-1', status.cls)}>
            <status.icon className="h-3.5 w-3.5" />{status.label}
          </span>
          {response.assets.map((x) => (
            <span key={x.asset_id} className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-700">
              <span className="font-mono font-semibold">{x.asset_id}</span>
              <span className="text-slate-400">·</span>{x.model}<span className="text-slate-400">·</span>{x.site}
            </span>
          ))}
        </div>

        <p className="text-lg font-semibold leading-snug text-slate-900 sm:text-xl">
          {a.headline.text}<Cite ids={a.headline.citations} />
        </p>

        {/* manufacturer notices that apply to this exact machine */}
        {response.bulletins.length > 0 && (
          <div className="space-y-2">
            {response.bulletins.map((b) => (
              <div key={b.number + b.asset_id} className="flex gap-3 rounded-xl bg-indigo-50 p-3 ring-1 ring-indigo-100">
                <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-indigo-600" />
                <div className="text-sm">
                  <p className="font-semibold text-indigo-900">
                    Manufacturer notice {b.number} applies to this machine{b.campaign_code ? ` (campaign ${b.campaign_code})` : ''}
                  </p>
                  <p className="mt-0.5 text-indigo-900/80">{b.effect.split('. ')[0]}.</p>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* certification needed */}
        {a.status === 'restricted' && (
          <div className="rounded-xl bg-orange-50 p-4 ring-1 ring-orange-200">
            <div className="flex gap-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-orange-100"><Lock className="h-4 w-4 text-orange-700" /></span>
              <div>
                <p className="font-semibold text-orange-900">
                  {certs.length ? `This job needs a ${certs.map((c) => CERT_PLAIN[c] ?? c).join(' and ')} certified technician` : 'This job needs a certified technician'}
                </p>
                <p className="mt-1 text-sm text-orange-900/80">
                  {response.restricted?.procedures.length
                    ? `The procedure (${response.restricted.procedures.map((p) => p.doc_number ?? p.title).join(', ')}) is only available to certified technicians, so its steps aren't shown here.`
                    : "You don't hold the certification this work needs, so the steps aren't shown here."}
                </p>
              </div>
            </div>
            {a.restricted_notice && a.restricted_notice.what_you_can_do.length > 0 && (
              <div className="mt-4">
                <p className="mb-2 text-xs font-bold uppercase tracking-wide text-orange-900/70">What you can do now</p>
                <ul className="space-y-2">
                  {a.restricted_notice.what_you_can_do.map((w, i) => (
                    <li key={i} className="flex gap-2 text-sm text-slate-800">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-orange-600" />
                      <span>{w.text}<Cite ids={w.citations} /></span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {/* which machine? */}
        {a.clarify_question && (
          <div className="rounded-xl bg-sky-50 p-4 ring-1 ring-sky-100">
            <p className="text-sm font-medium text-sky-900">{a.clarify_question.text}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {response.clarify_options.map((o) => (
                <Button key={o} variant="secondary" className="font-mono" onClick={() => onFollowUp(`${question} (asset ${o})`)}>{o}</Button>
              ))}
            </div>
          </div>
        )}

        {/* safety first */}
        {warnings.length > 0 && (
          <div className="space-y-2">
            {warnings.map((w, i) => (
              <div key={i} className="flex gap-3 rounded-xl bg-red-50 p-3 text-sm text-red-900 ring-1 ring-red-100">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
                <span>{w.text}<Cite ids={w.citations} /></span>
              </div>
            ))}
          </div>
        )}

        {/* steps as a checklist */}
        {steps.length > 0 && (
          <div>
            <div className="mb-2 flex items-center justify-between">
              <p className="text-sm font-semibold text-slate-900">Steps</p>
              <span className="text-xs text-slate-500">{done.size} of {steps.length} done</span>
            </div>
            <ol className="space-y-1.5">
              {steps.map((s, i) => {
                const isDone = done.has(i)
                return (
                  <li key={i}>
                    <label className={cx('flex cursor-pointer gap-3 rounded-xl p-3 text-sm ring-1 transition',
                      isDone ? 'bg-slate-50 text-slate-400 ring-slate-100' : 'bg-white text-slate-800 ring-slate-200 hover:ring-slate-300')}>
                      <input type="checkbox" className="sr-only" checked={isDone}
                        onChange={() => setDone((d) => { const n = new Set(d); if (n.has(i)) n.delete(i); else n.add(i); return n })} />
                      <span className={cx('flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold',
                        isDone ? 'bg-emerald-500 text-white' : 'bg-slate-100 text-slate-600')}>
                        {isDone ? <Check className="h-3.5 w-3.5" /> : i + 1}
                      </span>
                      <span className={cx('pt-0.5', isDone && 'line-through')}>
                        {s.text.replace(/^(Step\s*)?\d+[.):]\s*/i, '')}<Cite ids={s.citations} />
                      </span>
                    </label>
                  </li>
                )
              })}
            </ol>
          </div>
        )}

        {/* good to know */}
        {notes.length > 0 && (
          <div className={cx('space-y-2', a.status === 'not_covered' && 'rounded-xl bg-slate-50 p-4')}>
            <p className="flex items-center gap-1.5 text-sm font-semibold text-slate-900">
              <Lightbulb className="h-4 w-4 text-amber-500" />{a.status === 'not_covered' ? 'What we found instead' : 'Good to know'}
            </p>
            <ul className="space-y-1.5">
              {notes.map((n, i) => (
                <li key={i} className="flex gap-2 text-sm text-slate-700">
                  <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-300" />
                  <span>{n.text}<Cite ids={n.citations} /></span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* details, collapsed */}
        <div className="divide-y divide-slate-100 rounded-xl ring-1 ring-slate-200">
          {(history.length > 0 || Object.values(response.history).some((h) => h.events.length)) && (
            <Section icon={History} title="Past jobs on this machine"
              count={history.length || Object.values(response.history).reduce((n, h) => n + h.events.length, 0)}>
              <ul className="space-y-2 text-sm text-slate-700">
                {history.map((h, i) => <li key={i}>{h.text}<Cite ids={h.citations} /></li>)}
              </ul>
              {Object.entries(response.history).map(([asset, h]) => h.events.length > 0 && (
                <ol key={asset} className="mt-3 space-y-2 border-l-2 border-slate-200 pl-4">
                  {h.events.slice(0, 8).map((e) => (
                    <li key={e.source + e.ref} className="relative text-xs text-slate-600">
                      <span className="absolute -left-[21px] top-1 h-2 w-2 rounded-full bg-slate-300" />
                      <span className="font-semibold text-slate-800">{new Date(e.date).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}</span>
                      {e.fault_code && <span className="ml-1.5 rounded bg-slate-100 px-1 font-mono">{e.fault_code}</span>}
                      <p className="mt-0.5 line-clamp-2">{e.notes}</p>
                    </li>
                  ))}
                </ol>
              ))}
            </Section>
          )}
          {a.conflicts.length > 0 && (
            <Section icon={Scale} title="Documents disagree" count={a.conflicts.length} tone="amber">
              <div className="space-y-4">
                {a.conflicts.map((c) => (
                  <div key={c.conflict_id} className="text-sm">
                    <p className="font-medium text-slate-900">{c.topic}</p>
                    <p className="mt-1 rounded-lg bg-emerald-50 p-2 text-emerald-900">
                      <span className="font-semibold">Follow this: </span>{c.resolution.text}<Cite ids={c.resolution.citations} />
                    </p>
                    <ul className="mt-2 space-y-1 text-slate-600">
                      {c.positions.map((p, i) => <li key={i}>• {p.text}<Cite ids={p.citations} /></li>)}
                    </ul>
                  </div>
                ))}
              </div>
            </Section>
          )}
          <Section icon={BookOpen} title={`Based on ${sourceList.length} source${sourceList.length === 1 ? '' : 's'}`}>
            <ul className="space-y-1">
              {sourceList.map((s) => (
                <li key={s.id}>
                  <button onClick={() => setOpenSource(s.id)} className="flex w-full items-start gap-2 rounded-lg p-1.5 text-left text-sm hover:bg-slate-50">
                    <span className="mt-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-indigo-50 px-1 text-[10px] font-bold text-indigo-700">{s.n}</span>
                    <span className="text-slate-700">{s.system ? `System check: ${s.label}` : s.label}</span>
                    {s.outdated && <span className="ml-auto shrink-0 rounded bg-red-50 px-1.5 text-[10px] font-semibold text-red-700">outdated</span>}
                  </button>
                </li>
              ))}
            </ul>
          </Section>
        </div>

        {/* actions */}
        <div className="flex flex-wrap items-center gap-2">
          <Button variant={a.follow_up.needed ? 'primary' : 'secondary'} disabled={creatingWO}
            onClick={() => onCreateWorkOrder(response.message_id)}>
            {creatingWO ? <Spinner /> : <ClipboardList className="h-4 w-4" />}
            {a.follow_up.needed ? ctaLabel : 'Create work order'}
          </Button>
          <Button variant="ghost" onClick={copyText}>
            {copied ? <Check className="h-4 w-4 text-emerald-600" /> : <Copy className="h-4 w-4" />}{copied ? 'Copied' : 'Copy'}
          </Button>
          <div className="relative ml-auto">
            <Button variant="ghost" onClick={() => setMenu((m) => !m)} aria-label="More"><MoreHorizontal className="h-4 w-4" /></Button>
            {menu && (
              <div className="absolute bottom-full right-0 z-20 mb-2 w-72 rounded-xl bg-white p-2 shadow-lg ring-1 ring-slate-200">
                <p className="px-2 pb-1 pt-1 text-xs font-semibold text-slate-500">Ask the same question as…</p>
                <div className="max-h-64 overflow-y-auto">
                  {users.filter((u) => u.id !== response.user.id).map((u) => (
                    <button key={u.id} onClick={() => { setMenu(false); onAskAs(u.id, question) }}
                      className="flex w-full items-center gap-2 rounded-lg p-2 text-left hover:bg-slate-50">
                      <Avatar name={u.name} size="sm" />
                      <span className="text-sm">
                        <span className="font-medium text-slate-900">{u.name}</span>
                        <span className="block text-xs text-slate-500">{u.title}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
        <p className="flex items-center gap-1.5 text-xs text-slate-400">
          <UserRound className="h-3 w-3" />Answered for {response.user.name} using only documents they can access
          {response.timings.validate ? ` · ${Math.round(response.timings.validate)} s` : ''}
        </p>
      </div>
      {openSource && <SourceDrawer id={openSource} number={numbers.get(openSource)} response={response} onClose={() => setOpenSource(null)} />}
    </Card>
  )
}

function Section({ icon: Icon, title, count, tone, children }:
  { icon: typeof History; title: string; count?: number; tone?: 'amber'; children: ReactNode }) {
  return (
    <details className="group">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium text-slate-700 hover:bg-slate-50">
        <Icon className={cx('h-4 w-4', tone === 'amber' ? 'text-amber-500' : 'text-slate-400')} />
        {title}
        {count !== undefined && <span className="rounded-full bg-slate-100 px-2 text-xs text-slate-600">{count}</span>}
        <ChevronDown className="ml-auto h-4 w-4 text-slate-400 transition group-open:rotate-180" />
      </summary>
      <div className="px-4 pb-4">{children}</div>
    </details>
  )
}
