<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import dayjs from 'dayjs'
import {
  getRegenHistoryApi,
  getRuntimeConfigApi,
  getStoryTreeApi,
  getTaskStatusApi,
  rollbackRegenVersionApi,
} from '@/api'
import ChapterViewModal from './ChapterViewModal.vue'
import ReleaseDrawer from './ReleaseDrawer.vue'
import PlotTuneModal from '../plotInterpretation/PlotTuneModal.vue'

const props = defineProps({
  taskId: { type: String, default: '' },
  progress: { type: Number, default: 0 },
  published: { type: Boolean, default: false },
})

// isShowBtnAndStep：进版本管理/编辑子视图时让父级收掉步骤条
const emit = defineEmits(['isShowBtnAndStep', 'published'])

const VERSION_COLUMNS = [
  { title: '序号', dataIndex: 'id', key: 'id', align: 'center', width: 80 },
  { title: '版本号', dataIndex: 'version', key: 'version', align: 'center', width: 110 },
  { title: '修改人', dataIndex: 'author', key: 'author', align: 'center', width: 140 },
  { title: '修改时间', dataIndex: 'time', key: 'time', align: 'center', width: 180 },
  { title: '修改点', dataIndex: 'remark', key: 'remark', align: 'center' },
  { title: '操作', key: 'action', align: 'center', width: 150 },
]

const EDIT_COLUMNS = [
  { title: '序号', dataIndex: 'no', key: 'no', align: 'center', width: 80 },
  { title: '章节名称', dataIndex: 'title', key: 'title', align: 'center' },
  { title: '字数', dataIndex: 'words', key: 'words', align: 'center', width: 120 },
  { title: '完成时间', dataIndex: 'created_time', key: 'created_time', align: 'center', width: 180 },
  { title: '操作', key: 'action', align: 'center', width: 150 },
]

const INFO_LIST = [
  { label: '作品名称', fieldName: 'name' },
  { label: '作品ID', fieldName: 'story_id' },
  { label: '创建者', fieldName: 'creator_name' },
  { label: '历史编辑人员', fieldName: 'editor_names' },
]

const loading = ref(false)
const gameProgress = ref(0)
/** true = 主视图(iframe + 信息 + 四按钮)，false = 表格子视图(版本管理/编辑) */
const isShowGame = ref(true)
const isEditMode = ref(false)
const versionLoading = ref(false)
const editLoading = ref(false)

const iframeUrl = ref('')
const gameBaseUrl = ref('')
const chapterData = reactive({
  name: '--',
  story_id: '--',
  creator_name: '--',
  editor_names: '--',
})
const chapterList = ref([])

const columns = ref(VERSION_COLUMNS)
const dataSource = ref([])
const versionDataSource = ref([])
// a-table 的 columns 整表替换时不会重算 slot，靠 key 强制重建
const tableKey = ref(0)

const detailOpen = ref(false)
const detailRecord = ref(null)

const chapterViewModalRef = ref(null)
const releaseDrawerRef = ref(null)
const plotTuneModalRef = ref(null)

const isReady = computed(() => gameProgress.value >= 100)
const busy = computed(() => versionLoading.value || editLoading.value)

function isActiveVersion(record) {
  return record?.is_active === true || record?.is_active === 1 || record?.is_active === 'true'
}

function rollbackDisabledReason(record) {
  if (props.published) return '作品已发布，不可回滚素材版本'
  if (!isActiveVersion(record)) return '只能从当前生效版本回滚到上一版本'
  if (Number(record?.material_version) <= 0) return '当前已经是原始版本'
  return ''
}

function canRollback(record) {
  return !rollbackDisabledReason(record)
}

function filterNullData(value) {
  if (value === null || value === undefined || value === '') return '--'
  return value
}

function formatTime(time) {
  if (!time) return '--'
  const d = dayjs(time)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm:ss') : '--'
}

/** 章节运行时地址由后端下发的 game_base_url 拼出，不硬编码测试域名 */
function buildChapterLink(node) {
  if (!gameBaseUrl.value || !props.taskId) return ''
  const nodeId = String(node?.node_id ?? '').replace(/^node_/, '')
  if (!nodeId) return ''
  const base = gameBaseUrl.value.replace(/\/+$/, '')
  return `${base}/${props.taskId}/chapter_${node?.depth ?? 0}_${nodeId}/`
}

async function init(progress) {
  gameProgress.value = Number(progress ?? 0) || 0
  if (!props.taskId) return
  loading.value = true
  try {
    // 父页面的 progress 是进入创作页时的快照。同页完成演绎后直接来到这里时，
    // 该值可能仍是 0；必须先读取后端最新状态，否则发布页会永久卡在“正在创作”。
    const status = await getTaskStatusApi(props.taskId, { skipErrorToast: true })
    const statusData = status?.data ?? status ?? {}
    gameProgress.value = Math.max(
      gameProgress.value,
      Number(statusData.progress_percentage ?? 0) || 0,
    )
    if (!isReady.value) return

    try {
      const cfg = await getRuntimeConfigApi({ skipErrorToast: true })
      gameBaseUrl.value = (cfg?.data ?? cfg)?.game_base_url ?? ''
    } catch {
      gameBaseUrl.value = ''
    }

    const editorNames = (statusData.editors ?? [])
      .map((e) => e?.user_name?.trim())
      .filter(Boolean)
      .join('、')
    chapterData.name = filterNullData(statusData.project_name)
    chapterData.story_id = filterNullData(statusData.task_id ?? props.taskId)
    chapterData.creator_name = filterNullData(
      statusData.creator_name ?? statusData.owner_name ?? statusData.user_name,
    )
    chapterData.editor_names = editorNames || '--'

    const tree = await getStoryTreeApi(props.taskId, { skipErrorToast: true })
    const nodes = (tree?.data ?? tree)?.nodes ?? []
    chapterList.value = nodes.map((node, index) => ({
      ...node,
      no: index + 1,
      link: buildChapterLink(node),
      words: node?.summary ? String(node.summary).length : 0,
    }))
    iframeUrl.value = chapterList.value[0]?.link ?? ''
  } catch (error) {
    iframeUrl.value = ''
    chapterList.value = []
    message.error(`读取作品信息失败：${error?.message ?? error}`)
  } finally {
    loading.value = false
  }
}

function enterTableView(editMode) {
  isEditMode.value = editMode
  columns.value = editMode ? EDIT_COLUMNS : VERSION_COLUMNS
  dataSource.value = []
  tableKey.value += 1
  isShowGame.value = false
  emit('isShowBtnAndStep', false)
}

async function handleVersion() {
  if (versionLoading.value) return
  versionLoading.value = true
  enterTableView(false)
  try {
    const res = await getRegenHistoryApi(props.taskId, {})
    const records = (res?.data ?? res)?.records ?? []
    // material_version 0 是初次生成，不算"修改记录"
    versionDataSource.value = records.filter((item) => item?.material_version !== 0)
    dataSource.value = versionDataSource.value
  } catch (error) {
    message.error(`读取版本记录失败：${error?.message ?? error}`)
  } finally {
    versionLoading.value = false
  }
}

async function handleEdit() {
  if (props.published) {
    message.warning('作品已发布，不可修改')
    return
  }
  if (editLoading.value) return
  editLoading.value = true
  enterTableView(true)
  try {
    if (!chapterList.value.length) await init(gameProgress.value)
    dataSource.value = chapterList.value
  } catch (error) {
    message.error(`读取章节列表失败：${error?.message ?? error}`)
  } finally {
    editLoading.value = false
  }
}

function handlePreview() {
  const el = document.getElementById('export-main-iframe')
  if (!el) {
    message.warning('游戏还未加载完成')
    return
  }
  const request =
    el.requestFullscreen ??
    el.webkitRequestFullscreen ??
    el.mozRequestFullScreen ??
    el.msRequestFullscreen
  if (!request) {
    message.warning('您的浏览器不支持全屏功能')
    return
  }
  try {
    request.call(el)
  } catch (error) {
    message.error(`全屏失败：${error?.message ?? error}`)
  }
}

function handlePublish() {
  if (props.published) {
    message.warning('作品已发布，请勿重复发布')
    return
  }
  releaseDrawerRef.value?.init(props.taskId)
}

function handlePublished() {
  emit('published')
  void init(gameProgress.value)
}

function goBack() {
  tableKey.value += 1
  isEditMode.value = false
  columns.value = VERSION_COLUMNS
  dataSource.value = versionDataSource.value
  isShowGame.value = true
  emit('isShowBtnAndStep', true)
}

function handleDetail(record) {
  detailRecord.value = record
  detailOpen.value = true
}

function handlePreviewChapter(record) {
  chapterViewModalRef.value?.init(record?.link ?? '')
}

function handleModifyChapter(record) {
  if (props.published) {
    message.warning('作品已发布，不可修改')
    return
  }
  const index = chapterList.value.findIndex((item) => item.node_id === record?.node_id)
  plotTuneModalRef.value?.init({
    chapter: record,
    index: index >= 0 ? index : 0,
  })
}

function handleRollback(record) {
  const disabledReason = rollbackDisabledReason(record)
  if (disabledReason) {
    message.info(disabledReason)
    return
  }
  if (props.published) {
    message.warning('作品已发布，不可回滚素材')
    return
  }
  Modal.confirm({
    title: '确认回滚',
    content: `确定将当前版本 ${record?.version_str ?? record?.material_version} 回滚到上一版本吗？此操作不可撤销。`,
    okText: '确定',
    cancelText: '取消',
    onOk: async () => {
      try {
        const res = await rollbackRegenVersionApi(props.taskId, {
          task_id: props.taskId,
          node_id: record?.node_id,
          material_id: record?.material_id,
          current_material_version: record?.material_version,
        })
        const data = res?.data ?? res
        if (data?.status === 'success') {
          message.success('已回滚到上一版本')
          await handleVersion()
        } else {
          message.error(data?.message || '回滚失败，请稍后重试')
        }
      } catch (error) {
        message.error(`回滚失败：${error?.message ?? error}`)
      }
    },
  })
}

watch(
  () => [props.taskId, props.progress],
  ([id, progress]) => {
    if (!id) return
    void init(progress)
  },
  { immediate: true },
)

defineExpose({ init })
</script>

<template>
  <section class="export-release">
    <!-- 主视图 -->
    <div v-if="isShowGame" class="create-panel">
      <div class="create-panel__head">
        <div>
          <h3>导出与发布</h3>
          <p class="export-release__subtitle">预览最终游戏、检查作品信息并完成正式发布。</p>
        </div>
        <div class="export-release__status">
          <span v-if="isReady && !published" class="export-release__tag export-release__tag--ready">可发布</span>
          <span v-if="published" class="export-release__tag">已发布</span>
        </div>
      </div>

      <p v-if="!isReady" class="create-empty">
        正在积极创作，当前进度 {{ gameProgress }}%，请耐心等待……
      </p>

      <template v-else>
        <a-spin :spinning="loading">
          <div class="export-release__main">
            <section class="export-release__preview-card">
              <div class="export-release__section-head">
                <div>
                  <strong>游戏预览</strong>
                  <span>最终在线包 · 第一章节</span>
                </div>
                <a-button type="text" :disabled="busy" @click="init(gameProgress)">
                  重新加载
                </a-button>
              </div>
              <div class="export-release__stage">
                <iframe
                  v-if="iframeUrl"
                  id="export-main-iframe"
                  :src="iframeUrl"
                  frameborder="0"
                  allowfullscreen
                ></iframe>
                <p v-else class="export-release__stage-empty">
                  暂无可预览的游戏地址，可点击「重新加载」重试
                </p>
              </div>
            </section>

            <div class="export-release__side">
              <section class="export-release__info-card">
                <div class="export-release__section-head">
                  <div>
                    <strong>作品信息</strong>
                    <span>发布前请确认以下内容</span>
                  </div>
                </div>
                <dl class="export-release__info">
                  <template v-for="item in INFO_LIST" :key="item.fieldName">
                    <dt>{{ item.label }}</dt>
                    <dd>
                      <a-tooltip :title="chapterData[item.fieldName]" placement="topLeft">
                        <a-typography-paragraph
                          :content="String(chapterData[item.fieldName])"
                          :ellipsis="{ rows: 1, css: true, expandable: false }"
                        />
                      </a-tooltip>
                    </dd>
                  </template>
                </dl>
              </section>

              <section class="export-release__action-card">
                <div class="export-release__section-head">
                  <div>
                    <strong>发布操作</strong>
                    <span>{{ published ? '作品已锁定为只读' : '发布后将无法继续编辑' }}</span>
                  </div>
                </div>
                <a-button type="primary" :disabled="busy || published" @click="handlePublish">
                  {{ published ? '作品已发布' : '填写信息并发布' }}
                </a-button>
                <div class="export-release__actions">
                  <a-button :loading="versionLoading" :disabled="busy" @click="handleVersion">
                    版本管理
                  </a-button>
                  <a-button :disabled="busy" @click="handlePreview">全屏预览</a-button>
                  <a-button :loading="editLoading" :disabled="busy || published" @click="handleEdit">
                    章节编辑
                  </a-button>
                  <a-tooltip title="导出功能暂未开放">
                    <a-button disabled>本地导出</a-button>
                  </a-tooltip>
                </div>
              </section>
            </div>
          </div>
        </a-spin>
      </template>
    </div>

    <!-- 表格子视图：版本管理 / 编辑 -->
    <div v-else class="create-panel export-release__table-panel">
      <div class="create-panel__head">
        <h3>{{ isEditMode ? '章节编辑' : '版本管理' }}</h3>
        <div class="create-panel__head-actions">
          <a-button @click="goBack">返回</a-button>
        </div>
      </div>

      <a-table
        :key="tableKey"
        :columns="columns"
        :data-source="dataSource"
        :pagination="false"
        :loading="versionLoading || editLoading"
        bordered
        size="small"
        :scroll="{ y: 'calc(100vh - 340px)', x: 'max-content' }"
        row-key="rowKey"
      >
        <template #bodyCell="{ column, record, index }">
          <template v-if="column.key === 'id'">{{ index + 1 }}</template>
          <template v-else-if="column.key === 'version'">
            {{ filterNullData(record.version_str) }}
          </template>
          <template v-else-if="column.key === 'author'">
            {{ filterNullData(record.user_name) }}
          </template>
          <template v-else-if="column.key === 'time'">
            {{ formatTime(record.created_at) }}
          </template>
          <template v-else-if="column.key === 'remark'">
            {{ filterNullData(record.oss_key) }}
          </template>
          <template v-else-if="column.key === 'no'">{{ index + 1 }}</template>
          <template v-else-if="column.key === 'title'">
            {{ filterNullData(record.title) }}
          </template>
          <template v-else-if="column.key === 'words'">
            {{ record.words || 0 }}
          </template>
          <template v-else-if="column.key === 'created_time'">
            {{ formatTime(record.created_at) }}
          </template>
          <template v-else-if="column.key === 'action'">
            <template v-if="isEditMode">
              <a-button type="link" size="small" @click="handlePreviewChapter(record)">
                预览
              </a-button>
              <a-button
                type="link"
                size="small"
                :disabled="published"
                @click="handleModifyChapter(record)"
              >
                修改
              </a-button>
            </template>
            <template v-else>
              <a-button type="link" size="small" @click="handleDetail(record)">详情</a-button>
              <a-tooltip :title="rollbackDisabledReason(record)">
                <span>
                  <a-button
                    type="link"
                    size="small"
                    :disabled="!canRollback(record)"
                    @click="handleRollback(record)"
                  >
                    回滚
                  </a-button>
                </span>
              </a-tooltip>
            </template>
          </template>
        </template>
      </a-table>
    </div>

    <a-modal
      v-model:open="detailOpen"
      title="版本详情"
      :width="600"
      :footer="null"
      wrap-class-name="version-detail-modal"
      centered
    >
      <dl v-if="detailRecord" class="export-release__detail">
        <dt>版本号</dt>
        <dd>{{ filterNullData(detailRecord.version_str) }}</dd>
        <dt>修改人</dt>
        <dd>{{ filterNullData(detailRecord.user_name) }}</dd>
        <dt>修改时间</dt>
        <dd>{{ formatTime(detailRecord.created_at) }}</dd>
        <dt>修改点</dt>
        <dd class="export-release__detail-break">{{ filterNullData(detailRecord.oss_key) }}</dd>
      </dl>
    </a-modal>

    <ChapterViewModal ref="chapterViewModalRef" />
    <ReleaseDrawer
      ref="releaseDrawerRef"
      :owner-task-id="props.taskId"
      @success="handlePublished"
    />
    <PlotTuneModal
      ref="plotTuneModalRef"
      :is-renewal="true"
      :task-id="props.taskId"
      :published="props.published"
    />
  </section>
</template>

<style scoped>
@import '../../styles/panel.css';

.export-release__tag {
  padding: 3px 11px;
  border: 1px solid #4d7d5f;
  border-radius: 999px;
  background: #1d2a22;
  color: #7fd6a2;
  font-size: 12px;
}

.export-release__tag--ready {
  border-color: rgba(137, 154, 226, 0.3);
  background: rgba(104, 125, 220, 0.13);
  color: #aebaf3;
}

.export-release__subtitle {
  margin: 6px 0 0;
  color: #858da2;
  font-size: 12px;
}

.export-release__status {
  display: flex;
  align-items: center;
  gap: 8px;
}

.export-release__main {
  display: grid;
  grid-template-columns: minmax(0, 2.2fr) minmax(280px, 0.9fr);
  gap: 18px;
  align-items: stretch;
}

.export-release__preview-card,
.export-release__info-card,
.export-release__action-card {
  padding: 14px;
  border: 1px solid rgba(145, 158, 205, 0.15);
  border-radius: 15px;
  background: rgba(17, 20, 29, 0.38);
}

.export-release__preview-card {
  min-width: 0;
}

.export-release__section-head {
  display: flex;
  min-height: 34px;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.export-release__section-head > div {
  display: grid;
  gap: 2px;
}

.export-release__section-head strong {
  color: #dce1ee;
  font-size: 13px;
  font-weight: 600;
}

.export-release__section-head span {
  color: #858da2;
  font-size: 11px;
}

.export-release__stage {
  display: flex;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border: 1px solid rgba(145, 158, 205, 0.18);
  border-radius: 14px;
  background: rgba(10, 13, 20, 0.78);
  box-shadow: inset 0 0 24px rgba(0, 0, 0, 0.2);
}

.export-release__stage iframe {
  width: 100%;
  height: 100%;
  border: 0;
}

.export-release__stage-empty {
  margin: auto;
  padding: 0 20px;
  color: #767c8f;
  font-size: 13px;
  text-align: center;
}

.export-release__side {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 14px;
  min-width: 280px;
}

.export-release__info {
  display: grid;
  grid-template-columns: 86px minmax(0, 1fr);
  gap: 8px 10px;
  margin: 0;
  padding: 2px 0 0;
}

.export-release__info dt {
  color: #8a91a8;
  font-size: 12px;
}

.export-release__info dd {
  min-width: 0;
  margin: 0;
  color: #e7eaf5;
  font-size: 13px;
}

.export-release__info dd :deep(.ant-typography) {
  margin-bottom: 0 !important;
  color: #e7eaf5;
}

.export-release__action-card {
  display: grid;
  gap: 12px;
  margin-top: auto;
}

.export-release__action-card > .ant-btn-primary {
  width: 100%;
  height: 42px;
}

.export-release__actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.export-release__actions .ant-btn {
  min-width: 0;
}

.export-release__table-panel {
  border-color: rgba(145, 158, 205, 0.2);
  background: linear-gradient(145deg, rgba(31, 35, 48, 0.98), rgba(20, 23, 32, 0.98));
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.035), 0 18px 46px rgba(0, 0, 0, 0.2);
}

.export-release__table-panel .create-panel__head h3 {
  color: #e7eaf3;
}

.export-release__table-panel :deep(.ant-btn-default) {
  border-color: rgba(151, 164, 211, 0.24);
  background: rgba(41, 46, 61, 0.82);
  color: #cbd1e1;
  box-shadow: none;
}

.export-release__table-panel :deep(.ant-table) {
  background: rgba(15, 18, 26, 0.46);
  color: #c7cddd;
}

.export-release__table-panel :deep(.ant-table-container) {
  overflow: hidden;
  border-color: rgba(145, 158, 205, 0.18) !important;
  border-radius: 12px;
}

.export-release__table-panel :deep(.ant-table-thead > tr > th) {
  border-color: rgba(145, 158, 205, 0.16) !important;
  background: rgba(48, 54, 72, 0.84) !important;
  color: #bcc4d8;
  font-weight: 600;
}

.export-release__table-panel :deep(.ant-table-tbody > tr > td) {
  border-color: rgba(145, 158, 205, 0.12) !important;
  background: rgba(27, 31, 42, 0.72);
  color: #b8c0d2;
}

.export-release__table-panel :deep(.ant-table-tbody > tr:nth-child(even) > td) {
  background: rgba(31, 36, 49, 0.74);
}

.export-release__table-panel :deep(.ant-table-tbody > tr.ant-table-row:hover > td) {
  background: rgba(49, 57, 78, 0.82) !important;
}

.export-release__table-panel :deep(.ant-btn-link) {
  color: #9dacdf;
}

.export-release__table-panel :deep(.ant-btn-link:disabled) {
  color: #626a7d;
}

:global(.version-detail-modal .ant-modal-content),
:global(.version-detail-modal .ant-modal-header) {
  background: #222633;
}

:global(.version-detail-modal .ant-modal-title) {
  color: #e7eaf3;
}

:global(.version-detail-modal .export-release__detail dt) {
  color: #8d96aa;
}

:global(.version-detail-modal .export-release__detail dd) {
  color: #cbd1df;
}

.export-release__detail {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 10px 16px;
  margin: 0;
}

.export-release__detail dt {
  color: #8a91a8;
  font-size: 13px;
}

.export-release__detail dd {
  margin: 0;
  color: #e7eaf5;
  font-size: 13px;
}

.export-release__detail-break {
  line-height: 1.7;
  word-break: break-all;
}

@media (max-width: 920px) {
  .export-release__main {
    grid-template-columns: 1fr;
  }

  .export-release__side {
    min-width: 0;
  }
}
</style>
