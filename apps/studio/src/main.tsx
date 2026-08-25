import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
// Inter, self-hosted and subsetted. The stylesheet asked for it and nothing loaded it, so
// every weight in the design system silently fell back to the platform UI font -- 540, 560,
// 580, 620, 660 and 680 all rendering as whatever Segoe UI or San Francisco has, which is
// two weights. Self-hosted rather than from a CDN because this is a tool you can run
// offline, and a font that only arrives with an internet connection is not that.
import '@fontsource-variable/inter'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
