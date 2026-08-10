"""Tests for the kbagent-promotion-pipeline skill's generator script.

Imported by path since the script lives under plugins/, not src/.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT = (
    Path(__file__).parent.parent
    / "plugins/kbagent/skills/kbagent-promotion-pipeline/scripts/generate_promotion_pipeline.py"
)
_spec = importlib.util.spec_from_file_location("generate_promotion_pipeline", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

Pipeline = _mod.Pipeline
gen_pull = _mod.gen_pull
gen_push = _mod.gen_push
gen_validate = _mod.gen_validate
_validate_pipelines = _mod._validate_pipelines


def _parse_yaml(generated: str) -> dict:
    """Generated workflows carry an unresolved @@INSTALL@@ placeholder until
    `run()` substitutes real install steps -- fill in a stub so the YAML parses."""
    return yaml.safe_load(generated.replace(_mod._INSTALL_TOKEN, "      - run: 'noop'\n"))


def _pipeline(name: str = "SALESFORCE", directory: str = "salesforce") -> Pipeline:
    return Pipeline(
        name=name,
        directory=directory,
        source_stack_url="https://connection.keboola.com",
        dest_stack_url="https://connection.keboola.com",
    )


class TestPullMechanic:
    def test_pulls_into_scratch_not_tracked_directory(self) -> None:
        yml = gen_pull([_pipeline()], schedule=None, main_branch="main")
        assert "/tmp/promote-scratch/salesforce" in yml
        assert "--directory /tmp/promote-scratch/salesforce --force" in yml

    def test_merge_step_preserves_keboola_manifest(self) -> None:
        yml = gen_pull([_pipeline()], schedule=None, main_branch="main")
        assert yml.count("if item.name == '.keboola'") == 2

    def test_merge_step_does_not_follow_symlinks(self) -> None:
        yml = gen_pull([_pipeline()], schedule=None, main_branch="main")
        assert "item.is_symlink()" in yml
        assert "refusing to copy symlink" in yml

    def test_pr_uses_pat_not_default_token(self) -> None:
        yml = gen_pull([_pipeline()], schedule=None, main_branch="main")
        assert "token: ${{ secrets.PROMOTION_PR_TOKEN }}" in yml

    def test_generated_yaml_is_valid(self) -> None:
        yml = gen_pull([_pipeline()], schedule=None, main_branch="main")
        parsed = _parse_yaml(yml)
        assert "pull" in parsed["jobs"]


class TestPushIsolation:
    def test_one_job_per_pipeline(self) -> None:
        pipelines = [_pipeline("SALESFORCE", "salesforce"), _pipeline("GA4", "ga4")]
        parsed = _parse_yaml(gen_push(pipelines, main_branch="main"))
        assert set(parsed["jobs"]) == {"push_salesforce", "push_ga4"}

    def test_every_job_has_its_own_prod_environment(self) -> None:
        pipelines = [_pipeline("SALESFORCE", "salesforce"), _pipeline("GA4", "ga4")]
        parsed = _parse_yaml(gen_push(pipelines, main_branch="main"))
        assert all(job["environment"] == "prod" for job in parsed["jobs"].values())

    def test_jobs_have_no_needs_dependency_so_one_failure_does_not_block_others(self) -> None:
        pipelines = [_pipeline("SALESFORCE", "salesforce"), _pipeline("GA4", "ga4")]
        parsed = _parse_yaml(gen_push(pipelines, main_branch="main"))
        assert all("needs" not in job for job in parsed["jobs"].values())


class TestValidate:
    def test_generated_yaml_is_valid(self) -> None:
        parsed = _parse_yaml(gen_validate([_pipeline()]))
        assert "validate" in parsed["jobs"]


class TestValidatePipelines:
    def test_rejects_path_traversal_directory(self) -> None:
        with pytest.raises(ValueError, match="unsafe directory"):
            _validate_pipelines([_pipeline(directory="../etc")])

    def test_rejects_quote_in_stack_url(self) -> None:
        p = _pipeline()
        p.source_stack_url = "https://connection.keboola.com'; rm -rf /"
        with pytest.raises(ValueError, match="unsafe source_stack_url"):
            _validate_pipelines([p])

    def test_rejects_unsafe_name(self) -> None:
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_pipelines([_pipeline(name="sales'; rm -rf /")])

    def test_rejects_directory_collision(self) -> None:
        with pytest.raises(ValueError, match="both use directory"):
            _validate_pipelines([_pipeline("SALESFORCE", "shared"), _pipeline("GA4", "shared")])

    def test_rejects_label_collision(self) -> None:
        with pytest.raises(ValueError, match="secret-name label"):
            _validate_pipelines([_pipeline("sales-force", "a"), _pipeline("sales force", "b")])

    def test_accepts_distinct_safe_pipelines(self) -> None:
        _validate_pipelines([_pipeline("SALESFORCE", "salesforce"), _pipeline("GA4", "ga4")])


class TestConfigParsing:
    def test_missing_config_file_exits_2_not_traceback(self, tmp_path, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            _mod.main(["--config", str(tmp_path / "missing.json"), str(tmp_path)])
        assert exc.value.code == 2
        assert "invalid" in capsys.readouterr().err

    def test_invalid_json_exits_2_not_traceback(self, tmp_path, capsys) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _mod.main(["--config", str(bad), str(tmp_path)])
        assert exc.value.code == 2
        assert "invalid" in capsys.readouterr().err

    def test_missing_required_key_exits_2_not_traceback(self, tmp_path, capsys) -> None:
        cfg = tmp_path / "missing_key.json"
        cfg.write_text('[{"name": "X", "directory": "x"}]', encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _mod.main(["--config", str(cfg), str(tmp_path)])
        assert exc.value.code == 2
        assert "missing required key" in capsys.readouterr().err
