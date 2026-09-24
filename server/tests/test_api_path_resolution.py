"""
Unit tests for api.py's _resolve_host_path - invariant #5 in CLAUDE.md:
"never accept a client-supplied path without validating it stays under the
caller's own library root." This is the one check standing between a
client-supplied path and the filesystem, so it gets its own direct test
rather than relying on exercising it indirectly through a running server.
"""

import pytest
from fastapi import HTTPException

import api
import config


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "alice-photos"
    root.mkdir()
    (root / "2026-01-01-New-Year").mkdir()
    (root / "2026-01-01-New-Year" / "photo.jpg").write_bytes(b"fake")
    return config.Library(root)


def test_resolves_path_inside_library(library):
    resolved = api._resolve_host_path(library, "2026-01-01-New-Year/photo.jpg")
    assert resolved == library.photos_root / "2026-01-01-New-Year" / "photo.jpg"


def test_rejects_parent_traversal(library):
    with pytest.raises(HTTPException) as exc:
        api._resolve_host_path(library, "../bob-photos/photo.jpg")
    assert exc.value.status_code == 400


def test_rejects_absolute_path_escaping_root(library):
    with pytest.raises(HTTPException) as exc:
        api._resolve_host_path(library, "/etc/passwd")
    assert exc.value.status_code == 400


def test_rejects_traversal_disguised_with_real_subdir(library):
    # A path that starts inside the library but walks back out - the case
    # a naive "startswith" string check (rather than resolve()) would miss.
    with pytest.raises(HTTPException) as exc:
        api._resolve_host_path(library, "2026-01-01-New-Year/../../etc/passwd")
    assert exc.value.status_code == 400
