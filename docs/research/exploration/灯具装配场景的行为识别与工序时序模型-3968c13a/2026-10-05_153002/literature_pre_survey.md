# 灯具装配行为识别与工序时序：文献预调研

检索与核验日期：2026-10-05（Asia/Shanghai）。本表收录30项代表性研究，所有条目均在本轮成功获取原始摘要页、正式会议页或出版商页，未将记忆条目、旧项目参考文献或仅有搜索结果的记录算作已核验来源。范围是选题级预调研，主要依据摘要和出版元数据；不是逐篇全文评述或系统综述。

表中作者超过3人时缩写，完整原始作者列表、访问时间和摘要证据保存在[verified_sources.json](verified_sources.json)。本页后半部分提供逐条访问记录与不超过25个英文词的摘要摘录，原始HTML与元数据文件保留在`source_evidence/`。论文摘要中的性能数字均为作者对各自数据集的报告，本研究未复现，也不据此推算灯具准确率。

## 1. 代表性文献表

| 编号 | 标题 | 作者 | 年份 | 会议／期刊 | 本轮读取的原始URL | 与灯具研究的关系 |
|---|---|---|---|---|---|---|
| L01 | Assembly101: A Large-Scale Multi-View Video Dataset for Understanding Procedural Activities | Sener, Fadime; Chatterjee, Dibyadip; Shelepov, Daniel 等（共7人） | 2022 | CVPR2022 | [原始页面](https://openaccess.thecvf.com/content/CVPR2022/html/Sener_Assembly101_A_Large-Scale_Multi-View_Video_Dataset_for_Understanding_Procedural_Activities_CVPR_2022_paper.html) | 多视角装配/拆卸、顺序变体与错误；适合通用事件分段验证，玩具任务不能代替灯具SOP。 |
| L02 | The IKEA ASM Dataset: Understanding People Assembling Furniture Through Actions, Objects and Pose | Ben-Shabat, Yizhak; Yu, Xin; Saleh, Fatemeh 等（共7人） | 2021 | WACV2021 | [原始页面](https://openaccess.thecvf.com/content/WACV2021/html/Ben-Shabat_The_IKEA_ASM_Dataset_Understanding_People_Assembling_Furniture_Through_Actions_WACV_2021_paper.html) | 多视角家具装配、动作/对象/姿态标注；可检查交互特征，但缺灯具孔位与工艺设备证据。 |
| L03 | The MECCANO Dataset: Understanding Human-Object Interactions From Egocentric Videos in an Industrial-Like Domain | Ragusa, Francesco; Furnari, Antonino; Livatino, Salvatore 等（共4人） | 2021 | WACV2021 | [原始页面](https://openaccess.thecvf.com/content/WACV2021/html/Ragusa_The_MECCANO_Dataset_Understanding_Human-Object_Interactions_From_Egocentric_Videos_in_WACV_2021_paper.html) | 小零件与工具的时空交互标注；为手—物—工具特征及困难孔位关联提供参照。 |
| L04 | HoloAssist: an Egocentric Human Interaction Dataset for Interactive AI Assistants in the Real World | Wang, Xin; Kwon, Taein; Rad, Mahdi 等（共12人） | 2023 | ICCV2023 | [原始页面](https://openaccess.thecvf.com/content/ICCV2023/html/Wang_HoloAssist_an_Egocentric_Human_Interaction_Dataset_for_Interactive_AI_Assistants_ICCV_2023_paper.html) | 操作与纠错交互数据及错误检测基准；可借鉴纠错标签，不能直接当智能工具完成证据。 |
| L05 | AssemblyHands: Towards Egocentric Activity Understanding via 3D Hand Pose Estimation | Ohkawa, Takehiko; He, Kun; Sener, Fadime 等（共6人） | 2023 | CVPR2023 | [原始页面](https://openaccess.thecvf.com/content/CVPR2023/html/Ohkawa_AssemblyHands_Towards_Egocentric_Activity_Understanding_via_3D_Hand_Pose_Estimation_CVPR_2023_paper.html) | 装配中多视角3D手姿态与动作理解；对精细手部特征有用，估计手姿态仍不等于完成插接。 |
| L06 | IndustReal: A Dataset for Procedure Step Recognition Handling Execution Errors in Egocentric Videos in an Industrial-Like Setting | Schoonbeek, Tim J.; Houben, Tim; Onvlee, Hans 等（共5人） | 2024 | WACV2024 | [原始页面](https://openaccess.thecvf.com/content/WACV2024/html/Schoonbeek_IndustReal_A_Dataset_for_Procedure_Step_Recognition_Handling_Execution_Errors_WACV_2024_paper.html) | 区分动作发生与步骤成功，包含程序和执行错误；最接近工业步骤完成与错误样本设计。 |
| L07 | FACT: Frame-Action Cross-Attention Temporal Modeling for Efficient Action Segmentation | Lu, Zijia; Elhamifar, Ehsan | 2024 | CVPR2024 | [原始页面](https://openaccess.thecvf.com/content/CVPR2024/html/Lu_FACT_Frame-Action_Cross-Attention_Temporal_Modeling_for_Efficient_Action_Segmentation_CVPR_2024_paper.html) | 帧—动作双支交叉注意力分段基线；用于离线比较，在线使用必须独立检查整个输入链的因果性。 |
| L08 | Progress-Aware Online Action Segmentation for Egocentric Procedural Task Videos | Shen, Yuhan; Elhamifar, Ehsan | 2024 | CVPR2024 | [原始页面](https://openaccess.thecvf.com/content/CVPR2024/html/Shen_Progress-Aware_Online_Action_Segmentation_for_Egocentric_Procedural_Task_Videos_CVPR_2024_paper.html) | 直接在线分段并建模进度和任务图；主课题的重要最近邻，需比较延迟与错误保留能力。 |
| L09 | Weakly-Supervised Online Action Segmentation in Multi-View Instructional Videos | Ghoddoosian, Reza; Dwivedi, Isht; Agarwal, Nakul 等（共5人） | 2022 | CVPR2022 | [原始页面](https://openaccess.thecvf.com/content/CVPR2022/html/Ghoddoosian_Weakly-Supervised_Online_Action_Segmentation_in_Multi-View_Instructional_Videos_CVPR_2022_paper.html) | 多视角训练的弱监督在线分段；可借鉴时间戳/多视角标注效率及训练推理一致性。 |
| L10 | SVIP: Sequence VerIfication for Procedures in Videos | Qian, Yicheng; Luo, Weixin; Lian, Dongze 等（共6人） | 2022 | CVPR2022 | [原始页面](https://openaccess.thecvf.com/content/CVPR2022/html/Qian_SVIP_Sequence_VerIfication_for_Procedures_in_Videos_CVPR_2022_paper.html) | 用步骤变换定义程序序列验证；提供错序评价思路，但视频对相似性不等于完整工艺证据。 |
| L11 | PREGO: Online Mistake Detection in PRocedural EGOcentric Videos | Flaborea, Alessandro; di Melendugno, Guido Maria D'Amely; Plini, Leonardo 等（共8人） | 2024 | CVPR2024 | [原始页面](https://openaccess.thecvf.com/content/CVPR2024/html/Flaborea_PREGO_Online_Mistake_Detection_in_PRocedural_EGOcentric_Videos_CVPR_2024_paper.html) | 在线识别当前动作并与符号预测比对以检测程序错误；需作为在线错误检测参照。 |
| L12 | Error Detection in Egocentric Procedural Task Videos | Lee, Shih-Po; Lu, Zijia; Zhang, Zekun 等（共5人） | 2024 | CVPR2024 | [原始页面](https://openaccess.thecvf.com/content/CVPR2024/html/Lee_Error_Detection_in_Egocentric_Procedural_Task_Videos_CVPR_2024_paper.html) | 手物关系图与正常步骤原型用于错误检测；不能把引入手物图本身视为新的贡献。 |
| L13 | PHGC: Procedural Heterogeneous Graph Completion for Natural Language Task Verification in Egocentric Videos | Jiang, Xun; Huang, Zhiyi; Xu, Xing 等（共6人） | 2025 | CVPR2025 | [原始页面](https://openaccess.thecvf.com/content/CVPR2025/html/Jiang_PHGC_Procedural_Heterogeneous_Graph_Completion_for_Natural_Language_Task_Verification_CVPR_2025_paper.html) | 自然语言规则与视频操作流的异构图对齐；SOP语义建模的相关工作，自动规则仍需现场核准。 |
| L14 | Modeling Multiple Normal Action Representations for Error Detection in Procedural Tasks | Huang, Wei-Jin; Li, Yuan-Ming; Xia, Zhi-Wei 等（共7人） | 2025 | CVPR2025 | [原始页面](https://openaccess.thecvf.com/content/CVPR2025/html/Huang_Modeling_Multiple_Normal_Action_Representations_for_Error_Detection_in_Procedural_CVPR_2025_paper.html) | 显式建模多个合法下一动作；是允许偏序/多路径时必须考虑的近邻，不能只与唯一下一步比较。 |
| L15 | MistSense: Versatile Online Detection of Procedural and Execution Mistakes | Patsch, Constantin; Wu, Yuankai; Zakour, Marsil 等（共5人） | 2025 | ICCV2025 | [原始页面](https://openaccess.thecvf.com/content/ICCV2025/html/Patsch_MistSense_Versatile_Online_Detection_of_Procedural_and_Execution_Mistakes_ICCV_2025_paper.html) | RGB与手姿态联合在线检测程序和执行错误；对本课题创新主张有直接约束。 |
| L16 | Error Recognition in Procedural Videos using Generalized Task Graph | Lee, Shih-Po; Elhamifar, Ehsan | 2025 | ICCV2025 | [原始页面](https://openaccess.thecvf.com/content/ICCV2025/html/Lee_Error_Recognition_in_Procedural_Videos_using_Generalized_Task_Graph_ICCV_2025_paper.html) | 任务图与视频对齐联合分段和错误类型识别；图推理与异常分支保留的关键对照。 |
| L17 | Mistake Attribution: Fine-Grained Mistake Understanding in Egocentric Videos | Li, Yayuan; Jain, Aadit; Bellos, Filippos 等（共4人） | 2026 | CVPR2026 | [原始页面](https://openaccess.thecvf.com/content/CVPR2026/html/Li_Mistake_Attribution_Fine-Grained_Mistake_Understanding_in_Egocentric_Videos_CVPR_2026_paper.html) | CVPR 2026错误归因同时覆盖指令语义、不可逆时间点及空间位置；限制泛化错误解释的首创主张。 |
| L18 | Polyphony: Diffusion-based Dual-Hand Action Segmentation with Alternating Vision Transformer and Semantic Conditioning | Zheng, Hao; Wang, Hu; Zheng, Tiantian 等（共5人） | 2026 | CVPR2026 | [原始页面](https://openaccess.thecvf.com/content/CVPR2026/html/Zheng_Polyphony_Diffusion-based_Dual-Hand_Action_Segmentation_with_Alternating_Vision_Transformer_and_CVPR_2026_paper.html) | CVPR 2026双手稠密动作分段；工位固定/操作并发时应检查单标签分段的表达边界。 |
| L19 | A Stitch in Time: Learning Procedural Workflow via Self-Supervised Plackett-Luce Ranking | Che, Chengan; Wang, Chao; Chen, Xinyue 等（共5人） | 2026 | CVPR2026 | [原始页面](https://openaccess.thecvf.com/content/CVPR2026/html/Che_A_Stitch_in_Time_Learning_Procedural_Workflow_via_Self-Supervised_Plackett-Luce_CVPR_2026_paper.html) | CVPR 2026使用帧顺序与跨帧对象关联做时序自监督；可作为少标注特征学习的后续路线。 |
| L20 | Ego-Exo4D: Understanding Skilled Human Activity from First- and Third-Person Perspectives | Grauman, Kristen; Westbury, Andrew; Torresani, Lorenzo 等（共100人） | 2024 | CVPR2024 | [原始页面](https://openaccess.thecvf.com/content/CVPR2024/html/Grauman_Ego-Exo4D_Understanding_Skilled_Human_Activity_from_First-_and_Third-Person_Perspectives_CVPR_2024_paper.html) | 同步第一/第三视角、多模态技能活动数据；用于跨视角迁移背景，不能替代工业完成标签。 |
| L21 | MS-TCN: Multi-Stage Temporal Convolutional Network for Action Segmentation | Farha, Yazan Abu; Gall, Jurgen | 2019 | CVPR2019 | [原始页面](https://openaccess.thecvf.com/content_CVPR_2019/html/Abu_Farha_MS-TCN_Multi-Stage_Temporal_Convolutional_Network_for_Action_Segmentation_CVPR_2019_paper.html) | 多阶段扩张时序卷积及过分段惩罚；是轻量时序基线的重要起点。 |
| L22 | The HA4M dataset: Multi-Modal Monitoring of an assembly task for Human Action recognition in Manufacturing | Cicirelli, Grazia; Marani, Roberto; Romeo, Laura 等（共7人） | 2022 | Scientific Data | [原始页面](https://www.nature.com/articles/s41597-022-01843-z) | 制造装配的RGB/深度/IR/点云/骨架多模态数据；为实验室装配识别提供来源，现场域迁移仍需验证。 |
| L23 | ASFormer: Transformer for Action Segmentation | Yi, Fangqiu; Wen, Hongyu; Jiang, Tingting | 2021 | BMVC2021 | [原始页面](https://arxiv.org/abs/2110.08568) | 局部先验、层次序列表示及预测细化；可做离线较强基线，不能直接宣称满足在线延迟。 |
| L24 | Diffusion Action Segmentation | Liu, Daochang; Li, Qiyue; Dinh, AnhDung 等（共6人） | 2023 | ICCV2023 | [原始页面](https://arxiv.org/abs/2303.17959) | 以条件去噪扩散产生动作分段；用于离线性能参照，迭代生成的延迟成本需测量。 |
| L25 | MS-TCN++: Multi-Stage Temporal Convolutional Network for Action Segmentation | Li, Shijie; Farha, Yazan Abu; Liu, Yun 等（共5人） | 2023（预印本2020） | IEEE TPAMI | [原始页面](https://arxiv.org/abs/2006.09220) | MS-TCN的双扩张层和阶段设计改进；比较因果/非因果时必须控制特征、窗口及训练协议。 |
| L26 | On Calibration of Modern Neural Networks | Chuan Guo; Geoff Pleiss; Yu Sun 等（共4人） | 2017 | ICML2017 | [原始页面](https://proceedings.mlr.press/v70/guo17a.html) | 神经网络概率校准与温度缩放；支持拒判阈值的验证集校准，不保证设备缺失或分布外可靠性。 |
| L27 | Temporal Action Segmentation: An Analysis of Modern Techniques | Ding, Guodong; Sener, Fadime; Yao, Angela | 2024（预印本2022） | IEEE TPAMI | [原始页面](https://arxiv.org/abs/2210.10352) | 综述TAS定义、监督形式、特征/时间建模和指标；用于设计分层基线和避开只报帧准确率。 |
| L28 | Declarative workflows: Balancing between flexibility and support | van der Aalst, W. M. P.; Pesic, M.; Schonenberg, H. | 2009 | Computer Science - Research and Development | [原始页面](https://link.springer.com/article/10.1007/s00450-009-0057-9) | Declare声明式流程在支持与灵活性间取平衡；偏序和约束工艺建模已有理论基础。 |
| L29 | Conformance Checking over Uncertain Event Data | Pegoraro, Marco; Uysal, Merih Seran; van der Aalst, Wil M. P. | 2021（预印本2020） | Information Systems | [原始页面](https://arxiv.org/abs/2009.14452) | 明确研究时间、活动及case不确定的日志并给出一致性上下界；本课题事件不确定性不是概念首创。 |
| L30 | Conformance Checking Based on Partially Ordered Event Data | Lu, Xixi; Fahland, Dirk; van der Aalst, Wil M. P. | 2015 | Business Process Management Workshops, LNBIP 202（2015出版） | [原始页面](https://link.springer.com/chapter/10.1007/978-3-319-15895-2_7) | 偏序轨迹与偏序对齐处理并发、顺序及时间戳不确定；时间排序与偏序校验必须和已有方法区分。 |

## 2. 应优先精读的组合

- **界定工业步骤完成**：L06（IndustReal）＋L12（EgoPER错误检测论文）＋L15（MistSense）。先区分动作、正确执行和完成证据。
- **在线动作与错误检测最近邻**：L08＋L11＋L14＋L16。比较在线约束、正常分支、未知错误与任务图作用，不能只拿旧TCN证明创新。
- **不确定事件与偏序语义**：L28＋L29＋L30。建立可复核的时间/位置/case不确定性定义，承认不确定日志与偏序一致性已有成熟研究。
- **2026发展与潜在重叠**：L17（错误语义/时间/空间归因）、L18（双手分段）、L19（时序自监督）。这些文献提供新的最近邻，未证明能在灯具线上直接达到同样效果。
- **最小基线和指标**：L21/L25＋L23/L07＋L27；L26只支持概率校准方法，不能将一个高分数解释为有完整完成证据。

## 3. 年份、场景与方法的核验说明

1. IKEA ASM的本表条目来自**WACV 2021**正式页面；早期预印本年份不同，不写成ECCV 2020。
2. MS-TCN++的原始摘要页首发时间为2020；本轮Crossref记录其TPAMI正式卷期为45(6)、2023。表中同时保留预印本与正式出版年，不能仅根据DOI中的2020推断卷期。
3. TAS综述原始arXiv首发为2022，页面comments标记TPAMI 2023，本轮Crossref正式卷期为46(2)、2024。表中采用卷期年2024并显式标注预印本2022。
4. 不确定事件一致性论文arXiv首发为2020，原页面journal reference明确为Information Systems 102 (2021) 101810，表中采用2021。
5. 2026条目均来自本轮成功读取的CVPR官方页面。2025/2026会议年份只能支持年度覆盖；本表不假称核准了每篇在最近6个月内首次公开，也不声称穷尽最新文献。
6. MS-TCN、ASFormer、FACT、DiffAct的原始任务/摘要不能证明它们具有本研究所需在线性质。未来实验必须审计特征提取、时序操作、后处理和延迟。
7. 公共装配数据含家具、玩具、模型和实验室装配；缺少本灯具正式SOP、孔位、产品周期和设备完成证据。公开基准上的结果与灯具现场实验要分别报告。

## 4. 逐条访问与摘要证据

以下摘录仅用于检查来源与相关性；全文摘要及原始HTML属于采集证据，未据此宣称读过全部论文正文。

### L01 Assembly101: A Large-Scale Multi-View Video Dataset for Understanding Procedural Activities

- 原始URL：https://openaccess.thecvf.com/content/CVPR2022/html/Sener_Assembly101_A_Large-Scale_Multi-View_Video_Dataset_for_Understanding_Procedural_Activities_CVPR_2022_paper.html
- 访问：2026-10-05T15:38:37.372697+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2022；CVPR2022；页面日期字段 `2022`。
- 摘要摘录：“Assembly101 is a new procedural activity dataset featuring 4321 videos of people assembling and disassembling 101 "take-apart" toy vehicles. Participants work without fixed instructions, and …”
- 相关性：多视角装配/拆卸、顺序变体与错误；适合通用事件分段验证，玩具任务不能代替灯具SOP。
- [HTML存档](source_evidence/assembly101_a_large_scale_multi_view_video_dataset_for_understanding_procedural_activities_806a92db.html)；[页面元数据/摘要记录](source_evidence/assembly101_a_large_scale_multi_view_video_dataset_for_understanding_procedural_activities_806a92db.json)。

### L02 The IKEA ASM Dataset: Understanding People Assembling Furniture Through Actions, Objects and Pose

- 原始URL：https://openaccess.thecvf.com/content/WACV2021/html/Ben-Shabat_The_IKEA_ASM_Dataset_Understanding_People_Assembling_Furniture_Through_Actions_WACV_2021_paper.html
- 访问：2026-10-05T15:38:37.321343+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2021；WACV2021；页面日期字段 `2021`。
- 摘要摘录：“The availability of a large labelled dataset is a key requirement for applying deep learning methods to solve various computer vision tasks. In the context …”
- 相关性：多视角家具装配、动作/对象/姿态标注；可检查交互特征，但缺灯具孔位与工艺设备证据。
- [HTML存档](source_evidence/the_ikea_asm_dataset_understanding_people_assembling_furniture_through_actions_objects_and_25185dca.html)；[页面元数据/摘要记录](source_evidence/the_ikea_asm_dataset_understanding_people_assembling_furniture_through_actions_objects_and_25185dca.json)。

### L03 The MECCANO Dataset: Understanding Human-Object Interactions From Egocentric Videos in an Industrial-Like Domain

- 原始URL：https://openaccess.thecvf.com/content/WACV2021/html/Ragusa_The_MECCANO_Dataset_Understanding_Human-Object_Interactions_From_Egocentric_Videos_in_WACV_2021_paper.html
- 访问：2026-10-05T15:38:37.351661+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2021；WACV2021；页面日期字段 `2021`。
- 摘要摘录：“Wearable cameras allow to collect images and videos of humans interacting with the world. While human-object interactions have been thoroughly investigated in third person vision, …”
- 相关性：小零件与工具的时空交互标注；为手—物—工具特征及困难孔位关联提供参照。
- [HTML存档](source_evidence/the_meccano_dataset_understanding_human_object_interactions_from_egocentric_videos_in_an_i_ea6eb03a.html)；[页面元数据/摘要记录](source_evidence/the_meccano_dataset_understanding_human_object_interactions_from_egocentric_videos_in_an_i_ea6eb03a.json)。

### L04 HoloAssist: an Egocentric Human Interaction Dataset for Interactive AI Assistants in the Real World

- 原始URL：https://openaccess.thecvf.com/content/ICCV2023/html/Wang_HoloAssist_an_Egocentric_Human_Interaction_Dataset_for_Interactive_AI_Assistants_ICCV_2023_paper.html
- 访问：2026-10-05T15:38:37.408551+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2023；ICCV2023；页面日期字段 `2023`。
- 摘要摘录：“Building an interactive AI assistant that can perceive, reason, and collaborate with humans in the real world has been a long-standing pursuit in the AI …”
- 相关性：操作与纠错交互数据及错误检测基准；可借鉴纠错标签，不能直接当智能工具完成证据。
- [HTML存档](source_evidence/holoassist_an_egocentric_human_interaction_dataset_for_interactive_ai_assistants_in_the_re_384a189e.html)；[页面元数据/摘要记录](source_evidence/holoassist_an_egocentric_human_interaction_dataset_for_interactive_ai_assistants_in_the_re_384a189e.json)。

### L05 AssemblyHands: Towards Egocentric Activity Understanding via 3D Hand Pose Estimation

- 原始URL：https://openaccess.thecvf.com/content/CVPR2023/html/Ohkawa_AssemblyHands_Towards_Egocentric_Activity_Understanding_via_3D_Hand_Pose_Estimation_CVPR_2023_paper.html
- 访问：2026-10-05T15:38:37.371190+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2023；CVPR2023；页面日期字段 `2023`。
- 摘要摘录：“We present AssemblyHands, a large-scale benchmark dataset with accurate 3D hand pose annotations, to facilitate the study of egocentric activities with challenging hand-object interactions. The …”
- 相关性：装配中多视角3D手姿态与动作理解；对精细手部特征有用，估计手姿态仍不等于完成插接。
- [HTML存档](source_evidence/assemblyhands_towards_egocentric_activity_understanding_via_3d_hand_pose_estimation_03590d31.html)；[页面元数据/摘要记录](source_evidence/assemblyhands_towards_egocentric_activity_understanding_via_3d_hand_pose_estimation_03590d31.json)。

### L06 IndustReal: A Dataset for Procedure Step Recognition Handling Execution Errors in Egocentric Videos in an Industrial-Like Setting

- 原始URL：https://openaccess.thecvf.com/content/WACV2024/html/Schoonbeek_IndustReal_A_Dataset_for_Procedure_Step_Recognition_Handling_Execution_Errors_WACV_2024_paper.html
- 访问：2026-10-05T15:38:37.630691+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2024；WACV2024；页面日期字段 `2024`。
- 摘要摘录：“Although action recognition for procedural tasks has received notable attention, it has a fundamental flaw in that no measure of success for actions is provided. …”
- 相关性：区分动作发生与步骤成功，包含程序和执行错误；最接近工业步骤完成与错误样本设计。
- [HTML存档](source_evidence/industreal_a_dataset_for_procedure_step_recognition_handling_execution_errors_in_egocentri_dfc930bf.html)；[页面元数据/摘要记录](source_evidence/industreal_a_dataset_for_procedure_step_recognition_handling_execution_errors_in_egocentri_dfc930bf.json)。

### L07 FACT: Frame-Action Cross-Attention Temporal Modeling for Efficient Action Segmentation

- 原始URL：https://openaccess.thecvf.com/content/CVPR2024/html/Lu_FACT_Frame-Action_Cross-Attention_Temporal_Modeling_for_Efficient_Action_Segmentation_CVPR_2024_paper.html
- 访问：2026-10-05T15:38:37.665533+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2024；CVPR2024；页面日期字段 `2024`。
- 摘要摘录：“We study supervised action segmentation whose goal is to predict framewise action labels of a video. To capture temporal dependencies over long horizons prior works …”
- 相关性：帧—动作双支交叉注意力分段基线；用于离线比较，在线使用必须独立检查整个输入链的因果性。
- [HTML存档](source_evidence/fact_frame_action_cross_attention_temporal_modeling_for_efficient_action_segmentation_892e4ce0.html)；[页面元数据/摘要记录](source_evidence/fact_frame_action_cross_attention_temporal_modeling_for_efficient_action_segmentation_892e4ce0.json)。

### L08 Progress-Aware Online Action Segmentation for Egocentric Procedural Task Videos

- 原始URL：https://openaccess.thecvf.com/content/CVPR2024/html/Shen_Progress-Aware_Online_Action_Segmentation_for_Egocentric_Procedural_Task_Videos_CVPR_2024_paper.html
- 访问：2026-10-05T15:38:37.694514+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2024；CVPR2024；页面日期字段 `2024`。
- 摘要摘录：“We address the problem of online action segmentation for egocentric procedural task videos. While previous studies have mostly focused on offline action segmentation where entire …”
- 相关性：直接在线分段并建模进度和任务图；主课题的重要最近邻，需比较延迟与错误保留能力。
- [HTML存档](source_evidence/progress_aware_online_action_segmentation_for_egocentric_procedural_task_videos_23e935f0.html)；[页面元数据/摘要记录](source_evidence/progress_aware_online_action_segmentation_for_egocentric_procedural_task_videos_23e935f0.json)。

### L09 Weakly-Supervised Online Action Segmentation in Multi-View Instructional Videos

- 原始URL：https://openaccess.thecvf.com/content/CVPR2022/html/Ghoddoosian_Weakly-Supervised_Online_Action_Segmentation_in_Multi-View_Instructional_Videos_CVPR_2022_paper.html
- 访问：2026-10-05T15:38:37.709619+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2022；CVPR2022；页面日期字段 `2022`。
- 摘要摘录：“This paper addresses a new problem of weakly-supervised online action segmentation in instructional videos. We present a framework to segment streaming videos online at test …”
- 相关性：多视角训练的弱监督在线分段；可借鉴时间戳/多视角标注效率及训练推理一致性。
- [HTML存档](source_evidence/weakly_supervised_online_action_segmentation_in_multi_view_instructional_videos_53f81b9a.html)；[页面元数据/摘要记录](source_evidence/weakly_supervised_online_action_segmentation_in_multi_view_instructional_videos_53f81b9a.json)。

### L10 SVIP: Sequence VerIfication for Procedures in Videos

- 原始URL：https://openaccess.thecvf.com/content/CVPR2022/html/Qian_SVIP_Sequence_VerIfication_for_Procedures_in_Videos_CVPR_2022_paper.html
- 访问：2026-10-05T15:38:37.723195+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2022；CVPR2022；页面日期字段 `2022`。
- 摘要摘录：“In this paper, we propose a novel sequence verification task that aims to distinguish positive video pairs performing the same action sequence from negative ones …”
- 相关性：用步骤变换定义程序序列验证；提供错序评价思路，但视频对相似性不等于完整工艺证据。
- [HTML存档](source_evidence/svip_sequence_verification_for_procedures_in_videos_db3630b0.html)；[页面元数据/摘要记录](source_evidence/svip_sequence_verification_for_procedures_in_videos_db3630b0.json)。

### L11 PREGO: Online Mistake Detection in PRocedural EGOcentric Videos

- 原始URL：https://openaccess.thecvf.com/content/CVPR2024/html/Flaborea_PREGO_Online_Mistake_Detection_in_PRocedural_EGOcentric_Videos_CVPR_2024_paper.html
- 访问：2026-10-05T15:38:37.959066+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2024；CVPR2024；页面日期字段 `2024`。
- 摘要摘录：“Promptly identifying procedural errors from egocentric videos in an online setting is highly challenging and valuable for detecting mistakes as soon as they happen. This …”
- 相关性：在线识别当前动作并与符号预测比对以检测程序错误；需作为在线错误检测参照。
- [HTML存档](source_evidence/prego_online_mistake_detection_in_procedural_egocentric_videos_5aecf414.html)；[页面元数据/摘要记录](source_evidence/prego_online_mistake_detection_in_procedural_egocentric_videos_5aecf414.json)。

### L12 Error Detection in Egocentric Procedural Task Videos

- 原始URL：https://openaccess.thecvf.com/content/CVPR2024/html/Lee_Error_Detection_in_Egocentric_Procedural_Task_Videos_CVPR_2024_paper.html
- 访问：2026-10-05T15:38:37.991509+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2024；CVPR2024；页面日期字段 `2024`。
- 摘要摘录：“We present a new egocentric procedural error dataset containing videos with various types of errors as well as normal videos and propose a new framework …”
- 相关性：手物关系图与正常步骤原型用于错误检测；不能把引入手物图本身视为新的贡献。
- [HTML存档](source_evidence/error_detection_in_egocentric_procedural_task_videos_ac667052.html)；[页面元数据/摘要记录](source_evidence/error_detection_in_egocentric_procedural_task_videos_ac667052.json)。

### L13 PHGC: Procedural Heterogeneous Graph Completion for Natural Language Task Verification in Egocentric Videos

- 原始URL：https://openaccess.thecvf.com/content/CVPR2025/html/Jiang_PHGC_Procedural_Heterogeneous_Graph_Completion_for_Natural_Language_Task_Verification_CVPR_2025_paper.html
- 访问：2026-10-05T15:38:38.031128+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2025；CVPR2025；页面日期字段 `2025`。
- 摘要摘录：“Natural Language-based Egocentric Task Verification (NLETV) aims to equip agents to determine if operation flows of procedural tasks in egocentric videos align with natural language …”
- 相关性：自然语言规则与视频操作流的异构图对齐；SOP语义建模的相关工作，自动规则仍需现场核准。
- [HTML存档](source_evidence/phgc_procedural_heterogeneous_graph_completion_for_natural_language_task_verification_in_e_cbcefbfa.html)；[页面元数据/摘要记录](source_evidence/phgc_procedural_heterogeneous_graph_completion_for_natural_language_task_verification_in_e_cbcefbfa.json)。

### L14 Modeling Multiple Normal Action Representations for Error Detection in Procedural Tasks

- 原始URL：https://openaccess.thecvf.com/content/CVPR2025/html/Huang_Modeling_Multiple_Normal_Action_Representations_for_Error_Detection_in_Procedural_CVPR_2025_paper.html
- 访问：2026-10-05T15:38:38.035128+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2025；CVPR2025；页面日期字段 `2025`。
- 摘要摘录：“Error detection in procedural activities is essential for consistent and correct outcomes in AR-assisted and robotic systems. Existing methods often focus on temporal ordering errors …”
- 相关性：显式建模多个合法下一动作；是允许偏序/多路径时必须考虑的近邻，不能只与唯一下一步比较。
- [HTML存档](source_evidence/modeling_multiple_normal_action_representations_for_error_detection_in_procedural_tasks_56dc476c.html)；[页面元数据/摘要记录](source_evidence/modeling_multiple_normal_action_representations_for_error_detection_in_procedural_tasks_56dc476c.json)。

### L15 MistSense: Versatile Online Detection of Procedural and Execution Mistakes

- 原始URL：https://openaccess.thecvf.com/content/ICCV2025/html/Patsch_MistSense_Versatile_Online_Detection_of_Procedural_and_Execution_Mistakes_ICCV_2025_paper.html
- 访问：2026-10-05T15:38:38.041474+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2025；ICCV2025；页面日期字段 `2025`。
- 摘要摘录：“Online mistake detection is crucial across various domains, ranging from industrial automation to educational applications, as mistakes can be corrected by the human operator after …”
- 相关性：RGB与手姿态联合在线检测程序和执行错误；对本课题创新主张有直接约束。
- [HTML存档](source_evidence/mistsense_versatile_online_detection_of_procedural_and_execution_mistakes_f408dd18.html)；[页面元数据/摘要记录](source_evidence/mistsense_versatile_online_detection_of_procedural_and_execution_mistakes_f408dd18.json)。

### L16 Error Recognition in Procedural Videos using Generalized Task Graph

- 原始URL：https://openaccess.thecvf.com/content/ICCV2025/html/Lee_Error_Recognition_in_Procedural_Videos_using_Generalized_Task_Graph_ICCV_2025_paper.html
- 访问：2026-10-05T15:38:38.274918+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2025；ICCV2025；页面日期字段 `2025`。
- 摘要摘录：“Understanding user actions and their possible mistakes is essential for successful operation of task assistants. In this paper, we develop a unified framework for joint …”
- 相关性：任务图与视频对齐联合分段和错误类型识别；图推理与异常分支保留的关键对照。
- [HTML存档](source_evidence/error_recognition_in_procedural_videos_using_generalized_task_graph_f367a98a.html)；[页面元数据/摘要记录](source_evidence/error_recognition_in_procedural_videos_using_generalized_task_graph_f367a98a.json)。

### L17 Mistake Attribution: Fine-Grained Mistake Understanding in Egocentric Videos

- 原始URL：https://openaccess.thecvf.com/content/CVPR2026/html/Li_Mistake_Attribution_Fine-Grained_Mistake_Understanding_in_Egocentric_Videos_CVPR_2026_paper.html
- 访问：2026-10-05T15:38:38.321558+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2026；CVPR2026；页面日期字段 `2026`。
- 摘要摘录：“We introduce Mistake Attribution (MATT), a new task for fine-grained understanding of human mistakes in egocentric videos. While prior work detects whether a mistake occurs, …”
- 相关性：CVPR 2026错误归因同时覆盖指令语义、不可逆时间点及空间位置；限制泛化错误解释的首创主张。
- [HTML存档](source_evidence/mistake_attribution_fine_grained_mistake_understanding_in_egocentric_videos_0c38eaa7.html)；[页面元数据/摘要记录](source_evidence/mistake_attribution_fine_grained_mistake_understanding_in_egocentric_videos_0c38eaa7.json)。

### L18 Polyphony: Diffusion-based Dual-Hand Action Segmentation with Alternating Vision Transformer and Semantic Conditioning

- 原始URL：https://openaccess.thecvf.com/content/CVPR2026/html/Zheng_Polyphony_Diffusion-based_Dual-Hand_Action_Segmentation_with_Alternating_Vision_Transformer_and_CVPR_2026_paper.html
- 访问：2026-10-05T15:38:38.376437+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2026；CVPR2026；页面日期字段 `2026`。
- 摘要摘录：“Dual-hand action segmentation, densely predicting actions for both hands from untrimmed videos, is essential for understanding complex bimanual activities. However, it poses several unique challenges: …”
- 相关性：CVPR 2026双手稠密动作分段；工位固定/操作并发时应检查单标签分段的表达边界。
- [HTML存档](source_evidence/polyphony_diffusion_based_dual_hand_action_segmentation_with_alternating_vision_transforme_907418c9.html)；[页面元数据/摘要记录](source_evidence/polyphony_diffusion_based_dual_hand_action_segmentation_with_alternating_vision_transforme_907418c9.json)。

### L19 A Stitch in Time: Learning Procedural Workflow via Self-Supervised Plackett-Luce Ranking

- 原始URL：https://openaccess.thecvf.com/content/CVPR2026/html/Che_A_Stitch_in_Time_Learning_Procedural_Workflow_via_Self-Supervised_Plackett-Luce_CVPR_2026_paper.html
- 访问：2026-10-05T15:38:38.365619+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2026；CVPR2026；页面日期字段 `2026`。
- 摘要摘录：“Procedural activities, ranging from routine cooking to complex surgical operations, are highly structured sequences of actions performed in a specific temporal order. Despite the success …”
- 相关性：CVPR 2026使用帧顺序与跨帧对象关联做时序自监督；可作为少标注特征学习的后续路线。
- [HTML存档](source_evidence/a_stitch_in_time_learning_procedural_workflow_via_self_supervised_plackett_luce_ranking_8ab29dd3.html)；[页面元数据/摘要记录](source_evidence/a_stitch_in_time_learning_procedural_workflow_via_self_supervised_plackett_luce_ranking_8ab29dd3.json)。

### L20 Ego-Exo4D: Understanding Skilled Human Activity from First- and Third-Person Perspectives

- 原始URL：https://openaccess.thecvf.com/content/CVPR2024/html/Grauman_Ego-Exo4D_Understanding_Skilled_Human_Activity_from_First-_and_Third-Person_Perspectives_CVPR_2024_paper.html
- 访问：2026-10-05T15:38:38.373334+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2024；CVPR2024；页面日期字段 `2024`。
- 摘要摘录：“We present Ego-Exo4D a diverse large-scale multimodal multiview video dataset and benchmark challenge. Ego-Exo4D centers around simultaneously-captured egocentric and exocentric video of skilled human activities …”
- 相关性：同步第一/第三视角、多模态技能活动数据；用于跨视角迁移背景，不能替代工业完成标签。
- [HTML存档](source_evidence/ego_exo4d_understanding_skilled_human_activity_from_first_and_third_person_perspectives_394ab8ec.html)；[页面元数据/摘要记录](source_evidence/ego_exo4d_understanding_skilled_human_activity_from_first_and_third_person_perspectives_394ab8ec.json)。

### L21 MS-TCN: Multi-Stage Temporal Convolutional Network for Action Segmentation

- 原始URL：https://openaccess.thecvf.com/content_CVPR_2019/html/Abu_Farha_MS-TCN_Multi-Stage_Temporal_Convolutional_Network_for_Action_Segmentation_CVPR_2019_paper.html
- 访问：2026-10-05T15:38:38.596973+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2019；CVPR2019；页面日期字段 `2019`。
- 摘要摘录：“Temporally locating and classifying action segments in long untrimmed videos is of particular interest to many applications like surveillance and robotics. While traditional approaches follow …”
- 相关性：多阶段扩张时序卷积及过分段惩罚；是轻量时序基线的重要起点。
- [HTML存档](source_evidence/ms_tcn_multi_stage_temporal_convolutional_network_for_action_segmentation_658ec713.html)；[页面元数据/摘要记录](source_evidence/ms_tcn_multi_stage_temporal_convolutional_network_for_action_segmentation_658ec713.json)。

### L22 The HA4M dataset: Multi-Modal Monitoring of an assembly task for Human Action recognition in Manufacturing

- 原始URL：https://www.nature.com/articles/s41597-022-01843-z
- 访问：2026-10-05T15:38:42.166995+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2022；Scientific Data；页面日期字段 `2022/12/02`。
- 摘要摘录：“This paper introduces the Human Action Multi-Modal Monitoring in Manufacturing (HA4M) dataset, a collection of multi-modal data relative to actions performed by different subjects building …”
- 相关性：制造装配的RGB/深度/IR/点云/骨架多模态数据；为实验室装配识别提供来源，现场域迁移仍需验证。
- [HTML存档](source_evidence/the_ha4m_dataset_multi_modal_monitoring_of_an_assembly_task_for_human_action_recognition_i_0027df5b.html)；[页面元数据/摘要记录](source_evidence/the_ha4m_dataset_multi_modal_monitoring_of_an_assembly_task_for_human_action_recognition_i_0027df5b.json)。

### L23 ASFormer: Transformer for Action Segmentation

- 原始URL：https://arxiv.org/abs/2110.08568
- 访问：2026-10-05T15:38:39.124710+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2021；BMVC2021；页面日期字段 `2021/10/16`。
- 摘要摘录：“Algorithms for the action segmentation task typically use temporal models to predict what action is occurring at each frame for a minute-long daily activity. Recent …”
- 相关性：局部先验、层次序列表示及预测细化；可做离线较强基线，不能直接宣称满足在线延迟。
- [HTML存档](source_evidence/asformer_transformer_for_action_segmentation_7f292c43.html)；[页面元数据/摘要记录](source_evidence/asformer_transformer_for_action_segmentation_7f292c43.json)。

### L24 Diffusion Action Segmentation

- 原始URL：https://arxiv.org/abs/2303.17959
- 访问：2026-10-05T15:38:39.091354+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2023；ICCV2023；页面日期字段 `2023/03/31`。
- 摘要摘录：“Temporal action segmentation is crucial for understanding long-form videos. Previous works on this task commonly adopt an iterative refinement paradigm by using multi-stage models. We …”
- 相关性：以条件去噪扩散产生动作分段；用于离线性能参照，迭代生成的延迟成本需测量。
- [HTML存档](source_evidence/diffusion_action_segmentation_bac3db68.html)；[页面元数据/摘要记录](source_evidence/diffusion_action_segmentation_bac3db68.json)。

### L25 MS-TCN++: Multi-Stage Temporal Convolutional Network for Action Segmentation

- 原始URL：https://arxiv.org/abs/2006.09220
- 访问：2026-10-05T15:38:39.148317+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2023（预印本2020）；IEEE TPAMI；页面日期字段 `2020/06/16`。
- 摘要摘录：“With the success of deep learning in classifying short trimmed videos, more attention has been focused on temporally segmenting and classifying activities in long untrimmed …”
- 相关性：MS-TCN的双扩张层和阶段设计改进；比较因果/非因果时必须控制特征、窗口及训练协议。
- [HTML存档](source_evidence/ms_tcn_multi_stage_temporal_convolutional_network_for_action_segmentation_d639f1de.html)；[页面元数据/摘要记录](source_evidence/ms_tcn_multi_stage_temporal_convolutional_network_for_action_segmentation_d639f1de.json)。

### L26 On Calibration of Modern Neural Networks

- 原始URL：https://proceedings.mlr.press/v70/guo17a.html
- 访问：2026-10-05T15:38:39.129334+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2017；ICML2017；页面日期字段 `2017/07/17`。
- 摘要摘录：“Confidence calibration – the problem of predicting probability estimates representative of the true correctness likelihood – is important for classification models in many applications. We …”
- 相关性：神经网络概率校准与温度缩放；支持拒判阈值的验证集校准，不保证设备缺失或分布外可靠性。
- [HTML存档](source_evidence/on_calibration_of_modern_neural_networks_9dfa40e8.html)；[页面元数据/摘要记录](source_evidence/on_calibration_of_modern_neural_networks_9dfa40e8.json)。

### L27 Temporal Action Segmentation: An Analysis of Modern Techniques

- 原始URL：https://arxiv.org/abs/2210.10352
- 访问：2026-10-05T15:38:39.207008+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2024（预印本2022）；IEEE TPAMI；页面日期字段 `2022/10/19`。
- 摘要摘录：“Temporal action segmentation (TAS) in videos aims at densely identifying video frames in minutes-long videos with multiple action classes. As a long-range video understanding task, …”
- 相关性：综述TAS定义、监督形式、特征/时间建模和指标；用于设计分层基线和避开只报帧准确率。
- [HTML存档](source_evidence/temporal_action_segmentation_an_analysis_of_modern_techniques_989a840b.html)；[页面元数据/摘要记录](source_evidence/temporal_action_segmentation_an_analysis_of_modern_techniques_989a840b.json)。

### L28 Declarative workflows: Balancing between flexibility and support

- 原始URL：https://link.springer.com/article/10.1007/s00450-009-0057-9
- 访问：2026-10-05T15:38:41.982699+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2009；Computer Science - Research and Development；页面日期字段 `2009/05`。
- 摘要摘录：“Today’s process-aware information systems tend to either support business processes or provide flexibility. Classical workflow management systems offer good process support as long as the …”
- 相关性：Declare声明式流程在支持与灵活性间取平衡；偏序和约束工艺建模已有理论基础。
- [HTML存档](source_evidence/declarative_workflows_balancing_between_flexibility_and_support_6b9ee87d.html)；[页面元数据/摘要记录](source_evidence/declarative_workflows_balancing_between_flexibility_and_support_6b9ee87d.json)。

### L29 Conformance Checking over Uncertain Event Data

- 原始URL：https://arxiv.org/abs/2009.14452
- 访问：2026-10-05T15:38:39.241379+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2021（预印本2020）；Information Systems；页面日期字段 `2020/09/29`。
- 摘要摘录：“The strong impulse to digitize processes and operations in companies and enterprises have resulted in the creation and automatic recording of an increasingly large amount …”
- 相关性：明确研究时间、活动及case不确定的日志并给出一致性上下界；本课题事件不确定性不是概念首创。
- [HTML存档](source_evidence/conformance_checking_over_uncertain_event_data_27cc1eb5.html)；[页面元数据/摘要记录](source_evidence/conformance_checking_over_uncertain_event_data_27cc1eb5.json)。

### L30 Conformance Checking Based on Partially Ordered Event Data

- 原始URL：https://link.springer.com/chapter/10.1007/978-3-319-15895-2_7
- 访问：2026-10-05T15:38:42.308986+08:00（Asia/Shanghai）；HTTP 200。
- 出版记录：2015；Business Process Management Workshops, LNBIP 202（2015出版）；页面日期字段 `2015`。
- 摘要摘录：“Conformance checking is becoming more important for the analysis of business processes. While the diagnosed results of conformance checking techniques are used in diverse context …”
- 相关性：偏序轨迹与偏序对齐处理并发、顺序及时间戳不确定；时间排序与偏序校验必须和已有方法区分。
- [HTML存档](source_evidence/conformance_checking_based_on_partially_ordered_event_data_d9c2a15f.html)；[页面元数据/摘要记录](source_evidence/conformance_checking_based_on_partially_ordered_event_data_d9c2a15f.json)。

