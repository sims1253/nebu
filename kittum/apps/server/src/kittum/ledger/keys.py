"""Ed25519 keys for checkpoints and bundle manifests.

The signed message for a checkpoint is

    SHA-256("kittum.checkpoint.v1" NUL canonical_json({project_id, seq, head_hash, key_id}))

and a key's ID is the SHA-256 of its raw 32-byte public key, so anyone can compare a bundle's key
against a published fingerprint. Create a key with `openssl genpkey -algorithm ed25519 -out key.pem`
and point KITTUM_CHECKPOINT_KEY at it. Keep it away from the database administrator.
"""

from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from kittum.core import Id, content_hash, sha256_hex
from kittum.ledger.model import Checkpoint

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


def checkpoint_message(*, project_id: Id, seq: int, head_hash: str, key_id: str) -> bytes:
    digest = content_hash(
        {"project_id": str(project_id), "seq": seq, "head_hash": head_hash, "key_id": key_id},
        domain="kittum.checkpoint.v1",
    )
    return bytes.fromhex(digest)


def document_message(document: dict[str, Any], *, domain: str) -> bytes:
    return bytes.fromhex(content_hash(document, domain=domain))


class VerifyKey:
    """A trusted public key."""

    def __init__(self, key: Ed25519PublicKey) -> None:
        self._key = key
        self.raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.key_id = sha256_hex(self.raw)

    @classmethod
    def from_raw_hex(cls, raw: str) -> VerifyKey:
        return cls(Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw)))

    @classmethod
    def from_pem(cls, pem: bytes) -> VerifyKey:
        """Load a public key, or the public half of a private key."""
        if b"PRIVATE KEY" in pem:
            return SigningKey.from_pem(pem).verify_key
        key = serialization.load_pem_public_key(pem)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("not an Ed25519 public key")
        return cls(key)

    def to_pem(self) -> bytes:
        return self._key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def verifies(self, signature_hex: str, message: bytes) -> bool:
        try:
            self._key.verify(bytes.fromhex(signature_hex), message)
        except InvalidSignature, ValueError:
            return False
        return True

    def verifies_checkpoint(self, checkpoint: Checkpoint) -> bool:
        message = checkpoint_message(
            project_id=checkpoint.project_id,
            seq=checkpoint.seq,
            head_hash=checkpoint.head_hash,
            key_id=checkpoint.key_id,
        )
        return checkpoint.key_id == self.key_id and self.verifies(checkpoint.signature, message)


class SigningKey:
    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        self.verify_key = VerifyKey(key.public_key())
        self.key_id = self.verify_key.key_id

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: bytes) -> SigningKey:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("not an Ed25519 private key")
        return cls(key)

    @classmethod
    def from_file(cls, path: Path) -> SigningKey:
        return cls.from_pem(path.read_bytes())

    def sign(self, message: bytes) -> str:
        return self._key.sign(message).hex()

    def sign_checkpoint(
        self, *, id: Id, project_id: Id, seq: int, head_hash: str, created_at: datetime
    ) -> Checkpoint:
        message = checkpoint_message(
            project_id=project_id, seq=seq, head_hash=head_hash, key_id=self.key_id
        )
        return Checkpoint(
            id=id,
            project_id=project_id,
            seq=seq,
            head_hash=head_hash,
            key_id=self.key_id,
            signature=self.sign(message),
            created_at=created_at,
        )
