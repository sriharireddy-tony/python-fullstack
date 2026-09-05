"""Assembles the v1 API from module routers.

Modules own their routers. This file is the only place that knows the full set,
which keeps the URL surface visible in one place as the application grows.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.meta import router as meta_router
from app.modules.attachments.router import router as attachments_router
from app.modules.clients.router import router as clients_router
from app.modules.comments.router import router as comments_router
from app.modules.identity.router import router as auth_router
from app.modules.identity.users_router import router as users_router
from app.modules.intelligence.chat_router import router as chat_router
from app.modules.intelligence.router import router as intelligence_router
from app.modules.teams.router import router as teams_router
from app.modules.tickets.router import router as tickets_router

api_router = APIRouter()

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(teams_router, prefix="/teams", tags=["teams"])
api_router.include_router(clients_router, prefix="/clients", tags=["clients"])
api_router.include_router(tickets_router, prefix="/tickets", tags=["tickets"])

# The AI layer owns URLs under /tickets without the tickets module knowing it
# exists -- the dependency points intelligence -> tickets, never back.
api_router.include_router(intelligence_router, prefix="/tickets", tags=["intelligence"])

# The assistant is not scoped to a ticket, so it gets its own prefix.
api_router.include_router(chat_router, prefix="/chat", tags=["chat"])

# Comments and attachments declare full paths of their own, because they hang
# off both /tickets/{id}/... and /comments/{id}/... and a single prefix cannot
# express that.
api_router.include_router(comments_router, tags=["comments"])
api_router.include_router(attachments_router, tags=["attachments"])

api_router.include_router(meta_router, tags=["meta"])
