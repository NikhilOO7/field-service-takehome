import { useQueries, useQuery } from '@tanstack/react-query'
import { Cpu, ExternalLink, FileText, ScanLine, X } from 'lucide-react'
import { useEffect } from 'react'
import { api, type AskResponse } from '../api'
import { DOC_TYPE } from '../lib/labels'
import { Spinner, cx } from './ui'

export function SourceDrawer({ id, number, response, onClose }:
  { id: string; number?: number; response: AskResponse; onClose: () => void }) {
  const source = response.sources.find((s) => s.id === id)
  const fact = response.facts.find((f) => f.id === id)
  const detail = useQuery({ queryKey: ['source', source?.chunk_id], queryFn: () => api.source(source!.chunk_id), enabled: !!source })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const d = detail.data
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-900/30 backdrop-blur-[1px]" onClick={onClose}>
      <aside className="flex h-full w-full max-w-lg flex-col bg-white shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <header className="flex items-start gap-3 border-b border-slate-100 p-5">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-indigo-50 text-indigo-700">
            {fact ? <Cpu className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-slate-500">
              Source {number ?? ''} · {fact ? 'System check' : DOC_TYPE[d?.doc_type ?? source?.doc_type ?? ''] ?? 'Document'}
            </p>
            <h3 className="mt-0.5 font-semibold leading-snug text-slate-900">{d?.title ?? fact?.label ?? source?.label}</h3>
            {d && (
              <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] font-medium">
                <span className={cx('rounded-full px-2 py-0.5', d.doc_status === 'current' ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700')}>
                  {d.doc_status === 'current' ? 'Current version' : 'Out of date'}
                </span>
                {d.revision && <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">Revision {d.revision}</span>}
                {d.effective_date && <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">{d.effective_date}</span>}
                {d.flags.includes('ocr') && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-amber-800"><ScanLine className="h-3 w-3" />Scanned page</span>
                )}
              </div>
            )}
          </div>
          <button onClick={onClose} className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Close"><X className="h-5 w-5" /></button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto p-5 text-sm">
          {fact && (
            <>
              <p className="rounded-xl bg-slate-50 p-4 leading-relaxed text-slate-800">{fact.text}</p>
              <p className="text-xs text-slate-500">Worked out by the system from the equipment list, the manufacturer notices' serial ranges and the service history you can access.</p>
              <FactEvidence chunkIds={fact.chunk_ids ?? []} />
            </>
          )}
          {source && detail.isLoading && <p className="flex items-center gap-2 text-slate-500"><Spinner />Loading…</p>}
          {source && detail.error && <p className="rounded-xl bg-red-50 p-3 text-red-700">This source isn't available to you.</p>}
          {d && (
            <>
              <p className="text-xs font-medium text-slate-500">{d.label}</p>
              <div className="space-y-2">
                {d.context.map((c) => (
                  <p key={c.id} className={cx('whitespace-pre-wrap rounded-xl p-3 leading-relaxed',
                    c.id === d.chunk_id ? 'bg-amber-50 text-slate-900 ring-1 ring-amber-200' : 'text-slate-400')}>
                    {c.text}
                  </p>
                ))}
              </div>
            </>
          )}
        </div>

        {d && (
          <footer className="border-t border-slate-100 p-4">
            <a href={api.fileUrl(d.doc_id, d.locator.page)} target="_blank" rel="noreferrer"
              className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-800">
              <ExternalLink className="h-4 w-4" />Open the original{d.locator.page ? ` at page ${d.locator.page}` : ''}
            </a>
          </footer>
        )}
      </aside>
    </div>
  )
}

/** The records a computed fact was built from, each opened through the same access-checked source endpoint. */
function FactEvidence({ chunkIds }: { chunkIds: string[] }) {
  const results = useQueries({
    queries: chunkIds.slice(0, 10).map((id) => ({ queryKey: ['source', id], queryFn: () => api.source(id) })),
  })
  if (chunkIds.length === 0) return null
  return (
    <div>
      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Based on</p>
      <ul className="space-y-2">
        {results.map((r, i) => r.data ? (
          <li key={chunkIds[i]} className="rounded-xl p-3 ring-1 ring-slate-200">
            <p className="text-xs font-medium text-slate-500">{r.data.label}</p>
            <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-slate-700">{r.data.text}</p>
            <a href={api.fileUrl(r.data.doc_id, r.data.locator.page)} target="_blank" rel="noreferrer"
              className="mt-2 inline-flex items-center gap-1 text-xs font-semibold text-indigo-700 hover:underline">
              <ExternalLink className="h-3 w-3" />Open the original
            </a>
          </li>
        ) : r.isLoading ? <li key={chunkIds[i]} className="text-xs text-slate-400">Loading…</li> : null)}
      </ul>
    </div>
  )
}
