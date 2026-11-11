# 正式后台采集与轮次控制补齐

日期：2026-11-08。首页工位 1–6 新增“后台采集（正式）”，使用 `station-collector.py --controlled`。本次解决此前首页刷新会中断浏览器采集、常驻采集器缺少逐件控制的两处缺口。启用步骤见[正式采集指南](../guides/controlled-station-collection.md)。

## 结果

- 独立采集器持有相机。页面刷新、关闭、切换工位或退出登录只停止页面观察；同一检测轮次继续采样。
- 每件显式开始、结束归档或中断。开始前的缓存帧丢弃，下一件使用新的流与时序状态；空闲不自动建轮。
- 本地持久请求和服务端命令均保留原 UUID。响应丢失先精确核对，已执行命令不重复执行；结束必须确认最终判定入库并释放流才回执。
- 控制请求冻结操作身份、设备实例、绑定版本与服务代次，未送达的旧请求不能在换绑或重启后控制新工件。
- 相机故障、采集器失联、服务重启使旧轮次中断；迟到的 GPU/取证结果在发布前再检查状态，不恢复旧 PASS。
- 显式数据库 v3 迁移只新增控制表，保留已有检测与人工复核历史。

## 验证

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 综合后端、静态资源、采集器及迁移回归 | 179 passed，27 skipped；该轮未配置 PostgreSQL 的参数跳过 | `test-results/controlled-collection-regression.xml` |
| 隔离 PostgreSQL 与 SQLite 迁移/采集器联动 | 55 passed，1 skipped；无遗留测试 schema | `test-results/controlled-collection-postgres.xml` |
| 最后身份前置校验修改后的定向回归 | 40 passed，8 skipped（未配置 PostgreSQL 参数） | `tests/test_collector_control.py`、`tests/test_controlled_collection_integration.py`、`tests/test_static_assets.py` |
| 真实浏览器＋HTTP API＋SQL＋独立采集循环 | 通过：丢回包、刷新同流新帧、关闭唯一页面继续采样、归档/释放回执、下一件、中断 | [报告](../../test-results/controlled-collection-e2e/run-5dp0yubs/controlled-collection-ui.json) |
| 浏览器恢复与界面边界 | 16 类检查通过，含真实命令 API 开始/刷新/停止、BFCache、持久请求、存储失败及迟到预览 | [报告](../../test-results/pilot-browser-tests/run-c8nsjy_6/station-collector-ui.json) |
| 原画面流水线 | 25/25 | [报告](../../test-results/pilot-browser-tests/run-47qitrxg/station-pipeline-ui.json) |
| 原 NG 确认恢复 | 通过 | [报告](../../test-results/pilot-browser-tests/run-6r3oovhs/review-recovery.json) |
| 原六工位归档 | 通过 | [报告](../../test-results/pilot-browser-tests/run-dw_nnj80/station-completion.json) |

各测试均使用隔离数据库与测试服务，没有迁移或重启现场服务。真实链路测试仅替代相机和算法推理，使用生产采集器控制状态机；故障浏览器测试的模拟控制/算法边界在报告中标明。本次不包含物理相机、六路 GPU 吞吐或识别准确率验收。

页面摄像头仍依赖页面生命周期，选项明确标注“刷新会中断”；该路径不能提供后台持续采集。正式逐件操作需使用已连接的受控采集器。独立 `/terminal` 页面保持原行为。
