# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Static validation of an exported Fabric workspace folder.

Runs in the CI pipeline before anything is deployed. It does not call any
Fabric API, so it can also run as a pre-commit check on a developer machine.

Checks
------
* every item folder is named ``<DisplayName>.<ItemType>`` and carries a
  ``.platform`` file whose ``metadata.type`` matches the folder suffix
* logical ids are unique across the workspace
* item types are supported by fabric-cicd (when the package is installed) or
  by the built-in fallback list
* the parameter file parses, uses only known top-level sections, and every
  ``replace_value`` provides a value for every promoted environment
* nothing that looks like a secret has been committed

Exit code 1 when any error is found; warnings never fail the run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

FALLBACK_ITEM_TYPES = [
    "ApacheAirflowJob", "CopyJob", "DataAgent", "DataBuildToolJob", "DataPipeline", "Dataflow",
    "Environment", "Eventhouse", "Eventstream", "GraphQLApi", "KQLDashboard", "KQLDatabase",
    "KQLQueryset", "Lakehouse", "Map", "MirroredDatabase", "MLExperiment", "MountedDataFactory",
    "Notebook", "Ontology", "PaginatedReport", "Reflex", "Report", "SemanticModel",
    "SparkJobDefinition", "SQLDatabase", "UserDataFunction", "VariableLibrary", "Warehouse",
]

PARAMETER_SECTIONS = {"find_replace", "key_value_replace", "spark_pool"}

SECRET_PATTERNS = [
    ("client secret", re.compile(r"(?i)(client[_-]?secret|app[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9~._-]{20,}")),
    ("connection string password", re.compile(r"(?i)(password|pwd)\s*=\s*[^;\s'\"]{6,}")),
    ("SAS token", re.compile(r"(?i)[?&]sig=[A-Za-z0-9%+/=]{20,}")),
    ("storage account key", re.compile(r"(?i)accountkey\s*=\s*[A-Za-z0-9+/=]{60,}")),
    ("Azure DevOps PAT", re.compile(r"(?<![A-Za-z0-9])[a-z0-9]{52}(?![A-Za-z0-9])")),
]
SCAN_EXTENSIONS = {".py", ".json", ".yml", ".yaml", ".txt", ".ipynb", ".sql", ".tmdl", ".pbir", ".platform", ""}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.errors.append(message)
        print(f"##vso[task.logissue type=error]{message}")

    def warning(self, message: str) -> None:
        self.warnings.append(message)
        print(f"##vso[task.logissue type=warning]{message}")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errorCount": len(self.errors),
            "warningCount": len(self.warnings),
            "errors": self.errors,
            "warnings": self.warnings,
            "items": self.items,
        }


def supported_item_types() -> list[str]:
    try:
        from fabric_cicd import constants

        return sorted(constants.ACCEPTED_ITEM_TYPES)
    except Exception:  # noqa: BLE001 - package optional at validation time
        return FALLBACK_ITEM_TYPES


def parse_list(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").strip().strip("[]").split(",") if x.strip()]


def validate_items(repo_dir: Path, item_types_in_scope: list[str], report: Report) -> None:
    supported = supported_item_types()
    supported_lookup = {t.lower(): t for t in supported}
    logical_ids: dict[str, str] = {}
    item_count = 0

    for folder in sorted(p for p in repo_dir.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        if "." not in folder.name:
            report.warning(f"Folder '{folder.name}' is not an item folder (<name>.<ItemType>) and will be ignored.")
            continue

        display_name, _, suffix = folder.name.rpartition(".")
        item_type = supported_lookup.get(suffix.lower())
        if item_type is None:
            report.error(f"Item folder '{folder.name}' has unsupported item type '{suffix}'.")
            continue

        platform_file = folder / ".platform"
        if not platform_file.is_file():
            report.error(f"Item folder '{folder.name}' has no .platform file.")
            continue

        try:
            platform = json.loads(platform_file.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            report.error(f"{platform_file} is not valid JSON: {exc}")
            continue

        metadata = platform.get("metadata", {})
        config = platform.get("config", {})
        meta_type = metadata.get("type")
        meta_name = metadata.get("displayName")
        logical_id = config.get("logicalId")

        if meta_type != item_type:
            report.error(f"{folder.name}: .platform type '{meta_type}' does not match folder suffix '{item_type}'.")
        if meta_name and meta_name != display_name:
            report.warning(f"{folder.name}: .platform displayName '{meta_name}' differs from folder name '{display_name}'.")
        if not logical_id:
            report.error(f"{folder.name}: .platform has no config.logicalId.")
        elif logical_id in logical_ids:
            report.error(f"{folder.name}: logicalId {logical_id} is also used by '{logical_ids[logical_id]}'.")
        else:
            logical_ids[logical_id] = folder.name

        definition_files = [p for p in folder.rglob("*") if p.is_file() and p.name != ".platform"]
        if not definition_files and item_type not in {"Lakehouse", "Warehouse", "SQLDatabase"}:
            report.warning(f"{folder.name}: no definition files found besides .platform.")

        in_scope = item_type in item_types_in_scope if item_types_in_scope else True
        if not in_scope:
            report.warning(f"{folder.name}: item type {item_type} is not in the deployment scope and will not be deployed.")

        item_count += 1
        report.items.append({
            "folder": folder.name,
            "type": item_type,
            "displayName": meta_name or display_name,
            "logicalId": logical_id,
            "inScope": in_scope,
            "files": len(definition_files),
        })

    if item_count == 0:
        report.error(f"No Fabric items found under {repo_dir}. Was the workspace exported to this folder?")
    else:
        print(f"Found {item_count} item folder(s) under {repo_dir}.")


def validate_parameter_file(parameter_file: Path, environments: list[str], repo_dir: Path, report: Report) -> None:
    if not parameter_file.is_file():
        report.warning(f"Parameter file {parameter_file} not found; deployments will not be parameterized.")
        return
    if yaml is None:
        report.warning("PyYAML is not installed; skipping parameter file validation.")
        return

    try:
        content = yaml.safe_load(parameter_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        report.error(f"{parameter_file} is not valid YAML: {exc}")
        return

    if not isinstance(content, dict):
        report.error(f"{parameter_file} must be a mapping with find_replace / key_value_replace / spark_pool sections.")
        return

    unknown = set(content) - PARAMETER_SECTIONS
    if unknown:
        report.error(f"{parameter_file}: unknown section(s) {', '.join(sorted(unknown))}. Expected {', '.join(sorted(PARAMETER_SECTIONS))}.")

    repo_text_cache: dict[Path, str] = {}

    def repo_contains(value: str) -> bool:
        for path in repo_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SCAN_EXTENSIONS:
                continue
            if path not in repo_text_cache:
                try:
                    repo_text_cache[path] = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    repo_text_cache[path] = ""
            if value in repo_text_cache[path]:
                return True
        return False

    for section in ("find_replace", "key_value_replace", "spark_pool"):
        entries = content.get(section) or []
        if not isinstance(entries, list):
            report.error(f"{parameter_file}: section '{section}' must be a list.")
            continue
        for index, entry in enumerate(entries, start=1):
            label = f"{section}[{index}]"
            if not isinstance(entry, dict):
                report.error(f"{parameter_file}: {label} must be a mapping.")
                continue
            key_field = "find_value" if section == "find_replace" else "find_key" if section == "key_value_replace" else "instance_pool_id"
            find_value = entry.get(key_field)
            if not find_value:
                report.error(f"{parameter_file}: {label} is missing '{key_field}'.")
            replace_value = entry.get("replace_value")
            if not isinstance(replace_value, dict) or not replace_value:
                report.error(f"{parameter_file}: {label} needs a 'replace_value' mapping keyed by environment.")
                continue
            missing = [env for env in environments if env not in replace_value]
            if missing:
                report.error(f"{parameter_file}: {label} has no replace_value for environment(s) {', '.join(missing)}.")
            extra = [env for env in replace_value if env not in environments]
            if extra:
                report.warning(f"{parameter_file}: {label} defines environment(s) {', '.join(extra)} that are not promoted by this pipeline.")
            if section == "find_replace" and isinstance(find_value, str) and not entry.get("is_regex") \
                    and not find_value.startswith("$") and not repo_contains(find_value):
                report.warning(f"{parameter_file}: {label} find_value '{find_value}' does not occur anywhere under {repo_dir}.")


def scan_for_secrets(repo_dir: Path, report: Report) -> None:
    for path in repo_dir.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in SCAN_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                report.error(f"Possible {label} committed in {path.relative_to(repo_dir)}:{line}. Move it to Key Vault / a variable library.")
                break


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository-directory", required=True)
    parser.add_argument("--parameter-file", default="config/parameter.yml")
    parser.add_argument("--environments", default="DEV,TEST,PROD", help="Comma separated environment keys.")
    parser.add_argument("--item-types", default="all", help="Comma separated item types in deployment scope, or 'all'.")
    parser.add_argument("--report", default=None, help="Write a JSON report to this path.")
    parser.add_argument("--skip-secret-scan", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> Report:
    args = build_parser().parse_args(argv)
    report = Report()

    repo_dir = Path(args.repository_directory).resolve()
    if not repo_dir.is_dir():
        report.error(f"Repository directory not found: {repo_dir}")
    else:
        item_types = [] if args.item_types.strip().lower() in ("", "all") else parse_list(args.item_types)
        validate_items(repo_dir, item_types, report)
        validate_parameter_file(Path(args.parameter_file).resolve(), parse_list(args.environments), repo_dir, report)
        if not args.skip_secret_scan:
            scan_for_secrets(repo_dir, report)

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    print(f"Validation finished: {len(report.errors)} error(s), {len(report.warnings)} warning(s).")
    return report


def main(argv: list[str] | None = None) -> int:
    return 0 if run(argv).ok else 1


if __name__ == "__main__":
    sys.exit(main())
