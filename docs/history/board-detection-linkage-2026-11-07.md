# Board 无工件检测联动 · 2026-11-07

用户要求先取消工件依赖并使用指定视频联动检测。原 Board 读取人工工件流程计数，视频观察轮次虽在推理，但总工件数与合格数为 0。

`/board` 现在读取 `GET /api/v1/board/snapshot?mode=detection`，约每秒更新。新模块 `backend/board_detection.py` 按检测流统计轮次，读取六工位实时状态与当日检测归档。实时判定、最终归档按同一流去重，未绑定工件的归档也会显示。兼容生产接口未移除，既有工件记录未修改。

卡片展示实际检测任务与相机，采样在线独立于合规判定，显示实时 fps 和累计安装确认。螺丝模型的 `positionsConfirmed` 包含 EMPTY；安装进度改用 `operationsCompleted + preassembledConfirmed`。停止后显示“上轮”记录；只有待复核归档时，趋势明确显示待复核轮次数。

初次联调通过 `/terminal?standalone=1` 隐藏工件区；后续已在默认 `/terminal` 中完整删除工件关联，见文末更新。过期定时器同步更新在线徽标和年龄，避免顶部仍显示在线而下方已经过期。

此前 run-02 真实六路测试：目标各 4 fps，实际 2.82–3.05 fps，采样到后端返回 P95 为 390–421 ms。每路均取得 11/11 累计视觉确认，但视频约 14–15 秒出现超过配置的采样间隔，六路最终均 REVIEW。大屏显示 6 轮、0 PASS、0 FAIL、6 待判定。没有放宽采样保护来制造 PASS。

83 次真实快照、498 次卡片、142 次终端结果核对无差异，安装进度额外回查原始帧结果。独立 API 重启后归档仍可显示，工件表为空。此前 run-01 为 5 PASS、1 REVIEW，原始记录保留。六路吞吐、预览 409 与偶发过期仍未解决，不代表系统性能验收通过。

验证：后端回归 57 passed / 6 skipped（PostgreSQL 无测试库）；修正安装口径后专项 5 passed；Board 与终端浏览器回归通过；归档重启真实页面检查通过。所有测试使用独立数据库。

完整报告及截图：`test-results/board-linkage-no-workpiece-2026-11-07/run-02/report.md`。复现脚本 `tools/evaluate-board-video.py` 新增 `--runs single|six|both`，默认 both。

## 后续：默认移除工件关联

按工厂没有工件号联动的要求，默认终端、后端检测接口及常驻采集器已完整移除工件关联依赖。历史数据保留，新检测按工位与轮次归档。后端回归 70 passed / 6 skipped；默认终端及 Board UI 回归通过。

新一轮真实六路测试实际 2.86–2.95 fps，采样到响应 P95 为 391–422 ms，最终 3 PASS、3 REVIEW，复核原因为采样间隔超限。83 次快照、498 次工位卡片、143 次终端结果核对无差异，工件表 0 条、独立归档 6 条。吞吐仍未达到每路 4 fps，未改模型及采样保护阈值。

新报告与截图：`test-results/workpiece-linkage-removed-2026-11-07/run-01/report.md`。
