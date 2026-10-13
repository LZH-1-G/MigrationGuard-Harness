# MigrationGuard：简历项目与面试准备（基于 v0.1.0 已记录数据）

> 数据来源：`benchmarks/results.json`、`benchmarks/RESULTS.md`、
> `docs/VERIFICATION.md`；统计仅代表 5 个项目、每种策略运行 1 次的实验。
> 安全加固 PR 中的改动不能当作已合并、已验证的 v0.1.0 功能描述。

## 中文简历版本

**MigrationGuard — 基于 Claude Agent SDK 的可验证依赖迁移 Harness**
技术栈：Python、Claude Agent SDK、Docker、SQLite、Git Worktree、libcst、uv、Pytest

- **Agent 执行与恢复**：设计状态机驱动的自动化代码迁移流水线，结合 Claude Agent SDK 的受控工具调用、SQLite checkpoint、预算与重试机制，支持额度耗尽后的任务暂停与恢复；在真实 webhooks 迁移任务中验证跨额度窗口续跑。
- **行为可靠性**：基于双环境执行与自动生成的 Pydantic 行为探针构建差分验证，以 Evidence Gates 和独立 Reviewer 对测试完整性、权限边界及行为回归进行验收，避免 Agent 自报成功即被接受。
- **安全执行**：基于 Docker、Git Worktree 及工具权限策略限制代码修改范围，保留可审计的测试、探针和最终补丁；设计 8 类攻击行为的对抗性评测。
- **量化评测**：构建 5 项 Pydantic v1→v2 Benchmark；在该数据集上完整 Harness 实现 5/5 次 oracle 正确迁移，32 次真正触发攻击的对抗运行均未误接受；历史本地验收报告记载 105 passed、82% host-side 行覆盖率，并由 GitHub CI 在 Python 3.10、3.12 上成功完成自动检查。

GitHub：https://github.com/LZH-1-G/MigrationGuard-Harness

## 30 秒项目介绍

> MigrationGuard 是面向 Python 依赖升级的 Agent Harness，首个场景是 Pydantic v1 到 v2。
> Claude 负责无法被确定性规则覆盖的迁移和修复；Harness 负责 worktree 隔离、
> 基线/目标环境、证据检查、独立审查与断点恢复。与只看 pytest 是否通过不同，
> 我使用 v1/v2 行为探针对比输入输出并冻结测试，防止“测试绿了但语义变了”。
> 五项目实验里完整 Harness 做到 5/5 oracle 正确迁移，但样本量还不足以推广为普遍成功率。

## 关键架构与技术难点

**技术难点 1：Agent 写了代码≠迁移正确。**
先在旧依赖上收集基线测试及行为探针，再在新依赖上重放相同输入；不只比较返回值，
还要处理 Pydantic 的数据强制转换、校验、错误和 JSON 序列化差异。
差分证据由确定性 gate 决策，LLM Reviewer 是附加否决而非覆盖机制。

**技术难点 2：防止 reward hacking。**
Agent 工具层阻止写入测试、conftest、配置和越界路径；验证层冻结可见测试并进行完整性审计；
外部 hidden oracle 仅用于评测，不泄露给 Agent 或接受策略。

**技术难点 3：长任务可恢复。**
在每个阶段持久化状态与 commit/checkpoint，遇到额度限制变更为 PAUSED 而不是 FAILED；
恢复时读取历史事件、阶段结果与预算，避免盲目从头执行。真实额度恢复测试已记载于
`docs/VERIFICATION.md`。

**技术难点 4：隔离与依赖边界。**
Docker 和 worktree 用于降低测试代码对宿主机的影响；但 host 侧依赖解析、下载与安装不是
完整的可信沙箱。不要把“沙箱测试通过”解释为“能安全执行任意恶意仓库”。

## 常见面试追问（回答要点）

**Q1：和 Claude Code 的区别是什么？** 不是重新造通用 Coding Agent；
MigrationGuard 使用模型修复能力，但将迁移任务强制接入差分行为验证、测试完整性门禁、
版本约束与 checkpoint，可重现地决定是否接受结果。

**Q2：为什么既有 Codemod 又有 Agent？** 语法映射类变更由 libcst 做可审计、稳定的转换；
涉及类型语义和上下文的变更交给 Claude；减少不必要的模型调用，但不假设所有迁移能靠静态规则完成。

**Q3：为什么单元测试绿了仍可能错？** Pydantic 主版本升级会改变隐式 coercion、
序列化和错误行为；业务测试不一定覆盖这些输入。构造同输入的旧/新行为探针形成更强的验收证据。

**Q4：如何判断 Reviewer 是否可信？** Reviewer 是非确定性的，仅提供额外风险分析与否决权；
不能覆盖确定性失败。最终验收必须有机器可验证的测试和探针证据。

**Q5：32/32 阻断意味着系统绝对安全吗？** 不意味。数字来自 5 个项目中 32 次实际触发的对抗运行，
只覆盖这 8 类策略和特定输入；可能存在未知攻击、构造不充分、缺失行为探针等风险。

**Q6：Benchmarks 为什么不说成功率 100%？** 五个案例每组只跑一次，模型和采样有随机性；
只能准确表达“本实验 5/5”，不能推断广泛项目上的统计表现。

**Q7：如何测量 Harness 带来的收益？** 对照 `codemod-only`、`claude-tests-only`、
`claude-no-codemod` 与全 Harness，统计 oracle 正确率、假接受率、成本与时延。
注意 Full 5/5 与 no-codemod 5/5 说明该小数据集上不能宣称精度显著领先；
单次总模型成本 $1.79 对 $2.04 仅是初步证据。

**Q8：如果让你迭代下一个版本？** 先补足独立多次 benchmark、明确模型版本、
加强输入分布和服务依赖测试，再在临时 VM 中隔离所有包构建/安装。

## 可证实与不宜夸大的指标

- **可证实**：报告记录本地 105 passed / 1 skipped、82% host-side 代码覆盖；GitHub CI 成功。
- **可证实**：5/5 完整策略成功（该基准）、32/32 实际攻击运行拒绝、真实额度恢复。
- **不可宣传**：“任何迁移都能 100% 成功”“生产级沙箱安全已经认证”
  “提高普遍成功率 25%”“单次实验成本节约具有统计显著性”。
