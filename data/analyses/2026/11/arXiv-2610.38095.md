---
id: "arXiv:2610.38095"
title: "Nemotron 3.5 Super and Lightning: Open, Efficient Mixture-of-Experts Hybrid Mamba-Transformer Models for Agentic AI"
arxiv_id: "2610.38095"
analyzed_at: "2026-11-02"
content_hash: "fa6c0325d87c7d307a3d58feaaf65d2cf08cb254593c36271b92ad67cca8ef23"
schema_version: 1
---
```
paper_id：arXiv:2610.38095

## TL;DR（三句半）
坐标：演进——论文明确 Super 3.5 Base 从 Nemotron 3 Super Base 初始化，Lightning 3.5 Base 从 Nano 3 Base 初始化（§2）。
痛点：长周期 agentic 场景中，LLM 能力与推理效率难以同时满足（§1）。
手术：把原有两个非共享 MTP 头合并为单一递归共享草稿头（§2.1），推理侧用 NVFP4 量化 + MTP 原生推测解码替换 BF16 逐 token 解码（摘要/§4/§5）；训练侧用 1T token 蒸馏、Indic/agentic 继续训练、MOPD 替代单一 SFT/RL。
疗效：单 GB200 GPU 最高 4,856 output tokens/s/GPU，且至少 50 tokens/s/user（摘要/§5.1）。
代价：引入约 1T token 蒸馏、百 B 级继续预训练/后训练和复杂量化-草稿推理栈；落地需优化 Mamba 状态缓存、NVFP4 敏感层和草稿头调度（§2.2/§2.3/§5.3/附录 B）。

## 六段
### 总体介绍
Nemotron 3.5 Super（120B 总参/12B 活跃，多模态）与 Lightning（30B 总参/3B 活跃，文本）是混合 Mamba-Transformer 的 MoE 模型，面向推理、长上下文和 agentic 负载，重点联合优化能力与部署效率。

### 核心思想
它不是单一模块创新，而是把“架构—训练—后处理—量化—推理”串成联合优化路径：用 Mamba-Transformer 混合 + MoE 控制活跃参数；用递归共享 MTP 头把逐 token 生成改为草稿+多 token 验证；用大规模蒸馏、领域继续预训练、mid-training、多教师 on-policy 蒸馏把文本基座塑造成多模态 agent；再用 NVFP4 量化、混合精度和专用 kernel 把这些能力变成吞吐。核心在于让训练、草稿、量化三者彼此匹配，而不是孤立提高模型分数。

### 技术路线与结构特点
按四块组织：  
（1）预训练：MTP 初始化 + 100B token warmup，1T token 蒸馏（Super 用 Skew Reverse KL，Lightning 用 KL+CE），Indic CPT（100B tokens，含 10B Indic），1M 长上下文，Super 再做 65.4B tokens agentic mid-training 和 9B token 恢复。  
（2）后训练：两阶段 SFT 扩展到 VLM，RLVR/RLHF，专项教师（agentic/reasoning/video/vision tool/vision reasoning/alignment），两阶段 MOPD，MTP boosting。  
（3）量化：NVFP4 PTQ、敏感性驱动混合精度、量化感知蒸馏、KV cache/MTP/并行草稿量化。  
（4）推理：低并发内存受限优化、prefix reuse、Mamba 检查点、并行草稿器（LiLiCorr/DFlash/DSpark）。  
结构特点包括稀疏 MoE、混合状态、递归 MTP、并行草稿器和逐块缩放低精度格式。

### 主要贡献
相对 Nemotron 3 系列，该工作报告了：两个开放高效模型及检查点/数据/配方；MTP 草稿头合并与隐藏状态混合，将 T=1 平均接受长度从 3.787 提升到 4.612（§3.7）；Indic CPT 最高 +9.82 pp 多语言提升（§2.3）；agentic mid-training 在多个 harness 上提升鲁棒性，如 Codex SWE-bench Verified 从 48.4 提升到 61.8（§2.5）；MOPD 整合多位教师；NVFP4 量化达到 99.62% median accuracy recovery（摘要/§4）；GB200 上最高 4,856 tokens/s/GPU（§5.1）。

### 历史地位
待观察：发表 5 天，被引 1 次，施引数据不足。

### 局限与不足
- 作者承认：量化分析中 weight-versus-activation 归因是推断而非实验，未做 W16A4；MRCR-128k 是唯一消歧基准，结论依赖单一 benchmark；注意力分数稀释是在已有 FP8 KV cache 基线上测量的（附录 B.6）。去污管线召回受候选选择限制，只检测 problem statement（附录 C.6）。
- 分析推断：NVFP4 量化在 MRCR-128k 长上下文消歧上造成 2.65–3.6 pp 损失（附录 B.3），说明 4-bit 部署在需要精确位置索引/消歧的检索任务上可能不稳健；MTP 草稿在短序列/小 batch 下可能因草稿头额外开销而不划算（正文未直接给出该对比）；混合 Mamba 状态缓存检查点粗粒度可能限制高复用长 prompt 场景（§5.3）。

## 背景透镜（chip）
- 瓶颈资源：显存/带宽是主要瓶颈——长上下文 KV cache、Mamba 状态缓存与 MoE 专家权重在低并发推理中占主导；论文明确针对 low-concurrency, memory-bound 场景优化（摘要/§5.5）。算力为次要瓶颈，多专家 GEMM 仍占计算。
- 把什么换成了什么：用 NVFP4 量化把 BF16 权重访存换成低精度 GEMM/反量化计算；用 MTP 草稿把逐 token 的 memory-bound 主路径换成草稿头计算 + 批量 token 验证；用稀疏 MoE 把全参数计算换成专家路由下的少活跃参数计算（§2/§4/§3.7）。
- 代价落在哪个环节：额外草稿头前向/验证 kernel，混合状态缓存与粗粒度检查点，以及 NVFP4 敏感层（注意力投影、Mamba 输出投影、unembedding）的校准与混合精度补偿；可优化空间包括逐块缩放、per-block 量化、量化感知蒸馏和 MTP 隐藏状态混合。
- 什么条件下会失效：正文显示量化在 MRCR-128k 长上下文消歧上损失 2.65–3.6 pp，32k+ 上下文、近 identical 候选时更明显（附录 B.3）；MTP 草稿可能在短序列/小 batch 下因额外草稿头开销而不划算（分析推断）。
- 能否反向迁移到我的领域：可迁移。可借鉴“敏感层识别 + per-block 精度 + 训练侧配合（QAD/MTP boosting）”的联合优化，用于其他大模型推理栈；尤其适用于稀疏化、流水线和低精度部署场景，但需保留对长上下文消歧类任务的精度验证。

## 客观信号
paper_id：arXiv:2610.38095｜venue：—｜被引：1｜发表至今：5 天｜有无代码：有模型检查点/数据/配方发布，代码库未在摘要中明确。

## 引用出处
- 坐标初始化：§2（正文“initialized from ... Super 3 Base ... Nano 3 Base”）
- 痛点：§1 引言
- 疗效：4,856 tokens/s/GPU、50 tokens/s/user：摘要/§5.1
- 代价：1T token 蒸馏：§2.2；Indic 2.83T 生成 token：§2.3；mid-training 65.4B tokens：§2.5
- MTP 合并两个非共享头：§2.1
- MTP boosting 接受长度 3.787→4.612（T=1）：§3.7/表8
- Indic CPT 最大 +9.82 pp：§2.3/表2
- agentic mid-training Codex SWE-bench Verified 48.4→61.8：§2.5/表5
- NVFP4 99.62% median accuracy recovery：摘要/§4
- MRCR-128k 量化损失 2.65–3.6 pp：附录 B/B.3
- 低并发 memory-bound：摘要/§5.5
- Mamba 输出投影/unembedding 敏感：附录 B.4/B.5
```
