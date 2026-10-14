<script setup>
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

/** 侧边栏菜单配置 */
const menuItems = [
  {
    key: '/home',
    label: '首页',
    icon: 'HomeOutlined',
  },
  {
    key: '/test',
    label: '测试页面',
    icon: 'ExperimentOutlined',
  },
]

/** 当前选中的菜单 key */
const selectedKeys = computed(() => [route.path])

/**
 * 菜单点击跳转
 * @param {{ key: string }} param - 菜单项
 */
function handleMenuClick({ key }) {
  router.push(key)
}
</script>

<template>
  <a-layout-sider class="app-sider" width="220">
    <a-menu
      mode="inline"
      theme="light"
      :selected-keys="selectedKeys"
      @click="handleMenuClick"
    >
      <a-menu-item v-for="item in menuItems" :key="item.key">
        <span>{{ item.label }}</span>
      </a-menu-item>
    </a-menu>
  </a-layout-sider>
</template>

<style scoped>
.app-sider {
  background: #fff;
  border-right: 1px solid #f0f0f0;
}
</style>
