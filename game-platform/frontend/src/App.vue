<script setup>
import { theme } from 'ant-design-vue'
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'

dayjs.locale('zh-cn')

// 龙族主题:ant 暗色算法 + 龙金主色,业务页组件自动继承
const dragonTheme = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: '#e8b64c',
    colorInfo: '#35d6d0',
    colorSuccess: '#3ddc97',
    colorWarning: '#f0a92e',
    colorError: '#f2576b',
    colorBgBase: '#0a0d14',
    colorTextBase: '#eef1f8',
    borderRadius: 10,
    fontSize: 14,
  },
}
</script>

<template>
  <a-config-provider :theme="dragonTheme" :locale="zhCN">
    <router-view />
  </a-config-provider>
</template>
