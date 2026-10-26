---
id: "arXiv:2610.30231"
title: "Edit, Don't Imitate: Minimal-Edit On-Policy Distillation for Multimodal Reasoning"
arxiv_id: "2610.30231"
analyzed_at: "2026-10-26"
content_hash: "906842b42b24f64fe7c566bb557e16d7fc813e2dc20e7a191fd0e77ad31c16e8"
schema_version: 1
---
paper_id: arXiv:2610.30231

## TL;DR（三句半）

坐标：演进 —— 在 Agarwal et al. 2024 on-policy distillation 上新增 minimal-edit teacher 与 token mask。  
痛点：以前 small VLM 做视觉数学时卡在 diagram perception；Qwen3-VL-2B 仅图输入 Geometry3K pass@1=0.321，给参考图描述后升至 0.462（§3.1）。  
手术：把 full-token privileged OPD 换成 minimal-edit 纠错评分：错误 rollout 由 frozen teacher 对同一 token 序列打分，只对 \(k_t>0\) 的 teacher-disfavored tokens 施加 distill；正确 rollout 走 GRPO task reward（§3.2）。  
疗效：2B student + 8B teacher 的 Geometry3K pass@1=0.554，超过 privileged OPD 0.487，也超过带参考描述 base 0.462（Table 1）。  
代价：需训练期参考描述/参考解与 teacher scoring；2B/8B 配置每步 FLOPs 比 naive OPD 多 13.3%（Table 7），但测试期无需额外描述。

## 六段

### 总体介绍

这是一项视觉语言模型训练方法研究，针对多模态数学推理中的“感知先行失败”：小模型即使后面推理正确，也会因读错几何图而全盘错误。它用训练期参考图描述与参考解，训练学生模型最终只靠图像和问题作答。

### 核心思想

核心不是让教师生成一个正确答案，而是让教师做“最小编辑式批评者”。对每个错误 rollout，教师看到参考图描述、已验证参考解、学生响应，并被要求以最小编辑原则纠正错误、保留正确部分；教师不生成新解，而是对学生的原 token 序列做 teacher-forced 评分。逐 token 计算 \(k_t=\log\pi_\theta(y_t|\cdot)-\log\pi_T(y_t|\cdot)\)，\(k_t>0\) 的位置被解释为教师比学生更不赞成该 token，进入 corrective mask。只有这些 selected tokens 参与 distillation loss；正确 rollout 只进入 task-reward RL。这样绕开全响应模仿，并把监督集中在真正错误片段上。

### 技术路线与结构特点

路线分两部分：先用 reference figure descriptions 探明 Geometry3K 上的感知瓶颈；再提出 minimal-edit corrective on-policy distillation。结构上由三块组成：规则 verifier 路由 correct/incorrect；GRPO 处理 correct rollouts；minimal-edit teacher scoring + \(k_t>0\) mask 处理 incorrect rollouts。训练使用 \(K=8\) 个 on-policy rollouts，响应上限 2048 tokens，\(\lambda=0.01\)，30 epochs；teacher 全部 frozen。教师只在 incorrect rollout 上做一次 scoring pass。附录 B 给出 FLOPs 成本模型：minimal-edit 与 naive OPD 的开销分界为 \(w^*=D/(P_t+2R)\)。

### 主要贡献

相对既有 OPD/privileged OPD，它多了一个“响应专属批评者”的设置：不生成修正解，只评分原 rollout；再多一个由 minimal-edit 条件诱导的 sparse token mask，而不是全 token 蒸馏或生成错误后缀。实验上，2B student 的 Geometry3K pass@1 从 base 0.321 升到 minimal-edit（8B teacher）0.554，self-teacher 也达 0.498；MathVerse ALL 从 37.8 升到 45.8；Geoperception pass@1 从 0.179 升到 0.455（Table 1）。Qwen2.5-VL-7B 仅用约 1.8K 训练题，在 Table 3 的多模态数学指标上为所列最优。

### 历史地位

待观察：发表 5 天，被引 0 次，施引数据不足。

### 局限与不足

- 作者承认：sign test 不能保证选中所有错误 token；Figure 3 中某些 leg–hypotenuse 误读位置没有获得正 \(k_1\)，因此未被 mask 选中，定位只是部分有效。
- 分析推断：训练域高度集中在 Geometry3K 几何题；方法依赖外部模型生成的 reference description 和 verified reference solution，缺失时性能下降（Table 5 no ref. solution 六项指标均降）；\(\lambda\) 敏感，0.05/0.1 时明显回退；2B self-teacher 相对更大 teacher 仍有可见差距；带宽/显存/通信成本论文未报告，无法做完整硬件代价判断。

## 背景透镜（chip）

- 瓶颈资源：训练期算力/FLOPs，尤其是教师 scoring forward。2B/8B 配置每步为 \(22.17\,KND\)，比 naive OPD 多 13.3%；8B/32B 为 \(18.17\,KND\)，比 naive OPD 少 7.1%（Table 7）。带宽、显存、AllReduce 未报告。
- 把什么换成了什么：把“测试期需要参考图描述/额外模型调用”换成了“训练期 teacher scoring 计算”；训练内部则把 naive OPD 对每个 rollout 全 token 的教师评分，换成只对 incorrect rollouts + 约 2% corrective tokens 的稀疏监督（Fig 5）。
- 代价落在哪个环节：主要落在训练时 incorrect rollout 的 teacher-force scoring 和离线数据准备（描述生成、32 个参考解采样过滤）。2B/8B 高 incorrect fraction \(w=0.456\) 使成本超过 naive OPD；8B/32B 的 \(w=0.275\) 低于阈值 \(w^*=0.333\)，因此反而更便宜。可优化点：随 correct fraction 上升，incorrect 比例下降，teacher 调用减少。
- 什么条件下会失效：已证实的计算条件是当 \(w>w^*=D/(P_t+2R)\)，minimal-edit 不再比 naive OPD 省算力（2B/8B：\(0.456>0.344\)）。方法条件上，若任务没有规则化 final-answer verifier，correct/incorrect 路由会失效；若教师验证能力不足或 sign test 漏检错误 token，校正信号会减弱。序列短/batch 小的影响论文未报告，不宜强套。
- 能否反向迁移到我的领域：可以借鉴“训练期动态稀疏梯度/监督 mask”的思想：只对 teacher-disfavored 的约 2% token 做反向更新，类似稀疏化或 selective backprop。但要落地硬件/训练系统，需先解决 mask 计算本身仍依赖完整 teacher forward，FLOPs 收益在高 incorrect fraction 场景会消失；可考虑把 teacher scoring 换成更轻量的验证器或批量化、流水线化处理。

## 客观信号

venue：—｜被引：0｜发表至今：5 天｜有无代码：未提及/输入无代码信息

## 引用出处

- 2B base 0.321、加参考描述 0.462：§3.1，Figure 2。
- minimal-edit 2B + 8B teacher Geometry3K pass@1 0.554、privileged OPD 0.487、self-teacher 0.498：Table 1。
- MathVerse ALL 37.8→45.8、Geoperception 0.179→0.455：Table 1。
- corrective mask keep rate 约 2%：Figure 5。
- \(\lambda\) 峰值 0.01；移除 reference solution 后六项下降：Table 5。
- FLOPs 比值 +13.3%/−7.1%、阈值 \(w^*\)：Appendix B，Table 7。
- 数据过滤 2.1K→1.8K：§4 Dataset construction。
- 训练 \(K=8\)、2048 tokens、\(\lambda=0.01\)、30 epochs：§4 Training setup。
