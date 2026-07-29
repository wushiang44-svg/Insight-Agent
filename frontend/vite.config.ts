import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
// Single config file for both Vite (build/dev) and Vitest (test) -- `test` is
// vitest/config's addition on top of Vite's own defineConfig, not a separate
// tool. Kept minimal: jsdom environment, no globals (this codebase's existing
// style already prefers explicit imports everywhere, e.g. verbatimModuleSyntax
// in tsconfig), one setup file for React Testing Library's automatic cleanup.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
  },
})
