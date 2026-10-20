import type { Config } from 'tailwindcss'

const config: Config = {
  content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'],
  theme: {
    extend: {
      // 360-400px phones are the narrowest real traffic; `xs` lets display
      // type step up just above that without waiting for `sm` (640px).
      screens: { xs: '400px' },
      fontFamily: {
        sans: ['var(--font-inter)', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['var(--font-mono)', 'ui-monospace', 'Cascadia Code', 'Fira Code', 'monospace'],
      },
      colors: {
        /* Backgrounds — channel format so `bg-bg/80` and friends work.
           See the note beside --accent-ch in globals.css: a plain
           `var(--x)` here makes every opacity modifier silently vanish. */
        bg: 'rgb(var(--bg-ch) / <alpha-value>)',
        paper: 'rgb(var(--paper-ch) / <alpha-value>)',
        surface: {
          0: 'rgb(var(--bg-ch) / <alpha-value>)',       // legacy alias
          1: 'rgb(var(--surface-1-ch) / <alpha-value>)',
          2: 'rgb(var(--surface-2-ch) / <alpha-value>)',
          3: 'rgb(var(--surface-3-ch) / <alpha-value>)',
          4: 'rgb(var(--surface-4-ch) / <alpha-value>)',
        },
        /* Borders */
        border: {
          DEFAULT: 'rgb(var(--border-ch) / <alpha-value>)',
          subtle:  'rgb(var(--border-subtle-ch) / <alpha-value>)',
        },
        /* Accent — DEFAULT and light use channel format for opacity modifiers */
        accent: {
          DEFAULT: 'rgb(var(--accent-ch) / <alpha-value>)',
          hover:   'var(--accent-hover)',
          muted:   'var(--accent-muted)',
          light:   'rgb(var(--accent-light-ch) / <alpha-value>)',
          glow:    'var(--accent-glow)',
          dim:     'var(--accent-dim)',
        },
        /* Text */
        text: {
          primary:   'var(--text-primary)',
          secondary: 'var(--text-secondary)',
          muted:     'var(--text-muted)',
          inverse:   'var(--text-inverse)',
        },
        /* Semantic status — channel format for opacity modifiers */
        success: 'rgb(var(--success-ch) / <alpha-value>)',
        warning: 'rgb(var(--warning-ch) / <alpha-value>)',
        error:   'rgb(var(--error-ch) / <alpha-value>)',
        info:    'var(--color-info)',
      },
      borderRadius: {
        sm:   'var(--r-sm)',
        md:   'var(--r-md)',
        lg:   'var(--r-lg)',
        xl:   'var(--r-xl)',
        full: 'var(--r-full)',
      },
      boxShadow: {
        sm:     'var(--shadow-sm)',
        md:     'var(--shadow-md)',
        lg:     'var(--shadow-lg)',
        accent: 'var(--shadow-accent)',
      },
      keyframes: {
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        fadeIn: {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
        fadeUp: {
          from: { opacity: '0', transform: 'translateY(12px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
        slideIn: {
          from: { opacity: '0', transform: 'translateX(-6px)' },
          to:   { opacity: '1', transform: 'translateX(0)' },
        },
        pulseOpacity: {
          '0%, 100%': { opacity: '0.4' },
          '50%': { opacity: '1' },
        },
        shimmer: {
          '0%':   { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        spin: {
          from: { transform: 'rotate(0deg)' },
          to:   { transform: 'rotate(360deg)' },
        },
        scalePulse: {
          '0%, 100%': { transform: 'scale(1)', opacity: '0.6' },
          '50%': { transform: 'scale(1.4)', opacity: '1' },
        },
      },
      animation: {
        blink:        'blink 0.9s step-end infinite',
        fadeIn:       'fadeIn 0.2s ease-out',
        fadeUp:       'fadeUp 0.3s ease-out',
        slideIn:      'slideIn 0.2s ease-out',
        pulseOpacity: 'pulseOpacity 1.5s ease-in-out infinite',
        shimmer:      'shimmer 2s linear infinite',
        spin:         'spin 0.8s linear infinite',
        scalePulse:   'scalePulse 1.2s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}

export default config
