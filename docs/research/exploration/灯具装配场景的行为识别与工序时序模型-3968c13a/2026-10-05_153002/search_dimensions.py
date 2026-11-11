from collect_sources import *

dimensions = [
('热点','procedural activity understanding temporal action segmentation 2025 2026'),
('开放问题','industrial assembly online mistake detection occlusion uncertainty'),
('综述','Temporal Action Segmentation An Analysis of Modern Techniques'),
('基准','IKEA ASM MECCANO HA4M industrial assembly dataset'),
('应用','IndustReal procedure step recognition execution errors'),
('交叉领域','runtime verification LTL three valued semantics Declare conformance checking'),
('最近6–12个月','procedural mistake detection temporal action segmentation 2026'),
]
note = '# 检索笔记与访问证据\n\n研究方向：灯具装配场景的行为识别与工序时序模型。检索日：2026-10-05（Asia/Shanghai）。本轮没有 WebSearch/WebFetch 接口，使用 requests 读取 CVF 官方会议目录/摘要页、Crossref 检索 API 及出版商/原始摘要页。Crossref 仅用于发现和交叉核对，入选文献另抓取原始页面。未调用 LLM SDK、未执行外部代码。\n\n截至本日最近12个月指2025-10-05至2026-10-05；最近6个月指2026-04-05至2026-10-05。会议年份不等于论文首次公开日。\n\n'
(RUN/'search_notes.md').write_text(note,encoding='utf-8')
for i,(dimension,query) in enumerate(dimensions,1):
    params={'query':query,'rows':8}
    if i==7: params['filter']='from-pub-date:2025-10-05,until-pub-date:2026-10-05'
    try:
        r=SESSION.get('https://api.crossref.org/works',params=params,timeout=35); r.raise_for_status()
        items=r.json()['message']['items']
        data={'dimension':dimension,'query':query,'url':r.url,'accessed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'items':items}
        (RAW/f'dimension_{i}_crossref.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        lines=[f'## {i}. {dimension}',f'\n- 检索词：`{query}`',f'- 检索端点：{r.url}',f'- UTC访问时间：{data["accessed_at"]}',f'- 返回候选 {len(items)} 条；检索响应归档：`source_evidence/dimension_{i}_crossref.json`。']
        lines += [f'  - {x.get("title",[""])[0]} | {x.get("DOI", "")} | {x.get("published",{}).get("date-parts",[])}' for x in items[:5]]
    except Exception as e:
        lines=[f'## {i}. {dimension}',f'\n- 检索词：`{query}`',f'- 请求失败：{e}；改用已抓取的 CVF 官方会议目录逐题筛选，失败不能解释为无文献。']
    with (RUN/'search_notes.md').open('a',encoding='utf-8') as f: f.write('\n'.join(lines)+'\n\n')
    print(json.dumps({'dimension':dimension,'lines':lines},ensure_ascii=False),flush=True)
