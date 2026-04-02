"""
Kalshi API Authentication
==========================

RSA-PSS based authentication for Kalshi's Trade API.

Usage:
    auth = KalshiAuth.from_env()           # Load from KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH
    auth = KalshiAuth(key_id, private_key) # Direct construction

    headers = auth.get_headers("GET", "/trade-api/v2/markets")
"""

import base64
import logging
import os
import time
from typing import Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)


class KalshiAuth:
    """
    RSA-PSS authentication for Kalshi API.

    Signs requests with: timestamp_ms + METHOD + path (without query params).
    """

    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey):
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def from_env(cls) -> "KalshiAuth":
        """
        Load credentials from environment variables.

        Env vars:
            KALSHI_API_KEY: API key ID
            KALSHI_PRIVATE_KEY_PATH: Path to PEM private key file
            KALSHI_PRIVATE_KEY: Raw PEM key string (alternative to path)
        """
        key_id = os.environ.get("KALSHI_API_KEY")
        if not key_id:
            raise ValueError("KALSHI_API_KEY environment variable not set")

        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        key_str = os.environ.get("KALSHI_PRIVATE_KEY")

        if key_path:
            private_key = _load_private_key_from_file(key_path)
        elif key_str:
            private_key = _load_private_key_from_string(key_str)
        else:
            raise ValueError(
                "Either KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY must be set"
            )

        return cls(key_id=key_id, private_key=private_key)

    @classmethod
    def from_file(cls, key_id: str, key_path: str) -> "KalshiAuth":
        """Load from explicit key ID and PEM file path."""
        private_key = _load_private_key_from_file(key_path)
        return cls(key_id=key_id, private_key=private_key)

    @classmethod
    def from_string(cls, key_id: str, key_pem: str) -> "KalshiAuth":
        """Load from explicit key ID and PEM key string."""
        private_key = _load_private_key_from_string(key_pem)
        return cls(key_id=key_id, private_key=private_key)

    def get_headers(self, method: str, path: str) -> dict[str, str]:
        """
        Generate authentication headers for a Kalshi API request.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: Full API path (e.g., /trade-api/v2/markets).
                  Query parameters are stripped automatically.

        Returns:
            Dict with KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE,
            KALSHI-ACCESS-TIMESTAMP headers.
        """
        timestamp_ms = str(int(time.time() * 1000))

        # Strip query params from path for signing
        path_without_query = path.split("?")[0]

        # Build message: timestamp_ms + METHOD + path
        message = timestamp_ms + method.upper() + path_without_query
        signature = _sign_pss(self._private_key, message)

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    def get_ws_headers(self) -> dict[str, str]:
        """
        Generate authentication headers for WebSocket handshake.

        Signs: timestamp_ms + "GET" + "/trade-api/ws/v2"
        """
        return self.get_headers("GET", "/trade-api/ws/v2")


def _load_private_key_from_file(file_path: str) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file."""
    with open(file_path, "rb") as f:
        private_key = serialization.load_pem_private_key(
            f.read(),
            password=None,
            backend=default_backend(),
        )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError(f"Expected RSA private key, got {type(private_key).__name__}")
    return private_key


def _load_private_key_from_string(key_str: str) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM string."""
    private_key = serialization.load_pem_private_key(
        key_str.encode("utf-8"),
        password=None,
        backend=default_backend(),
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError(f"Expected RSA private key, got {type(private_key).__name__}")
    return private_key


def _sign_pss(private_key: rsa.RSAPrivateKey, text: str) -> str:
    """Sign text using RSA-PSS with SHA256, return base64-encoded signature."""
    message = text.encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")
