"""Dedicated trading wallet (directive §9).

- Generates a fresh ed25519 keypair, saved in Solana CLI format (JSON array of
  64 bytes) with 0600 permissions, optionally encrypted with a passphrase.
- Mario never asks for, imports or stores your main wallet / seed phrase.
- There is no withdraw command in Mario: moving funds out is done by you, in
  your own wallet app, after importing the key.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .util import b58decode, b58encode

ENV_SECRET = "MARIO_WALLET_SECRET"   # base58 private key, for hosts like Render (set as a secret env var)


def _raw_pub(sk: Ed25519PrivateKey) -> bytes:
    return sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass
class Keypair:
    seed: bytes  # 32 bytes

    @classmethod
    def generate(cls) -> "Keypair":
        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        return cls(seed)

    @classmethod
    def from_secret_bytes(cls, b: bytes) -> "Keypair":
        if len(b) == 64:
            kp = cls(b[:32])
            if _raw_pub(kp._sk()) != b[32:]:
                raise ValueError("secret key and public key halves do not match")
            return kp
        if len(b) == 32:
            return cls(b)
        raise ValueError("expected 32 or 64 byte secret")

    def _sk(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.seed)

    @property
    def pubkey_bytes(self) -> bytes:
        return _raw_pub(self._sk())

    @property
    def pubkey(self) -> str:
        return b58encode(self.pubkey_bytes)

    def secret_64(self) -> bytes:
        return self.seed + self.pubkey_bytes

    def secret_b58(self) -> str:
        """Phantom/Solflare 'import private key' format."""
        return b58encode(self.secret_64())

    def sign(self, msg: bytes) -> bytes:
        return self._sk().sign(msg)


def _kdf(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def save_keypair(kp: Keypair, path: str, passphrase: str | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        raise FileExistsError(f"{path} already exists - refusing to overwrite a wallet")
    if passphrase:
        salt, nonce = os.urandom(16), os.urandom(12)
        ct = AESGCM(_kdf(passphrase, salt)).encrypt(nonce, kp.secret_64(), b"mario-wallet")
        body = json.dumps({"encrypted": True, "pubkey": kp.pubkey,
                           "salt": base64.b64encode(salt).decode(),
                           "nonce": base64.b64encode(nonce).decode(),
                           "ct": base64.b64encode(ct).decode()})
    else:
        body = json.dumps(list(kp.secret_64()))
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)


def load_keypair(path: str, passphrase: str | None = None) -> Keypair:
    if os.environ.get(ENV_SECRET):
        return Keypair.from_secret_bytes(b58decode(os.environ[ENV_SECRET].strip()))
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no wallet at {path}. Run: python -m mario wallet new")
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass  # read-only mount
    data = json.loads(p.read_text())
    if isinstance(data, list):
        return Keypair.from_secret_bytes(bytes(data))
    if data.get("encrypted"):
        passphrase = passphrase or os.environ.get("MARIO_WALLET_PASSPHRASE")
        if not passphrase:
            import getpass
            passphrase = getpass.getpass("Wallet passphrase: ")
        key = _kdf(passphrase, base64.b64decode(data["salt"]))
        secret = AESGCM(key).decrypt(base64.b64decode(data["nonce"]), base64.b64decode(data["ct"]), b"mario-wallet")
        return Keypair.from_secret_bytes(secret)
    raise ValueError("unrecognised wallet file")


def wallet_pubkey(path: str) -> str | None:
    if os.environ.get(ENV_SECRET):
        return load_keypair(path).pubkey
    p = Path(path)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    if isinstance(data, list):
        return Keypair.from_secret_bytes(bytes(data)).pubkey
    return data.get("pubkey")
