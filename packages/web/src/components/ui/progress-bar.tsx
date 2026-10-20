interface ProgressBarProps {
  value: number      // 0–100
  label?: string
  caption?: string
  className?: string
}

export function ProgressBar({ value, label, caption, className = '' }: ProgressBarProps) {
  const clamped = Math.min(100, Math.max(0, value))
  const pct = `${clamped}%`

  return (
    <div className={`space-y-1.5 ${className}`}>
      {(label || caption) && (
        <div className="flex items-center justify-between">
          {label && <span className="text-xs text-text-secondary">{label}</span>}
          {caption && <span className="text-xs text-text-muted">{caption}</span>}
        </div>
      )}
      <div className="h-1.5 bg-surface-3 rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500 ease-out"
          style={{
            width: pct,
            background: clamped > 80
              ? 'var(--color-warning)'
              : clamped > 60
              ? 'var(--accent-light)'
              : 'var(--accent)',
          }}
        />
      </div>
    </div>
  )
}
