---
id: "arXiv:2611.11533"
title: "Ostinato: Fast CPU-GPU MoE Inference for Local Agents via Resident Experts in Every Forward Pass"
arxiv_id: "2611.11533"
analyzed_at: "2026-11-12"
content_hash: "602b1e2c4f0e52b201ea6ce86adb3ce05f6a642258cb41329a7263679e11dd26"
schema_version: 1
selection_channel: "A1_watchlist"
selection_groups: ["llm-inference", "ai-infra", "model-architecture", "agent"]
selection_score: 4.0
selection_tier: null
selection_model: "deepseek-v4-flash"
selection_fulltext: true
---
paper_id：arXiv:2611.11533

## TL;DR（三句半）
坐标：演进（把GPU常驻专家从主要服务decode/小forward扩展到每次forward pass，并组合split/upload路径与每GPU一个CUDA graph；摘要、§3.1-3.4）｜痛点：本地agent工具输出后的短prefill（SWE中位249新token、τ2中位118）卡在expert cache只服务≤32 token、短prefill全走CPU，SWE工具后首token中位2.36–2.78s（§2.2、§4.3）｜手术：短prefill的hot routes（dev选出的44%专家承接held-out 84.0% routes）走GPU常驻专家，冷路由留CPU；长prefill每冷专家上传一次；decode每GPU一个固定shape CUDA graph（摘要、§2.3、§3.3-3.4）｜疗效：对llama.cpp expert cache与KTransformers，工具输出后中位等待降22–55%，session完成快1.4–2.5×（摘要；§4.3：SWE 1.26s vs 2.78s、τ2 0.98s vs 2.11s）｜代价：引入离线驻留规划/容量测试/成本模型、零slot与host mapped buffer、decode借用内存上传；落地要按开发会话与机器校准驻留集和Np=512，并依赖路由偏斜稳定（§3.1-3.4）。

## 六段
### 总体介绍
Ostinato是面向本地workstation上agent会话的CPU–GPU MoE推理引擎，目标是在不改模型计算的前提下，同时缩短工具调用后的短prefill等待并保持decode速度（摘要、§1）。

### 核心思想
关键不是“多塞专家进显存”，而是换计量单位：短prefill按distinct expert看很多、上传不划算；按token–expert route看却高度集中且跨session稳定，dev选出的44%专家覆盖held-out 84.0% routes，因此一套离线频率驻留集可在每次forward pass服务大多数route，剩余cold route再按forward pass大小分派CPU或一次性上传（摘要、§2.3、§3.2）。

### 技术路线与结构特点
两阶段：离线按开发会话路由频率排序专家，用容量测试固定每GPU专家槽，跨层共享一个pool，成本模型约束驻留集与最优静态集的差距（§3.1-3.2）；在线用index table把路由拆成GPU hot分支与CPU cold分支。短prefill（<512 token）走split path：GPU算hot、CPU顺序算cold；长prefill走upload path：每个distinct cold expert只上传一次；decode把cold专家交给常驻CPU线程池与GPU hot分支重叠，零slot保持固定shape，使一个GPU上所有层重放为一个大CUDA graph，并用wait kernel汇合；decode期间还把prefill预留的空闲显存借给少量cold专家上传（§3.3-3.4）。实现于llama.cpp，约4.3K行C++/CUDA（§3.6）。

### 主要贡献
相对既有CPU–GPU引擎，它把resident experts用于每一次forward pass，而不只服务decode或≤32 token的小forward；给出size-dependent prefill路径、零slot固定shape的decode graph与borrowed memory；在DeepSeek-V4-Flash四RTX 4090D上对六个baseline，所有held-out session/task均更快，工具后中位等待降22–55%，session完成快1.4–2.5×；Qwen3-235B-A22B上仅用通用驻留与prefill路径即降总请求时间16.2%（§1、§4.2、§4.6）。

### 历史地位
待观察：发表 4 天，被引 0 次，施引数据不足。

### 局限与不足
- 作者承认：实验每次只服务一个session（§4.1）；驻留集运行期固定，borrowed expert只服务一个请求的decode且prefill前归还（§3.2、§3.4）；Np=512与容量测试依赖开发会话和本机校准（§3.1-3.3）；首turn/long prefill优势收窄，≥1024新token时与llama.cpp layer placement约持平（3.87 vs 3.78s，§4.4）；Qwen3上未启decode graph与borrowed memory，decode仅与buun差3.1%以内（§4.6）。
- 分析推断：若agent工作负载路由漂移、cold route比例升高或CPU/PCIe/NUMA变弱，静态hot set与split path收益会缩小；单session replay未验证并发多agent/batching；端到端任务成功率未评，只强制回放记录token；公开代码链接未见（§4.1、Appendix A）。

## 背景透镜（chip）
- 瓶颈资源：本地四消费级GPU的显存容量、host–PCIe带宽与CPU算力；DeepSeek-V4-Flash routed experts 137GiB，仅约一半能进四张24GB GPU（§1、§2.1）。
- 把什么换成了什么：用GPU常驻专家显存换短prefill的CPU计算/PCIe上传；decode用零slot+固定shape换CUDA graph可重放，用CPU线程池与GPU重叠换cold专家暴露时间；borrowed memory用decode空闲显存换少量后台上传（§3.3-3.4）。
- 代价落在哪个环节：离线规划/容量测试与成本模型；GPU显存占用（5702 resident + 544 borrowed slots，Table 6/§3.6）；host mapped buffer与wait kernel同步；borrowed上传约0.01ms/step等待，但省1.4ms（SWE）/0.4ms（τ2）每step（§3.4、§4.5）；短prefill的CPU cold分支仍在critical path且与GPU分支顺序执行（§3.3）。可优化点：按workload重校准驻留集/Np、降低cold fraction、调PCIe/NUMA分块。
- 什么条件下会失效：路由偏斜/稳定性不足或hot set覆盖下降；显存装不下有效hot set；CPU弱或PCIe拥塞；长prefill≥1024 token时优势收窄；并发多session/大batch未验证；Qwen3未启完整decode设计（§4.4、§4.6、§4.1）。
- 能否反向迁移到我的领域：可迁移（分析推断）：按访问频率做静态驻留+按请求大小切换split/upload+固定shape图重放+CPU/GPU重叠，适合稀疏专家/稀疏特征缓存、agent serving与流水线式异构执行。

## 客观信号
venue：—（arXiv cs.DC）｜被引：0｜发表至今：4 天｜有无代码：正文未给公开仓库链接；§3.6称在llama.cpp上原型，约4.3K行C++/CUDA。

## 引用出处
- 44%/84.0% routes、短prefill集中性：摘要、§2.2、§2.3。
- SWE/τ2短prefill中位249/118新token：§2.2、Table 2。
- expert cache仅服务≤32 token forward pass：摘要、§2.1、§2.2。
- 工具后首token中位2.36–2.78s：§2.2、Table 3。
- SWE 1.26s vs 2.78s、τ2 0.98s vs 2.11s：§4.3、Table 3。
- 等待降22–55%、session快1.4–2.5×：摘要、§4.2、§4.3。
- 137GiB routed experts、约一半fit四GPU：§1。
- 5702 resident + 544 borrowed slots：Table 6、§3.6。
- borrowed上传等待约0.01ms/step、省1.4/0.4ms每step：§3.4、§4.5。
- Np=512：§3.3。
- 成本模型驻留集差距0.142ms/decode step：§3.2、Appendix B。
- 约4.3K行C++/CUDA：§3.6。
- Qwen3总请求时间降16.2%、TTFT降39.4%/57.8%：§4.6、Table 4。
- ≥1024新token时3.87 vs 3.78s：§4.4。
- 首turn 4.70 vs 4.57s等：§4.3、Table 7。
- 单session实验：§4.1。
- decode与同步参考bitwise一致：Appendix A。
