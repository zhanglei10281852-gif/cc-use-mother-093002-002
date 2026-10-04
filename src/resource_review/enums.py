"""领域枚举与中文别名解析。

所有枚举均可通过 ``parse_enum`` 接受英文取值、枚举名或常用中文叫法，
方便教研负责人在离线命令中直接使用中文输入。
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable, TypeVar

from .errors import ValidationError


class ResourceKind(str, Enum):
    TEXT_FRAGMENT = "text_fragment"  # 文本片段
    QUESTION = "question"  # 题目
    LECTURE_UNIT = "lecture_unit"  # 讲义单元


class Origin(str, Enum):
    GENERATED = "generated"  # 生成式工具产出
    MANUAL = "manual"  # 人工编写或人工改写


class ReviewDimension(str, Enum):
    LANGUAGE = "language"  # 语言
    CULTURE = "culture"  # 文化
    FACT = "fact"  # 事实
    COPYRIGHT = "copyright"  # 版权
    PEDAGOGY = "pedagogy"  # 教学适切性


class RiskLevel(str, Enum):
    LOW = "low"  # 低
    MEDIUM = "medium"  # 中
    HIGH = "high"  # 高

    @property
    def severity(self) -> int:
        return _RISK_SEVERITY[self]

    @classmethod
    def highest(cls, levels: Iterable["RiskLevel"]) -> "RiskLevel":
        levels = list(levels)
        if not levels:
            return cls.LOW
        return max(levels, key=lambda level: level.severity)


_RISK_SEVERITY = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


class Verdict(str, Enum):
    APPROVE = "approve"  # 通过
    REJECT = "reject"  # 拒绝
    NEEDS_REVISION = "needs_revision"  # 需修改

    @property
    def is_positive(self) -> bool:
        return self is Verdict.APPROVE


class ConclusionResult(str, Enum):
    PASSED = "passed"  # 审校通过
    REJECTED = "rejected"  # 审校不通过


class ConclusionStatus(str, Enum):
    FINAL = "final"  # 已生效
    PENDING_REREVIEW = "pending_rereview"  # 高风险结论，等待复核


class RereviewOutcome(str, Enum):
    CONFIRMED = "confirmed"  # 确认原高风险结论
    OVERTURNED = "overturned"  # 推翻并下调风险等级


class TokenStatus(str, Enum):
    ACTIVE = "active"  # 有效
    SUPERSEDED = "superseded"  # 被更新版本的令牌取代
    REVOKED = "revoked"  # 已随资源撤销而失效


class CourseVersionStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    QUARANTINED = "quarantined"  # 已隔离


class DispositionAction(str, Enum):
    QUARANTINE = "quarantine"  # 隔离
    REPLACE = "replace"  # 替换


class DispositionStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"


E = TypeVar("E", bound=Enum)

_ALIASES: dict[type[Enum], dict[str, Enum]] = {
    ResourceKind: {
        "文本片段": ResourceKind.TEXT_FRAGMENT,
        "片段": ResourceKind.TEXT_FRAGMENT,
        "题目": ResourceKind.QUESTION,
        "习题": ResourceKind.QUESTION,
        "讲义单元": ResourceKind.LECTURE_UNIT,
        "讲义": ResourceKind.LECTURE_UNIT,
        "单元": ResourceKind.LECTURE_UNIT,
    },
    Origin: {
        "生成": Origin.GENERATED,
        "智能生成": Origin.GENERATED,
        "生成式": Origin.GENERATED,
        "人工": Origin.MANUAL,
        "人工编写": Origin.MANUAL,
        "人工改写": Origin.MANUAL,
    },
    ReviewDimension: {
        "语言": ReviewDimension.LANGUAGE,
        "文化": ReviewDimension.CULTURE,
        "事实": ReviewDimension.FACT,
        "版权": ReviewDimension.COPYRIGHT,
        "教学适切性": ReviewDimension.PEDAGOGY,
        "适切性": ReviewDimension.PEDAGOGY,
        "教学": ReviewDimension.PEDAGOGY,
    },
    RiskLevel: {
        "低": RiskLevel.LOW,
        "中": RiskLevel.MEDIUM,
        "高": RiskLevel.HIGH,
        "低风险": RiskLevel.LOW,
        "中风险": RiskLevel.MEDIUM,
        "高风险": RiskLevel.HIGH,
    },
    Verdict: {
        "通过": Verdict.APPROVE,
        "拒绝": Verdict.REJECT,
        "驳回": Verdict.REJECT,
        "需修改": Verdict.NEEDS_REVISION,
        "需修订": Verdict.NEEDS_REVISION,
    },
    RereviewOutcome: {
        "确认": RereviewOutcome.CONFIRMED,
        "维持": RereviewOutcome.CONFIRMED,
        "推翻": RereviewOutcome.OVERTURNED,
        "下调": RereviewOutcome.OVERTURNED,
    },
    DispositionAction: {
        "隔离": DispositionAction.QUARANTINE,
        "替换": DispositionAction.REPLACE,
    },
}


def parse_enum(enum_cls: type[E], value: object) -> E:
    """把枚举成员、英文取值或中文别名解析为枚举成员。"""
    if isinstance(value, enum_cls):
        return value
    text = str(value).strip()
    for member in enum_cls:
        if member.value == text or member.name.lower() == text.lower():
            return member
    alias = _ALIASES.get(enum_cls, {}).get(text)
    if alias is not None:
        return alias  # type: ignore[return-value]
    raise ValidationError(f"无法识别的 {enum_cls.__name__} 取值: {value!r}")
