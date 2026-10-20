import type { ReactNode } from 'react'

interface CardProps {
  children: ReactNode
  className?: string
  hover?: boolean
  onClick?: () => void
}

export function Card({ children, className = '', hover = false, onClick }: CardProps) {
  return (
    <div
      onClick={onClick}
      className={[
        'bg-surface-1 border border-border rounded-xl',
        hover && 'hover:border-border/80 hover:bg-surface-2 cursor-pointer transition-all duration-150',
        onClick && 'cursor-pointer',
        className,
      ].filter(Boolean).join(' ')}
    >
      {children}
    </div>
  )
}

interface CardSectionProps {
  children: ReactNode
  className?: string
}

Card.Header  = function CardHeader({ children, className = '' }: CardSectionProps) {
  return <div className={`px-5 py-4 border-b border-border ${className}`}>{children}</div>
}

Card.Body    = function CardBody({ children, className = '' }: CardSectionProps) {
  return <div className={`px-5 py-4 ${className}`}>{children}</div>
}

Card.Footer  = function CardFooter({ children, className = '' }: CardSectionProps) {
  return <div className={`px-5 py-3 border-t border-border ${className}`}>{children}</div>
}
