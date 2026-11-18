---
id: "arXiv:2611.22115"
title: "TeleChat to Xing4: Advancing Frontier Agentic Models on Domestic Chips and Frameworks"
arxiv_id: "2611.22115"
analyzed_at: "2026-11-19"
content_hash: "779f7e6a17e46b29ddce301f3c1214cca4ac831d4a02e0fb8aeba1c797db82f8"
schema_version: 1
selection_channel: "A2_heat"
selection_groups: []
selection_score: 0.0
selection_tier: null
selection_model: "deepseek-flash"
selection_fulltext: true
---
paper_id：`arXiv:2611.22115`

## TL;DR（三句半）

**坐标**：演进＋缝合——坐落在 TeleChat→TeleChat2/2.5/T1→TeleChat3-MoE 的系列延长线上（§1），MLA、DSA、IndexShare、DSpark、Muon 均为外部已有件（§1、§2.2、§2.4、§3.2），本文增量是把它们拼装并适配到 Ascend 910C + MindSpore。

**痛点**：在国产栈上做长上下文 MoE，卡在 ①MLA 仍要读大量历史 KV，decode 延迟随上下文涨；②深窄网络的归一化/路由/索引等非矩阵开销压低 Cube 利用率；③PP 气泡与主机调度干扰（§2.1、§3.1、§5.1）。

**手术**：把「MLA-only + 60 层深窄 MoE + 靠 PP 切层省显存 + 整层重算」换成「MLA+DSA+IndexShare + 45 层宽浅 MoE + HSDP(PP=1) + owner 化 Muon + 背包式细粒度重算 + CPU 绑核隔离」。

**疗效**：285B 在 8,192 张 910C 上跑通预训练（Abstract/§3）；1,024 卡上 step time 36.4s→31.8s（1.14×，Table 7），再叠 owner-Muon −9.1%（Table 8），逐层 indexer 反传单独 −15.2%（Table 11）。

**代价**：换来的是通信与主机侧开销——per-parameter 零拷贝集合通信、跨 SuperPoD 粗粒度 AllReduce、owner 热点与临时显存峰值；落地要优化 owner 分配/形状均衡与 CPU 隔离部署。

## 六段

### 总体介绍
Xing 4 是中国电信 Xingchen AGI Lab 的 TeleChat 系列新一代模型族技术报告，主线不是刷榜，而是回答「在 Ascend NPU + MindSpore 上，百 B 级 MoE 长上下文模型怎么训得动、推得快」。产出包括一个已开源的 29B-A4B MoE 成员、一个 285B 代表配置的系统级实测，以及一套 Agent/推理后训练基础设施 TeleGym。

### 核心思想
不是「提出某个新算子」，而是把**架构选择、并行策略、重算粒度、主机调度放进同一张执行成本账**上联合求解：先认定 910C 的硬件画像（Cube 矩阵算力强，Vector 与 HBM 带宽相对受限，§2），再据此反推「矩阵密集优先、稀疏访存减压、层间串行最少」的架构定型原则；系统侧则按 SuperPoD 两级带宽层次（域内高带宽、域间受限）决定什么通信放在哪一层。第二层思想是**把离散工程决策变成可解的优化问题**：哪些算子保留输出、哪些重算，被写成 0–1 背包（式 1，§3.3.1），用 profile 出的耗时与输出占用联合排序，而不是按层一刀切。

### 技术路线与结构特点
四条线并行推进、互相咬合：
1. **架构线（§2）**：MLA+DSA+IndexShare 做长上下文（索引跨层复用，索引计算降到 0.21×）；深度-宽度实验选 45 层宽 FFN 而非 60 层窄 FFN；DSpark 用「块级 draft 模型 + 轻量 Markov 头 + 接受置信头」做投机解码。
2. **预训练系统线（§3）**：HyperParallel-FSDP 把分片限制在 SuperPoD 内、跨域只做粗粒度梯度归约，从而 **PP=1**；per-parameter 零拷贝通信替代 PyTorch FSDP 的 flatten 打包；owner-based Muon 让全矩阵 Newton–Schulz 只在单一 owner rank 上做；逐层 indexer 反传把辅助图的生命周期从「等到主分支反传结束」缩到一层。
3. **长序列数据面（§3.4）**：Q-block 头尾配对（CP/TP 内）+ 跨 DP 的 SNAKE 重排（用文档边界元数据预估代价）+ 文档感知 KV 窗口压缩（Q=[32,40) 只需 KV=[28,40)，图 15）。
4. **后训练线（§4）**：TeleGym（执行/沙箱/会话）+ 中间 server（把交互转成带 policy 版本的轨迹组）+ Slime（rollout 调度与策略优化），用分离式异步执行、有界过采样、partial rollout 吸收任务时长异构。

### 主要贡献
- 给出了一套**可复用的「硬件画像 → 架构选型」推导链**：不是「我们选了 MLA+DSA」，而是「因为 910C 的 Vector/HBM 相对弱，所以选稀疏访存 + 宽浅拓扑」，并用受控实验（Table 2/3/4）支撑每一步。
- 把 FSDP 的**通信拓扑显式对齐到 SuperPoD 两级带宽**，用「消除 PP」而非「优化 PP」解决气泡问题——这是一个方向性选择，不是调参。
- owner-based Muon 解决了一个具体矛盾：**全矩阵正交化 vs 参数分片**，思路是「移动计算而非移动状态」（参数与 momentum 布局不变，Table 8 因此可归因）。
- 重算选择从「按层/按算子类型」推进到「按 profile 求解的 0–1 背包」，并额外处理了 exclude 区内的**边界激活驻留**问题（§3.3.2）。
- TeleGym 把「执行级 reward 分组」写进框架：一次多轮执行产生的多个训练段只算一次尝试，避免多工具调用任务在 group 统计里被重复计数（§4.1）。

### 历史地位
待观察：发表 2 天，被引 0 次，施引数据不足。

### 局限与不足
**作者承认：**
- 注意力方案的对比是 **Zhanlu 仿真**（32 卡、256K），"仿真不能覆盖全部生产 serving 开销，结果可能比端到端部署更乐观"（§2.2）。
- 系统优化的 step time 与算子拆解均**停留在 1,024 卡剖析尺度**，8,192 卡只报了「跑通」与 CPU 隔离的 5–10%（§3、§5.2）。
- owner-based Muon 的临时显存预算"是启发式而非全局内存上界"；批处理会改变浮点结果，**不保证逐位等价或收敛不变**；收益取决于分配均衡度与矩阵形状分布（§3.2）。
- 逐层 indexer 反传"不能被描述为零显存开销"——Top-K 输出、早反传输入、组内共享投影仍占显存（§3.4.1）。
- 动态 Q-block 配对是"保守启发式而非全局最优搜索"，多文档 TND 下因果位置重置使头尾互补不再保证均衡；DP 打分与 dynamic paired_owners 的成本口径**可能不一致**（§3.4.2）。
- KV 窗口压缩后 **AllGather 仍覆盖全长**（图 15）；折叠方案只适用于同步 CP、PP=1 的稀疏 DSA 路径（§3.4.2）。
- CPU 隔离收益高度依赖规模：512 卡 1–2%，8,192 卡 5–10%（Table 5-1）。

**分析推断：**
- **缺模型质量证据**：全文给出的是吞吐/延迟/step time，未见 Xing 4 在 MMLU/GSM8K 等上的精度数字，也未做「宽浅 vs 深窄」「DSpark vs 基线」的质量对照。Table 5 只在 6 个 benchmark 上报吞吐，未报接受长度与精度代价，因此**效率结论无法与质量结论对齐**。
- **基准不完全对等**：Table 7 中 PP-1F1B / PP-Interleaved / HSDP 的 micro-batch 分别是 8 / 4 / 2，step time 的差异混入了 mbs 变化，1.14× 不是纯粹的「去 PP」效应。
- **数字口径未闭合**：Abstract 称消除 PP 带来 **16.1%** 吞吐提升，Table 7 报的是 step time 36.4s→31.8s（−12.6%、1.14×，折合吞吐 +14.4%），文中未给出 16.1% 的对应表格或定义。
- **不可复现边界**：依赖 Ascend 910C / SuperPoD / MindSpore HyperParallel 专有栈；仅 29B 权重开源，285B 权重与完整训练代码未见说明；正文多处以「不得描述为…」「需以源码为准」自我限定（§3.4.1–3.4.2），说明部分结论依赖具体版本。
- **后训练章节在提供正文中被截断**，TeleGym 的实测（任务成功率、reward 曲线、异步带来的 policy lag 影响）无法核实；「万亿参数训练中」为 ongoing，无数据。
- 作者列表约 140 人、技术报告体裁，单个机制的独立可归因性弱。

## 背景透镜（chip）

- **瓶颈资源**：三层瓶颈并存——(a) HBM 带宽与容量（历史 KV 访问、激活驻留）；(b) Vector 计算（910C 上 Cube 强、Vector 相对弱，§2）；(c) 主机 CPU 调度与跨 SuperPoD 通信带宽（§3.1、§5.1）。非矩阵开销（归一化、路由、索引、调度、数据搬运）是贯穿性的第四类损耗（§2.1）。
- **把什么换成了什么**：
  - 用**稀疏访存**（DSA Top-K + IndexShare 跨层复用索引）换 HBM 历史 KV 读取：KV HBM 0.80×、索引计算 0.21×（Table 2）。
  - 用**更宽更浅的矩阵密集计算**换层间串行开销：45 层 vs 60 层，prefill 1.20×、decode 1.11×、训练吞吐 +≈10%（Table 4、§2.3）。
  - 用 **HSDP 的数据维分片**换 PP 的层维内存切分：PP=1，气泡归零（Table 7）。
  - 用**重算**换激活显存，但用背包把「重算什么」变成按 profile 的性价比选择（式 1、Table 9）。
  - 用**单 owner 全矩阵 NS** 换掉「每 rank 重复做全矩阵 NS」的冗余（Table 8）。
  - 用**主机 CPU 绑核/隔离**换掉调度抖动（Table 5-1）。
- **代价落在哪个环节**：
  - 通信环节：per-parameter 通信放弃了 flatten 融合，集合通信**调用次数上升**（靠域内高带宽吸收）；跨 SuperPoD 的粗粒度 AllReduce 仍暴露在关键路径外沿（§3.1，图 4）。
  - 计算热点环节：owner 承担全矩阵 NS 与批处理，**负载与临时显存向 owner 集中**，收益取决于形状分布与分配均衡度（§3.2）。
  - 主机环节：CPU 绑核/隔离是**部署约束**（需独占核、迁走中断），收益随集群规模从 1–2% 到 5–10%（Table 5-1）。
  - 数值环节：批处理与执行重排改变浮点结果，精度配置成为数值正确性的一部分（§3.2）。
  - 可优化性：owner 分配与背包预算作者明说是启发式，仍有调优空间；KV 窗口的 AllGather 全长未消。
- **什么条件下会失效**：
  - **序列短**：DSA/IndexShare/KV 窗口/长序列配对全部失去收益，稀疏索引的固定开销反而占优（§2.2 前提是 256K/1M 级）。
  - **batch / 并发小**：decode 吞吐收益被摊薄，TPOT 改善主要靠并发摊薄访存（Table 2 的 decode 侧为 TP2DP8）。
  - **单文档 / 文档边界少**：KV compact 退化为全长前缀，头尾互补配对也不成立（§3.4.2–3.4.3）。
  - **集群规模小**：CPU 隔离仅 1–2%（Table 5-1）。
  - **shard 布局不支持 P2P 或形状极端不均**：owner-based Muon 回退到原路径（§3.2）。
  - **非同步 CP 或 PP>1**：网络级折叠方案不适用（§3.4.2）。
- **能否反向迁移到我的领域**：可以，且可迁移的是**方法形态**而非具体算子：
  1. 「先测硬件画像（哪类计算/访存相对稀缺），再反推结构选型」——任何加速器适配都适用。
  2. 「把离散工程开关写成带预算的 0–1 背包，用 profile 的收益/代价排序」——这是本文最可移植的一招，Table 9 说明了「按输出大小排序会选错算子」（最大输出 516MiB 只省 13.47ms，最贵算子 38.32ms 只占 193MiB）。
  3. 「移动计算而非移动状态」（owner 化）——适用于任何需要全矩阵/全局归约但状态必须分片的场景。
  4. 「通信按拓扑层次分级：细粒度留在高带宽域内，粗粒度跨域」——通用并行设计原则。
  5. 后训练的「异步解耦 + 有界过采样 + partial rollout + 执行级 reward 分组」——可迁移到任何时长异构的 rollout 训练系统。
  6. 需要警惕的反面：**以上全部依赖长序列/大集群前提**，短序列小集群直接照搬会亏。

## 客观信号

venue：arXiv 预印本（cs.AI），`arXiv:2611.22115v1`，2026-11-17，无会议/期刊信息｜被引：0｜发表至今：2 天｜有无代码：部分——29B 成员 Xing4.0-29B-A4B 权重与模型卡已开源（Hugging Face，§1、§2.5）；训练栈 MindSpore HyperParallel 指向官方仓库；**285B 权重与完整训练/后训练代码在正文中未见开源说明**。

## 引用出处

| 数字/事实 | 出处 |
|---|---|
| 29B 成员：29B 总参、4B 激活、256K 原生上下文、mHC+MLA+MTP | Abstract；§1；§2.5 |
| 285B 成员：45 层、4096 hidden、256 routed experts、8 experts/token、1 shared、≈17B 激活、1M 上下文 | §2.5 Table 6 |
| 8,192 张 Ascend 910C 预训练 285B | Abstract；§3 |
| 注意力仿真设置：32 卡、P/D 分离、TP4DP4 / TP2DP8、256K | §2.2 Table 1 |
| MLA+DSA：KV HBM 0.85×、索引计算 0.37×、prefill 2.67×、decode 1.18× | §2.2 Table 2 |
| MLA+DSA+IndexShare：KV HBM 0.80×、索引计算 0.21×、prefill **4.68×**、decode 1.27× | §2.2 Table 2；Abstract |
| 深度-宽度：60 层→45 层，TTFT 0.83×、TPOT 0.90×、prefill 1.20×、decode 1.11× | §2.3 Table 4 |
| 宽浅拓扑训练吞吐 +≈10% | §2.3 |
| 投机解码：MTP3 1.80–1.93×、DSpark **2.67–3.77×**、DSpark/MTP3 1.48–2.10×；HumanEval 35.7/64.1/134.7 tok/s | §2.4 Table 5 |
| HSDP 实验条件：1,024 NPU、seq 262,144、global batch 16、TP4/EP64/CP32 | §3、Table 7 |
| PP-1F1B 36.4s → PP-Interleaved 34.2s（1.06×）→ HSDP(PP=1) 31.8s（1.14×，step time −12.6%）；mbs 分别为 8/4/2 | §3.1 Table 7 |
| Abstract 称消除 PP 带来 16.1% 吞吐提升（与 Table 7 口径未对齐） | Abstract vs §3.1 Table 7 |
| owner-based Muon：31.8s→28.9s（−9.1%，1.10×） | §3.2 Table 8 |
| 背包式选择性重算：28.9s→26.9s（−6.9%，1.07×） | §3.3 Table 10 |
| 单层 150 个 kernel、115.1ms；SparseLightningIndexerGradKLLoss 38.32ms/193MiB；SparseFlashAttention 13.47ms/516MiB；AllGatherMatmul 15.90ms/656MiB | §3.3.1 Table 9 |
| 逐层 indexer 反传：28.9s→24.5s（−15.2%，1.18×），三者均为独立对照 HSDP+Muon | §3.4.1 Table 11；§3 引言 |
| 组合流水线算子回归：8 卡/8 层/16K、TP2/CP4、主 loss 最大绝对差 0.000530、MTP loss 0.000441 | §3.4.3 |
| CPU 绑核+隔离：512 卡 1–2%、8,192 卡 5–10% | §5.2 Table 5-1 |
| 万亿参数训练进行中（无数据） | §6 |
