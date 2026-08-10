/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        panel: '#151922',
        panelalt: '#1b2029',
        edge: '#262c38',
        ink: '#e7eaf0',
        muted: '#96a0b5',
        accent: '#4c8dff',
        good: '#3ecf8e',
        warn: '#f5a524',
        bad: '#f3496a',
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
};
