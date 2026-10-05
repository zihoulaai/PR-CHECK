"""Fernet 对称加密：GitLab Token / LLM API Key / KB API Key 加密存储。

主密钥来自 APP_ENCRYPTION_KEY，仅部署注入，不进入数据库（C2）。
主密钥缺失或非法时抛出清晰可定位错误而非崩溃（S2 关键坑）。
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.errors import SecurityError


class SecretStore:
    def __init__(self, master_key: str | None):
        if not master_key:
            raise SecurityError("主密钥 APP_ENCRYPTION_KEY 未配置，无法加解密敏感信息。")
        try:
            self._fernet = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)
        except Exception as exc:  # 非标准 key
            raise SecurityError(f"主密钥格式非法（需为 Fernet key）：{exc}") from exc

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        # 以字符串存储，兼容 SQLite TEXT
        return token.decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise SecurityError("敏感信息解密失败，可能主密钥已变更。") from exc


_cache: SecretStore | None = None


def get_secret_store() -> SecretStore:
    global _cache
    if _cache is None:
        _cache = SecretStore(get_settings().app_encryption_key)
    return _cache


def reset_secret_store() -> None:
    global _cache
    _cache = None
