# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Run a notebook in the target workspace after a deployment and wait for it.

Typical uses: seed reference data, refresh a semantic model, apply OneLake
security roles, or re-bind connections that fabric-cicd parameterization
cannot express. The notebook is executed through the Job Scheduler API with
the pipeline's own identity, so it must be shared with (or owned by) the
deployment identity.

Example::

    python run_post_deploy_notebook.py --workspace-id <guid> \
        --notebook-name nb_post_deployment \
        --parameters '{"environment": {"type": "string", "value": "TEST"}}'

``--parameters`` uses the Fabric notebook parameter format
(``{"name": {"type": "string|int|float|bool", "value": ...}}``). Plain
``{"name": "value"}`` mappings are converted for you.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fabric_api import FabricApiError, find_item_id, run_item_job  # noqa: E402
from fabric_auth import AUTH_MODES, get_credential  # noqa: E402

sys.stdout.reconfigure(line_buffering=True, write_through=True)


def normalise_parameters(raw: str | None) -> dict:
    if not raw or not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--parameters must be a JSON object.")
    result = {}
    for name, value in parsed.items():
        if isinstance(value, dict) and "type" in value and "value" in value:
            result[name] = value
            continue
        if isinstance(value, bool):
            kind = "bool"
        elif isinstance(value, int):
            kind = "int"
        elif isinstance(value, float):
            kind = "float"
        else:
            kind, value = "string", str(value)
        result[name] = {"type": kind, "value": value}
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace-id", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--notebook-id")
    target.add_argument("--notebook-name")
    parser.add_argument("--parameters", default="{}", help="JSON object of notebook parameters.")
    parser.add_argument("--timeout-minutes", type=int, default=30)
    parser.add_argument("--auth", default="cli", choices=AUTH_MODES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        parameters = normalise_parameters(args.parameters)
    except ValueError as exc:
        print(f"##vso[task.logissue type=error]Invalid --parameters: {exc}")
        return 2

    credential = get_credential(args.auth)

    try:
        notebook_id = args.notebook_id or find_item_id(credential, args.workspace_id, "Notebook", args.notebook_name)
        print(f"Running notebook {args.notebook_name or notebook_id} ({notebook_id}) in workspace {args.workspace_id}")
        execution_data = {"parameters": parameters} if parameters else None
        job = run_item_job(
            credential,
            args.workspace_id,
            notebook_id,
            job_type="RunNotebook",
            execution_data=execution_data,
            timeout_minutes=args.timeout_minutes,
        )
    except FabricApiError as exc:
        print(f"##vso[task.logissue type=error]{exc}")
        return 1

    print(f"Notebook run {job.get('id')} completed (started {job.get('startTimeUtc')}, ended {job.get('endTimeUtc')}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
