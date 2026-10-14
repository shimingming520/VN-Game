/**
 * echarts 按需引入:core + 柱状/饼图 + 必要组件 + Canvas 渲染器
 * 页面统一 `import * as echarts from '@/utils/echarts'` 使用,避免全量打包
 */
import * as echarts from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  CanvasRenderer,
])

export * from 'echarts/core'
