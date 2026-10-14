<script setup>
import { onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  getPromoGamesApi,
  uploadPromoCoverApi,
  createPromoGameApi,
  deletePromoGameApi,
  getDistributedIdsApi,
  distributePromoGamesApi,
  recallPromoGamesApi,
} from '@/api'

const loading = ref(false)
const games = ref([])
const total = ref(0)
const selectedIds = ref([])
const distributedIds = ref([])
const distributing = ref(false)
const pagination = reactive({ current: 1, pageSize: 24 })

const modalOpen = ref(false)
const submitting = ref(false)
const coverUploading = ref(false)
const form = reactive({ name: '', url: '', cover: '', description: '' })

const playing = ref(null)
function openGame(g) {
  if (g?.url) playing.value = g
}

async function fetchGames() {
  loading.value = true
  try {
    const res = await getPromoGamesApi({
      page: pagination.current,
      page_size: pagination.pageSize,
    })
    games.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  } finally {
    loading.value = false
  }
}

async function fetchDistributed() {
  try {
    const res = await getDistributedIdsApi()
    distributedIds.value = res.ids || []
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  }
}

function isDistributed(id) {
  return distributedIds.value.includes(id)
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

async function handleDistribute() {
  if (!selectedIds.value.length) return
  distributing.value = true
  try {
    const res = await distributePromoGamesApi(selectedIds.value)
    message.success(`已分发 ${res.distributed} 个${res.skipped ? `,${res.skipped} 个已分发跳过` : ''}`)
    selectedIds.value = []
    await fetchDistributed()
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  } finally {
    distributing.value = false
  }
}

async function handleRecall() {
  if (!selectedIds.value.length) return
  distributing.value = true
  try {
    const res = await recallPromoGamesApi(selectedIds.value)
    message.success(`已撤销 ${res.recalled} 个`)
    selectedIds.value = []
    await fetchDistributed()
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  } finally {
    distributing.value = false
  }
}

function openUpload() {
  form.name = ''
  form.url = ''
  form.cover = ''
  form.description = ''
  modalOpen.value = true
}

/** a-upload 拦截:不走默认 action,手动上传拿回 OSS URL 做预览 */
async function beforeCoverUpload(file) {
  coverUploading.value = true
  try {
    const res = await uploadPromoCoverApi(file)
    form.cover = res.cover_url
    message.success('封面上传成功')
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  } finally {
    coverUploading.value = false
  }
  return false // 阻止 antd 默认上传
}

async function handleSubmit() {
  if (!form.name.trim()) {
    message.warning('请填写游戏名')
    return
  }
  submitting.value = true
  try {
    await createPromoGameApi({
      name: form.name.trim(),
      url: form.url.trim(),
      cover: form.cover.trim(),
      description: form.description.trim(),
    })
    message.success('已添加')
    modalOpen.value = false
    pagination.current = 1
    fetchGames()
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  } finally {
    submitting.value = false
  }
}

async function handleDelete(id) {
  try {
    await deletePromoGameApi(id)
    message.success('已删除')
    const i = selectedIds.value.indexOf(id)
    if (i >= 0) selectedIds.value.splice(i, 1)
    fetchGames()
  } catch (e) {
    // 错误提示已由 request 拦截器统一处理
  }
}

onMounted(() => {
  fetchGames()
  fetchDistributed()
})
</script>

<template>
  <div class="games-promo">
    <div class="dr-page-head">
      <div>
        <div class="dr-page-title">创作推广</div>
        <div class="dr-page-desc">手动上传的推广游戏 · 分发后在创造平台游戏仓库可见</div>
      </div>
    </div>

    <a-card :bordered="false">
      <div class="games-promo__toolbar">
        <span class="games-promo__count">共 {{ total }} 个</span>
        <div class="games-promo__actions">
          <a-button
            type="primary"
            :disabled="selectedIds.length === 0"
            :loading="distributing"
            @click="handleDistribute"
          >
            分发{{ selectedIds.length ? `(${selectedIds.length})` : '' }}
          </a-button>
          <a-button
            danger
            :disabled="selectedIds.length === 0"
            :loading="distributing"
            @click="handleRecall"
          >
            撤回分发{{ selectedIds.length ? `(${selectedIds.length})` : '' }}
          </a-button>
          <a-button type="primary" @click="openUpload">上传游戏</a-button>
        </div>
      </div>

      <a-spin :spinning="loading">
        <a-empty v-if="!games.length && !loading" description="暂无游戏,点右上角上传" />

        <div v-else class="games-promo__grid">
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
            <a-tag v-if="isDistributed(g.id)" color="green" class="game-card__badge">已分发</a-tag>
            <div class="game-card__name" :title="g.name">{{ g.name }}</div>
            <div class="game-card__desc" :title="g.description">{{ g.description }}</div>
            <div class="game-card__footer" @click.stop>
              <a
                v-if="g.url"
                class="game-card__link"
                href="javascript:void(0)"
                @click="openGame(g)"
              >
                打开
              </a>
              <a-popconfirm
                title="确定删除这个游戏?"
                ok-text="删除"
                cancel-text="取消"
                @confirm="handleDelete(g.id)"
              >
                <a class="game-card__delete">删除</a>
              </a-popconfirm>
            </div>
          </a-card>
        </div>

        <div class="games-promo__pagination">
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
      v-model:open="modalOpen"
      title="上传游戏"
      width="560px"
      :confirm-loading="submitting"
      ok-text="保存"
      cancel-text="取消"
      @ok="handleSubmit"
    >
      <a-form layout="vertical">
        <a-form-item label="封面">
          <a-upload
            :show-upload-list="false"
            :before-upload="beforeCoverUpload"
            accept="image/*"
          >
            <div class="cover-uploader">
              <a-spin v-if="coverUploading" />
              <a-image
                v-else-if="form.cover"
                :src="form.cover"
                :preview="false"
                :width="120"
              />
              <div v-else class="cover-uploader__empty">
                <div>+</div>
                <div>上传封面</div>
              </div>
            </div>
          </a-upload>
        </a-form-item>
        <a-form-item label="游戏名" required>
          <a-input v-model:value="form.name" placeholder="游戏名称" />
        </a-form-item>
        <a-form-item label="游戏链接(URL)">
          <a-input v-model:value="form.url" placeholder="https://..." />
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="form.description" :rows="3" placeholder="游戏简介" />
        </a-form-item>
      </a-form>
    </a-modal>

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
        :src="playing.url"
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

.games-promo__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.games-promo__count {
  color: var(--dr-text-3);
  font-size: 13px;
}

.games-promo__actions {
  display: flex;
  gap: 8px;
}

.games-promo__grid {
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

.game-card__badge {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 2;
  margin: 0;
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

.game-card__desc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--dr-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.game-card__footer {
  margin-top: 8px;
  display: flex;
  justify-content: space-between;
  font-size: 13px;
}

.game-card__delete {
  color: #ff4d4f;
}

.games-promo__pagination {
  margin-top: 20px;
  text-align: right;
}

.cover-uploader {
  width: 120px;
  min-height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--dr-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
  cursor: pointer;
  transition: border-color 0.18s ease;
}

.cover-uploader:hover {
  border-color: var(--dr-gold);
}

.cover-uploader__empty {
  text-align: center;
  color: var(--dr-text-3);
  font-size: 13px;
}

.cover-uploader__empty div:first-child {
  font-size: 24px;
}
</style>
