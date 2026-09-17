# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Credential helpers shared by the deployment scripts.

Four authentication modes are supported. All of them return an
``azure.core.credentials.TokenCredential`` that fabric-cicd and the REST
helpers can use to request tokens for ``https://api.fabric.microsoft.com``.

cli
    Reuse the login performed by the ``AzureCLI@2`` task (or ``az login`` on
    a developer machine). This is the recommended mode in Azure Pipelines
    because it works with workload identity federation, client secret and
    managed identity service connections without any script changes.

pipelines
    Use ``AzurePipelinesCredential`` directly. Requires ``SYSTEM_ACCESSTOKEN``
    plus the ``AZURESUBSCRIPTION_*`` variables that ``AzureCLI@2`` exports
    when the service connection uses workload identity federation.

spn
    Client secret flow. Reads ``FABRIC_TENANT_ID``, ``FABRIC_CLIENT_ID`` and
    ``FABRIC_CLIENT_SECRET`` from the environment (map them from a Key Vault
    backed variable group). Kept for tenants that cannot use federation yet.

default
    ``DefaultAzureCredential`` for local development.
"""

from __future__ import annotations

import os
from typing import Optional

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_API_ROOT = "https://api.fabric.microsoft.com/v1"

AUTH_MODES = ("cli", "pipelines", "spn", "default")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise EnvironmentError(
            f"Environment variable {name} is required for the selected authentication mode."
        )
    return value


def get_credential(mode: str = "cli"):
    """Return a TokenCredential for the requested mode."""
    mode = (mode or "cli").lower()
    if mode not in AUTH_MODES:
        raise ValueError(f"Unknown auth mode '{mode}'. Expected one of {', '.join(AUTH_MODES)}.")

    if mode == "cli":
        from azure.identity import AzureCliCredential

        return AzureCliCredential()

    if mode == "pipelines":
        from azure.identity import AzurePipelinesCredential

        return AzurePipelinesCredential(
            tenant_id=_require_env("AZURESUBSCRIPTION_TENANT_ID"),
            client_id=_require_env("AZURESUBSCRIPTION_CLIENT_ID"),
            service_connection_id=_require_env("AZURESUBSCRIPTION_SERVICE_CONNECTION_ID"),
            system_access_token=_require_env("SYSTEM_ACCESSTOKEN"),
        )

    if mode == "spn":
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(
            tenant_id=_require_env("FABRIC_TENANT_ID"),
            client_id=_require_env("FABRIC_CLIENT_ID"),
            client_secret=_require_env("FABRIC_CLIENT_SECRET"),
        )

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def get_fabric_token(credential, scope: Optional[str] = None) -> str:
    """Request a bearer token for the Fabric REST API."""
    return credential.get_token(scope or FABRIC_SCOPE).token


def auth_headers(credential) -> dict:
    return {
        "Authorization": f"Bearer {get_fabric_token(credential)}",
        "Content-Type": "application/json",
    }
