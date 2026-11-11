from collect_sources import *
items=[
{'title':'Temporal Action Segmentation: An Analysis of Modern Techniques','venue':'IEEE TPAMI','url':'https://arxiv.org/abs/2210.10352'},
{'title':'Runtime Verification for LTL and TLTL','venue':'ACM Transactions on Software Engineering and Methodology','url':'https://dl.acm.org/doi/10.1145/2000799.2000800'},
{'title':'Declarative workflows: Balancing between flexibility and support','venue':'Computer Science - Research and Development','url':'https://link.springer.com/article/10.1007/s00450-009-0057-9'},
{'title':'The HA4M dataset: Multi-Modal Monitoring of an assembly task for Human Action recognition in Manufacturing','venue':'Scientific Data','url':'https://www.nature.com/articles/s41597-022-01843-z'},
]
with cf.ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(fetch_paper,items))
(RUN/'additional_sources.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=False,indent=2))
