"""Select changed PR projects and verify independently generated project scores."""

import argparse
import csv
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml


PROJECT_FIELDS = ("name", "github_id", "description", "category")
GITHUB_ID = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9_.-]{1,100}\Z"
)


def load_projects(path):
    """Read and validate project entries without accepting YAML constructors."""
    path = Path(path)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: cannot read project YAML: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("projects"), list):
        raise ValueError(f"{path}: expected a mapping with a projects list")
    projects = {}
    for index, project in enumerate(document["projects"], 1):
        prefix = f"{path}: project {index}"
        if not isinstance(project, dict):
            raise ValueError(f"{prefix}: expected a mapping")
        github_id = project.get("github_id")
        if (
            not isinstance(github_id, str)
            or not GITHUB_ID.fullmatch(github_id)
            or "--" in github_id.split("/")[0]
            or github_id.split("/")[-1] in (".", "..")
        ):
            raise ValueError(f"{prefix}: github_id must be a valid owner/repo")
        key = github_id.lower()
        if key in projects:
            raise ValueError(f"{prefix}: duplicate github_id {github_id!r}")
        for field in PROJECT_FIELDS:
            if field in project and not isinstance(project[field], str):
                raise ValueError(f"{prefix}: {field} must be a string")
        projects[key] = project
    return projects


def select_projects(base_path, head_path):
    """Compare full entries, then discard all non-allowlisted scoring inputs."""
    base = load_projects(base_path)
    head = load_projects(head_path)
    selected = []
    for key, project in head.items():
        previous = base.get(key)
        if previous is None or project != previous:
            selected.append(
                {field: project[field] for field in PROJECT_FIELDS if field in project}
            )
    return selected


def write_summary(text):
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(text + "\n")


def prepare(base_path, head_path, workdir):
    selected = select_projects(base_path, head_path)
    workdir = Path(workdir)
    if workdir.exists() and any(workdir.iterdir()):
        raise ValueError(f"{workdir}: work directory must be empty; do not reuse scoring output")
    workdir.mkdir(parents=True, exist_ok=True)
    history = workdir / "output" / "history"
    history.mkdir(parents=True)
    config = {
        "output_file": str(workdir / "output" / "README.md"),
        "projects_history_folder": str(history),
        "min_projectrank": 0,
        "min_stars": 0,
        "require_license": False,
        "require_repo": False,
        "min_description_length": 0,
        "generate_install_hints": False,
    }
    (workdir / "projects.yaml").write_text(
        yaml.safe_dump({"configuration": config, "projects": selected}, sort_keys=False),
        encoding="utf-8",
    )
    (workdir / "selected.json").write_text(
        json.dumps({"github_ids": [entry["github_id"] for entry in selected],
                    "started_at_ns": time.time_ns()}, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"count={len(selected)}\n")
    if not selected:
        write_summary("## PR project score\n\nNo added or modified projects to score.")
    return len(selected)


def numeric_rank(value, label):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label}: expected a finite nonnegative number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label}: expected a finite nonnegative number")
    return number


def read_results(workdir, started_at_ns):
    files = list((workdir / "output" / "history").glob("*_projects.csv"))
    if len(files) != 1:
        raise ValueError(f"expected exactly one fresh *_projects.csv, found {len(files)}")
    path = files[0]
    if path.stat().st_mtime_ns < started_at_ns:
        raise ValueError(f"{path}: stale scoring CSV predates this run")
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, strict=True)
        fields = [field.lower() for field in (reader.fieldnames or [])]
        if len(fields) != len(set(fields)) or not {"github_id", "projectrank"} <= set(fields):
            raise ValueError(f"{path}: CSV needs unique github_id and projectrank columns")
        reader.fieldnames = fields
        rows = {}
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path}: malformed CSV row {reader.line_num}")
            rows.setdefault(row["github_id"].lower(), []).append(row)
    return rows


def verify(workdir, minimum, generator_outcome):
    minimum = numeric_rank(minimum, "minimum projectrank")
    workdir = Path(workdir)
    manifest = json.loads((workdir / "selected.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("selected.json: invalid selection manifest")
    ids, started_at_ns = manifest.get("github_ids"), manifest.get("started_at_ns")
    if (
        not isinstance(ids, list)
        or any(not isinstance(key, str) or not GITHUB_ID.fullmatch(key) for key in ids)
        or len({key.lower() for key in ids}) != len(ids)
        or type(started_at_ns) is not int
        or started_at_ns <= 0
    ):
        raise ValueError("selected.json: invalid selection manifest")
    errors = []
    if generator_outcome != "success":
        errors.append(f"Generator error: outcome was {generator_outcome!r}, not success.")
    rows = {}
    if ids:
        try:
            rows = read_results(workdir, started_at_ns)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            errors.append(f"Scoring output error: {exc}")
    table = ["## PR project score", "",
             "| Project | Rank | Minimum | Stars | Forks | Contributors | Commits | Recent commits (90d) | Releases | Result |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for github_id in ids:
        matches = rows.get(github_id.lower(), [])
        rank = "—"
        stats = ["—"] * 6
        if not matches:
            result = "ERROR: missing scoring row"
        elif len(matches) != 1:
            result = "ERROR: duplicate scoring rows"
        else:
            row = matches[0]
            for index, field in enumerate(("star_count", "fork_count", "contributor_count",
                                           "commit_count", "recent_commit_count", "release_count")):
                try:
                    stats[index] = format(numeric_rank(row.get(field), field), "f")
                    if "." in stats[index]:
                        stats[index] = stats[index].rstrip("0").rstrip(".")
                except ValueError:
                    pass
            try:
                rank = numeric_rank(row["projectrank"], f"{github_id} projectrank")
                # GitHub default-branch metadata proves a fetch; zero-star repos omit star_count.
                missing = [field for field in ("github_url", "created_at", "last_commit_pushed_at")
                           if not row.get(field, "").strip()]
                if missing:
                    result = "ERROR: missing GitHub metadata: " + ", ".join(missing)
                elif rank < minimum:
                    result = "FAIL: below minimum"
                else:
                    result = "PASS"
            except ValueError as exc:
                result = f"ERROR: {exc}"
        project = f"[{github_id}](https://github.com/{github_id})"
        table.append(f"| {project} | {rank} | {minimum} | {' | '.join(stats)} | {result} |")
        if result != "PASS":
            errors.append(f"{github_id}: {result}")
    if not ids:
        table.append("\nNo added or modified projects to score.")
    if errors:
        table.extend(["", "### Errors", "", *[f"- {error}" for error in errors]])
    report = "\n".join(table)
    (workdir / "report.md").write_text(report, encoding="utf-8")
    write_summary(report)
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("base_yaml")
    prepare_parser.add_argument("head_yaml")
    prepare_parser.add_argument("workdir")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("workdir")
    verify_parser.add_argument("--minimum", required=True)
    verify_parser.add_argument("--generator-outcome", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.base_yaml, args.head_yaml, args.workdir)
            return 0
        return verify(args.workdir, args.minimum, args.generator_outcome)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
