<script setup>
import { computed, onMounted, ref } from 'vue'
import { getLibraryGamesApi } from '@/api'

const loading = ref(false)
const games = ref([])
const keyword = ref('')

/**
 * 类型页签:本版所有游戏都是视觉小说,后端暂无 game_type 字段,
 * 缺省即按 novel 归类;等后端补字段后只需让 typeOf 读该字段。
 */
const TYPE_TABS = [
  { value: 'all', label: '全部' },
  { value: 'novel', label: '视觉小说' },
  { value: 'interactive', label: '互动影游', disabled: true },
]
const activeType = ref('all')

function selectType(type) {
  if (type.disabled) return
  activeType.value = type.value
}

function typeOf(g) {
  return g.game_type || 'novel'
}

// 内嵌游戏播放器(iframe 全屏浮层,不跳转新页)
const playing = ref(null)
function openGame(g) {
  if (g?.url) playing.value = g
}
function closeGame() {
  playing.value = null
}

async function fetchGames() {
  loading.value = true
  try {
    const res = await getLibraryGamesApi()
    games.value = res.items || []
  } finally {
    loading.value = false
  }
}

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  return games.value.filter((g) => {
    if (activeType.value !== 'all' && typeOf(g) !== activeType.value) return false
    if (!k) return true
    return g.name.toLowerCase().includes(k) || (g.description || '').toLowerCase().includes(k)
  })
})

function initial(name) {
  return (name || '?').slice(0, 1).toUpperCase()
}

const emptyText = computed(() => {
  if (keyword.value.trim()) return '没有匹配的作品'
  if (activeType.value === 'interactive') return '暂无互动影游'
  return '暂无游戏,请等待中台分发'
})

onMounted(fetchGames)
</script>

<template>
  <section class="warehouse-section">
    <div class="warehouse-top">
      <h2>游戏仓库</h2>
    </div>

    <div class="warehouse-controls-row">
      <div class="type-tabs">
        <button
          v-for="t in TYPE_TABS"
          :key="t.value"
          :class="{ active: activeType === t.value }"
          :disabled="t.disabled"
          :title="t.disabled ? '暂未开放' : ''"
          type="button"
          @click="selectType(t)"
        >
          {{ t.label }}{{ t.disabled ? '（暂未开放）' : '' }}
        </button>
      </div>
      <label class="warehouse-search">
        <input v-model="keyword" placeholder="请输入作者或作品名称" />
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" />
        </svg>
      </label>
    </div>

    <a-spin :spinning="loading">
      <div v-if="filtered.length" class="game-grid">
        <article v-for="g in filtered" :key="g.id" class="game-card" tabindex="0">
          <div class="game-art">
            <img v-if="g.cover" class="game-cover" :src="g.cover" :alt="`《${g.name}》封面`" />
            <button
              v-if="g.url"
              type="button"
              class="enter-cover-action"
              @click.stop="openGame(g)"
            >
              进入游戏
            </button>
          </div>
          <div class="game-details-panel">
            <div class="creator-avatar">{{ initial(g.name) }}</div>
            <div class="game-details-copy">
              <h4>《{{ g.name }}》</h4>
              <div class="game-meta-line">
                <span>{{ g.description || '暂无简介' }}</span>
              </div>
            </div>
          </div>
        </article>
      </div>
      <div v-else-if="!loading" class="empty-games">{{ emptyText }}</div>
    </a-spin>

    <!-- 内嵌游戏播放器:iframe 全屏浮层 -->
    <Teleport to="body">
      <div v-if="playing" class="game-player-overlay" @click.self="closeGame">
        <div class="game-player-frame">
          <div class="game-player-bar">
            <span class="game-player-title">《{{ playing.name }}》</span>
            <button type="button" class="game-player-close" aria-label="关闭游戏" @click="closeGame">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="m6 6 12 12M18 6 6 18" />
              </svg>
            </button>
          </div>
          <iframe
            :src="playing.url"
            class="game-player-iframe"
            title="游戏"
            allow="autoplay; fullscreen"
            allowfullscreen
          />
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.game-player-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(4, 5, 8, 0.82);
  backdrop-filter: blur(4px);
}
.game-player-frame {
  display: flex;
  flex-direction: column;
  width: min(100%, 1180px);
  height: min(100%, 760px);
  overflow: hidden;
  border: 1px solid rgba(150, 165, 231, 0.22);
  border-radius: 14px;
  background: #0d101a;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}
.game-player-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid rgba(150, 165, 231, 0.16);
  background: #101116;
}
.game-player-title {
  color: #f1f3f8;
  font-size: 15px;
  font-weight: 500;
}
.game-player-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: transparent;
  color: #9aa0b0;
  transition: color 0.15s ease, background 0.15s ease;
}
.game-player-close:hover {
  color: #f1f3f8;
  background: rgba(142, 159, 255, 0.12);
}
.game-player-iframe {
  flex: 1;
  width: 100%;
  border: 0;
  background: #000;
}

.type-tabs button:disabled {
  cursor: not-allowed;
  opacity: 0.42;
  filter: none;
}
</style>
