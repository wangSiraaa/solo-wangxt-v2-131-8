<template>
  <div>
    <header class="header">
      <div>
        <h1>电能质量离线复核平台</h1>
        <small>不可变原始块 · 固定清单/标定/参数 · 谐波 / 对称分量 / 缺口诊断</small>
      </div>
      <span>{{ health.status }} / {{ health.object_store }}</span>
    </header>

    <div class="layout">
      <aside class="panel">
        <h2>录波清单</h2>
        <div
          v-for="item in manifests"
          :key="item.id"
          class="manifest-item"
          :class="{ active: selectedId === item.id }"
          @click="selectManifest(item.id)"
        >
          <h3>{{ item.name }}</h3>
          <div class="meta">
            <span class="badge" :class="item.status">{{ item.status }}</span>
            {{ item.expected_chunks.length }} 块 · {{ item.channel_set.join(', ') }}
          </div>
        </div>
      </aside>

      <main class="panel" v-if="manifest">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <h2 style="margin:0 0 4px">{{ manifest.name }}</h2>
            <div class="meta">
              digest {{ short(manifest.manifest_digest) }} · {{ manifest.start_time }} → {{ manifest.end_time }}
            </div>
          </div>
          <div>
            <button @click="finalize" :disabled="manifest.status !== 'open'">核对并完成</button>
            <button class="secondary" @click="refreshAll">刷新</button>
          </div>
        </div>

        <h3>块与缺口</h3>
        <GapChart :chunks="chunks" :manifest="manifest" :issues="issues" />

        <div v-for="issue in issues" :key="issue.id" class="issue" :class="issue.severity">
          <strong>[{{ issue.severity }}] {{ issue.code }}</strong> — {{ issue.message }}
        </div>

        <h3>波形（按固定采样率分段，不做插值拼接）</h3>
        <WaveformChart :preview="preview" :bookmarks="bookmarks" :focus="waveformFocus" />

        <h3>分析任务</h3>
        <div style="margin-bottom:10px">
          <label>固定标定版本：
            <select v-model="selectedCalibrationId" style="max-width:360px">
              <option v-for="c in calibrations" :key="c.id" :value="c.id">
                {{ short(c.id) }} · {{ c.status }} · {{ c.change_note || '初始版本' }}
              </option>
            </select>
          </label>
          <button @click="createTask" :disabled="!selectedCalibrationId">创建/入队</button>
        </div>
        <table>
          <thead><tr><th>任务</th><th>状态</th><th>标定</th><th>尝试</th><th>阶段</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="task in tasks" :key="task.id">
              <td>{{ short(task.id) }}</td>
              <td><span class="badge" :class="task.status">{{ task.status }}</span>
                <div v-if="task.cancellation_requested" class="meta">取消请求中</div></td>
              <td>{{ short(task.calibration_version_id) }}</td>
              <td>{{ task.attempts }}</td>
              <td class="meta">{{ Object.keys(task.stage_results || {}).join(' → ') }}</td>
              <td>
                <button @click="run(task.id)">同步执行</button>
                <button class="secondary" @click="retry(task.id)">重试</button>
                <button class="danger" @click="cancel(task.id)">取消</button>
              </td>
            </tr>
          </tbody>
        </table>

        <h3>报告</h3>
        <template v-if="reports.length">
          <div class="grid" style="margin-bottom:12px">
            <div class="metric"><span>状态</span><strong><span class="badge" :class="report.status">{{ report.status }}</span></strong></div>
            <div class="metric"><span>A 相 RMS</span><strong>{{ metric('Va')?.rms?.toFixed(4) ?? '—' }}</strong></div>
            <div class="metric"><span>A 相 THD</span><strong>{{ metric('Va')?.thd_percent?.toFixed(3) ?? '—' }}%</strong></div>
          </div>
          <p class="meta" v-if="report.review_reason">{{ report.review_reason }}</p>
          <SpectrumChart :report="report" />
          <SequenceTable :report="report" />
          <pre>{{ JSON.stringify(qualitySummary, null, 2) }}</pre>
        </template>
        <p v-else class="meta">尚无已发布报告。失败任务只保存阶段诊断，不会冒充完成。</p>

        <template v-if="reportId">
          <h3>报告书签（锚定原始块，可回到波形窗口）</h3>
          <div class="bookmark-form">
            <label>采样率段
              <select v-model.number="bookmarkForm.segment_index">
                <option v-for="(seg, i) in preview.segments || []" :key="i" :value="i">
                  #{{ i }} · {{ seg.sample_rate }} Hz · {{ (seg.end_seconds - seg.start_seconds).toFixed(4) }}s
                </option>
              </select>
            </label>
            <label>通道
              <select v-model="bookmarkForm.channel">
                <option v-for="c in manifest.channel_set" :key="c" :value="c">{{ c }}</option>
              </select>
            </label>
            <label>段内秒（0 ~ {{ segmentDuration(bookmarkForm.segment_index) }}）
              <input type="number" v-model.number="bookmarkForm.offset_seconds" step="0.000001" min="0" :max="segmentDuration(bookmarkForm.segment_index)">
            </label>
            <label>类型
              <select v-model="bookmarkForm.bookmark_type">
                <option v-for="(label, value) in TYPE_LABELS" :key="value" :value="value">{{ label }}</option>
              </select>
            </label>
            <label>署名 <input v-model.trim="bookmarkForm.author" placeholder="复核人"></label>
            <label>备注 <input v-model="bookmarkForm.note" placeholder="短时异常描述"></label>
            <button @click="createBookmark" :disabled="!bookmarkForm.author">添加书签</button>
          </div>
          <div v-if="bookmarkError" class="issue error"><strong>{{ bookmarkError }}</strong></div>
          <table v-if="bookmarks.length">
            <thead><tr><th>类型</th><th>通道</th><th>段 / 采样率</th><th>段内秒</th><th>显示秒</th><th>原始块</th><th>署名</th><th>备注</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="b in bookmarks" :key="b.id" :class="{ invalid: b.source_valid === false }">
                <td>{{ typeLabel(b.bookmark_type) }}</td>
                <td>{{ b.channel }}</td>
                <td>#{{ b.segment_index }} · {{ b.sample_rate }} Hz</td>
                <td>{{ b.offset_seconds.toFixed(6) }}</td>
                <td>{{ b.display_seconds.toFixed(6) }}</td>
                <td>块 {{ b.chunk_sequence }} · 样本 {{ b.sample_index }}</td>
                <td>{{ b.author }}</td>
                <td>{{ b.note }}</td>
                <td>
                  <button @click="jumpToBookmark(b)">定位</button>
                  <button class="danger" @click="removeBookmark(b)">删除</button>
                  <div v-if="b.source_valid === false" class="meta">来源失效：{{ b.source_error?.message }}</div>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="meta">尚无书签。书签只记录位置与备注，不修改原始块、计算指标或质量状态。</p>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { api } from './api'

const health = ref({ status: 'connecting', object_store: '-' })
const manifests = ref([])
const selectedId = ref(null)
const manifest = ref(null)
const chunks = ref([])
const issues = ref([])
const preview = ref({ segments: [] })
const calibrations = ref([])
const selectedCalibrationId = ref('')
const tasks = ref([])
const reports = ref([])
const reportId = ref(null)
const report = ref(null)
const timer = ref(null)
const bookmarks = ref([])
const bookmarkError = ref('')
const waveformFocus = ref(null)
const TYPE_LABELS = { anomaly: '异常', question: '疑问', note: '备注', follow_up: '待复查' }
const typeLabel = (value) => TYPE_LABELS[value] || value
const bookmarkForm = ref({
  segment_index: 0,
  channel: '',
  offset_seconds: 0,
  bookmark_type: 'anomaly',
  note: '',
  author: ''
})

const segmentDuration = (index) => {
  const segment = (preview.value.segments || [])[index]
  return segment ? +(segment.end_seconds - segment.start_seconds).toFixed(6) : 0
}

const parseError = (error) => {
  try {
    const data = JSON.parse(error.message)
    const detail = data?.detail
    if (typeof detail === 'string') return detail
    if (detail?.message) return `[${detail.code}] ${detail.message}`
    return error.message
  } catch { return String(error.message || error) }
}

const short = (value) => value ? `${String(value).slice(0, 8)}…` : '—'
const unwrap = async (promise) => {
  try { return await promise } catch (error) { console.warn(error); return null }
}
async function loadHealth() { health.value = await api.health() }
async function loadManifests() {
  manifests.value = await api.manifests()
  if (!selectedId.value && manifests.value.length) selectedId.value = manifests.value[0].id
}
async function refreshAll() {
  await loadHealth(); await loadManifests(); await loadDetail()
}
async function finalize() {
  try { await api.finalize(selectedId.value) } finally { await loadDetail() }
}
async function selectManifest(id) { selectedId.value = id; await loadDetail() }
async function loadDetail() {
  if (!selectedId.value) return
  manifest.value = await api.manifest(selectedId.value)
  const [chunkList, issueList, previewData, taskList, reportList] = await Promise.all([
    unwrap(api.chunks(selectedId.value)),
    unwrap(api.issues(selectedId.value)),
    unwrap(api.preview(selectedId.value, selectedCalibrationId.value)),
    unwrap(api.tasks(selectedId.value)),
    unwrap(api.reports(selectedId.value))
  ])
  chunks.value = chunkList || []
  issues.value = issueList || []
  preview.value = previewData || { segments: [] }
  tasks.value = taskList || []
  reports.value = reportList || []
  const chosen = reports.value.find((item) => item.status === 'published') || reports.value[0]
  reportId.value = chosen?.id || null
  report.value = chosen || null
  await loadBookmarks()
  const calList = await unwrap(api.calibrations(manifest.value.channel_set_hash))
  calibrations.value = calList || []
  if (!selectedCalibrationId.value) {
    selectedCalibrationId.value = calibrations.value.find((item) => item.status === 'active')?.id || calibrations.value[0]?.id || ''
  }
  if (!bookmarkForm.value.channel) {
    bookmarkForm.value.channel = manifest.value.channel_set[0] || ''
  }
}
async function loadBookmarks() {
  bookmarks.value = reportId.value ? (await unwrap(api.bookmarks(reportId.value))) || [] : []
}
async function createBookmark() {
  bookmarkError.value = ''
  try {
    const segment = (preview.value.segments || [])[bookmarkForm.value.segment_index]
    await api.createBookmark(reportId.value, { ...bookmarkForm.value, sample_rate: segment?.sample_rate ?? null })
    bookmarkForm.value.note = ''
    await loadBookmarks()
  } catch (error) { bookmarkError.value = parseError(error) }
}
async function jumpToBookmark(bookmark) {
  bookmarkError.value = ''
  try {
    // The locate call re-validates the frozen source; invalidated sources
    // surface their explicit error here instead of jumping anywhere.
    const location = await api.locateBookmark(bookmark.id)
    waveformFocus.value = { ...location, nonce: Date.now() }
  } catch (error) { bookmarkError.value = parseError(error) }
}
async function removeBookmark(bookmark) {
  bookmarkError.value = ''
  try { await api.deleteBookmark(bookmark.id) } catch (error) { bookmarkError.value = parseError(error) }
  await loadBookmarks()
}
async function createTask() {
  await api.createTask(selectedId.value, selectedCalibrationId.value)
  await loadDetail()
}
async function run(id) { await api.runTask(id); await loadDetail() }
async function retry(id) { await api.retryTask(id); await loadDetail() }
async function cancel(id) { await api.cancelTask(id); await loadDetail() }

const metric = (channel) => {
  const first = report.value?.result?.segments?.[0]?.channels?.[channel]
  return first || null
}
const qualitySummary = computed(() => {
  if (!report.value) return null
  return {
    status: report.value.result.quality_status,
    quality: report.value.result.quality,
    conventions: report.value.result.conventions,
    snapshot_digest: report.value.snapshot_digest
  }
})

const GapChart = {
  props: ['chunks', 'manifest', 'issues'],
  setup(props) {
    const el = ref(null)
    let chart = null
    const render = () => {
      if (!el.value || !props.manifest) return
      chart ||= echarts.init(el.value)
      const total = props.manifest.expected_chunks.length
      const received = new Map(props.chunks.map((chunk) => [chunk.sequence, chunk]))
      const data = props.manifest.expected_chunks.map((item) => {
        const chunk = received.get(item.sequence)
        return {
          value: [item.sequence, item.sequence + 1, chunk ? 1 : -1],
          itemStyle: { color: chunk ? '#1c9b61' : '#d8274f' }
        }
      })
      chart.setOption({
        title: { text: '绿色已到 / 红色缺块', textStyle: { fontSize: 13 } },
        grid: { left: 40, right: 20, top: 35, bottom: 35 },
        xAxis: { type: 'value', name: '块序号', min: 0, max: total },
        yAxis: { type: 'category', data: ['chunk'], show: false },
        tooltip: { formatter: (p) => p.value[2] > 0 ? `块 ${p.value[0]} 已到` : `块 ${p.value[0]} 缺失` },
        series: [{ type: 'custom', renderItem: (_params, api) => {
          const values = api.value(2)
          const start = api.coord([api.value(0), 0]); const end = api.coord([api.value(1), 1])
          return { type: 'rect', shape: { x: start[0], y: start[1], width: Math.max(2, end[0] - start[0] - 1), height: end[1] - start[1] }, style: { fill: values > 0 ? '#1c9b61' : '#d8274f' } }
        }, data }]
      }, true)
    }
    watch(() => [props.chunks, props.manifest, props.issues], render, { deep: true })
    const resize = () => chart?.resize()
    onMounted(() => { render(); window.addEventListener('resize', resize) })
    onUnmounted(() => window.removeEventListener('resize', resize))
    return { el, render, resize }
  },
  template: '<div ref="el" class="chart"></div>'
}

const WaveformChart = {
  props: ['preview', 'bookmarks', 'focus'],
  setup(props) {
    const el = ref(null)
    let chart
    const render = () => {
      if (!el.value) return
      chart ||= echarts.init(el.value)
      const series = []
      const axes = []
      ;(props.preview.segments || []).forEach((segment, segmentIndex) => {
        const marks = (props.bookmarks || []).filter((b) => b.segment_index === segmentIndex)
        segment.series.forEach((entry, entryIndex) => {
          series.push({
            type: 'line',
            name: `${entry.channel} @${segment.sample_rate}Hz`,
            showSymbol: false,
            sampling: 'lttb',
            data: entry.values.map((value, index) => [segment.start_seconds + segment.times[index], value]),
            xAxisIndex: segmentIndex,
            markLine: entryIndex === 0 && marks.length ? {
              symbol: 'none',
              animation: false,
              label: { formatter: '{b}', fontSize: 10 },
              lineStyle: { type: 'dashed', color: '#b4234a' },
              data: marks.map((b) => ({ name: `${b.bookmark_type}:${b.channel}`, xAxis: b.display_seconds }))
            } : undefined
          })
        })
        axes.push({ type: 'value', gridIndex: segmentIndex, name: 's', min: segment.start_seconds, max: segment.end_seconds })
      })
      chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { type: 'scroll', top: 0 },
        grid: (props.preview.segments || []).map((_, i) => ({ left: 55, right: 20, top: 35 + i * 245, height: 210 })),
        xAxis: axes,
        yAxis: (props.preview.segments || []).map(() => ({ type: 'value', name: '标定后' })),
        dataZoom: (props.preview.segments || []).map((_, i) => ({ type: 'inside', xAxisIndex: i })),
        series
      }, true)
    }
    watch(() => [props.preview, props.bookmarks], render, { deep: true })
    watch(() => props.focus, (focus) => {
      if (!focus || !chart) return
      const segment = (props.preview.segments || [])[focus.segment_index]
      if (!segment) return
      const span = segment.end_seconds - segment.start_seconds
      const half = Math.max(span * 0.05, 5 / segment.sample_rate)
      chart.dispatchAction({
        type: 'dataZoom',
        dataZoomIndex: focus.segment_index,
        startValue: Math.max(segment.start_seconds, focus.display_seconds - half),
        endValue: Math.min(segment.end_seconds, focus.display_seconds + half)
      })
      el.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
    const resize = () => chart?.resize()
    onMounted(() => { render(); window.addEventListener('resize', resize) })
    onUnmounted(() => window.removeEventListener('resize', resize))
    return { el, render, resize }
  },
  template: '<div ref="el" :style="{height: `${Math.max(280, (preview.segments||[]).length * 270)}px`}"></div>'
}

const SpectrumChart = {
  props: ['report'],
  setup(props) {
    const el = ref(null)
    let chart
    const render = () => {
      if (!el.value || !props.report) return
      chart ||= echarts.init(el.value)
      const segment = props.report.result.segments?.[0]
      const channels = segment ? Object.keys(segment.channels).slice(0, 3) : []
      const firstHarmonics = segment?.channels[channels[0]]?.windows?.[0]?.harmonics || []
      chart.setOption({
        title: { text: '各次谐波 RMS（固定标定版本）', textStyle: { fontSize: 14 } },
        tooltip: { trigger: 'axis' },
        legend: { data: channels, top: 25 },
        grid: { left: 55, right: 20, top: 70, bottom: 40 },
        xAxis: { type: 'category', name: '次数', data: firstHarmonics.map((h) => h.order) },
        yAxis: { type: 'value', name: 'RMS' },
        series: channels.map((channel) => ({
          name: channel, type: 'bar',
          data: (segment.channels[channel].windows[0].harmonics || []).map((h) => h.rms)
        }))
      }, true)
    }
    watch(() => props.report, render, { deep: true })
    const resize = () => chart?.resize()
    onMounted(() => { render(); window.addEventListener('resize', resize) })
    onUnmounted(() => window.removeEventListener('resize', resize))
    return { el, render, resize }
  },
  template: '<div ref="el" class="chart"></div>'
}

const SequenceTable = {
  props: ['report'],
  setup(props) {
    const rows = computed(() => {
      const segment = props.report?.result?.segments?.[0]
      const voltage = segment?.symmetrical_components?.voltage?.[0]
      if (!voltage || voltage.status !== 'ok') return []
      return ['positive', 'negative', 'zero'].map((name) => ({
        name,
        rms: voltage[`${name}_rms`],
        phase: voltage[`${name}_phase_deg`],
        phasor: `${voltage[`${name}_phasor`].real.toFixed(4)} ${voltage[`${name}_phasor`].imag >= 0 ? '+' : '−'} j${Math.abs(voltage[`${name}_phasor`].imag).toFixed(4)}`
      }))
    })
    return { rows }
  },
  template: `<table v-if="rows.length"><thead><tr><th>分量</th><th>RMS</th><th>相位°</th><th>峰值相量</th></tr></thead>
    <tbody><tr v-for="r in rows" :key="r.name"><td>{{r.name}}</td><td>{{r.rms.toFixed(5)}}</td><td>{{r.phase.toFixed(3)}}</td><td>{{r.phasor}}</td></tr></tbody></table>`
}

onMounted(async () => { await refreshAll(); timer.value = setInterval(loadDetail, 4000) })
onUnmounted(() => clearInterval(timer.value))
watch(selectedCalibrationId, () => loadDetail())
</script>
