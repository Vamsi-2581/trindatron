import { gsap } from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'

if (typeof window !== 'undefined') gsap.registerPlugin(ScrollTrigger)

/** Wide screens with motion allowed get scrubbed, scroll-linked chapters (never pinned); everyone else reads them in flow. */
export const CINEMA = '(min-width: 900px) and (min-height: 560px) and (prefers-reduced-motion: no-preference)'
export const FLOW = '(max-width: 899px), (max-height: 559px), (prefers-reduced-motion: reduce)'

export { gsap, ScrollTrigger }
