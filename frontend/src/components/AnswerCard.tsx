import {
  AlertTriangle, BookOpen, Check, CheckCircle2, ChevronDown, ClipboardList, Copy, FileWarning, HelpCircle,
  History, Info, ListChecks, Lock, MoreHorizontal, Scale, ShieldAlert, ShieldCheck, Wrench,
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

// Per-outcome look of the answer card: soft tint, icon badge, label.
const TONE: Record<string, { card: string; badge: string; label: string; text: string }> = {
  answered: { card: 'from-emerald-50 ring-emerald-100', badge: 'bg-emerald-500', label: 'Answer', text: 'text-emerald-700' },
  partial: { card: 'from-amber-50 ring-amber-100', badge: 'bg-amber-500', label: 'Partial answer', text: 'text-amber-700' },
  restricted: { card: 'from-orange-50 ring-orange-100', badge: 'bg-orange-500', label: 'Certification required', text: 'text-orange-700' },
  not_covered: { card: 'from-slate-100 ring-slate-200', badge: 'bg-slate-500', label: 'Not in your documents', text: 'text-slate-600' },
  clarify: { card: 'from-sky-50 ring-sky-100', badge: 'bg-sky-500', label: 'Which machine?', text: 'text-sky-700' },
}

// Values a technician scans for: fault codes, document numbers, measurements with units.
const KEY_RE = /(\b[EPLC]-\d{2}\b|\b(?:SB-\d{4}-\d{3}|HV-PROC-\d+|CS-PROC-\d+|SOP-\d+|FS-STD-\d+|KT-WI-\d+|HDN-IOM-\d+)\b|\b\d+(?:[.,]\d+)*\s?(?:N-m|Nm|bar|mm|°C|V DC|V|A|kg|min|months?|hours?|h|s|%)(?![\w]))/g

function highlight(text: string): ReactNode[] {
  return text.split(KEY_RE).map((part, i) => i % 2
    ? <mark key={i} className="rounded-md bg-white px-1.5 py-0.5 font-semibold text-slate-900 ring-1 ring-slate-200">{part}</mark>
    : part)
}

const BASIS: Record<string, string> = {
  newer_revision: 'Why: the newer revision replaces the older one.',
  applicable_bulletin: "Why: the manufacturer notice applies to this machine's serial number.",
  higher_authority: 'Why: the higher-authority document wins (notice > manual > procedure > older instruction > notes).',
  unresolved: 'No rule decides this one: check with your team lead.',
  curated: 'Why: a Corvane rule for this case.',
}

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

  // One quiet link per item, in plain words ("HV-PROC-07 Rev 3 +2"), instead of a row of numbered bubbles.
  const shortLabel = (id: string) => {
    const s = response.sources.find((x) => x.id === id)
    if (!s) return 'System check'
    const doc = s.label.split(' · ')[0]
    return doc.length > 32 ? `${doc.slice(0, 30)}…` : doc
  }
  const cite = (ids: string[]) => {
    // name a document first; computed checks only when nothing else is cited
    const known = [...new Set(ids)].filter((id) => numbers.has(id)).sort((x, y) => Number(x.startsWith('F')) - Number(y.startsWith('F')))
    if (known.length === 0) return null
    return (
      <button onClick={() => setOpenSource(known[0])} title="Show where this comes from"
        className="ml-1.5 inline-flex items-center gap-1 whitespace-nowrap align-baseline text-xs text-slate-400 hover:text-indigo-600 hover:underline">
        <BookOpen className="h-3 w-3" />{shortLabel(known[0])}{known.length > 1 ? ` +${known.length - 1}` : ''}
      </button>
    )
  }

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

  const woButton = (
    <Button variant="primary" className="mt-3" disabled={creatingWO} onClick={() => onCreateWorkOrder(response.message_id)}>
      {creatingWO ? <Spinner /> : <ClipboardList className="h-4 w-4" />}{ctaLabel}
    </Button>
  )
  const machine = response.assets[0]
  const tone = TONE[a.status] ?? TONE.answered
  const faultCodes = [...new Set((question + ' ' + a.headline.text).match(/\b[EPLC]-\d{2}\b/g) ?? [])]
  const pastJobs = Object.values(response.history).reduce((n, h) => n + h.events.length, 0)
  // At-a-glance facts, all taken from structured data rather than the model's prose.
  const glance: { icon: typeof Wrench; text: string; title?: string; cls?: string }[] = [
    ...(machine ? [{ icon: Wrench, text: `${machine.asset_id} · ${machine.model}`, title: machine.site ?? undefined }] : []),
    ...faultCodes.map((c) => ({ icon: AlertTriangle, text: `Fault ${c}`, cls: 'text-red-700 ring-red-200' })),
    ...certs.map((c) => ({ icon: Lock, text: `Needs ${CERT_PLAIN[c] ?? c} certification`, cls: 'text-orange-700 ring-orange-200' })),
    ...(steps.length ? [{ icon: ListChecks, text: `${steps.length} steps` }] : []),
    ...response.bulletins.map((b) => ({ icon: ShieldAlert, text: `Notice ${b.number} applies`, cls: 'text-indigo-700 ring-indigo-200' })),
    ...(a.conflicts.length ? [{ icon: Scale, text: 'Documents disagree', cls: 'text-amber-700 ring-amber-200' }] : []),
    ...(pastJobs ? [{ icon: History, text: `${pastJobs} past job${pastJobs === 1 ? '' : 's'}` }] : []),
  ]
  const hasDetail = notes.length > 0 || response.bulletins.length > 0 || a.conflicts.length > 0
    || history.length > 0 || Object.values(response.history).some((h) => h.events.length)

  return (
    <Card className="overflow-visible">
      <div className="space-y-5 p-5 sm:p-6">
        {/* 1. the answer: the one thing to read first */}
        <div className={cx('rounded-2xl bg-gradient-to-br to-white p-5 ring-1', tone.card)}>
          <div className="flex items-center gap-2.5">
            <span className={cx('flex h-8 w-8 shrink-0 items-center justify-center rounded-xl text-white shadow-sm', tone.badge)}>
              <status.icon className="h-4 w-4" />
            </span>
            <span className={cx('text-sm font-semibold', tone.text)}>{tone.label}</span>
            <span className="ml-auto inline-flex items-center gap-1 text-xs text-slate-500" title="Every value was checked against the documents you can access">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />Checked against {sourceList.length} source{sourceList.length === 1 ? '' : 's'}
            </span>
          </div>
          <p className="mt-3 text-lg leading-relaxed text-slate-900 sm:text-xl sm:leading-relaxed">
            {highlight(a.headline.text)}{cite(a.headline.citations)}
          </p>
          {glance.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-1.5">
              {glance.map((g) => (
                <span key={g.text} title={g.title}
                  className={cx('inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-medium ring-1', g.cls ?? 'text-slate-700 ring-slate-200')}>
                  <g.icon className="h-3.5 w-3.5" />{g.text}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* 2. the next step: exactly one, chosen by the outcome */}
        <NextStep>
          {a.status === 'restricted' ? (
            <>
              <Button variant="primary" disabled={creatingWO} onClick={() => onCreateWorkOrder(response.message_id)}>
                {creatingWO ? <Spinner /> : <ClipboardList className="h-4 w-4" />}{ctaLabel}
              </Button>
              {a.restricted_notice && a.restricted_notice.what_you_can_do.length > 0 && (
                <div className="mt-4">
                  <p className="mb-1.5 text-sm font-medium text-slate-700">While you wait, you can:</p>
                  <ul className="space-y-1.5">
                    {a.restricted_notice.what_you_can_do.map((w, i) => (
                      <li key={i} className="flex gap-2 text-sm text-slate-700">
                        <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" /><span>{w.text}{cite(w.citations)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          ) : a.clarify_question ? (
            <>
              <p className="font-semibold text-slate-900">{a.clarify_question.text}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {response.clarify_options.map((o) => (
                  <Button key={o} variant="secondary" className="font-mono" onClick={() => onFollowUp(`${question} (asset ${o})`)}>{o}</Button>
                ))}
              </div>
            </>
          ) : steps.length > 0 ? (
            <>
              <GuidedSteps steps={steps} warnings={warnings} cite={cite} />
              {a.follow_up.needed && woButton}
            </>
          ) : a.status === 'not_covered' ? (
            <>
              <p className="font-semibold text-slate-900">Ask your team lead or the manufacturer.</p>
              <p className="mt-1 text-sm text-slate-600">None of the documents you can access covers this, so there's nothing safe to give you.</p>
              <Warnings items={warnings} cite={cite} />
            </>
          ) : (warnings.length > 0 || a.follow_up.needed) ? (
            <>
              <Warnings items={warnings} cite={cite} />
              {a.follow_up.needed && woButton}
            </>
          ) : null}
        </NextStep>

        {/* 3. everything else, on demand */}
        {hasDetail && (
          <details className="group">
            <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800">
              <Info className="h-4 w-4" />More detail
              <ChevronDown className="h-3.5 w-3.5 transition group-open:rotate-180" />
            </summary>
            <div className="mt-3 space-y-5 rounded-xl bg-slate-50 p-4 text-sm">
              {notes.length > 0 && (
                <ul className="space-y-1.5">
                  {notes.map((n, i) => (
                    <li key={i} className="flex gap-2 text-slate-700">
                      <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-300" /><span>{n.text}{cite(n.citations)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {response.bulletins.map((b) => (
                <p key={b.number + b.asset_id} className="flex gap-2 text-slate-700">
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-indigo-600" />
                  <span><span className="font-medium">Manufacturer notice {b.number}</span> applies to this machine{b.campaign_code ? ` (campaign ${b.campaign_code})` : ''}: {b.effect.split('. ')[0]}.</span>
                </p>
              ))}
              {a.conflicts.map((c) => (
                <div key={c.conflict_id}>
                  <p className="flex items-center gap-1.5 font-medium text-slate-900"><Scale className="h-4 w-4 text-amber-500" />Documents disagree: {c.topic}</p>
                  <p className="mt-1 text-slate-700"><span className="font-medium">Follow this: </span>{c.resolution.text}{cite(c.resolution.citations)}</p>
                  {c.basis && <p className="mt-0.5 text-xs text-slate-500">{BASIS[c.basis]}</p>}
                  <ul className="mt-1 space-y-0.5 text-slate-500">
                    {c.positions.map((p, i) => <li key={i}>• {p.text}{cite(p.citations)}</li>)}
                  </ul>
                </div>
              ))}
              {(history.length > 0 || Object.values(response.history).some((h) => h.events.length)) && (
                <div>
                  <p className="flex items-center gap-1.5 font-medium text-slate-900"><History className="h-4 w-4 text-slate-400" />Past jobs on this machine</p>
                  <ul className="mt-1 space-y-1 text-slate-700">
                    {history.map((h, i) => <li key={i}>{h.text}{cite(h.citations)}</li>)}
                  </ul>
                  {Object.entries(response.history).map(([asset, h]) => h.events.length > 0 && (
                    <ol key={asset} className="mt-2 space-y-1.5 border-l-2 border-slate-200 pl-3">
                      {h.events.slice(0, 6).map((e) => (
                        <li key={e.source + e.ref} className="text-xs text-slate-600">
                          <span className="font-semibold text-slate-800">{new Date(e.date).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}</span>
                          {e.fault_code && <span className="ml-1.5 rounded bg-slate-100 px-1 font-mono">{e.fault_code}</span>}
                          <span className="ml-1.5 line-clamp-1 inline">{e.notes}</span>
                        </li>
                      ))}
                    </ol>
                  ))}
                </div>
              )}
            </div>
          </details>
        )}

        {/* sources + secondary actions, quiet */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-slate-100 pt-4 text-sm">
          <details className="group">
            <summary className="flex cursor-pointer list-none items-center gap-1.5 text-slate-600 hover:text-slate-900">
              <BookOpen className="h-4 w-4" />{sourceList.length} source{sourceList.length === 1 ? '' : 's'}
              <ChevronDown className="h-3.5 w-3.5 transition group-open:rotate-180" />
            </summary>
            <ul className="mt-2 space-y-1">
              {sourceList.map((s) => (
                <li key={s.id}>
                  <button onClick={() => setOpenSource(s.id)} className="flex w-full items-start gap-2 rounded-lg p-1.5 text-left hover:bg-slate-50">
                    <span className="mt-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-indigo-50 px-1 text-[10px] font-bold text-indigo-700">{s.n}</span>
                    <span className="text-slate-700">{s.system ? `System check: ${s.label}` : s.label}</span>
                    {s.outdated && <span className="ml-auto shrink-0 rounded bg-red-50 px-1.5 text-[10px] font-semibold text-red-700">outdated</span>}
                  </button>
                </li>
              ))}
            </ul>
          </details>
          <div className="ml-auto flex items-center gap-1">
            {!a.follow_up.needed && a.status !== 'restricted' && (
              <Button variant="ghost" disabled={creatingWO} onClick={() => onCreateWorkOrder(response.message_id)}>
                <ClipboardList className="h-4 w-4" />Work order
              </Button>
            )}
            <Button variant="ghost" onClick={copyText}>
              {copied ? <Check className="h-4 w-4 text-emerald-600" /> : <Copy className="h-4 w-4" />}{copied ? 'Copied' : 'Copy'}
            </Button>
            <div className="relative">
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
        </div>
      </div>
      {openSource && <SourceDrawer id={openSource} number={numbers.get(openSource)} response={response} onClose={() => setOpenSource(null)} />}
    </Card>
  )
}

function NextStep({ children }: { children: ReactNode }) {
  // renders nothing when the outcome has no next step (a plain fact answer)
  const empty = children === null || children === false
  if (empty) return null
  return <div className="rounded-2xl bg-slate-50 p-4">{children}</div>
}

function Warnings({ items, cite, title }: {
  items: { text: string; citations: string[] }[]; cite: (ids: string[]) => ReactNode; title?: string
}) {
  if (items.length === 0) return null
  return (
    <div className="mb-3 space-y-2">
      {title && <p className="text-sm font-semibold text-red-800">{title}</p>}
      {items.map((w, i) => (
        <div key={i} className="flex gap-2.5 rounded-xl bg-red-50 p-3 text-sm text-red-900 ring-1 ring-red-100">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" /><span>{w.text}{cite(w.citations)}</span>
        </div>
      ))}
    </div>
  )
}

/** One step at a time, like a technician works: safety first, then Next / Back. The full list is one tap away. */
function GuidedSteps({ steps, warnings, cite }: {
  steps: { text: string; citations: string[] }[]
  warnings: { text: string; citations: string[] }[]
  cite: (ids: string[]) => ReactNode
}) {
  const [i, setI] = useState(0)
  const [showAll, setShowAll] = useState(false)
  const [finished, setFinished] = useState(false)
  const clean = (text: string) => text.replace(/^(Step\s*)?\d+[.):]\s*/i, '')
  const step = steps[i]

  return (
    <div>
      {i === 0 && !finished && <Warnings items={warnings} cite={cite} title="Before you start" />}
      {finished ? (
        <p className="flex items-center gap-1.5 text-sm text-emerald-700"><CheckCircle2 className="h-4 w-4" />All {steps.length} steps done.</p>
      ) : (
        <>
          <div className="mb-2 flex items-center justify-between text-sm">
            <span className="font-semibold text-slate-900">Step {i + 1} of {steps.length}</span>
            {i > 0 && warnings.length > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-red-700"><AlertTriangle className="h-3.5 w-3.5" />Safety notes above step 1</span>
            )}
          </div>
          <div className="mb-3 h-1 overflow-hidden rounded-full bg-slate-200">
            <div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${((i + 1) / steps.length) * 100}%` }} />
          </div>
          <p className="rounded-xl bg-white p-4 text-[15px] leading-relaxed text-slate-800 ring-1 ring-slate-200">
            {clean(step.text)}{cite(step.citations)}
          </p>
          <div className="mt-3 flex items-center gap-2">
            <Button variant="ghost" disabled={i === 0} onClick={() => setI(i - 1)}>Back</Button>
            <Button variant="primary" className="ml-auto"
              onClick={() => (i + 1 < steps.length ? setI(i + 1) : setFinished(true))}>
              {i + 1 < steps.length ? 'Next step' : 'Done'}
            </Button>
          </div>
        </>
      )}
      <button onClick={() => setShowAll((v) => !v)} className="mt-3 text-sm text-slate-500 hover:text-slate-800 hover:underline">
        {showAll ? 'Hide the full list' : `See all ${steps.length} steps`}
      </button>
      {showAll && (
        <ol className="mt-2 space-y-1 text-sm text-slate-600">
          {steps.map((s, n) => (
            <li key={n}>
              <button onClick={() => { setI(n); setFinished(false) }}
                className={cx('flex w-full gap-2 rounded-lg p-2 text-left hover:bg-white', n === i && !finished && 'bg-white font-medium text-slate-900 ring-1 ring-slate-200')}>
                <span className="w-5 shrink-0 text-right text-slate-400">{n + 1}.</span><span className="line-clamp-2">{clean(s.text)}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
