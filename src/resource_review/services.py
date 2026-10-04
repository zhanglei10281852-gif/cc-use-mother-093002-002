"""智能教学资源风险审校的应用服务。

每条命令先校验领域规则，再把事件原子追加到事实链（EventStore），
随后更新内存投影。已完成的命令立即持久化：即使审校进程中断，
重新打开服务后未完成的复核与处置仍可继续。

关键不变量：
- 每次生成或人工改写都保存为不可变版本并绑定内容摘要；
- 重复提交（同一幂等键，或同一实体内容摘要未变）不会制造另一条事实链；
- 审校人必须具备所审维度的资质，意见只能由被指派人提交；
- 高风险结论必须经第二名合格审校人复核后才生效；
- 意见冲突必须形成可追踪的裁决后才能得出结论；
- 发布令牌只能绑定审校通过时的摘要，签发必须携带幂等键；
- 撤销已发布资源会定位全部受影响课程版本并生成隔离/替换处置。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import events as ev
from .enums import (
    ConclusionResult,
    ConclusionStatus,
    CourseVersionStatus,
    DispositionAction,
    DispositionStatus,
    Origin,
    ResourceKind,
    ReviewDimension,
    RiskLevel,
    RereviewOutcome,
    Verdict,
    parse_enum,
)
from .errors import (
    IdempotencyConflictError,
    NotFoundError,
    QualificationError,
    StateError,
    ValidationError,
)
from .models import (
    Adjudication,
    Conclusion,
    CourseVersionRecord,
    Disposition,
    PublishToken,
    ResourceVersionRecord,
    ReviewAssignment,
    Reviewer,
    ReviewOpinion,
    Revocation,
    SourceDeclaration,
    content_digest,
    to_jsonable,
)
from .state import DomainState
from .store import EventStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ResourceReviewService:
    """风险审校服务门面：一个实例对应一条事实链。"""

    def __init__(self, store: EventStore):
        self._store = store
        self._state = DomainState()
        self._seq = 0
        self.sync()

    @classmethod
    def open(cls, path: str | Path) -> "ResourceReviewService":
        return cls(EventStore(path))

    @property
    def state(self) -> DomainState:
        return self._state

    def sync(self) -> None:
        """重新读取事实链并应用新事件（用于发现其他进程的决定）。"""
        seq, events = self._store.read()
        for envelope in events:
            if envelope["seq"] > self._seq:
                self._state.apply(envelope)
        self._seq = seq

    def _emit(self, *events: tuple[str, dict]) -> None:
        envelopes = self._store.append(
            self._seq, [{"type": etype, "payload": payload} for etype, payload in events]
        )
        for envelope in envelopes:
            self._state.apply(envelope)
        if envelopes:
            self._seq = envelopes[-1]["seq"]

    def _replay(self, key: str | None, event_type: str, match: dict) -> dict | None:
        """幂等键命中时返回原事件负载；键相同而内容不同则拒绝。"""
        if not key:
            return None
        hit = self._state.idempotency.get(key)
        if hit is None:
            return None
        etype, payload = hit
        if etype != event_type or any(payload.get(k) != v for k, v in match.items()):
            raise IdempotencyConflictError(
                f"幂等键 {key!r} 已绑定其他请求，重复提交不会生成新的事实链"
            )
        return payload

    # ---- 来源与版本 ----

    def declare_source(
        self,
        *,
        origin,
        tool_name: str = "",
        operator: str = "",
        note: str = "",
        declaration_id: str | None = None,
        idempotency_key: str | None = None,
        declared_at: str | None = None,
    ) -> SourceDeclaration:
        origin = parse_enum(Origin, origin)
        replay = self._replay(
            idempotency_key,
            ev.SOURCE_DECLARED,
            {"origin": origin.value, "tool_name": tool_name, "operator": operator, "note": note},
        )
        if replay:
            return self._state.declarations[replay["declaration_id"]]
        declaration_id = declaration_id or _new_id("SRC")
        if declaration_id in self._state.declarations:
            raise ValidationError(f"来源声明 {declaration_id} 已存在")
        payload = {
            "declaration_id": declaration_id,
            "origin": origin.value,
            "tool_name": tool_name,
            "operator": operator,
            "note": note,
            "declared_at": declared_at or _now(),
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        self._emit((ev.SOURCE_DECLARED, payload))
        return self._state.declarations[declaration_id]

    def submit_resource(
        self,
        *,
        kind,
        content: str,
        declaration_id: str,
        entity_id: str | None = None,
        display_name: str = "",
        derived_from_version_id: str | None = None,
        idempotency_key: str | None = None,
        submitted_at: str | None = None,
    ) -> ResourceVersionRecord:
        """提交一次生成或人工改写，保存为不可变版本。

        同一幂等键重放返回原版本；同一实体内容摘要未变化时也不产生新版本，
        重复提交不会制造另一条事实链。
        """
        kind = parse_enum(ResourceKind, kind)
        if not content.strip():
            raise ValidationError("内容不能为空")
        if declaration_id not in self._state.declarations:
            raise NotFoundError(f"来源声明不存在: {declaration_id}")
        match = {"kind": kind.value, "content": content, "declaration_id": declaration_id}
        if entity_id is not None:
            match["entity_id"] = entity_id
        replay = self._replay(idempotency_key, ev.RESOURCE_SUBMITTED, match)
        if replay:
            return self._state.versions[replay["version_id"]]
        digest = content_digest(kind, content)
        entity_id = entity_id or _new_id("RES")
        if entity_id in self._state.entity_kind:
            if self._state.entity_kind[entity_id] is not kind:
                raise ValidationError(f"资源实体 {entity_id} 的类型不可变更")
            latest = self._state.latest_version(entity_id)
            if latest is not None and latest.digest == digest:
                return latest  # 内容未变化：返回现有版本，不追加新事实
            revision = (latest.revision if latest else 0) + 1
        else:
            revision = 1
        if derived_from_version_id and derived_from_version_id not in self._state.versions:
            raise NotFoundError(f"改写来源版本不存在: {derived_from_version_id}")
        version_id = f"{entity_id}-v{revision}"
        payload = {
            "entity_id": entity_id,
            "version_id": version_id,
            "revision": revision,
            "kind": kind.value,
            "display_name": display_name,
            "content": content,
            "digest": digest,
            "declaration_id": declaration_id,
            "derived_from_version_id": derived_from_version_id,
            "submitted_at": submitted_at or _now(),
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        self._emit((ev.RESOURCE_SUBMITTED, payload))
        return self._state.versions[version_id]

    # ---- 审校人与分派 ----

    def register_reviewer(
        self, *, name: str, qualifications, reviewer_id: str | None = None
    ) -> Reviewer:
        quals = frozenset(parse_enum(ReviewDimension, q) for q in qualifications)
        if not name.strip():
            raise ValidationError("审校人姓名不能为空")
        reviewer_id = reviewer_id or _new_id("REV")
        if reviewer_id in self._state.reviewers:
            raise ValidationError(f"审校人 {reviewer_id} 已存在")
        payload = {
            "reviewer_id": reviewer_id,
            "name": name,
            "qualifications": sorted(q.value for q in quals),
        }
        self._emit((ev.REVIEWER_REGISTERED, payload))
        return self._state.reviewers[reviewer_id]

    def assign_reviewer(
        self,
        *,
        version_id: str,
        dimension,
        reviewer_id: str,
        assignment_id: str | None = None,
        assigned_at: str | None = None,
    ) -> ReviewAssignment:
        version = self._state.versions.get(version_id)
        if version is None:
            raise NotFoundError(f"资源版本不存在: {version_id}")
        dimension = parse_enum(ReviewDimension, dimension)
        reviewer = self._state.reviewers.get(reviewer_id)
        if reviewer is None:
            raise NotFoundError(f"审校人不存在: {reviewer_id}")
        if dimension not in reviewer.qualifications:
            raise QualificationError(
                f"审校人 {reviewer.name} 不具备「{dimension.value}」维度资质，不能分派"
            )
        existing = self._state.assignments.get(version_id, {}).get(dimension.value)
        if existing is not None:
            if existing.reviewer_id == reviewer_id:
                return existing
            raise StateError(f"版本 {version_id} 的「{dimension.value}」维度已分派其他审校人")
        payload = {
            "assignment_id": assignment_id or _new_id("ASG"),
            "entity_id": version.entity_id,
            "version_id": version_id,
            "dimension": dimension.value,
            "reviewer_id": reviewer_id,
            "assigned_at": assigned_at or _now(),
        }
        self._emit((ev.REVIEWER_ASSIGNED, payload))
        return self._state.assignments[version_id][dimension.value]

    # ---- 意见、裁决、结论、复核 ----

    def record_opinion(
        self,
        *,
        version_id: str,
        dimension,
        reviewer_id: str,
        risk_level,
        verdict,
        rationale: str = "",
        opinion_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: str | None = None,
    ) -> ReviewOpinion:
        version = self._state.versions.get(version_id)
        if version is None:
            raise NotFoundError(f"资源版本不存在: {version_id}")
        dimension = parse_enum(ReviewDimension, dimension)
        risk_level = parse_enum(RiskLevel, risk_level)
        verdict = parse_enum(Verdict, verdict)
        assignment = self._state.assignments.get(version_id, {}).get(dimension.value)
        if assignment is None:
            raise StateError(f"版本 {version_id} 的「{dimension.value}」维度尚未分派审校人")
        if assignment.reviewer_id != reviewer_id:
            raise StateError("审校意见只能由该维度被指派的审校人提交")
        replay = self._replay(
            idempotency_key,
            ev.OPINION_RECORDED,
            {
                "version_id": version_id,
                "dimension": dimension.value,
                "reviewer_id": reviewer_id,
                "risk_level": risk_level.value,
                "verdict": verdict.value,
            },
        )
        if replay:
            return self._state.opinion_index[replay["opinion_id"]]
        payload = {
            "opinion_id": opinion_id or _new_id("OPN"),
            "entity_id": version.entity_id,
            "version_id": version_id,
            "dimension": dimension.value,
            "reviewer_id": reviewer_id,
            "risk_level": risk_level.value,
            "verdict": verdict.value,
            "rationale": rationale,
            "created_at": created_at or _now(),
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        self._emit((ev.OPINION_RECORDED, payload))
        return self._state.opinion_index[payload["opinion_id"]]

    def adjudicate(
        self,
        *,
        version_id: str,
        opinion_ids,
        final_verdict,
        final_risk_level,
        rationale: str = "",
        decided_by: str = "",
        adjudication_id: str | None = None,
        decided_at: str | None = None,
    ) -> Adjudication:
        """对相互冲突的有效意见形成可追踪的裁决。"""
        version = self._state.versions.get(version_id)
        if version is None:
            raise NotFoundError(f"资源版本不存在: {version_id}")
        opinion_ids = tuple(opinion_ids)
        if not opinion_ids:
            raise ValidationError("裁决必须引用至少一条意见")
        effective = self._state.effective_opinions(version_id)
        referenced = []
        for oid in opinion_ids:
            opinion = self._state.opinion_index.get(oid)
            if opinion is None or opinion.version_id != version_id:
                raise ValidationError(f"意见 {oid} 不属于版本 {version_id}")
            if effective.get(opinion.dimension.value) is not opinion:
                raise StateError(f"意见 {oid} 已被更新的意见取代，不能作为裁决对象")
            referenced.append(opinion)
        if not any(not op.verdict.is_positive for op in referenced):
            raise StateError("所引意见均为通过，不存在需要裁决的冲突")
        final_verdict = parse_enum(Verdict, final_verdict)
        final_risk_level = parse_enum(RiskLevel, final_risk_level)
        payload = {
            "adjudication_id": adjudication_id or _new_id("ADJ"),
            "entity_id": version.entity_id,
            "version_id": version_id,
            "opinion_ids": list(opinion_ids),
            "final_verdict": final_verdict.value,
            "final_risk_level": final_risk_level.value,
            "rationale": rationale,
            "decided_by": decided_by,
            "decided_at": decided_at or _now(),
        }
        self._emit((ev.ADJUDICATION_RECORDED, payload))
        return self._state.adjudications[version_id][-1]

    def conclude(
        self,
        *,
        version_id: str,
        conclusion_id: str | None = None,
        reached_at: str | None = None,
    ) -> Conclusion:
        """汇总结论：全部已分派维度须有意见，冲突须已裁决。

        任一有效意见为高风险时，结论进入待复核状态，复核完成前不生效。
        """
        version = self._state.versions.get(version_id)
        if version is None:
            raise NotFoundError(f"资源版本不存在: {version_id}")
        assignments = self._state.assignments.get(version_id)
        if not assignments:
            raise StateError(f"版本 {version_id} 尚未分派任何审校人")
        effective = self._state.effective_opinions(version_id)
        missing = [d for d in assignments if d not in effective]
        if missing:
            raise StateError(f"版本 {version_id} 尚有维度未提交审校意见: {', '.join(missing)}")
        latest = self._state.latest_conclusion(version_id)
        input_seqs = [self._state.seq_of(op.opinion_id) for op in effective.values()]
        input_seqs += [
            self._state.seq_of(a.adjudication_id)
            for a in self._state.adjudications.get(version_id, [])
        ]
        if latest is not None and self._state.seq_of(latest.conclusion_id) >= max(input_seqs):
            return latest  # 意见与裁决均未变化：结论已是最新，不重复生成
        negatives = [op for op in effective.values() if not op.verdict.is_positive]
        if negatives:
            neg_ids = {op.opinion_id for op in negatives}
            neg_seq = max(self._state.seq_of(oid) for oid in neg_ids)
            covering = [
                a
                for a in self._state.adjudications.get(version_id, [])
                if neg_ids <= set(a.opinion_ids)
                and self._state.seq_of(a.adjudication_id) > neg_seq
            ]
            if not covering:
                raise StateError("存在未通过或相互冲突的审校意见，须先形成裁决")
            ruling = covering[-1]
            final_verdict = ruling.final_verdict
            risk = RiskLevel.highest(
                [op.risk_level for op in effective.values()] + [ruling.final_risk_level]
            )
        else:
            final_verdict = Verdict.APPROVE
            risk = RiskLevel.highest(op.risk_level for op in effective.values())
        result = ConclusionResult.PASSED if final_verdict.is_positive else ConclusionResult.REJECTED
        status = (
            ConclusionStatus.PENDING_REREVIEW if risk is RiskLevel.HIGH else ConclusionStatus.FINAL
        )
        payload = {
            "conclusion_id": conclusion_id or _new_id("CON"),
            "entity_id": version.entity_id,
            "version_id": version_id,
            "digest": version.digest,
            "result": result.value,
            "risk_level": risk.value,
            "status": status.value,
            "reached_at": reached_at or _now(),
        }
        self._emit((ev.CONCLUSION_REACHED, payload))
        return self._state.conclusions[payload["conclusion_id"]]

    def complete_rereview(
        self,
        *,
        conclusion_id: str,
        reviewer_id: str,
        outcome,
        adjusted_risk_level=None,
        rationale: str = "",
        created_at: str | None = None,
    ) -> Conclusion:
        """完成高风险结论的复核；复核人不得是该版本任何意见的作者。"""
        conclusion = self._state.conclusions.get(conclusion_id)
        if conclusion is None:
            raise NotFoundError(f"结论不存在: {conclusion_id}")
        if conclusion.status is not ConclusionStatus.PENDING_REREVIEW:
            raise StateError(f"结论 {conclusion_id} 不在待复核状态")
        reviewer = self._state.reviewers.get(reviewer_id)
        if reviewer is None:
            raise NotFoundError(f"审校人不存在: {reviewer_id}")
        dimensions = set(self._state.assignments.get(conclusion.version_id, {}))
        if not dimensions & {d.value for d in reviewer.qualifications}:
            raise QualificationError("复核人不具备该版本任一已审维度的资质")
        authors = {op.reviewer_id for op in self._state.opinions.get(conclusion.version_id, [])}
        if reviewer_id in authors:
            raise StateError("复核人不得是该版本审校意见的作者")
        outcome = parse_enum(RereviewOutcome, outcome)
        adjusted = parse_enum(RiskLevel, adjusted_risk_level) if adjusted_risk_level else None
        if outcome is RereviewOutcome.OVERTURNED:
            if adjusted not in (RiskLevel.LOW, RiskLevel.MEDIUM):
                raise ValidationError("推翻高风险结论时必须给出调整后的低/中风险等级")
        elif adjusted is not None:
            raise ValidationError("仅推翻结论时才能调整风险等级")
        payload = {
            "conclusion_id": conclusion_id,
            "entity_id": conclusion.entity_id,
            "version_id": conclusion.version_id,
            "reviewer_id": reviewer_id,
            "outcome": outcome.value,
            "adjusted_risk_level": adjusted.value if adjusted else None,
            "rationale": rationale,
            "created_at": created_at or _now(),
        }
        self._emit((ev.CONCLUSION_REREVIEWED, payload))
        return self._state.conclusions[conclusion_id]

    # ---- 发布与传播 ----

    def issue_publish_token(
        self,
        *,
        version_id: str,
        idempotency_key: str,
        token_id: str | None = None,
        issued_at: str | None = None,
    ) -> PublishToken:
        """签发发布令牌：只能绑定审校最终通过时的内容摘要。

        必须携带幂等键；同一版本重复签发、为更旧版本签发都会被拒绝，
        不会制造另一条事实链。
        """
        if not idempotency_key:
            raise ValidationError("签发发布令牌必须提供幂等键")
        version = self._state.versions.get(version_id)
        if version is None:
            raise NotFoundError(f"资源版本不存在: {version_id}")
        replay = self._replay(idempotency_key, ev.TOKEN_ISSUED, {"version_id": version_id})
        if replay:
            return self._state.tokens[replay["token_id"]]
        if version_id in self._state.version_revoked:
            raise StateError(f"版本 {version_id} 已被撤销，不得重新发布")
        conclusion = self._state.latest_conclusion(version_id)
        if (
            conclusion is None
            or conclusion.status is not ConclusionStatus.FINAL
            or conclusion.result is not ConclusionResult.PASSED
        ):
            raise StateError(f"版本 {version_id} 的审校未最终通过，不能签发发布令牌")
        active = self._state.active_token(version.entity_id)
        if active is not None:
            active_version = self._state.versions[active.version_id]
            if active.version_id == version_id:
                raise StateError("该版本已持有有效发布令牌，重复签发不会生成新令牌")
            if active_version.revision >= version.revision:
                raise StateError("不得为更旧的版本签发发布令牌")
        payload = {
            "token_id": token_id or _new_id("TOK"),
            "entity_id": version.entity_id,
            "version_id": version_id,
            "digest": conclusion.digest,
            "conclusion_id": conclusion.conclusion_id,
            "idempotency_key": idempotency_key,
            "issued_at": issued_at or _now(),
        }
        self._emit((ev.TOKEN_ISSUED, payload))
        return self._state.tokens[payload["token_id"]]

    def publish_course_version(
        self,
        *,
        course_id: str,
        entries,
        idempotency_key: str | None = None,
        created_at: str | None = None,
    ) -> CourseVersionRecord:
        """发布课程包版本：每个条目必须持有有效发布令牌。"""
        if not course_id.strip():
            raise ValidationError("课程标识不能为空")
        normalized = []
        for entry in entries:
            entity_id, version_id = (
                tuple(entry) if not isinstance(entry, dict) else (entry["entity_id"], entry["version_id"])
            )
            version = self._state.versions.get(version_id)
            if version is None or version.entity_id != entity_id:
                raise NotFoundError(f"资源版本不存在: {entity_id}/{version_id}")
            token = self._state.active_token(entity_id)
            if token is None or token.version_id != version_id:
                raise StateError(f"资源版本 {version_id} 未发布或发布令牌已失效，不能进入课程包")
            normalized.append(
                {
                    "entity_id": entity_id,
                    "version_id": version_id,
                    "digest": version.digest,
                    "token_id": token.token_id,
                }
            )
        if not normalized:
            raise ValidationError("课程包至少包含一个资源条目")
        replay = self._replay(
            idempotency_key, ev.COURSE_PUBLISHED, {"course_id": course_id, "entries": normalized}
        )
        if replay:
            return self._state.course_revision(course_id, replay["course_revision"])
        revision = len(self._state.courses.get(course_id, [])) + 1
        payload = {
            "course_id": course_id,
            "course_revision": revision,
            "entries": normalized,
            "created_at": created_at or _now(),
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        self._emit((ev.COURSE_PUBLISHED, payload))
        return self._state.course_revision(course_id, revision)

    # ---- 撤销与处置 ----

    def revoke_resource(
        self,
        *,
        entity_id: str,
        version_id: str | None = None,
        reason: str = "",
        revoked_by: str = "",
        action="quarantine",
        replacement_version_id: str | None = None,
        revocation_id: str | None = None,
        idempotency_key: str | None = None,
        revoked_at: str | None = None,
    ) -> tuple[Revocation, list[Disposition]]:
        """撤销已发布资源：令牌失效，定位受影响课程版本并生成处置。

        撤销事件与全部处置事件在同一批次原子落盘。
        """
        if entity_id not in self._state.entity_kind:
            raise NotFoundError(f"资源实体不存在: {entity_id}")
        action = parse_enum(DispositionAction, action)
        token = self._state.active_token(entity_id)
        version_id = version_id or (token.version_id if token else None)
        if version_id is None or token is None or token.version_id != version_id:
            raise StateError(f"资源 {entity_id} 未处于已发布状态，无法撤销")
        replay = self._replay(
            idempotency_key, ev.RESOURCE_REVOKED, {"entity_id": entity_id, "version_id": version_id}
        )
        if replay:
            revocation = self._state.revocations[replay["revocation_id"]]
            dispositions = [
                self._state.dispositions[did]
                for did in self._state.revocation_dispositions.get(revocation.revocation_id, [])
            ]
            return revocation, dispositions
        version = self._state.versions[version_id]
        if action is DispositionAction.REPLACE:
            if not replacement_version_id:
                raise ValidationError("替换处置必须指定替换版本")
            replacement = self._state.versions.get(replacement_version_id)
            if replacement is None or replacement.entity_id != entity_id:
                raise ValidationError("替换版本必须属于同一资源实体")
            if replacement_version_id == version_id:
                raise ValidationError("替换版本不能是被撤销版本本身")
            if replacement_version_id in self._state.version_revoked:
                raise StateError("替换版本已被撤销")
            conclusion = self._state.latest_conclusion(replacement_version_id)
            if (
                conclusion is None
                or conclusion.status is not ConclusionStatus.FINAL
                or conclusion.result is not ConclusionResult.PASSED
            ):
                raise StateError("替换版本尚未审校最终通过，不能用于替换")
        affected = [
            (record.course_id, record.course_revision)
            for revisions in self._state.courses.values()
            for record in revisions
            if record.status is CourseVersionStatus.ACTIVE
            and any(entry.digest == version.digest for entry in record.entries)
        ]
        revocation_id = revocation_id or _new_id("RVK")
        now = revoked_at or _now()
        events: list[tuple[str, dict]] = [
            (
                ev.RESOURCE_REVOKED,
                {
                    "revocation_id": revocation_id,
                    "entity_id": entity_id,
                    "version_id": version_id,
                    "digest": version.digest,
                    "reason": reason,
                    "revoked_by": revoked_by,
                    "token_id": token.token_id,
                    "revoked_at": now,
                    **({"idempotency_key": idempotency_key} if idempotency_key else {}),
                },
            )
        ]
        disposition_ids = []
        for course_id, course_revision in affected:
            disposition_id = _new_id("DIS")
            disposition_ids.append(disposition_id)
            events.append(
                (
                    ev.DISPOSITION_CREATED,
                    {
                        "disposition_id": disposition_id,
                        "revocation_id": revocation_id,
                        "entity_id": entity_id,
                        "course_id": course_id,
                        "course_revision": course_revision,
                        "action": action.value,
                        "replacement_version_id": replacement_version_id,
                        "created_at": now,
                    },
                )
            )
        self._emit(*events)
        return (
            self._state.revocations[revocation_id],
            [self._state.dispositions[did] for did in disposition_ids],
        )

    def complete_disposition(
        self, *, disposition_id: str, note: str = "", completed_at: str | None = None
    ) -> Disposition:
        """执行处置：隔离受影响课程版本，或用审校通过的版本替换。"""
        disposition = self._state.dispositions.get(disposition_id)
        if disposition is None:
            raise NotFoundError(f"处置不存在: {disposition_id}")
        if disposition.status is not DispositionStatus.PENDING:
            raise StateError(f"处置 {disposition_id} 已完成，不能重复执行")
        now = completed_at or _now()
        events: list[tuple[str, dict]] = []
        resulting_revision = None
        if disposition.action is DispositionAction.REPLACE:
            replacement = self._state.versions.get(disposition.replacement_version_id or "")
            if replacement is None:
                raise StateError("替换版本不存在，无法完成替换处置")
            if replacement.version_id in self._state.version_revoked:
                raise StateError("替换版本已被撤销，无法完成替换处置")
            conclusion = self._state.latest_conclusion(replacement.version_id)
            if (
                conclusion is None
                or conclusion.status is not ConclusionStatus.FINAL
                or conclusion.result is not ConclusionResult.PASSED
            ):
                raise StateError("替换版本的审校结论已失效，无法完成替换处置")
            token_id = _new_id("TOK")
            events.append(
                (
                    ev.TOKEN_ISSUED,
                    {
                        "token_id": token_id,
                        "entity_id": replacement.entity_id,
                        "version_id": replacement.version_id,
                        "digest": conclusion.digest,
                        "conclusion_id": conclusion.conclusion_id,
                        "idempotency_key": f"replace:{disposition_id}",
                        "issued_at": now,
                    },
                )
            )
            old_record = self._state.course_revision(
                disposition.course_id, disposition.course_revision
            )
            revoked_digest = self._state.revocations[disposition.revocation_id].digest
            new_entries = [
                {
                    "entity_id": replacement.entity_id,
                    "version_id": replacement.version_id,
                    "digest": replacement.digest,
                    "token_id": token_id,
                }
                if entry.digest == revoked_digest
                else {
                    "entity_id": entry.entity_id,
                    "version_id": entry.version_id,
                    "digest": entry.digest,
                    "token_id": entry.token_id,
                }
                for entry in (old_record.entries if old_record else ())
            ]
            resulting_revision = len(self._state.courses.get(disposition.course_id, [])) + 1
            events.append(
                (
                    ev.COURSE_PUBLISHED,
                    {
                        "course_id": disposition.course_id,
                        "course_revision": resulting_revision,
                        "entries": new_entries,
                        "created_at": now,
                    },
                )
            )
        else:
            record = self._state.course_revision(disposition.course_id, disposition.course_revision)
            if record is not None and record.status is CourseVersionStatus.ACTIVE:
                events.append(
                    (
                        ev.COURSE_STATUS_CHANGED,
                        {
                            "course_id": disposition.course_id,
                            "course_revision": disposition.course_revision,
                            "status": CourseVersionStatus.QUARANTINED.value,
                            "reason": f"撤销 {disposition.revocation_id} 的隔离处置",
                            "changed_at": now,
                        },
                    )
                )
        events.append(
            (
                ev.DISPOSITION_COMPLETED,
                {
                    "disposition_id": disposition_id,
                    "entity_id": disposition.entity_id,
                    "note": note,
                    "resulting_course_revision": resulting_revision,
                    "completed_at": now,
                },
            )
        )
        self._emit(*events)
        return self._state.dispositions[disposition_id]

    # ---- 待办与追踪 ----

    def pending_rereviews(self) -> list[Conclusion]:
        """未完成的高风险复核：进程中断重开后仍可继续。"""
        pending = [
            c
            for c in self._state.conclusions.values()
            if c.status is ConclusionStatus.PENDING_REREVIEW
        ]
        return sorted(pending, key=lambda c: c.reached_at)

    def pending_dispositions(self) -> list[Disposition]:
        pending = [
            d
            for d in self._state.dispositions.values()
            if d.status is DispositionStatus.PENDING
        ]
        return sorted(pending, key=lambda d: d.created_at)

    def incomplete_versions(self) -> list[dict]:
        """已分派但意见未齐的版本。"""
        result = []
        for version_id, assignments in self._state.assignments.items():
            effective = self._state.effective_opinions(version_id)
            missing = [d for d in assignments if d not in effective]
            if missing:
                version = self._state.versions[version_id]
                result.append(
                    {
                        "entity_id": version.entity_id,
                        "version_id": version_id,
                        "missing_dimensions": sorted(missing),
                    }
                )
        return sorted(result, key=lambda item: item["version_id"])

    def pending_work(self) -> dict:
        return {
            "pending_rereviews": to_jsonable(self.pending_rereviews()),
            "pending_dispositions": to_jsonable(self.pending_dispositions()),
            "incomplete_versions": self.incomplete_versions(),
        }

    def propagation(self, entity_id: str) -> list[dict]:
        """传播范围：所有课程版本中含有该资源任一摘要的记录。"""
        records = []
        for revisions in self._state.courses.values():
            for record in revisions:
                for entry in record.entries:
                    if entry.entity_id == entity_id:
                        records.append(
                            {
                                "course_id": record.course_id,
                                "course_revision": record.course_revision,
                                "course_status": record.status.value,
                                "version_id": entry.version_id,
                                "digest": entry.digest,
                            }
                        )
        return sorted(records, key=lambda r: (r["course_id"], r["course_revision"]))

    def origin_chain(self, entity_id: str) -> list[dict]:
        """原始来源链：从首个版本沿改写来源回溯到根。"""
        ids = self._state.entity_versions.get(entity_id)
        if not ids:
            raise NotFoundError(f"资源实体不存在: {entity_id}")
        chain = []
        seen = set()
        version = self._state.versions[ids[0]]
        while version is not None and version.version_id not in seen:
            seen.add(version.version_id)
            declaration = self._state.declarations.get(version.declaration_id)
            chain.append(
                {
                    "entity_id": version.entity_id,
                    "version_id": version.version_id,
                    "revision": version.revision,
                    "digest": version.digest,
                    "declaration": to_jsonable(declaration) if declaration else None,
                }
            )
            version = (
                self._state.versions.get(version.derived_from_version_id)
                if version.derived_from_version_id
                else None
            )
        return chain

    def trace(self, entity_id: str) -> dict:
        """追踪一个教学单元：原始来源、历次意见、传播范围、撤销后的每项处置。"""
        ids = self._state.entity_versions.get(entity_id)
        if not ids:
            raise NotFoundError(f"资源实体不存在: {entity_id}")
        versions = []
        for version_id in ids:
            version = self._state.versions[version_id]
            declaration = self._state.declarations.get(version.declaration_id)
            token = next(
                (t for t in self._state.tokens.values() if t.version_id == version_id), None
            )
            versions.append(
                {
                    "version_id": version_id,
                    "revision": version.revision,
                    "kind": version.kind.value,
                    "content": version.content,
                    "digest": version.digest,
                    "submitted_at": version.submitted_at,
                    "source": to_jsonable(declaration) if declaration else None,
                    "derived_from_version_id": version.derived_from_version_id,
                    "assignments": to_jsonable(
                        sorted(
                            self._state.assignments.get(version_id, {}).values(),
                            key=lambda a: a.dimension.value,
                        )
                    ),
                    "opinions": to_jsonable(self._state.opinions.get(version_id, [])),
                    "adjudications": to_jsonable(self._state.adjudications.get(version_id, [])),
                    "conclusions": to_jsonable(
                        [
                            self._state.conclusions[cid]
                            for cid in self._state.version_conclusions.get(version_id, [])
                        ]
                    ),
                    "publish_token": to_jsonable(token) if token else None,
                    "revoked": version_id in self._state.version_revoked,
                }
            )
        revocations = []
        for revocation in self._state.revocations.values():
            if revocation.entity_id != entity_id:
                continue
            dispositions = [
                self._state.dispositions[did]
                for did in self._state.revocation_dispositions.get(revocation.revocation_id, [])
            ]
            revocations.append(
                {**to_jsonable(revocation), "dispositions": to_jsonable(dispositions)}
            )
        revocations.sort(key=lambda r: r["revoked_at"])
        return {
            "entity_id": entity_id,
            "display_name": self._state.entity_name.get(entity_id, ""),
            "kind": self._state.entity_kind[entity_id].value,
            "versions": versions,
            "origin_chain": self.origin_chain(entity_id),
            "propagation": self.propagation(entity_id),
            "revocations": revocations,
        }
