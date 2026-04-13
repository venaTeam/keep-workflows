import enum

from src.common.core.config import config
from src.contextmanager.contextmanager import ContextManager
from src.secretmanager.secretmanager import BaseSecretManager


class SecretManagerTypes(enum.Enum):
    FILE = "file"
    GCP = "gcp"
    K8S = "k8s"
    VAULT = "vault"
    AWS = "aws"
    DB = "db"


class SecretManagerFactory:
    @staticmethod
    def get_secret_manager(
        context_manager: ContextManager,
        secret_manager_type: SecretManagerTypes = None,
        **kwargs,
    ) -> BaseSecretManager:
        if not secret_manager_type:
            secret_manager_type = SecretManagerTypes[
                config("SECRET_MANAGER_TYPE", default="FILE").upper()
            ]
        if secret_manager_type == SecretManagerTypes.FILE:
            from src.secretmanager.filesecretmanager import FileSecretManager

            return FileSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.GCP:
            from src.secretmanager.gcpsecretmanager import GcpSecretManager

            return GcpSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.K8S:
            from src.secretmanager.kubernetessecretmanager import (
                KubernetesSecretManager,
            )

            return KubernetesSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.VAULT:
            from src.secretmanager.vaultsecretmanager import VaultSecretManager

            return VaultSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.AWS:
            from src.secretmanager.awssecretmanager import AwsSecretManager

            return AwsSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.DB:
            from src.secretmanager.dbsecretmanager import DbSecretManager

            return DbSecretManager(context_manager, **kwargs)

        raise NotImplementedError(
            f"Secret manager type {str(secret_manager_type)} not implemented"
        )
