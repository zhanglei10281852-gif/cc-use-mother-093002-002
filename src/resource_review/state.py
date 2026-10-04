"""状态投影：把事实链事件回放为可查询的内存状态。

投影只做状态归并，不做领域校验；所有不变量由服务层在追加事件前检查。
"""
from __future__ import annotations

from dataclasses import replace

from . import events as ev
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
from .models import (
    Adjudication,
    Conclusion,
    CourseEntry,
    CourseVersionRecord,
    Disposition,
    PublishToken,
    ResourceVersionRecord,
    ReviewAssignment,
    Reviewer,
    ReviewOpinion,
    Revocation,
    RereviewRecord,
    SourceDeclaration,
)


class DomainState:
    def __init__(self) -> None:
        self.declarations: dict[str, SourceDeclaration] = {}
        self.entity_kind: dict[str, ResourceKind] = {}
        self.entity_name: dict[str, str] = {}
        self.versions: dict[str, ResourceVersionRecord] = {}
        self.entity_versions: dict[str, list[str]] = {}
        self.reviewers: dict[str, Reviewer] = {}
        self.assignments: dict[str, dict[str, ReviewAssignment]] = {}
        self.opinions: dict[str, list[ReviewOpinion]] = {}
        self.opinion_index: dict[str, ReviewOpinion] = {}
        self.adjudications: dict[str, list[Adjudication]] = {}
        self.conclusions: dict[str, Conclusion] = {}
        self.version_conclusions: dict[str, list[str]] = {}
        self.tokens: dict[str, PublishToken] = {}
        self.courses: dict[str, list[CourseVersionRecord]] = {}
        self.revocations: dict[str, Revocation] = {}
        self.version_revoked: dict[str, str] = {}
        self.dispositions: dict[str, Disposition] = {}
        self.revocation_dispositions: dict[str, list[str]] = {}
        self.idempotency: dict[str, tuple[str, dict]] = {}
        self._seq_of: dict[str, int] = {}

    # ---- 查询辅助 ----

    def seq_of(self, record_id: str) -> int:
        return self._seq_of.get(record_id, 0)

    def latest_version(self, entity_id: str) -> ResourceVersionRecord | None:
        ids = self.entity_versions.get(entity_id)
        return self.versions[ids[-1]] if ids else None

    def effective_opinions(self, version_id: str) -> dict[str, ReviewOpinion]:
        """每个维度最新一条意见为有效意见。"""
        effective: dict[str, ReviewOpinion] = {}
        for opinion in self.opinions.get(version_id, []):
            effective[opinion.dimension.value] = opinion
        return effective

    def latest_conclusion(self, version_id: str) -> Conclusion | None:
        ids = self.version_conclusions.get(version_id)
        return self.conclusions[ids[-1]] if ids else None

    def active_token(self, entity_id: str) -> PublishToken | None:
        for token in self.tokens.values():
            if token.entity_id == entity_id and token.status is TokenStatus.ACTIVE:
                return token
        return None

    def course_revision(self, course_id: str, course_revision: int) -> CourseVersionRecord | None:
        for record in self.courses.get(course_id, []):
            if record.course_revision == course_revision:
                return record
        return None

    # ---- 事件回放 ----

    def apply(self, envelope: dict) -> None:
        etype = envelope["type"]
        payload = envelope["payload"]
        seq = envelope["seq"]
        handler = getattr(self, f"_on_{etype}", None)
        if handler is None:
            raise ValueError(f"未知事件类型: {etype}")
        handler(payload)
        key = payload.get("idempotency_key")
        if key:
            self.idempotency[key] = (etype, payload)
        for id_field in ("opinion_id", "adjudication_id", "conclusion_id"):
            if id_field in payload:
                self._seq_of[payload[id_field]] = seq

    def _on_source_declared(self, p: dict) -> None:
        self.declarations[p["declaration_id"]] = SourceDeclaration(
            declaration_id=p["declaration_id"],
            origin=Origin(p["origin"]),
            tool_name=p.get("tool_name", ""),
            operator=p.get("operator", ""),
            note=p.get("note", ""),
            declared_at=p.get("declared_at", ""),
        )

    def _on_resource_submitted(self, p: dict) -> None:
        record = ResourceVersionRecord(
            entity_id=p["entity_id"],
            version_id=p["version_id"],
            revision=p["revision"],
            kind=ResourceKind(p["kind"]),
            content=p["content"],
            digest=p["digest"],
            declaration_id=p["declaration_id"],
            derived_from_version_id=p.get("derived_from_version_id"),
            submitted_at=p.get("submitted_at", ""),
        )
        self.versions[record.version_id] = record
        self.entity_versions.setdefault(record.entity_id, []).append(record.version_id)
        self.entity_kind.setdefault(record.entity_id, record.kind)
        if p.get("display_name"):
            self.entity_name.setdefault(record.entity_id, p["display_name"])

    def _on_reviewer_registered(self, p: dict) -> None:
        self.reviewers[p["reviewer_id"]] = Reviewer(
            reviewer_id=p["reviewer_id"],
            name=p["name"],
            qualifications=frozenset(ReviewDimension(d) for d in p["qualifications"]),
        )

    def _on_reviewer_assigned(self, p: dict) -> None:
        assignment = ReviewAssignment(
            assignment_id=p["assignment_id"],
            entity_id=p["entity_id"],
            version_id=p["version_id"],
            dimension=ReviewDimension(p["dimension"]),
            reviewer_id=p["reviewer_id"],
            assigned_at=p.get("assigned_at", ""),
        )
        self.assignments.setdefault(p["version_id"], {})[p["dimension"]] = assignment

    def _on_opinion_recorded(self, p: dict) -> None:
        opinion = ReviewOpinion(
            opinion_id=p["opinion_id"],
            entity_id=p["entity_id"],
            version_id=p["version_id"],
            dimension=ReviewDimension(p["dimension"]),
            reviewer_id=p["reviewer_id"],
            risk_level=RiskLevel(p["risk_level"]),
            verdict=Verdict(p["verdict"]),
            rationale=p.get("rationale", ""),
            created_at=p.get("created_at", ""),
        )
        self.opinions.setdefault(p["version_id"], []).append(opinion)
        self.opinion_index[opinion.opinion_id] = opinion

    def _on_adjudication_recorded(self, p: dict) -> None:
        adjudication = Adjudication(
            adjudication_id=p["adjudication_id"],
            entity_id=p["entity_id"],
            version_id=p["version_id"],
            opinion_ids=tuple(p["opinion_ids"]),
            final_verdict=Verdict(p["final_verdict"]),
            final_risk_level=RiskLevel(p["final_risk_level"]),
            rationale=p.get("rationale", ""),
            decided_by=p.get("decided_by", ""),
            decided_at=p.get("decided_at", ""),
        )
        self.adjudications.setdefault(p["version_id"], []).append(adjudication)

    def _on_conclusion_reached(self, p: dict) -> None:
        conclusion = Conclusion(
            conclusion_id=p["conclusion_id"],
            entity_id=p["entity_id"],
            version_id=p["version_id"],
            digest=p["digest"],
            result=ConclusionResult(p["result"]),
            risk_level=RiskLevel(p["risk_level"]),
            status=ConclusionStatus(p["status"]),
            reached_at=p.get("reached_at", ""),
        )
        self.conclusions[conclusion.conclusion_id] = conclusion
        self.version_conclusions.setdefault(p["version_id"], []).append(conclusion.conclusion_id)

    def _on_conclusion_rereviewed(self, p: dict) -> None:
        conclusion = self.conclusions[p["conclusion_id"]]
        outcome = RereviewOutcome(p["outcome"])
        adjusted = RiskLevel(p["adjusted_risk_level"]) if p.get("adjusted_risk_level") else None
        record = RereviewRecord(
            reviewer_id=p["reviewer_id"],
            outcome=outcome,
            adjusted_risk_level=adjusted,
            rationale=p.get("rationale", ""),
            created_at=p.get("created_at", ""),
        )
        risk = adjusted if (outcome is RereviewOutcome.OVERTURNED and adjusted) else conclusion.risk_level
        self.conclusions[conclusion.conclusion_id] = replace(
            conclusion, status=ConclusionStatus.FINAL, risk_level=risk, rereview=record
        )

    def _on_publish_token_issued(self, p: dict) -> None:
        current = self.active_token(p["entity_id"])
        if current is not None:
            self.tokens[current.token_id] = replace(current, status=TokenStatus.SUPERSEDED)
        self.tokens[p["token_id"]] = PublishToken(
            token_id=p["token_id"],
            entity_id=p["entity_id"],
            version_id=p["version_id"],
            digest=p["digest"],
            conclusion_id=p["conclusion_id"],
            status=TokenStatus.ACTIVE,
            issued_at=p.get("issued_at", ""),
        )

    def _on_course_version_published(self, p: dict) -> None:
        revisions = self.courses.setdefault(p["course_id"], [])
        if revisions and revisions[-1].status is CourseVersionStatus.ACTIVE:
            revisions[-1] = replace(revisions[-1], status=CourseVersionStatus.SUPERSEDED)
        entries = tuple(
            CourseEntry(
                entity_id=e["entity_id"],
                version_id=e["version_id"],
                digest=e["digest"],
                token_id=e["token_id"],
            )
            for e in p["entries"]
        )
        revisions.append(
            CourseVersionRecord(
                course_id=p["course_id"],
                course_revision=p["course_revision"],
                entries=entries,
                status=CourseVersionStatus.ACTIVE,
                created_at=p.get("created_at", ""),
            )
        )

    def _on_course_version_status_changed(self, p: dict) -> None:
        record = self.course_revision(p["course_id"], p["course_revision"])
        if record is not None:
            updated = replace(record, status=CourseVersionStatus(p["status"]))
            revisions = self.courses[p["course_id"]]
            revisions[revisions.index(record)] = updated

    def _on_resource_revoked(self, p: dict) -> None:
        self.revocations[p["revocation_id"]] = Revocation(
            revocation_id=p["revocation_id"],
            entity_id=p["entity_id"],
            version_id=p["version_id"],
            digest=p["digest"],
            reason=p.get("reason", ""),
            revoked_by=p.get("revoked_by", ""),
            revoked_at=p.get("revoked_at", ""),
        )
        self.version_revoked[p["version_id"]] = p["revocation_id"]
        token = self.tokens.get(p.get("token_id") or "")
        if token is None:
            token = self.active_token(p["entity_id"])
        if token is not None and token.version_id == p["version_id"]:
            self.tokens[token.token_id] = replace(token, status=TokenStatus.REVOKED)

    def _on_disposition_created(self, p: dict) -> None:
        disposition = Disposition(
            disposition_id=p["disposition_id"],
            revocation_id=p["revocation_id"],
            entity_id=p["entity_id"],
            course_id=p["course_id"],
            course_revision=p["course_revision"],
            action=DispositionAction(p["action"]),
            status=DispositionStatus.PENDING,
            replacement_version_id=p.get("replacement_version_id"),
            resulting_course_revision=None,
            note="",
            created_at=p.get("created_at", ""),
            completed_at=None,
        )
        self.dispositions[disposition.disposition_id] = disposition
        self.revocation_dispositions.setdefault(p["revocation_id"], []).append(
            disposition.disposition_id
        )

    def _on_disposition_completed(self, p: dict) -> None:
        disposition = self.dispositions[p["disposition_id"]]
        self.dispositions[p["disposition_id"]] = replace(
            disposition,
            status=DispositionStatus.DONE,
            note=p.get("note", ""),
            resulting_course_revision=p.get("resulting_course_revision"),
            completed_at=p.get("completed_at", ""),
        )
