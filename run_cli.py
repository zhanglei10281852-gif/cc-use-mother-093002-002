#!/usr/bin/env python3
"""智能教学资源风险审校 —— 离线管理命令。

所有命令读写同一份仅追加事件日志，默认位置 data/review.journal.jsonl；
进程中断后重新执行命令即自动重放恢复，可用 pending 查看未竟事项。

示例：
    python run_cli.py reviewer --id RV-LIN --name 林老师 \
        --categories language,culture --senior culture
    python run_cli.py submit --entity E-ASEAN-01 --kind text \
        --title 泼水节例句 --content-file sample.txt \
        --author-kind generator --author 试听课备课组 \
        --generator 示例生成工具 --source-title 某语料网页 --source-url https://example.com
    python run_cli.py opine --digest <摘要> --category culture \
        --reviewer RV-LIN --conclusion changes_requested --risk high --note 习俗解释有误
    python run_cli.py trace --query E-ASEAN-01
    python run_cli.py pending

无参数运行时执行一段内存中的端到端冒烟演示。
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from resource_review.models import (  # noqa: E402
    CATEGORIES,
    CATEGORY_LABELS,
    SourceClaim,
)
from resource_review.service import RuleError, ReviewService  # noqa: E402
from resource_review.store import EventStore  # noqa: E402

DEFAULT_JOURNAL = "data/review.journal.jsonl"


def _emit(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _open_service(journal: str) -> ReviewService:
    return ReviewService(EventStore(journal))


def _read_content(args) -> str:
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8").strip()
    if args.content is not None:
        return args.content
    raise RuleError("请用 --content 或 --content-file 提供资源内容")


def cmd_reviewer(svc: ReviewService, args) -> None:
    reviewer = svc.register_reviewer(
        reviewer_id=args.id,
        name=args.name,
        qualifications={c.strip() for c in args.categories.split(",") if c.strip()},
        senior_for={c.strip() for c in (args.senior or "").split(",") if c.strip()},
        can_adjudicate=args.adjudicate,
    )
    _emit(
        {
            "reviewer_id": reviewer.reviewer_id,
            "name": reviewer.name,
            "qualifications": sorted(reviewer.qualifications),
            "senior_for": sorted(reviewer.senior_for),
            "can_adjudicate": reviewer.can_adjudicate,
        }
    )


def cmd_submit(svc: ReviewService, args) -> None:
    source = SourceClaim(
        title=args.source_title,
        author=args.source_author,
        url=args.source_url,
        license_tag=args.source_license,
        location=args.source_location,
        generator=args.generator,
        note=args.source_note,
    )
    version, created = svc.submit(
        entity_id=args.entity,
        kind=args.kind,
        title=args.title,
        content=_read_content(args),
        source=source,
        author_kind=args.author_kind,
        author=args.author,
        parent_digest=args.parent,
    )
    _emit(
        {
            "created": created,
            "digest": version.digest,
            "entity_id": version.entity_id,
            "revision": version.revision,
            "parent_digest": version.parent_digest,
            "summary_hash": version.summary_hash,
            "created_at": version.created_at,
        }
    )


def cmd_opine(svc: ReviewService, args) -> None:
    opinion = svc.record_opinion(
        digest=args.digest,
        category=args.category,
        reviewer_id=args.reviewer,
        conclusion=args.conclusion,
        risk_level=args.risk,
        note=args.note or "",
    )
    result = {
        "opinion_id": opinion.opinion_id,
        "stage": opinion.stage,
        "category": CATEGORY_LABELS[opinion.category],
        "conclusion": opinion.conclusion,
        "risk_level": opinion.risk_level,
    }
    progress = svc.category_status(opinion.digest, opinion.category)
    result["category_state"] = progress["reason"]
    if progress["adjudication"] is not None and not progress["adjudication"].resolved():
        result["adjudication_opened"] = progress["adjudication"].adjudication_id
    _emit(result)


def cmd_adjudicate(svc: ReviewService, args) -> None:
    adj = svc.resolve_adjudication(args.id, args.decider, args.outcome, args.rationale)
    _emit(
        {
            "adjudication_id": adj.adjudication_id,
            "outcome": adj.outcome,
            "decider_id": adj.decider_id,
            "resolved_at": adj.resolved_at,
        }
    )


def cmd_progress(svc: ReviewService, args) -> None:
    _emit(
        [
            {"category": CATEGORY_LABELS[s["category"]], "passed": s["passed"], "state": s["reason"]}
            for s in svc.review_progress(args.digest)
        ]
    )


def cmd_publish(svc: ReviewService, args) -> None:
    token = svc.publish(args.digest)
    _emit(
        {
            "token": token.token,
            "digest": token.digest,
            "summary_hash": token.summary_hash,
            "approvals_fingerprint": token.approvals_fingerprint,
            "created_at": token.created_at,
        }
    )


def cmd_package(svc: ReviewService, args) -> None:
    package = svc.package_course(
        package_id=args.id,
        title=args.title,
        version=args.version,
        digests=[d.strip() for d in args.digests.split(",") if d.strip()],
    )
    _emit(
        {
            "package_id": package.package_id,
            "title": package.title,
            "version": package.version,
            "digests": list(package.digests),
        }
    )


def cmd_revoke(svc: ReviewService, args) -> None:
    revocation, dispositions = svc.revoke(args.digest, args.reason, args.actor)
    _emit(
        {
            "revocation_id": revocation.revocation_id,
            "digest": revocation.digest,
            "reason": revocation.reason,
            "actor": revocation.actor,
            "quarantined_packages": [
                {
                    "disposition_id": d.disposition_id,
                    "package_id": d.package_id,
                    "package_version": d.package_version,
                    "action": d.action,
                }
                for d in dispositions
            ],
        }
    )


def cmd_replace(svc: ReviewService, args) -> None:
    disp = svc.replace_disposition(args.disposition, args.substitute)
    _emit(
        {
            "disposition_id": disp.disposition_id,
            "package_id": disp.package_id,
            "package_version": disp.package_version,
            "action": disp.action,
            "substitute_digest": disp.substitute_digest,
            "replaced_at": disp.replaced_at,
        }
    )


def cmd_trace(svc: ReviewService, args) -> None:
    _emit(svc.trace(args.query))


def cmd_pending(svc: ReviewService, args) -> None:
    _emit(svc.pending_work())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="智能教学资源风险审校离线管理命令")
    parser.add_argument("--journal", default=DEFAULT_JOURNAL, help="事件日志路径")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("reviewer", help="登记审校人及资质")
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--categories", required=True, help="逗号分隔，可选：" + ",".join(sorted(CATEGORIES)))
    p.add_argument("--senior", help="可承担高风险复核的类别，逗号分隔")
    p.add_argument("--adjudicate", action="store_true", help="具备负责人裁决资格")
    p.set_defaults(func=cmd_reviewer)

    p = sub.add_parser("submit", help="提交资源版本（内容重复时幂等返回）")
    p.add_argument("--entity", required=True, help="教学实体标识，如 E-ASEAN-01")
    p.add_argument("--kind", required=True, choices=["text", "question", "handout"])
    p.add_argument("--title", required=True)
    p.add_argument("--content", help="直接给出内容")
    p.add_argument("--content-file", help="从文件读取内容")
    p.add_argument("--author-kind", required=True, choices=["human", "generator"])
    p.add_argument("--author", required=True, help="整理人或备课组")
    p.add_argument("--parent", help="改写所基于的父版本内容指纹")
    p.add_argument("--generator", help="生成式工具名称（生成式素材必填）")
    p.add_argument("--source-title")
    p.add_argument("--source-author")
    p.add_argument("--source-url")
    p.add_argument("--source-license")
    p.add_argument("--source-location", help="纸质/线下出处位置")
    p.add_argument("--source-note")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("opine", help="录入初审/复核意见")
    p.add_argument("--digest", required=True)
    p.add_argument("--category", required=True, choices=sorted(CATEGORIES))
    p.add_argument("--reviewer", required=True)
    p.add_argument("--conclusion", required=True, choices=["approved", "changes_requested", "rejected"])
    p.add_argument("--risk", required=True, choices=["low", "high"])
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_opine)

    p = sub.add_parser("adjudicate", help="负责人裁决初审与复核的冲突")
    p.add_argument("--id", required=True, help="裁决单标识 ADJ-...")
    p.add_argument("--decider", required=True)
    p.add_argument("--outcome", required=True, choices=["uphold_first", "uphold_recheck"])
    p.add_argument("--rationale", required=True)
    p.set_defaults(func=cmd_adjudicate)

    p = sub.add_parser("progress", help="查看一个版本五类审校进度")
    p.add_argument("--digest", required=True)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("publish", help="五类通过后发布，令牌绑定通过时刻摘要")
    p.add_argument("--digest", required=True)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("package", help="将已发布资源打入课程包版本")
    p.add_argument("--id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--digests", required=True, help="逗号分隔的内容指纹")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("revoke", help="撤销已发布资源并隔离受影响课程版本")
    p.add_argument("--digest", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", required=True)
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("replace", help="将隔离处置推进为替换")
    p.add_argument("--disposition", required=True)
    p.add_argument("--substitute", required=True, help="替换版本（须已发布）的内容指纹")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("trace", help="追溯实体/资源/课程包：来源、意见、传播、撤销处置")
    p.add_argument("--query", required=True)
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("pending", help="列出中断后需继续的复核、裁决与隔离替换")
    p.set_defaults(func=cmd_pending)

    return parser


def smoke_demo() -> None:
    """内存日志上的端到端冒烟：覆盖全部关键规则。"""
    with tempfile.TemporaryDirectory() as tmp:
        svc = ReviewService(EventStore(Path(tmp) / "smoke.jsonl"))

        lin = svc.register_reviewer("RV-LIN", "林老师", ["language", "culture"], ["culture"])
        chen = svc.register_reviewer("RV-CHEN", "陈老师", ["fact", "copyright", "suitability"],
                                    ["fact", "copyright", "suitability"])
        zhao = svc.register_reviewer("RV-ZHAO", "赵老师", ["culture"], ["culture"])
        head = svc.register_reviewer("RV-HEAD", "教研负责人", ["culture"], ["culture"],
                                    can_adjudicate=True)

        v1, created1 = svc.submit(
            entity_id="E-ASEAN-01",
            kind="text",
            title="泼水节问候例句",
            content="泼水节那天，当地人会把水泼向客人表示不满。",
            source=SourceClaim(title="某语料网页", url="https://example.com/songkran",
                               generator="示例生成工具"),
            author_kind="generator",
            author="试听课备课组",
        )
        # 重复提交不产生第二条事实链
        v1_dup, created_dup = svc.submit(
            entity_id="E-ASEAN-01",
            kind="text",
            title="泼水节问候例句",
            content="泼水节那天，当地人会把水泼向客人表示不满。",
            source=SourceClaim(title="某语料网页", url="https://example.com/songkran",
                               generator="示例生成工具"),
            author_kind="generator",
            author="试听课备课组",
        )
        # 其余四类低风险通过；文化类初审高风险纠错
        svc.record_opinion(v1.digest, "language", lin.reviewer_id, "approved", "low")
        svc.record_opinion(v1.digest, "fact", chen.reviewer_id, "approved", "low")
        svc.record_opinion(v1.digest, "copyright", chen.reviewer_id, "approved", "low")
        svc.record_opinion(v1.digest, "suitability", chen.reviewer_id, "approved", "low")
        svc.record_opinion(v1.digest, "culture", lin.reviewer_id,
                           "changes_requested", "high", note="泼水寓意祝福而非不满")

        pending_before = svc.pending_work()
        # 另一资深审校人复核，与初审冲突 → 裁决单
        svc.record_opinion(v1.digest, "culture", zhao.reviewer_id, "approved", "high",
                           note="复核认为可接受")
        open_adj = svc.pending_work()["open_adjudications"]
        svc.resolve_adjudication(open_adj[0], head.reviewer_id, "uphold_first",
                                 "东盟籍顾问确认泼水表祝福，原句文化解释错误")

        # 改写纠错后走新版本
        v2, created2 = svc.submit(
            entity_id="E-ASEAN-01",
            kind="text",
            title="泼水节问候例句",
            content="泼水节那天，当地人会把清水轻轻泼洒在客人身上，表示祝福。",
            source=SourceClaim(title="某语料网页", url="https://example.com/songkran",
                               generator="示例生成工具"),
            author_kind="generator",
            author="试听课备课组",
            parent_digest=v1.digest,
        )
        for cat, rv in [("language", lin), ("culture", lin), ("fact", chen),
                        ("copyright", chen), ("suitability", chen)]:
            svc.record_opinion(v2.digest, cat, rv.reviewer_id, "approved", "low")
        token = svc.publish(v2.digest)
        token_again = svc.publish(v2.digest)

        pkg = svc.package_course("PKG-ASEAN-TRIAL", "东盟教师试听课·入门", "2026.09",
                                 [v2.digest])

        # 撤销并隔离，随后替换
        rev, disps = svc.revoke(v2.digest, "来源网页失效，文化注释仍需复核", "教研负责人")
        disp = disps[0]

        v3, _ = svc.submit(
            entity_id="E-ASEAN-01",
            kind="text",
            title="泼水节问候例句",
            content="泼水节期间，人们以蘸水的方式相互淋洒，寓意洗去不顺、祝福彼此。",
            source=SourceClaim(title="东南亚文化节令教材（正式出版）", location="图书馆3楼-民俗-12",
                               author="东盟文化教研室"),
            author_kind="human",
            author="陈老师",
            parent_digest=v2.digest,
        )
        for cat, rv in [("language", lin), ("culture", lin), ("fact", chen),
                        ("copyright", chen), ("suitability", chen)]:
            svc.record_opinion(v3.digest, cat, rv.reviewer_id, "approved", "low")
        token3 = svc.publish(v3.digest)
        replaced = svc.replace_disposition(disp.disposition_id, v3.digest)

        trace = svc.trace("E-ASEAN-01")
        revoked_trace = next(v for v in trace["versions"] if v["digest"] == v2.digest)
        _emit(
            {
                "dedup": {"duplicate_submit_created_new_chain": created_dup,
                          "same_digest": v1_dup.digest == v1.digest},
                "pending_while_high_risk_first": pending_before["pending_rechecks"],
                "token": token.token,
                "concurrent_publish_same_token": token_again.token == token.token,
                "package_quarantined": {
                    "package_id": pkg.package_id,
                    "version": pkg.version,
                    "disposition": disp.action,
                },
                "final_disposition": {
                    "action": replaced.action,
                    "substitute_digest": replaced.substitute_digest,
                    "substitute_token": token3.token,
                },
                "trace_versions": len(trace["versions"]),
                "lineage_length": len(svc.lineage(v3.digest)),
                "revocation": revoked_trace["revocation"]["revocation_id"],
            }
        )


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        smoke_demo()
        return 0
    try:
        svc = _open_service(args.journal)
        args.func(svc, args)
    except RuleError as exc:
        print(f"操作被拒绝：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
