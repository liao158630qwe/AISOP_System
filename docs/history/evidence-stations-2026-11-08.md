# 告警录像按工位查看

2026-11-08 已启用到本机服务，API 与 Worker 就绪。

## 使用方式

- 从工位页面点击“告警录像”，默认打开当前工位的录像列表。
- 使用“查看工位”切换工位 1–6，每次只显示一个工位；列表和播放标题均显示所属工位。
- 无有效工位标识的历史录像单独放在“未关联工位”，不会按检测模型猜测归属。
- 切换工位会清空上一工位的列表和分页游标；迟到的请求不会覆盖新选择。
- 播放页面提供返回原工位列表的按钮。刷新、分页和归档重试保留当前选择。

## 接口与兼容

`GET /api/v1/alerts?station_id=station-2&limit=30` 在数据库分页前筛选工位；
`station_id=unassigned` 查询无归属记录。列表、详情和重试响应新增
`station_id`、`station_name`。旧客户端省略参数仍可查询其账户的原有列表。

归属优先使用录像保存时的 `details.result.stationId`；缺失或无效时，实时录像可通过
`source_id` 匹配同一账户的 `station_inspections.stream_id`。离线来源不会误关联同名实时流。
本次没有数据库迁移，架构仍为 v2，也没有改写既有录像或证据元数据。

## 验证

- SQLite 告警归档、真实工位结束归档和工位筛选测试：38 通过，8 个 PostgreSQL 用例另行验证。
- 隔离 PostgreSQL 实例的工位筛选和历史兼容测试：17 通过，临时测试 schema 全部清理。
- 静态资源与检测历史回归：31 通过。
- 浏览器测试：真实 SQLite、HTTP、登录、MP4 编码与解码；检测画面和判定为测试夹具。
  覆盖工位默认值、隔离、分页、空列表、未关联工位、慢请求、错误恢复、重试、播放返回及手机布局。
- 本机现有 4 条录像：工位 2 有 3 条，未关联工位有 1 条；全部通过视频 Range 下载检查，
  并实际播放现有工位录像。升级前后告警记录指纹完全一致，页面无 JavaScript 错误。

报告：

- `test-results/evidence-stations-sqlite.xml`
- `test-results/evidence-stations-postgres.xml`
- `test-results/evidence-stations-history-regression.xml`
- `test-results/evidence-ui/run-137ad62f08/verification.json`
- `test-results/evidence-stations-live-smoke.json`
- `test-results/evidence-stations-live.png`

复验命令：

```powershell
node tools/python.cjs -m pytest tests/test_evidence_stations.py tests/test_station_evidence.py tests/test_evidence_storage.py -q
node tools/python.cjs tools/test-evidence-history.py
```

设置 `TEST_POSTGRES_URL` 后，工位筛选测试会为 PostgreSQL 创建独立 schema 并在结束时清理。
