import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AppShell } from '@/layout/AppShell'
import { DashboardPage } from '@/features/dashboard/DashboardPage'
import { LoginPage } from '@/features/auth/LoginPage'
import { ProfilePage } from '@/features/profile/ProfilePage'
import { ChatPage } from '@/features/intelligence/pages/ChatPage'
import { TicketListPage } from '@/features/tickets/TicketListPage'
import { TicketDetailPage } from '@/features/tickets/TicketDetailPage'
import { CreateTicketPage } from '@/features/tickets/CreateTicketPage'
import { ClientsPage } from '@/features/admin/ClientsPage'
import { TeamsPage } from '@/features/admin/TeamsPage'
import { UsersPage } from '@/features/admin/UsersPage'
import { AuditPage } from '@/features/admin/AuditPage'
import { RequireAuth, RequirePermission } from './guards'
import { Placeholder } from './Placeholder'
import { Permission } from '@/lib/permissions'

/**
 * Route definitions.
 *
 * Guards mirror the navigation permissions, because hiding a nav item does not
 * stop someone typing the URL. Both are UX; the API enforces independently.
 *
 * The saved views (/tickets/mine, /inbox, /awaiting) are the same list screen
 * with a preset applied — one component, four entry points.
 */
export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },

  {
    path: '/',
    element: <RequireAuth />,
    children: [
      {
        element: <AppShell />,
        children: [
          { index: true, element: <DashboardPage /> },
          { path: 'profile', element: <ProfilePage /> },

          {
            path: 'tickets/new',
            element: <RequirePermission permission={Permission.TICKET_CREATE} />,
            children: [{ index: true, element: <CreateTicketPage /> }],
          },
          {
            path: 'tickets',
            element: <RequirePermission permission={Permission.TICKET_READ} />,
            children: [
              { index: true, element: <TicketListPage /> },
              {
                path: 'mine',
                element: (
                  <TicketListPage
                    preset="mine"
                    title="My Tickets"
                    subtitle="Assigned to you and still open."
                  />
                ),
              },
              {
                path: 'inbox',
                element: (
                  <TicketListPage
                    preset="inbox"
                    title="Team Inbox"
                    subtitle="Unassigned tickets waiting for someone to take them."
                  />
                ),
              },
              {
                path: 'awaiting',
                element: (
                  <TicketListPage
                    preset="awaiting"
                    title="Awaiting Closure"
                    subtitle="Resolved or rejected by engineering. Confirm with the client, then close."
                  />
                ),
              },
              { path: ':ticketRef', element: <TicketDetailPage /> },
            ],
          },

          {
            // Only TICKET_READ: the assistant has no write tools, so it cannot
            // do anything a viewer could not do by hand.
            path: 'assistant',
            element: <RequirePermission permission={Permission.TICKET_READ} />,
            children: [{ index: true, element: <ChatPage /> }],
          },

          {
            path: 'clients',
            element: <RequirePermission permission={Permission.CLIENT_MANAGE} />,
            children: [{ index: true, element: <ClientsPage /> }],
          },
          {
            path: 'teams',
            element: <RequirePermission permission={Permission.TEAM_MANAGE} />,
            children: [{ index: true, element: <TeamsPage /> }],
          },
          {
            path: 'users',
            element: <RequirePermission permission={Permission.USER_MANAGE} />,
            children: [{ index: true, element: <UsersPage /> }],
          },
          {
            path: 'audit',
            element: <RequirePermission permission={Permission.AUDIT_READ} />,
            children: [{ index: true, element: <AuditPage /> }],
          },
          {
            path: 'platform/tenants',
            element: <RequirePermission permission={Permission.TENANT_MANAGE} />,
            children: [{ index: true, element: <Placeholder title="Tenants" phase="Phase 7" /> }],
          },

          {
            path: 'forbidden',
            element: (
              <Placeholder
                title="Not permitted"
                phase="Ask an administrator if you think this is wrong"
              />
            ),
          },
          { path: '*', element: <Navigate to="/" replace /> },
        ],
      },
    ],
  },
])
