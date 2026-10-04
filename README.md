# 智能教学资源风险审校

面向国际中文教研场景的教学资源风险审校服务：接收文本片段、题目、讲义单元及其来源声明，
以**单一追加式事实链**（事件日志）记录每一次生成、改写、审校、裁决、发布与撤销，
支持离线管理命令追踪任一教学单元的完整生命周期。

## 领域规则

- **不可变版本与内容摘要**：每次生成或人工改写保存为不可变版本，绑定 `sha256` 内容摘要；
  同一幂等键重放、同一实体内容摘要未变化的重复提交都返回原版本，不会制造另一条事实链。
- **分维度资质审校**：按语言、文化、事实、版权、教学适切性五个维度分派审校人；
  审校人必须具备对应资质，意见只能由被指派人提交。
- **高风险复核**：任一有效意见为高风险时，结论进入待复核状态，
  须由未参与该版本意见的其他合格审校人确认或推翻（推翻须下调风险等级）后才生效。
- **冲突裁决**：有效意见中存在拒绝/需修改时，必须先形成引用被裁意见、可追踪的裁决，
  才能汇总结论。
- **发布令牌**：只能绑定审校最终通过时的内容摘要；签发必须携带幂等键；
  同一版本重复签发、为更旧版本签发均被拒绝；新版本的令牌生效后旧令牌自动被取代。
- **并发与中断**：所有命令携带期望序号追加事件，并发决定只有一方能推进事实链；
  事件批次原子落盘，崩溃残留的损坏尾部在读取时被忽略，进程中断重开后
  未完成的复核与处置可继续（`pending` 命令查看）。
- **撤销联动**：撤销已发布资源会使令牌失效，定位全部受影响课程版本，
  并同批次生成隔离或替换处置；替换处置完成时自动为审校通过的替换版本
  签发令牌并发布新的课程版本。

## 目录结构

    src/resource_review/
      contracts.py   基础契约（ResourceVersion / ReviewFinding）
      enums.py       领域枚举与中文别名解析
      models.py      不可变领域记录与内容摘要
      events.py      事实链事件类型
      store.py       追加式事件存储（乐观序号、崩溃容忍）
      state.py       事件回放投影
      services.py    应用服务（全部领域规则）
      cli.py         离线管理命令

## 运行测试

    python -m unittest discover -s tests -v

编译检查：

    python -m compileall -q src tests run_cli.py

命令行冒烟（无参数时输出演示 JSON）：

    python run_cli.py

## 离线管理命令

所有命令通过 `run_cli.py` 进入，`--store` 指定事实链文件（默认 `./resource_review_store.jsonl`），
输出为 JSON；维度、风险、结论等参数可直接使用中文（如 `文化`、`高`、`通过`）。

```bash
# 登记来源声明（生成 / 人工）
python run_cli.py --store review.jsonl declare-source --origin 生成 --tool 某生成工具 --id SRC-1

# 提交讲义单元版本（幂等键防止重复提交）
python run_cli.py --store review.jsonl submit --kind 讲义单元 --entity E-1 \
    --name 第一课 --declaration SRC-1 --content-file unit.txt --key sub-1

# 登记审校人并按维度分派
python run_cli.py --store review.jsonl register-reviewer --name 王文化 --qual 文化 --id REV-1
python run_cli.py --store review.jsonl assign --version E-1-v1 --dimension 文化 --reviewer REV-1

# 记录意见 → 汇总结论（高风险自动待复核）→ 必要时复核 / 裁决
python run_cli.py --store review.jsonl opinion --version E-1-v1 --dimension 文化 \
    --reviewer REV-1 --risk 低 --verdict 通过
python run_cli.py --store review.jsonl conclude --version E-1-v1
python run_cli.py --store review.jsonl rereview --conclusion CON-xxx --reviewer REV-2 --outcome 确认
python run_cli.py --store review.jsonl adjudicate --version E-1-v1 --opinions OPN-a,OPN-b \
    --final-verdict 通过 --final-risk 低 --by 教研负责人

# 签发发布令牌（绑定通过时的摘要）并发布课程包版本
python run_cli.py --store review.jsonl publish --version E-1-v1 --key pub-1
python run_cli.py --store review.jsonl course-publish --course C-1 --entry E-1:E-1-v1

# 撤销已发布资源（自动生成隔离/替换处置），执行处置
python run_cli.py --store review.jsonl revoke --entity E-1 --reason 习俗解释有误 --action 隔离
python run_cli.py --store review.jsonl dispose --disposition DIS-xxx --note 已隔离

# 查看待办（未完成复核 / 待处置 / 意见未齐）
python run_cli.py --store review.jsonl pending

# 追踪教学单元：原始来源、历次意见、传播范围、撤销后的每项处置
python run_cli.py --store review.jsonl trace E-1
```
