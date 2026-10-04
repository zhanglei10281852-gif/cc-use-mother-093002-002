"""领域错误类型。"""


class DomainError(Exception):
    """领域规则被违反时的基础错误。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class ValidationError(DomainError):
    """输入参数不合法。"""


class QualificationError(DomainError):
    """审校人不具备对应维度的资质。"""


class StateError(DomainError):
    """当前状态不允许执行该操作。"""


class ConcurrencyConflictError(DomainError):
    """并发决定冲突：事实链已被其他决定推进，本次提交被拒绝。"""


class IdempotencyConflictError(DomainError):
    """同一幂等键被用于不同的请求内容。"""
