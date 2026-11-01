"""Encryption of stored credentials (AES-256-GCM).

The key lives in its own file (`secret.key`, mode 0600) next to - but separate from - the database,
or comes from LANLOOM_SECRET_KEY_FILE (for example a container secret). Plaintext secrets are only
held in memory while a source uses them and are never logged or returned by the API.
"""
import os
import secrets as _secrets
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import Store

KEY_BYTES = 32


def load_or_create_key(data_dir: Path) -> bytes:
    override = os.environ.get("LANLOOM_SECRET_KEY_FILE")
    if override:
        raw = Path(override).read_bytes()
        if len(raw) < KEY_BYTES:
            raise RuntimeError("secret key file is too short")
        return raw[:KEY_BYTES]
    path = data_dir / "secret.key"
    try:
        raw = path.read_bytes()
        if len(raw) == KEY_BYTES:
            return raw
    except FileNotFoundError:
        pass
    key = _secrets.token_bytes(KEY_BYTES)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return key


class Vault:
    """Encrypted name -> secret mapping on top of the store."""

    def __init__(self, store: Store, key: bytes):
        self._store = store
        self._aead = AESGCM(key)

    def __repr__(self) -> str:
        return "Vault(<encrypted>)"

    def put(self, name: str, value: str) -> None:
        nonce = _secrets.token_bytes(12)
        ciphertext = self._aead.encrypt(nonce, value.encode("utf-8"), name.encode("utf-8"))
        self._store.execute("INSERT OR REPLACE INTO secrets(name, nonce, ciphertext) VALUES(?,?,?)", (name, nonce, ciphertext))

    def get(self, name: str) -> Optional[str]:
        row = self._store.one("SELECT nonce, ciphertext FROM secrets WHERE name=?", (name,))
        if not row:
            return None
        return self._aead.decrypt(row["nonce"], row["ciphertext"], name.encode("utf-8")).decode("utf-8")

    def has(self, name: str) -> bool:
        return self._store.one("SELECT 1 AS x FROM secrets WHERE name=?", (name,)) is not None

    def delete(self, name: str) -> None:
        self._store.execute("DELETE FROM secrets WHERE name=?", (name,))

    def delete_prefix(self, prefix: str) -> None:
        self._store.execute("DELETE FROM secrets WHERE name LIKE ?", (prefix.replace("%", "") + "%",))
