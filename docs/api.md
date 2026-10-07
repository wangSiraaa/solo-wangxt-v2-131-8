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

## 报告波形书签

复核人员在波形上留标记，可回到原始数据。书签锚定 `(segment_index, offset_seconds)`，
服务端按报告冻结快照重放与预览一致的分段语义，解析出 `(chunk_sequence, sample_index)` 并绑定
`report_id + manifest_digest`。显示秒只是派生值，不作为定位依据，因此两个采样率段即使
显示秒相同也不会串到错误块。书签不改原始块、计算指标或质量状态。

### `POST /reports/{report_id}/bookmarks`

```json
{
  "segment_index": 1,
  "channel": "Va",
  "offset_seconds": 0.01,
  "sample_rate": 7000,
  "bookmark_type": "anomaly",
  "note": "短时跌落",
  "author": "张工"
}
```

- `sample_rate` 为可选守卫：与冻结段采样率不一致返回 422 `bookmark_sample_rate_mismatch`，防止过期视图错锚。
- `bookmark_type`：`anomaly | question | note | follow_up`。
- 显式错误（均带 `detail.code`）：`bookmark_segment_invalid`、`bookmark_channel_invalid`、
  `bookmark_time_out_of_range`（越界时间）、`bookmark_source_invalid`（409，来源已失效）、
  `report_not_found`（404）。

### `GET /reports/{report_id}/bookmarks`

按显示秒排序返回书签，每条带 `source_valid` 与 `source_error`；失效来源显式标注而不是静默丢弃。

### `GET /waveform-bookmarks/{id}/locate`

解析回预览窗口与原始块：`segment_index`、`display_seconds`、`segment_display_start/end`、
`chunk_sequence`、`sample_index`、`chunk_object_key`、`chunk_sha256`。
来源失效返回 409 `bookmark_source_invalid`，`reason` 区分
`report_missing | manifest_missing | manifest_digest_changed | chunk_missing | chunk_digest_changed`。

### `DELETE /waveform-bookmarks/{id}`

只删除书签行（204）；报告、指标、质量状态、块元数据与原始对象均不受影响。
