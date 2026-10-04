# 智能教学资源风险审校

面向国际中文教研场景（如东盟教师试听课）的教学资源风险审校服务。
接收文本片段、题目、讲义单元及其**来源声明**，为每次生成或人工改写保存
**不可变版本与内容摘要**，按语言、文化、事实、版权、教学适切性五个类别
分派具备相应资质的审校人，高风险结论强制复核，意见冲突形成可追踪裁决，
发布令牌只绑定审校通过时刻的摘要；撤销已发布资源时自动定位受影响课程版本
并进入隔离或替换流程。所有状态保存在仅追加的事件日志中，进程中断后
重放即可继续未完成的复核、裁决与处置。

## 制度规则的落点

| 要求 | 实现 |
| --- | --- |
| 不可变版本 + 内容摘要 | `service.submit`：内容 SHA-256 指纹寻址，版本不可变；`Version.summary_hash` 绑定结构化摘要 |
| 重复提交不产生另一条事实链 | 相同内容幂等返回既有版本（测试 `test_duplicate_submit_is_idempotent`） |
| 生成/人工改写可追溯 | 改写必须挂接父版本，`lineage` 返回完整版本谱系；生成式素材强制登记所用工具 |
| 五类资质分派 | 语言/文化/事实/版权/教学适切性，审校人按 `qualifications` 校验 |
| 高风险复核 | 高风险初审必须由**另一名**该类别资深审校人复核 |
| 意见冲突可追踪裁决 | 初审与复核结论不一致自动开立裁决单，由中立负责人裁决，裁决终局 |
| 发布令牌绑定通过时摘要 | 五类全部通过方可发布；令牌 = H(摘要, 五类审校证据指纹)；并发/重复发布同牌且日志仅一条事件 |
| 撤销传播 | 撤销时定位全部引用该摘要的课程包版本并逐个隔离；可推进为替换，替换版本须已发布 |
| 中断继续 | JSONL 事件日志（追加即 fsync），重启重放；`pending` 列出待复核/待裁决/待替换 |
| 离线追溯 | `trace` 输入教学单元/指纹/课程包，返回原始来源、历次意见与裁决、传播范围、撤销处置 |

## 运行测试

    python -m unittest discover -s tests -v

## 编译检查

    python -m compileall -q src tests run_cli.py

## 命令行冒烟（内存日志，端到端演示）

    python run_cli.py

## 离线管理命令（文件日志，默认 data/review.journal.jsonl）

```bash
# 1. 登记审校人（--senior 为高风险复核类别，--adjudicate 为负责人裁决资格）
python run_cli.py reviewer --id RV-LIN --name 林老师 \
    --categories language,culture --senior culture --adjudicate

# 2. 提交教学资源（生成式素材须带 --generator；来源须给标题/作者/链接/位置之一）
python run_cli.py submit --entity E-ASEAN-01 --kind text \
    --title 泼水节问候例句 --content-file sample.txt \
    --author-kind generator --author 试听课备课组 --generator 示例生成工具 \
    --source-title 东南亚民俗网页 --source-url https://example.org/songkran

# 3. 录入五类意见（高风险初审后须由另一资深审校人再次 opine 作为复核）
python run_cli.py opine --digest <指纹> --category culture \
    --reviewer RV-LIN --conclusion changes_requested --risk high --note 习俗解释有误

# 4. 初审与复核冲突时裁决
python run_cli.py adjudicate --id ADJ-... --decider RV-LIN \
    --outcome uphold_first --rationale "东盟籍顾问确认泼水表祝福"

# 5. 查看进度、发布、入课程包
python run_cli.py progress --digest <指纹>
python run_cli.py publish  --digest <指纹>
python run_cli.py package --id PKG-ASEAN-TRIAL --title 东盟教师试听课 \
    --version 2026.10 --digests <指纹>

# 6. 撤销（自动隔离受影响课程版本）→ 替换
python run_cli.py revoke --digest <指纹> --reason 来源无法核验 --actor 教研负责人
python run_cli.py replace --disposition DSP-... --substitute <新指纹>

# 7. 中断后的待办与离线追溯
python run_cli.py pending
python run_cli.py trace --query E-ASEAN-01
```

## 代码结构

- `src/resource_review/models.py` — 不可变领域模型（版本、来源、审校人、意见、裁决、令牌、课程包、撤销、处置）
- `src/resource_review/store.py` — 仅追加 JSONL 事件日志（临时文件 fsync 后追加，重放恢复）
- `src/resource_review/service.py` — 审校制度全部业务规则与事件重放
- `run_cli.py` — 离线管理命令与端到端冒烟演示
- `tests/` — 契约测试与 19 项服务规则测试（含并发发布、跨"进程"恢复）
