# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Thin helpers over the Fabric REST API used by the deployment scripts.

Only the handful of calls the pipeline needs are implemented here; fabric-cicd
handles item publishing. Long running operations are polled the way the
Fabric API documents them (202 + Location + Retry-After).
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

import requests

from fabric_auth import FABRIC_API_ROOT, auth_headers


class FabricApiError(RuntimeError):
    pass


def _raise_for_status(response: requests.Response, context: str) -> None:
    if response.ok:
        return
    detail = response.text
    try:
        body = response.json()
        detail = f"{body.get('errorCode', '')} {body.get('message', '')}".strip() or detail
    except ValueError:
        pass
    raise FabricApiError(f"{context} failed with HTTP {response.status_code}: {detail}")


def paged_get(credential, path: str, timeout: int = 60) -> Iterator[dict]:
    """Yield every element of a paged ``value`` collection."""
    url = f"{FABRIC_API_ROOT}/{path.lstrip('/')}"
    params = {}
    while True:
        response = requests.get(url, headers=auth_headers(credential), params=params, timeout=timeout)
        _raise_for_status(response, f"GET {path}")
        body = response.json()
        yield from body.get("value", [])
        token = body.get("continuationToken")
        if not token:
            return
        params = {"continuationToken": token}


def find_workspace_id(credential, workspace_name: str) -> str:
    """Resolve a workspace display name to its id (exact, case-insensitive)."""
    matches = [
        ws for ws in paged_get(credential, "workspaces")
        if ws.get("displayName", "").lower() == workspace_name.lower()
    ]
    if not matches:
        raise FabricApiError(
            f"Workspace '{workspace_name}' was not found or the deployment identity has no access to it."
        )
    if len(matches) > 1:
        raise FabricApiError(
            f"Workspace name '{workspace_name}' is ambiguous ({len(matches)} matches). Use --workspace-id."
        )
    return matches[0]["id"]


def find_item_id(credential, workspace_id: str, item_type: str, display_name: str) -> str:
    """Resolve an item display name within a workspace to its id."""
    for item in paged_get(credential, f"workspaces/{workspace_id}/items?type={item_type}"):
        if item.get("displayName", "").lower() == display_name.lower():
            return item["id"]
    raise FabricApiError(f"{item_type} '{display_name}' was not found in workspace {workspace_id}.")


def run_item_job(
    credential,
    workspace_id: str,
    item_id: str,
    job_type: str,
    execution_data: Optional[dict] = None,
    timeout_minutes: int = 30,
    poll_seconds: int = 20,
) -> dict:
    """Start an on-demand item job and wait for it to finish.

    Returns the final job instance document. Raises FabricApiError when the
    job fails, is cancelled or does not finish before ``timeout_minutes``.
    """
    url = f"{FABRIC_API_ROOT}/workspaces/{workspace_id}/items/{item_id}/jobs/instances"
    body = {"executionData": execution_data} if execution_data else None
    response = requests.post(
        url, headers=auth_headers(credential), params={"jobType": job_type}, json=body, timeout=60
    )
    _raise_for_status(response, f"Start {job_type} job")

    status_url = response.headers.get("Location")
    if not status_url:
        raise FabricApiError("The job was accepted but no Location header was returned to poll.")

    retry_after = int(response.headers.get("Retry-After", poll_seconds) or poll_seconds)
    deadline = time.monotonic() + timeout_minutes * 60
    terminal = {"Completed", "Failed", "Cancelled", "Deduped"}

    while True:
        time.sleep(max(5, min(retry_after, 60)))
        poll = requests.get(status_url, headers=auth_headers(credential), timeout=60)
        _raise_for_status(poll, "Poll job status")
        job = poll.json()
        status = job.get("status", "Unknown")
        print(f"  job {job.get('id', '')}: {status}")
        if status in terminal:
            if status != "Completed":
                failure = job.get("failureReason") or {}
                raise FabricApiError(
                    f"Job finished with status {status}: {failure.get('message', failure) or 'no details'}"
                )
            return job
        if time.monotonic() > deadline:
            raise FabricApiError(f"Job did not finish within {timeout_minutes} minutes (last status {status}).")
