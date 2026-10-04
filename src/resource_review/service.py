"""智能教学资源风险审校核心服务。

业务规则（对应审校制度）：

* 资源按内容指纹去重：重复提交返回同一版本，不产生新的事实链；
  人工或生成式改写通过父版本挂接，形成可追溯的版本谱系。
* 五个类别（语言、文化、事实、版权、教学适切性）按审校人资质分派；
  高风险结论必须由另一资深审校人复核；初审与复核冲突时形成裁决单，
  由教研负责人裁决后方为定论。
* 五类结论全部通过方可发布；发布令牌绑定通过时刻的内容摘要与审校证据，
  重复或并发发布只得到同一枚令牌。
* 撤销已发布资源时，自动定位引用它的全部课程包版本，逐个隔离，
  并支持替换为新的已发布版本；中断后可用 pending_work 补齐未竟事项。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import DefaultDict, Dict, List, Optional, Tuple

from .models import (
    ACTION_QUARANTINED,
    ACTION_REPLACED,
    CATEGORIES,
    CATEGORY_LABELS,
    CONCLUSION_APPROVED,
    CONCLUSIONS,
    RISK_HIGH,
    RISK_LEVELS,
    STAGE_FIRST,
    STAGE_RECHECK,
    Adjudication,
    CoursePackage,
    Disposition,
    Opinion,
    Reviewer,
    Revocation,
    SourceClaim,
    TokenRecord,
    Version,
    canonical_json,
    sha256_hex,
)

ADJ_UPHOLD_FIRST = "uphold_first"
ADJ_UPHOLD_RECHECK = "uphold_recheck"
ADJ_OUTCOMES = frozenset({ADJ_UPHOLD_FIRST, ADJ_UPHOLD_RECHECK})


class RuleError(ValueError):
    """违反审校制度的操作。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewService:
    def __init__(self, store) -> None:
        self.store = store
        self._lock = threading.RLock()

        self.reviewers: Dict[str, Reviewer] = {}
        self.versions: Dict[str, Version] = {}
        self._entity_digests: DefaultDict[str, List[str]] = defaultdict(list)
        self._opinions: Dict[str, Opinion] = {}
        # (digest, category) -> {"first": Opinion|None, "recheck": Opinion|None}
        self._dc: Dict[Tuple[str, str], Dict[str, Optional[Opinion]]] = {}
        self._adjudications: Dict[str, Adjudication] = {}
        self._adj_by_dc: Dict[Tuple[str, str], Adjudication] = {}
        self.tokens: Dict[str, TokenRecord] = {}
        self.packages: Dict[Tuple[str, str], CoursePackage] = {}
        self._packages_by_digest: DefaultDict[str, List[Tuple[str, str]]] = defaultdict(list)
        self.revocations: Dict[str, Revocation] = {}
        self._revocation_by_digest: Dict[str, str] = {}
        self.dispositions: Dict[str, Disposition] = {}
        self._disp_by_triple: Dict[Tuple[str, str, str], Disposition] = {}

        # 实时追加与重放走同一条事件处理路径，保证两套状态更新逻辑不漂移
        self.store.subscribe(self._handle)
        self.store.replay(self._handle)

    # ------------------------------------------------------------------ 摘要

    @staticmethod
    def content_summary(version: Version) -> dict:
        """版本发布时绑定的结构化内容摘要。"""
        return {
            "digest": version.digest,
            "entity_id": version.entity_id,
            "revision": version.revision,
            "kind": version.kind,
            "title": version.title,
            "length": len(version.content),
            "content_sha256": version.digest,
            "source": version.source.as_dict(),
        }

    def summary_hash(self, version: Version) -> str:
        return sha256_hex(canonical_json(self.content_summary(version)))

    # ------------------------------------------------------------ 审校人登记

    def register_reviewer(
        self,
        reviewer_id: str,
        name: str,
        qualifications,
        senior_for=frozenset(),
        can_adjudicate: bool = False,
    ) -> Reviewer:
        quals = frozenset(qualifications)
        seniors = frozenset(senior_for)
        if not quals <= CATEGORIES:
            raise RuleError(f"未知审校类别：{sorted(quals - CATEGORIES)}")
        if not seniors <= quals:
            raise RuleError("高风险复核资质必须以该类别初审资质为前提")
        with self._lock:
            existing = self.reviewers.get(reviewer_id)
            if existing is not None:
                if (
                    existing.name == name
                    and existing.qualifications == quals
                    and existing.senior_for == seniors
                    and existing.can_adjudicate == can_adjudicate
                ):
                    return existing
                raise RuleError(f"审校人 {reviewer_id} 已存在且档案不一致")
            reviewer = Reviewer(reviewer_id, name, quals, seniors, can_adjudicate)
            self.store.append(
                {
                    "type": "reviewer_registered",
                    "reviewer": {
                        "reviewer_id": reviewer_id,
                        "name": name,
                        "qualifications": sorted(quals),
                        "senior_for": sorted(seniors),
                        "can_adjudicate": can_adjudicate,
                    },
                }
            )
            return reviewer

    # -------------------------------------------------------------- 版本提交

    def submit(
        self,
        entity_id: str,
        kind: str,
        title: str,
        content: str,
        source: SourceClaim,
        author_kind: str,
        author: str,
        parent_digest: Optional[str] = None,
    ) -> Tuple[Version, bool]:
        """提交资源版本；返回 (版本, 是否新建)。

        相同内容重复提交幂等返回既有版本，不产生另一条事实链。
        """
        if not entity_id or not title or not content or not author:
            raise RuleError("实体标识、标题、内容与作者均不能为空")
        if kind not in ("text", "question", "handout"):
            raise RuleError(f"未知资源类型：{kind}")
        if author_kind not in ("human", "generator"):
            raise RuleError("作者类型只能是 human 或 generator")
        if author_kind == "generator" and not source.generator:
            raise RuleError("生成式工具产出的素材必须在来源声明中记录所用工具")

        digest = sha256_hex(canonical_json({"kind": kind, "title": title, "content": content}))
        with self._lock:
            # 内容寻址：全局去重
            existing = self.versions.get(digest)
            if existing is not None:
                return existing, False

            if parent_digest is None:
                if self._entity_digests[entity_id]:
                    raise RuleError(
                        f"实体 {entity_id} 已有历史版本，再次提交必须挂接父版本"
                    )
                revision = 1
            else:
                parent = self.versions.get(parent_digest)
                if parent is None:
                    raise RuleError("父版本不存在")
                if parent.entity_id != entity_id:
                    raise RuleError("改写版本必须与父版本属于同一教学实体")
                revision = parent.revision + 1

            created_at = _now()
            version = Version(
                digest=digest,
                entity_id=entity_id,
                revision=revision,
                kind=kind,
                title=title,
                content=content,
                source=source,
                author_kind=author_kind,
                author=author,
                parent_digest=parent_digest,
                summary_hash="",  # 下方补齐
                created_at=created_at,
            )
            object.__setattr__(version, "summary_hash", self.summary_hash(version))
            self.store.append(
                {
                    "type": "version_submitted",
                    "version": {
                        "digest": version.digest,
                        "entity_id": entity_id,
                        "revision": revision,
                        "kind": kind,
                        "title": title,
                        "content": content,
                        "source": source.as_dict(),
                        "author_kind": author_kind,
                        "author": author,
                        "parent_digest": parent_digest,
                        "summary_hash": version.summary_hash,
                        "created_at": created_at,
                    },
                }
            )
            return version, True

    def list_versions(self, entity_id: str) -> List[Version]:
        return [self.versions[d] for d in self._entity_digests.get(entity_id, [])]

    def lineage(self, digest: str) -> List[Version]:
        """从最早版本到给定版本的谱系。"""
        chain: List[Version] = []
        current = self.versions.get(digest)
        while current is not None:
            chain.append(current)
            current = self.versions.get(current.parent_digest) if current.parent_digest else None
        chain.reverse()
        return chain

    # -------------------------------------------------------------- 意见分派

    def _slot(self, digest: str, category: str) -> Dict[str, Optional[Opinion]]:
        return self._dc.setdefault((digest, category), {"first": None, "recheck": None})

    def record_opinion(
        self,
        digest: str,
        category: str,
        reviewer_id: str,
        conclusion: str,
        risk_level: str,
        note: str = "",
    ) -> Opinion:
        if digest not in self.versions:
            raise RuleError("资源版本不存在")
        if category not in CATEGORIES:
            raise RuleError(f"未知审校类别：{category}")
        if conclusion not in CONCLUSIONS:
            raise RuleError(f"未知结论：{conclusion}")
        if risk_level not in RISK_LEVELS:
            raise RuleError(f"未知风险等级：{risk_level}")
        reviewer = self.reviewers.get(reviewer_id)
        if reviewer is None:
            raise RuleError("审校人未登记")
        if category not in reviewer.qualifications:
            raise RuleError(f"审校人 {reviewer.name} 不具备 {CATEGORY_LABELS[category]} 类别资质")

        with self._lock:
            if digest in self.tokens:
                raise RuleError("该版本已发布，意见不可变更；请以新版本提交改写")
            if digest in self._revocation_by_digest:
                raise RuleError("该版本已撤销，不再接受审校意见")

            slot = self._slot(digest, category)
            adj = self._adj_by_dc.get((digest, category))
            if adj is not None and not adj.resolved():
                raise RuleError("初审与复核意见冲突，须先由负责人裁决")
            if adj is not None and adj.resolved():
                raise RuleError("该类别已有终局裁决，结论以裁决为准")

            if slot["first"] is None:
                stage = STAGE_FIRST
            else:
                stage = STAGE_RECHECK
                first = slot["first"]
                assert first is not None
                if reviewer_id == first.reviewer_id:
                    raise RuleError("复核必须由初审人以外的审校人承担")
                if category not in reviewer.senior_for:
                    raise RuleError("高风险/复审意见须由该类别的资深审校人复核")
                if slot["recheck"] is not None:
                    raise RuleError("该类别复核意见已存在，结论冲突须走裁决")

            opinion_id = "OP-{}-{}-{}".format(digest[:10], category, stage)
            opinion = Opinion(
                opinion_id=opinion_id,
                digest=digest,
                category=category,
                stage=stage,
                reviewer_id=reviewer_id,
                conclusion=conclusion,
                risk_level=risk_level,
                note=note,
                created_at=_now(),
            )
            self.store.append(
                {
                    "type": "opinion_recorded",
                    "opinion": {
                        "opinion_id": opinion_id,
                        "digest": digest,
                        "category": category,
                        "stage": stage,
                        "reviewer_id": reviewer_id,
                        "conclusion": conclusion,
                        "risk_level": risk_level,
                        "note": note,
                        "created_at": opinion.created_at,
                    },
                }
            )

            # 复核与初审结论冲突 → 自动开立可追踪裁决单
            if stage == STAGE_RECHECK and conclusion != slot["first"].conclusion:
                first = slot["first"]
                adjudication = Adjudication(
                    adjudication_id="ADJ-{}-{}".format(digest[:10], category),
                    digest=digest,
                    category=category,
                    first_opinion_id=first.opinion_id,
                    recheck_opinion_id=opinion_id,
                    opened_at=_now(),
                )
                self.store.append(
                    {
                        "type": "adjudication_opened",
                        "adjudication": {
                            "adjudication_id": adjudication.adjudication_id,
                            "digest": digest,
                            "category": category,
                            "first_opinion_id": first.opinion_id,
                            "recheck_opinion_id": opinion_id,
                            "opened_at": adjudication.opened_at,
                        },
                    }
                )
            return opinion

    def resolve_adjudication(
        self, adjudication_id: str, decider_id: str, outcome: str, rationale: str
    ) -> Adjudication:
        if outcome not in ADJ_OUTCOMES:
            raise RuleError("裁决结果只能是 uphold_first 或 uphold_recheck")
        if not rationale or not rationale.strip():
            raise RuleError("裁决必须记录理由")
        decider = self.reviewers.get(decider_id)
        if decider is None or not decider.can_adjudicate:
            raise RuleError("裁决须由具备负责人资格的人员作出")
        with self._lock:
            adj = self._adjudications.get(adjudication_id)
            if adj is None:
                raise RuleError("裁决单不存在")
            if adj.resolved():
                raise RuleError("裁决单已终局，不可重复裁决")
            if decider_id in (
                self._opinions[adj.first_opinion_id].reviewer_id,
                self._opinions[adj.recheck_opinion_id].reviewer_id,
            ):
                raise RuleError("裁决人不得是冲突意见的任一审校人")
            resolved_at = _now()
            self.store.append(
                {
                    "type": "adjudication_resolved",
                    "adjudication_id": adjudication_id,
                    "decider_id": decider_id,
                    "outcome": outcome,
                    "rationale": rationale,
                    "resolved_at": resolved_at,
                }
            )
            return self._adjudications[adjudication_id]

    # -------------------------------------------------------------- 类别状态

    def category_status(self, digest: str, category: str) -> dict:
        """返回单个类别的审校状态。"""
        slot = self._dc.get((digest, category), {"first": None, "recheck": None})
        first, recheck = slot["first"], slot["recheck"]
        adj = self._adj_by_dc.get((digest, category))

        result = {
            "category": category,
            "first": first,
            "recheck": recheck,
            "adjudication": adj,
            "recheck_required": False,
            "passed": False,
            "effective": None,
            "reason": "尚未分派初审",
        }
        if first is None:
            return result

        if adj is not None and adj.resolved():
            upheld = first if adj.outcome == ADJ_UPHOLD_FIRST else recheck
            result["effective"] = upheld
            result["passed"] = upheld.conclusion == CONCLUSION_APPROVED
            result["reason"] = (
                "裁决采纳{}，结论：{}".format(
                    "初审" if adj.outcome == ADJ_UPHOLD_FIRST else "复核",
                    upheld.conclusion,
                )
            )
            return result

        if adj is not None and not adj.resolved():
            result["reason"] = "初审与复核结论冲突，等待负责人裁决"
            return result

        if recheck is not None:
            # 无裁决即两者结论一致
            result["effective"] = recheck
            result["passed"] = recheck.conclusion == CONCLUSION_APPROVED
            result["reason"] = "初审与复核一致：{}".format(recheck.conclusion)
            return result

        result["effective"] = first
        if first.risk_level == RISK_HIGH:
            result["recheck_required"] = True
            result["reason"] = "初审为高风险结论，等待资深审校人复核"
        elif first.conclusion != CONCLUSION_APPROVED:
            result["reason"] = "初审未通过：{}".format(first.conclusion)
        else:
            result["passed"] = True
            result["reason"] = "初审通过（低风险）"
        return result

    def review_progress(self, digest: str) -> List[dict]:
        return [self.category_status(digest, c) for c in sorted(CATEGORIES)]

    def _approvals_fingerprint(self, digest: str) -> str:
        """发布时刻五类审校证据的指纹。"""
        evidence = []
        for status in self.review_progress(digest):
            adj = status["adjudication"]
            effective = status["effective"]
            evidence.append(
                {
                    "category": status["category"],
                    "first": status["first"].opinion_id if status["first"] else None,
                    "recheck": status["recheck"].opinion_id if status["recheck"] else None,
                    "adjudication": adj.adjudication_id if adj else None,
                    "adjudication_outcome": adj.outcome if adj else None,
                    "effective_opinion": effective.opinion_id if effective else None,
                    "conclusion": effective.conclusion if effective else None,
                }
            )
        return sha256_hex(canonical_json(evidence))

    # ---------------------------------------------------------------- 发布

    def publish(self, digest: str) -> TokenRecord:
        """五类全部通过后发布；重复/并发调用返回同一枚令牌。"""
        with self._lock:
            if digest not in self.versions:
                raise RuleError("资源版本不存在")
            existing = self.tokens.get(digest)
            if existing is not None:
                return existing
            if digest in self._revocation_by_digest:
                raise RuleError("该版本已撤销，不能发布")

            pending = [
                CATEGORY_LABELS[s["category"]] + "（" + s["reason"] + "）"
                for s in self.review_progress(digest)
                if not s["passed"]
            ]
            if pending:
                raise RuleError("尚有类别未通过审校：" + "、".join(pending))

            version = self.versions[digest]
            fingerprint = self._approvals_fingerprint(digest)
            token_id = "TOK-" + sha256_hex(
                canonical_json(
                    {"summary_hash": version.summary_hash, "approvals": fingerprint}
                )
            )[:24].upper()
            record = TokenRecord(
                token=token_id,
                digest=digest,
                summary_hash=version.summary_hash,
                approvals_fingerprint=fingerprint,
                created_at=_now(),
            )
            self.store.append(
                {
                    "type": "token_published",
                    "token": {
                        "token": token_id,
                        "digest": digest,
                        "summary_hash": version.summary_hash,
                        "approvals_fingerprint": fingerprint,
                        "created_at": record.created_at,
                    },
                }
            )
            return record

    # ------------------------------------------------------------ 课程打包

    def package_course(
        self, package_id: str, title: str, version: str, digests
    ) -> CoursePackage:
        digests = tuple(digests)
        if not digests:
            raise RuleError("课程包至少包含一个资源")
        with self._lock:
            key = (package_id, version)
            if key in self.packages:
                existing = self.packages[key]
                if existing.title == title and existing.digests == digests:
                    return existing
                raise RuleError(f"课程包 {package_id} 版本 {version} 已存在且内容不一致")
            for d in digests:
                if d not in self.versions:
                    raise RuleError(f"资源 {d[:12]} 不存在")
                if d not in self.tokens:
                    raise RuleError(f"资源 {d[:12]} 尚未通过审校发布，不得入包")
                if d in self._revocation_by_digest:
                    raise RuleError(f"资源 {d[:12]} 已撤销，不得入包")
            package = CoursePackage(
                package_id=package_id,
                title=title,
                version=version,
                digests=digests,
                created_at=_now(),
            )
            self.store.append(
                {
                    "type": "course_packaged",
                    "package": {
                        "package_id": package_id,
                        "title": title,
                        "version": version,
                        "digests": list(digests),
                        "created_at": package.created_at,
                    },
                }
            )
            return package

    # ------------------------------------------------------------ 撤销传播

    def revoke(self, digest: str, reason: str, actor: str) -> Tuple[Revocation, List[Disposition]]:
        """撤销已发布资源，并对全部受影响课程版本执行隔离。"""
        if not reason or not reason.strip():
            raise RuleError("撤销必须记录原因")
        with self._lock:
            if digest not in self.versions:
                raise RuleError("资源版本不存在")
            if digest not in self.tokens:
                raise RuleError("仅已发布资源可以撤销")
            if digest in self._revocation_by_digest:
                raise RuleError("该版本已撤销")

            revocation = Revocation(
                revocation_id="REV-" + digest[:12].upper(),
                digest=digest,
                reason=reason,
                actor=actor,
                created_at=_now(),
            )
            self.store.append(
                {
                    "type": "resource_revoked",
                    "revocation": {
                        "revocation_id": revocation.revocation_id,
                        "digest": digest,
                        "reason": reason,
                        "actor": actor,
                        "created_at": revocation.created_at,
                    },
                }
            )

            affected = sorted(self._packages_by_digest.get(digest, []))
            dispositions: List[Disposition] = []
            for package_id, package_version in affected:
                disp = self._quarantine(revocation.revocation_id, package_id, package_version, digest)
                dispositions.append(disp)
            return revocation, dispositions

    def _quarantine(self, revocation_id, package_id, package_version, digest) -> Disposition:
        triple = (package_id, package_version, digest)
        existing = self._disp_by_triple.get(triple)
        if existing is not None:
            return existing
        disp = Disposition(
            disposition_id="DSP-{}-{}-{}".format(revocation_id[4:], package_id, package_version),
            revocation_id=revocation_id,
            package_id=package_id,
            package_version=package_version,
            digest=digest,
            action=ACTION_QUARANTINED,
            created_at=_now(),
        )
        self.store.append(
            {
                "type": "disposition_quarantined",
                "disposition": {
                    "disposition_id": disp.disposition_id,
                    "revocation_id": revocation_id,
                    "package_id": package_id,
                    "package_version": package_version,
                    "digest": digest,
                    "action": ACTION_QUARANTINED,
                    "created_at": disp.created_at,
                },
            }
        )
        return disp

    def replace_disposition(
        self, disposition_id: str, substitute_digest: str
    ) -> Disposition:
        """将隔离中的受影响课程版本处置推进为替换。"""
        with self._lock:
            disp = self.dispositions.get(disposition_id)
            if disp is None:
                raise RuleError("处置记录不存在")
            if disp.action == ACTION_REPLACED:
                if disp.substitute_digest == substitute_digest:
                    return disp
                raise RuleError("该处置已替换为其他版本")
            if substitute_digest == disp.digest:
                raise RuleError("替换版本不能与被撤销版本相同")
            if substitute_digest not in self.tokens:
                raise RuleError("替换版本须已通过审校发布")
            if substitute_digest in self._revocation_by_digest:
                raise RuleError("替换版本自身已被撤销")
            replaced_at = _now()
            self.store.append(
                {
                    "type": "disposition_replaced",
                    "disposition_id": disposition_id,
                    "substitute_digest": substitute_digest,
                    "replaced_at": replaced_at,
                }
            )
            return self.dispositions[disposition_id]

    # ------------------------------------------------------------ 中断恢复

    def pending_work(self) -> dict:
        """汇总进程中断后仍需继续的事项。"""
        rechecks: List[str] = []
        adjudications: List[str] = []
        for (digest, category), slot in self._dc.items():
            if digest in self.tokens or digest in self._revocation_by_digest:
                continue
            adj = self._adj_by_dc.get((digest, category))
            if adj is not None and not adj.resolved():
                adjudications.append(adj.adjudication_id)
            elif slot["first"] is not None and slot["recheck"] is None:
                first = slot["first"]
                assert first is not None
                if first.risk_level == RISK_HIGH:
                    rechecks.append(
                        "{} / {}（{}）".format(digest[:12], CATEGORY_LABELS[category], first.opinion_id)
                    )
        quarantined = [
            d.disposition_id
            for d in self.dispositions.values()
            if d.action == ACTION_QUARANTINED
        ]
        return {
            "pending_rechecks": sorted(set(rechecks)),
            "open_adjudications": sorted(adjudications),
            "quarantined_awaiting_replacement": sorted(quarantined),
        }

    # ------------------------------------------------------------ 离线追溯

    def _version_trace(self, version: Version) -> dict:
        digest = version.digest
        opinions = []
        for status in self.review_progress(digest):
            for stage_name in ("first", "recheck"):
                op = status[stage_name]
                if op is not None:
                    reviewer = self.reviewers.get(op.reviewer_id)
                    opinions.append(
                        {
                            "opinion_id": op.opinion_id,
                            "category": CATEGORY_LABELS[op.category],
                            "stage": "初审" if op.stage == STAGE_FIRST else "复核",
                            "reviewer": reviewer.name if reviewer else op.reviewer_id,
                            "conclusion": op.conclusion,
                            "risk_level": op.risk_level,
                            "note": op.note,
                            "created_at": op.created_at,
                        }
                    )
            adj = status["adjudication"]
            if adj is not None:
                opinions.append(
                    {
                        "adjudication_id": adj.adjudication_id,
                        "category": CATEGORY_LABELS[adj.category],
                        "state": "已裁决：" + adj.outcome if adj.resolved() else "等待裁决",
                        "decider": adj.decider_id,
                        "rationale": adj.rationale,
                        "opened_at": adj.opened_at,
                        "resolved_at": adj.resolved_at,
                    }
                )

        packages = [
            {
                "package_id": pid,
                "version": ver,
                "title": self.packages[(pid, ver)].title,
            }
            for pid, ver in sorted(self._packages_by_digest.get(digest, []))
        ]
        revocation = None
        rev_id = self._revocation_by_digest.get(digest)
        if rev_id is not None:
            rev = self.revocations[rev_id]
            revocation = {
                "revocation_id": rev.revocation_id,
                "reason": rev.reason,
                "actor": rev.actor,
                "created_at": rev.created_at,
                "dispositions": [
                    {
                        "disposition_id": d.disposition_id,
                        "package_id": d.package_id,
                        "package_version": d.package_version,
                        "action": d.action,
                        "substitute_digest": d.substitute_digest,
                        "created_at": d.created_at,
                        "replaced_at": d.replaced_at,
                    }
                    for d in sorted(
                        (x for x in self.dispositions.values() if x.revocation_id == rev_id),
                        key=lambda x: x.created_at,
                    )
                ],
            }

        return {
            "digest": digest,
            "entity_id": version.entity_id,
            "revision": version.revision,
            "kind": version.kind,
            "title": version.title,
            "content": version.content,
            "summary": self.content_summary(version),
            "source": version.source.as_dict(),
            "author_kind": version.author_kind,
            "author": version.author,
            "parent_digest": version.parent_digest,
            "created_at": version.created_at,
            "published_token": self.tokens[digest].token if digest in self.tokens else None,
            "opinions_and_adjudications": opinions,
            "distributed_in": packages,
            "revocation": revocation,
        }

    def trace(self, query: str) -> dict:
        """按实体标识、内容指纹、标题或课程包标识追溯教学单元全貌。"""
        q = query.strip()
        if not q:
            raise RuleError("追溯标识不能为空")

        entity_versions = self.list_versions(q)
        if entity_versions:
            return {
                "query": q,
                "matched": "entity",
                "entity_id": q,
                "versions": [self._version_trace(v) for v in entity_versions],
            }
        if q in self.versions:
            return {
                "query": q,
                "matched": "digest",
                "versions": [self._version_trace(self.versions[q])],
            }
        package_keys = sorted(
            key for key in self.packages if key[0] == q or self.packages[key].title == q
        )
        if package_keys:
            return {
                "query": q,
                "matched": "course_package",
                "packages": [
                    {
                        "package_id": pid,
                        "title": self.packages[(pid, ver)].title,
                        "version": ver,
                        "digests": list(self.packages[(pid, ver)].digests),
                        "created_at": self.packages[(pid, ver)].created_at,
                    }
                    for pid, ver in package_keys
                ],
            }
        titled = [v for v in self.versions.values() if v.title == q]
        if titled:
            return {
                "query": q,
                "matched": "title",
                "versions": [self._version_trace(v) for v in titled],
            }
        raise RuleError(f"未找到与 {q} 匹配的教学实体、资源或课程包")

    # ------------------------------------------------------------ 事件重放

    def _handle(self, event: dict) -> None:
        etype = event["type"]
        if etype == "reviewer_registered":
            data = event["reviewer"]
            self.reviewers[data["reviewer_id"]] = Reviewer(
                data["reviewer_id"],
                data["name"],
                frozenset(data["qualifications"]),
                frozenset(data["senior_for"]),
                data["can_adjudicate"],
            )
        elif etype == "version_submitted":
            data = event["version"]
            version = Version(
                digest=data["digest"],
                entity_id=data["entity_id"],
                revision=data["revision"],
                kind=data["kind"],
                title=data["title"],
                content=data["content"],
                source=SourceClaim.from_dict(data["source"]),
                author_kind=data["author_kind"],
                author=data["author"],
                parent_digest=data["parent_digest"],
                summary_hash=data["summary_hash"],
                created_at=data["created_at"],
            )
            self.versions[data["digest"]] = version
            self._entity_digests[data["entity_id"]].append(data["digest"])
        elif etype == "opinion_recorded":
            data = event["opinion"]
            opinion = Opinion(**data)
            self._opinions[opinion.opinion_id] = opinion
            slot = self._slot(opinion.digest, opinion.category)
            slot[opinion.stage] = opinion
        elif etype == "adjudication_opened":
            data = event["adjudication"]
            adj = Adjudication(
                adjudication_id=data["adjudication_id"],
                digest=data["digest"],
                category=data["category"],
                first_opinion_id=data["first_opinion_id"],
                recheck_opinion_id=data["recheck_opinion_id"],
                opened_at=data["opened_at"],
            )
            self._adjudications[adj.adjudication_id] = adj
            self._adj_by_dc[(adj.digest, adj.category)] = adj
        elif etype == "adjudication_resolved":
            data = event
            adj = self._adjudications[data["adjudication_id"]]
            resolved = Adjudication(
                adjudication_id=adj.adjudication_id,
                digest=adj.digest,
                category=adj.category,
                first_opinion_id=adj.first_opinion_id,
                recheck_opinion_id=adj.recheck_opinion_id,
                opened_at=adj.opened_at,
                decider_id=data["decider_id"],
                outcome=data["outcome"],
                rationale=data["rationale"],
                resolved_at=data["resolved_at"],
            )
            self._adjudications[resolved.adjudication_id] = resolved
            self._adj_by_dc[(resolved.digest, resolved.category)] = resolved
        elif etype == "token_published":
            data = event["token"]
            self.tokens[data["digest"]] = TokenRecord(**data)
        elif etype == "course_packaged":
            data = event["package"]
            digests = tuple(data["digests"])
            package = CoursePackage(
                package_id=data["package_id"],
                title=data["title"],
                version=data["version"],
                digests=digests,
                created_at=data["created_at"],
            )
            key = (package.package_id, package.version)
            self.packages[key] = package
            for d in digests:
                self._packages_by_digest[d].append(key)
        elif etype == "resource_revoked":
            data = event["revocation"]
            rev = Revocation(**data)
            self.revocations[rev.revocation_id] = rev
            self._revocation_by_digest[rev.digest] = rev.revocation_id
        elif etype == "disposition_quarantined":
            data = event["disposition"]
            disp = Disposition(**data)
            self.dispositions[disp.disposition_id] = disp
            self._disp_by_triple[(disp.package_id, disp.package_version, disp.digest)] = disp
        elif etype == "disposition_replaced":
            data = event
            disp = self.dispositions[data["disposition_id"]]
            replaced = Disposition(
                disposition_id=disp.disposition_id,
                revocation_id=disp.revocation_id,
                package_id=disp.package_id,
                package_version=disp.package_version,
                digest=disp.digest,
                action=ACTION_REPLACED,
                created_at=disp.created_at,
                substitute_digest=data["substitute_digest"],
                replaced_at=data["replaced_at"],
            )
            self.dispositions[replaced.disposition_id] = replaced
            self._disp_by_triple[
                (replaced.package_id, replaced.package_version, replaced.digest)
            ] = replaced
        else:
            raise RuleError(f"未知事件类型：{etype}")
