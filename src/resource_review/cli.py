"""离线管理命令：教研负责人在本地对事实链执行全部审校管理操作。

用法示例：

    python run_cli.py --store review.jsonl declare-source --origin 生成 --tool 某生成工具
    python run_cli.py --store review.jsonl submit --kind 讲义单元 --declaration SRC-1 \
        --name 第一课 --content-file unit.txt --key sub-1
    python run_cli.py --store review.jsonl trace E-1

所有命令输出 JSON；领域规则冲突时以退出码 2 返回错误说明。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import DomainError
from .models import to_jsonable
from .services import ResourceReviewService

DEFAULT_STORE = "resource_review_store.jsonl"


def _print(obj) -> None:
    print(json.dumps(to_jsonable(obj), ensure_ascii=False, indent=2))


def _cmd_declare_source(svc, args):
    return svc.declare_source(
        origin=args.origin,
        tool_name=args.tool,
        operator=args.operator,
        note=args.note,
        declaration_id=args.id,
        idempotency_key=args.key,
    )


def _cmd_submit(svc, args):
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    else:
        content = args.content or ""
    return svc.submit_resource(
        kind=args.kind,
        content=content,
        declaration_id=args.declaration,
        entity_id=args.entity,
        display_name=args.name,
        derived_from_version_id=args.derived_from,
        idempotency_key=args.key,
    )


def _cmd_register_reviewer(svc, args):
    return svc.register_reviewer(name=args.name, qualifications=args.qual, reviewer_id=args.id)


def _cmd_assign(svc, args):
    return svc.assign_reviewer(
        version_id=args.version, dimension=args.dimension, reviewer_id=args.reviewer
    )


def _cmd_opinion(svc, args):
    return svc.record_opinion(
        version_id=args.version,
        dimension=args.dimension,
        reviewer_id=args.reviewer,
        risk_level=args.risk,
        verdict=args.verdict,
        rationale=args.rationale,
        idempotency_key=args.key,
    )


def _cmd_adjudicate(svc, args):
    return svc.adjudicate(
        version_id=args.version,
        opinion_ids=[part.strip() for part in args.opinions.split(",") if part.strip()],
        final_verdict=args.final_verdict,
        final_risk_level=args.final_risk,
        rationale=args.rationale,
        decided_by=args.by,
    )


def _cmd_conclude(svc, args):
    return svc.conclude(version_id=args.version)


def _cmd_rereview(svc, args):
    return svc.complete_rereview(
        conclusion_id=args.conclusion,
        reviewer_id=args.reviewer,
        outcome=args.outcome,
        adjusted_risk_level=args.adjusted_risk,
        rationale=args.rationale,
    )


def _cmd_publish(svc, args):
    return svc.issue_publish_token(version_id=args.version, idempotency_key=args.key)


def _cmd_course_publish(svc, args):
    entries = []
    for raw in args.entry:
        if ":" in raw:
            entity_id, version_id = raw.split(":", 1)
        else:
            version = svc.state.versions.get(raw)
            if version is None:
                raise DomainError(f"无法识别的课程条目: {raw}")
            entity_id, version_id = version.entity_id, version.version_id
        entries.append((entity_id, version_id))
    return svc.publish_course_version(course_id=args.course, entries=entries, idempotency_key=args.key)


def _cmd_revoke(svc, args):
    revocation, dispositions = svc.revoke_resource(
        entity_id=args.entity,
        version_id=args.version,
        reason=args.reason,
        revoked_by=args.by,
        action=args.action,
        replacement_version_id=args.replacement,
        idempotency_key=args.key,
    )
    return {"revocation": revocation, "dispositions": dispositions}


def _cmd_dispose(svc, args):
    return svc.complete_disposition(disposition_id=args.disposition, note=args.note)


def _cmd_pending(svc, args):
    return svc.pending_work()


def _cmd_trace(svc, args):
    return svc.trace(args.entity)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resource-review",
        description="智能教学资源风险审校：离线管理命令",
    )
    parser.add_argument("--store", default=DEFAULT_STORE, help="事实链存储文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("declare-source", help="登记来源声明（生成/人工）")
    p.add_argument("--origin", required=True, help="来源：生成 / 人工")
    p.add_argument("--tool", default="", help="生成工具名称")
    p.add_argument("--operator", default="", help="操作人")
    p.add_argument("--note", default="", help="备注")
    p.add_argument("--id", default=None, help="指定声明标识")
    p.add_argument("--key", default=None, help="幂等键")
    p.set_defaults(func=_cmd_declare_source)

    p = sub.add_parser("submit", help="提交资源版本（文本片段/题目/讲义单元）")
    p.add_argument("--kind", required=True, help="类型：文本片段 / 题目 / 讲义单元")
    p.add_argument("--content", default=None, help="资源内容")
    p.add_argument("--content-file", default=None, help="从文件读取内容")
    p.add_argument("--declaration", required=True, help="来源声明标识")
    p.add_argument("--entity", default=None, help="既有实体标识（省略则新建）")
    p.add_argument("--name", default="", help="显示名称")
    p.add_argument("--derived-from", default=None, help="改写来源版本标识")
    p.add_argument("--key", default=None, help="幂等键")
    p.set_defaults(func=_cmd_submit)

    p = sub.add_parser("register-reviewer", help="登记审校人及其资质维度")
    p.add_argument("--name", required=True)
    p.add_argument("--qual", action="append", required=True, help="资质维度，可多次：语言/文化/事实/版权/教学适切性")
    p.add_argument("--id", default=None)
    p.set_defaults(func=_cmd_register_reviewer)

    p = sub.add_parser("assign", help="按维度分派审校人")
    p.add_argument("--version", required=True)
    p.add_argument("--dimension", required=True)
    p.add_argument("--reviewer", required=True)
    p.set_defaults(func=_cmd_assign)

    p = sub.add_parser("opinion", help="记录审校意见")
    p.add_argument("--version", required=True)
    p.add_argument("--dimension", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--risk", required=True, help="风险：低 / 中 / 高")
    p.add_argument("--verdict", required=True, help="结论：通过 / 拒绝 / 需修改")
    p.add_argument("--rationale", default="")
    p.add_argument("--key", default=None, help="幂等键")
    p.set_defaults(func=_cmd_opinion)

    p = sub.add_parser("adjudicate", help="对冲突意见形成裁决")
    p.add_argument("--version", required=True)
    p.add_argument("--opinions", required=True, help="被裁意见标识，逗号分隔")
    p.add_argument("--final-verdict", required=True)
    p.add_argument("--final-risk", required=True)
    p.add_argument("--rationale", default="")
    p.add_argument("--by", default="", help="裁决人")
    p.set_defaults(func=_cmd_adjudicate)

    p = sub.add_parser("conclude", help="汇总审校结论（高风险自动进入待复核）")
    p.add_argument("--version", required=True)
    p.set_defaults(func=_cmd_conclude)

    p = sub.add_parser("rereview", help="完成高风险结论的复核")
    p.add_argument("--conclusion", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--outcome", required=True, help="确认 / 推翻")
    p.add_argument("--adjusted-risk", default=None, help="推翻时调整后的风险：低 / 中")
    p.add_argument("--rationale", default="")
    p.set_defaults(func=_cmd_rereview)

    p = sub.add_parser("publish", help="签发发布令牌（绑定审校通过时的摘要）")
    p.add_argument("--version", required=True)
    p.add_argument("--key", required=True, help="幂等键（必填）")
    p.set_defaults(func=_cmd_publish)

    p = sub.add_parser("course-publish", help="发布课程包版本（仅可纳入已发布资源）")
    p.add_argument("--course", required=True)
    p.add_argument("--entry", action="append", required=True, help="条目：实体:版本 或 版本标识")
    p.add_argument("--key", default=None, help="幂等键")
    p.set_defaults(func=_cmd_course_publish)

    p = sub.add_parser("revoke", help="撤销已发布资源并生成隔离/替换处置")
    p.add_argument("--entity", required=True)
    p.add_argument("--version", default=None, help="缺省为当前发布版本")
    p.add_argument("--reason", default="")
    p.add_argument("--by", default="")
    p.add_argument("--action", default="quarantine", help="隔离 / 替换")
    p.add_argument("--replacement", default=None, help="替换处置时的替换版本")
    p.add_argument("--key", default=None, help="幂等键")
    p.set_defaults(func=_cmd_revoke)

    p = sub.add_parser("dispose", help="执行一条撤销处置")
    p.add_argument("--disposition", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=_cmd_dispose)

    p = sub.add_parser("pending", help="查看未完成复核、待处置与意见未齐的版本")
    p.set_defaults(func=_cmd_pending)

    p = sub.add_parser("trace", help="追踪教学单元：来源、历次意见、传播范围、撤销处置")
    p.add_argument("entity")
    p.set_defaults(func=_cmd_trace)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    service = ResourceReviewService.open(args.store)
    try:
        result = args.func(service, args)
    except DomainError as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    if result is not None:
        _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
