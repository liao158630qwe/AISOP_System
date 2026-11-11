# 数据库与取证存储优化验收记录

日期：2026-11-06。实现与配置说明见 [数据库与告警录像存储](../guides/database-storage.md)。本报告区分已执行的隔离测试、合成性能测量和未执行的生产验收。

## 本次完成的变更

原先实时检测的结果主要保存在内存，结束/释放流后缺少独立告警录像索引；保留期配置也可以短于30天。现在实时FAIL及最终REVIEW自动落盘取证，后台转成H.264/MP4并登记录像索引；离线异常记录复用已存在的WebM文件，保留期引用阻止普通清理提前删除。

数据采用“SQL元数据与检索索引 + 独立视频文件 + 压缩证据清单”。SQL不存图像/视频BLOB，历史列表不读完整模型配置，轮询不读大JSON；添加复合索引和JSON路径表达式索引。清理批量删除元数据，目录按有界批次扫描。PostgreSQL连接池有明确上限和短等待；SQLite采用WAL及FULL同步。

录像保留期从成功归档的数据库时间开始计算，最低2,592,000秒。READY记录必须具备路径、校验值、大小和有效保留时间；到期后分阶段删除，失败继续重试。旧任务队列和新归档队列均使用租约/token隔离过期执行者。

## 功能与回归结果

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| Python全量回归 | **377通过，2跳过，0失败，0错误**，耗时64.96秒 | `test-results/storage-full-suite.xml` |
| 新增录像专项 | 19项通过：实际MP4编码、浏览器兼容数据格式、私有Range访问、故障恢复、保留策略等 | `tests/test_evidence_storage.py` |
| 真实API+浏览器 | 9项验收点通过，无页面异常 | `test-results/evidence-ui/run-aa0e909c66/verification.json` |
| 既有实时传输浏览器回归 | 15项验收点通过，无页面异常 | `test-results/realtime-transport-20261105/verification.json` |
| 本机业务库副本增量升级 | 连续初始化两次，旧表全部记录逐行摘要一致；完整性检查前后均为ok | `test-results/storage-migration/verification.json` |
| PostgreSQL实库 | **未运行**；两项测试因缺少`TEST_POSTGRES_URL`跳过 | `tests/test_postgres.py` |

两条SQLAlchemy警告来自SQLite反射无法枚举表达式索引。初始化改用原生 `CREATE INDEX IF NOT EXISTS`，实测可重复执行；另有实际带参数的EXPLAIN测试确认类型过滤使用表达式索引。警告不表示索引创建失败。

浏览器验证使用真实隔离SQLite、实际API/认证/CSRF/MP4文件，以及合成图像和判定。首屏30条、游标加载到33条、FAILED重试到READY、保留时间显示、视频解码与播放、SHA-256展示均通过。录像归档验证不依赖真实缺陷识别准确率。

![告警录像列表](../../test-results/evidence-ui/run-aa0e909c66/history.png)

![浏览器实际播放取证MP4](../../test-results/evidence-ui/run-aa0e909c66/playback.png)

## 5万条记录性能测量

环境：Windows-11-10.0.22621-SP0；Python 3.12.7；SQLAlchemy 2.0.34；SQLite本地磁盘、WAL、`synchronous=FULL`。构造50,000条任务，每条配置JSON约7,582字节，1%为FAILED。比较同一账户最近100条FAILED历史，预热后各执行40轮。旧查询读取整行/完整JSON，新查询投影列表所需字段并使用复合索引，优化侧执行ANALYZE后测量。该对照衡量本次组合优化，不能将提升全部归因于某一个索引。

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 查询中位耗时 | 27.480 ms | 2.324 ms |
| 查询P95 | 45.120 ms | 3.350 ms |
| 查询结果统一JSON序列化体积 | 741,801 bytes | 26,901 bytes |
| 索引选择 | `owner/created_at/id` | `owner/status/created_at/id` |

本轮中位耗时提高 **11.82倍**，取回结果的统一序列化体积减少 **96.37%**。体积指标是查询取回行的标准化JSON大小，用于对比物化数据量，不是抓包测得的PostgreSQL传输字节数；最终HTTP列表字段保持兼容。这里只构造一个媒体行，执行计划中的 `SCAN media` 扫描的是这个单行夹具。

8线程并发执行32次优化查询，5轮批次的中位完成时间为34.386ms，最慢/P95为46.916ms。样本有限，不能据此推断现场最大QPS或连接数。

## 采样与编码测量

| 测量 | 本轮结果 |
| --- | ---: |
| 非告警取证缓存1000次操作P95 | 0.001 ms |
| 上述缓存操作额外告警表SQL | 0 |
| 告警发生后同步写入暂存并登记队列 | 126.967 ms |
| 后台编码并发布READY | 217.363 ms |
| 采样帧数 | 51 |
| 本轮MP4大小 | 195,532 bytes |
| 写入保留期 | 2,592,000秒 |

输入为640×360的固定随机噪声图像，重复形成51个时间采样，因此不能将本轮195KB级文件大小视作真实运动画面的平均压缩率。该测量不包含GPU推理、真实网络、认证开销和持续排队时间。正常HTTP实时检测仍会读取会话；“零SQL”仅指新增取证缓存逻辑没有每帧额外查询/写入告警表。

告警发生时需要同步落盘才确认PENDING，因此触发帧会承担约127ms的存储成本；视频编码在后台运行，不在GPU推理锁内。这个成本是实测结果的一部分，未通过内存排队将其隐藏为“已持久化”。

## 故障与保留边界

- 第29天和第30天整点不删，到期后1秒具备清理条件；支持dry-run、延长到60天和legal_hold。删除失败已进入DELETING时，延长策略仍会阻止过早重试。
- 数据库在告警写入时中断，完整暂存仍落盘；新服务恢复队列后能生成READY录像。插入已提交但回执丢失时，确定性ID避免重复记录。
- 两个归档器竞争同一任务只有一个领取；编码期间允许其他数据库写入。旧token完成也不能覆盖新归档者发布的路径。
- 逐帧摘要不符时拒绝归档，最多3次自动尝试后FAILED；原暂存继续保留，修复后通过带CSRF的重试接口归档成功。
- 文件被占用导致删除失败时保留索引和重试状态，不提前宣布清理成功。路径越界、目录链接和错配归档身份被拒绝。
- 磁盘写入失败时向客户端返回UNAVAILABLE取证回执，模型FAIL结论仍保留；不会虚报READY。
- 离线异常录像即使遇到短周期低层清理调用，也因有效取证引用而保留，直到取证过期。

## 兼容性副本验证

通过SQLite只读备份接口复制 `.runtime/local.db` 后在副本上执行两次初始化，未改动源业务数据库。源表记录数：analysis_runs=58、media=35、run_metrics=51，其余现有表也逐行摘要校验一致。新增 `alert_evidence` 表，未重写老表JSON列，未删除历史数据。副本的完整性检查前后均为 `ok`。

## 复现与运行边界

```powershell
npm run test:storage
npm run test:evidence-ui
npm run benchmark:storage -- --rows 50000
node tools/python.cjs -m pytest tests -q --junitxml=test-results/storage-full-suite.xml
```

完整原始基准：[storage-benchmark.json](../../test-results/storage-benchmark.json)；代码：[benchmark-storage.py](../../tools/benchmark-storage.py)。

当前代码与隔离验证已经完成，未自动重启当前主服务、升级业务库或清理业务录像。启用前按指南安装依赖、备份并初始化数据库，然后重启API/Worker/Nginx。首次大表建索引应安排维护窗口。

本次未进行PostgreSQL实库执行、Linux Compose容器验收、连续30天运行或真实工位缺陷评测。保留期边界使用模拟时钟；客户端只有JPEG采样，尚无RTSP/NVR原始全帧率连续录像。上述范围需在现场或专用环境单独验收。历史已完成任务不自动回填新的告警索引。
