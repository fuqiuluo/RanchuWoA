"""按 system/vold KeyStorage.cpp 的 encryptWithoutKeystore(kEmptyAuthentication)
格式，为模拟器自身 data 分区生成全新的 metadata 加密密钥组（随机生成，非提取或破解既有密钥）。

产出（写入 /tmp/apk 下）:
  version        -> "1"
  encrypted_key  -> nonce(12) || AES-256-GCM(SHA512(prefix||appId)[:32], raw_key) (ct+tag16)
  raw_key 保留本地以便调试（不进 metadata）
"""
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

OUT = Path(r"C:\Users\admin\AppData\Local\Temp\apk")

KEYGEN_PREFIX = b"Android key wrapping key generation SHA512"
RAW_KEY_LEN = 64   # aes-256-xts 双密钥

raw_key = os.urandom(RAW_KEY_LEN)
app_id = b""   # kEmptyAuthentication: secret="" 且无 secdiscardable
wrap_key = hashlib.sha512(KEYGEN_PREFIX + app_id).digest()[:32]
nonce = os.urandom(12)
ct = AESGCM(wrap_key).encrypt(nonce, raw_key, None)

(OUT / "version").write_bytes(b"1")
(OUT / "encrypted_key").write_bytes(nonce + ct)
(OUT / "raw_key_debug").write_bytes(raw_key)
print(f"version={1} raw_key={RAW_KEY_LEN}B encrypted_key={12 + len(ct)}B wrap_key={wrap_key.hex()[:16]}...")
