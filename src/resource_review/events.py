"""事实链事件类型常量。

事件是唯一的写入口径：所有状态变化都以事件追加到日志，
投影（state.py）只负责回放。事件负载中的枚举一律使用英文取值存储。
"""

SOURCE_DECLARED = "source_declared"
RESOURCE_SUBMITTED = "resource_submitted"
REVIEWER_REGISTERED = "reviewer_registered"
REVIEWER_ASSIGNED = "reviewer_assigned"
OPINION_RECORDED = "opinion_recorded"
ADJUDICATION_RECORDED = "adjudication_recorded"
CONCLUSION_REACHED = "conclusion_reached"
CONCLUSION_REREVIEWED = "conclusion_rereviewed"
TOKEN_ISSUED = "publish_token_issued"
COURSE_PUBLISHED = "course_version_published"
COURSE_STATUS_CHANGED = "course_version_status_changed"
RESOURCE_REVOKED = "resource_revoked"
DISPOSITION_CREATED = "disposition_created"
DISPOSITION_COMPLETED = "disposition_completed"
