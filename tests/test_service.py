"""审校服务核心规则测试。"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from resource_review.models import (  # noqa: E402
    CATEGORY_CULTURE,
    CATEGORY_FACT,
    CATEGORY_LANGUAGE,
    SourceClaim,
)
from resource_review.service import (  # noqa: E402
    ADJ_UPHOLD_FIRST,
    RuleError,
    ReviewService,
)
from resource_review.store import EventStore  # noqa: E402


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = ReviewService(EventStore(Path(self.tmp.name) / "review.jsonl"))
        # 林老师：语言+文化，文化资深；赵老师：文化资深（可复核）；
        # 陈老师：事实/版权/适切性资深；负责人可裁决
        self.lin = self.svc.register_reviewer(
            "RV-LIN", "林老师", ["language", "culture"], ["culture"]
        )
        self.zhao = self.svc.register_reviewer(
            "RV-ZHAO", "赵老师", ["culture"], ["culture"]
        )
        self.chen = self.svc.register_reviewer(
            "RV-CHEN", "陈老师",
            ["fact", "copyright", "suitability"],
            ["fact", "copyright", "suitability"],
        )
        self.head = self.svc.register_reviewer(
            "RV-HEAD", "负责人", ["culture"], ["culture"], can_adjudicate=True
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def source(self, **kw) -> SourceClaim:
        base = {"title": "东南亚民俗讲义", "location": "图书馆3楼-民俗-12"}
        base.update(kw)
        return SourceClaim(**base)

    def submit_v1(self, content="泼水节人们向彼此泼洒清水表示祝福。", entity="E-1", **kw):
        version, _ = self.svc.submit(
            entity_id=entity,
            kind="text",
            title="泼水节例句",
            content=content,
            source=self.source(**kw),
            author_kind="human",
            author="陈老师",
        )
        return version

    def approve_all(self, digest) -> None:
        for cat, rv in [
            ("language", self.lin),
            ("culture", self.lin),
            ("fact", self.chen),
            ("copyright", self.chen),
            ("suitability", self.chen),
        ]:
            self.svc.record_opinion(digest, cat, rv.reviewer_id, "approved", "low")


class VersioningTests(ServiceTestBase):
    def test_duplicate_submit_is_idempotent(self):
        v1, created1 = self.svc.submit(
            entity_id="E-1", kind="text", title="泼水节例句",
            content="泼水节人们向彼此泼洒清水表示祝福。",
            source=self.source(), author_kind="human", author="陈老师",
        )
        v2, created2 = self.svc.submit(
            entity_id="E-1", kind="text", title="泼水节例句",
            content="泼水节人们向彼此泼洒清水表示祝福。",
            source=self.source(), author_kind="human", author="陈老师",
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(v1.digest, v2.digest)
        self.assertEqual(len(self.svc.list_versions("E-1")), 1)

    def test_rewrite_forms_parented_lineage(self):
        v1 = self.submit_v1()
        v2, created = self.svc.submit(
            entity_id="E-1", kind="text", title="泼水节例句",
            content="泼水节期间人们以蘸水方式相互淋洒祝福。",
            source=self.source(), author_kind="human", author="陈老师",
            parent_digest=v1.digest,
        )
        self.assertTrue(created)
        self.assertEqual(v2.revision, 2)
        self.assertEqual([v.revision for v in self.svc.lineage(v2.digest)], [1, 2])

    def test_existing_entity_without_parent_rejected(self):
        self.submit_v1()
        with self.assertRaises(RuleError):
            self.submit_v1(content="完全不同的另一句话。")

    def test_generator_requires_tool_in_source(self):
        with self.assertRaises(RuleError):
            self.svc.submit(
                entity_id="E-X", kind="text", title="t", content="c",
                source=self.source(), author_kind="generator", author="备课组",
            )

    def test_source_needs_locator(self):
        with self.assertRaises(ValueError):
            SourceClaim(note="只记得是网上看到的")


class DispatchAndReviewTests(ServiceTestBase):
    def test_reviewer_without_qualification_rejected(self):
        v = self.submit_v1()
        with self.assertRaises(RuleError):
            self.svc.record_opinion(
                v.digest, CATEGORY_FACT, self.lin.reviewer_id, "approved", "low"
            )

    def test_high_risk_first_opinion_requires_recheck(self):
        v = self.submit_v1()
        self.svc.record_opinion(
            v.digest, CATEGORY_CULTURE, self.lin.reviewer_id,
            "changes_requested", "high", note="习俗解释错误",
        )
        status = self.svc.category_status(v.digest, CATEGORY_CULTURE)
        self.assertTrue(status["recheck_required"])
        self.assertFalse(status["passed"])
        self.assertIn(
            v.digest[:12], self.svc.pending_work()["pending_rechecks"][0]
        )

    def test_recheck_must_be_different_senior_reviewer(self):
        v = self.submit_v1()
        self.svc.record_opinion(
            v.digest, CATEGORY_CULTURE, self.lin.reviewer_id,
            "changes_requested", "high",
        )
        # 初审人不能复核自己的意见
        with self.assertRaises(RuleError):
            self.svc.record_opinion(
                v.digest, CATEGORY_CULTURE, self.lin.reviewer_id,
                "changes_requested", "high",
            )
        # 非资深资质不能复核
        fresh = self.svc.register_reviewer("RV-NEW", "新老师", ["culture"])
        with self.assertRaises(RuleError):
            self.svc.record_opinion(
                v.digest, CATEGORY_CULTURE, fresh.reviewer_id,
                "changes_requested", "high",
            )

    def test_low_risk_approved_passes_without_recheck(self):
        v = self.submit_v1()
        self.svc.record_opinion(
            v.digest, CATEGORY_LANGUAGE, self.lin.reviewer_id, "approved", "low"
        )
        self.assertTrue(self.svc.category_status(v.digest, CATEGORY_LANGUAGE)["passed"])


class AdjudicationTests(ServiceTestBase):
    def _conflict(self, digest, first="changes_requested", recheck="approved"):
        self.svc.record_opinion(
            digest, CATEGORY_CULTURE, self.lin.reviewer_id, first, "high",
            note="初审意见",
        )
        self.svc.record_opinion(
            digest, CATEGORY_CULTURE, self.zhao.reviewer_id, recheck, "high",
            note="复核意见",
        )

    def test_conflict_opens_trackable_adjudication(self):
        v = self.submit_v1()
        self._conflict(v.digest)
        pending = self.svc.pending_work()["open_adjudications"]
        self.assertEqual(len(pending), 1)
        status = self.svc.category_status(v.digest, CATEGORY_CULTURE)
        self.assertFalse(status["passed"])
        # 裁决未出前不得继续录入意见
        with self.assertRaises(RuleError):
            self.svc.record_opinion(
                v.digest, CATEGORY_CULTURE, self.head.reviewer_id, "approved", "high"
            )

    def test_decider_must_be_neutral_lead(self):
        v = self.submit_v1()
        self._conflict(v.digest)
        adj_id = self.svc.pending_work()["open_adjudications"][0]
        # 冲突意见人不能裁决
        with self.assertRaises(RuleError):
            self.svc.resolve_adjudication(
                adj_id, self.lin.reviewer_id, ADJ_UPHOLD_FIRST, "理由"
            )
        # 无负责人资格不能裁决
        with self.assertRaises(RuleError):
            self.svc.resolve_adjudication(
                adj_id, self.chen.reviewer_id, ADJ_UPHOLD_FIRST, "理由"
            )

    def test_resolved_adjudication_is_final_and_gates_publish(self):
        v = self.submit_v1()
        self._conflict(v.digest)
        adj_id = self.svc.pending_work()["open_adjudications"][0]
        with self.assertRaises(RuleError):  # 裁决必须有理由
            self.svc.resolve_adjudication(adj_id, "RV-HEAD", ADJ_UPHOLD_FIRST, "  ")
        self.svc.resolve_adjudication(
            adj_id, "RV-HEAD", ADJ_UPHOLD_FIRST,
            "东盟籍顾问确认泼水寓意祝福，原句解释错误",
        )
        status = self.svc.category_status(v.digest, CATEGORY_CULTURE)
        self.assertFalse(status["passed"])  # 采纳初审 → 不通过
        with self.assertRaises(RuleError):  # 裁决终局不可重复
            self.svc.resolve_adjudication(adj_id, "RV-HEAD", ADJ_UPHOLD_FIRST, "再裁一次")
        with self.assertRaises(RuleError):  # 不能发布
            self.svc.publish(v.digest)


class PublishTests(ServiceTestBase):
    def test_publish_requires_all_five_categories(self):
        v = self.submit_v1()
        with self.assertRaises(RuleError):
            self.svc.publish(v.digest)
        for cat in ["language", "culture", "fact", "copyright"]:
            rv = self.lin if cat in ("language", "culture") else self.chen
            self.svc.record_opinion(v.digest, cat, rv.reviewer_id, "approved", "low")
        with self.assertRaises(RuleError):  # 还差适切性
            self.svc.publish(v.digest)
        self.svc.record_opinion(
            v.digest, "suitability", self.chen.reviewer_id, "approved", "low"
        )
        token = self.svc.publish(v.digest)
        self.assertTrue(token.token.startswith("TOK-"))
        # 令牌绑定的是该版本的摘要
        self.assertEqual(token.summary_hash, v.summary_hash)

    def test_token_is_stable_under_concurrent_publish(self):
        v = self.submit_v1()
        self.approve_all(v.digest)
        results = []
        errors = []

        def publish():
            try:
                results.append(self.svc.publish(v.digest))
            except RuleError as exc:  # pragma: no cover - 不应发生
                errors.append(exc)

        threads = [threading.Thread(target=publish) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(len({r.token for r in results}), 1)
        # 日志中只有一条发布事件，没有产生另一条事实链
        published = [e for e in self.svc.store.read_all() if e["type"] == "token_published"]
        self.assertEqual(len(published), 1)

    def test_opinions_immutable_after_publish(self):
        v = self.submit_v1()
        self.approve_all(v.digest)
        self.svc.publish(v.digest)
        with self.assertRaises(RuleError):
            self.svc.record_opinion(
                v.digest, CATEGORY_CULTURE, self.zhao.reviewer_id, "rejected", "high"
            )


class PackageRevocationTests(ServiceTestBase):
    def _published(self, content, entity, parent=None):
        version, _ = self.svc.submit(
            entity_id=entity,
            kind="text",
            title="泼水节例句",
            content=content,
            source=self.source(),
            author_kind="human",
            author="陈老师",
            parent_digest=parent,
        )
        self.approve_all(version.digest)
        self.svc.publish(version.digest)
        return version

    def test_unpublished_resource_cannot_be_packaged(self):
        v = self.submit_v1()
        with self.assertRaises(RuleError):
            self.svc.package_course("PKG-1", "试听课", "1.0", [v.digest])

    def test_revocation_quarantines_every_affected_package_version(self):
        v1 = self._published("泼水节人们向彼此泼洒清水表示祝福。", "E-1")
        v2 = self._published("你好，欢迎来到课堂。", "E-2")
        self.svc.package_course("PKG-A", "东盟试听课", "2026.09", [v1.digest])
        self.svc.package_course("PKG-A", "东盟试听课", "2026.10", [v1.digest, v2.digest])
        self.svc.package_course("PKG-B", "文化专题", "1.1", [v2.digest])

        _, dispositions = self.svc.revoke(v1.digest, "来源无法核验", "负责人")
        affected = {(d.package_id, d.package_version) for d in dispositions}
        self.assertEqual(affected, {("PKG-A", "2026.09"), ("PKG-A", "2026.10")})

        # 撤销后不得再次入包或重新发布
        with self.assertRaises(RuleError):
            self.svc.package_course("PKG-C", "新课", "1.0", [v1.digest])
        with self.assertRaises(RuleError):
            self.svc.revoke(v1.digest, "再次撤销", "负责人")

        # 隔离处置可推进为替换，替换版本必须合法
        disp = dispositions[0]
        with self.assertRaises(RuleError):
            self.svc.replace_disposition(disp.disposition_id, v1.digest)
        fixed = self._published(
            "泼水节以蘸水淋洒寓意祝福（据正式出版教材）。", "E-1", parent=v1.digest
        )
        updated = self.svc.replace_disposition(disp.disposition_id, fixed.digest)
        self.assertEqual(updated.action, "replaced")
        self.assertEqual(updated.substitute_digest, fixed.digest)
        # 替换幂等
        again = self.svc.replace_disposition(disp.disposition_id, fixed.digest)
        self.assertEqual(again.disposition_id, updated.disposition_id)

    def test_trace_covers_source_opinions_distribution_and_dispositions(self):
        v1 = self._published("泼水节人们向彼此泼洒清水表示祝福。", "E-TRACE")
        self.svc.package_course("PKG-T", "追溯课", "1.0", [v1.digest])
        _, disps = self.svc.revoke(v1.digest, "文化解释需更正", "负责人")
        fixed = self._published(
            "泼水节以蘸水淋洒寓意祝福彼此。", "E-TRACE", parent=v1.digest
        )
        self.svc.replace_disposition(disps[0].disposition_id, fixed.digest)

        trace = self.svc.trace("E-TRACE")
        self.assertEqual(trace["matched"], "entity")
        revoked = next(x for x in trace["versions"] if x["digest"] == v1.digest)
        self.assertIn("location", revoked["source"])
        self.assertTrue(revoked["opinions_and_adjudications"])
        self.assertEqual(revoked["distributed_in"][0]["package_id"], "PKG-T")
        rev = revoked["revocation"]
        self.assertEqual(rev["reason"], "文化解释需更正")
        self.assertEqual(rev["dispositions"][0]["action"], "replaced")
        self.assertEqual(rev["dispositions"][0]["substitute_digest"], fixed.digest)

        # 按课程包标识也能追溯
        pkg_trace = self.svc.trace("PKG-T")
        self.assertEqual(pkg_trace["matched"], "course_package")


class RecoveryTests(ServiceTestBase):
    def test_interrupted_work_resumes_after_restart(self):
        # 场景：一个高风险初审待复核，一个冲突待裁决，一个隔离待替换
        v_pending = self.submit_v1(content="待复核的句子。", entity="E-P")
        self.svc.record_opinion(
            v_pending.digest, CATEGORY_CULTURE, self.lin.reviewer_id,
            "changes_requested", "high",
        )

        v_conflict = self.submit_v1(content="有冲突的句子。", entity="E-C")
        self.svc.record_opinion(
            v_conflict.digest, CATEGORY_CULTURE, self.lin.reviewer_id,
            "changes_requested", "high",
        )
        self.svc.record_opinion(
            v_conflict.digest, CATEGORY_CULTURE, self.zhao.reviewer_id,
            "approved", "high",
        )
        adj_id = self.svc.pending_work()["open_adjudications"][0]

        v_pub = self.submit_v1(content="已发布待撤销的句子。", entity="E-R")
        self.approve_all(v_pub.digest)
        self.svc.publish(v_pub.digest)
        self.svc.package_course("PKG-R", "课", "1.0", [v_pub.digest])
        _, disps = self.svc.revoke(v_pub.digest, "来源失效", "负责人")
        disp_id = disps[0].disposition_id

        expected_pending = self.svc.pending_work()

        # 模拟进程中断：重建服务，重放日志
        revived = ReviewService(EventStore(Path(self.tmp.name) / "review.jsonl"))
        self.assertEqual(revived.pending_work(), expected_pending)

        # 未完成的复核可继续
        revived.record_opinion(
            v_pending.digest, CATEGORY_CULTURE, self.zhao.reviewer_id,
            "changes_requested", "high",
        )
        # 未完成的裁决可继续
        revived.resolve_adjudication(adj_id, "RV-HEAD", ADJ_UPHOLD_FIRST, "复核后维持初审")
        # 隔离替换可继续
        fixed, _ = revived.submit(
            entity_id="E-R", kind="text", title="泼水节例句",
            content="替换后的合规句子。", source=self.source(),
            author_kind="human", author="陈老师", parent_digest=v_pub.digest,
        )
        for cat, rv in [("language", self.lin), ("culture", self.lin),
                        ("fact", self.chen), ("copyright", self.chen),
                        ("suitability", self.chen)]:
            revived.record_opinion(fixed.digest, cat, rv.reviewer_id, "approved", "low")
        revived.publish(fixed.digest)
        final_disp = revived.replace_disposition(disp_id, fixed.digest)
        self.assertEqual(final_disp.action, "replaced")
        self.assertEqual(revived.pending_work()["open_adjudications"], [])
        self.assertEqual(revived.pending_work()["pending_rechecks"], [])
        self.assertEqual(revived.pending_work()["quarantined_awaiting_replacement"], [])


if __name__ == "__main__":
    unittest.main()
