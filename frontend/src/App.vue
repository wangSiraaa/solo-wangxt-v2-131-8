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
        <p class="meta">点击波形可在该采样率段的该通道、该段内秒位置留下书签；两个采样率段相同显示秒数互不串段。</p>
        <WaveformChart
          ref="waveformRef"
          :preview="preview"
          :bookmarks="bookmarks"
          @mark="openBookmarkForm"
        />

        <div v-if="report" class="bookmarks">
          <div class="bookmark-head">
            <h3>报告波形书签</h3>
            <button class="secondary" @click="loadBookmarks">刷新书签</button>
          </div>
          <div v-if="bookmarkDraft" class="bookmark-form">
            <div class="meta">
              段 {{ bookmarkDraft.segment_index }}（{{ bookmarkDraft.sample_rate }} Hz）·
              通道 {{ bookmarkDraft.channel }} · 段内 {{ bookmarkDraft.offset_seconds.toFixed(6) }}s
              <span v-if="bookmarkDraft.source">({{ bookmarkDraft.source }})</span>
            </div>
            <div class="grid">
              <label>类型
                <select v-model="bookmarkDraft.bookmark_type">
                  <option v-for="t in bookmarkTypes" :key="t.value" :value="t.value">{{ t.label }}</option>
                </select>
              </label>
              <label>署名
                <input v-model="bookmarkDraft.author" maxlength="128" placeholder="复核人" />
              </label>
            </div>
            <label>备注
              <textarea v-model="bookmarkDraft.note" rows="2" maxlength="4000" placeholder="异常现象、怀疑原因……"></textarea>
            </label>
            <div>
              <button @click="saveBookmark" :disabled="!bookmarkDraft.note.trim() || !bookmarkDraft.author.trim()">保存书签</button>
              <button class="secondary" @click="bookmarkDraft = null">取消</button>
              <span v-if="bookmarkError" class="issue error">{{ bookmarkError }}</span>
            </div>
          </div>
          <p v-else class="meta">在上方波形点击取点，或<a href="#" @click.prevent="openBookmarkForm()">手工填写</a>。书签只绑定报告 ID / 清单摘要 / 原始块序号，不改动原始块、指标与质量状态。</p>
          <table v-if="bookmarks.length">
            <thead><tr><th>类型</th><th>段/采样率</th><th>通道</th><th>段内秒</th><th>原始块</th><th>备注</th><th>署名</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="b in bookmarks" :key="b.id" class="bookmark-row" @click="focusBookmark(b)">
                <td><span class="badge bookmark-type" :class="b.bookmark_type">{{ bookmarkTypeLabel(b.bookmark_type) }}</span></td>
                <td>#{{ b.segment_index }} · {{ b.sample_rate }} Hz</td>
                <td>{{ b.channel }}</td>
                <td>{{ b.offset_seconds.toFixed(6) }}</td>
                <td class="meta">seq {{ b.chunk_sequence }} +{{ b.chunk_sample_offset }}</td>
                <td>{{ b.note }}</td>
                <td>{{ b.author }}</td>
                <td @click.stop>
                  <button class="danger" @click="removeBookmark(b.id)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

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
const bookmarks = ref([])
const bookmarkDraft = ref(null)
const bookmarkError = ref('')
const waveformRef = ref(null)
const timer = ref(null)

const bookmarkTypes = [
  { value: 'anomaly', label: '异常' },
  { value: 'question', label: '疑问' },
  { value: 'note', label: '备注' }
]
const bookmarkTypeLabel = (value) => bookmarkTypes.find((t) => t.value === value)?.label || value

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
}
async function createTask() {
  await api.createTask(selectedId.value, selectedCalibrationId.value)
  await loadDetail()
}
async function run(id) { await api.runTask(id); await loadDetail() }
async function retry(id) { await api.retryTask(id); await loadDetail() }
async function cancel(id) { await api.cancelTask(id); await loadDetail() }

async function loadBookmarks() {
  if (!reportId.value) { bookmarks.value = []; return }
  bookmarks.value = (await unwrap(api.bookmarks(reportId.value))) || []
}

function openBookmarkForm(locator) {
  if (!report.value) {
    bookmarkError.value = '请先选择一份已保存报告再添加书签'
    return
  }
  bookmarkError.value = ''
  if (locator) {
    bookmarkDraft.value = {
      segment_index: locator.segment_index,
      sample_rate: locator.sample_rate,
      channel: locator.channel,
      offset_seconds: locator.offset_seconds,
      bookmark_type: 'anomaly',
      note: '',
      author: '',
      source: '波形点击'
    }
  } else {
    const first = preview.value.segments?.[0]
    bookmarkDraft.value = {
      segment_index: 0,
      sample_rate: first?.sample_rate || 0,
      channel: first?.channels?.[0] || preview.value.channels?.[0] || '',
      offset_seconds: 0,
      bookmark_type: 'anomaly',
      note: '',
      author: '',
      source: '手工填写'
    }
  }
}

async function saveBookmark() {
  const draft = bookmarkDraft.value
  bookmarkError.value = ''
  try {
    await api.addBookmark(reportId.value, {
      segment_index: draft.segment_index,
      channel: draft.channel,
      offset_seconds: draft.offset_seconds,
      bookmark_type: draft.bookmark_type,
      note: draft.note,
      author: draft.author
    })
    bookmarkDraft.value = null
    await loadBookmarks()
  } catch (error) {
    bookmarkError.value = formatError(error)
  }
}

async function removeBookmark(id) {
  try {
    await api.deleteBookmark(reportId.value, id)
    await loadBookmarks()
  } catch (error) {
    bookmarkError.value = formatError(error)
  }
}

async function focusBookmark(b) {
  bookmarkError.value = ''
  // Always re-resolve server-side: a bookmark whose raw block disappeared or
  // changed must surface an explicit error instead of jumping to a wrong panel.
  let resolved
  try {
    resolved = await api.resolveBookmark(b.id)
  } catch (error) {
    bookmarkError.value = `书签 ${b.bookmark_type}（${b.note}）已失效：${formatError(error)}`
    return
  }
  if (!preview.value.segments?.some((s) => s.index === resolved.segment_index)) {
    await loadDetail()
  }
  waveformRef.value?.focus({
    segmentIndex: resolved.segment_index,
    channel: resolved.channel,
    offsetSeconds: resolved.offset_seconds
  })
}

function formatError(error) {
  try {
    const parsed = JSON.parse(String(error.message).replace(/^Error: /, ''))
    if (parsed?.detail?.message) return parsed.detail.message
    if (parsed?.detail?.code) return parsed.detail.code
    if (parsed?.detail && typeof parsed.detail === 'string') return parsed.detail
  } catch (_) { /* fall through */ }
  return String(error.message || error)
}

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
  props: ['preview', 'bookmarks'],
  emits: ['mark'],
  setup(props, { emit, expose }) {
    const el = ref(null)
    let chart
    // Zoom state survives preview refreshes so a located panel does not reset.
    let zoomBySegment = new Map()

    const gridRects = () => {
      // Official grid coordinate-system rectangles; one per segment panel.
      const component = chart.getModel().getComponent('grid')
      const coordMap = component?.coordSysMap || {}
      return Object.values(coordMap).map((grid) => grid.getRect())
    }

    const render = () => {
      if (!el.value) return
      chart ||= echarts.init(el.value)
      const zr = chart.getZr()
      zr.off('click')
      const series = []
      const axes = []
      const segments = props.preview.segments || []
      segments.forEach((segment) => {
        segment.series.forEach((entry) => {
          series.push({
            type: 'line',
            name: `${entry.channel} @${segment.sample_rate}Hz`,
            showSymbol: false,
            sampling: 'lttb',
            // times are local to the segment (0..duration); no global cursor.
            data: entry.values.map((value, index) => [segment.times[index], value]),
            xAxisIndex: segment.index
          })
        })
        // Bookmark pins live above the waveform.
        const marks = (props.bookmarks || []).filter(
          (b) => b.segment_index === segment.index && segment.channels.includes(b.channel)
        )
        series.push({
          type: 'scatter',
          name: `书签 #${segment.index}`,
          symbol: 'pin',
          symbolSize: 22,
          xAxisIndex: segment.index,
          yAxisIndex: segment.index,
          itemStyle: { color: '#d8274f' },
          z: 5,
          silent: true,
          data: marks.map((b) => [b.offset_seconds, 0])
        })
        axes.push({ type: 'value', gridIndex: segment.index, name: 's (段内)', min: 0, max: segment.end_seconds })
      })
      chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { show: false },
        grid: segments.map((_, i) => ({ left: 55, right: 20, top: 35 + i * 245, height: 210 })),
        xAxis: axes,
        yAxis: segments.map(() => ({ type: 'value', name: '标定后' })),
        dataZoom: segments.map((segment, i) => {
          const saved = zoomBySegment.get(i)
          return { type: 'inside', xAxisIndex: i, start: saved?.start ?? 0, end: saved?.end ?? 100 }
        }),
        series
      }, true)

      chart.getZr().on('click', (event) => {
        const point = [event.offsetX, event.offsetY]
        const rects = gridRects()
        let segmentIndex = -1
        rects.forEach((rect, i) => {
          if (point[0] >= rect.x && point[0] <= rect.x + rect.width &&
              point[1] >= rect.y && point[1] <= rect.y + rect.height) segmentIndex = i
        })
        const segment = segments[segmentIndex]
        if (!segment) return
        const [xSeconds] = chart.convertFromPixel({ xAxisIndex: segment.index }, point)
        if (!(xSeconds >= 0 && xSeconds <= segment.end_seconds)) return
        // Pick the channel whose waveform is vertically closest at this time.
        let channel = segment.channels[0]
        let bestDist = Infinity
        segment.series.forEach((entry) => {
          const values = entry.values
          const idx = Math.max(0, Math.min(values.length - 1, Math.round((xSeconds / segment.end_seconds) * (values.length - 1))))
          const pixel = chart.convertToPixel({ xAxisIndex: segment.index }, [xSeconds, values[idx]])
          const dist = Math.abs(pixel[1] - point[1])
          if (dist < bestDist) { bestDist = dist; channel = entry.channel }
        })
        emit('mark', { segment_index: segment.index, sample_rate: segment.sample_rate, channel, offset_seconds: xSeconds })
      })
    }

    watch(() => [props.preview, props.bookmarks], render, { deep: true })
    const resize = () => chart?.resize()
    onMounted(() => {
      render()
      window.addEventListener('resize', resize)
    })
    onUnmounted(() => {
      window.removeEventListener('resize', resize)
      chart?.getZr()?.off('click')
    })

    const focus = ({ segmentIndex, offsetSeconds }) => {
      const segments = props.preview.segments || []
      const segment = segments.find((s) => s.index === segmentIndex)
      if (!segment) return
      // Preserve current zoom states before the re-render.
      segments.forEach((s, i) => {
        const opt = chart.getOption().dataZoom?.[i]
        if (opt) zoomBySegment.set(i, { start: opt.start, end: opt.end })
      })
      const windowSeconds = Math.min(segment.end_seconds, Math.max(0.02, segment.end_seconds * 0.1))
      const center = Math.min(Math.max(offsetSeconds, windowSeconds / 2), segment.end_seconds - windowSeconds / 2)
      const startValue = Math.max(0, center - windowSeconds / 2)
      const startPct = (startValue / segment.end_seconds) * 100
      const endPct = Math.min(100, startPct + (windowSeconds / segment.end_seconds) * 100)
      zoomBySegment.set(segmentIndex, { start: startPct, end: endPct })
      render()
      chart?.resize()
      if (!el.value) return
      // Scroll the target segment's grid into view. Grids are stacked with a
      // fixed 245px pitch starting 35px inside the chart container.
      const chartTop = el.value.getBoundingClientRect().top + window.scrollY
      const gridTop = 35 + segmentIndex * 245
      window.scrollTo({ top: chartTop + gridTop - 40, behavior: 'smooth' })
    }
    expose({ focus })

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
