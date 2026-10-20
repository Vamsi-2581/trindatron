/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/renderer/src/**/*.{js,ts,jsx,tsx}', './src/renderer/index.html'],
  theme: {
    extend: {
      colors: {
        surface: {
          0: '#0d0d14',
          1: '#14141e',
          2: '#1c1c2a',
          3: '#242436',
        },
        border: '#2c2c42',
        accent: {
          DEFAULT: '#7c3aed',
          light: '#a78bfa',
          dim: '#4c1d95',
        },
      },
      keyframes: {
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        fadeIn: {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        blink: 'blink 0.9s step-end infinite',
        fadeIn: 'fadeIn 0.15s ease-out',
      },
    },
  },
  plugins: [],
}
