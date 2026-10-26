---
id: "arXiv:2610.31667"
title: "Direct Pass@$k$ Optimization for Thought Pruning: Balancing Quality and Diversity in Test-Time Reasoning"
arxiv_id: "2610.31667"
analyzed_at: "2026-10-26"
content_hash: "bebdce14bebf157d6f9b099d2cf3867a90b2996c191605e622b3e45ac30f6471"
schema_version: 1
---
## TL;DR（三句半）
坐标：缝合 — 论文将质量型剪枝（Linear Probe）与多样性剪枝（RepExp）统一到直接优化 `pass@k` 的目标（§1、§3.3）。  
痛点：以前在测试时缩放中，从 N=128 条 partial rollouts 里选 k 条继续生成，卡在质量型选相似高置信 rollout 一起错、多样性型选 outlier 浪费预算，缺少直接平衡两者的剪枝准则（§1、§4.2）。  
手术：把原来“对每条 rollout 单独打分/只按表示残差选”替换为 latent Gaussian joint outcome model，用二阶闭式 surrogate 估计集合失败概率，并贪心最小化 `log L̃(S)`（§3.1–3.2）。  
疗效：DeepSeek-R1-Distill-Qwen-1.5B 在 k=4 时，PKTP 比 Linear Probe 高 3.0 pp，比 RepExp 高 3.2 pp（§4.2）。  
代价：引入一次 joint likelihood 训练和每题的贪心选择，复杂度 `O(k(Nd+d^2))` 时间、`O(Nd+d^2)` 内存（§3.2）；落地需更优 partial rollout 表示以增强质量信号（§4.2）。

## 六段
### 总体介绍
PKTP 是测试时思维剪枝框架：先让 LLM 对每题生成 N 条前缀到 cutoff T，再从中选 k 条继续生成至 budget B；它直接以所选池的 `pass@k`（至少一条正确）为目标，而不是只做单条质量打分或只做多样性选择（§1、§2）。

### 核心思想
关键不是“再设计一个质量-多样性加权分”，而是建模所选集合里多条 rollout 的联合失败事件。它假设每条 rollout 的潜在正确性分数 `Z_i = q_i + h_i^T f + m_i`，其中 `q_i` 是个体质量，`h_i^T f` 让相似表示共享失败相关性，`m_i` 为独立噪声；正确性由 `Z_i ≥ 0` 阈值决定。于是 `pass@k` 变成集合函数 `P(S)=Pr(∪_{i∈S} C_i=+1)`，其失败概率 surrogate 为  
`log L̃(S)=∑logΦ(-q_i) + correlation terms`。  
该式左边奖励高质量 `q_i`，右边的 logdet/二次项惩罚所选 `h_i` 同向堆积，因此质量与多样性由同一准则自然平衡；这也解释了 Linear Probe 与 RepExp 只是两个极端（§3.1–3.3）。

### 技术路线与结构特点
方法分两阶段：学习阶段用训练题中 N 条 rollout 的表示 H 与最终 outcomes c 最大化联合 likelihood 的闭式 surrogate，训练线性 `Qθ(h)=θ^T h+θ0`（§3.1）；推理阶段对每题的 N 个候选用贪心从空集加入使 `Δ_j(S)` 最小者，并用 rank-one updates 维护 `Ω_S, b_S`，复杂度 `O(k(Nd+d^2))` 时间、`O(Nd+d^2)` 内存（§3.2）。理论部分给出 Theorem 1 二阶展开、Theorem 2 surrogate 单调子模且贪心有 `1-e^{-1}` 保证、Proposition 2 弱相关展开连接 probe 与 RepExp（§3.2–3.3）。实验覆盖 8 个 open-weight reasoning models 两个 size tier，`N=128,T=4000,B=32768`，8-fold CV repeated 20 times，judge verification（§4.1）。

### 主要贡献
- 将 `pass@k` 从评价/训练目标引入 partial rollout 剪枝，建模相似 rollout 会一起失败（§1 Contributions、§2）。  
- 推导 closed-form surrogate，并证明其保持单调子模，使贪心选择有 `(1-e^{-1})` 保证；同时证明 exact selection 是 NP-hard（§3.2）。  
- 解析展示 PKTP 在 `ρ→0` 或 equal-quality 极限下如何退化为 Linear Probe / RepExp，使质量-多样性权衡显式化（§3.3、Proposition 2）。  
- 实验显示在八个模型上，尤其 moderate k，PKTP 高于 Linear Probe 与 RepExp，并稳定不低于 Random（Fig.3、Table 1）。

### 历史地位
待观察：发表 4 天，被引 0 次，施引数据不足。

### 局限与不足
- 作者承认：partial rollout embeddings 的 correctness signal 有限，尤其 large k 时 PKTP 与 RepExp 接近，作者自问“能否从 partial rollout 得到 quality embedding，甚至是否可能”；EMA rate `1e-3` 平均最近约 1000 tokens，可能丢失早期 token 信号；因数据有限采用固定 `h_i` 和线性 `Qθ`；改进目前只在 EMA hidden-state 表示类上展示（§4.2、§4.3、§3.1、§2）。  
- 分析推断：surrogate 是 local second-order approximation，不是 exact identity 或 bound，`O(|ε|^3)` 剩余项被丢弃；greedy guarantee 针对 surrogate `P̃` 而非真实 `P`；binary correctness 依赖 Verify 判定，连续 reward 需改 max@k；训练与评测在同一 benchmark pool 内做 cross-validation，跨域泛化未验证；selection 维护 `d^2` 矩阵，`d=4096` 时有额外内存/更新成本（§3.2、§4.1、§5）。

## 背景透镜（chip）
- 瓶颈资源：算力——生成 128 条完整 rollout 到 `B=32768` tokens 的 decode 成本，以及 best-of-N 中评估完整 rollout 的 reward model 成本；论文未单独测量带宽/显存（§1、§4.1）。  
- 把什么换成了什么：把“N 条完整长 rollout 的继续生成计算”换成“N 条裁剪到 `T=4000` 的短前缀 + 一个 `O(k(Nd+d^2))` 的 selection kernel”，只对保留的 k 条继续生成（§2、§3.2）。  
- 代价落在哪个环节：额外落在离线训练 `Qθ` 的 joint-likelihood 阶段和推理时 subset selection kernel；selection 已通过 rank-one 更新从 naive `O(kNd^3)` 降到 `O(k(Nd+d^2))`，内存 `O(Nd+d^2)`（§3.2）。  
- 什么条件下会失效：cutoff T 太短时质量信号弱，如 DeepSeek-R1-Distill-Qwen-7B 的 pass@4 从 `T=2000` 的 57.4% 升至 `T=8000` 的 61.7%（§4.3）；pool 太小时可能无足够多样性候选，DeepSeek-R1-Distill-Qwen-7B 上 PKTP 在 `N≥80` 后才明显领先（§4.4）；表示无法反映最终正确性时也会失效（§4.2）。  
- 能否反向迁移到我的领域：可以迁移：将“从候选配置/设计中选 k 个，最大化至少一个通过 yield/timing closure”视为子模集函数优化；芯片 Design Space Exploration、测试向量/配置选择、冗余配置调度等可用类似“质量打分 + 相关失败联合模型 + 贪心低秩更新”。需要把 rollout correctness 事件替换为硬件/配置成功事件，并假设有可训练历史成功数据；若候选失败近似独立或数据太少，优势会退化。

## 客观信号
paper_id：arXiv:2610.31667  
venue：—｜被引：0｜发表至今：4 天｜有无代码：未提供/正文未附代码链接

## 引用出处
- 坐标缝合、质量型/多样性型剪枝及 `pass@k` 目标：§1、§2、§3.3。  
- 实验设置 `N=128,T=4000,B=32768`、8 models：§4.1。  
- DeepSeek-R1-Distill-Qwen-1.5B k=4 时 PKTP 比 Linear Probe 高 3.0 pp、比 RepExp 高 3.2 pp：§4.2。  
- Greedy 复杂度 `O(k(Nd+d^2))`、内存 `O(Nd+d^2)`、naive `O(kNd^3)`：§3.2、Appendix A.2。  
- cutoff `T=2000→8000` 时 DeepSeek-R1-Distill-Qwen-7B pass@4 `57.4%→61.7%`：§4.3。  
- pool size `N≥80` 后 DeepSeek-R1-Distill-Qwen-7B 中 PKTP 明显领先：§4.4。
