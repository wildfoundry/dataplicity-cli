"""Qualification must fail closed when actual released-agent evidence is absent."""
import asyncio
from pathlib import Path

import pytest

from qualification.staging_acceptance import _start_legacy, qualify_named_and_legacy


def test_live_acceptance_rejects_non_staging_before_any_fixture_effects():
    with pytest.raises(RuntimeError, match='only targets staging'):
        asyncio.run(qualify_named_and_legacy({'api_url': 'https://api.dataplicity.com'}))


def test_missing_actual_agent_is_failure(monkeypatch, tmp_path):
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_ROOT', str(tmp_path))
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_SHA', 'a' * 40)
    with pytest.raises(RuntimeError, match='source is missing'):
        asyncio.run(_start_legacy({}, 12345, {}))


def test_agent_revision_must_match_qualification_pin(monkeypatch, tmp_path):
    source = tmp_path / 'dataplicity'
    source.mkdir()
    (source / 'client.py').write_text('# actual agent would be installed here')
    (tmp_path / 'REVISION').write_text('b' * 40)
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_ROOT', str(tmp_path))
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_SHA', 'a' * 40)
    with pytest.raises(RuntimeError, match='immutable qualification pin'):
        asyncio.run(_start_legacy({}, 12345, {}))


def test_alpha_agent_cannot_satisfy_legacy_release_gate(monkeypatch, tmp_path):
    source = tmp_path / 'dataplicity'
    source.mkdir()
    (source / 'client.py').write_text('# alpha agent would be installed here')
    (source / '_version.py').write_text('__version__ = "0.5.13a3"')
    (tmp_path / 'REVISION').write_text('a' * 40)
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_ROOT', str(tmp_path))
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_SHA', 'a' * 40)
    with pytest.raises(RuntimeError, match='stable released version'):
        asyncio.run(_start_legacy({}, 12345, {}))
