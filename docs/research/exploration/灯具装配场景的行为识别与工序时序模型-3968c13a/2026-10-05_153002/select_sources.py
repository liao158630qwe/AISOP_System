from collect_sources import *
prefixes=['Assembly101:', 'The IKEA ASM Dataset:', 'The MECCANO Dataset:', 'HoloAssist:', 'AssemblyHands:', 'IndustReal:', 'FACT:', 'Progress-Aware Online', 'Weakly-Supervised Online Action', 'SVIP:', 'PREGO:', 'Error Detection in Egocentric', 'PHGC:', 'Modeling Multiple Normal', 'MistSense:', 'Error Recognition in Procedural', 'Streaming VideoLLMs', 'Mistake Attribution:', 'Polyphony:', 'A Stitch in Time:']
items=[]
for prefix in prefixes:
    matches=[x for f in RAW.glob('index_*.json') for x in json.loads(f.read_text(encoding='utf-8'))['matches'] if x['title'].startswith(prefix)]
    assert len(matches)==1,(prefix,matches)
    items.append(matches[0])
items += [
{'title':'Ego-Exo4D: Understanding Skilled Human Activity from First- and Third-Person Perspectives','venue':'CVPR2024','url':'https://openaccess.thecvf.com/content/CVPR2024/html/Grauman_Ego-Exo4D_Understanding_Skilled_Human_Activity_from_First-_and_Third-Person_Perspectives_CVPR_2024_paper.html'},
{'title':'MS-TCN: Multi-Stage Temporal Convolutional Network for Action Segmentation','venue':'CVPR2019','url':'https://openaccess.thecvf.com/content_CVPR_2019/html/Abu_Farha_MS-TCN_Multi-Stage_Temporal_Convolutional_Network_for_Action_Segmentation_CVPR_2019_paper.html'},
{'title':'The HA4M dataset: Multi-Modal Monitoring of an assembly task for Human Action recognition in Manufacturing','venue':'Scientific Data','url':'https://www.nature.com/articles/s41597-022-01843-z'},
{'title':'ASFormer: Transformer for Action Segmentation','venue':'BMVC2021','url':'https://arxiv.org/abs/2110.08568'},
{'title':'Diffusion Action Segmentation','venue':'ICCV2023','url':'https://arxiv.org/abs/2303.17959'},
{'title':'MS-TCN++: Multi-Stage Temporal Convolutional Network for Action Segmentation','venue':'IEEE TPAMI','url':'https://arxiv.org/abs/2006.09220'},
{'title':'On Calibration of Modern Neural Networks','venue':'ICML2017','url':'https://proceedings.mlr.press/v70/guo17a.html'},
]
(RUN/'selected_urls.json').write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf-8')
print('Selected',len(items))
