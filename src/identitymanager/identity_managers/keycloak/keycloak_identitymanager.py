from src.common.models.user import User
from src.contextmanager.contextmanager import ContextManager
from src.identitymanager.authverifierbase import AuthVerifierBase
from src.identitymanager.identity_managers.keycloak.keycloak_authverifier import (
    KeycloakAuthVerifier,
)
from src.identitymanager.identitymanager import BaseIdentityManager


class KeycloakIdentityManager(BaseIdentityManager):
    """Minimal manager -- auth is handled by KeycloakAuthVerifier; on_start is a no-op."""

    def __init__(self, tenant_id, context_manager: ContextManager, **kwargs):
        super().__init__(tenant_id, context_manager, **kwargs)
        self.logger.info("Keycloak Identity Manager initialized")

    def on_start(self, app) -> None:
        # No Keycloak signin endpoint / UMA resource registration in workflows.
        pass

    def get_users(self) -> list[User]:
        return []

    def get_auth_verifier(self, scopes) -> AuthVerifierBase:
        return KeycloakAuthVerifier(scopes)
