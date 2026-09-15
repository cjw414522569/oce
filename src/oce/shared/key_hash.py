"""用户 API key 的哈希约定。

auth 校验与 store 写入共享同一实现——两处独立定义一旦漂移（如改加盐/前缀），
所有用户 key 会静默 401。放 shared 层供 api 与 infrastructure 共用。
"""

from __future__ import annotations

from hashlib import sha256


def hash_api_key(api_key: str) -> str:
    # 无盐 sha256：key 是 ~256bit 随机串，不存在密码场景的字典攻击面
    return sha256(api_key.encode("utf-8")).hexdigest()
