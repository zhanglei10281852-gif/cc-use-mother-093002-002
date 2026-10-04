import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from resource_review import (
    ConclusionResult,
    ConclusionStatus,
    ConcurrencyConflictError,
    CourseVersionStatus,
    DispositionAction,
    DispositionStatus,
    IdempotencyConflictError,
    Origin,
    QualificationError,
    ResourceKind,
    ResourceReviewService,
    ReviewDimension,
    RiskLevel,
    StateError,
    TokenStatus,
    ValidationError,
    Verdict,
    content_digest,
)

DIMS = [
    ReviewDimension.LANGUAGE,
    ReviewDimension.CULTURE,
    ReviewDimension.FACT,
    ReviewDimension.COPYRIGHT,
    ReviewDimension.PEDAGOGY,
]


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.jsonl"
        self.svc = ResourceReviewService.open(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def reopen(self):
        self.svc = ResourceReviewService.open(self.path)

    def declare(self, did="SRC-1", origin=Origin.GENERATED):
        return self.svc.declare_source(
            origin=origin, tool_name="某生成工具", operator="张老师", declaration_id=did
        )

    def submit(self, entity="E-1", content="例句：泼水节大家互相泼水。", did="SRC-1", **kw):
        return self.svc.submit_resource(
            kind="lecture_unit",
            content=content,
            declaration_id=did,
            entity_id=entity,
            display_name="第一课",
            **kw,
        )

    def make_reviewers(self):
        for i, dim in enumerate(DIMS, start=1):
            self.svc.register_reviewer(
                name=f"审校{i}", qualifications=[dim], reviewer_id=f"REV-{dim.value}"
            )

    def assign_all(self, version_id):
        for dim in DIMS:
            self.svc.assign_reviewer(
                version_id=version_id, dimension=dim, reviewer_id=f"REV-{dim.value}"
            )

    def opine(self, version_id, risk=RiskLevel.LOW, verdict=Verdict.APPROVE, skip=()):
        for dim in DIMS:
            if dim in skip:
                continue
            self.svc.record_opinion(
                version_id=version_id,
                dimension=dim,
                reviewer_id=f"REV-{dim.value}",
                risk_level=risk,
                verdict=verdict,
                rationale=f"{dim.value}维度意见",
            )

    def approved_version(self, entity="E-1", content="例句：泼水节大家互相泼水。"):
        self.declare()
        version = self.submit(entity=entity, content=content)
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id)
        conclusion = self.svc.conclude(version_id=version.version_id)
        return version, conclusion


class VersionTests(ServiceTestBase):
    def test_submit_creates_immutable_version_with_digest(self):
        self.declare()
        v1 = self.submit()
        self.assertEqual(v1.revision, 1)
        self.assertEqual(v1.version_id, "E-1-v1")
        self.assertEqual(v1.digest, content_digest(ResourceKind.LECTURE_UNIT, v1.content))
        with self.assertRaises(Exception):
            v1.content = "篡改"  # 冻结数据类：不可变版本不允许改写
        v2 = self.submit(content="例句：水灯节人们放水灯。")
        self.assertEqual(v2.revision, 2)
        self.assertNotEqual(v1.digest, v2.digest)

    def test_entity_kind_is_immutable(self):
        self.declare()
        self.submit()
        with self.assertRaises(ValidationError):
            self.svc.submit_resource(
                kind="question", content="新题目", declaration_id="SRC-1", entity_id="E-1"
            )

    def test_duplicate_submission_does_not_fork_fact_chain(self):
        self.declare()
        v1 = self.submit(idempotency_key="sub-1")
        again = self.submit(idempotency_key="sub-1")
        self.assertEqual(v1.version_id, again.version_id)
        dedup = self.submit(idempotency_key="sub-2")  # 内容未变，不同幂等键
        self.assertEqual(v1.version_id, dedup.version_id)
        self.assertEqual(len(self.svc.state.entity_versions["E-1"]), 1)
        with self.assertRaises(IdempotencyConflictError):
            self.submit(content="不同内容", idempotency_key="sub-1")

    def test_derived_version_keeps_origin_chain(self):
        self.declare("SRC-G", Origin.GENERATED)
        self.declare("SRC-M", Origin.MANUAL)
        v1 = self.submit(did="SRC-G")
        v2 = self.svc.submit_resource(
            kind="lecture_unit",
            content="人工改写后的例句。",
            declaration_id="SRC-M",
            entity_id="E-2",
            derived_from_version_id=v1.version_id,
        )
        chain = self.svc.origin_chain("E-2")
        self.assertEqual(chain[0]["version_id"], v2.version_id)
        self.assertEqual(chain[0]["declaration"]["origin"], "manual")
        self.assertEqual(chain[1]["version_id"], v1.version_id)
        self.assertEqual(chain[1]["declaration"]["origin"], "generated")


class ReviewFlowTests(ServiceTestBase):
    def test_assignment_requires_qualification(self):
        self.declare()
        version = self.submit()
        self.svc.register_reviewer(name="只懂语言", qualifications=["语言"], reviewer_id="REV-L")
        with self.assertRaises(QualificationError):
            self.svc.assign_reviewer(
                version_id=version.version_id, dimension="文化", reviewer_id="REV-L"
            )

    def test_opinion_only_from_assignee(self):
        self.declare()
        version = self.submit()
        self.make_reviewers()
        with self.assertRaises(StateError):
            self.svc.record_opinion(
                version_id=version.version_id,
                dimension="culture",
                reviewer_id="REV-culture",
                risk_level="low",
                verdict="approve",
            )
        self.svc.assign_reviewer(
            version_id=version.version_id, dimension="culture", reviewer_id="REV-culture"
        )
        with self.assertRaises(StateError):
            self.svc.record_opinion(
                version_id=version.version_id,
                dimension="culture",
                reviewer_id="REV-fact",
                risk_level="low",
                verdict="approve",
            )

    def test_conclude_waits_for_all_assigned_dimensions(self):
        self.declare()
        version = self.submit()
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id, skip={ReviewDimension.PEDAGOGY})
        with self.assertRaises(StateError):
            self.svc.conclude(version_id=version.version_id)

    def test_high_risk_conclusion_requires_rereview(self):
        self.declare()
        version = self.submit()
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id)
        self.svc.record_opinion(
            version_id=version.version_id,
            dimension="culture",
            reviewer_id="REV-culture",
            risk_level="high",
            verdict="approve",
            rationale="地区习俗描述存疑",
        )
        conclusion = self.svc.conclude(version_id=version.version_id)
        self.assertEqual(conclusion.status, ConclusionStatus.PENDING_REREVIEW)
        self.assertEqual(conclusion.risk_level, RiskLevel.HIGH)
        with self.assertRaises(StateError):
            self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        # 原意见作者不能充当复核人
        with self.assertRaises(StateError):
            self.svc.complete_rereview(
                conclusion_id=conclusion.conclusion_id,
                reviewer_id="REV-culture",
                outcome="confirmed",
            )
        self.svc.register_reviewer(
            name="复核人", qualifications=["culture", "fact"], reviewer_id="REV-CHK"
        )
        done = self.svc.complete_rereview(
            conclusion_id=conclusion.conclusion_id, reviewer_id="REV-CHK", outcome="确认"
        )
        self.assertEqual(done.status, ConclusionStatus.FINAL)
        token = self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        self.assertEqual(token.digest, version.digest)

    def test_overturned_rereview_adjusts_risk(self):
        self.declare()
        version = self.submit()
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id, risk=RiskLevel.HIGH)
        conclusion = self.svc.conclude(version_id=version.version_id)
        self.svc.register_reviewer(name="复核人", qualifications=["fact"], reviewer_id="REV-CHK")
        with self.assertRaises(ValidationError):
            self.svc.complete_rereview(
                conclusion_id=conclusion.conclusion_id,
                reviewer_id="REV-CHK",
                outcome="overturned",
            )
        done = self.svc.complete_rereview(
            conclusion_id=conclusion.conclusion_id,
            reviewer_id="REV-CHK",
            outcome="推翻",
            adjusted_risk_level="中",
        )
        self.assertEqual(done.risk_level, RiskLevel.MEDIUM)
        self.assertEqual(done.status, ConclusionStatus.FINAL)

    def test_conflicting_opinions_need_traceable_adjudication(self):
        self.declare()
        version = self.submit()
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id)
        conflict = self.svc.record_opinion(
            version_id=version.version_id,
            dimension="copyright",
            reviewer_id="REV-copyright",
            risk_level="medium",
            verdict="reject",
            rationale="例句疑似整段摘自他人教材",
        )
        with self.assertRaises(StateError):
            self.svc.conclude(version_id=version.version_id)
        ruling = self.svc.adjudicate(
            version_id=version.version_id,
            opinion_ids=[conflict.opinion_id],
            final_verdict="approve",
            final_risk_level="low",
            rationale="已核对为公有领域语料",
            decided_by="教研负责人",
        )
        self.assertIn(conflict.opinion_id, ruling.opinion_ids)
        conclusion = self.svc.conclude(version_id=version.version_id)
        self.assertEqual(conclusion.result, ConclusionResult.PASSED)
        self.assertEqual(conclusion.status, ConclusionStatus.FINAL)
        trace = self.svc.trace("E-1")
        adjudications = trace["versions"][0]["adjudications"]
        self.assertEqual(adjudications[0]["decided_by"], "教研负责人")

    def test_adjudication_rejected_blocks_publish(self):
        self.declare()
        version = self.submit()
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id)
        conflict = self.svc.record_opinion(
            version_id=version.version_id,
            dimension="fact",
            reviewer_id="REV-fact",
            risk_level="high",
            verdict="reject",
        )
        self.svc.adjudicate(
            version_id=version.version_id,
            opinion_ids=[conflict.opinion_id],
            final_verdict="reject",
            final_risk_level="high",
        )
        conclusion = self.svc.conclude(version_id=version.version_id)
        self.assertEqual(conclusion.result, ConclusionResult.REJECTED)
        self.assertEqual(conclusion.status, ConclusionStatus.PENDING_REREVIEW)


class PublishTokenTests(ServiceTestBase):
    def test_token_binds_passed_digest_and_requires_key(self):
        version, _ = self.approved_version()
        with self.assertRaises(ValidationError):
            self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="")
        token = self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        self.assertEqual(token.digest, version.digest)
        self.assertEqual(token.status, TokenStatus.ACTIVE)
        replay = self.svc.issue_publish_token(
            version_id=version.version_id, idempotency_key="pub-1"
        )
        self.assertEqual(token.token_id, replay.token_id)
        with self.assertRaises(StateError):
            self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-2")

    def test_new_version_supersedes_old_token(self):
        v1, _ = self.approved_version()
        t1 = self.svc.issue_publish_token(version_id=v1.version_id, idempotency_key="pub-1")
        v2 = self.submit(content="修订后的例句。")
        self.assign_all(v2.version_id)
        self.opine(v2.version_id)
        self.svc.conclude(version_id=v2.version_id)
        t2 = self.svc.issue_publish_token(version_id=v2.version_id, idempotency_key="pub-2")
        self.assertEqual(self.svc.state.tokens[t1.token_id].status, TokenStatus.SUPERSEDED)
        self.assertEqual(t2.digest, v2.digest)
        with self.assertRaises(StateError):
            self.svc.issue_publish_token(version_id=v1.version_id, idempotency_key="pub-3")

    def test_concurrent_decision_cannot_fork_fact_chain(self):
        version, _ = self.approved_version()
        other = ResourceReviewService.open(self.path)
        self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        with self.assertRaises(ConcurrencyConflictError):
            other.issue_publish_token(version_id=version.version_id, idempotency_key="pub-2")
        other.sync()
        token = other.state.active_token("E-1")
        self.assertIsNotNone(token)


class CourseAndRevocationTests(ServiceTestBase):
    def published_version(self):
        version, _ = self.approved_version()
        token = self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        return version, token

    def test_course_entry_requires_active_token(self):
        version, _ = self.approved_version()
        with self.assertRaises(StateError):
            self.svc.publish_course_version(course_id="C-1", entries=[("E-1", version.version_id)])

    def test_revocation_locates_courses_and_quarantines(self):
        version, token = self.published_version()
        self.svc.publish_course_version(course_id="C-1", entries=[("E-1", version.version_id)])
        self.svc.publish_course_version(course_id="C-2", entries=[("E-1", version.version_id)])
        revocation, dispositions = self.svc.revoke_resource(
            entity_id="E-1", reason="地区习俗解释错误", revoked_by="教研负责人"
        )
        self.assertEqual(len(dispositions), 2)
        self.assertIsNone(self.svc.state.active_token("E-1"))
        self.assertEqual(self.svc.state.tokens[token.token_id].status, TokenStatus.REVOKED)
        affected = {(d.course_id, d.course_revision) for d in dispositions}
        self.assertEqual(affected, {("C-1", 1), ("C-2", 1)})
        done = self.svc.complete_disposition(
            disposition_id=dispositions[0].disposition_id, note="已隔离"
        )
        self.assertEqual(done.status, DispositionStatus.DONE)
        record = self.svc.state.course_revision(done.course_id, done.course_revision)
        self.assertEqual(record.status, CourseVersionStatus.QUARANTINED)
        with self.assertRaises(StateError):
            self.svc.complete_disposition(disposition_id=done.disposition_id)
        # 已撤销版本不得再进入新课程包
        with self.assertRaises(StateError):
            self.svc.publish_course_version(course_id="C-3", entries=[("E-1", version.version_id)])

    def test_revocation_replace_flow(self):
        v1, _ = self.published_version()
        self.svc.publish_course_version(course_id="C-1", entries=[("E-1", v1.version_id)])
        v2 = self.submit(content="更正后的例句。")
        self.assign_all(v2.version_id)
        self.opine(v2.version_id)
        self.svc.conclude(version_id=v2.version_id)
        revocation, dispositions = self.svc.revoke_resource(
            entity_id="E-1",
            reason="内容错误",
            action="replace",
            replacement_version_id=v2.version_id,
        )
        self.assertEqual(dispositions[0].action, DispositionAction.REPLACE)
        done = self.svc.complete_disposition(disposition_id=dispositions[0].disposition_id)
        self.assertEqual(done.resulting_course_revision, 2)
        new_record = self.svc.state.course_revision("C-1", 2)
        self.assertEqual(new_record.entries[0].digest, v2.digest)
        self.assertEqual(
            self.svc.state.course_revision("C-1", 1).status, CourseVersionStatus.SUPERSEDED
        )
        active = self.svc.state.active_token("E-1")
        self.assertEqual(active.version_id, v2.version_id)
        self.assertEqual(active.digest, v2.digest)

    def test_revoke_unpublished_is_rejected(self):
        self.approved_version()
        with self.assertRaises(StateError):
            self.svc.revoke_resource(entity_id="E-1", reason="未发布")


class RecoveryTests(ServiceTestBase):
    def test_pending_rereview_survives_restart(self):
        self.declare()
        version = self.submit()
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id, risk=RiskLevel.HIGH)
        conclusion = self.svc.conclude(version_id=version.version_id)
        # 模拟审校进程中断：丢弃内存状态，重新打开
        self.reopen()
        pending = self.svc.pending_rereviews()
        self.assertEqual([c.conclusion_id for c in pending], [conclusion.conclusion_id])
        self.svc.register_reviewer(name="复核人", qualifications=["fact"], reviewer_id="REV-CHK")
        done = self.svc.complete_rereview(
            conclusion_id=conclusion.conclusion_id, reviewer_id="REV-CHK", outcome="confirmed"
        )
        self.assertEqual(done.status, ConclusionStatus.FINAL)
        token = self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        self.assertEqual(token.digest, version.digest)

    def test_pending_disposition_survives_restart(self):
        version, _ = self.approved_version()
        self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        self.svc.publish_course_version(course_id="C-1", entries=[("E-1", version.version_id)])
        _, dispositions = self.svc.revoke_resource(entity_id="E-1", reason="错误")
        self.reopen()
        pending = self.svc.pending_dispositions()
        self.assertEqual([d.disposition_id for d in pending], [dispositions[0].disposition_id])
        done = self.svc.complete_disposition(disposition_id=pending[0].disposition_id)
        self.assertEqual(done.status, DispositionStatus.DONE)

    def test_corrupt_tail_is_ignored(self):
        version, _ = self.approved_version()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"seq": 99, "type": "conclu')  # 崩溃残留的半行
        self.reopen()
        self.assertEqual(self.svc.state.latest_conclusion(version.version_id).result,
                         ConclusionResult.PASSED)


class TraceTests(ServiceTestBase):
    def test_trace_reports_origin_opinions_propagation_and_dispositions(self):
        self.declare("SRC-G", Origin.GENERATED)
        version = self.submit(did="SRC-G")
        self.make_reviewers()
        self.assign_all(version.version_id)
        self.opine(version.version_id)
        self.svc.record_opinion(
            version_id=version.version_id,
            dimension="culture",
            reviewer_id="REV-culture",
            risk_level="medium",
            verdict="needs_revision",
            rationale="地区习俗解释需核实",
        )
        conflict = self.svc.state.effective_opinions(version.version_id)["culture"]
        self.svc.adjudicate(
            version_id=version.version_id,
            opinion_ids=[conflict.opinion_id],
            final_verdict="approve",
            final_risk_level="low",
            decided_by="教研负责人",
        )
        self.svc.conclude(version_id=version.version_id)
        self.svc.issue_publish_token(version_id=version.version_id, idempotency_key="pub-1")
        self.svc.publish_course_version(course_id="C-1", entries=[("E-1", version.version_id)])
        self.svc.revoke_resource(entity_id="E-1", reason="习俗解释确认有误")

        trace = self.svc.trace("E-1")
        self.assertEqual(trace["kind"], "lecture_unit")
        self.assertEqual(trace["origin_chain"][0]["declaration"]["origin"], "generated")
        opinions = trace["versions"][0]["opinions"]
        self.assertEqual(len(opinions), 6)  # 五个维度 + 文化维度补充意见
        self.assertEqual(len(trace["versions"][0]["adjudications"]), 1)
        self.assertEqual(trace["propagation"][0]["course_id"], "C-1")
        self.assertTrue(trace["versions"][0]["revoked"])
        dispositions = trace["revocations"][0]["dispositions"]
        self.assertEqual(dispositions[0]["action"], "quarantine")
        self.assertEqual(dispositions[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
