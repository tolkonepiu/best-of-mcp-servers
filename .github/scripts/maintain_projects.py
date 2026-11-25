"""Remove hidden projects, update repository identities, and remove duplicates."""

import argparse
import csv
from datetime import date
from pathlib import Path
import re
import sys

import yaml


def latest_history(history):
    candidates = []
    for path in history.glob("*_projects.csv"):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_projects\.csv", path.name)
        if not match:
            raise ValueError(f"Invalid history filename: {path.name}")
        candidates.append((date.fromisoformat(match[1]), path))
    if not candidates:
        raise ValueError(f"No dated project CSV found in {history}")
    return max(candidates)[1]


def history_rows(path, require_updates=False):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        header = next(reader, [])
        if len(header) != len(set(header)):
            raise ValueError("Duplicate CSV columns")
        required = {"github_id", "show"}
        if require_updates:
            required.add("updated_github_id")
        if not required <= set(header):
            raise ValueError(f"CSV requires columns: {', '.join(sorted(required))}")
        for row in reader:
            if len(row) != len(header):
                raise ValueError(f"Malformed CSV row at line {reader.line_num}")
            row = dict(zip(header, row))
            show = row["show"].strip().casefold()
            if show not in {"true", "false"}:
                raise ValueError(f"Invalid show value at line {reader.line_num}")
            row["show"] = show
            rows.append(row)
    return rows


def hidden_ids(path):
    return {row["github_id"].strip().casefold() for row in history_rows(path)
            if row["github_id"].strip() and row["show"] == "false"}


def validate_node(node):
    if isinstance(node, yaml.MappingNode):
        keys = set()
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                raise ValueError("YAML mapping keys must be strings")
            if key.value in keys:
                raise ValueError(f"Duplicate YAML key: {key.value}")
            keys.add(key.value)
            validate_node(value)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            validate_node(value)


def content_end(node):
    """Mapping end marks include following comments; use the last scalar instead."""
    if isinstance(node, yaml.MappingNode):
        return max(content_end(child) for pair in node.value for child in pair)
    if isinstance(node, yaml.SequenceNode) and node.value:
        return max(content_end(child) for child in node.value)
    return node.end_mark.index


def project_document(original):
    text = original.decode("utf-8")
    if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
           for token in yaml.scan(text)):
        raise ValueError("YAML anchors and aliases are not supported")
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("YAML root must be a mapping")
    validate_node(root)
    data = yaml.safe_load(text)
    projects = dict((key.value, value) for key, value in root.value).get("projects")
    if not isinstance(projects, yaml.SequenceNode):
        raise ValueError("YAML projects must be a list")
    if projects.flow_style and projects.value:
        raise ValueError("YAML projects must use a block list")

    for index, node in enumerate(projects.value):
        if not isinstance(node, yaml.MappingNode) or node.flow_style:
            raise ValueError("YAML projects must contain block mappings")
        project = data["projects"][index]
        for field in ("github_id", "name"):
            if field in project and (
                not isinstance(project[field], str) or not project[field].strip()
            ):
                raise ValueError(f"YAML {field} must be a nonempty string")
    return text, data, projects


def cleaned_yaml(original, hidden):
    text, data, projects = project_document(original)
    removed = {index for index, project in enumerate(data["projects"])
               if project.get("github_id", "").strip().casefold() in hidden}
    return removed_entries(text, data, projects, removed)


def removed_entries(text, data, projects, removed):
    spans = []
    for index, node in enumerate(projects.value):
        if index not in removed:
            continue
        start = text.rfind("\n", 0, node.start_mark.index) + 1
        if not re.fullmatch(r" *- +", text[start:node.start_mark.index]):
            raise ValueError("Project list item must start on its own line")
        end = content_end(node)
        if end and text[end - 1] != "\n":
            newline = text.find("\n", end)
            end = len(text) if newline == -1 else newline + 1
        spans.append((start, end))

    updated = text
    for start, end in reversed(spans):
        updated = updated[:start] + updated[end:]
    if spans and len(spans) == len(projects.value):
        start = spans[0][0]
        indent = projects.start_mark.column
        newline = "\r\n" if "\r\n" in text else "\n"
        updated = updated[:start] + " " * indent + "[]" + newline + updated[start:]
    expected = [project for index, project in enumerate(data["projects"])
                if index not in removed]
    expected_data = dict(data, projects=expected)
    if yaml.safe_load(updated) != expected_data:
        raise ValueError("YAML range removal changed retained data")
    return updated.encode("utf-8"), len(spans)


def valid_repository(identity):
    return (
        re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}",
            identity,
        )
        and "--" not in identity.split("/")[0]
        and identity.split("/")[1] not in {".", ".."}
    )


def updated_identities(original, rows):
    text, data, projects = project_document(original)
    expected = [dict(project) for project in data["projects"]]
    for row in rows:
        updated = row["updated_github_id"].strip()
        if not updated:
            continue
        old = row["github_id"].strip()
        if not valid_repository(old) or not valid_repository(updated):
            raise ValueError(f"Invalid repository rename: {old!r} -> {updated!r}")
        for project in expected:
            if project.get("github_id", "").strip().casefold() == old.casefold():
                project["name"] = project["github_id"] = updated
    edits = []
    count = 0
    for project, renamed, node in zip(data["projects"], expected, projects.value):
        if project != renamed:
            if "name" not in project:
                raise ValueError("Renamed YAML project must have a name")
            fields = {key.value: value for key, value in node.value}
            for field in ("name", "github_id"):
                scalar = fields[field]
                if scalar.style not in (None, "'", '"'):
                    raise ValueError(f"Renamed YAML {field} must use a single-line scalar")
                replacement = renamed[field]
                if scalar.style:
                    replacement = scalar.style + replacement + scalar.style
                edits.append((scalar.start_mark.index, scalar.end_mark.index, replacement))
            count += 1
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if yaml.safe_load(text) != dict(data, projects=expected):
        raise ValueError("Repository identity update changed retained data")
    return text.encode("utf-8"), count


def update_github_ids(projects, history, dry_run=False):
    latest = latest_history(history)
    rows = history_rows(latest, require_updates=True)
    original = projects.read_bytes()
    updated, count = updated_identities(original, rows)
    if not dry_run and updated != original:
        projects.write_bytes(updated)
    print(f"{latest.name}: {'Would update' if dry_run else 'Updated'} {count} projects")
    return count


def remove_duplicate_names(projects, dry_run=False):
    original = projects.read_bytes()
    text, data, nodes = project_document(original)
    names = set()
    removed = set()
    for index, project in enumerate(data["projects"]):
        if "name" not in project:
            raise ValueError("YAML project must have a name for duplicate removal")
        name = project["name"].strip().casefold()
        if name in names:
            removed.add(index)
        names.add(name)
    updated, count = removed_entries(text, data, nodes, removed)
    if not dry_run and updated != original:
        projects.write_bytes(updated)
    print(f"{'Would remove' if dry_run else 'Removed'} {count} duplicate projects")
    return count


def cleanup(projects, history, dry_run=False):
    latest = latest_history(history)
    hidden = hidden_ids(latest)
    original = projects.read_bytes()
    updated, count = cleaned_yaml(original, hidden)
    if not dry_run and updated != original:
        projects.write_bytes(updated)
    print(f"{latest.name}: {'Would remove' if dry_run else 'Removed'} {count} projects")
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",
                        choices=("cleanup", "update-github-ids", "remove-duplicate-names"))
    parser.add_argument("--projects", type=Path, default=Path("projects.yaml"))
    parser.add_argument("--history", type=Path, default=Path("history"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "remove-duplicate-names":
            remove_duplicate_names(args.projects, args.dry_run)
        elif args.action == "update-github-ids":
            update_github_ids(args.projects, args.history, args.dry_run)
        else:
            cleanup(args.projects, args.history, args.dry_run)
    except (OSError, ValueError, csv.Error, yaml.YAMLError) as error:
        print(f"Cleanup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
