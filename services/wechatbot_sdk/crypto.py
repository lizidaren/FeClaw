"""AES-128-ECB encryption for WeChat CDN media files."""

from __future__ import annotations

import base64
import binascii
import os
import re
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .errors import MediaError

_HEX_32 = re.compile(r"^[0-9a-fA-F]{32}$")


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt with AES-128-ECB + PKCS7 padding."""
    if len(key) != 16:
        raise MediaError(f"AES key must be 16 bytes, got {len(key)}")
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB and remove PKCS7 padding."""
    if len(key) != 16:
        raise MediaError(f"AES key must be 16 bytes, got {len(key)}")
    if len(ciphertext) % 16 != 0:
        raise MediaError(f"Ciphertext length {len(ciphertext)} is not a multiple of 16")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def generate_aes_key() -> bytes:
    """Generate a random 16-byte AES key."""
    return os.urandom(16)


def encrypted_size(raw_size: int) -> int:
    """Calculate size after AES-128-ECB with PKCS7 padding."""
    return ((raw_size + 1 + 15) // 16) * 16


def decode_aes_key(encoded: str) -> bytes:
    """Decode an aes_key from the protocol.

    Handles all three formats:
      - Direct hex string (32 hex chars) — from image_item.aeskey
      - base64(raw 16 bytes) — Format A
      - base64(hex string 32 chars) — Format B
    """
    # Direct hex
    if _HEX_32.match(encoded):
        return binascii.unhexlify(encoded)

    # Base64 decode
    try:
        decoded = base64.b64decode(encoded)
    except Exception as e:
        raise MediaError(f"Cannot base64 decode aes_key: {e}") from e

    if len(decoded) == 16:
        return decoded

    if len(decoded) == 32:
        try:
            hex_str = decoded.decode("ascii")
            if _HEX_32.match(hex_str):
                return binascii.unhexlify(hex_str)
        except (UnicodeDecodeError, binascii.Error):
            pass

    raise MediaError(f"Decoded aes_key has unexpected length {len(decoded)} (want 16 or 32)")


def encode_aes_key_hex(key: bytes) -> str:
    """Encode key as hex string (for getuploadurl)."""
    return key.hex()


def encode_aes_key_base64(key: bytes) -> str:
    """Encode key as base64(hex) (for CDNMedia.aes_key)."""
    return base64.b64encode(key.hex().encode("utf-8")).decode("ascii")
