"""Unit tests for the shared flat {id: scalar} YAML-mapping loader (yaml_input.py).

The loader backs `sync clone --bucket-map/--variable-values/--instance-rename`.
Before it existed, `commands/sync.py` coerced every value with bare
``str(value)``, so a nested mapping (a fat-fingered colon in the YAML) was
silently used as the literal string ``"{'new': 'in.c-new'}"`` -- a wrong
bucket ID in the target project instead of a rejection.
"""

from pathlib import Path

import pytest

from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.yaml_input import load_flat_scalar_mapping, yaml_type_name


class TestYamlTypeName:
    """Type names come out in YAML vocabulary, not Python's."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"a": 1}, "mapping"),
            (["a"], "list"),
            (None, "null"),
            ("x", "string"),
            (42, "number"),
            (4.2, "number"),
            (True, "boolean"),
        ],
    )
    def test_known_types(self, value: object, expected: str) -> None:
        assert yaml_type_name(value) == expected

    def test_unknown_type_falls_back_to_python_name(self) -> None:
        class Weird:
            pass

        assert yaml_type_name(Weird()) == "Weird"


class TestLoadFlatScalarMapping:
    def _write(self, tmp_path: Path, content: str) -> Path:
        path = tmp_path / "map.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_json(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, '{"in.c-old": "in.c-new"}')
        assert load_flat_scalar_mapping(path) == {"in.c-old": "in.c-new"}

    def test_valid_yaml_scalars_coerced_to_str(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "retries: 3\nname: prod\n")
        assert load_flat_scalar_mapping(path) == {"retries": "3", "name": "prod"}

    def test_nested_mapping_value_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "in.c-old:\n  new: in.c-new\n")
        with pytest.raises(ConfigError, match=r"'in\.c-old'.*mapping"):
            load_flat_scalar_mapping(path)

    def test_list_value_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "in.c-old:\n  - in.c-new\n")
        with pytest.raises(ConfigError, match=r"'in\.c-old'.*list"):
            load_flat_scalar_mapping(path)

    def test_null_value_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "in.c-old:\n")
        with pytest.raises(ConfigError, match=r"'in\.c-old'.*null"):
            load_flat_scalar_mapping(path)

    def test_top_level_list_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "- in.c-old\n- in.c-new\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_flat_scalar_mapping(path)

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "")
        with pytest.raises(ConfigError, match="mapping"):
            load_flat_scalar_mapping(path)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_flat_scalar_mapping(tmp_path / "nope.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "key: [unclosed\n")
        with pytest.raises(ConfigError, match=r"[Cc]annot parse"):
            load_flat_scalar_mapping(path)

    def test_label_used_in_messages(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"[Oo]verride file not found"):
            load_flat_scalar_mapping(tmp_path / "nope.yaml", label="override file")
