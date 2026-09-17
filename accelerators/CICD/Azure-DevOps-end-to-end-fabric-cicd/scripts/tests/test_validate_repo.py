# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for validate_repo.py and the argument helpers in fabric_deploy.py.

They run against the sample workspace shipped with the accelerator plus a few
synthetic folders, and never call a Fabric API.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
ACCELERATOR = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import validate_repo  # noqa: E402
from fabric_deploy import parse_item_types  # noqa: E402
from run_post_deploy_notebook import normalise_parameters  # noqa: E402

SAMPLE_WORKSPACE = ACCELERATOR / "workspace"
SAMPLE_PARAMETERS = ACCELERATOR / "config" / "parameter.yml"


def make_item(root: Path, name: str, item_type: str, logical_id: str | None = None, extra_file: bool = True) -> Path:
    folder = root / f"{name}.{item_type}"
    folder.mkdir(parents=True)
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": item_type, "displayName": name, "description": ""},
        "config": {"version": "2.0", "logicalId": logical_id or str(uuid.uuid4())},
    }
    (folder / ".platform").write_text(json.dumps(platform), encoding="utf-8")
    if extra_file:
        (folder / "definition.txt").write_text("hello", encoding="utf-8")
    return folder


def test_sample_workspace_is_valid(tmp_path):
    report = validate_repo.run([
        "--repository-directory", str(SAMPLE_WORKSPACE),
        "--parameter-file", str(SAMPLE_PARAMETERS),
        "--environments", "DEV,TEST,PROD",
        "--report", str(tmp_path / "report.json"),
    ])
    assert report.ok, report.errors
    written = json.loads((tmp_path / "report.json").read_text())
    assert written["ok"] is True
    assert {item["type"] for item in written["items"]} >= {"Notebook", "Lakehouse"}


def test_missing_platform_file_is_an_error(tmp_path):
    folder = tmp_path / "Broken.Notebook"
    folder.mkdir()
    (folder / "notebook-content.py").write_text("# nothing", encoding="utf-8")
    report = validate_repo.run(["--repository-directory", str(tmp_path), "--parameter-file", "missing.yml"])
    assert not report.ok
    assert any("no .platform" in e for e in report.errors)


def test_type_mismatch_and_duplicate_logical_id(tmp_path):
    shared = str(uuid.uuid4())
    make_item(tmp_path, "One", "Notebook", logical_id=shared)
    make_item(tmp_path, "Two", "Notebook", logical_id=shared)
    bad = make_item(tmp_path, "Three", "DataPipeline")
    platform = json.loads((bad / ".platform").read_text())
    platform["metadata"]["type"] = "Notebook"
    (bad / ".platform").write_text(json.dumps(platform))

    report = validate_repo.run(["--repository-directory", str(tmp_path), "--parameter-file", "missing.yml"])
    assert any("also used by" in e for e in report.errors)
    assert any("does not match folder suffix" in e for e in report.errors)


def test_unsupported_item_type(tmp_path):
    make_item(tmp_path, "Thing", "Widget")
    report = validate_repo.run(["--repository-directory", str(tmp_path), "--parameter-file", "missing.yml"])
    assert any("unsupported item type 'Widget'" in e for e in report.errors)


def test_parameter_file_missing_environment(tmp_path):
    make_item(tmp_path, "Nb", "Notebook")
    (tmp_path / "Nb.Notebook" / "definition.txt").write_text("lakehouse-id-dev", encoding="utf-8")
    params = tmp_path / "parameter.yml"
    params.write_text(
        "find_replace:\n"
        "  - find_value: lakehouse-id-dev\n"
        "    replace_value:\n"
        "      TEST: lakehouse-id-test\n",
        encoding="utf-8",
    )
    report = validate_repo.run([
        "--repository-directory", str(tmp_path),
        "--parameter-file", str(params),
        "--environments", "DEV,TEST,PROD",
    ])
    assert any("no replace_value for environment(s) DEV, PROD" in e for e in report.errors)


def test_unknown_parameter_section(tmp_path):
    make_item(tmp_path, "Nb", "Notebook")
    params = tmp_path / "parameter.yml"
    params.write_text("replace_everything:\n  - a: b\n", encoding="utf-8")
    report = validate_repo.run(["--repository-directory", str(tmp_path), "--parameter-file", str(params)])
    assert any("unknown section(s) replace_everything" in e for e in report.errors)


def test_secret_scan_flags_client_secret(tmp_path):
    folder = make_item(tmp_path, "Nb", "Notebook")
    (folder / "notebook-content.py").write_text(
        'client_secret = "abcdefghijklmnopqrstuvwxyz0123456789~._-"\n', encoding="utf-8"
    )
    report = validate_repo.run(["--repository-directory", str(tmp_path), "--parameter-file", "missing.yml"])
    assert any("Possible client secret" in e for e in report.errors)

    clean = validate_repo.run([
        "--repository-directory", str(tmp_path), "--parameter-file", "missing.yml", "--skip-secret-scan",
    ])
    assert clean.ok


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("all", ["DataPipeline", "Lakehouse", "Notebook"]),
        ("", ["DataPipeline", "Lakehouse", "Notebook"]),
        ("Notebook,lakehouse", ["Notebook", "Lakehouse"]),
        ('["Notebook","DataPipeline"]', ["Notebook", "DataPipeline"]),
    ],
)
def test_parse_item_types(raw, expected):
    assert parse_item_types(raw, ["DataPipeline", "Lakehouse", "Notebook"]) == expected


def test_parse_item_types_rejects_unknown():
    with pytest.raises(ValueError):
        parse_item_types("Notebook,Widget", ["Notebook"])


def test_normalise_parameters_converts_plain_values():
    result = normalise_parameters('{"env": "TEST", "retries": 3, "dry": true, "ratio": 0.5}')
    assert result == {
        "env": {"type": "string", "value": "TEST"},
        "retries": {"type": "int", "value": 3},
        "dry": {"type": "bool", "value": True},
        "ratio": {"type": "float", "value": 0.5},
    }


def test_normalise_parameters_keeps_explicit_format():
    result = normalise_parameters('{"env": {"type": "string", "value": "PROD"}}')
    assert result == {"env": {"type": "string", "value": "PROD"}}
    assert normalise_parameters("") == {}
