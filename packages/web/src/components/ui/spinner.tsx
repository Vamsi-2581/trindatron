import type { SVGProps } from 'react'

interface SpinnerProps extends SVGProps<SVGSVGElement> {
  size?: 'xs' | 'sm' | 'md' | 'lg'
}

const sizes = { xs: 'w-3 h-3', sm: 'w-4 h-4', md: 'w-5 h-5', lg: 'w-6 h-6' }

export function Spinner({ size = 'sm', className = '', ...props }: SpinnerProps) {
  return (
    <svg
      className={`animate-spin text-current ${sizes[size]} ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      {...props}
    >
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2.5" opacity="0.2" />
      <path
        d="M12 2a10 10 0 0 1 10 10"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  )
}
