"""Kalshi request signing (matches the official SDK `KalshiAuth`).

Signature = base64(sign(timestamp_ms + METHOD + path_without_query)).
RSA keys use RSA-PSS/SHA-256 with salt length = digest length; Ed25519 keys sign directly.
Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


class SignerError(RuntimeError):
    pass


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes) -> None:
        if not key_id:
            raise SignerError("API key id is empty")
        try:
            key = serialization.load_pem_private_key(private_key_pem, password=None)
        except (ValueError, TypeError) as exc:
            # Never include key material in the error.
            raise SignerError("private key could not be loaded (expected unencrypted PEM)") from exc
        if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
            raise SignerError("private key must be RSA or Ed25519")
        self.key_id = key_id
        self._key = key

    @classmethod
    def from_file(cls, key_id: str, path: Path) -> KalshiSigner:
        try:
            pem = path.read_bytes()
        except OSError as exc:
            raise SignerError(f"cannot read private key file ({exc.__class__.__name__})") from exc
        return cls(key_id, pem)

    def _sign(self, message: bytes) -> bytes:
        if isinstance(self._key, Ed25519PrivateKey):
            return self._key.sign(message)
        return self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method: str, path: str, *, now_ms: int | None = None) -> dict[str, str]:
        ts = str(int(time.time() * 1000) if now_ms is None else now_ms)
        path_only = path.split("?", 1)[0]
        sig = self._sign((ts + method.upper() + path_only).encode("utf-8"))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
        }

    def __repr__(self) -> str:  # never leak key material
        return f"KalshiSigner(key_id={self.key_id[:4]}…)"
