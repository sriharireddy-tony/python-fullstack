import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'

import 'bootstrap/dist/css/bootstrap.min.css'
import '@/styles/app.css'

import { queryClient } from '@/api/queryClient'
import { ThemeProvider } from '@/lib/theme'
import { SessionProvider } from '@/lib/session'
import { router } from '@/routes/router'

const container = document.getElementById('root')
if (!container) throw new Error('Root element #root not found')

// Order matters: ThemeProvider reads the session to hydrate the server-stored
// preference, so it must sit inside SessionProvider.
createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <ThemeProvider>
          <RouterProvider router={router} />
        </ThemeProvider>
      </SessionProvider>
    </QueryClientProvider>
  </StrictMode>,
)
