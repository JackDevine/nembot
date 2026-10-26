"""Where model credentials and setup choices live.

A TOML file only the user can read, ``~/.config/nembot/credentials.toml`` (or
``$NEMBOT_CONFIG_DIR``), written by the onboarding page::

    [settings]
    claude_plan = false   # offer Claude through your Claude plan (see subscription.py)
    keyring = false       # keep API keys in the OS keychain instead of this file

    [providers.openrouter]
    api_key = "sk-or-..."

    [providers.ollama]
    base_url = "http://127.0.0.1:11434/v1"

Environment variables (``OPENROUTER_API_KEY`` etc.) take priority over stored
keys. With ``keyring = true`` keys go to the OS keychain (the optional
``keyring`` package) and the file only records that a key is stored there.
"""
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import tomllib

KEYRING_SERVICE = 'nembot'
_lock = threading.Lock()


def config_dir() -> Path:
    if os.environ.get('NEMBOT_CONFIG_DIR'):
        return Path(os.environ['NEMBOT_CONFIG_DIR'])
    base = os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'
    return Path(base) / 'nembot'


def path() -> Path:
    return config_dir() / 'credentials.toml'


def load() -> dict:
    try:
        with open(path(), 'rb') as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    data.setdefault('settings', {})
    data.setdefault('providers', {})
    return data


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


def dumps(data: dict) -> str:
    lines = []
    settings = data.get('settings') or {}
    if settings:
        lines.append('[settings]')
        lines += [f'{key} = {_toml_value(value)}' for key, value in sorted(settings.items())]
    for name, fields in sorted((data.get('providers') or {}).items()):
        if fields:
            table = name if re.fullmatch(r'[A-Za-z0-9_-]+', name) else json.dumps(name)
            lines += ['', f'[providers.{table}]']
            lines += [f'{key} = {_toml_value(value)}' for key, value in sorted(fields.items())]
    return '\n'.join(lines).lstrip('\n') + '\n'


def save(data: dict) -> None:
    """Write atomically, readable only by the user."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.credentials-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(dumps(data))
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def keyring_available() -> bool:
    return _keyring() is not None


def setting(name: str, default=None):
    return load()['settings'].get(name, default)


def set_setting(name: str, value) -> None:
    with _lock:
        data = load()
        data['settings'][name] = value
        save(data)


def provider(name: str) -> dict:
    return dict(load()['providers'].get(name) or {})


def api_key(name: str, env: str | None = None) -> tuple[str | None, str | None]:
    """(key, source): the environment variable first, then the keychain or the file."""
    if env and os.environ.get(env):
        return os.environ[env], 'environment'
    fields = load()['providers'].get(name) or {}
    if fields.get('key_in_keyring') and (kr := _keyring()):
        key = kr.get_password(KEYRING_SERVICE, name)
        if key:
            return key, 'keychain'
    if fields.get('api_key'):
        return fields['api_key'], 'file'
    return None, None


def save_provider(name: str, *, api_key: str | None = None, **fields) -> None:
    """Store a provider's key and settings (``None`` values are removed)."""
    with _lock:
        data = load()
        entry = dict(data['providers'].get(name) or {})
        for key, value in fields.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        if api_key is not None:
            entry.pop('api_key', None)
            entry.pop('key_in_keyring', None)
            if data['settings'].get('keyring') and (kr := _keyring()):
                kr.set_password(KEYRING_SERVICE, name, api_key)
                entry['key_in_keyring'] = True
            else:
                entry['api_key'] = api_key
        data['providers'][name] = entry
        save(data)


def forget_provider(name: str) -> None:
    with _lock:
        data = load()
        entry = data['providers'].pop(name, None) or {}
        if entry.get('key_in_keyring') and (kr := _keyring()):
            try:
                kr.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        save(data)


def use_keyring(on: bool) -> None:
    """Switch key storage, moving the stored keys between this file and the OS keychain."""
    kr = _keyring()
    if on and kr is None:
        raise RuntimeError('The keychain needs the optional keyring package: uv sync --extra keyring')
    with _lock:
        data = load()
        for name, entry in data['providers'].items():
            if on and entry.get('api_key'):
                kr.set_password(KEYRING_SERVICE, name, entry.pop('api_key'))
                entry['key_in_keyring'] = True
            elif not on and entry.get('key_in_keyring') and kr is not None:
                key = kr.get_password(KEYRING_SERVICE, name)
                entry.pop('key_in_keyring')
                if key:
                    entry['api_key'] = key
                    kr.delete_password(KEYRING_SERVICE, name)
        data['settings']['keyring'] = on
        save(data)
