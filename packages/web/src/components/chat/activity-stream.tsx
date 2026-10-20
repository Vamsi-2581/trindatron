'use client'

import { useEffect, useRef } from 'react'
import { TrinityMark } from '@/components/brand/trinity-mark'
import { t } from '@/i18n'

interface ActivityStreamProps {
  messages: string[]
  isActive: boolean
}

export function ActivityStream({ messages, isActive }: ActivityStreamProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [messages])

  if (!isActive || messages.length === 0) return null

  const lastMessages = messages.slice(-3)

  return (
    <div className="flex gap-3 animate-fadeIn" ref={containerRef}>
      {/* TRINITY avatar */}
      <div className="shrink-0 mt-0.5">
        <div className="w-7 h-7 rounded-lg bg-accent flex items-center justify-center">
          <TrinityMark className="w-3.5 h-3 text-white" />
        </div>
      </div>

      {/* Activity messages */}
      <div className="flex-1 pt-0.5 space-y-1.5">
        <p className="text-xs text-accent-light font-medium">TRINDATRON</p>
        <div className="space-y-1">
          {lastMessages.map((msg, i) => {
            const isLatest = i === lastMessages.length - 1
            return (
              <div
                key={`${msg}-${i}`}
                className={`flex items-center gap-2 text-sm transition-all duration-300 ${
                  isLatest ? 'text-text-secondary animate-slideIn' : 'text-text-muted'
                }`}
              >
                <ActivityDot active={isLatest} index={i} />
                <span>{t(msg)}</span>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function ActivityDot({ active, index }: { active: boolean; index: number }) {
  if (!active) {
    return (
      <span className="w-1.5 h-1.5 rounded-full bg-surface-4 shrink-0" />
    )
  }

  return (
    <span className="relative shrink-0 w-1.5 h-1.5">
      <span
        className="absolute inset-0 rounded-full bg-accent-light animate-scalePulse"
        style={{ animationDelay: `${index * 100}ms` }}
      />
    </span>
  )
}
