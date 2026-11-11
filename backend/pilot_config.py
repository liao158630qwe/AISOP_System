"""Manual station definitions; no model resources or inferred quantities."""


def positions(prefix, count, label):
    return [{'id': f'{prefix}{i:02}', 'label': f'{label} {i}'} for i in range(1, count + 1)]


def stage(identity, name, items, guidance, requires=()):
    return dict(id=identity, name=name, items=items, guidance=guidance, requires=list(requires))


def screws(count, requires=()):
    return stage('SCREWS', f'检查{count}处螺丝', positions('S', count, '螺丝'),
                 f'逐项核对 S01 至 S{count:02} 的当前安装状态，保持同一工件。', requires)


def terminals(label='端子连接'):
    return stage('TERMINALS', label, [{'id': 'TERMINALS', 'label': label + '人工记录'}],
                 '记录实际连接数量、接口位置与可见完成情况；数量和到位标准尚待现场确认。')


STATIONS = {
    'station-1': dict(title='4孔检查后4螺丝', stages=[
        stage('HOLES', '检查4个定位孔', positions('H', 4, '定位孔'),
              '装配遮挡前逐项检查 H01 至 H04，保存证据后进入下一阶段。'),
        screws(4, ['HOLES']),
    ], notes='先保存4孔前置证据，再确认4处螺丝。', pending_specs=[]),
    'station-2': dict(title='5螺丝检查', stages=[screws(5)],
                     notes='S01 至 S05 独立核对，不重复累计同一位置。', pending_specs=[]),
    'station-3': dict(title='2螺丝与黄色卡扣', stages=[
        screws(2), stage('CLIPS', '黄色卡扣检查', [{'id': 'CLIPS', 'label': '黄色卡扣安装人工记录'}],
                        '记录黄色卡扣的实际数量、位置及可见安装状态；具体要求待现场确认。'),
    ], notes='螺丝与卡扣独立取证，可选择任一未完成项目；尚未设置强制先后。',
        pending_specs=['黄色卡扣数量、位置及到位标准', '卡扣与螺丝的先后关系']),
    'station-4': dict(title='2螺丝检查', stages=[screws(2)],
                     notes='逐项确认 S01、S02 两处螺丝。', pending_specs=[]),
    'station-5': dict(title='接端子后8螺丝', stages=[terminals(), screws(8, ['TERMINALS'])],
                     notes='先保存接端子记录，再开放 S01 至 S08 螺丝检查。',
                     pending_specs=['端子数量、接口位置及可见完成标准']),
    'station-6': dict(title='吹净、端子与4螺丝', stages=[
        stage('HOUSING_AIR', '壳体吹净', [{'id': 'HOUSING_AIR', 'label': '壳体吹净动作人工记录'}],
              '单独记录壳体对象的吹净动作及覆盖区域，保留该动作的阶段截图。'),
        stage('FRAME_AIR', '装饰框总成吹净', [{'id': 'FRAME_AIR', 'label': '装饰框总成吹净动作人工记录'}],
              '单独记录装饰框总成的吹净动作，不能用壳体吹净记录代替。'),
        terminals('线束端子连接'), screws(4),
    ], notes='四类项目分别取证，可选择未完成项目；展示顺序不代表强制工序。',
        pending_specs=['线束端子数量、接口位置及可见完成标准', '吹净区域、持续时间及完成标准', '四类任务的先后或并行关系']),
}

for identity, config in STATIONS.items():
    config.update(station=identity, name='工位' + identity[-1], version=identity + '-assisted-v1',
                  mode='manual', model=None, final_evidence_max_age_seconds=180)

# Keep the original shape available to existing clients and saved station-1 records.
STATIONS['station-1'].update(holes=[f'H{i:02}' for i in range(1, 5)], screws=[f'S{i:02}' for i in range(1, 5)])


def stages(config):
    if 'stages' in config:
        return config['stages']
    return [dict(item, items=[{'id': identity, 'label': identity} for identity in config[key]])
            for item, key in zip(STATIONS['station-1']['stages'], ('holes', 'screws'))]


def final_stages(config):
    prerequisites = {identity for item in stages(config) for identity in item['requires']}
    return [item for item in stages(config) if item['id'] not in prerequisites]
