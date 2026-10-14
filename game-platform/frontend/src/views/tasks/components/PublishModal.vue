<script setup>
import { ref, reactive, watch } from 'vue'
import { message } from 'ant-design-vue'
import { getPublishInfoApi, publishCmsApi, getCmsTagsApi, getCmsClassesApi } from '@/api'

const props = defineProps({
  open: { type: Boolean, default: false },
  taskId: { type: String, default: '' },
})
const emit = defineEmits(['update:open', 'published'])

// 分类/标签:从 CMS 拉真实选项(后端代理),弹窗首次打开时加载
const classOptions = ref([])
const tagOptions = ref([])
const optionsLoaded = ref(false)

async function loadCmsOptions() {
  if (optionsLoaded.value) return
  try {
    const [classes, tags] = await Promise.all([getCmsClassesApi(), getCmsTagsApi()])
    classOptions.value = classes.items || []
    tagOptions.value = tags.items || []
    optionsLoaded.value = true
  } catch {
    // 拉取失败提示由 request 拦截器统一弹;下次打开弹窗会重试
  }
}

const loading = ref(false)
const submitting = ref(false)
const coverLoadFailed = ref(false)

const form = reactive({
  name: '',
  description: '',
  cover: '',
  link: '',
  classPath: [], // cascader 选中路径,取末级作为 class_id
  tags: [],
  chapters: [], // { number, title, nodeCount, coins, is_free }
})

/** 打开弹窗时拉取预填数据 */
watch(
  () => props.open,
  async (open) => {
    if (!open || !props.taskId) return
    loading.value = true
    coverLoadFailed.value = false
    form.cover = '' // 清掉上次的封面,避免 <a-image> 拿旧地址/空串先触发一次 error
    loadCmsOptions()
    try {
      const info = await getPublishInfoApi(props.taskId)
      form.name = info.name
      form.description = info.description
      form.cover = info.cover
      form.link = info.link
      form.classPath = []
      form.tags = []
      form.chapters = (info.chapters || []).map((c) => ({
        number: c.number,
        title: c.title,
        nodeCount: (c.nodes || []).length,
        coins: c.coins ?? 0,
        is_free: c.is_free ?? true,
      }))
    } catch {
      // 拉取失败(如任务没跑完没有 story_tree)时关掉弹窗,报错提示由 request 拦截器统一弹
      emit('update:open', false)
    } finally {
      loading.value = false
    }
  },
)

function handleCancel() {
  emit('update:open', false)
}

async function handleSubmit() {
  if (!form.name.trim()) {
    message.warning('请填写游戏名称')
    return
  }
  if (!form.classPath.length) {
    message.warning('请选择游戏分类')
    return
  }
  submitting.value = true
  try {
    const res = await publishCmsApi(props.taskId, {
      name: form.name.trim(),
      description: form.description.trim(),
      class_id: form.classPath.length ? form.classPath[form.classPath.length - 1] : 0,
      tags: form.tags,
      chapters: form.chapters.map((c) => ({
        number: c.number,
        coins: c.is_free ? 0 : c.coins,
        is_free: c.is_free,
      })),
    })
    // CMS 成功码实测有 0 和 1 两种(文档写 1,标签/分类接口实测 0),都认作成功
    if (res.code === 0 || res.code === 1) {
      message.success('发布成功')
      emit('update:open', false)
      emit('published')
    } else {
      message.error(`CMS 返回: ${res.msg || `code=${res.code}`}`)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    title="发布到 CMS"
    width="680px"
    :confirm-loading="submitting"
    ok-text="发布"
    cancel-text="取消"
    @ok="handleSubmit"
    @cancel="handleCancel"
  >
    <a-spin :spinning="loading">
      <a-form layout="vertical">
        <a-row :gutter="16">
          <a-col :span="16">
            <a-form-item label="游戏名称" required>
              <a-input v-model:value="form.name" :maxlength="100" show-count />
            </a-form-item>
            <a-form-item label="游戏描述">
              <a-textarea
                v-model:value="form.description"
                :rows="5"
                :maxlength="2000"
                show-count
              />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="封面(自动)">
              <a-image
                v-if="form.cover && !coverLoadFailed"
                :src="form.cover"
                :width="160"
                @error="coverLoadFailed = true"
              />
              <div v-else-if="coverLoadFailed" class="publish-modal__cover-fallback">封面加载失败</div>
              <div v-else class="publish-modal__cover-fallback">加载中...</div>
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="游戏分类" required>
              <a-cascader
                v-model:value="form.classPath"
                :options="classOptions"
                placeholder="选择分类"
              />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="标签">
              <a-select
                v-model:value="form.tags"
                mode="multiple"
                :options="tagOptions"
                placeholder="选择标签"
              />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="游戏链接(自动)">
          <a-typography-link :href="form.link" target="_blank">{{ form.link }}</a-typography-link>
        </a-form-item>

        <a-form-item label="章节价格">
          <a-table
            :data-source="form.chapters"
            :pagination="false"
            row-key="number"
            size="small"
          >
            <a-table-column title="章节" data-index="title" />
            <a-table-column title="节点数" data-index="nodeCount" :width="80" />
            <a-table-column title="免费" :width="80">
              <template #default="{ record }">
                <a-switch v-model:checked="record.is_free" size="small" />
              </template>
            </a-table-column>
            <a-table-column title="价格(金币)" :width="140">
              <template #default="{ record }">
                <a-input-number
                  v-model:value="record.coins"
                  :min="0"
                  :disabled="record.is_free"
                  style="width: 110px"
                />
              </template>
            </a-table-column>
          </a-table>
        </a-form-item>
      </a-form>
    </a-spin>
  </a-modal>
</template>

<style scoped>
.publish-modal__cover-fallback {
  width: 160px;
  height: 90px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.04);
  border: 1px dashed var(--dr-border);
  color: var(--dr-text-3);
  border-radius: 6px;
  font-size: 12px;
}
</style>
