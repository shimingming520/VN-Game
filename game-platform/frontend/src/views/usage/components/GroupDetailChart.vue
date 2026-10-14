<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import * as echarts from '@/utils/echarts'

/**
 * 单管线专属图:四指标(total_tokens/图片数/TTS字数/音乐数)横向条形,
 * 底色条为全部管线合计,彩色条为该管线值,右侧标数值+占比
 * @prop {Object} row - 该管线的 summary 行
 * @prop {Array} allRows - 全部管线 summary 行(算占比用)
 */
const props = defineProps({
  row: { type: Object, default: null },
  allRows: { type: Array, default: () => [] },
})

const chartRef = ref(null)
let chart = null

const METRICS = [
  { key: 'total_tokens', label: 'Token 用量', color: '#e8b64c' },
  { key: 'image_count', label: '图片数', color: '#35d6d0' },
  { key: 'tts_chars', label: 'TTS 字数', color: '#d4237a' },
  { key: 'music_count', label: '音乐数', color: '#7ceee9' },
]

function render() {
  if (!chart || !props.row) return

  const totals = {}
  for (const m of METRICS) {
    totals[m.key] = props.allRows.reduce((acc, r) => acc + (r[m.key] || 0), 0)
  }

  const labels = METRICS.map((m) => m.label)
  // 该管线占比(%),全部管线为 0 时记 0
  const pctData = METRICS.map((m, i) => {
    const total = totals[m.key]
    const value = props.row[m.key] || 0
    const pct = total ? (value / total) * 100 : 0
    return {
      value: pct,
      itemStyle: { color: METRICS[i].color, borderRadius: [0, 8, 8, 0] },
      metricValue: value,
      metricPct: pct,
    }
  })

  chart.setOption(
    {
      title: {
        text: `${props.row.group_name} · 四项指标(占全部管线比例)`,
        left: 'center',
        top: 6,
        textStyle: { fontSize: 14, color: '#f5d089', fontFamily: 'Noto Serif SC, serif' },
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'none' },
        backgroundColor: 'rgba(15,20,32,0.96)',
        borderColor: '#26304a',
        textStyle: { color: '#eef1f8' },
        formatter: (params) => {
          const p = params.find((x) => x.seriesName === 'value') || params[0]
          const d = pctData[p.dataIndex]
          return `${labels[p.dataIndex]}<br/><b>${d.metricValue.toLocaleString()}</b>(占全部管线 ${d.metricPct.toFixed(1)}%)`
        },
      },
      grid: { left: 90, right: 130, top: 48, bottom: 20 },
      xAxis: { type: 'value', max: 100, show: false },
      yAxis: {
        type: 'category',
        data: labels,
        inverse: true,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { fontSize: 13, color: '#a6b0c3' },
      },
      series: [
        {
          // 底色条(100%)
          name: 'bg',
          type: 'bar',
          barGap: '-100%',
          barWidth: 22,
          data: METRICS.map(() => 100),
          itemStyle: { color: 'rgba(255,255,255,0.06)', borderRadius: 8 },
          tooltip: { show: false },
          silent: true,
        },
        {
          name: 'value',
          type: 'bar',
          barWidth: 22,
          data: pctData,
          label: {
            show: true,
            position: 'right',
            distance: 8,
            formatter: (p) =>
              `${pctData[p.dataIndex].metricValue.toLocaleString()}  ${pctData[p.dataIndex].metricPct.toFixed(1)}%`,
            color: '#a6b0c3',
            fontSize: 12,
          },
        },
      ],
    },
    true,
  )
}

function handleResize() {
  chart?.resize()
}

onMounted(() => {
  chart = echarts.init(chartRef.value)
  render()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})

watch(() => [props.row, props.allRows], render, { deep: true })
</script>

<template>
  <div ref="chartRef" class="group-detail-chart" />
</template>

<style scoped>
.group-detail-chart {
  width: 100%;
  height: 300px;
}
</style>
