import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { GROUP_LABEL, groupKind, initials } from '../lib/labels'

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(' ')
}

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success'
const VARIANT: Record<Variant, string> = {
  primary: 'bg-slate-900 text-white hover:bg-slate-800 shadow-sm',
  secondary: 'bg-white text-slate-800 ring-1 ring-slate-200 hover:bg-slate-50 shadow-sm',
  ghost: 'text-slate-600 hover:bg-slate-100',
  danger: 'bg-white text-red-700 ring-1 ring-red-200 hover:bg-red-50',
  success: 'bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm',
}

export function Button({ variant = 'secondary', className, children, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button {...rest} className={cx(
      'inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold transition',
      'disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400',
      VARIANT[variant], className)}>
      {children}
    </button>
  )
}

export function Avatar({ name, size = 'md' }: { name: string; size?: 'sm' | 'md' | 'lg' }) {
  const s = { sm: 'h-7 w-7 text-[11px]', md: 'h-9 w-9 text-xs', lg: 'h-11 w-11 text-sm' }[size]
  const hue = [...name].reduce((a, c) => a + c.charCodeAt(0), 0) % 6
  const colors = ['bg-indigo-100 text-indigo-700', 'bg-emerald-100 text-emerald-700', 'bg-amber-100 text-amber-800',
    'bg-sky-100 text-sky-700', 'bg-rose-100 text-rose-700', 'bg-violet-100 text-violet-700'][hue]
  return <span className={cx('inline-flex shrink-0 items-center justify-center rounded-full font-bold', s, colors)}>{initials(name)}</span>
}

const KIND_STYLE = {
  region: 'bg-slate-100 text-slate-700',
  family: 'bg-slate-100 text-slate-700',
  cert: 'bg-amber-50 text-amber-800 ring-1 ring-amber-200',
  role: 'bg-violet-50 text-violet-700 ring-1 ring-violet-200',
}

export function AccessChip({ group }: { group: string }) {
  return (
    <span className={cx('inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium', KIND_STYLE[groupKind(group)])}>
      {GROUP_LABEL[group] ?? group}
    </span>
  )
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('rounded-2xl bg-white shadow-sm ring-1 ring-slate-200/70', className)}>{children}</div>
}

export function Spinner({ className }: { className?: string }) {
  return <span className={cx('inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent', className)} />
}
