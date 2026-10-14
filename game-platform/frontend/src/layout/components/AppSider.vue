<script setup>
import { h } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MenuIcon from './MenuIcon.vue'

const route = useRoute()
const router = useRouter()

/** 菜单图标渲染:统一走 MenuIcon(纯路径 SVG,颜色随菜单态) */
const icon = (name) => () => h(MenuIcon, { name })

/** 侧边栏菜单配置(a-menu items 写法,children = 子菜单) */
const menuItems = [
  { key: '/tasks', label: '任务日志', icon: icon('task') },
  { key: '/usage', label: 'Token用量', icon: icon('usage') },
  {
    key: 'pipelines',
    label: '管线管理',
    icon: icon('pipeline'),
    children: [
      { key: '/pipelines/novel', label: '视觉小说' },
      { key: '/pipelines/film', label: '影游(敬请期待)', disabled: true },
    ],
  },
  {
    key: 'creation-test',
    label: '创作测试',
    icon: icon('test'),
    children: [
      { key: '/creation-test/novel', label: '视觉小说' },
      { key: '/creation-test/film', label: '影游(敬请期待)', disabled: true },
    ],
  },
  {
    key: 'games',
    label: '游戏管理',
    icon: icon('game'),
    children: [
      { key: '/games/novel', label: '视觉小说' },
      { key: '/games/promo', label: '创作推广' },
    ],
  },
  {
    key: 'assets',
    label: '素材库',
    icon: icon('material'),
    children: [
      { key: '/assets/character', label: '角色' },
      { key: '/assets/scene', label: '场景' },
      { key: '/assets/bgm', label: 'BGM' },
      { key: '/assets/sfx', label: '音效' },
      { key: '/assets/video', label: '视频(敬请期待)', disabled: true },
    ],
  },
]

/** 当前选中的菜单 key */
const selectedKeys = computed(() => [route.path])

/** 带子菜单的路由前缀 → 父菜单 key,进入该路由时自动展开 */
const SUBMENU_PREFIXES = {
  '/assets': 'assets',
  '/games': 'games',
  '/pipelines': 'pipelines',
  '/creation-test': 'creation-test',
}

const openKeys = ref(
  Object.entries(SUBMENU_PREFIXES)
    .filter(([prefix]) => route.path.startsWith(prefix))
    .map(([, key]) => key),
)

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
      class="app-sider__menu"
      mode="inline"
      theme="dark"
      :items="menuItems"
      :selected-keys="selectedKeys"
      v-model:open-keys="openKeys"
      @click="handleMenuClick"
    />
    <div class="app-sider__seal">
      <img src="/dragon.svg" alt="" />
    </div>
  </a-layout-sider>
</template>

<style scoped>
.app-sider {
  position: relative;
  background: linear-gradient(180deg, #101623 0%, #0b0f18 100%) !important;
  border-right: 1px solid var(--dr-border);
}
/* 右侧一道金→青竖向流光,与顶栏横向流光成直角呼应 */
.app-sider::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  right: -1px;
  width: 1px;
  background: linear-gradient(180deg, transparent, rgba(232, 182, 76, 0.5) 22%, rgba(212, 35, 122, 0.4) 55%, rgba(53, 214, 208, 0.45) 82%, transparent);
  pointer-events: none;
}

/* 菜单深色化 + 金色选中态 */
.app-sider__menu {
  padding-top: 10px;
  background: transparent !important;
  border-inline-end: none !important;
}
.app-sider :deep(.ant-menu-item),
.app-sider :deep(.ant-menu-submenu-title) {
  margin: 4px 10px !important;
  width: calc(100% - 20px) !important;
  border-radius: 9px;
  color: var(--dr-text-2) !important;
  transition: all 0.18s ease;
}
.app-sider :deep(.ant-menu-item:hover),
.app-sider :deep(.ant-menu-submenu-title:hover) {
  background: rgba(53, 214, 208, 0.08) !important;
  color: var(--dr-cyan-2) !important;
}
/* 选中项:金色渐变底 + 左侧竖条 */
.app-sider :deep(.ant-menu-item-selected) {
  position: relative;
  border: 1px solid rgba(232, 182, 76, 0.3);
  background: linear-gradient(90deg, rgba(232, 182, 76, 0.2), rgba(232, 182, 76, 0.04)) !important;
  color: var(--dr-gold-2) !important;
  font-family: 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'SimSun', serif;
  font-weight: 700;
  letter-spacing: 1px;
}
/* 选中项图标跟着发金光 */
.app-sider :deep(.ant-menu-item-selected .ant-menu-item-icon) {
  filter: drop-shadow(0 0 5px rgba(232, 182, 76, 0.75));
}
.app-sider :deep(.ant-menu-item-selected)::before {
  content: '';
  position: absolute;
  left: 0;
  top: 18%;
  bottom: 18%;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--dr-grad-gold);
  box-shadow: 0 0 8px rgba(232, 182, 76, 0.7);
}
.app-sider :deep(.ant-menu-sub) {
  background: transparent !important;
}
.app-sider :deep(.ant-menu-item-disabled) {
  color: var(--dr-text-3) !important;
}

/* 底部龙纹印记(装饰) */
.app-sider__seal {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 18px;
  display: flex;
  justify-content: center;
  pointer-events: none;
}
.app-sider__seal img {
  width: 74px;
  height: 74px;
  opacity: 0.07;
  filter: hue-rotate(285deg) saturate(1.4) brightness(1.6);
}
</style>
