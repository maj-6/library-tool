import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { FocusStyleManager } from '@blueprintjs/core'
import '@blueprintjs/core/lib/css/blueprint.css'
import '@blueprintjs/icons/lib/css/blueprint-icons.css'
import './styles.css'
import App from './App'

FocusStyleManager.onlyShowFocusOnTabs()

const root = document.getElementById('root')
if (!root) throw new Error('#root missing from index.html')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
