"""Roles and permissions.

Permissions are the unit of checking; roles are named bundles of them. Every
authorization check tests a *permission*, never a role name -- so adding a role
later means defining a new bundle rather than editing every endpoint.

This map lives in code for v1: version-controlled, testable, and no query per
request. Move it into the database the day a tenant needs custom roles; that is
a contained change precisely because nothing checks role names.

Mirrored on the frontend in src/lib/permissions.ts for UX purposes only. The
frontend copy hides buttons; this one decides.

See docs/02-roles-and-permissions.md.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    TENANT_MANAGE = "tenant:manage"
    USER_MANAGE = "user:manage"
    TEAM_MANAGE = "team:manage"
    CLIENT_MANAGE = "client:manage"

    TICKET_CREATE = "ticket:create"
    TICKET_READ = "ticket:read"
    TICKET_UPDATE = "ticket:update"
    TICKET_ASSIGN = "ticket:assign"
    TICKET_TRANSITION = "ticket:transition"
    TICKET_RESOLVE = "ticket:resolve"
    TICKET_CLOSE = "ticket:close"
    TICKET_REOPEN = "ticket:reopen"
    TICKET_REJECT = "ticket:reject"
    TICKET_TRANSFER_TEAM = "ticket:transfer_team"
    TICKET_OVERRIDE_PRIORITY = "ticket:override_priority"

    COMMENT_CREATE = "comment:create"
    ATTACHMENT_UPLOAD = "attachment:upload"
    AUDIT_READ = "audit:read"


class Role(StrEnum):
    TENANT_ADMIN = "tenant_admin"
    CS_LEAD = "cs_lead"
    CS_AGENT = "cs_agent"
    TEAM_MANAGER = "team_manager"
    DEVELOPER = "developer"


# Platform super admins are deliberately NOT in Role: they live in a separate
# table with no tenant_id, so there is no code path that could turn a normal
# user into a cross-tenant one. See docs/02-roles-and-permissions.md.
PLATFORM_ADMIN_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.TENANT_MANAGE, Permission.AUDIT_READ}
)


_CS_SHARED = {
    Permission.TICKET_CREATE,
    Permission.TICKET_READ,
    Permission.TICKET_UPDATE,
    Permission.TICKET_CLOSE,
    Permission.TICKET_REOPEN,
    Permission.COMMENT_CREATE,
    Permission.ATTACHMENT_UPLOAD,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.TENANT_ADMIN: frozenset(set(Permission) - {Permission.TENANT_MANAGE}),
    Role.CS_LEAD: frozenset(
        _CS_SHARED
        | {
            Permission.CLIENT_MANAGE,
            Permission.TICKET_ASSIGN,
            Permission.TICKET_TRANSITION,
            Permission.TICKET_REJECT,
            Permission.TICKET_TRANSFER_TEAM,
            Permission.TICKET_OVERRIDE_PRIORITY,
        }
    ),
    Role.CS_AGENT: frozenset(_CS_SHARED),
    Role.TEAM_MANAGER: frozenset(
        {
            Permission.TICKET_CREATE,
            Permission.TICKET_READ,
            Permission.TICKET_UPDATE,
            # Assigning is unrestricted for a manager; a developer holds the
            # same permission but may only assign to themselves. That narrowing
            # is a resource policy (layer 3), not expressible here.
            Permission.TICKET_ASSIGN,
            Permission.TICKET_TRANSITION,
            Permission.TICKET_RESOLVE,
            Permission.TICKET_REOPEN,
            Permission.TICKET_REJECT,
            Permission.TICKET_TRANSFER_TEAM,
            Permission.COMMENT_CREATE,
            Permission.ATTACHMENT_UPLOAD,
            # Scoped to their own team by resource policy.
            Permission.AUDIT_READ,
        }
    ),
    Role.DEVELOPER: frozenset(
        {
            Permission.TICKET_READ,
            Permission.TICKET_UPDATE,
            Permission.TICKET_ASSIGN,
            Permission.TICKET_TRANSITION,
            Permission.TICKET_RESOLVE,
            Permission.TICKET_REJECT,
            Permission.COMMENT_CREATE,
            Permission.ATTACHMENT_UPLOAD,
        }
    ),
}


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in permissions_for(role)


# Developers deliberately cannot raise tickets -- only CS does. Asserted at
# import so a careless edit to the map above fails fast rather than quietly
# widening access.
assert Permission.TICKET_CREATE not in ROLE_PERMISSIONS[Role.DEVELOPER]
assert Permission.TICKET_CLOSE not in ROLE_PERMISSIONS[Role.DEVELOPER]
assert Permission.TICKET_CLOSE not in ROLE_PERMISSIONS[Role.TEAM_MANAGER]
assert Permission.TENANT_MANAGE not in set().union(*ROLE_PERMISSIONS.values())
