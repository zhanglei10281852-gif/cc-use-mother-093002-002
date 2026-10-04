"""审校领域的数据模型。

所有模型均为不可变值对象：版本、意见、裁决一经产生不得修改，
状态变化只能通过追加新的事实记录（新版本、新意见、处置记录）表达。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

# 五类审校分工
CATEGORY_LANGUAGE = "language"
CATEGORY_CULTURE = "culture"
CATEGORY_FACT = "fact"
CATEGORY_COPYRIGHT = "copyright"
CATEGORY_SUITABILITY = "suitability"

CATEGORIES: FrozenSet[str] = frozenset(
    {
        CATEGORY_LANGUAGE,
        CATEGORY_CULTURE,
        CATEGORY_FACT,
        CATEGORY_COPYRIGHT,
        CATEGORY_SUITABILITY,
    }
)

CATEGORY_LABELS = {
    CATEGORY_LANGUAGE: "语言",
    CATEGORY_CULTURE: "文化",
    CATEGORY_FACT: "事实",
    CATEGORY_COPYRIGHT: "版权",
    CATEGORY_SUITABILITY: "教学适切性",
}

RESOURCE_KINDS = frozenset({"text", "question", "handout"})

CONCLUSION_APPROVED = "approved"
CONCLUSION_CHANGES = "changes_requested"
CONCLUSION_REJECTED = "rejected"
CONCLUSIONS = frozenset(
    {CONCLUSION_APPROVED, CONCLUSION_CHANGES, CONCLUSION_REJECTED}
)

RISK_LOW = "low"
RISK_HIGH = "high"
RISK_LEVELS = frozenset({RISK_LOW, RISK_HIGH})

STAGE_FIRST = "first"
STAGE_RECHECK = "recheck"

ACTION_QUARANTINED = "quarantined"
ACTION_REPLACED = "replaced"


def canonical_json(data: object) -> bytes:
    """确定性的规范 JSON 序列化，用于指纹与哈希。"""
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SourceClaim:
    """来源声明：素材的可核验出处。

    生成式工具整理的素材必须记录所用工具；人工素材记录整理人。
    至少要有一个可定位的线索（标题、作者、链接或位置）。
    """

    title: Optional[str] = None
    author: Optional[str] = None
    url: Optional[str] = None
    license_tag: Optional[str] = None
    location: Optional[str] = None
    generator: Optional[str] = None
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if not any([self.title, self.author, self.url, self.location]):
            raise ValueError("来源声明至少需要标题、作者、链接或出处位置之一")

    def as_dict(self) -> dict:
        return {
            key: value
            for key, value in {
                "title": self.title,
                "author": self.author,
                "url": self.url,
                "license": self.license_tag,
                "location": self.location,
                "generator": self.generator,
                "note": self.note,
            }.items()
            if value is not None
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceClaim":
        return cls(
            title=data.get("title"),
            author=data.get("author"),
            url=data.get("url"),
            license_tag=data.get("license"),
            location=data.get("location"),
            generator=data.get("generator"),
            note=data.get("note"),
        )


@dataclass(frozen=True)
class Version:
    """教学资源的一个不可变版本（文本片段、题目或讲义单元）。"""

    digest: str
    entity_id: str
    revision: int
    kind: str
    title: str
    content: str
    source: SourceClaim
    author_kind: str  # "human" 或 "generator"
    author: str
    parent_digest: Optional[str]
    summary_hash: str
    created_at: str


@dataclass(frozen=True)
class Reviewer:
    """具有审校资质的人员。

    qualifications 为可承担初审的类别；senior_for 为可承担高风险复核的类别；
    can_adjudicate 表示可作为教研负责人裁决意见冲突。
    """

    reviewer_id: str
    name: str
    qualifications: FrozenSet[str] = field(default_factory=frozenset)
    senior_for: FrozenSet[str] = field(default_factory=frozenset)
    can_adjudicate: bool = False


@dataclass(frozen=True)
class Opinion:
    """一条审校意见（初审或复核），不可变。"""

    opinion_id: str
    digest: str
    category: str
    stage: str
    reviewer_id: str
    conclusion: str
    risk_level: str
    note: str
    created_at: str


@dataclass(frozen=True)
class Adjudication:
    """意见冲突的裁决记录。"""

    adjudication_id: str
    digest: str
    category: str
    first_opinion_id: str
    recheck_opinion_id: str
    opened_at: str
    decider_id: Optional[str] = None
    outcome: Optional[str] = None
    rationale: Optional[str] = None
    resolved_at: Optional[str] = None

    def resolved(self) -> bool:
        return self.outcome is not None


@dataclass(frozen=True)
class TokenRecord:
    """发布令牌：绑定审校通过时刻的内容摘要与审校证据。"""

    token: str
    digest: str
    summary_hash: str
    approvals_fingerprint: str
    created_at: str


@dataclass(frozen=True)
class CoursePackage:
    """课程包的一个不可变版本，引用若干已发布资源摘要。"""

    package_id: str
    title: str
    version: str
    digests: tuple
    created_at: str


@dataclass(frozen=True)
class Revocation:
    """已发布资源的撤销记录。"""

    revocation_id: str
    digest: str
    reason: str
    actor: str
    created_at: str


@dataclass(frozen=True)
class Disposition:
    """撤销后对单个受影响课程版本的处置：先隔离，可进一步替换。"""

    disposition_id: str
    revocation_id: str
    package_id: str
    package_version: str
    digest: str
    action: str
    created_at: str
    substitute_digest: Optional[str] = None
    replaced_at: Optional[str] = None
