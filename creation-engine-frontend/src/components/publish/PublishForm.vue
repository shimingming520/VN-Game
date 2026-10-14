<script setup>
import { defineComponent, onMounted, reactive, ref, watchEffect } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined } from '@ant-design/icons-vue'
import {
  createCmsTagApi,
  getCmsClassesApi,
  getCmsTagsApi,
  getPublishInfoApi,
  uploadCoverApi,
} from '@/api'

/**
 * 发布表单。单个发布(ReleaseDrawer)与批量发布(逐项过表单)共用这一份，
 * 字段与中台 PublishModal 对齐；提交动作由外层负责，这里只出数据。
 */
const props = defineProps({
  taskId: { type: String, required: true },
  autoLoad: { type: Boolean, default: false },
})

const TAG_LIMIT = 10

const loading = ref(false)
const optionsLoading = ref(false)
const formRef = ref(null)
const classTree = ref([])
const tagOptions = ref([])
const gameLink = ref('')
const autoCover = ref('')
const uploading = ref(false)
const tagName = ref('')
const savingTag = ref(false)
const presetHint = ref('')
const cmsOptionsError = ref('')
const lastPublishInfo = ref({})

const formState = reactive({
  name: '',
  description: '',
  cover: '',
  class_id: [],
  tags: [],
  chapters: [],
  is_recommend: true,
})

const rules = {
  name: [{ required: true, message: '请输入游戏名称' }],
  description: [{ required: true, message: '请输入简介/剧情概要' }],
  cover: [{ required: true, message: '请上传游戏封面' }],
  class_id: [{ required: true, type: 'array', message: '请选择游戏分类' }],
  tags: [{ required: true, type: 'array', message: '请选择标签' }],
}

const chapterColumns = [
  { title: '章节', dataIndex: 'title', key: 'title' },
  { title: '节点数', dataIndex: 'node_count', key: 'node_count', width: 100, align: 'center' },
  { title: '免费', key: 'is_free', width: 90, align: 'center' },
  { title: '价格(金币)', key: 'coins', width: 150, align: 'center' },
]

// a-select 的 dropdownRender 插槽拿到的是 VNode，需要一层组件把它渲染出来
const VNodes = defineComponent({
  props: { vnodes: { type: Object, required: true } },
  render() {
    return this.vnodes
  },
})

// 标签上限硬约束：超出立即截断，避免提交时才报错
watchEffect(() => {
  if (formState.tags.length > TAG_LIMIT) {
    message.warning(`最多只能选择 ${TAG_LIMIT} 个标签`)
    formState.tags = formState.tags.slice(0, TAG_LIMIT)
  }
})

/** 扁平 class_id 反查成 a-cascader 需要的整条路径 */
function findTreePath(tree, targetId, idKey = 'id', childrenKey = 'children') {
  if (!Array.isArray(tree)) return []
  for (const node of tree) {
    if (String(node?.[idKey]) === String(targetId)) return [node[idKey]]
    const path = findTreePath(node?.[childrenKey], targetId, idKey, childrenKey)
    if (path.length) return [node[idKey], ...path]
  }
  return []
}

function unwrap(res) {
  const data = res?.data ?? res
  return data?.data ?? data
}

function apiErrorText(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  return error?.message || '服务暂时不可用'
}

/** 兼容中台 {items:[{value,label}]} 和旧版 [{id,name}] 两种响应 */
function responseItems(res) {
  const data = unwrap(res)
  if (Array.isArray(data)) return data
  return Array.isArray(data?.items) ? data.items : []
}

function normalizeClassTree(rows) {
  return responseItems(rows).map(normalizeClassNode).filter((item) => item.id !== undefined)
}

function normalizeClassNode(node) {
  const children = Array.isArray(node?.children)
    ? node.children.map(normalizeClassNode).filter((item) => item.id !== undefined)
    : []
  return {
    ...node,
    id: node?.id ?? node?.value,
    name: node?.name ?? node?.label ?? String(node?.id ?? node?.value ?? ''),
    ...(children.length ? { children } : {}),
  }
}

function normalizeTagOptions(rows) {
  return responseItems(rows)
    .map((item) => ({
      ...item,
      id: item?.id ?? item?.value,
      name: item?.name ?? item?.label ?? String(item?.id ?? item?.value ?? ''),
    }))
    .filter((item) => item.id !== undefined)
}

function matchText(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[\s\-_/·,，。:：()（）【】\[\]]+/g, '')
}

function isNameMatch(actual, expected) {
  const left = matchText(actual)
  const right = matchText(expected)
  return Boolean(left && right && (left === right || left.includes(right) || right.includes(left)))
}

/** 按后端给的候选名优先级，在 CMS 实际分类树中匹配路径 */
function findTreePathByNames(tree, names) {
  function firstLeafPath(node, parents = []) {
    const path = [...parents, node.id]
    const children = node.children || []
    if (!children.length) return path
    for (const child of children) {
      const leafPath = firstLeafPath(child, path)
      if (leafPath.length) return leafPath
    }
    return path
  }

  function walk(nodes, expected, parents = []) {
    for (const node of nodes || []) {
      const path = [...parents, node.id]
      // Cascader 默认只能选择末级。匹配到父分类时继续补到第一个可选叶子，
      // 否则 v-model 虽然有值，控件仍会显示成“尚未选择”。
      if (isNameMatch(node.name, expected)) return firstLeafPath(node, parents)
      const childPath = walk(node.children, expected, path)
      if (childPath.length) return childPath
    }
    return []
  }

  for (const name of names || []) {
    const path = walk(tree, name)
    if (path.length) return path
  }
  return []
}

function firstTreeLeafPath(tree) {
  const first = tree?.[0]
  if (!first) return []
  const path = [first.id]
  let node = first
  while (node?.children?.length) {
    node = node.children[0]
    path.push(node.id)
  }
  return path
}

function findTagIds(options, names) {
  const ids = []
  for (const name of names || []) {
    const matched = options.find((item) => isNameMatch(item.name, name))
    if (matched && !ids.includes(matched.id)) ids.push(matched.id)
    if (ids.length >= TAG_LIMIT) break
  }
  return ids
}

function applyPublishInfo(info) {
  formState.name = info.name ?? info.project_name ?? ''
  formState.description = info.description ?? info.book_summary ?? ''
  autoCover.value = info.cover ?? ''
  formState.cover = info.cover ?? ''
  gameLink.value = info.link ?? info.game_url ?? ''
  formState.is_recommend = info.is_recommend ?? true
  formState.chapters = (info.chapters ?? []).map((c, index) => ({
    number: c.number ?? c.no ?? index + 1,
    title: c.title ?? `第 ${c.number ?? c.no ?? index + 1} 章`,
    node_count: c.node_count ?? c.nodes?.length ?? 0,
    is_free: c.is_free ?? false,
    coins: Number(c.coins ?? 0) || 0,
  }))
}

function applyClassificationPreset(info) {
  let presetApplied = false
  let usedCategoryFallback = false
  if (info.class_id) {
    formState.class_id = findTreePath(classTree.value, info.class_id)
  } else if (!formState.class_id.length) {
    const typeCandidates = info.gameplay === 'free_growth'
      ? ['模拟养成', '养成游戏', '模拟经营', '角色扮演']
      : ['视觉小说', '互动小说', '文字冒险', '剧情互动', '角色扮演']
    let suggestedPath = findTreePathByNames(
      classTree.value,
      [...(info.suggested_categories ?? []), ...typeCandidates],
    )
    if (!suggestedPath.length) {
      suggestedPath = firstTreeLeafPath(classTree.value)
      usedCategoryFallback = suggestedPath.length > 0
    }
    if (suggestedPath.length) {
      formState.class_id = suggestedPath
      presetApplied = true
    }
  }

  if (Array.isArray(info.tags) && info.tags.length) {
    formState.tags = info.tags
      .map((item) => (typeof item === 'object' ? item?.id ?? item?.value : item))
      .filter(Boolean)
  } else if (!formState.tags.length) {
    const suggestedTags = findTagIds(tagOptions.value, info.suggested_tags ?? [])
    if (suggestedTags.length) {
      formState.tags = suggestedTags
      presetApplied = true
    }
  }

  if (presetApplied) {
    const typeLabel = info.gameplay === 'free_growth' ? '自由成长' : '互动剧情'
    const genreLabel = info.genre ? `·${info.genre}` : ''
    presetHint.value = usedCategoryFallback
      ? `已根据${typeLabel}${genreLabel}预选分类和标签，请确认后按需修改。`
      : `已根据${typeLabel}${genreLabel}自动推荐，分类和标签仍可修改。`
  }
}

async function load() {
  loading.value = true
  optionsLoading.value = true
  presetHint.value = ''
  cmsOptionsError.value = ''
  lastPublishInfo.value = {}
  const classRequest = getCmsClassesApi({ skipErrorToast: true })
  const tagRequest = getCmsTagsApi({ skipErrorToast: true })
  let info = {}
  try {
    info = unwrap(await getPublishInfoApi(props.taskId, { skipErrorToast: true })) ?? {}
    lastPublishInfo.value = info
    applyPublishInfo(info)
  } catch (error) {
    const isTimeout = /timeout/i.test(error?.message || '') || error?.code === 'ECONNABORTED'
    message.error(isTimeout ? '发布信息加载较慢，请稍后重试' : `读取发布信息失败：${error?.message ?? error}`)
  } finally {
    loading.value = false
  }

  const [classResult, tagResult] = await Promise.allSettled([classRequest, tagRequest])
  if (classResult.status === 'fulfilled') {
    classTree.value = normalizeClassTree(classResult.value)
  } else {
    classTree.value = []
  }
  if (tagResult.status === 'fulfilled') {
    tagOptions.value = normalizeTagOptions(tagResult.value)
  } else {
    tagOptions.value = []
  }
  const optionErrors = []
  if (classResult.status === 'rejected') {
    optionErrors.push(`分类：${apiErrorText(classResult.reason)}`)
  }
  if (tagResult.status === 'rejected') {
    optionErrors.push(`标签：${apiErrorText(tagResult.reason)}`)
  }
  cmsOptionsError.value = optionErrors.join('；')
  if (cmsOptionsError.value) message.warning('CMS 分类或标签加载失败，可在表单内重试')
  applyClassificationPreset(info)
  optionsLoading.value = false
}

async function retryCmsOptions() {
  if (optionsLoading.value) return
  optionsLoading.value = true
  cmsOptionsError.value = ''
  const [classResult, tagResult] = await Promise.allSettled([
    getCmsClassesApi({ skipErrorToast: true }),
    getCmsTagsApi({ skipErrorToast: true }),
  ])
  const optionErrors = []
  if (classResult.status === 'fulfilled') {
    classTree.value = normalizeClassTree(classResult.value)
  } else {
    optionErrors.push(`分类：${apiErrorText(classResult.reason)}`)
  }
  if (tagResult.status === 'fulfilled') {
    tagOptions.value = normalizeTagOptions(tagResult.value)
  } else {
    optionErrors.push(`标签：${apiErrorText(tagResult.reason)}`)
  }
  cmsOptionsError.value = optionErrors.join('；')
  applyClassificationPreset(lastPublishInfo.value)
  optionsLoading.value = false
  if (!cmsOptionsError.value) message.success('CMS 分类和标签已重新加载')
}

async function handleAddTag() {
  const name = tagName.value.trim()
  if (!name) {
    message.warning('请输入标签名称')
    return
  }
  savingTag.value = true
  try {
    const res = await createCmsTagApi({ id: 0, name, status: 1 })
    const data = unwrap(res) ?? {}
    if (!data.id) throw new Error('未返回标签 ID')
    tagOptions.value = [{ id: data.id, name: data.name ?? name }, ...tagOptions.value]
    if (formState.tags.length < TAG_LIMIT) formState.tags = [...formState.tags, data.id]
    tagName.value = ''
  } catch (error) {
    message.error(`新增标签失败：${error?.message ?? error}`)
  } finally {
    savingTag.value = false
  }
}

async function handleUpload({ file }) {
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    const url = unwrap(await uploadCoverApi(fd))?.url
    if (!url) throw new Error('未返回封面地址')
    formState.cover = url
    message.success('封面已更新')
  } catch (error) {
    message.error(`封面上传失败：${error?.message ?? error}`)
  } finally {
    uploading.value = false
  }
}

function resetCover() {
  formState.cover = autoCover.value
}

/** 校验并返回提交用的 payload；校验失败滚到第一个错误项并返回 null */
async function validateAndCollect() {
  try {
    await formRef.value?.validate()
  } catch (error) {
    const firstField = error?.errorFields?.[0]?.name
    if (firstField) {
      formRef.value?.scrollToField(firstField, { behavior: 'smooth', block: 'center' })
    }
    message.warning('请先补全必填项')
    return null
  }
  return {
    task_id: props.taskId,
    name: formState.name.trim(),
    description: formState.description.trim(),
    cover: formState.cover,
    class_id: formState.class_id.at(-1) ?? null,
    tags: [...formState.tags],
    is_recommend: formState.is_recommend,
    chapters: formState.chapters.map((c) => ({
      number: c.number,
      is_free: c.is_free,
      coins: c.is_free ? 0 : Number(c.coins) || 0,
    })),
  }
}

/** 回填已填过的一项（批量发布返回上一项时用） */
function restore(payload) {
  if (!payload) return
  formState.name = payload.name ?? ''
  formState.description = payload.description ?? ''
  formState.cover = payload.cover ?? ''
  formState.tags = [...(payload.tags ?? [])]
  formState.is_recommend = payload.is_recommend ?? true
  if (payload.class_id) formState.class_id = findTreePath(classTree.value, payload.class_id)
  if (payload.chapters?.length) {
    formState.chapters = formState.chapters.map((c) => {
      const saved = payload.chapters.find((s) => s.number === c.number)
      return saved ? { ...c, is_free: saved.is_free, coins: saved.coins } : c
    })
  }
}

onMounted(() => {
  if (props.autoLoad) void load()
})

defineExpose({ load, validateAndCollect, restore, loading })
</script>

<template>
  <a-spin :spinning="loading">
    <div class="publish-form__intro">
      <span>RELEASE PROFILE</span>
      <strong>完善作品发布资料</strong>
      <p>名称、分类、封面、简介和标签已尽量自动预填，提交前仍可自由修改。</p>
    </div>
    <a-form ref="formRef" class="publish-form" :model="formState" :rules="rules" layout="vertical" scroll-to-first-error>
      <a-form-item label="游戏名称" name="name">
        <a-input
          v-model:value="formState.name"
          placeholder="请输入游戏名称"
          allow-clear
          :maxlength="50"
          show-count
        />
      </a-form-item>

      <a-form-item label="游戏分类" name="class_id">
        <a-cascader
          v-model:value="formState.class_id"
          :options="classTree"
          :field-names="{ label: 'name', value: 'id', children: 'children' }"
          expand-trigger="hover"
          popup-class-name="publish-form-dropdown"
          :loading="optionsLoading"
          show-search
          placeholder="请选择游戏分类"
        />
        <div v-if="cmsOptionsError" class="publish-options-error">
          <span>{{ cmsOptionsError }}</span>
          <a-button type="link" size="small" :loading="optionsLoading" @click="retryCmsOptions">
            重新加载
          </a-button>
        </div>
        <div v-if="presetHint" class="publish-preset-hint">{{ presetHint }}</div>
      </a-form-item>

      <a-form-item label="游戏封面" name="cover">
        <div class="publish-cover">
          <a-image v-if="formState.cover" :src="formState.cover" :width="160" />
          <div v-else class="publish-cover__placeholder">暂无封面</div>
          <div class="publish-cover__actions">
            <a-upload
              :show-upload-list="false"
              accept="image/*"
              :custom-request="handleUpload"
              :disabled="uploading"
            >
              <a-button :loading="uploading">上传替换</a-button>
            </a-upload>
            <a-button v-if="autoCover" type="link" @click="resetCover">用自动封面</a-button>
          </div>
        </div>
      </a-form-item>

      <a-form-item label="简介 / 剧情概要" name="description">
        <a-textarea
          v-model:value="formState.description"
          placeholder="请输入简介 / 剧情概要"
          :rows="5"
          allow-clear
          :maxlength="500"
          show-count
        />
      </a-form-item>

      <a-form-item :label="`标签（最多 ${TAG_LIMIT} 个）`" name="tags">
        <a-select
          v-model:value="formState.tags"
          mode="multiple"
          placement="topLeft"
          :options="tagOptions"
          :field-names="{ label: 'name', value: 'id' }"
          option-filter-prop="name"
          show-search
          popup-class-name="publish-form-dropdown"
          :loading="optionsLoading"
          placeholder="请选择标签"
          @dropdown-visible-change="tagName = ''"
        >
          <template #dropdownRender="{ menuNode: menu }">
            <div class="publish-tag-add">
              <a-input
                v-model:value="tagName"
                placeholder="请输入新标签"
                :maxlength="50"
                show-count
              />
              <a-button :loading="savingTag" @click="handleAddTag">
                <template #icon><PlusOutlined /></template>
                添加
              </a-button>
            </div>
            <a-divider style="margin: 4px 0" />
            <VNodes :vnodes="menu" />
          </template>
        </a-select>
      </a-form-item>

      <a-form-item label="推广设置" name="is_recommend">
        <a-switch v-model:checked="formState.is_recommend" />
        <div class="publish-hint">加入新游推荐列表，开启后将出现在首页「本周新游」栏目。</div>
      </a-form-item>

      <a-form-item label="章节价格">
        <div v-if="!formState.chapters.length" class="publish-empty">暂无章节</div>
        <a-table
          v-else
          :columns="chapterColumns"
          :data-source="formState.chapters"
          :pagination="false"
          size="small"
          bordered
          row-key="number"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'is_free'">
              <a-switch v-model:checked="record.is_free" size="small" />
            </template>
            <template v-else-if="column.key === 'coins'">
              <a-input-number
                v-model:value="record.coins"
                :min="0"
                :precision="0"
                :disabled="record.is_free"
                size="small"
              />
            </template>
          </template>
        </a-table>
      </a-form-item>

      <a-form-item v-if="gameLink" label="游戏链接">
        <a-input :value="gameLink" readonly />
      </a-form-item>
    </a-form>
  </a-spin>
</template>

<style scoped>
.publish-form__intro {
  margin-bottom: 18px;
  padding: 15px 17px;
  border: 1px solid rgba(145, 158, 205, 0.16);
  border-radius: 13px;
  background:
    radial-gradient(240px 100px at 100% 0, rgba(116, 136, 224, 0.13), transparent 72%),
    #202431;
}

.publish-form__intro span {
  display: block;
  margin-bottom: 4px;
  color: #94a2d9;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.14em;
}

.publish-form__intro strong {
  color: #e5e9f3;
  font-size: 16px;
  font-weight: 600;
}

.publish-form__intro p {
  margin: 5px 0 0;
  color: #929aad;
  font-size: 12px;
  line-height: 1.6;
}

.publish-form :deep(.ant-form-item-label > label) {
  color: #bec5d6;
  font-size: 12px;
  font-weight: 600;
}

.publish-form :deep(.ant-input),
.publish-form :deep(.ant-input-affix-wrapper),
.publish-form :deep(.ant-select-selector),
.publish-form :deep(.ant-input-number),
.publish-form :deep(.ant-cascader-picker) {
  border-color: rgba(151, 164, 211, 0.22) !important;
  border-radius: 9px !important;
  background: rgba(15, 18, 26, 0.68) !important;
  color: #e5e9f3 !important;
  box-shadow: none !important;
}

.publish-form :deep(.ant-input:hover),
.publish-form :deep(.ant-input-affix-wrapper:hover),
.publish-form :deep(.ant-select-selector:hover),
.publish-form :deep(.ant-input-number:hover) {
  border-color: rgba(130, 149, 232, 0.58) !important;
}

.publish-form :deep(.ant-input:focus),
.publish-form :deep(.ant-input-affix-wrapper-focused),
.publish-form :deep(.ant-select-focused .ant-select-selector),
.publish-form :deep(.ant-input-number-focused) {
  border-color: #7188e5 !important;
  box-shadow: 0 0 0 2px rgba(113, 136, 229, 0.12) !important;
}

.publish-form :deep(.ant-input::placeholder),
.publish-form :deep(.ant-select-selection-placeholder),
.publish-form :deep(.ant-input-number-input::placeholder) {
  color: #687186 !important;
}

.publish-form :deep(.ant-input-show-count-suffix),
.publish-form :deep(.ant-select-arrow),
.publish-form :deep(.ant-select-clear),
.publish-form :deep(.ant-input-clear-icon) {
  color: #778096;
}

.publish-form :deep(.ant-select-selection-item) {
  color: #dce1ee;
}

.publish-form :deep(.ant-select-multiple .ant-select-selection-item) {
  border-color: rgba(137, 154, 226, 0.24);
  background: rgba(104, 125, 220, 0.16);
  color: #cbd4f7;
}

.publish-form :deep(.ant-select-selection-item-remove) {
  color: #8f99b1;
}

.publish-form :deep(.ant-input-number-input) {
  color: #e5e9f3;
}

.publish-form :deep(.ant-input[readonly]) {
  color: #9ca5b8 !important;
}

.publish-form :deep(.ant-input-number-disabled),
.publish-form :deep(.ant-input-disabled) {
  background: rgba(29, 33, 45, 0.72) !important;
  color: #686f80 !important;
}

.publish-form :deep(.ant-form-item-explain-error) {
  color: #df8f98;
}

.publish-form :deep(.ant-btn-default) {
  border-color: rgba(151, 164, 211, 0.24);
  background: rgba(41, 46, 61, 0.88);
  color: #cbd1e1;
  box-shadow: none;
}

.publish-form :deep(.ant-btn-default:not(:disabled):hover) {
  border-color: rgba(142, 159, 232, 0.56);
  background: rgba(65, 73, 103, 0.76);
  color: #f0f2fa;
}

.publish-form :deep(.ant-btn-link) {
  color: #9dacdf;
}

.publish-form :deep(.ant-table-container) {
  overflow: hidden;
  border-color: rgba(145, 158, 205, 0.18) !important;
  border-radius: 10px;
}

.publish-form :deep(.ant-table-thead > tr > th) {
  border-color: rgba(145, 158, 205, 0.16) !important;
  background: rgba(48, 54, 72, 0.9) !important;
  color: #bcc4d8;
}

.publish-form :deep(.ant-table-tbody > tr > td) {
  border-color: rgba(145, 158, 205, 0.12) !important;
  background: rgba(27, 31, 42, 0.84);
  color: #b8c0d2;
}

.publish-form :deep(.ant-table-tbody > tr.ant-table-row:hover > td) {
  background: rgba(49, 57, 78, 0.82) !important;
}

.publish-cover {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.publish-cover__placeholder {
  display: flex;
  width: 160px;
  height: 90px;
  align-items: center;
  justify-content: center;
  border: 1px dashed rgba(151, 164, 211, 0.3);
  border-radius: 10px;
  background: rgba(15, 18, 26, 0.52);
  color: #788196;
  font-size: 12px;
}

.publish-cover :deep(.ant-image-img) {
  overflow: hidden;
  border: 1px solid rgba(145, 158, 205, 0.18);
  border-radius: 10px;
}

.publish-cover__actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.publish-tag-add {
  display: flex;
  gap: 8px;
  padding: 8px;
}

.publish-hint {
  margin-top: 6px;
  color: #858da2;
  font-size: 12px;
}

.publish-preset-hint {
  margin-top: 7px;
  color: #9eaae0;
  font-size: 12px;
  line-height: 1.5;
}

.publish-options-error {
  display: flex;
  margin-top: 7px;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: #d7a45d;
  font-size: 12px;
  line-height: 1.5;
}

.publish-empty {
  display: flex;
  min-height: 90px;
  align-items: center;
  justify-content: center;
  border: 1px dashed rgba(151, 164, 211, 0.3);
  border-radius: 10px;
  background: rgba(15, 18, 26, 0.52);
  color: #788196;
  font-size: 13px;
}

:global(.publish-form-dropdown) {
  border: 1px solid rgba(145, 158, 205, 0.2);
  background: #202431;
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.42);
}

:global(.publish-form-dropdown .ant-select-item),
:global(.publish-form-dropdown .ant-cascader-menu-item) {
  color: #c8cfde;
}

:global(.publish-form-dropdown .ant-select-item-option-active:not(.ant-select-item-option-disabled)),
:global(.publish-form-dropdown .ant-cascader-menu-item:hover) {
  background: rgba(104, 125, 220, 0.14);
}

:global(.publish-form-dropdown .ant-select-item-option-selected:not(.ant-select-item-option-disabled)),
:global(.publish-form-dropdown .ant-cascader-menu-item-active:not(.ant-cascader-menu-item-disabled)) {
  background: rgba(112, 132, 224, 0.22);
  color: #e1e6f7;
}

:global(.publish-form-dropdown .ant-cascader-menu) {
  border-inline-end-color: rgba(145, 158, 205, 0.14);
}

:global(.publish-form-dropdown .ant-cascader-menu-item-expand-icon),
:global(.publish-form-dropdown .ant-select-item-option-state) {
  color: #9aa8df;
}

:global(.publish-form-dropdown .ant-empty-description) {
  color: #7f8799;
}

:global(.publish-form-dropdown .publish-tag-add .ant-input),
:global(.publish-form-dropdown .publish-tag-add .ant-input-affix-wrapper) {
  border-color: rgba(151, 164, 211, 0.22);
  background: rgba(15, 18, 26, 0.68);
  color: #e5e9f3;
}

:global(.publish-form-dropdown .publish-tag-add .ant-btn-default) {
  border-color: rgba(151, 164, 211, 0.24);
  background: rgba(41, 46, 61, 0.88);
  color: #cbd1e1;
}

:global(.publish-form-dropdown .ant-divider) {
  border-color: rgba(145, 158, 205, 0.14);
}
</style>
