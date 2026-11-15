---
id: "arXiv:2610.36848"
title: "Evolving Agent Teams"
arxiv_id: "2610.36848"
analyzed_at: "2026-11-16"
content_hash: "8bb9e3311f1e3fa65f79e23025d19ddefd19a13aa0d8f7e91e3b595a2fe2b71a"
schema_version: 1
selection_channel: "A1_watchlist"
selection_groups: ["llm-inference", "ai-infra", "model-architecture", "agent"]
selection_score: 4.0
selection_tier: null
selection_model: "deepseek-flash"
selection_fulltext: true
---
## TL;DR（三句半）

**坐标**：演进 —— 论文 §3.1 把既有系统统一写成固定的团队配置 `T=(R,Π,M)`（角色/协调/知识管理三者开跑前即定死），§2 指出 DyLAN 只从手工角色池里选、AgentVerse 每轮重建且不保留全局上下文、ADAS 演化的是"最优单体架构"；EAT 把 `T` 本身变成运行时被演化的对象。不是开山（多智能体、演化搜索都已存在），不是跨界替代（仍在 coding agent + kernel 优化内）。
**痛点**：以前在真实生产 GPU kernel（FlashInfer MLA decode）这类长周期开放式优化场景，卡在 agent 早期选了易实现的架构方向（WMMA 而非 WGMMA）后锁死——Codex 5.5 xHigh 花 $1K 只到专家 28%（§1），Claude Code 在 47 个生产 workload 上平台于 0.51×（§4.2）。
**手术**：把"人预先设计的固定团队"换成"supervisor 诊断每次 plateau 的结构性成因、重构下一代团队 + 跨版本持久文件系统 K"。
**疗效**：从零生成 CUDA kernel，1.21× 于生产 FlashInfer MLA paged decode，47 个生产 workload 胜 36 个（Abstract / §4.3）。
**代价**：supervisor 反思 + 团队重启的上下文重建吃掉大量 token 与墙钟——单次完整运行可达 $3.0K / 186 小时 / 73 代（Appendix C）。

## 六段

### 总体介绍
Evolving Agent Teams 是一个多智能体框架，让"团队组织"与"被优化程序"共同演化，用于开放式程序优化（GPU kernel 为主战场），目标是解决强 coding agent 在长周期任务上收敛到局部最优的问题。

### 核心思想
不是把 agent 变强，而是让**组织的形状跟着问题阶段变**。论文的形式化很有信息量：既有系统优化解 `s`，但从不优化团队配置 `T`。EAT 的核心断言是——长周期任务里"有效的劳动"会定性改变（从知识获取 → 实现 → 调试硬件原语 → profile → 架构重设计），而固定角色/固定协调协议/固定知识管理会在阶段切换处失配；因此失配是**结构性**的，不是能力性的（Appendix B 用 SMEM 布局 bug 案例证明：信息在团队里，但被角色边界切碎，谁都读不到全貌）。据此，supervisor 把"失败模式"当诊断输入，把"下一代团队"当输出。

### 技术路线与结构特点
双层循环。**Team evolution**：supervisor 由"当前团队跑完"或"固定间隔 cron"触发，读进度、反思行为模式（滑向易路径 / 前人洞察丢失 / 卡在调试 / 过度微优化），沿 `T=(R,Π,M)` 三轴重新设计——角色现场发明而非从池中选（Appendix A 记录 20+ 个，如 Profiling Scientist、Challenger、Knowledge Integrator、Workload Analyst、Dead-End Revisitor）、协调可 pipeline/并行分支/混合（例如一个 agent 专攻阻塞 bug 且禁碰主干，另一个继续优化）、知识管理指定产物写到哪里。**Team execution**：Claude Code agent 带 shell/编译/profiler 自由迭代。**文件系统 K**：learnings document + per-team artifacts + evolution history，跨所有团队版本持久。与 FunSearch/AlphaEvolve 的关键区别：**无种群、无 crossover**，是顺序的、诊断驱动的重构。

### 主要贡献
1) 把多智能体的 team configuration 从"人工预设常量"提升为"运行时被演化的变量"，并给出 supervisor + 持久文件系统这一可运行机制；2) 匹配 token 预算下在 4 个 kernel 任务（FA4/GDN/MLA/G+AR，跨 B200/H100）一致优于 Claude Code，MLA 上 0.74–0.75× vs 0.51×（§4.2）；3) 加预算后从零超出生产 FlashInfer（1.21×，36/47）；4) 消融把增益归因于"演化"而非"模型"：固定四角色 0.31×、Codex 0.28×、EAT 0.74×，同模型同多智能体结构（§4.5）；5) 跨域外推——AMD MI350X KDA 3.01× over vLLM、B200 3.44× over FlashKDA、Apple M4 Pro MoE offload 30.3 tok/s、RoboDojo 62.1 vs 榜首 55.3（§4.7）；6) 对已被专家优化过的实现（Flash-KMeans Triton→CUDA WGMMA）仍达 1.71×（§4.4）。

### 历史地位
待观察：发表 20 天，被引 0 次，施引数据不足。

### 局限与不足
- **作者承认**：主设置每个只跑两次、§4.7 每个设置只跑一次，统计结论受限；cost-vs-performance 的 Pareto 分析和"更嘈杂领域"的受控研究留作未来工作（§5）。明确指出部分 run 起始于既有 kernel 的移植，衡量的是"在强起点上的改进"而非从零（Appendix C）。
- **分析推断**：
  1. **元层未演化**。supervisor 的 prompt 全程固定（Appendix C 明言"same supervisor prompt"），被演化的是它设计出的团队；谁是"诊断者"的诊断质量本身无机制保障，也无失败回退。
  2. **评测口径偏乐观**。"两次取最好"（§4.1）+ 单次运行的跨域结果，方差未被刻画；GPU kernel 与 RoboDojo 都是单次。
  3. **强绑商业闭源模型**（Opus 4.6/5.5、Sonnet 5.5、GPT-5.5）；模型版本漂移会直接改变结论，且 $800–$3.0K/任务的成本门槛高。
  4. **任务同质性**。所有任务都有可自动执行的 harness 与数值正确性判据；supervisor 的诊断信号依赖这种稠密反馈，反馈稀疏或不可验证时机制未验证。
  5. **基线公平性存疑**。K-Search / CUDAForge 用"公开实现 + 同模型同预算"重跑（§4.6），但未见对其调参努力的校准说明。

## 背景透镜（chip）

- **瓶颈资源**：两层。优化对象的瓶颈是硬件资源——MLA decode 的极端寄存器压力下 CUDA core 与 tensor core 的重叠效率、FA4 在 Blackwell 上 tensor core 吞吐翻倍而 shared memory 带宽与非 matmul 单元不翻倍的非对称、G+AR 的跨卡通信延迟（§4.1）；优化过程的瓶颈则是 token 预算与墙钟。
- **把什么换成了什么**：把"单 agent/固定团队的顺序深挖"换成"supervisor 重构 + 并行分支 + 跨代知识继承"，本质是**用额外 token 与并行度换搜索方向的覆盖度与不早停**（回避"过早锁定 WMMA 路线"这一具体失败模式，§4.2/§4.5）。注意这不是硬件层的访存↔计算权衡，不要混用。
- **代价落在哪个环节**：落在 supervisor 的反思/诊断调用、团队重启时的上下文重建（文件系统只缓解不消除）、以及跨代重复的编译与 profile；这是 token 与 wall-clock 成本，不是运行时开销——**生成出的 kernel 本身不带这个代价**。可优化点：cron 触发间隔、learnings 文档的压缩与检索、已验证组件的复用粒度。
- **什么条件下会失效**：任务短或单阶段（无可诊断的阶段切换，重构开销摊不平）；反馈信号不可自动验证（supervisor 无从判定 plateau 成因）；正确性门槛极高且每次重构都丢上下文；预算极小（Appendix D 显示 $200 时 team 与 solo 差距很小，1.056× vs 1.047×，需到 $509 才拉开）。
- **能否反向迁移**：能，且方向明确——把"人工固定的配置常量"提升为"元层按诊断信号重构的对象 + 持久经验库 grounding"。对应到芯片侧：固定搜索空间的 autotuner → 由瓶颈诊断驱动重构搜索空间的调优器；编译器 pass 顺序/pragma 配置亦可套用同一双层结构。风险同上：诊断信号必须廉价且可靠。

## 客观信号

paper_id：`arXiv:2610.36848`｜venue：—（arXiv preprint, cs.AI, 2026-10-27）｜被引：0｜发表至今：20 天｜有无代码：有（https://github.com/caoshiyi/EAT）

## 引用出处

| 数字/事实 | 出处 |
|---|---|
| Codex 5.5 xHigh 花 $1K 仅达专家 28% | §1 Introduction |
| Claude Code 在 47 个生产 workload 平台于 0.51× | §4.2 |
| EAT 匹配预算 0.74×（§4.2）/ 0.75×（§4.2 正文另一处表述） | §4.2、Figure 3 |
| 1.21× mean speedup over 生产 FlashInfer，36/47 胜，其余 11 个 0.86–0.99× | Abstract、§4.3 |
| 固定四角色 0.31×；Codex 0.28×；EAT 0.74× | §4.5、Figure 6 |
| K-Search 0.602×/0.598×；CUDAForge 0.448×/0.283×；EAT 0.721×/0.685×；$800 预算 | §4.6、Table 2 |
| GDN prefill 2.2× → 4.06×（21 个团队版本）；G+AR 1.56× | §4.3、Figure 4 |
| Flash-KMeans 1.71× over Triton，最高 1.5× 更低延迟 | §4.4、Figure 5 |
| KDA：MI350X 3.01× over vLLM；B200 3.44× over FlashKDA；B300 3.54× | §4.7、Appendix C |
| MoE offload：48GB M4 Pro 上 30.3 tok/s（naive 13.4） | §4.7、Appendix C、Table 4 |
| RoboDojo 62.1 held-out vs 榜首 55.3、π0.5 单独 11.4 | §4.7、Figure 7d |
| 单次 KDA 运行 $3.0K / 186 小时 / 73 iterations | Appendix C |
| 匹配预算 $200 时 team 1.056× vs solo 1.047×；$509 时 team 1.083× | Appendix D、Table 4 |
| 每个主设置跑两次取最好；§4.7 单次 | §4.1、§5 |
| supervisor prompt 全程固定、无预定义角色 | §4.1、Appendix C |
| 既有系统 `T=(R,Π,M)` 固定，只优化 `s` | §3.1 |
| DyLAN 从手工角色池选、AgentVerse 每轮重建无全局上下文、ADAS 演化单体架构 | §2 |
