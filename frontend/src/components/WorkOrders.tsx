import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle, BookOpen, Check, ClipboardList, Cpu, Inbox, Package, Pencil, Send, ShieldCheck, Wrench, X,
} from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { ApiError, api, type Asset, type User, type WorkOrder } from '../api'
import { timeAgo } from '../lib/labels'
import { Button, Card, Spinner, cx } from './ui'

const CERTS = ['None', 'HV', 'Refrigerant', 'Confined space']
const CERT_LABEL: Record<string, string> = { None: 'None needed', HV: 'High-voltage', Refrigerant: 'Refrigerant', 'Confined space': 'Confined space' }
const PRIORITIES = ['Critical', 'High', 'Medium', 'Low']
const PRIORITY_STYLE: Record<string, string> = {
  Critical: 'bg-red-600 text-white', High: 'bg-orange-500 text-white', Medium: 'bg-amber-100 text-amber-900', Low: 'bg-slate-100 text-slate-700',
}
const STATUS: Record<WorkOrder['status'], { label: string; cls: string }> = {
  draft: { label: 'Draft', cls: 'bg-slate-100 text-slate-700' },
  pending_approval: { label: 'Waiting for approval', cls: 'bg-amber-100 text-amber-900' },
  approved: { label: 'Approved', cls: 'bg-emerald-100 text-emerald-800' },
  sent: { label: 'Sent', cls: 'bg-emerald-600 text-white' },
  rejected: { label: 'Rejected', cls: 'bg-red-100 text-red-800' },
}
const FIELD_LABEL: Record<string, string> = {
  asset_id: 'Machine', fault_code: 'Fault code', summary: 'Summary', observations: 'What you saw', work_steps: 'Work to do',
  procedure_refs: 'Procedures', parts: 'Parts', required_certifications: 'Certification', campaign_code: 'Campaign code',
  priority: 'Priority', priority_reason: 'Reason for priority', related_cases: 'Related cases', site_permits: 'Permits',
  estimated_duration_h: 'Time on site', site: 'Site', model: 'Model', serial: 'Serial',
}

export function WorkOrders({ me, selected, onSelect, notify }: {
  me: User; selected: string | null; onSelect: (id: string | null) => void; notify: (m: string, t?: 'error' | 'ok') => void
}) {
  const list = useQuery({ queryKey: ['work-orders', me.id], queryFn: api.workOrders })
  const mine = list.data?.work_orders.filter((w) => w.created_by === me.id) ?? []
  const queue = list.data?.work_orders.filter((w) => w.created_by !== me.id && w.status === 'pending_approval') ?? []

  return (
    <div className="mx-auto grid max-w-6xl gap-6 px-4 py-6 lg:grid-cols-[300px_1fr]">
      <div className="space-y-6">
        {list.data?.can_approve && <WOList title="Waiting for your approval" empty="Nothing to approve" items={queue} selected={selected} onSelect={onSelect} />}
        <WOList title="My work orders" empty="Create one from an answer" items={mine} selected={selected} onSelect={onSelect} />
      </div>
      {selected ? <WODetail key={selected} id={selected} me={me} notify={notify} /> : (
        <Card className="flex flex-col items-center justify-center p-12 text-center">
          <ClipboardList className="h-10 w-10 text-slate-300" />
          <p className="mt-3 font-medium text-slate-900">No work order selected</p>
          <p className="mt-1 max-w-sm text-sm text-slate-500">When an answer needs follow-up (parts, a certified technician or a return visit), use the button under the answer to create a draft.</p>
        </Card>
      )}
    </div>
  )
}

function WOList({ title, empty, items, selected, onSelect }: {
  title: string; empty: string; items: WorkOrder[]; selected: string | null; onSelect: (id: string) => void
}) {
  return (
    <div>
      <p className="mb-2 px-1 text-xs font-semibold uppercase tracking-wide text-slate-400">{title}</p>
      {items.length === 0 && <p className="px-1 text-sm text-slate-400">{empty}</p>}
      <div className="space-y-2">
        {items.map((w) => (
          <button key={w.id} onClick={() => onSelect(w.id)}
            className={cx('w-full rounded-2xl bg-white p-3 text-left shadow-sm ring-1 transition hover:ring-indigo-300',
              selected === w.id ? 'ring-2 ring-indigo-500' : 'ring-slate-200')}>
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm font-semibold text-slate-900">{String(w.payload.asset_id?.value ?? 'No machine')}</span>
              {w.payload.fault_code?.value ? <span className="rounded bg-slate-100 px-1.5 font-mono text-xs text-slate-600">{String(w.payload.fault_code.value)}</span> : null}
              <span className={cx('ml-auto rounded-full px-2 py-0.5 text-[11px] font-semibold', STATUS[w.status].cls)}>{STATUS[w.status].label}</span>
            </div>
            <p className="mt-1 line-clamp-2 text-xs text-slate-500">{String(w.payload.summary?.value ?? 'No summary yet')}</p>
          </button>
        ))}
      </div>
    </div>
  )
}

function Prov({ p }: { p?: string }) {
  if (!p) return null
  const meta = {
    system: { icon: Cpu, title: 'Filled in automatically' },
    answer: { icon: BookOpen, title: 'From the documents' },
    technician: { icon: Pencil, title: 'Edited by you' },
  }[p as 'system' | 'answer' | 'technician']
  if (!meta) return null
  return <span title={meta.title} className="ml-1.5 inline-flex text-slate-400"><meta.icon className="h-3 w-3" /></span>
}

function Field({ label, prov, children, error }: { label: string; prov?: string; children: ReactNode; error?: string }) {
  return (
    <label className="block">
      <span className="flex items-center text-xs font-medium text-slate-500">{label}<Prov p={prov} /></span>
      <div className="mt-1">{children}</div>
      {error && <span className="mt-1 block text-xs text-red-600">{error}</span>}
    </label>
  )
}

function Group({ icon: Icon, title, children }: { icon: typeof Wrench; title: string; children: ReactNode }) {
  return (
    <Card className="p-5">
      <p className="mb-4 flex items-center gap-2 text-sm font-semibold text-slate-900"><Icon className="h-4 w-4 text-slate-400" />{title}</p>
      <div className="space-y-4">{children}</div>
    </Card>
  )
}

/** Machines in the technician's region, grouped by site. Picking one saves it, and the server refills serial,
 * model, site and the rule-based fields from the fleet register. */
function MachinePicker({ value, disabled, onPick }: { value: string; disabled: boolean; onPick: (assetId: string) => void }) {
  const assets = useQuery({ queryKey: ['assets'], queryFn: api.assets, staleTime: Infinity })
  const bySite = new Map<string, Asset[]>()
  for (const a of assets.data?.assets ?? []) {
    const site = a.site_name ?? 'Other'
    bySite.set(site, [...(bySite.get(site) ?? []), a])
  }
  return (
    <select value={value} disabled={disabled || assets.isLoading} onChange={(e) => e.target.value && onPick(e.target.value)}
      className={cx(input, 'font-mono', !value && 'text-slate-400 ring-red-300')}>
      <option value="">Choose a machine…</option>
      {[...bySite].map(([site, list]) => (
        <optgroup key={site} label={site}>
          {list.map((a) => <option key={a.asset_id} value={a.asset_id}>{a.asset_id} · {a.model}</option>)}
        </optgroup>
      ))}
    </select>
  )
}

const input = 'w-full rounded-xl border-0 bg-slate-50 px-3 py-2 text-sm text-slate-900 ring-1 ring-slate-200 focus:bg-white focus:outline-none focus:ring-2 focus:ring-indigo-400 disabled:bg-transparent disabled:ring-0 disabled:px-0'

function WODetail({ id, me, notify }: { id: string; me: User; notify: (m: string, t?: 'error' | 'ok') => void }) {
  const qc = useQueryClient()
  const wo = useQuery({ queryKey: ['work-order', id, me.id], queryFn: () => api.workOrder(id) })
  const [form, setForm] = useState<Record<string, unknown>>({})
  const [confirming, setConfirming] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')

  useEffect(() => {
    if (wo.data) setForm(Object.fromEntries(Object.entries(wo.data.payload).map(([k, v]) => [k, v.value])))
  }, [wo.data])

  const refresh = (w: WorkOrder) => {
    qc.setQueryData(['work-order', id, me.id], w)
    void qc.invalidateQueries({ queryKey: ['work-orders'] })
  }
  const onError = (e: unknown) => {
    if (e instanceof ApiError) {
      const d = e.detail as { message?: string; errors?: { field: string; message: string }[] }
      const first = d?.errors?.[0]
      notify(first ? `${FIELD_LABEL[first.field] ?? first.field}: ${first.message}` : (d?.message ?? e.message), 'error')
      window.scrollTo({ top: 0, behavior: 'smooth' })
      void wo.refetch()
    } else notify(String(e), 'error')
  }
  const save = useMutation({ mutationFn: (c: Record<string, unknown>) => api.editWorkOrder(id, c), onSuccess: (w) => { refresh(w); notify('Saved') }, onError })
  const submit = useMutation({ mutationFn: () => api.submitWorkOrder(id), onSuccess: (w) => { refresh(w); notify('Sent for approval') }, onError })
  const approve = useMutation({ mutationFn: () => api.approveWorkOrder(id), onSuccess: (w) => { setConfirming(false); refresh(w); notify('Approved and sent') }, onError })
  const reject = useMutation({ mutationFn: () => api.rejectWorkOrder(id, reason), onSuccess: (w) => { setRejecting(false); refresh(w); notify('Rejected') }, onError })

  if (wo.isLoading) return <Card className="flex items-center justify-center p-12"><Spinner className="text-slate-400" /></Card>
  if (wo.error || !wo.data) return <Card className="p-6 text-sm text-slate-600">This work order isn't available to you.</Card>

  const w = wo.data
  const p = w.payload
  const editable = w.status === 'draft' && w.created_by === me.id
  const canApprove = w.status === 'pending_approval' && me.can_approve
  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }))
  const changes = () => Object.fromEntries(Object.entries(form).filter(([k, v]) =>
    k in p && !k.startsWith('_') && JSON.stringify(v) !== JSON.stringify(p[k].value)))
  const errors = w.validation.filter((v) => v.severity === 'error')
  const warnings = w.validation.filter((v) => v.severity === 'warning')
  const errorFor = (f: string) => errors.filter((e) => e.field === f).map((e) => e.message).join('; ') || undefined
  const certs = (form.required_certifications as string[] | undefined) ?? []
  const parts = (form.parts as { part_no: string; qty: number; description?: string }[] | undefined) ?? []
  const steps = (form.work_steps as string[] | undefined) ?? []
  const steps_ = ['draft', 'pending_approval', 'sent'] as const
  const stepIndex = w.status === 'rejected' ? 1 : Math.max(0, steps_.indexOf(w.status === 'approved' ? 'sent' : w.status as typeof steps_[number]))

  return (
    <div className="space-y-4 pb-24">
      {/* header + progress */}
      <Card className="p-5">
        <div className="flex flex-wrap items-start gap-3">
          <div>
            <p className="text-xs font-medium text-slate-500">Work order</p>
            <h2 className="text-xl font-semibold text-slate-900">{String(p.asset_id?.value ?? 'No machine')} {p.fault_code?.value ? `· ${String(p.fault_code.value)}` : ''}</h2>
            <p className="text-sm text-slate-500">{String(p.model?.value ?? '')} {p.site?.value ? `at ${String(p.site.value)}` : ''}</p>
          </div>
          <span className={cx('ml-auto rounded-full px-3 py-1 text-xs font-semibold', STATUS[w.status].cls)}>{STATUS[w.status].label}</span>
        </div>
        <ol className="mt-5 grid grid-cols-3 gap-2 text-xs">
          {['Draft', 'Approval', 'Sent'].map((s, i) => (
            <li key={s}>
              <div className={cx('h-1.5 rounded-full', w.status === 'rejected' && i === 1 ? 'bg-red-400' : i <= stepIndex ? 'bg-emerald-500' : 'bg-slate-200')} />
              <p className={cx('mt-1.5', i <= stepIndex ? 'font-medium text-slate-700' : 'text-slate-400')}>{s}</p>
            </li>
          ))}
        </ol>
        {w.rejected_reason && <p className="mt-4 rounded-xl bg-red-50 p-3 text-sm text-red-800">Rejected: {w.rejected_reason}</p>}
        {errors.length > 0 && (
          <div className="mt-4 rounded-xl bg-red-50 p-3 ring-1 ring-red-100">
            <p className="flex items-center gap-2 text-sm font-semibold text-red-800"><AlertCircle className="h-4 w-4" />{errors.length} thing{errors.length > 1 ? 's' : ''} to fix before {editable ? 'submitting' : 'approval'}</p>
            <ul className="mt-2 space-y-1 text-sm text-red-800">
              {errors.map((e, i) => <li key={i}>• <span className="font-medium">{FIELD_LABEL[e.field] ?? e.field}:</span> {e.message}</li>)}
            </ul>
          </div>
        )}
        {warnings.length > 0 && (
          <ul className="mt-3 space-y-1 text-sm text-amber-800">
            {warnings.map((e, i) => <li key={i}>⚠ <span className="font-medium">{FIELD_LABEL[e.field] ?? e.field}:</span> {e.message}</li>)}
          </ul>
        )}
        {editable && (
          <p className="mt-4 flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span className="inline-flex items-center gap-1"><Cpu className="h-3 w-3" />filled in automatically</span>
            <span className="inline-flex items-center gap-1"><BookOpen className="h-3 w-3" />from the documents</span>
            <span className="inline-flex items-center gap-1"><Pencil className="h-3 w-3" />edited by you</span>
          </p>
        )}
      </Card>

      <Group icon={Wrench} title="The problem">
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Machine" prov={p.asset_id?.provenance} error={errorFor('asset_id')}>
            {editable
              ? <MachinePicker value={(p.asset_id?.value as string | null) ?? ''} disabled={save.isPending}
                  onPick={(assetId) => save.mutate({ ...changes(), asset_id: assetId })} />
              : <p className="font-mono text-sm font-semibold">{String(p.asset_id?.value ?? '—')}</p>}
          </Field>
          <Field label="Serial" prov={p.serial?.provenance}><p className="font-mono text-sm">{String(p.serial?.value ?? '—')}</p></Field>
          <Field label="Fault code" prov={p.fault_code?.provenance} error={errorFor('fault_code')}>
            <input disabled={!editable} className={cx(input, 'font-mono')} value={String(form.fault_code ?? '')} onChange={(e) => set('fault_code', e.target.value)} />
          </Field>
        </div>
        <Field label="Summary" prov={p.summary?.provenance} error={errorFor('summary')}>
          <textarea disabled={!editable} rows={2} className={input} value={String(form.summary ?? '')} onChange={(e) => set('summary', e.target.value)} />
        </Field>
        <Field label="What you saw on site" prov={p.observations?.provenance} error={errorFor('observations')}>
          <textarea disabled={!editable} rows={2} className={input} placeholder="Readings, alarm time, what the machine was doing…"
            value={String(form.observations ?? '')} onChange={(e) => set('observations', e.target.value)} />
        </Field>
      </Group>

      <Group icon={ClipboardList} title="Work to do">
        <Field label="Steps (one per line, refer to procedure numbers)" prov={p.work_steps?.provenance} error={errorFor('work_steps')}>
          {editable
            ? <textarea rows={Math.max(3, steps.length + 1)} className={input} value={steps.join('\n')} onChange={(e) => set('work_steps', e.target.value.split('\n').filter(Boolean))} />
            : <ol className="list-decimal space-y-1 pl-5 text-sm text-slate-800">{steps.map((s) => <li key={s}>{s}</li>)}</ol>}
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Procedures" prov={p.procedure_refs?.provenance} error={errorFor('procedure_refs')}>
            <div className="flex flex-wrap gap-1.5">{((p.procedure_refs?.value as string[]) ?? []).map((d) => <span key={d} className="rounded-full bg-slate-100 px-2.5 py-1 font-mono text-xs">{d}</span>)}</div>
          </Field>
          <Field label="Time on site (hours)" prov={p.estimated_duration_h?.provenance}>
            <input type="number" min={0} step={0.5} disabled={!editable} className={input}
              value={String(form.estimated_duration_h ?? '')} onChange={(e) => set('estimated_duration_h', e.target.value === '' ? null : Number(e.target.value))} />
          </Field>
        </div>
      </Group>

      <Group icon={Package} title="Parts">
        {parts.length === 0 && <p className="text-sm text-slate-400">No parts needed</p>}
        {parts.map((pt, i) => (
          <div key={i} className="flex items-center gap-3 rounded-xl bg-slate-50 p-3">
            <div className="min-w-0 flex-1">
              <p className="font-mono text-sm font-semibold">{pt.part_no}</p>
              <p className="truncate text-xs text-slate-500">{pt.description ?? 'Part from the documents'}</p>
            </div>
            <label className="flex items-center gap-2 text-xs text-slate-500">Qty
              <input type="number" min={1} disabled={!editable} value={pt.qty}
                onChange={(e) => set('parts', parts.map((x, j) => (j === i ? { ...x, qty: Number(e.target.value) } : x)))}
                className="w-16 rounded-lg bg-white px-2 py-1 text-sm ring-1 ring-slate-200 disabled:bg-transparent disabled:ring-0" />
            </label>
          </div>
        ))}
        {errorFor('parts') && <p className="text-xs text-red-600">{errorFor('parts')}</p>}
      </Group>

      <Group icon={ShieldCheck} title="Safety, priority and paperwork">
        <Field label="Who can do this work" prov={p.required_certifications?.provenance} error={errorFor('required_certifications')}>
          <div className="flex flex-wrap gap-2">
            {CERTS.map((c) => {
              const on = certs.includes(c)
              return (
                <button type="button" key={c} disabled={!editable}
                  onClick={() => set('required_certifications', on ? certs.filter((x) => x !== c) : [...certs.filter((x) => c === 'None' ? false : x !== 'None'), c])}
                  className={cx('rounded-full px-3 py-1.5 text-sm font-medium ring-1 transition disabled:cursor-default',
                    on ? 'bg-slate-900 text-white ring-slate-900' : 'bg-white text-slate-600 ring-slate-200 hover:ring-slate-300')}>
                  {on && <Check className="mr-1 inline h-3.5 w-3.5" />}{CERT_LABEL[c]}
                </button>
              )
            })}
          </div>
        </Field>
        <Field label="Priority" prov={p.priority?.provenance} error={errorFor('priority')}>
          <div className="flex flex-wrap gap-2">
            {PRIORITIES.map((x) => (
              <button type="button" key={x} disabled={!editable} onClick={() => set('priority', x)}
                className={cx('rounded-full px-3 py-1.5 text-sm font-semibold transition disabled:cursor-default',
                  form.priority === x ? PRIORITY_STYLE[x] : 'bg-white text-slate-500 ring-1 ring-slate-200')}>{x}</button>
            ))}
          </div>
        </Field>
        <Field label="Why this priority" prov={p.priority_reason?.provenance} error={errorFor('priority_reason')}>
          <input disabled={!editable} className={input} value={String(form.priority_reason ?? '')} onChange={(e) => set('priority_reason', e.target.value)} />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Permits needed" prov={p.site_permits?.provenance}>
            <ul className="space-y-1 text-sm text-slate-700">{((p.site_permits?.value as string[]) ?? []).map((x) => <li key={x}>• {x}</li>)}
              {!((p.site_permits?.value as string[]) ?? []).length && <li className="text-slate-400">None</li>}</ul>
          </Field>
          <Field label="Manufacturer notices" prov={p.bulletins_checked?.provenance} error={errorFor('campaign_code')}>
            <p className="text-sm text-slate-700">{String(p.bulletins_checked?.value ?? '—')}</p>
            {p.campaign_code?.value ? <p className="mt-1 text-xs text-slate-500">Campaign code <span className="font-mono font-semibold">{String(p.campaign_code.value)}</span></p> : null}
          </Field>
        </div>
        {((p.related_cases?.value as string[]) ?? []).length > 0 && (
          <Field label="Related cases" prov={p.related_cases?.provenance}>
            <p className="font-mono text-sm">{((p.related_cases?.value as string[]) ?? []).join(', ')}</p>
          </Field>
        )}
      </Group>

      {w.events && w.events.length > 0 && (
        <details className="px-1 text-xs text-slate-500">
          <summary className="cursor-pointer">Activity ({w.events.length})</summary>
          <ul className="mt-2 space-y-1">{w.events.map((e, i) => <li key={i}>{timeAgo(e.at)} · {e.actor} {e.action}</li>)}</ul>
        </details>
      )}

      {/* sticky actions */}
      {(editable || canApprove) && (
        <div className="fixed inset-x-0 bottom-0 z-30 border-t border-slate-200 bg-white/95 backdrop-blur">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-end gap-2 px-4 py-3">
            {editable && (
              <>
                <Button variant="ghost" disabled={save.isPending} onClick={() => save.mutate(changes())}>Save draft</Button>
                <Button variant="primary" disabled={submit.isPending}
                  onClick={async () => { const c = changes(); if (Object.keys(c).length) await save.mutateAsync(c); submit.mutate() }}>
                  <Send className="h-4 w-4" />Send for approval
                </Button>
              </>
            )}
            {canApprove && (
              <>
                <Button variant="danger" onClick={() => setRejecting(true)}><X className="h-4 w-4" />Reject</Button>
                <Button variant="success" onClick={() => setConfirming(true)}><Check className="h-4 w-4" />Approve & send</Button>
              </>
            )}
          </div>
        </div>
      )}

      {confirming && (
        <Modal onClose={() => setConfirming(false)}>
          <Inbox className="h-8 w-8 text-emerald-600" />
          <h3 className="mt-3 text-lg font-semibold">Send this work order?</h3>
          <dl className="mt-3 space-y-1 rounded-xl bg-slate-50 p-3 text-sm">
            <div className="flex justify-between"><dt className="text-slate-500">Machine</dt><dd className="font-mono font-semibold">{String(p.asset_id?.value)}</dd></div>
            <div className="flex justify-between"><dt className="text-slate-500">Certification</dt><dd>{((p.required_certifications?.value as string[]) ?? []).map((c) => CERT_LABEL[c] ?? c).join(', ')}</dd></div>
            <div className="flex justify-between"><dt className="text-slate-500">Priority</dt><dd>{String(p.priority?.value)}</dd></div>
            {p.campaign_code?.value ? <div className="flex justify-between"><dt className="text-slate-500">Campaign</dt><dd className="font-mono">{String(p.campaign_code.value)}</dd></div> : null}
          </dl>
          <p className="mt-3 text-sm text-slate-500">Nothing is sent until you say yes. The supervisor then assigns a technician with the right certification.</p>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setConfirming(false)}>No, not yet</Button>
            <Button variant="success" disabled={approve.isPending} onClick={() => approve.mutate()}>
              {approve.isPending ? <Spinner /> : <Check className="h-4 w-4" />}Yes, send it
            </Button>
          </div>
        </Modal>
      )}
      {rejecting && (
        <Modal onClose={() => setRejecting(false)}>
          <h3 className="text-lg font-semibold">Reject this work order?</h3>
          <p className="mt-1 text-sm text-slate-500">Tell the technician what to change.</p>
          <textarea rows={3} autoFocus value={reason} onChange={(e) => setReason(e.target.value)} className={cx(input, 'mt-3')} placeholder="Reason" />
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setRejecting(false)}>Cancel</Button>
            <Button variant="danger" disabled={!reason.trim() || reject.isPending} onClick={() => reject.mutate()}>Reject</Button>
          </div>
        </Modal>
      )}
    </div>
  )
}

function Modal({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/40 p-4 sm:items-center" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>{children}</div>
    </div>
  )
}
