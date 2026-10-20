import { createContext } from 'react'

/**
 * Asks TRINDATRON to go on with an answer that stopped at the plan's output limit.
 *
 * The chat screen provides it (it owns sending); a message row only knows it can offer it. `null` while a reply is
 * already in progress, so "Continuar" is never offered twice.
 */
export const ContinueAnswerContext = createContext<(() => void) | null>(null)
