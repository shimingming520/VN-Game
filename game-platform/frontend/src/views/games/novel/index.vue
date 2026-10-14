<script setup>
import { onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { getCmsGamesApi } from '@/api'

const loading = ref(false)
const games = ref([])
const total = ref(0)
const selectedIds = ref([])
const status = ref(0) // 0=全部 1=已下架 2=审核中 3=已发布
const pagination = reactive({ current: 1, pageSize: 24 })

const STATUS_OPTIONS = [
  { value: 0, label: '全部' },
  { value: 3, label: '已发布' },
  { value: 2, label: '审核中' },
  { value: 1, label: '已下架' },
]

async function fetchGames() {
  loading.value = true
  try {
    const res = await getCmsGamesApi({
      page: pagination.current,
      page_size: pagination.pageSize,
      status: status.value,
    })
    games.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  } finally {
    loading.value = false
  }
}

function handleStatusChange() {
  pagination.current = 1
  selectedIds.value = []
  fetchGames()
}

const playing = ref(null)
function openGame(g) {
  if (g?.link) playing.value = g
}

function toggleSelect(id) {
  const i = selectedIds.value.indexOf(id)
  if (i >= 0) selectedIds.value.splice(i, 1)
  else selectedIds.value.push(id)
}

function handlePageChange(page, pageSize) {
  pagination.current = page
  pagination.pageSize = pageSize
  fetchGames()
}

function handleDistribute() {
  message.info(`分发功能开发中(已选 ${selectedIds.value.length} 个游戏)`)
}

function handleRecall() {
  message.info(`撤回分发功能开发中(已选 ${selectedIds.value.length} 个游戏)`)
}

onMounted(fetchGames)
</script>

<template>
  <div class="games-novel">
    <div class="dr-page-head">
      <div>
        <div class="dr-page-title">视觉小说</div>
        <div class="dr-page-desc">CMS 已上架作品 · 勾选后可批量分发</div>
      </div>
    </div>

    <a-card :bordered="false">
      <div class="games-novel__toolbar">
        <div class="games-novel__left">
          <a-select
            v-model:value="status"
            :options="STATUS_OPTIONS"
            style="width: 120px"
            @change="handleStatusChange"
          />
        </div>
        <a-space>
          <a-button
            type="primary"
            :disabled="selectedIds.length === 0"
            @click="handleDistribute"
          >
            分发{{ selectedIds.length ? `(${selectedIds.length})` : '' }}
          </a-button>
          <a-button
            :disabled="selectedIds.length === 0"
            @click="handleRecall"
          >
            撤回分发{{ selectedIds.length ? `(${selectedIds.length})` : '' }}
          </a-button>
        </a-space>
      </div>

      <a-spin :spinning="loading">
        <a-empty v-if="!games.length && !loading" description="暂无游戏" />

        <div v-else class="games-novel__grid">
          <a-card
            v-for="g in games"
            :key="g.id"
            class="game-card"
            :class="{ 'game-card--selected': selectedIds.includes(g.id) }"
            :bordered="false"
            hoverable
            @click="toggleSelect(g.id)"
          >
            <div class="game-card__check" @click.stop>
              <a-checkbox
                :checked="selectedIds.includes(g.id)"
                @change="toggleSelect(g.id)"
              />
            </div>
            <div class="game-card__cover">
              <a-image
                :src="g.cover"
                :preview="false"
                fallback="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='140'%3E%3C/svg%3E"
              />
            </div>
            <div class="game-card__name" :title="g.name">{{ g.name }}</div>
            <a
              v-if="g.link"
              class="game-card__link"
              href="javascript:void(0)"
              @click.stop="openGame(g)"
            >
              打开游戏
            </a>
          </a-card>
        </div>

        <div class="games-novel__pagination">
          <a-pagination
            :current="pagination.current"
            :page-size="pagination.pageSize"
            :total="total"
            :page-size-options="['12', '24', '48', '96']"
            show-size-changer
            :show-total="(t) => `共 ${t} 条`"
            @change="handlePageChange"
          />
        </div>
      </a-spin>
    </a-card>

    <a-modal
      :open="!!playing"
      :title="playing ? `《${playing.name}》` : ''"
      :footer="null"
      width="1180px"
      wrap-class-name="game-player-modal"
      destroy-on-close
      @cancel="playing = null"
    >
      <iframe
        v-if="playing"
        :src="playing.link"
        class="game-player-iframe"
        title="游戏"
        allow="autoplay; fullscreen"
        allowfullscreen
      />
    </a-modal>
  </div>
</template>

<style scoped>
.game-player-iframe {
  width: 100%;
  height: 70vh;
  border: 0;
  border-radius: 8px;
  background: #000;
}

.games-novel__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.games-novel__left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.games-novel__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
}

.game-card {
  position: relative;
  border: 1px solid var(--dr-border);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 10px 24px rgba(0, 0, 0, 0.35);
  cursor: pointer;
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}

.game-card:hover {
  transform: translateY(-3px);
  border-color: rgba(53, 214, 208, 0.45);
  box-shadow: 0 16px 34px rgba(0, 0, 0, 0.45), var(--dr-glow-cyan);
}

.game-card--selected {
  border-color: var(--dr-gold);
  box-shadow: var(--dr-glow-gold);
}

.game-card__check {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 2;
}

.game-card__cover :deep(.ant-image) {
  width: 100%;
}

.game-card__cover :deep(.ant-image-img) {
  width: 100%;
  height: 200px;
  object-fit: cover;
  border-radius: 6px;
}

.game-card__name {
  margin-top: 10px;
  color: var(--dr-text);
  font-family: 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'SimSun', serif;
  font-weight: 700;
  letter-spacing: 0.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.game-card__link {
  display: inline-block;
  margin-top: 6px;
  font-size: 13px;
}

.games-novel__pagination {
  margin-top: 20px;
  text-align: right;
}
</style>
