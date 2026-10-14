<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'

const route = useRoute()
const router = useRouter()

// 与 demo 完全一致的五项菜单(id/icon/label);素材默认进 character 分类
const navItems = [
  { id: 'home', icon: 'grid', label: '首页', path: '/home', match: ['/home'] },
  { id: 'warehouse', icon: 'archive', label: '游戏仓库', path: '/library', match: ['/library'] },
  { id: 'create', icon: 'spark', label: '创作', path: '/create', match: ['/create'] },
  { id: 'assets', icon: 'layers', label: '资产', path: '/my-assets', match: ['/my-assets'] },
  { id: 'materials', icon: 'image', label: '素材', path: '/assets/character', match: ['/assets'] },
]

const activeId = computed(() => navItems.find((n) => n.match.some((p) => route.path.startsWith(p)))?.id)

function go(item) {
  if (!route.path.startsWith(item.match[0])) router.push(item.path)
}

function social(label) {
  message.info(`${label} 社区入口待接入`)
}

</script>

<template>
  <aside class="side-nav">
    <div class="side-links">
      <button
        v-for="item in navItems"
        :key="item.id"
        class="side-link"
        :class="{ selected: item.id === activeId }"
        :aria-label="item.label"
        type="button"
        @click="go(item)"
      >
        <svg
          :width="31"
          :height="31"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <template v-if="item.icon === 'grid'">
            <rect x="3" y="3" width="7" height="7" rx="1" />
            <rect x="14" y="3" width="7" height="7" rx="1" />
            <rect x="3" y="14" width="7" height="7" rx="1" />
            <rect x="14" y="14" width="7" height="7" rx="1" />
          </template>
          <template v-else-if="item.icon === 'archive'">
            <path d="M3 7.5h18v12H3z" />
            <path d="M2 4h20v3.5H2zM9 12h6" />
          </template>
          <template v-else-if="item.icon === 'spark'">
            <path d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8L12 2Z" />
            <path d="m19 16 .8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16Z" />
          </template>
          <template v-else-if="item.icon === 'layers'">
            <path d="m12 3 9 5-9 5-9-5 9-5Z" />
            <path d="m3 12 9 5 9-5M3 16l9 5 9-5" />
          </template>
          <template v-else-if="item.icon === 'image'">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <circle cx="8.5" cy="9" r="1.5" />
            <path d="m21 15-4.5-4.5L7 20" />
          </template>
        </svg>
        <span>{{ item.label }}</span>
      </button>
    </div>

    <div class="side-social">
      <button class="utility-link social-link" aria-label="X" type="button" @click="social('X')">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M5 4 18 20M18 4 5 20" />
        </svg>
        <!-- 悬停提示暂不需要,保留待恢复 -->
        <!-- <span>X 社区</span> -->
      </button>
      <button class="utility-link social-link" aria-label="Discord" type="button" @click="social('Discord')">
        <svg class="discord-icon" width="26" height="26" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M7.1 7.4A11.1 11.1 0 0 1 10.5 6l.6 1.1a8.6 8.6 0 0 1 1.8 0l.6-1.1a11.1 11.1 0 0 1 3.4 1.4c1.3 2 1.8 4.4 1.5 6.8a10.5 10.5 0 0 1-3.1 1.7l-1-1.4" />
          <path d="M8.7 15.6a10.5 10.5 0 0 1-3.1-1.7c-.3-2.4.2-4.8 1.5-6.8" />
          <path d="M9.5 12.5h.01M14.5 12.5h.01" />
        </svg>
        <!-- 悬停提示暂不需要,保留待恢复 -->
        <!-- <span>Discord 社区</span> -->
      </button>
    </div>
  </aside>
</template>
