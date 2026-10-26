---
id: "arXiv:2610.30940"
title: "Vision Transformers for Estimating Irradiance Using Data Scarce Sky Images"
arxiv_id: "2610.30940"
analyzed_at: "2026-10-25"
content_hash: "87b042ba3358d0520e6436a46c9b37980bc075e1fb5be834a36e2d26f578955e"
schema_version: 1
---
paper_id: arXiv:2610.30940

## TL;DR（三句半）
坐标：缝合（摘要可查：“combining physics-informed modelling with unsupervised segmentation”）｜痛点：以前在数据稀缺天空图像估计 DHI 场景，卡在长期高质量天空图像稀缺，ViT 替代昂贵辐照仪器的实际部署受限｜手术：把“全量数据训练的单一 ViT 直接估计 DHI”换成“物理参数编码 + 合成 GHI 递进 + 原始/叠加增强双分支”｜疗效：单月训练下，sequential/dual-parallel 与全量数据 ViT 的 RMSE 差值分别为 2-3 W/m² 与 1-6 W/m²（摘要）｜代价：引入合成 GHI 前序预测、无监督云分割与双路图像处理，落地要优化多阶段推理与分割开销

## 六段
### 总体介绍
本文提出一种面向数据稀缺场景的 DHI 估计方法：用 Vision Transformer 结合物理参数与无监督云分割，减少对长期高质量天空图像与地面辐照仪器的依赖。

### 核心思想
把 DHI 估计从“单帧图像到目标”的纯黑盒，改造成“物理先验→粗辐照→细辐照”的多级表征：先用可计算大气参数和周期时间编码提供无仪器先验；再以合成 GHI 作为中间特征桥接天空图像与 DHI；同时用物理引导的无监督云分割生成 overlay，迫使双分支网络关注动态云区。其本质是用廉价物理/几何先验补偿图像样本不足。

### 技术路线与结构特点
结构上分两条可独立评估的框架：Sequential 先预测合成 GHI，再作为特征精化 DHI；Dual-parallel 同时处理原始鱼眼图与 overlay 增强图。输入侧统一整合地外辐照与周期时间编码；overlay 由无监督物理信息云分割生成。模拟数据稀缺时，使用 Chilbolton Observatory 单月数据训练，并在时间不重叠的全年测试集上评估。

### 主要贡献
相对既有全量数据训练的 ViT，本文主要在数据效率上给出可核查结果：单月训练即可将 RMSE 差距压到 2-3 W/m²（sequential）与 1-6 W/m²（dual-parallel）；同时引入无仪器物理参数表示与物理引导的无监督分割增强动态天空特征。

### 历史地位
待观察：发表 3 天，被引 4 次，施引数据不足（施引论文标题：缺施引数据）。

### 局限与不足
- 作者承认：摘要未明示；【需正文】。
- 分析推断：数据仅来自 Chilbolton 单站温带气候，热带/干旱/极地等云况外推性未知；单月训练可能只覆盖特定季节云型，季节泛化可能有偏；overlay 依赖无监督云分割，分割失败会向 DHI 传导误差；摘要未给计算量、延迟、显存与推理吞吐，工程可部署性不可评估。

## 背景透镜（chip）
- 瓶颈资源：【需正文】摘要未提供算力/带宽/显存数据。
- 把什么换成了什么：【需正文】摘要只有模型架构组合，未给访存/计算口径。
- 代价落在哪个环节：【需正文】摘要未给额外 kernel/通信/硬件开销。
- 什么条件下会失效：【需正文】摘要未给序列长度/batch 大小等硬件失效边界。
- 能否反向迁移到我的领域：【需正文】缺少正文与平台映射，不作猜测。

## 客观信号
venue：—｜被引：4｜发表至今：3 天｜有无代码：未提供（元数据未标注）

## 引用出处
- “within 2-3 W/m² and 1-6 W/m² … of a state-of-the-art ViT trained on the complete dataset”：摘要。
- “trained on a single month (e.g., January) … evaluated across a temporally disjoint, full-year test set”：摘要。
- “Chilbolton Observatory”：摘要。
- 被引 4 次、发表至今 3 天：输入元数据。
