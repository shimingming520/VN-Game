<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import * as echarts from '@/utils/echarts'

/**
 * 各管线总览大饼图:等分(不按量占比),悬浮展示四指标,点击进管线专属图
 * @prop {Array} rows - usage/summary 返回的按管线汇总数组
 * @emits group-click - 点击某分片,参数为 group_name
 */
const props = defineProps({
  rows: { type: Array, default: () => [] },
})
const emit = defineEmits(['group-click'])

const chartRef = ref(null)
let chart = null

// 龙族配色:金 / 青 / 玫 三主色循环
const COLORS = ['#e8b64c', '#35d6d0', '#d4237a', '#7ceee9', '#f5d089', '#7262fd', '#65789b']

function render() {
  if (!chart) return
  // 等分:每组 value=1,大小不代表用量
  const data = props.rows.map((r, i) => ({
    name: r.group_name || '(未标记)',
    value: 1,
    itemStyle: { color: COLORS[i % COLORS.length] },
  }))
  chart.setOption(
    {
      title: {
        text: '管线用量总览',
        subtext: '悬浮查看指标 · 点击查看该管线专属图表',
        left: 'center',
        top: 8,
        textStyle: { color: '#f5d089', fontFamily: 'Noto Serif SC, serif', fontSize: 16 },
        subtextStyle: { color: '#6b7689', fontSize: 12 },
      },
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15,20,32,0.96)',
        borderColor: '#26304a',
        padding: [10, 14],
        textStyle: { color: '#eef1f8' },
        formatter: (p) => {
          const r = props.rows[p.dataIndex] || {}
          return [
            `<b style="font-size:14px">${p.name}</b>`,
            `total_tokens&nbsp;&nbsp;<b>${(r.total_tokens ?? 0).toLocaleString()}</b>`,
            `图片数&nbsp;&nbsp;<b>${(r.image_count ?? 0).toLocaleString()}</b>`,
            `TTS字数&nbsp;&nbsp;<b>${(r.tts_chars ?? 0).toLocaleString()}</b>`,
            `音乐数&nbsp;&nbsp;<b>${(r.music_count ?? 0).toLocaleString()}</b>`,
          ].join('<br/>')
        },
      },
      legend: {
        bottom: 4,
        left: 'center',
        icon: 'circle',
        textStyle: { color: '#a6b0c3' },
        inactiveColor: '#3a445c',
      },
      series: [
        {
          type: 'pie',
          radius: ['36%', '64%'],
          center: ['50%', '54%'],
          // 等分饼:关闭按值排序视觉暗示
          label: { formatter: '{b}', fontSize: 13, fontWeight: 500, color: '#a6b0c3' },
          labelLine: { length: 12, length2: 10, lineStyle: { color: '#3a445c' } },
          itemStyle: { borderRadius: 6, borderColor: '#131824', borderWidth: 3 },
          emphasis: {
            scaleSize: 8,
            label: { fontSize: 15, fontWeight: 'bold', color: '#f5d089' },
            itemStyle: { shadowBlur: 22, shadowColor: 'rgba(232,182,76,0.45)' },
          },
          data,
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
  chart.on('click', (params) => {
    const row = props.rows[params.dataIndex]
    if (row?.group_name) emit('group-click', row.group_name)
  })
  render()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})

watch(() => props.rows, render, { deep: true })
</script>

<template>
  <div ref="chartRef" class="group-chart" />
</template>

<style scoped>
.group-chart {
  width: 100%;
  height: 400px;
}
</style>
