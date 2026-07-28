import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { MantineProvider } from '@mantine/core'
import '@mantine/core/styles.css'

import App from './App'
import { theme } from './theme'
import './global.css'

const host = document.getElementById('root')
if (!host) throw new Error('#root missing from index.html')

createRoot(host).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="dark" forceColorScheme="dark">
      <App />
    </MantineProvider>
  </StrictMode>,
)
