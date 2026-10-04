"""领域模型：不可变版本、审校意见、裁决、结论、发布令牌与撤销处置。

所有记录均为冻结数据类，一旦写入事实链不可更改；
后续决定只能以新的事件追加，形成单一事实链。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum

from .enums import (
    ConclusionResult,
    ConclusionStatus,
    CourseVersionStatus,
    DispositionAction,
    DispositionStatus,
    Origin,
    ResourceKind,
    RereviewOutcome,
    ReviewDimension,
    RiskLevel,
    TokenStatus,
    Verdict,
)


def content_digest(kind: ResourceKind, content: str) -> str:
    """内容摘要：同一类型与内容必然得到同一摘要。"""
    return hashlib.sha256(f"{kind.value}\n{content}".encode("utf-8")).hexdigest()


def _require(value: object, message: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(message)


@dataclass(frozen=True)
class SourceDeclaration:
    """来源声明：说明内容是生成式工具产出还是人工编写/改写。"""

    declaration_id: str
    origin: Origin
    tool_name: str = ""
    operator: str = ""
    note: str = ""
    declared_at: str = ""

    def __post_init__(self) -> None:
        _require(self.declaration_id, "来源声明缺少标识")


@dataclass(frozen=True)
class ResourceVersionRecord:
    """资源的一次不可变版本，绑定内容摘要与来源声明。"""

    entity_id: str
    version_id: str
    revision: int
    kind: ResourceKind
    content: str
    digest: str
    declaration_id: str
    derived_from_version_id: str | None
    submitted_at: str

    def __post_init__(self) -> None:
        _require(self.entity_id, "版本缺少资源标识")
        _require(self.version_id, "版本缺少版本标识")
        _require(self.digest, "版本缺少内容摘要")
        if self.revision < 1:
            raise ValueError("版本序号必须大于等于 1")


@dataclass(frozen=True)
class Reviewer:
    reviewer_id: str
    name: str
    qualifications: frozenset[ReviewDimension]

    def __post_init__(self) -> None:
        _require(self.reviewer_id, "审校人缺少标识")
        if not self.qualifications:
            raise ValueError("审校人至少应具备一个维度的资质")


@dataclass(frozen=True)
class ReviewAssignment:
    assignment_id: str
    entity_id: str
    version_id: str
    dimension: ReviewDimension
    reviewer_id: str
    assigned_at: str


@dataclass(frozen=True)
class ReviewOpinion:
    """某维度审校人对指定版本的一次意见。"""

    opinion_id: str
    entity_id: str
    version_id: str
    dimension: ReviewDimension
    reviewer_id: str
    risk_level: RiskLevel
    verdict: Verdict
    rationale: str
    created_at: str


@dataclass(frozen=True)
class Adjudication:
    """裁决：对相互冲突的有效意见给出可追踪的最终裁定。"""

    adjudication_id: str
    entity_id: str
    version_id: str
    opinion_ids: tuple[str, ...]
    final_verdict: Verdict
    final_risk_level: RiskLevel
    rationale: str
    decided_by: str
    decided_at: str

    def __post_init__(self) -> None:
        if not self.opinion_ids:
            raise ValueError("裁决必须引用被裁定的意见")


@dataclass(frozen=True)
class RereviewRecord:
    """复核记录：高风险结论经第二名审校人确认或推翻。"""

    reviewer_id: str
    outcome: RereviewOutcome
    adjusted_risk_level: RiskLevel | None
    rationale: str
    created_at: str


@dataclass(frozen=True)
class Conclusion:
    """审校结论：绑定通过时的内容摘要；高风险结论须复核后才生效。"""

    conclusion_id: str
    entity_id: str
    version_id: str
    digest: str
    result: ConclusionResult
    risk_level: RiskLevel
    status: ConclusionStatus
    reached_at: str
    rereview: RereviewRecord | None = None


@dataclass(frozen=True)
class PublishToken:
    """发布令牌：只能绑定审校通过时的内容摘要。"""

    token_id: str
    entity_id: str
    version_id: str
    digest: str
    conclusion_id: str
    status: TokenStatus
    issued_at: str


@dataclass(frozen=True)
class CourseEntry:
    entity_id: str
    version_id: str
    digest: str
    token_id: str


@dataclass(frozen=True)
class CourseVersionRecord:
    """课程包版本：记录发布时纳入的资源摘要，用于追踪传播范围。"""

    course_id: str
    course_revision: int
    entries: tuple[CourseEntry, ...]
    status: CourseVersionStatus
    created_at: str


@dataclass(frozen=True)
class Revocation:
    revocation_id: str
    entity_id: str
    version_id: str
    digest: str
    reason: str
    revoked_by: str
    revoked_at: str


@dataclass(frozen=True)
class Disposition:
    """处置：撤销后定位到的每个受影响课程版本对应一条处置。"""

    disposition_id: str
    revocation_id: str
    entity_id: str
    course_id: str
    course_revision: int
    action: DispositionAction
    status: DispositionStatus
    replacement_version_id: str | None
    resulting_course_revision: int | None
    note: str
    created_at: str
    completed_at: str | None


def to_jsonable(obj: object) -> object:
    """把领域对象递归转换为可 JSON 序列化的结构。"""
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {key: to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(item) for item in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(to_jsonable(item) for item in obj)
    return obj
