---
id: "arXiv:2611.26358"
title: "Beam Model Technical Report"
arxiv_id: "2611.26358"
analyzed_at: "2026-11-23"
content_hash: "bb5ae2bdd38bd440c68b644ca4d39a0e06305833e85b13e4ee5855eef47cf37f"
schema_version: 1
selection_channel: "A1_watchlist"
selection_groups: ["llm-inference", "model-architecture", "training-alignment", "agent"]
selection_score: 4.0
selection_tier: null
selection_model: "deepseek-flash"
selection_fulltext: true
---
## TL;DR（三句半）
坐标：演进——Reflection 首个开放权重模型，501.3B 总参/23.4B 激活的稀疏 MoE，23.8T token 预训练，两阶段 midtraining 扩至 1M 上下文，10.5K GB300 GPU 异步 RL（§1、Table 1、Abstract）｜痛点：以前在编码/推理/智能体场景，卡在前沿推理能力与推理计算成本、长上下文能力难以兼得｜手术：把单阶段 midtraining + 同步 RL 换成了两阶段正确性锚定 midtraining + 全异步 RL（110K 并发 rollouts、12 秒权重更新、99.99% 动态批打包）+ MOPD 多教师合并（§3、§4.3、§4.4）｜疗效：10.5K GB300 GPU 上 4 周生成 100M+ rollouts，能力随 RL 计算增加持续提升且无平台期（§1、§4）｜代价：引入大规模异步 RL 与推理加速的工程复杂度（故障恢复、沙箱调度、策略陈旧管理、量化精度调优），落地需优化集群调度与精度-吞吐权衡（§4.4.2、§6）

## 六段
### 总体介绍
Beam 是 Reflection 团队的首个开放权重稀疏 MoE 语言模型，501.3B 总参数、23.4B 激活参数，面向编码、推理与智能体工作负载，在 23.8T token 上预训练并扩展至 1M 上下文（Abstract、§1）。

### 核心思想
不是单纯堆参数，而是用“可验证、正确性锚定的数据”贯穿预训练→midtraining→RL 后训练全流程，并以大规模异步 RL 与硬件-模型协同设计，在控制推理计算的同时逼近前沿推理能力（Abstract、§1、§3、§4）。

### 技术路线与结构特点
架构为 52 层解码器 MoE，256 专家/层、8 激活，GQA（48 Q / 8 KV），5:1 局部-全局注意力交错（局部窗口 512），SandwichNorm、QK Norm、门控注意力、全局 NoPE、DSRes 深度缩放残差、无共享专家的 DeepSeek 风格 MoE + 序列辅助损失负载均衡（Table 1、§2.1.1）。预训练用 AdamC、DLS 数据受限采样、域分区+质量感知上采样、大规模 OCR 管道（§2.1.2、§2.1.3、§2.2）。Midtraining 分两阶段扩至 1M 上下文（§3）。后训练为 Agentic Expert（SFT+高算力异步 RL）、Alignment Teacher、MOPD 合并，配 Serving Mesh、沙箱、judge 服务（§4）。推理侧 FP8/NVFP4 量化 + 1.93B DFlash 2 投机解码（§4.5、§4.6）。

### 主要贡献
相对既有工作，它提供了：两阶段 midtraining 实现 1M 上下文而不降通用能力（§3）；大规模异步 RL 系统工程（110K 并发 rollouts、12 秒权重更新、99.99% 动态批打包，§4.4）；DSRes 与自适应专家偏置更新等架构/优化改进（§2.1.1）；完整的开放权重技术报告，含数据、基础设施与安全评估细节（§1–§7）；安全-能力 Pareto 前沿上 88.9% 拒绝/转向率与 81.5% 合规率（Abstract、§7）。

### 历史地位
待观察：发表 4 天，被引 0 次，施引数据不足。

### 局限与不足
- 作者承认：去重管道在规模上接受一定假阳性（§2.2.4）；异步 RL 存在策略陈旧，需在吞吐与策略新鲜度间平衡（§4.4.2）；安全评估各小节附有 “Interpretation and Limitations”（§7.3）；小于 0.5B 激活参数的配置信号不足（§2.1.3）。
- 分析推断：技术报告极长但大量组件属工程集成与调优，新算法贡献相对分散；10.5K GB300 GPU 的 RL 规模难以复现，开放权重不等于开放完整训练栈；1M 上下文的两阶段 midtraining 数据配比与课程细节公开程度有限；安全评估以内部 ISE 基准为主，第三方完整结果需查原文；投机解码 drafter 在分布外任务上的泛化性未充分讨论；MoE 路由长期稳定性仍依赖启发式调度。

## 背景透镜（chip）
- 瓶颈资源：训练/推理算力与显存带宽——预训练 6,144 GB300 GPU、21% MFU、92.3% goodput（§1），RL 10.5K GB300 GPU（§1、§4.4），推理侧 KV cache 与量化（§4.5、§4.6）。
- 把什么换成了什么：用稀疏 MoE 把稠密计算换成稀疏激活（23.4B/501.3B）；用 5:1 局部-全局注意力 + GQA（8 KV heads）把全注意力 KV cache 换成局部窗口+少量全局头；用 FP8/NVFP4 量化把高精度换成低精度；用 DFlash 2 投机解码把小模型草稿+大模型验证替换串行解码；用异步 RL 把同步等待换成流水线重叠（§2.1.1、§4.4、§4.5、§4.6）。
- 代价落在哪个环节：MoE All-to-All 通信与路由开销；异步 RL 权重同步（12 秒）与策略陈旧；量化校准与精度损失；drafter 显存与验证开销；两阶段 midtraining 额外训练阶段（§2.1.1、§4.4.2、§4.5、§4.6、§3）。
- 什么条件下会失效：序列短/batch 小时投机解码收益下降；需要精确全局回忆的长上下文任务中 5:1 全局注意力可能不足；异步 RL 策略陈旧在训练后期影响收敛；NVFP4 在敏感任务上可能掉点（§4.4.2、§4.5、§4.6、§2.1.1）。
- 能否反向迁移到我的领域：可以——稀疏激活、混合精度、投机解码、异步流水线的组合对大规模推理/训练系统有参考价值；但需针对目标硬件（非 GB300）重新调优拓扑与调度。

## 客观信号
venue：—（arXiv preprint）｜被引：0｜发表至今：4 天｜有无代码：未见代码链接；论文称 open-weight model，权重开放情况待确认

## 引用出处
- 501.3B 总参 / 23.4B 激活 / 52 层 / 256 专家 / 8 激活：Abstract、§1、Table 1
- 23.8T token 预训练：Abstract、§1、Table 3
- 3-4x less inference compute：Abstract、§1
- 1M-token context / 两阶段 midtraining：Abstract、§1、§3
- 5:1 局部-全局注意力 / 窗口 512 / 8 KV heads：Table 1、§2.1.1
- 6,144 GB300 GPU / 92.3% goodput / 21% MFU：§1
- 10.5K GB300 GPU / 100M+ rollouts / 110K 并发 / 12 秒权重更新 / 99.99% 动态批打包：§1、§4.4
- 88.9% 拒绝/转向 / 81.5% 合规：Abstract、§1、§7
- 1.93B DFlash 2：Abstract、§1、§4.6
- DLS 数据受限采样：§2.1.3
- DSRes / 自适应专家偏置更新：§2.1.1
- AdamC：§2.1.2、Appendix A/B
- MOPD：§1、§4.3
- FP8/NVFP4：Abstract、§4.5
- 去重假阳性 / 策略陈旧 / 安全评估局限 / <0.5B 信号不足：§2.2.4、§4.4.2、§7.3、§2.1.3
