# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Publish a Fabric workspace definition from the repository with fabric-cicd.

Designed to run inside an ``AzureCLI@2`` task in Azure Pipelines, but works
just as well from a developer machine after ``az login``.

Examples
--------
Deploy to the Dev workspace by name::

    python fabric_deploy.py --workspace-name Fabric-CICD-Dev --environment DEV \
        --repository-directory workspace --item-types Notebook,DataPipeline

Deploy everything fabric-cicd supports and remove orphans::

    python fabric_deploy.py --workspace-id <guid> --environment PROD \
        --repository-directory workspace --item-types all --unpublish-orphans

Exit codes: 0 success, 1 deployment failure, 2 bad arguments / configuration.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fabric_auth import AUTH_MODES, get_credential  # noqa: E402

# Force unbuffered output so pipeline logs stream in order.
sys.stdout.reconfigure(line_buffering=True, write_through=True)
sys.stderr.reconfigure(line_buffering=True, write_through=True)


def log(message: str) -> None:
    print(message, flush=True)


def vso_error(message: str) -> None:
    print(f"##vso[task.logissue type=error]{message}", flush=True)


def vso_warning(message: str) -> None:
    print(f"##vso[task.logissue type=warning]{message}", flush=True)


def vso_set_output(name: str, value: str) -> None:
    print(f"##vso[task.setvariable variable={name};isOutput=true]{value}", flush=True)


def cleanup_copied_parameter_file(path: Path | None) -> None:
    """Remove the parameter.yml copy so the checkout / artifact stays untouched."""
    if path is None:
        return
    try:
        path.unlink()
    except OSError as exc:  # pragma: no cover - best effort
        vso_warning(f"Could not remove temporary parameter file {path}: {exc}")


def parse_item_types(raw: str, accepted: list[str]) -> list[str]:
    raw = (raw or "").strip().strip("[]")
    if not raw or raw.lower() == "all":
        return list(accepted)
    requested = [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]
    lookup = {t.lower(): t for t in accepted}
    unknown = [t for t in requested if t.lower() not in lookup]
    if unknown:
        raise ValueError(
            f"Unsupported item type(s): {', '.join(unknown)}. Supported: {', '.join(accepted)}"
        )
    return [lookup[t.lower()] for t in requested]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace-id", help="Target workspace id (GUID).")
    target.add_argument("--workspace-name", help="Target workspace display name (resolved through the API).")
    parser.add_argument("--environment", required=True, help="Environment key used in parameter.yml (DEV, TEST, PROD ...).")
    parser.add_argument("--repository-directory", required=True, help="Folder containing the exported workspace items.")
    parser.add_argument("--parameter-file", default=None,
                        help="fabric-cicd parameter file. Defaults to <repository-directory>/parameter.yml if present.")
    parser.add_argument("--item-types", default="all", help="Comma separated item types, or 'all'.")
    parser.add_argument("--unpublish-orphans", action="store_true",
                        help="Delete items from the workspace that are not in the repository.")
    parser.add_argument("--orphan-exclude-regex", default="",
                        help="Regex of item names to keep when unpublishing orphans.")
    parser.add_argument("--feature-flags", default="", help="Comma separated fabric-cicd feature flags.")
    parser.add_argument("--auth", default="cli", choices=AUTH_MODES, help="Authentication mode (default: cli).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve the target and list what would be published, then stop.")
    parser.add_argument("--debug", action="store_true", help="Enable fabric-cicd DEBUG logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from fabric_cicd import (  # imported here so --help works without the package
            FabricWorkspace,
            append_feature_flag,
            change_log_level,
            constants,
            publish_all_items,
            unpublish_all_orphan_items,
        )
    except ImportError as exc:  # pragma: no cover - environment problem
        vso_error(f"fabric-cicd is not installed: {exc}. Run pip install -r scripts/requirements.txt")
        return 2

    if args.debug or os.getenv("SYSTEM_DEBUG", "false").lower() == "true":
        change_log_level("DEBUG")

    repository_directory = Path(args.repository_directory).resolve()
    if not repository_directory.is_dir():
        vso_error(f"Repository directory not found: {repository_directory}")
        return 2

    accepted = sorted(constants.ACCEPTED_ITEM_TYPES)
    try:
        item_types = parse_item_types(args.item_types, accepted)
    except ValueError as exc:
        vso_error(str(exc))
        return 2

    for flag in [f.strip() for f in args.feature_flags.split(",") if f.strip()]:
        log(f"Enabling fabric-cicd feature flag: {flag}")
        append_feature_flag(flag)

    # fabric-cicd looks for parameter.yml inside the repository directory. When
    # the parameter file lives elsewhere (config/parameter.yml in this
    # accelerator) copy it in place so a single file drives every environment.
    parameter_file = Path(args.parameter_file).resolve() if args.parameter_file else None
    copied_parameter_file: Path | None = None
    if parameter_file and parameter_file.is_file():
        destination = repository_directory / "parameter.yml"
        if destination.resolve() != parameter_file:
            if destination.exists():
                vso_warning(f"{destination} already exists and will be replaced by {parameter_file} for this run.")
            destination.write_bytes(parameter_file.read_bytes())
            copied_parameter_file = destination
            log(f"Using parameter file {parameter_file} (copied to {destination})")
    elif parameter_file:
        vso_warning(f"Parameter file {parameter_file} not found; deploying without parameterization.")

    try:
        credential = get_credential(args.auth)
    except Exception as exc:  # noqa: BLE001
        vso_error(f"Could not build credential for auth mode '{args.auth}': {exc}")
        return 2

    workspace_id = args.workspace_id
    if not workspace_id:
        from fabric_api import FabricApiError, find_workspace_id

        try:
            workspace_id = find_workspace_id(credential, args.workspace_name)
        except FabricApiError as exc:
            vso_error(str(exc))
            return 1
        log(f"Resolved workspace '{args.workspace_name}' to {workspace_id}")

    vso_set_output("fabricWorkspaceId", workspace_id)

    log("=" * 78)
    log(f"Target workspace   : {workspace_id}")
    log(f"Environment key    : {args.environment}")
    log(f"Repository folder  : {repository_directory}")
    log(f"Item types         : {', '.join(item_types)}")
    log(f"Unpublish orphans  : {args.unpublish_orphans}")
    log("=" * 78)

    if args.dry_run:
        found = sorted(p.name for p in repository_directory.iterdir() if p.is_dir() and "." in p.name)
        log("Dry run - item folders that would be considered:")
        for name in found:
            log(f"  - {name}")
        cleanup_copied_parameter_file(copied_parameter_file)
        return 0

    try:
        workspace = FabricWorkspace(
            workspace_id=workspace_id,
            environment=args.environment,
            repository_directory=str(repository_directory),
            item_type_in_scope=item_types,
            token_credential=credential,
        )
        publish_all_items(workspace)
        if args.unpublish_orphans:
            if args.orphan_exclude_regex:
                unpublish_all_orphan_items(workspace, item_name_exclude_regex=args.orphan_exclude_regex)
            else:
                unpublish_all_orphan_items(workspace)
    except Exception as exc:  # noqa: BLE001 - surface every failure to the pipeline
        vso_error(f"Deployment to workspace {workspace_id} failed: {exc}")
        return 1
    finally:
        cleanup_copied_parameter_file(copied_parameter_file)

    log(f"Deployment to workspace {workspace_id} completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
