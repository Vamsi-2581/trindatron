import type { ReactNode } from 'react'

type Variant = 'default' | 'accent' | 'success' | 'warning' | 'error' | 'muted'

interface BadgeProps {
  children: ReactNode
  variant?: Variant
  className?: string
}

const variants: Record<Variant, string> = {
  default:  'bg-surface-3 text-text-secondary border border-border',
  accent:   'bg-accent/10 text-accent-light border border-accent/20',
  success:  'bg-success/10 text-success border border-success/20',
  warning:  'bg-warning/10 text-warning border border-warning/20',
  error:    'bg-error/10 text-error border border-error/20',
  muted:    'bg-surface-2 text-text-muted border border-border-subtle',
}

export function Badge({ children, variant = 'default', className = '' }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${variants[variant]} ${className}`}
    >
      {children}
    </span>
  )
}
