<script setup>
import { computed } from 'vue'
import {
  ArrowRightOutlined,
  CheckOutlined,
  DownOutlined,
  FullscreenOutlined,
  HistoryOutlined,
  ImportOutlined,
  MinusOutlined,
  PauseCircleOutlined,
  PictureOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  SlidersOutlined,
  TeamOutlined,
  UserOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons-vue'

defineOptions({ inheritAttrs: false })

const props = defineProps({ icon: { type: String, default: '' } })

const iconMap = {
  'mdi:account': UserOutlined,
  'mdi:account-multiple-outline': TeamOutlined,
  'mdi:arrow-right': ArrowRightOutlined,
  'mdi:check': CheckOutlined,
  'mdi:chevron-down': DownOutlined,
  'mdi:fit-to-page-outline': FullscreenOutlined,
  'mdi:history': HistoryOutlined,
  'mdi:image-area': PictureOutlined,
  'mdi:image-multiple-outline': PictureOutlined,
  'mdi:import': ImportOutlined,
  'mdi:minus': MinusOutlined,
  'mdi:movie-open-outline': VideoCameraOutlined,
  'mdi:play': PlayCircleOutlined,
  'mdi:play-circle-outline': PlayCircleOutlined,
  'mdi:pause': PauseCircleOutlined,
  'mdi:plus': PlusOutlined,
  'mdi:tune-variant': SlidersOutlined,
}

const component = computed(() => iconMap[props.icon] || PictureOutlined)
</script>

<template>
  <component :is="component" v-bind="$attrs" />
</template>
