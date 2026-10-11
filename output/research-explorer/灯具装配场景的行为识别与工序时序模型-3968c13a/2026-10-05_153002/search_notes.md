# 检索笔记与访问证据

研究方向：灯具装配场景的行为识别与工序时序模型。检索日：2026-10-05（Asia/Shanghai）。本轮没有 WebSearch/WebFetch 接口，使用 requests 读取 CVF 官方会议目录/摘要页、Crossref 检索 API 及出版商/原始摘要页。Crossref 仅用于发现和交叉核对，入选文献另抓取原始页面。未调用 LLM SDK、未执行外部代码。

截至本日最近12个月指2025-10-05至2026-10-05；最近6个月指2026-04-05至2026-10-05。会议年份不等于论文首次公开日。

## 1. 热点

- 检索词：`procedural activity understanding temporal action segmentation 2025 2026`
- 检索端点：https://api.crossref.org/works?query=procedural+activity+understanding+temporal+action+segmentation+2025+2026&rows=8
- UTC访问时间：2026-10-05T07:32:37.955771+00:00
- 返回候选 8 条；检索响应归档：`source_evidence/dimension_1_crossref.json`。
  - Action-incrementer: Action-incremental learning for temporal action segmentation | 10.1016/j.cviu.2026.104963 | [[2026, 11]]
  - ProtoStep: Few-Shot Procedural Activity Recognition via Temporal Prototype Alignment | 10.2139/ssrn.6240858 | [[2026]]
  - Review for "A video action segmentation method based on multi-scale temporal features" | 10.1088/2631-8695/ae68d8/v2/review1 | [[2026, 4, 9]]
  - Review for "A video action segmentation method based on multi-scale temporal features" | 10.1088/2631-8695/ae68d8/v1/review1 | [[2026, 2, 19]]
  - Review for "A video action segmentation method based on multi-scale temporal features" | 10.1088/2631-8695/ae68d8/v2/review2 | [[2026, 4, 16]]

## 2. 开放问题

- 检索词：`industrial assembly online mistake detection occlusion uncertainty`
- 检索端点：https://api.crossref.org/works?query=industrial+assembly+online+mistake+detection+occlusion+uncertainty&rows=8
- UTC访问时间：2026-10-05T07:32:38.649684+00:00
- 返回候选 8 条；检索响应归档：`source_evidence/dimension_2_crossref.json`。
  - Introduction to Mistake in Contract Law | 10.1093/oso/9780199731404.003.0039 | [[2018, 10, 18]]
  - MISTAKE, MISREPRESENTATION, UNCERTAINTY AND OTHER ISSUES | 10.4324/9781315756615-25 | [[2015, 5, 22]]
  -  | 10.1117/12.2633617.6312678666112 | []
  - Learning by Mistake: Constructing the Conceptual Framework of Mistake Literacy | 10.1080/19404476.2025.2542672 | [[2025, 8, 9]]
  - II. Mistake | 10.1163/2589-4021_ieco_com_071102 | []

## 3. 综述

- 检索词：`Temporal Action Segmentation An Analysis of Modern Techniques`
- 检索端点：https://api.crossref.org/works?query=Temporal+Action+Segmentation+An+Analysis+of+Modern+Techniques&rows=8
- UTC访问时间：2026-10-05T07:32:39.276884+00:00
- 返回候选 8 条；检索响应归档：`source_evidence/dimension_3_crossref.json`。
  - Temporal Action Segmentation: An Analysis of Modern Techniques | 10.1109/tpami.2023.3327284 | [[2024, 2]]
  - Goal-oriented temporal action segmentation and action anticipation | 10.32657/10356/200239 | []
  - Long-term temporal modeling for egocentric video action segmentation | 10.17760/d20741537 | []
  - Ego-Exo Temporal Action Segmentation in Industrial Environments | 10.5220/0014464500004084 | [[2026]]
  - Review for "A video action segmentation method based on multi-scale temporal features" | 10.1088/2631-8695/ae68d8/v2/review1 | [[2026, 4, 9]]

## 4. 基准

- 检索词：`IKEA ASM MECCANO HA4M industrial assembly dataset`
- 检索端点：https://api.crossref.org/works?query=IKEA+ASM+MECCANO+HA4M+industrial+assembly+dataset&rows=8
- UTC访问时间：2026-10-05T07:32:39.837712+00:00
- 返回候选 8 条；检索响应归档：`source_evidence/dimension_4_crossref.json`。
  - Human Motion Prediction on the IKEA-ASM Dataset | 10.5220/0011902300003417 | [[2023]]
  - The IKEA ASM Dataset: Understanding People Assembling Furniture through Actions, Objects and Pose | 10.1109/wacv48630.2021.00089 | [[2021, 1]]
  - The HA4M dataset: Multi-Modal Monitoring of an assembly task for Human Action recognition in Manufacturing | 10.1038/s41597-022-01843-z | [[2022, 12, 2]]
  - MECCANO: A multimodal egocentric dataset for humans behavior understanding in the industrial-like domain | 10.1016/j.cviu.2023.103764 | [[2023, 10]]
  - The MECCANO Dataset: Understanding Human-Object Interactions from Egocentric Videos in an Industrial-like Domain | 10.1109/wacv48630.2021.00161 | [[2021, 1]]

## 5. 应用

- 检索词：`IndustReal procedure step recognition execution errors`
- 检索端点：https://api.crossref.org/works?query=IndustReal+procedure+step+recognition+execution+errors&rows=8
- UTC访问时间：2026-10-05T07:32:40.424984+00:00
- 返回候选 8 条；检索响应归档：`source_evidence/dimension_5_crossref.json`。
  - IndustReal: A Dataset for Procedure Step Recognition Handling Execution Errors in Egocentric Videos in an Industrial-Like Setting | 10.1109/wacv57701.2024.00431 | [[2024, 1, 3]]
  - A two-step string-matching procedure | 10.1016/0031-3203(91)90038-7 | [[1991, 1]]
  - step execution | 10.1007/springerreference_25644 | []
  - single-step execution | 10.1007/springerreference_24961 | []
  - Updating NONMEM IV installation and execution on the VAX 9000: DCL procedure for single-step execution | 10.1016/0169-2607(94)90140-6 | [[1994, 2]]

## 6. 交叉领域

- 检索词：`runtime verification LTL three valued semantics Declare conformance checking`
- 检索端点：https://api.crossref.org/works?query=runtime+verification+LTL+three+valued+semantics+Declare+conformance+checking&rows=8
- UTC访问时间：2026-10-05T07:32:41.145863+00:00
- 返回候选 8 条；检索响应归档：`source_evidence/dimension_6_crossref.json`。
  - Accelerated Runtime Verification of LTL Specifications with Counting Semantics | 10.1007/978-3-319-46982-9_16 | [[2016]]
  - Runtime verification based on 4-valued past time LTL | 10.1109/csip.2012.6308917 | [[2012, 8]]
  - Checking Traces for Regulatory Conformance | 10.1007/978-3-540-89247-2_6 | [[2008]]
  - Comparing LTL Semantics for Runtime Verification | 10.1093/logcom/exn075 | [[2009, 2, 26]]
  - Runtime Checking for Program Verification | 10.1007/978-3-540-77395-5_17 | []

## 7. 最近6–12个月

- 检索词：`procedural mistake detection temporal action segmentation 2026`
- 检索端点：https://api.crossref.org/works?query=procedural+mistake+detection+temporal+action+segmentation+2026&rows=8&filter=from-pub-date%3A2025-10-05%2Cuntil-pub-date%3A2026-10-05
- UTC访问时间：2026-10-05T07:32:41.845347+00:00
- 返回候选 8 条；检索响应归档：`source_evidence/dimension_7_crossref.json`。
  - Mistake Detection in Egocentric Procedural Videos Via CoT-Based Action Anticipation | 10.1109/tmm.2026.3716085 | [[2026]]
  - Review for "A video action segmentation method based on multi-scale temporal features" | 10.1088/2631-8695/ae68d8/v1/review1 | [[2026, 2, 19]]
  - Review for "A video action segmentation method based on multi-scale temporal features" | 10.1088/2631-8695/ae68d8/v2/review1 | [[2026, 4, 9]]
  - Review for "A video action segmentation method based on multi-scale temporal features" | 10.1088/2631-8695/ae68d8/v2/review2 | [[2026, 4, 16]]
  - Ego-Exo Temporal Action Segmentation in Industrial Environments | 10.5220/0014464500004084 | [[2026]]

## 8. 官方目录扩展、筛选与纠错

- 为补充Crossref长查询的低相关结果，本轮读取CVPR 2019/2022/2023/2024/2025/2026、ICCV 2023/2025、WACV 2021/2024官方目录，以`assembly / procedural / mistake / action segmentation / egocentric / uncertainty`逐题筛选，目录URL及访问时间见`source_evidence/index_*.json`。CVPR2019目录未返回有效命中，改用已知论文的原始CVF页面核验MS-TCN。
- 热点维度保留FACT、PHGC、AMNAR与最新错误归因；剔除无关3D几何“assembly”、影像不确定性、法律“mistake”与出版后评论。Crossref热点检索出现2026年11月条目，晚于当前2026-10-05，未作为已发表来源采用。
- 开放问题维度用原始摘要核准IndustReal的“动作识别不保证正确完成”、MistSense的程序/执行双错误、AMNAR的多个合法下一步、Progress-Aware的训练推理不一致、Polyphony的双手表示冲突。它们是作者提出的问题，不等于灯具现场问题已经实证。
- 综述维度保留Ding/Sener/Yao的TAS综述；出版卷期与arXiv首发年分开记录。
- 基准维度保留Assembly101、IKEA ASM、MECCANO、HoloAssist、AssemblyHands、IndustReal、HA4M及Ego-Exo4D；不假定这些数据已下载、已获得许可或具备灯具设备真值。
- 应用维度重点保留IndustReal的步骤完成定义与工位5/6的可观察性差异；不得以气枪出现替代供气、以插接姿态替代锁止。
- 交叉维度追加精确检索`Conformance Checking over Uncertain Event Data`、`Conformance Checking Based on Partially Ordered Event Data`、`Declarative workflows: Balancing between flexibility and support`；从Crossref发现DOI，经OpenAlex定位原始arXiv，再读取原始摘要。最终保留L28–30，不把位置/时间/case不确定或偏序本身当首创。
- 最新维度成功读取ICCV2025和CVPR2026官方目录及原始摘要；最终表中包括CVPR2026 MATT、Polyphony、PL-Stitch，ICCV2025 MistSense、GTG，以及CVPR2025 PHGC、AMNAR。2025年会场论文可能早于最近12个月首次公开，须区分会议年与首发日期。未穷尽2026所有期刊、ECCV论文、预印本和专利。

## 9. 获取限制和未采用来源

- `https://dl.acm.org/doi/10.1145/2000799.2000800`（Runtime Verification for LTL and TLTL）本轮返回403，因此不计入30条；相关Crossref元数据只能作为后续待查线索。
- 初次HA4M请求HTTP200但通用选择器没有提取摘要；随后读取Nature的`section[aria-labelledby=Abs1]`并成功抓取，最终记录含正式摘要和日期。
- 初次文件名标准化会将MS-TCN和MS-TCN++归为相同文件名；已修复为标题片段加URL哈希，并重新抓取最终30条。正式报告只引用带哈希的存档，避免证据覆盖混淆。
- Streaming VideoLLMs原始页成功抓取，但为了保持30条与工业位置/偏序问题的相关性，最终让位于偏序日志一致性研究；不纳入正式文献表。
- 本轮只读取页面和运行自编采集/排版脚本，没有执行论文仓库代码、训练模型、安装大依赖或调用LLM SDK。

## 10. 最终证据清单和可复核结论

- 正式入选30条；每条均有HTTP200原始页面、canonical title、完整作者、页面日期、会场或期刊、摘要、访问时间、HTML和JSON存档。
- `verified_sources.json`是整理后主索引，含L01–L30编号、参考文献年与中文相关性；`source_evidence/*_<URL哈希>.json`是保留原始页面字段的记录。`journal_dates_crossref.json`支持MS-TCN++和TAS综述的正式卷期年核准。
- 七维检索结果在每一维结束时立即追加保存；低相关检索并未被当作检索充分的证明，后续官方目录与精确题名查找已列出。
- 当前证据支持“相关问题已有大量先行工作，适合提出需验证的工业装配研究方案”；不支持声称首次方法、现场准确率、显著优越性或可部署结论。下一阶段要做最近邻全文比较，必要时把成果定位为工业应用/系统方法与场景评测。
