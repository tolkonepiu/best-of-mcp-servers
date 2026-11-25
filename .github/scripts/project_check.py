"""Check GitHub availability for added or modified PR projects."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml


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
        for field in ("name", "description", "category"):
            if field in project and not isinstance(project[field], str):
                raise ValueError(f"{prefix}: {field} must be a string")
        projects[key] = project
    return projects


def select_projects(base_path, head_path):
    """Compare full entries, ignoring removals and YAML formatting changes."""
    base = load_projects(base_path)
    head = load_projects(head_path)
    return [project for key, project in head.items() if base.get(key) != project]


def check_repository(github_id, token):
    request = Request(
        f"https://api.github.com/repos/{github_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-project-check",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError(f"unexpected HTTP status {response.status}")
            repository = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        raise ValueError(f"{github_id}: GitHub API request failed: {exc}") from exc
    if (
        not isinstance(repository, dict)
        or repository.get("private") is not False
        or not isinstance(repository.get("full_name"), str)
        or not GITHUB_ID.fullmatch(repository["full_name"])
    ):
        raise ValueError(f"{github_id}: expected a public GitHub repository")


def check_projects(base_path, head_path, token):
    selected = select_projects(base_path, head_path)
    if selected and not token:
        raise ValueError("GH_TOKEN is required to check repositories")
    for project in selected:
        if project.get("name") != project["github_id"]:
            raise ValueError(f"{project['github_id']}: name must exactly equal github_id")
        check_repository(project["github_id"], token)
    print(f"GitHub availability check passed for {len(selected)} added/modified projects.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_yaml")
    parser.add_argument("head_yaml")
    args = parser.parse_args()
    try:
        check_projects(args.base_yaml, args.head_yaml, os.environ.get("GH_TOKEN"))
    except (OSError, UnicodeError, ValueError) as exc:
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
