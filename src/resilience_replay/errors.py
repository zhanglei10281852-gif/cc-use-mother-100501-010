"""跨网络韧性演练复盘的统一领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有可预期的领域错误基类。"""


class ScenarioValidationError(DomainError, ValueError):
    """冻结场景校验失败（引用缺失、成环、计划不完整等）。"""


class EventValidationError(DomainError, ValueError):
    """事件信封或载荷不符合协议。"""


class EventConflictError(DomainError):
    """同一事件标识出现了不同内容，存在伪造或串改风险。"""


class JournalIntegrityError(DomainError):
    """追加日志的哈希链校验失败，存储已被破坏。"""


class ExerciseNotFoundError(DomainError, KeyError):
    """指定演练不存在。"""
