from datetime import datetime

from sqlmodel import Field, SQLModel


class TenantRoleGrant(SQLModel, table=True):
    # `subject` is the token identifier: username/email for a user, group path
    # for a group. Owned by keep-api-gateway; workflows only reads this table.
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True)
    subject: str = Field(primary_key=True)
    subject_type: str  # 'user' | 'group'
    role: str  # 'admin' | 'editor' | 'viewer'
    created_at: datetime = Field(default_factory=datetime.utcnow)
