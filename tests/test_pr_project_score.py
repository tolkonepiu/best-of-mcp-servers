import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))
import pr_project_score as score


def project(github_id="owner/repo", **fields):
    return {"name": "Example", "github_id": github_id, "description": "An example",
            "category": "tools", **fields}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "base.yaml"
        self.head = Path(self.temp.name) / "head.yaml"

    def select(self, base, head):
        self.base.write_text(yaml.safe_dump(base), encoding="utf-8")
        self.head.write_text(yaml.safe_dump(head), encoding="utf-8")
        before = self.head.read_bytes()
        selected = score.select_projects(self.base, self.head)
        self.assertEqual(self.head.read_bytes(), before)
        return selected

    def test_several_additions(self):
        additions = [project("one/repo"), project("two/repo"), project("three/repo")]
        self.assertEqual(self.select({"projects": [project()]},
                                     {"projects": [project(), *additions]}), additions)

    def test_any_entry_field_change_selects(self):
        for field, value in (("npm_id", "@owner/package"), ("description", "Changed"),
                             ("name", "example"), ("category", "other"),
                             ("projectrank", 999), ("star_count", 100000)):
            with self.subTest(field=field):
                changed = project(**{field: value})
                selected = self.select({"projects": [project()]}, {"projects": [changed]})
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0], {
                    k: v for k, v in changed.items() if k in score.PROJECT_FIELDS
                })

    def test_replaced_id_selects_new_id(self):
        replacement = project("other/replacement")
        self.assertEqual(self.select({"projects": [project()]},
                                     {"projects": [replacement]}), [replacement])

    def test_deletions_reordering_and_outer_edits_select_nothing(self):
        first, second = project(), project("other/repo")
        base = {"projects": [first, second], "configuration": {"min_projectrank": 99},
                "categories": [{"category": "old"}]}
        for entries in ([second, first], [second], []):
            with self.subTest(entries=entries):
                self.assertEqual(self.select(base, {"projects": entries,
                                                   "configuration": {"min_projectrank": 0},
                                                   "categories": []}), [])

    def test_case_insensitive_identity_preserves_case_sensitive_field_changes(self):
        self.assertEqual(self.select({"projects": [project()]},
                                     {"projects": [project("OWNER/Repo")]}),
                         [project("OWNER/Repo")])
        changed = project("OWNER/Repo", description="AN EXAMPLE")
        self.assertEqual(self.select({"projects": [project()]},
                                     {"projects": [changed]}), [changed])

    def test_yaml_formatting_is_irrelevant(self):
        self.base.write_text("projects:\n- github_id: owner/repo\n  name: Example\n")
        self.head.write_text('# comment\nprojects: [{name: "Example", github_id: owner/repo}]\n')
        self.assertEqual(score.select_projects(self.base, self.head), [])

    def test_injected_metrics_and_config_are_not_forwarded(self):
        injected = project(projectrank=100, star_count=999999, github_stars=999999,
                           configuration={"min_projectrank": 0}, arbitrary="bad")
        self.assertEqual(self.select({"projects": []}, {"projects": [injected]}),
                         [project()])

    def test_supported_package_identifiers_are_forwarded(self):
        packages = {field: "package" for field in (
            "npm_id", "pypi_id", "conda_id", "dockerhub_id", "maven_id", "cargo_id", "go_id")}
        entry = project(**packages)
        self.assertEqual(self.select({"projects": []}, {"projects": [entry]}), [entry])

    def test_invalid_yaml_and_schema_in_either_file(self):
        invalid = (
            ("projects: [", "cannot read project YAML"),
            ("!!python/object/apply:os.system ['false']", "cannot read project YAML"),
            ("[]", "projects list"),
            ("projects: {}", "projects list"),
            ("projects: [null]", "expected a mapping"),
            ("projects: [{name: Missing}]", "valid owner/repo"),
            ("projects: [{github_id: owner/repo/extra}]", "valid owner/repo"),
            ("projects: [{github_id: 'owner/..'}]", "valid owner/repo"),
            ("projects: [{github_id: 'owner/repo', npm_id: []}]", "npm_id must be a string"),
            ("projects: [{github_id: owner/repo}, {github_id: OWNER/REPO}]", "duplicate"),
        )
        for content, message in invalid:
            for target in (self.base, self.head):
                with self.subTest(content=content, target=target.name):
                    self.base.write_text("projects: []\n")
                    self.head.write_text("projects: []\n")
                    target.write_text(content)
                    with self.assertRaisesRegex(ValueError, message):
                        score.select_projects(self.base, self.head)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "base.yaml"
        self.head = self.root / "head.yaml"
        self.workdir = self.root / "scoring"
        self.summary = self.root / "summary.md"
        self.output = self.root / "github-output"
        self.base.write_text("projects: []\n")
        self.entries = [project("owner/one"), project("owner/two")]
        self.head.write_text(yaml.safe_dump({
            "projects": self.entries,
            "configuration": {"output_file": "/bad", "min_projectrank": 999,
                              "markdown_header_file": "/bad", "extensions": ["bad"]},
        }))
        env = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary),
                                      "GITHUB_OUTPUT": str(self.output)})
        env.start()
        self.addCleanup(env.stop)
        score.prepare(self.base, self.head, self.workdir)

    def csv(self, ranks=("10", "10"), metadata=True):
        rows = []
        for entry, rank in zip(self.entries, ranks):
            row = {"github_id": entry["github_id"], "projectrank": rank}
            if metadata:
                row.update(github_url="https://github.com/" + entry["github_id"],
                           created_at="2020-01-01T00:00:00Z",
                           last_commit_pushed_at="2026-11-22T00:00:00Z")
            rows.append(row)
        return self.write_rows(rows)

    def write_rows(self, rows):
        path = self.workdir / "output" / "history" / "2026_projects.csv"
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def verify(self, minimum="10", outcome="success"):
        with contextlib.redirect_stdout(io.StringIO()):
            result = score.verify(self.workdir, minimum, outcome)
        return result, self.summary.read_text()

    def test_prepare_is_isolated_and_uses_only_trusted_configuration(self):
        config = yaml.safe_load((self.workdir / "projects.yaml").read_text())
        self.assertEqual(config["projects"], self.entries)
        self.assertEqual(config["configuration"], {
            "output_file": str(self.workdir / "output" / "README.md"),
            "projects_history_folder": str(self.workdir / "output" / "history"),
            "min_projectrank": 0, "min_stars": 0, "require_license": False,
            "require_repo": False, "min_description_length": 0,
            "generate_install_hints": False,
        })
        self.assertEqual(list((self.workdir / "output" / "history").iterdir()), [])
        manifest = json.loads((self.workdir / "selected.json").read_text())
        self.assertEqual(manifest["github_ids"], ["owner/one", "owner/two"])
        self.assertGreater(manifest["started_at_ns"], 0)
        self.assertEqual(self.output.read_text(), "count=2\n")
        self.assertEqual(yaml.safe_load(self.head.read_text())["configuration"]["output_file"], "/bad")
        with self.assertRaisesRegex(ValueError, "do not reuse"):
            score.prepare(self.base, self.head, self.workdir)

    def test_zero_selection_has_explicit_summary(self):
        self.head.write_text("projects: []\n")
        with contextlib.redirect_stdout(io.StringIO()):
            score.prepare(self.base, self.head, self.root / "empty")
        self.assertIn("No added or modified projects", self.summary.read_text())
        self.assertIn("count=0\n", self.output.read_text())

    def test_boundary_and_complete_batch_summary(self):
        self.csv(("9", "10"))
        result, summary = self.verify()
        self.assertEqual(result, 1)
        self.assertIn("| owner/one | 9 | 10 | FAIL: below minimum |", summary)
        self.assertIn("| owner/two | 10 | 10 | PASS |", summary)

    def test_equal_threshold_passes_without_star_count(self):
        self.csv()
        self.assertEqual(self.verify()[0], 0)

    def test_zero_rank_is_valid(self):
        self.csv(("0", "0"))
        self.assertEqual(self.verify(minimum="0")[0], 0)

    def test_invalid_ranks_are_not_threshold_failures(self):
        for value in ("NaN", "Infinity", "-Infinity", "", "abc", "-1"):
            with self.subTest(value=value):
                self.csv((value, "10"))
                result, summary = self.verify()
                self.assertEqual(result, 1)
                self.assertIn("expected a finite nonnegative number", summary)

    def test_invalid_minimum(self):
        for value in ("NaN", "Infinity", "-1", "abc", ""):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "minimum projectrank"):
                    self.verify(minimum=value)

    def test_missing_metadata_is_distinct_from_low_score(self):
        self.csv(("1", "10"), metadata=False)
        result, summary = self.verify()
        self.assertEqual(result, 1)
        self.assertIn("missing GitHub metadata", summary)
        self.assertNotIn("below minimum", summary)

    def test_each_core_metadata_field_is_required(self):
        for missing in ("github_url", "created_at", "last_commit_pushed_at"):
            with self.subTest(missing=missing):
                path = self.csv()
                with path.open(newline="") as source:
                    rows = list(csv.DictReader(source))
                rows[0][missing] = " "
                self.write_rows(rows)
                result, summary = self.verify()
                self.assertEqual(result, 1)
                self.assertIn("missing GitHub metadata: " + missing, summary)

    def test_missing_row_and_duplicate_matching_rows(self):
        self.csv(("10",))
        self.assertIn("owner/two: ERROR: missing scoring row", self.verify()[1])
        row = {"github_id": "owner/one", "projectrank": "10"}
        self.write_rows([row, {**row, "github_id": "OWNER/ONE"}])
        self.assertIn("owner/one: ERROR: duplicate scoring rows", self.verify()[1])

    def test_case_insensitive_results_and_headers(self):
        self.write_rows([
            {"GITHUB_ID": entry["github_id"].upper(), "PROJECTRANK": "10",
             "GITHUB_URL": "https://github.com/" + entry["github_id"],
             "CREATED_AT": "2020-01-01", "LAST_COMMIT_PUSHED_AT": "2026-11-22"}
            for entry in self.entries
        ])
        self.assertEqual(self.verify()[0], 0)

    def test_missing_stale_and_multiple_csvs(self):
        result, summary = self.verify()
        self.assertEqual(result, 1)
        self.assertIn("exactly one fresh", summary)
        path = self.csv()
        os.utime(path, ns=(1, 1))
        self.assertIn("stale scoring CSV", self.verify()[1])
        self.csv()
        (path.parent / "other_projects.csv").write_text(path.read_text())
        self.assertIn("found 2", self.verify()[1])

    def test_generator_failure_even_with_valid_csv(self):
        self.csv()
        result, summary = self.verify(outcome="failure")
        self.assertEqual(result, 1)
        self.assertIn("Generator error", summary)
        self.assertIn("| owner/one | 10 | 10 | PASS |", summary)

    def test_verify_uses_manifest_not_source_yaml(self):
        self.csv()
        self.head.unlink()
        self.base.unlink()
        self.assertEqual(self.verify()[0], 0)

    def test_malformed_csv_is_clear(self):
        path = self.csv()
        path.write_text("other,projectrank\nowner/one,10\n")
        self.assertIn("CSV needs unique github_id and projectrank", self.verify()[1])

    def test_cli_error_is_escaped(self):
        script = Path(score.__file__)
        result = subprocess.run(
            [sys.executable, str(script), "prepare", str(self.root / "missing%\n.yaml"),
             str(self.head), str(self.root / "cli")], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::", result.stderr)
        self.assertIn("%25%0A", result.stderr)

    def test_cli_prepare_and_verify(self):
        script = Path(score.__file__)
        workdir = self.root / "cli"
        result = subprocess.run(
            [sys.executable, str(script), "prepare", str(self.base), str(self.head),
             str(workdir)], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.csv()
        (workdir / "output" / "history" / source.name).write_bytes(source.read_bytes())
        result = subprocess.run(
            [sys.executable, str(script), "verify", str(workdir), "--minimum", "10",
             "--generator-outcome", "success"], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| owner/two | 10 | 10 | PASS |", result.stdout)


class WorkflowPreparationTests(unittest.TestCase):
    def test_actual_prepare_step_uses_merge_base_and_isolated_workdir(self):
        source_root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load(
            (source_root / ".github" / "workflows" / "pr-project-score.yml").read_text()
        )
        run_block = next(step["run"] for step in workflow["jobs"]["pr-project-score"]["steps"]
                         if step.get("name") == "Prepare changed projects")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, origin, runner_temp = root / "repo", root / "origin.git", root / "runner"
            repo.mkdir()
            runner_temp.mkdir()
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "RUNNER_TEMP": str(runner_temp),
                   "GITHUB_OUTPUT": str(root / "github-output"),
                   "GITHUB_STEP_SUMMARY": str(root / "summary.md")}

            def git(*args):
                result = subprocess.run(["git", *args], cwd=repo, env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            def commit(entry, message):
                (repo / "projects.yaml").write_text(yaml.safe_dump({"projects": [entry]}))
                git("add", "projects.yaml")
                git("commit", "-m", message)
                return git("rev-parse", "HEAD")

            git("init")
            git("config", "user.name", "Offline test")
            git("config", "user.email", "offline@example.invalid")
            ancestor = commit(project(), "Common ancestor")
            git("checkout", "-b", "pr-head", ancestor)
            head = commit(project(npm_id="package", projectrank=999), "PR package change")
            git("checkout", "-b", "base-tip", ancestor)
            base = commit(project(npm_id="package", projectrank=999), "Independent base change")
            self.assertNotEqual(base, head)
            self.assertEqual(git("show", f"{base}:projects.yaml"),
                             git("show", f"{head}:projects.yaml"))
            git("init", "--bare", str(origin))
            git("remote", "add", "origin", str(origin))
            git("push", "origin", "pr-head", "base-tip")
            helper = repo / ".github" / "scripts" / "pr_project_score.py"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(Path(score.__file__).read_bytes())
            original = (repo / "projects.yaml").read_bytes()

            for head_sha, count in ((head, "1"), (ancestor, "0")):
                with self.subTest(count=count):
                    Path(env["GITHUB_OUTPUT"]).write_text("")
                    result = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c", run_block], cwd=repo,
                        env={**env, "BASE_SHA": base, "HEAD_SHA": head_sha},
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    outputs = dict(line.split("=", 1) for line in
                                   Path(env["GITHUB_OUTPUT"]).read_text().splitlines())
                    self.assertEqual(outputs["count"], count)
                    self.assertFalse(Path(outputs["workdir"]).is_absolute())
                    workdir = repo / outputs["workdir"]
                    prepared = yaml.safe_load((workdir / "projects.yaml").read_text())
                    expected = [project(npm_id="package")] if count == "1" else []
                    self.assertEqual(prepared["projects"], expected)
                    config = prepared["configuration"]
                    self.assertEqual(config["output_file"],
                                     outputs["workdir"] + "/output/README.md")
                    self.assertEqual(config["projects_history_folder"],
                                     outputs["workdir"] + "/output/history")
                    self.assertEqual(list((workdir / "output" / "history").iterdir()), [])
                    self.assertFalse((workdir / "base.yaml").exists())
                    self.assertFalse((workdir / "head.yaml").exists())
                    self.assertEqual((repo / "projects.yaml").read_bytes(), original)
                    if count == "0":
                        self.assertIn("No added or modified projects",
                                      Path(env["GITHUB_STEP_SUMMARY"]).read_text())


if __name__ == "__main__":
    unittest.main()
