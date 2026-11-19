import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// The GitHub Pages demo has no server: this swaps in an in-page one before
// the app makes its first request. Dead-code-eliminated from normal builds.
if (import.meta.env.VITE_DEMO) {
  const { installDemoServer } = await import('./demo/demoServer')
  await installDemoServer()
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
