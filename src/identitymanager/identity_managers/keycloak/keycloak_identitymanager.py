import json
import os

from fastapi import HTTPException
from keycloak.exceptions import KeycloakDeleteError, KeycloakGetError, KeycloakPostError
from keycloak.openid_connection import KeycloakOpenIDConnection

from src.common.core.config import config
from src.common.models.user import Group, ResourcePermission, User
from src.contextmanager.contextmanager import ContextManager
from src.identitymanager.authenticatedentity import AuthenticatedEntity
from src.identitymanager.authverifierbase import AuthVerifierBase
from src.identitymanager.identity_managers.keycloak.keycloak_authverifier import (
    KeycloakAuthVerifier,
)
from src.identitymanager.identitymanager import BaseIdentityManager
from keycloak import KeycloakAdmin

# Some good sources on this topic:
# 1. https://stackoverflow.com/questions/42186537/resources-scopes-permissions-and-policies-in-keycloak
# 2. MUST READ - https://www.keycloak.org/docs/24.0.4/authorization_services/
# 3. ADMIN REST API - https://www.keycloak.org/docs-api/22.0.1/rest-api/index.html
# 4. (TODO) PROTECTION API - https://www.keycloak.org/docs/latest/authorization_services/index.html#_service_protection_api


class KeycloakIdentityManager(BaseIdentityManager):
    """Keycloak identity manager for keep-workflows.

    keep-workflows is a workflow-automation microservice, not the primary
    user-management backend (that is keep-api-gateway). This implementation
    therefore mirrors the gateway's authentication/user-management surface but
    intentionally omits the heavy UMA / Keycloak-authorization provisioning
    (scopes, resources, roles, policies, permissions) that the gateway performs
    on start. Authorization is handled by KeycloakAuthVerifier (which currently
    short-circuits `_authorize` to allow access) and the multi-org
    `/auth/user/orgs` endpoint that the UI TenantSwitcher relies on.
    """

    RESOURCES = {}

    def __init__(self, tenant_id, context_manager: ContextManager, **kwargs):
        super().__init__(tenant_id, context_manager, **kwargs)
        self.server_url = os.environ.get("KEYCLOAK_URL")
        self.keycloak_verify_cert = (
            os.environ.get("KEYCLOAK_VERIFY_CERT", "true").lower() == "true"
        )
        try:
            self.keycloak_admin = KeycloakAdmin(
                server_url=os.environ["KEYCLOAK_URL"] + "/admin",
                username=os.environ.get("KEYCLOAK_ADMIN_USER"),
                password=os.environ.get("KEYCLOAK_ADMIN_PASSWORD"),
                realm_name=os.environ["KEYCLOAK_REALM"],
                verify=self.keycloak_verify_cert,
            )
            self.client_id = self.keycloak_admin.get_client_id(
                os.environ["KEYCLOAK_CLIENT_ID"]
            )
            self.keycloak_id_connection = KeycloakOpenIDConnection(
                server_url=os.environ["KEYCLOAK_URL"],
                client_id=os.environ["KEYCLOAK_CLIENT_ID"],
                realm_name=os.environ["KEYCLOAK_REALM"],
                client_secret_key=os.environ["KEYCLOAK_CLIENT_SECRET"],
                verify=self.keycloak_verify_cert,
            )

            self.admin_url = f"{os.environ['KEYCLOAK_URL']}/admin/realms/{os.environ['KEYCLOAK_REALM']}/clients/{self.client_id}"
            self.admin_url_without_client = f"{os.environ['KEYCLOAK_URL']}/admin/realms/{os.environ['KEYCLOAK_REALM']}"
            self.realm = os.environ["KEYCLOAK_REALM"]
            # if Keep controls the Keycloak server so it have event listener
            # for future use
            self.keep_controlled_keycloak = (
                os.environ.get("KEYCLOAK_KEEP_CONTROLLED", "false") == "true"
            )
            # Does ABAC is enabled
            self.abac_enabled = (
                os.environ.get("KEYCLOAK_ABAC_ENABLED", "true") == "true"
            )

            self.keycloak_multi_org = config(
                "KEYCLOAK_ROLES_FROM_GROUPS", default=False, cast=bool
            )

        except Exception as e:
            self.logger.error(
                "Failed to initialize Keycloak Identity Manager: %s", str(e)
            )
            raise
        self.logger.info("Keycloak Identity Manager initialized")

    def on_start(self, app) -> None:
        # if the on start process is disabled:
        if os.environ.get("SKIP_KEYCLOAK_ONSTART", "false") == "true":
            self.logger.info("Skipping keycloak on start")
            return

        # keep-workflows deviation from keep-api-gateway:
        # The gateway provisions Keycloak scopes/resources/roles for every route
        # (heavy UMA/authorization setup). keep-workflows is a workflow engine and
        # does not own that provisioning, so we skip it. We only register the
        # multi-org `/auth/user/orgs` endpoint used by the UI TenantSwitcher.
        if self.keycloak_multi_org:
            current_routes = [route.path for route in app.routes]
            if "/auth/user/orgs" not in current_routes:
                self.logger.info("Adding /auth/user/orgs endpoint")

                from fastapi import Depends

                from src.identitymanager.identitymanagerfactory import (
                    IdentityManagerFactory,
                )

                @app.get("/auth/user/orgs")
                def tenant(
                    authenticated_entity: AuthenticatedEntity = Depends(
                        IdentityManagerFactory.get_auth_verifier([])
                    ),
                ):
                    tenants = authenticated_entity.user_orgs
                    return tenants

    @property
    def support_sso(self) -> bool:
        return True

    def get_sso_providers(self) -> list[str]:
        return []

    def get_sso_wizard_url(self, authenticated_entity: AuthenticatedEntity) -> str:
        tenant_realm = authenticated_entity.org_realm
        org_id = authenticated_entity.org_id
        return f"{self.server_url}realms/{tenant_realm}/wizard/?org_id={org_id}/#iss={self.server_url}/realms/{tenant_realm}"

    def get_auth_verifier(self, scopes: list) -> AuthVerifierBase:
        return KeycloakAuthVerifier(scopes)

    def get_users(self) -> list[User]:
        try:
            # TODO: query only users that Keep created (so not show all LDAP users)
            users = self.keycloak_admin.get_users({})
            users = [user for user in users if "firstName" in user]

            users_dto = []
            for user in users:
                # todo: should be more efficient
                groups = self.keycloak_admin.get_user_groups(user["id"])
                groups = [
                    {
                        "id": group["id"],
                        "name": group["name"],
                    }
                    for group in groups
                ]
                role = self.get_user_current_role(user_id=user.get("id"))
                user_dto = User(
                    email=user.get("email", ""),
                    name=user.get("firstName", ""),
                    role=role,
                    created_at=user.get("createdTimestamp", ""),
                    ldap=(
                        True
                        if user.get("attributes", {}).get("LDAP_ID", False)
                        else False
                    ),
                    last_login=user.get("attributes", {}).get("last-login", [""])[0],
                    groups=groups,
                )
                users_dto.append(user_dto)
            return users_dto
        except KeycloakGetError as e:
            self.logger.error("Failed to fetch users from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to fetch users")

    def create_user(
        self,
        user_email: str,
        user_name: str,
        password: str,
        role: list[str],
        groups: list[str],
    ) -> dict:
        try:
            user_data = {
                "username": user_email,
                "email": user_email,
                "enabled": True,
                "firstName": user_name,
                "lastName": user_name,
                "emailVerified": True,
            }
            if password:
                user_data["credentials"] = [
                    {"type": "password", "value": password, "temporary": False}
                ]

            user_id = self.keycloak_admin.create_user(user_data)
            if role:
                role_id = self.keycloak_admin.get_client_role_id(self.client_id, role)
                self.keycloak_admin.assign_client_role(
                    client_id=self.client_id,
                    user_id=user_id,
                    roles=[{"id": role_id, "name": role}],
                )
            for group in groups:
                self.add_user_to_group(user_id=user_id, group=group)

            return {
                "status": "success",
                "message": "User created successfully",
                "user_id": user_id,
            }
        except KeycloakPostError as e:
            if "User exists" in str(e):
                self.logger.error(
                    "Failed to create user - user %s already exists", user_email
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"Failed to create user - user {user_email} already exists",
                )
            self.logger.error("Failed to create user in Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to create user")

    def get_user_id_by_email(self, user_email: str) -> str:
        user_id = self.keycloak_admin.get_users(query={"email": user_email})
        if not user_id:
            self.logger.error("User does not exists")
            raise HTTPException(status_code=404, detail="User does not exists")
        elif len(user_id) > 1:
            self.logger.error("Multiple users found")
            raise HTTPException(
                status_code=500, detail="Multiple users found, please contact admin"
            )
        user_id = user_id[0]["id"]
        return user_id

    def get_user_current_role(self, user_id: str) -> str:
        current_role = (
            self.keycloak_admin.connection.raw_get(
                self.admin_url_without_client + f"/users/{user_id}/role-mappings"
            )
            .json()
            .get("clientMappings", {})
            .get(self.realm, {})
            .get("mappings")
        )

        if current_role:
            # remove uma protection
            current_role = [
                role for role in current_role if role["name"] != "uma_protection"
            ]
            # if uma_protection is the only role, then the user has no role
            if current_role:
                return current_role[0]["name"]
            else:
                return None
        else:
            return None

    def add_user_to_group(self, user_id: str, group: str):
        resp = self.keycloak_admin.connection.raw_put(
            f"{self.admin_url_without_client}/users/{user_id}/groups/{group}",
            data=json.dumps({}),
        )
        resp.raise_for_status()

    def update_user(self, user_email: str, update_data: dict) -> dict:
        try:
            user_id = self.get_user_id_by_email(user_email)
            if "role" in update_data and update_data["role"]:
                role = update_data["role"]
                # get current role and understand if needs to be updated:
                current_role = self.get_user_current_role(user_id)
                # update the role only if its different than current
                # TODO: more than one role
                if current_role != role:
                    role_id = self.keycloak_admin.get_client_role_id(
                        self.client_id, role
                    )
                    if not role_id:
                        self.logger.error("Role does not exists")
                        raise HTTPException(
                            status_code=404, detail="Role does not exists"
                        )
                    self.keycloak_admin.assign_client_role(
                        client_id=self.client_id,
                        user_id=user_id,
                        roles=[{"id": role_id, "name": role}],
                    )
            if "groups" in update_data and update_data["groups"]:
                # get the current groups
                groups = self.keycloak_admin.get_user_groups(user_id)
                groups_ids = [g.get("id") for g in groups]
                # calc with groups needs to be removed and which to be added
                groups_to_remove = [
                    group_id
                    for group_id in groups_ids
                    if group_id not in update_data["groups"]
                ]

                groups_to_add = [
                    group for group in update_data["groups"] if group not in groups_ids
                ]
                # remove
                for group in groups_to_remove:
                    self.logger.info("Leaving group")
                    resp = self.keycloak_admin.connection.raw_delete(
                        f"{self.admin_url_without_client}/users/{user_id}/groups/{group}"
                    )
                    resp.raise_for_status()
                    self.logger.info("Left group")
                # add
                for group in groups_to_add:
                    self.logger.info("Joining group")
                    self.add_user_to_group(user_id=user_id, group=group)
                    self.logger.info("Joined group")
            return {"status": "success", "message": "User updated successfully"}
        except KeycloakPostError as e:
            self.logger.error("Failed to update user in Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to update user")

    def delete_user(self, user_email: str) -> dict:
        try:
            user_id = self.get_user_id_by_email(user_email)
            self.keycloak_admin.delete_user(user_id)
            # delete the policy for the user (if not implicitly deleted?)
            return {"status": "success", "message": "User deleted successfully"}
        except KeycloakDeleteError as e:
            self.logger.error("Failed to delete user from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to delete user")

    def create_resource(
        self,
        resource_name: str,
        scopes: list[str] = [],
        resource_type="keep_generic",
        attributes={},
    ) -> None:
        resource = {
            "name": resource_name,
            "displayName": f"Resource for {resource_name}",
            "type": "urn:keep:resources:" + resource_type,
            "scopes": [{"name": scope} for scope in scopes],
            "attributes": attributes,
        }
        try:
            self.keycloak_admin.create_client_authz_resource(self.client_id, resource)
        except KeycloakPostError as e:
            if "already exists" in str(e):
                self.logger.info("Resource already exists in Keycloak")
                pass
            else:
                self.logger.error("Failed to create resource in Keycloak: %s", str(e))
                raise HTTPException(status_code=500, detail="Failed to create resource")

    def delete_resource(self, resource_id: str) -> None:
        try:
            resources = self.keycloak_admin.get_client_authz_resources(
                os.environ["KEYCLOAK_CLIENT_ID"]
            )
            for resource in resources:
                if resource["uris"] == ["/resource/" + resource_id]:
                    self.keycloak_admin.delete_client_authz_resource(
                        os.environ["KEYCLOAK_CLIENT_ID"], resource["id"]
                    )
        except KeycloakDeleteError as e:
            self.logger.error("Failed to delete resource from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to delete resource")

    def get_groups(self) -> list[Group]:
        try:
            groups = self.keycloak_admin.get_groups(
                query={"briefRepresentation": False}
            )
            result = []
            for group in groups:
                group_id = group["id"]
                group_name = group["name"]
                roles = group.get("clientRoles", {}).get("keep", [])

                # Fetch members for each group
                members = self.keycloak_admin.get_group_members(group_id)
                member_names = [member.get("email", "") for member in members]
                member_count = len(members)

                result.append(
                    Group(
                        id=group_id,
                        name=group_name,
                        roles=roles,
                        memberCount=member_count,
                        members=member_names,
                    )
                )
            return result
        except KeycloakGetError as e:
            self.logger.error("Failed to fetch groups from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to fetch groups")

    def get_permissions(self) -> list[ResourcePermission]:
        return []

    def get_user_permission_on_resource_type(
        self, resource_type: str, authenticated_entity: AuthenticatedEntity
    ) -> list[ResourcePermission]:
        # TODO: implement
        return []
