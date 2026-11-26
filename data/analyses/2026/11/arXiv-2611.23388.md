---
id: "arXiv:2611.23388"
title: "QDA: Closed-Loop LLM Quantization Design with Agents"
arxiv_id: "2611.23388"
analyzed_at: "2026-11-26"
content_hash: "2491600f4d07474d80511370a4c39f96c0f156b9a958c2d005760f5234d7ab2a"
schema_version: 1
selection_channel: "A1_watchlist"
selection_groups: ["llm-inference", "model-architecture", "training-alignment", "agent"]
selection_score: 4.0
selection_tier: null
selection_model: "deepseek-flash"
selection_fulltext: true
---
## TL;DR（三句半）
坐标：跨界替代——用 LLM agent 闭环搜索替代“专家为每个模型×精度手写 recipe”（§1：ModelOpt 为 28 个模型各维护一份手写 recipe，作者称不可扩展）。｜痛点：新模型/新精度持续涌现（§1：2025-10 至 2026-09 有 90+ 个 >10B 开源权重模型，按其四档精度需 360+ 份 recipe），卡在单次全量评估 23–86 GPU-hours、跨代码库组合方法要手工移植（GPTQ 在 256 专家 MoE 上构建约 16 GPU-hours）。｜手术：把“专家选方法+设置+校准数据”换成“agent 在 21 个可组合 PTQ 方法库上迭代”，评分改用原始模型 top-128 KL 代理（分钟级，350–990× 便宜）+ 长生成检查。｜疗效：320B MoE GLM-5.3-Flash 在 NVFP4 W4A4 恢复 FP8 精度 98.3%，高于 NVIDIA 的 96.9%（Table 3, §6.2）。｜代价：每搜索 64 GPU-hours + $19–75 token，另需维护 21 方法库与隔离 evaluator 的工程成本。

## 六段

### 总体介绍
QDA 是一个“让 LLM agent 为每个模型×每个量化精度自动设计 post-training quantization recipe”的系统。它不提出新的量化算法，而是解决“最好的 recipe 因模型和精度而异、专家手工重设不可扩展”这一工程瓶颈。

### 核心思想
论文的真正判断是：recipe 设计的瓶颈不在“想法”，而在**反馈回路的速度与保真度**。它把问题重构为——固定 GPU 预算下用 agent 做搜索器，关键是要造一个既便宜又不骗人的代理指标，并给出三条设计原则：

1. **代理必须在整模型 + 真实 serving kernel 上测**。逐层输出误差（Eq.3，GPTQ/AWQ 优化的目标）看不到跨层累积误差，也看不到 kernel 引入的误差（如 W4A8 kernel 把 scale 存成 FP8）。§4.2 给出反例：Qwen3.5-9B W4A16 上逐层误差偏好 GPTQ，但 AWQ 的准确率反而高 5.1 点。
2. **代理必须对 agent 不可见**。否则 agent 可以直接在参考 trace 上校准来刷分（§5.3 明确写出这条攻击路径）。
3. **代理看不见的失败要用另一种信号补**。量化会让 reasoning model 想得更久、更容易陷入重复循环或截断（§4.3），这些失败只在长生成后段出现，KL 看不出，于是加“长生成检查”。

配套的第二半是：把“方法能不能组合”从 agent 的负担变成基础设施的负担——所有方法统一为 checkpoint→checkpoint，层间依赖关系从模型自身推导而非硬编码。

### 技术路线与结构特点
**问题形式化（§4.1）**：recipe = 有序方法列表 + 各自设置 + 校准数据；目标 max A(x) s.t. Σc ≤ B（Eq.4），但 A 只在最终 checkpoint 上测一次。

**四步闭环（Fig.3）**：(1) agent 提案 → (2) 库构建 checkpoint → (3) evaluator 打分（分钟级）→ (4) 少数候选做长生成检查。

**KL 评分（§4.2, Eq.5–6）**：在固定参考 trace（48 个 prompt × 2 条 BF16 continuation，覆盖数学/科学/代码/写作规划，与测试集做 id + 归一化哈希 + 13-gram 三重比对无命中）上，逐 token 平均 KL。为省存储只保留 top-k=128 的 token 加一个残差桶：Proposition 1 证明该估计**永不超过**全量 KL（log-sum 不等式），实测低估 0–0.4%；缓存从 0.21–0.56 TB 降到 0.47–1.09 GiB。
**并列判定**：KL 差小于 σ（同一 recipe 换校准数据子集重建的相对标准差，0.5–1.6%）即标为 tie，防止 agent 追噪声。

**可组合库（§5.1, Table 8）**：在 llm-compressor 上扩到 21 个方法，分四类（变换 / rounding / scale 与端到端调优 / 校准数据）+ 组合操作。三个工程点：
- 层间关系从模型自身推导并数值校验，新架构只需 adapter（GLM-5.3-Flash 用了 72 行 + FP8/MTP 扩展，其余三个模型 adapter 为 0–14 行）；
- 按 kernel 实际存储精度选码（W4A8 的 FP8 scale），而非按 quantizer 的 BF16 假设；
- MoE 的 768 个小 solve 堆叠二阶统计量做一次 batched 分解，GPTQ 构建 16 → 0.42 GPU-hours（KL 差 <1%）。

**Harness（§5.3）**：agent 在无 GPU、无网络的 sandbox 里，只能通过 4 条命令触达系统（qda / kl / qsched / handin）；参考 trace 与测试集在 sandbox 外只读；单优先级队列 + deadline 检查；12 次搜索中 KL 打分中位 5 秒启动，GPU 占用率 72%。

### 主要贡献
相对既有工作，它多了四样东西（不是多了一个量化算法）：

1. **搜索维度**：AMQ / EvoPress 只搜 per-layer bit-width，OptiKIT 跑用户给定的 recipe，ModelOpt 的 recipe-search skill 只在自家内置选项里选、不写新方法/新数据，HAQA 调微调超参与 kernel 设置（§7）。QDA 搜的是**方法组合 + 设置 + 校准数据**，且允许 agent 自己写代码、自己造校准数据。
2. **代理指标**：全模型、含 serving kernel 误差的 top-k KL；在 44 对 checkpoint 上排序与准确率一致率 82%，逐层误差为 73%（Fig.2c）。
3. **可跨架构组合的库**：checkpoint→checkpoint 接口让 21 个方法任意堆叠，并覆盖 Gated DeltaNet 混合架构与 256 专家细粒度 MoE。
4. **评测纪律**：agent 不可见 evaluator、20-gram 污染扫描、用 build 噪声定 tie 阈值、只有最终 checkpoint 上测试集。

**实证**：四个 reasoning model 上平均超最强公开 recipe 0.9 / 1.4 / 2.2 点（W4A16 / W4A8 / W3A16，每次搜索 64 GPU-hours）；R1-Llama 在 W3A16 上超 GPTQ 6.1 点；320B MoE NVFP4 恢复 98.3% FP8 精度；所有 checkpoint 跑现有 kernel，无推理开销，W4A16 解码比 ParoQuant 快 1.30–1.75×。消融显示换掉 agent（greedy / ASHA）掉 0.2–1.2 点，换掉基础设施掉 0.2–0.5 点，同一 agent 换 ModelOpt 掉 0.5–1.3 点——增益来自“搜索流程 + 基础设施”，不只是 agent 本身。

### 历史地位
待观察：发表 9 天，被引 0 次，施引数据不足。

### 局限与不足
**作者承认**：
- 在 Qwen3.6-35B-A3B 的 W3A16 上，QDA 比 GSQ 低 0.2 点（§6.2, Table 2）。
- Table 5b 的“固定 recipe”是从 QDA 自己的搜索里提炼的，作者明说“不是独立 baseline”。
- 不修基础设施时，agent 自己写 3.1k 行代码（vs 1.1k），从未尝试 rotation，多提交 50% 任务、损失 6.6% vs 3.1% 的 GPU-hours（§6.3）。
- Qwen3.8-27B W4A8 上 agent 主动跳过 distillation，因为 FP8 scale 的最小步长会整体重缩放 ≥6%，太粗（§6.4）。
- 复现依赖具体 agent 版本与预算：Claude Code v2.1.303 + Opus 5.5，$19–75 token/次（§6.1）。

**分析推断**：
- **统计功效弱**：除 Qwen3.6-35B-A3B 做了 3 次搜索（std 0.1–0.2 点），其余每个 model×precision 只有 1 次搜索；0.9 点的 W4A16 均值增益虽远大于 0.1–0.2 的噪声，但跨模型的搜索方差未被估计。
- **代理指标的验证面窄**：44 对排序验证集中在 8B–35B 模型的 W4A16/W4A8；NVFP4 与 320B 只有最终结果，没有“KL 排序是否正确”的独立检验。
- **搜索空间被 admissible recipe 约束裁剪**：必须沿用公开 baseline 的模块、位宽、格式、kernel，因此排除了混合精度（AMQ / EvoPress 的主战场），也排除了 ParoQuant 的 online rotation（需新算子）。QDA 在“不新增算子”的约束内取胜，这个结论不能外推到任意搜索空间。
- **前沿模型依赖**：整套方法的行为（会不会想到 rotation、会不会自己诊断 loss spike）绑定在特定闭源 agent 上；换模型/换版本的可迁移性未测。
- **成本未必更低**：64 GPU-hours × 每年 360+ 份 recipe ≈ 2.3 万 GPU-hours，论文只与“专家手工重设”做定性比较，没有人类专家基线的时间/成本数据。
- **潜在过拟合面**：task statement 明确告知四个评测域（数学/代码/科学/指令遵循）且“等权计分”，agent 据此自选 hard prompt 做长生成检查；虽然测试条目被隔离，域级先验仍可能引导搜索偏向。
- **可复现性悬空**：库、evaluator、harness 均为“will release”（摘要、§8），截至写作时未见发布链接。

## 背景透镜（chip）
- **瓶颈资源**：双层的。搜索侧瓶颈是 **GPU-hours**——单次全量评估 23–86 GPU-hours（§3.2）、GPTQ 构建 16 GPU-hours（§3.3）；服务侧瓶颈则是**显存容量与 weight-read 带宽**（§2.1：W4A16 让 320B 模型从 2 张 GB300 降到 1 张；解码受 weight read 限制），以及大 batch / prefill 下的 **tensor-core 算力**（W4A8、NVFP4 才真正降低算术）。
- **把什么换成了什么**：三层替换。①评估：把「两模型全量长生成 + 逐题评分」换成「缓存原始模型 top-128 分布 + 候选模型单次前向的 KL」（350–990× 便宜）；②构建：把「跨代码库手工移植 + 逐层串行 solve」换成「checkpoint→checkpoint 统一接口 + 模型自推导层关系 + MoE 批量分解」（16 → 0.42 GPU-hours）；③决策：把「人类专家的试错」换成「agent 的 token 消耗」（$19–75/次搜索）。本质是**用一次性的离线缓存与工程抽象，换重复的在线全量计算**。
- **代价落在哪个环节**：①搜索期——64 GPU-hours/搜索仍在，KL 打分单次仍要 3.2–11.7 GPU-min（App. C），agent token 成本不可压缩；②适配期——每遇新架构要写 adapter，若方法需要新算子则整类被排除；③系统期——需要 sandbox 隔离 + 优先级队列调度器，缺了它 agent 多提交 50% 任务、失败损失翻倍；④**服务期为零**：所有 checkpoint 跑原格式 kernel，作者在 §6.2 专门对照 ParoQuant 的 online rotation 说明这一点。可优化项集中在前三项（缓存复用、库复用、调度），token 成本难优化。
- **什么条件下会失效**：①代理与目标排序背离时——44 对里有 18% 排序错，KL 领先但准确率落后是可能的；②失败只出现在长生成后段（重复循环、截断）时，KL 无感，必须靠检查，而检查的 prompt 是 agent 自选的，覆盖不可保证；③kernel 精度约束与 quantizer 假设冲突时——W4A8 的 FP8 scale 步长 ≥6%，直接让 distillation 失效（Qwen3.8 实例）；④目标模型输出短、全量评估本来就便宜时，350–990× 的相对优势缩水，整套动机减弱（本文场景是数万 token 的 reasoning model）；⑤拿不到可缓存分布的原模型、或用不了同等强度的前沿 agent 时，方案不成立。
- **能否反向迁移到我的领域**：可以，且迁移的不是量化而是**四件方法论部件**：(a)「用整系统级、含部署栈误差的分布距离替代分层局部代理」——任何存在“局部代理 vs 全局目标”错位的调优问题（剪枝、稀疏度分配、编译 pass 顺序）都适用；(b)「evaluator 对优化器不可见 + 用 build 噪声定 tie 阈值」——agent 自动优化时通用的防刷分范式；(c)「把搜索空间做成 checkpoint→checkpoint 的原子操作库，让 agent 只做组合决策」——这是把 agent 从“写代码”降级为“做选择”的关键，直接可搬到 kernel 调度 / 流水线切分搜索；(d)「单一优先级队列 + deadline 检查 + job 脚本快照」的预算内调度。**不可迁移的部分**：整套方案依赖“原始模型分布可离线缓存且固定不变”，一旦目标函数随训练漂移，代理缓存即失效。

## 客观信号
venue：—（arXiv preprint，cs.LG）｜被引：0｜发表至今：9 天｜有无代码：论文摘要与 §8 称 “We will release the library, the evaluator and the harness”，截至本文写作未见发布，属承诺而非既成事实

## 引用出处
| 数字 / 事实 | 出处 |
|---|---|
| 单次全量评估 23–86 GPU-hours（R1-8B 23.3–26.0；Qwen 59.9–86.2） | §3.2；§4.1；App. C |
| 一次 GPTQ 构建 Qwen3.6-35B-A3B 约 16 GPU-hours → 0.42 GPU-hours | §3.3；§5.1 |
| KL 评分比全量评估便宜 350–990× | §1；§5.2；App. C |
| 单次 KL 打分 3.2–11.7 GPU-min | App. C |
| top-128 缓存 0.47–1.09 GiB，全量 BF16 需 0.21–0.56 TB | App. C |
| 残差桶平均占概率 0.015–0.048%；估计低估 0–0.4% | §4.2；App. C |
| KL 排序一致率 82%（44 对），逐层误差 73% | §4.2；Fig. 2(c) |
| tie 阈值 σ：0.0159 / 0.0047 / 0.0093 / 0.0063（对应四个模型） | §4.2；App. C |
| 21 个 PTQ 方法 | §1；§5.1；Table 8 |
| +0.9 / +1.4 / +2.2 点（W4A16 / W4A8 / W3A16） | Abstract；§1；§6.2；Table 2 |
| R1-Llama W3A16 超 GPTQ 6.1 点 | §6.2；Table 2 |
| 320B MoE NVFP4 恢复 98.3%，NVIDIA 96.9%、Red Hat 96.3% | Abstract；§6.2；Table 3 |
| QDA checkpoint 183.6 GiB vs 190.4 / 184.3 GiB | §6.2 |
| 320B 搜索：16×GB300 约 6.5 小时，$69 token | §1；§6.2 |
| 每次搜索 8×GH200 × 8 小时 = 64 GPU-hours，$19–75 token | §6.1 |
| W4A16 解码比 ParoQuant 快 1.30× / 1.57× / 1.75×（batch 64/16/1） | §6.2；Table 7 |
| 答案短 4%，撞长度上限少 1.7 点 | §6.2 |
| 同 agent + ModelOpt 落后 0.5–1.3 点 | §6.2；Table 4 |
| 无基础设施：3.1k vs 1.1k 行代码，多 50% 任务，失败损失 6.6% vs 3.1% | §6.3 |
| greedy / ASHA 落后 0.2–1.2 点（W4A8、W3A16） | §6.3；Table 5a |
| GPU 占用率 72%，KL 打分中位 5 秒启动 | §5.3 |
| 15 个月 90+ 个 >10B 开源权重模型 → 需 360+ 份 recipe | §1 |
| ModelOpt 为 28 个模型各存一份手写 recipe | §1 |
| GLM-5.3-Flash adapter 72 行；另三个模型 0–14 行 | §5.1；App. D |
| Qwen3.8-27B W4A8 的 FP8 scale 最小步长 ≥6% | §6.4 |
| 参考 trace：48 prompt × 2 条 BF16 continuation | App. C |
| Agent：Claude Code v2.1.303 + Opus 5.5，1M 上下文，high effort | §6.1；App. E |
