"""稳定的 JSON 序列化与摘要工具，是幂等、版本化与重放一致性的基础。"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from hashlib import sha256
from typing import Any, Mapping

from .clock import format_ts


def to_plain(obj: Any) -> Any:
    """递归转成可 JSON 化的普通结构，键顺序不影响摘要（canonical 另行排序）。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_plain(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, datetime):
        return format_ts(obj)
    if isinstance(obj, Mapping):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_plain(v) for v in obj]
    return obj


def canonical_dumps(obj: Any) -> str:
    """键排序、无空白的确定性 JSON。"""
    import json

    return json.dumps(
        to_plain(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(obj: Any) -> str:
    """对任意可序列化对象求 sha256，作为内容版本指纹。"""
    return sha256(canonical_dumps(obj).encode("utf-8")).hexdigest()


def short_digest(obj: Any, length: int = 10) -> str:
    return digest(obj)[:length]
