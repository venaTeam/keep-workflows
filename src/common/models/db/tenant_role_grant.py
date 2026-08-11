from datetime import datetime

from sqlmodel import Field, SQLModel


class TenantRoleGrant(SQLModel, table=True):
    # Per-tenant role grant. `subject` is the identifier carried by the Keycloak
    # token -- the username/email for a user, or the group path for a group -- so
    # the request-time membership check is a direct string match with no id
    # resolution. `subject_type` disambiguates the two. Owned (schema/migration)
    # by keep-api-gateway; workflows only READS this shared table. See
    # VENA-5596 §3 / §5.
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True)
    subject: str = Field(primary_key=True)
    subject_type: str  # 'user' | 'group'
    role: str  # 'admin' | 'editor' | 'viewer'
    created_at: datetime = Field(default_factory=datetime.utcnow)
