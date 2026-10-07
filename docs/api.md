# 主要 HTTP 接口

所有时间使用 ISO 8601。原始块编码为 `float32le-interleaved`。

## 清单与上传

### `POST /manifests`

创建不可变分块清单，请求示例：

```json
{
  "name": "line-20261001",
  "nominal_sample_rate": 6000,
  "expected_chunks": [
    {
      "sequence": 0,
      "sha256": "...64 hex...",
      "byte_offset": 0,
      "byte_length": 3600,
      "sample_count": 300,
      "sample_rate": 6000,
      "channels": ["Va", "Vb", "Vc"],
      "start_time": "2026-10-01T00:00:00",
      "end_time": "2026-10-01T00:00:00.049833333",
      "encoding": "float32le-interleaved"
    }
  ]
}
```

创建失败返回 422，`detail.issues` 中列出重叠、空洞、序号、时间或通道集合错误。

### `PUT /manifests/{manifest_id}/chunks/{sequence}/raw`

`multipart/form-data` 字段名：`chunk_file`。可乱序、重传。重复摘要返回已有块；摘要/长度错误返回 422。

### `POST /manifests/{manifest_id}/finalize`

执行最终核对。成功：

```json
{"completed": true, "already_completed": false, "warnings": []}
```

失败返回 422，issues 不做静默合并。并发时只有一个请求能完成首次转换。

### `GET /manifests/{id}/preview?calibration_version_id=...`

返回分段、采样率、降采样波形和缺口/质量 issue，供 Vue/ECharts 展示。后端使用磁盘 memmap 和降采样，避免把数 GB 原始波形全部放入响应内存。

每个固定采样率段使用**段内本地时间轴**：`start_seconds` 恒为 0，`end_seconds = samples/sample_rate`，`times` 也是 0 起始；段带 `index`（0..N-1）。因此两个不同采样率段即使显示相同秒数，也靠 `segment index` 区分，绝不跨段定位。前端点击波形得到的坐标就是 `(segment_index, channel, offset_seconds)`。

## 报告波形书签

书签是复核人在**已保存报告**波形上留下的定位标记，不修改原始块、分析指标或质量状态。持久化时绑定：

- `report_id`、`manifest_id`、`manifest_digest`（报告冻结清单摘要）；
- `segment_index`、`sample_rate`、`channel`、段内 `offset_seconds`；
- `sample_index`、`chunk_sequence`（原始块序号）、`chunk_sample_offset`、`chunk_sha256`、`object_key`；
- `bookmark_type`（`anomaly|question|note`）、`note`、`author`。

### `POST /reports/{id}/bookmarks`

```json
{
  "segment_index": 1,
  "channel": "Va",
  "offset_seconds": 0.02,
  "bookmark_type": "anomaly",
  "note": "短时脉冲，怀疑开关动作",
  "author": "reviewer-zhao"
}
```

定位只依据报告内冻结的 `fixed_snapshot.expected_chunks`，按采样率分段（与分析同一规则）。错误显式返回，不静默改指相邻块：

- 422 `bookmark_time_out_of_range`：时间不在该段 `[0, 总样本/采样率)` 内（段末瞬间属于下一段，会被拒绝）；
- 422 `bookmark_segment_not_found` / `bookmark_channel_not_found` / `invalid_bookmark_type`；
- 404 `report_not_found`；
- 409 `bookmark_source_stale`：冻结清单对应原始块缺失、摘要变化或对象存储中不可变对象丢失——此时不允许新建书签。

### `GET /reports/{id}/bookmarks` · `DELETE /reports/{id}/bookmarks/{bookmark_id}`

列出/删除书签。删除只删书签行，报告与原始录波都不受影响。

### `GET /bookmarks/{id}/resolution`

前端书签列表点击后调用，返回对应预览窗口坐标：

```json
{
  "manifest_id": "...", "segment_index": 1, "sample_rate": 7000.0,
  "segment_start_seconds": 0.0, "segment_end_seconds": 0.06,
  "offset_seconds": 0.02, "channel": "Va",
  "chunk_sequence": 1, "chunk_sample_offset": 140,
  "chunk_sha256": "...", "chunk_start_time": "...", "chunk_end_time": "...",
  "stale": false
}
```

若书签绑定的原始块已删除、摘要变化或对象缺失，返回 **409 `bookmark_source_stale`**（`detail.resolution` 含诊断信息），前端必须显式报错而不是跳到错误窗口。


## 标定

### `POST /calibrations`

```json
{
  "channel_set_hash": "manifest.channel_set_hash",
  "change_note": "CT 二次接线复核后修订",
  "coefficients": {
    "Va": {"gain": 1, "offset": 0, "phase_shift_rad": 0, "saturation_low": -450, "saturation_high": 450}
  }
}
```

新 active 版本会让使用旧 active 版本发布的报告进入 `needs_review`。

## 任务与报告

### `POST /analysis-tasks`

```json
{
  "manifest_id": "...",
  "calibration_version_id": "...",
  "params": {"fundamental_hz": 50, "cycles_per_window": 6, "max_harmonic": 15},
  "idempotency_key": "lab-job-123"
}
```

创建时冻结清单、标定和参数。若使用 Celery，提交后自动发送 `run_analysis`；本地测试可调用 `/run`。

### 执行、重试、取消

- `POST /analysis-tasks/{id}/run`
- `POST /analysis-tasks/{id}/retry`
- `POST /analysis-tasks/{id}/cancel`
- `POST /maintenance/recover-stale-tasks`

### `GET /reports/{id}`

报告状态：

- `published`：正常发布；
- `needs_review`：标定后来更新，需要复核；
- `diagnostic_failed`：分析有 error 阶段，保留诊断但不是完成报告。
