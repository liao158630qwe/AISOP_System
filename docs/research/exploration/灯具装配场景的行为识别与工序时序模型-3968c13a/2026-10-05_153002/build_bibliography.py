from collect_sources import *
from zoneinfo import ZoneInfo

sources=json.loads((RUN/'verified_sources.json').read_text(encoding='utf-8'))
assert len(sources)==30
notes=[
'多视角装配/拆卸、顺序变体与错误；适合通用事件分段验证，玩具任务不能代替灯具SOP。',
'多视角家具装配、动作/对象/姿态标注；可检查交互特征，但缺灯具孔位与工艺设备证据。',
'小零件与工具的时空交互标注；为手—物—工具特征及困难孔位关联提供参照。',
'操作与纠错交互数据及错误检测基准；可借鉴纠错标签，不能直接当智能工具完成证据。',
'装配中多视角3D手姿态与动作理解；对精细手部特征有用，估计手姿态仍不等于完成插接。',
'区分动作发生与步骤成功，包含程序和执行错误；最接近工业步骤完成与错误样本设计。',
'帧—动作双支交叉注意力分段基线；用于离线比较，在线使用必须独立检查整个输入链的因果性。',
'直接在线分段并建模进度和任务图；主课题的重要最近邻，需比较延迟与错误保留能力。',
'多视角训练的弱监督在线分段；可借鉴时间戳/多视角标注效率及训练推理一致性。',
'用步骤变换定义程序序列验证；提供错序评价思路，但视频对相似性不等于完整工艺证据。',
'在线识别当前动作并与符号预测比对以检测程序错误；需作为在线错误检测参照。',
'手物关系图与正常步骤原型用于错误检测；不能把引入手物图本身视为新的贡献。',
'自然语言规则与视频操作流的异构图对齐；SOP语义建模的相关工作，自动规则仍需现场核准。',
'显式建模多个合法下一动作；是允许偏序/多路径时必须考虑的近邻，不能只与唯一下一步比较。',
'RGB与手姿态联合在线检测程序和执行错误；对本课题创新主张有直接约束。',
'任务图与视频对齐联合分段和错误类型识别；图推理与异常分支保留的关键对照。',
'CVPR 2026错误归因同时覆盖指令语义、不可逆时间点及空间位置；限制泛化错误解释的首创主张。',
'CVPR 2026双手稠密动作分段；工位固定/操作并发时应检查单标签分段的表达边界。',
'CVPR 2026使用帧顺序与跨帧对象关联做时序自监督；可作为少标注特征学习的后续路线。',
'同步第一/第三视角、多模态技能活动数据；用于跨视角迁移背景，不能替代工业完成标签。',
'多阶段扩张时序卷积及过分段惩罚；是轻量时序基线的重要起点。',
'制造装配的RGB/深度/IR/点云/骨架多模态数据；为实验室装配识别提供来源，现场域迁移仍需验证。',
'局部先验、层次序列表示及预测细化；可做离线较强基线，不能直接宣称满足在线延迟。',
'以条件去噪扩散产生动作分段；用于离线性能参照，迭代生成的延迟成本需测量。',
'MS-TCN的双扩张层和阶段设计改进；比较因果/非因果时必须控制特征、窗口及训练协议。',
'神经网络概率校准与温度缩放；支持拒判阈值的验证集校准，不保证设备缺失或分布外可靠性。',
'综述TAS定义、监督形式、特征/时间建模和指标；用于设计分层基线和避开只报帧准确率。',
'Declare声明式流程在支持与灵活性间取平衡；偏序和约束工艺建模已有理论基础。',
'明确研究时间、活动及case不确定的日志并给出一致性上下界；本课题事件不确定性不是概念首创。',
'偏序轨迹与偏序对齐处理并发、顺序及时间戳不确定；时间排序与偏序校验必须和已有方法区分。',
]
year_overrides={25:'2023（预印本2020）',27:'2024（预印本2022）',29:'2021（预印本2020）'}
doi_overrides={25:'10.1109/TPAMI.2020.3021756',27:'10.1109/TPAMI.2023.3327284',28:'10.1007/s00450-009-0057-9',29:'10.1016/j.is.2021.101810',30:'10.1007/978-3-319-15895-2_7'}
for i,s in enumerate(sources,1):
    assert s.get('authors') and s.get('abstract') and s.get('http_status')==200 and s.get('publication_date'),s
    s['id']=f'L{i:02}'
    s['bibliographic_year']=year_overrides.get(i,str(s['publication_date'])[:4])
    s['relevance_note_zh']=notes[i-1]
    if i in doi_overrides:s['doi']=doi_overrides[i]
    if i==30:s['venue']='Business Process Management Workshops, LNBIP 202（2015出版）'
    s['accessed_at_shanghai']=datetime.datetime.fromisoformat(s['accessed_at']).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()

(RUN/'verified_sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding='utf-8')
out='''# 灯具装配行为识别与工序时序：文献预调研

检索与核验日期：2026-10-05（Asia/Shanghai）。本表收录30项代表性研究，所有条目均在本轮成功获取原始摘要页、正式会议页或出版商页，未将记忆条目、旧项目参考文献或仅有搜索结果的记录算作已核验来源。范围是选题级预调研，主要依据摘要和出版元数据；不是逐篇全文评述或系统综述。

表中作者超过3人时缩写，完整原始作者列表、访问时间和摘要证据保存在[verified_sources.json](verified_sources.json)。本页后半部分提供逐条访问记录与不超过25个英文词的摘要摘录，原始HTML与元数据文件保留在`source_evidence/`。论文摘要中的性能数字均为作者对各自数据集的报告，本研究未复现，也不据此推算灯具准确率。

## 1. 代表性文献表

| 编号 | 标题 | 作者 | 年份 | 会议／期刊 | 本轮读取的原始URL | 与灯具研究的关系 |
|---|---|---|---|---|---|---|
'''
for s in sources:
    authors='; '.join(s['authors'][:3])+(f' 等（共{len(s["authors"])}人）' if len(s['authors'])>3 else '')
    out+=f'| {s["id"]} | {s["canonical_title"]} | {authors} | {s["bibliographic_year"]} | {s["venue"]} | [原始页面]({s["url"]}) | {s["relevance_note_zh"]} |\n'
out+='''
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

'''
for s in sources:
    abstract=re.sub(r'^Abstract:?\s*','',s['abstract']).strip()
    excerpt=' '.join(abstract.split()[:25])
    out+=f'### {s["id"]} {s["canonical_title"]}\n\n'
    out+=f'- 原始URL：{s["url"]}\n- 访问：{s["accessed_at_shanghai"]}（Asia/Shanghai）；HTTP 200。\n'
    out+=f'- 出版记录：{s["bibliographic_year"]}；{s["venue"]}；页面日期字段 `{s["publication_date"]}`。\n'
    out+=f'- 摘要摘录：“{excerpt} …”\n'
    out+=f'- 相关性：{s["relevance_note_zh"]}\n'
    out+=f'- [HTML存档]({s["html_evidence"]})；[页面元数据/摘要记录]({s["json_evidence"]})。\n\n'
(RUN/'literature_pre_survey.md').write_text(out,encoding='utf-8')

with (RUN/'search_notes.md').open('a',encoding='utf-8') as f:
    f.write('''## 8. 官方目录扩展、筛选与纠错

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
''')
print(json.dumps({'sources':len(sources),'report':str(RUN/'literature_pre_survey.md'),'all_abstracts_present':all(s.get('abstract') for s in sources)},ensure_ascii=False))
