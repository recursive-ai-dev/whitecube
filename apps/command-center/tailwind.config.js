/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        'whitecube-accent': '#ff3b3b',
        'whitecube-warm-grey': '#AEA79F',
        'whitecube-cool-grey': '#1b2230',
        'whitecube-background': '#04070d',
        'whitecube-dark': '#06080d',
        'terminal-bg': 'rgba(4, 7, 13, 0.72)',
        'glass-bg': 'rgba(4, 7, 13, 0.85)',
      },
      fontFamily: {
        sans: ['Ubuntu', 'Cantarell', 'system-ui', 'sans-serif'],
        mono: ['Ubuntu Mono', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
};
