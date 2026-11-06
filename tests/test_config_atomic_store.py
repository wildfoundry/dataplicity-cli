import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dataplicity_cli.config import Config
from dataplicity_cli.config_store import ConfigError, ConfigChangedError, atomic_write, locked_config


def test_lock_file_symlink_and_nonregular_file_are_rejected(tmp_path):
    path = tmp_path/'cli.json'
    target = tmp_path/'private'; target.write_bytes(b'untouched')
    if os.name != 'nt':
        path.with_name(path.name+'.lock').symlink_to(target)
        with pytest.raises(ConfigError, match='unavailable'):
            with locked_config(path): pytest.fail('symlink lock admitted')
        assert target.read_bytes() == b'untouched'
        path.with_name(path.name+'.lock').unlink()
    else:
        with patch.object(Path, 'is_symlink', return_value=True), pytest.raises(ConfigError):
            with locked_config(path): pytest.fail('symlink lock admitted')
    with patch('dataplicity_cli.config_store.stat.S_ISREG', return_value=False), pytest.raises(ConfigError):
        with locked_config(path): pytest.fail('nonregular lock admitted')


def test_windows_lock_uses_one_persistent_byte_and_unlocks_the_same_offset(tmp_path):
    calls = []
    def locking(fd, operation, size): calls.append((operation, size, os.lseek(fd, 0, os.SEEK_CUR)))
    fake = SimpleNamespace(locking=locking, LK_NBLCK=2, LK_UNLCK=0)
    path = tmp_path/'cli.json'
    with patch.dict(sys.modules, {'msvcrt': fake}), patch('dataplicity_cli.config_store.WINDOWS', True):
        with locked_config(path): atomic_write(path, {'complete': True})
        with locked_config(path): pass
    assert calls == [(2,1,0),(0,1,0),(2,1,0),(0,1,0)]
    assert path.with_name(path.name+'.lock').read_bytes() == b'0'
    assert json.loads(path.read_text()) == {'complete': True}


def test_config_private_modes_and_unknown_metadata_survive_updates(tmp_path):
    path = tmp_path/'cli.json'
    original = {'auth_method':'jwt','access_token':'access','refresh_token':'refresh',
        'local_session_id':'invalid-uuid','extension':{'opaque':['exact',None,{'permission':'customer-value'}]}}
    path.write_text(json.dumps(original))
    config = Config.load(path)
    assert config.local_session_id == Config.load(path).local_session_id
    config.last_email = 'new@example.test'; config.save(path)
    assert json.loads(path.read_text())['extension'] == original['extension']
    if os.name != 'nt': assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('[]')
    assert Config.load(path).access_token is None


def test_read_failure_is_safe_and_never_replaces_the_file(tmp_path):
    path = tmp_path/'cli.json'; path.write_text('original')
    with patch.object(Path, 'read_bytes', side_effect=PermissionError('private-token-value')):
        with pytest.raises(ConfigError) as raised: Config.load(path)
    assert 'private-token-value' not in str(raised.value)
    assert path.read_text() == 'original'


def test_unwitnessed_overwrite_and_file_removal_cannot_restore_old_authentication(tmp_path):
    path = tmp_path/'cli.json'; Config(access_token='original').save(path)
    with pytest.raises(ConfigChangedError): Config(access_token='unwitnessed').save(path)
    old = Config.load(path); path.unlink()
    with pytest.raises(ConfigChangedError): old.save(path)
    assert not path.exists()
