"""Dedicated trading wallet (directive §9).

- `wallet new` creates a fresh wallet WITH A 12-WORD SEED PHRASE (BIP39), derived on
  Phantom/Solflare's default path m/44'/501'/0'/0', so the same phrase restores the same
  address in Phantom. The phrase is shown once at creation; write it down offline.
- Saved with 0600 permissions, optionally encrypted with a passphrase (AES-GCM + scrypt).
- Mario never asks for, imports or stores your MAIN wallet / seed phrase.
- Withdrawals only go to addresses you allow-list (see withdraw.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .util import b58decode, b58encode

ENV_SECRET = "MARIO_WALLET_SECRET"   # base58 private key, for hosts like Render (set as a secret env var)
PHANTOM_PATH = "m/44'/501'/0'/0'"


def _raw_pub(sk: Ed25519PrivateKey) -> bytes:
    return sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ------------------------------------------------------------------ seed phrase (BIP39 + SLIP-0010)
def _wordlist() -> list[str]:
    try:
        from mnemonic import Mnemonic
    except ImportError as e:
        raise RuntimeError("seed phrases need the `mnemonic` package: pip install mnemonic") from e
    return Mnemonic("english").wordlist


def entropy_to_mnemonic(entropy: bytes, words: list[str]) -> str:
    if len(entropy) not in (16, 20, 24, 28, 32) or len(words) != 2048:
        raise ValueError("bad entropy or wordlist")
    bits = bin(int.from_bytes(entropy, "big"))[2:].zfill(len(entropy) * 8)
    cs = bin(hashlib.sha256(entropy).digest()[0])[2:].zfill(8)[: len(entropy) * 8 // 32]
    b = bits + cs
    return " ".join(words[int(b[i:i + 11], 2)] for i in range(0, len(b), 11))


def mnemonic_is_valid(phrase: str, words: list[str]) -> bool:
    parts = phrase.split()
    if len(parts) not in (12, 15, 18, 21, 24) or any(w not in words for w in parts):
        return False
    b = "".join(bin(words.index(w))[2:].zfill(11) for w in parts)
    ent_len = len(b) * 32 // 33
    entropy = int(b[:ent_len], 2).to_bytes(ent_len // 8, "big")
    cs = bin(hashlib.sha256(entropy).digest()[0])[2:].zfill(8)[: ent_len // 32]
    return b[ent_len:] == cs


def mnemonic_to_seed(phrase: str, passphrase: str = "") -> bytes:
    m = unicodedata.normalize("NFKD", " ".join(phrase.split()))
    salt = unicodedata.normalize("NFKD", "mnemonic" + passphrase)
    return hashlib.pbkdf2_hmac("sha512", m.encode(), salt.encode(), 2048, 64)


def slip10_ed25519(seed: bytes, path: str) -> bytes:
    """SLIP-0010 ed25519 derivation (hardened only) -> 32-byte private key."""
    I = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    k, c = I[:32], I[32:]
    for part in path.split("/")[1:]:
        if not part.endswith("'"):
            raise ValueError("ed25519 supports hardened paths only")
        idx = int(part[:-1]) + 0x80000000
        I = hmac.new(c, b"\x00" + k + idx.to_bytes(4, "big"), hashlib.sha512).digest()
        k, c = I[:32], I[32:]
    return k


# ------------------------------------------------------------------ keypair
@dataclass
class Keypair:
    seed: bytes  # 32-byte ed25519 private key
    mnemonic: str | None = None
    path: str | None = None

    @classmethod
    def generate(cls) -> "Keypair":
        sk = Ed25519PrivateKey.generate()
        return cls(sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption()))

    @classmethod
    def generate_with_mnemonic(cls, words: int = 12) -> "Keypair":
        phrase = entropy_to_mnemonic(os.urandom(16 if words == 12 else 32), _wordlist())
        return cls.from_mnemonic(phrase)

    @classmethod
    def from_mnemonic(cls, phrase: str, path: str = PHANTOM_PATH) -> "Keypair":
        phrase = " ".join(phrase.lower().split())
        return cls(slip10_ed25519(mnemonic_to_seed(phrase), path), phrase, path)

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


# ------------------------------------------------------------------ storage
def _kdf(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def _payload(kp: Keypair) -> dict:
    return {"secret_b58": kp.secret_b58(), "mnemonic": kp.mnemonic, "path": kp.path}


def save_keypair(kp: Keypair, path: str, passphrase: str | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        raise FileExistsError(f"{path} already exists - refusing to overwrite a wallet")
    if passphrase:
        salt, nonce = os.urandom(16), os.urandom(12)
        ct = AESGCM(_kdf(passphrase, salt)).encrypt(nonce, json.dumps(_payload(kp)).encode(), b"mario-wallet-v2")
        body = {"version": 2, "encrypted": True, "pubkey": kp.pubkey,
                "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
                "ct": base64.b64encode(ct).decode()}
    else:
        body = {"version": 2, "encrypted": False, "pubkey": kp.pubkey, **_payload(kp)}
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(body, indent=1))


def _from_payload(d: dict) -> Keypair:
    kp = Keypair.from_secret_bytes(b58decode(d["secret_b58"]))
    kp.mnemonic, kp.path = d.get("mnemonic"), d.get("path")
    return kp


def load_keypair(path: str, passphrase: str | None = None) -> Keypair:
    if os.environ.get(ENV_SECRET):
        return Keypair.from_secret_bytes(b58decode(os.environ[ENV_SECRET].strip()))
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no wallet at {path}. Run: python -m mario wallet new")
    if p.stat().st_mode & 0o077:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass  # read-only mount
    data = json.loads(p.read_text())
    if isinstance(data, list):                         # v1: Solana CLI style array
        return Keypair.from_secret_bytes(bytes(data))
    if not data.get("encrypted"):
        return _from_payload(data)
    passphrase = passphrase or os.environ.get("MARIO_WALLET_PASSPHRASE")
    if not passphrase:
        import getpass
        passphrase = getpass.getpass("Wallet passphrase: ")
    key = _kdf(passphrase, base64.b64decode(data["salt"]))
    nonce, ct = base64.b64decode(data["nonce"]), base64.b64decode(data["ct"])
    if data.get("version") == 2:
        return _from_payload(json.loads(AESGCM(key).decrypt(nonce, ct, b"mario-wallet-v2")))
    return Keypair.from_secret_bytes(AESGCM(key).decrypt(nonce, ct, b"mario-wallet"))


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
