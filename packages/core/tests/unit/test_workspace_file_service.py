"""Unit tests for workspace file service — path traversal safety."""
from __future__ import annotations

import os
import pytest

from src.services.project_service import (
    _resolve_safe_path,
    list_workspace_files,
    read_workspace_file,
)


def test_resolve_safe_path_returns_root_when_empty(tmp_path):
    result = _resolve_safe_path(str(tmp_path), "")
    assert result == str(tmp_path.resolve())


def test_resolve_safe_path_accepts_nested(tmp_path):
    (tmp_path / "src").mkdir()
    result = _resolve_safe_path(str(tmp_path), "src")
    assert result == str((tmp_path / "src").resolve())


def test_resolve_safe_path_blocks_single_dotdot(tmp_path):
    with pytest.raises(ValueError, match="Path outside workspace root"):
        _resolve_safe_path(str(tmp_path), "..")


def test_resolve_safe_path_blocks_deep_traversal(tmp_path):
    with pytest.raises(ValueError, match="Path outside workspace root"):
        _resolve_safe_path(str(tmp_path), "../../etc/passwd")


def test_resolve_safe_path_blocks_embedded_dotdot(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(ValueError, match="Path outside workspace root"):
        _resolve_safe_path(str(tmp_path), "src/../../etc/passwd")


def test_list_workspace_files_returns_sorted_dirs_first(tmp_path):
    (tmp_path / "zebra.py").write_text("")
    (tmp_path / "alpha").mkdir()
    entries = list_workspace_files(str(tmp_path))
    assert entries[0]["is_dir"] is True
    assert entries[0]["name"] == "alpha"
    assert entries[1]["name"] == "zebra.py"


def test_list_workspace_files_includes_size_for_files(tmp_path):
    (tmp_path / "data.txt").write_text("hello")
    entries = list_workspace_files(str(tmp_path))
    assert entries[0]["size"] == 5


def test_list_workspace_files_size_is_none_for_dirs(tmp_path):
    (tmp_path / "mydir").mkdir()
    entries = list_workspace_files(str(tmp_path))
    assert entries[0]["is_dir"] is True
    assert entries[0]["size"] is None


def test_list_workspace_files_path_uses_forward_slashes(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.py").write_text("")
    entries = list_workspace_files(str(tmp_path), "sub")
    assert "/" in entries[0]["path"] or os.sep not in entries[0]["path"]
    assert "\\" not in entries[0]["path"]


def test_list_workspace_files_blocks_traversal(tmp_path):
    with pytest.raises(ValueError):
        list_workspace_files(str(tmp_path), "../../etc")


def test_read_workspace_file_returns_content(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')")
    content = read_workspace_file(str(tmp_path), "hello.py")
    assert content == "print('hi')"


def test_read_workspace_file_nested(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "utils.py").write_text("pass")
    content = read_workspace_file(str(tmp_path), "src/utils.py")
    assert content == "pass"


def test_read_workspace_file_blocks_traversal(tmp_path):
    with pytest.raises(ValueError):
        read_workspace_file(str(tmp_path), "../secret.txt")


def test_read_workspace_file_raises_on_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_workspace_file(str(tmp_path), "nonexistent.py")
