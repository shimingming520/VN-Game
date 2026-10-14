<template>
  <a-modal
    :open="open"
    :get-container="getModalContainer"
    width="90vw"
    :closable="!saveLoading"
    :keyboard="!saveLoading"
    centered
    destroy-on-close
    :footer="null"
    :mask-closable="false"
    wrap-class-name="plot-tune-modal"
    @cancel="handleCancel"
    @close="handleCancel"
  >
    <template #title>
      <div class="plot-tune__modal-title">
        <span class="plot-tune__modal-title-icon">
          <Icon icon="mdi:movie-edit-outline" />
        </span>
        <span>
          <strong>演出微调</strong>
          <small>调整画面景别、角色站位与素材表现</small>
        </span>
      </div>
    </template>
    <div
      class="plot-tune__body"
    >
      <div class="plot-tune__content">
        <section
          class="plot-tune__chapter"
        >
          <div class="mb-2 flex min-w-0 items-center gap-3">
            <a-tag
              class="!bg-primary/10 !text-primary !m-0 !rounded-full !border-0 !px-4 !py-1 !text-sm !font-bold"
            >
              {{ currentChapter?.chapterLabel || `第${chapterIndex + 1}章` }}
            </a-tag>
            <a-typography-paragraph
              :ellipsis="{ rows: 1, css: true, expandable: false }"
              :title="chapterTitle"
              class="!text-foreground !mb-0 !text-base !font-bold"
            >
              {{ chapterTitle }}
            </a-typography-paragraph>
          </div>

          <a-typography-paragraph
            :ellipsis="{ rows: 2, css: true, expandable: false }"
            class="!text-foreground/70 !mb-0 !text-sm !font-semibold !leading-5"
          >
            {{
              chapterSummary.length > 500
                ? `${chapterSummary.slice(0, 500)}...`
                : chapterSummary
            }}
          </a-typography-paragraph>
        </section>

        <div
          v-if="operationStatusText"
          class="plot-tune__operation-status"
          role="status"
          aria-live="polite"
        >
          <a-spin size="small" />
          <div>
            <strong>{{ operationStatusTitle }}</strong>
            <span>{{ operationStatusText }}</span>
          </div>
        </div>

        <div
          class="plot-tune__main"
        >
          <section class="plot-tune__preview-column">
            <div
              class="text-foreground flex items-center gap-2 text-sm font-bold"
            >
              <Icon
                icon="mdi:play-circle-outline"
                class="text-primary h-5 w-5"
              />
              实时播放
            </div>

            <div
              class="plot-tune__preview"
            >
              <iframe
                v-if="currentChapter?.link"
                ref="primaryPreviewIframeRef"
                :src="primaryPreviewIframeUrl"
                data-testid="preview-iframe-primary"
                title="章节实时播放"
                class="plot-tune__iframe"
                :class="{
                  'is-active': isPrimaryPreviewIframeActive,
                  'is-hidden': !isPrimaryPreviewIframeActive,
                }"
                :aria-hidden="!isPrimaryPreviewIframeActive"
                :tabindex="isPrimaryPreviewIframeActive ? 0 : -1"
                allowfullscreen
              ></iframe>
              <iframe
                v-if="currentChapter?.link"
                ref="secondaryPreviewIframeRef"
                :src="secondaryPreviewIframeUrl || 'about:blank'"
                data-testid="preview-iframe-secondary"
                title="章节实时播放缓冲"
                class="plot-tune__iframe"
                :class="{
                  'is-active': !isPrimaryPreviewIframeActive,
                  'is-hidden': isPrimaryPreviewIframeActive,
                }"
                :aria-hidden="isPrimaryPreviewIframeActive"
                :tabindex="isPrimaryPreviewIframeActive ? -1 : 0"
                allowfullscreen
              ></iframe>
              <div
                v-else
                class="bg-primary/10 flex h-full w-full items-center justify-center"
              >
                <Icon icon="mdi:image-area" class="text-primary/35 h-20 w-20" />
              </div>
            </div>
          </section>

          <div
            class="plot-tune__controls"
          >
            <section class="plot-tune__panel plot-tune__scene-section space-y-3">
              <div class="flex items-center justify-between gap-3">
                <div
                  class="text-foreground flex items-center gap-2 text-sm font-bold"
                >
                  <Icon
                    icon="mdi:movie-open-outline"
                    class="text-primary h-5 w-5"
                  />
                  场景演出
                </div>
                <span
                  v-if="!sceneLoading && !sceneError"
                  class="text-muted-foreground text-xs font-medium"
                >
                  共 {{ sceneSegments.length }} 个场景
                </span>
              </div>

              <div
                v-if="sceneLoading"
                class="border-border bg-card flex min-h-20 items-center justify-center rounded-lg border"
              >
                <a-spin tip="场景加载中..." />
              </div>
              <div v-else-if="sceneError" class="plot-tune__error-state">
                <a-alert :message="sceneError" show-icon type="warning" />
                <a-button size="small" @click="fetchSceneSegments">重新加载</a-button>
              </div>
              <a-empty
                v-else-if="!sceneSegments.length"
                :image="false"
                description="暂无场景数据"
              />
              <div v-else>
                <div
                  class="plot-tune__scene-grid"
                >
                  <a-popover
                    v-for="(segment, index) in sceneSegments"
                    :key="`${segment.scene_id}-${index}`"
                    :trigger="['click', 'focus']"
                    :auto-adjust-overflow="true"
                    overlay-class-name="plot-tune-frame-popover"
                    placement="topRight"
                  >
                    <template #content>
                      <div
                        class="plot-tune-frame-picker"
                      >
                        <div
                          class="border-border/70 mb-3 flex items-center justify-between gap-3 border-b pb-2.5"
                        >
                          <div
                            class="text-foreground flex items-center gap-2 text-sm font-bold"
                          >
                            <span
                              class="bg-primary/10 text-primary flex h-7 w-7 items-center justify-center rounded-lg"
                            >
                              <Icon
                                icon="mdi:image-multiple-outline"
                                class="h-4 w-4"
                              />
                            </span>
                            选择画面
                          </div>
                          <span
                            class="bg-muted text-muted-foreground shrink-0 rounded-full px-2 py-0.5 text-xs font-medium"
                          >
                            共 {{ segment.beats?.length ?? 0 }} 个画面
                          </span>
                        </div>
                        <div
                          v-if="segment.beats?.length"
                          class="plot-tune-frame-scroll"
                        >
                          <div class="plot-tune__beat-list">
                            <button
                              v-for="(beat, beatIndex) in segment.beats"
                              :key="beat"
                              type="button"
                              :title="`画面 ${beatIndex + 1}`"
                              :disabled="saveLoading"
                              :aria-pressed="
                                activeSegmentIndex === index &&
                                selectedBeatId === beat
                              "
                              :data-testid="`beat-${index}-${beat}`"
                              class="plot-tune__beat-card"
                              :class="{
                                'is-active':
                                  activeSegmentIndex === index &&
                                  selectedBeatId === beat,
                              }"
                              @click="handleBeatSelect(index, beat)"
                            >
                              <span
                                aria-hidden="true"
                                class="plot-tune__beat-index"
                              >
                                {{ String(beatIndex + 1).padStart(2, '0') }}
                              </span>
                              <span class="plot-tune__beat-icon">
                                <Icon icon="mdi:movie-outline" />
                              </span>
                              <span class="plot-tune__beat-label">
                                画面 {{ beatIndex + 1 }}
                              </span>
                            </button>
                          </div>
                        </div>
                        <a-empty
                          v-else
                          :image="false"
                          class="!my-2"
                          description="暂无画面"
                        />
                      </div>
                    </template>

                    <div
                      role="group"
                      tabindex="0"
                      :aria-label="`场景 ${index + 1}，共 ${segment.beats?.length ?? 0} 个画面`"
                      :data-testid="`scene-segment-${index}`"
                      class="plot-tune__scene-card"
                      :class="{
                        'is-active': activeSegmentIndex === index,
                      }"
                    >
                      <span class="plot-tune__scene-card-icon">
                        <Icon icon="mdi:movie-open-outline" />
                      </span>
                      <span class="plot-tune__scene-card-copy">
                        <strong>场景 {{ index + 1 }}</strong>
                        <small>{{ segment.beats?.length ?? 0 }} 个画面</small>
                      </span>
                      <Icon icon="mdi:chevron-right" class="plot-tune__scene-card-arrow" />
                    </div>
                  </a-popover>
                </div>

                <div
                  v-if="sceneSegments.length > 6"
                  class="text-muted-foreground relative flex items-center justify-center gap-1 pt-1 text-xs"
                >
                  <span
                    class="from-background pointer-events-none absolute inset-x-0 -top-4 h-4 bg-gradient-to-t to-transparent"
                  ></span>
                  <Icon icon="mdi:chevron-down" class="h-4 w-4" />
                  向下滚动查看更多
                </div>
              </div>
            </section>

            <div
              v-if="beatLoading"
              class="border-border bg-card flex min-h-36 items-center justify-center rounded-lg border"
            >
              <a-spin tip="画面详情加载中..." />
            </div>
            <div v-else-if="beatError" class="plot-tune__error-state">
              <a-alert :message="beatError" show-icon type="warning" />
              <a-button size="small" @click="retryBeatDetail">重新获取画面</a-button>
            </div>
            <a-empty
              v-else-if="!beatDetail"
              :image="false"
              description="请选择画面"
            />
            <template v-else>
              <section class="plot-tune__panel">
                <div
                  class="text-foreground mb-3 flex items-center justify-between gap-2 text-sm font-bold"
                >
                  <span class="flex items-center gap-2">
                    <Icon
                      icon="mdi:tune-variant"
                      class="text-primary h-5 w-5"
                    />
                    操作
                  </span>
                  <a-tag class="plot-tune__frame-tag">画面 {{ selectedFrameNumber }}</a-tag>
                </div>

                <div data-testid="scene-operation-row" class="space-y-3">
                  <div
                    data-testid="scene-shot-row"
                    class="plot-tune__field-row"
                  >
                    <div
                      class="text-foreground/70 w-20 shrink-0 whitespace-nowrap text-sm font-semibold leading-8"
                    >
                      当前景别
                    </div>
                    <a-segmented
                      v-model:value="shotType"
                      :options="shotTypeOptions"
                      :disabled="saveLoading || props.published || !selectedSceneImageModel"
                      :title="!selectedSceneImageModel ? '请先选择场景模型' : ''"
                      block
                      class="bg-muted/60 [&_.ant-segmented-item-selected]:!bg-primary/20 [&_.ant-segmented-item-selected]:!text-primary min-w-[200px] flex-1 [&_.ant-segmented-item-selected]:!font-bold"
                      @change="handleSceneShotChange"
                    />
                  </div>
                  <ImageModelGenerateControl
                    v-if="props.isRenewal"
                    v-model="selectedSceneImageModel"
                    class="w-full whitespace-nowrap"
                    :disabled="saveLoading || props.published"
                    empty-description="暂无场景模型"
                    label="场景模型"
                    :loading="generationTarget === 'scene'"
                    :models="sceneImageModelOptions"
                    @generate="handleSceneGenerate"
                  />
                </div>
              </section>

              <section class="plot-tune__panel">
                <div
                  class="text-foreground mb-4 flex items-center gap-2 text-sm font-bold"
                >
                  <Icon
                    icon="mdi:account-multiple-outline"
                    class="text-primary h-5 w-5"
                  />
                  角色站位和景别
                </div>

                <div
                  v-if="roleSettings.length"
                  data-testid="role-settings-list"
                  class="space-y-3"
                >
                  <div
                    v-for="role in roleSettings"
                    :key="role.key"
                    data-testid="role-setting-row"
                    class="plot-tune__role-card"
                  >
                    <div
                      data-testid="role-setting-fields-row"
                      class="plot-tune__role-fields"
                    >
                      <div
                        class="text-foreground flex min-w-0 items-center gap-2 text-sm font-bold"
                      >
                        <span
                          class="bg-primary/10 text-primary flex h-8 w-8 shrink-0 items-center justify-center rounded-full"
                        >
                          <Icon icon="mdi:account" class="h-4 w-4" />
                        </span>
                        <span class="truncate" :title="role.name">
                          {{ role.name }}
                        </span>
                      </div>

                      <a-select
                        v-model:value="role.position"
                        :options="positionOptions"
                        :disabled="saveLoading || props.published"
                        class="w-full min-w-0"
                        placeholder="请选择站位"
                        @change="handleConfirm"
                      />

                      <a-select
                        v-model:value="role.shot"
                        :options="shotTypeOptions"
                        :disabled="saveLoading || props.published"
                        class="w-full min-w-0"
                        placeholder="请选择景别"
                        @change="handleConfirm"
                      />
                    </div>
                    <ImageModelGenerateControl
                      v-if="props.isRenewal"
                      v-model="role.imageModel"
                      class="w-full whitespace-nowrap"
                      :disabled="saveLoading || props.published"
                      empty-description="暂无图像模型"
                      label="图像模型"
                      :loading="generationTarget === role.key"
                      :models="roleImageModelOptions"
                      @generate="handleRoleGenerate(role)"
                    />
                  </div>
                </div>
                <a-empty v-else description="当前画面无角色" />
              </section>
            </template>
          </div>
        </div>
      </div>
    </div>
  </a-modal>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';

import { message } from 'ant-design-vue';

import {
  getPerformanceBeatApi,
  getPerformanceScenesApi,
  getRegenerateStatusApi,
  regenerateCharacterApi,
  regenerateSceneApi,
  updateGameCastApi,
  updateSceneShotApi,
} from '@/api';
import { authState } from '@/store/auth';

import Icon from '../CompatIcon.vue';
import ImageModelGenerateControl from '../rolesAndScenes/ImageModelGenerateControl.vue';
import type { PlotChapterItem } from './plotCanvasTypes';

/** 演出微调弹窗属性 */
const props = withDefaults(
  defineProps<{
    isRenewal?: boolean;
    published?: boolean;
    taskId?: string;
  }>(),
  {
    isRenewal: false,
    published: false,
    taskId: '',
  },
);

interface PlotTuneInitParams {
  chapter: PlotChapterItem;
  index: number;
}

interface RoleSetting {
  characterKey: string;
  combo: string;
  emotion: string;
  imageModel: string;
  key: string;
  name: string;
  position: string;
  shot: string;
}

/** 场景重生成可选模型 */
const sceneImageModelOptions = ['gpt-image-2', 'wan2.5-t2i-preview'];
/** 角色重生成可选模型 */
const roleImageModelOptions = ['gpt-image-2', 'qwen-image-plus'];
/** 重生成状态轮询间隔 */
const regeneratePollInterval = 5000;
/** 重生成状态轮询超时时间 */
const regenerateTimeoutDuration = 300000;
/** 持续操作提示的固定 key */
const operationMessageKey = 'plot-tune-operation';

const userId = computed(
  () =>
    authState.user?.user_id ||
    authState.user?.id ||
    authState.user?.main_site_uid ||
    '',
);
const userName = computed(() => authState.user?.username || '');
/** 弹窗显示状态 */
const open = ref(false);
/** 当前微调章节 */
const currentChapter = ref<PlotChapterItem | null>(null);
/** 当前章节索引 */
const chapterIndex = ref(0);
/** 当前章节节点 ID */
const currentNodeId = ref('');
/** 场景分段列表 */
const sceneSegments = ref<any[]>([]);
/** 场景列表加载状态 */
const sceneLoading = ref(false);
/** 场景列表错误文案 */
const sceneError = ref('');
/** 当前选中场景分段索引 */
const activeSegmentIndex = ref(-1);
/** 当前选中 beat ID */
const selectedBeatId = ref('');
/** 当前 beat 演出详情 */
const beatDetail = ref<any | null>(null);
/** beat 详情加载状态 */
const beatLoading = ref(false);
/** beat 详情错误文案 */
const beatError = ref('');
/** 当前整体景别 */
const shotType = ref('');
/** 角色站位配置 */
const roleSettings = ref<RoleSetting[]>([]);
/** 主实时预览 iframe */
const primaryPreviewIframeRef = ref<HTMLIFrameElement | null>(null);
/** 备用实时预览 iframe */
const secondaryPreviewIframeRef = ref<HTMLIFrameElement | null>(null);
/** 主实时预览地址 */
const primaryPreviewIframeUrl = ref('');
/** 备用实时预览地址 */
const secondaryPreviewIframeUrl = ref('');
/** 是否显示主实时预览 iframe */
const isPrimaryPreviewIframeActive = ref(true);
/** 演出调整提交状态 */
const saveLoading = ref(false);
/** 当前场景重生成模型 */
const selectedSceneImageModel = ref('');
/** 当前正在重生成的场景或角色标识 */
const generationTarget = ref('');
/** 场景请求序号，用于忽略过期响应 */
let sceneRequestSequence = 0;
/** beat 请求序号，用于忽略过期响应 */
let beatRequestSequence = 0;
/** 演出调整保存请求序号 */
let saveRequestSequence = 0;
/** 重生成请求序号 */
let regenerateRequestSequence = 0;
/** 重生成状态轮询定时器 */
let regeneratePollingTimer: null | ReturnType<typeof setTimeout> = null;
/** 重生成状态轮询截止时间 */
let regeneratePollingDeadline = 0;
/** 当前 iframe load 监听目标 */
let iframeLoadTarget: HTMLIFrameElement | null = null;
/** 当前 iframe load 监听函数 */
let iframeLoadHandler: (() => void) | null = null;

/** 当前显示的实时预览 iframe */
const previewIframeRef = computed(() =>
  isPrimaryPreviewIframeActive.value
    ? primaryPreviewIframeRef.value
    : secondaryPreviewIframeRef.value,
);

/** 景别选项 */
const shotTypeOptions = computed(() => [
  { label: '远景', value: 'LS' },
  { label: '中景', value: 'MS' },
  { label: '近景', value: 'CS' },
]);

/** 站位选项 */
const positionOptions = computed(() => [
  { label: 'Left（左侧）', value: 'left' },
  { label: 'Left Center（左中）', value: 'left_center' },
  { label: 'Center（居中）', value: 'center' },
  { label: 'Right Center（右中）', value: 'right_center' },
  { label: 'Right（右侧）', value: 'right' },
]);

/** 章节标题 */
const chapterTitle = computed(() => {
  return currentChapter.value?.title || currentChapter.value?.name || '--';
});

/** 章节简介 */
const chapterSummary = computed(() => {
  return (
    currentChapter.value?.summary || currentChapter.value?.description || '--'
  );
});

/** 当前选中画面在场景内的显示序号 */
const selectedFrameNumber = computed(() => {
  /** 当前场景包含的画面 ID */
  const beatIds = sceneSegments.value[activeSegmentIndex.value]?.beats || [];
  /** 当前画面在场景内的索引 */
  const frameIndex = beatIds.indexOf(selectedBeatId.value);
  return frameIndex >= 0 ? frameIndex + 1 : '--';
});

/** 当前长耗时操作的标题 */
const operationStatusTitle = computed(() => {
  if (generationTarget.value === 'scene') {
    return '正在重生成场景画面';
  }
  if (generationTarget.value) {
    const role = roleSettings.value.find(
      (item) => item.key === generationTarget.value,
    );
    return `正在重生成${role?.name || '角色'}立绘`;
  }
  return saveLoading.value ? '正在应用演出调整' : '';
});

/** 当前长耗时操作的补充说明 */
const operationStatusText = computed(() => {
  if (generationTarget.value) {
    return '图片生成通常需要 1–5 分钟，完成后会自动刷新画面和预览。';
  }
  return saveLoading.value
    ? '正在写入新设置并刷新游戏预览，请稍候。'
    : '';
});

onBeforeUnmount(() => {
  regenerateRequestSequence += 1;
  stopRegeneratePolling();
  removeIframeLoadListener();
  stopPreviewPlayback();
  clearOperationMessage();
});

/**
 * 初始化演出微调弹窗
 * @param params 弹窗初始化参数
 */
function init(params: PlotTuneInitParams) {
  resetPerformanceState();
  currentChapter.value = params.chapter;
  primaryPreviewIframeUrl.value = params.chapter.link || '';
  secondaryPreviewIframeUrl.value = '';
  isPrimaryPreviewIframeActive.value = true;
  chapterIndex.value = Math.max(params.index, 0);
  currentNodeId.value = getChapterNodeId(params.chapter);
  open.value = true;
  void fetchSceneSegments();
}

/** 获取弹窗挂载容器，全屏时保持弹窗位于全屏元素内 */
function getModalContainer(): HTMLElement {
  return (document.fullscreenElement as HTMLElement | null) ?? document.body;
}

/** 获取章节节点 ID */
function getChapterNodeId(chapter: PlotChapterItem): string {
  return String(chapter?.node_id ?? chapter?.id ?? '');
}

/** 把 Axios 超时和下游错误转成可执行的中文提示 */
function requestErrorText(error: any, fallback: string): string {
  const messageText = String(error?.message || '');
  const isTimeout =
    error?.code === 'ECONNABORTED' ||
    error?.code === 'ETIMEDOUT' ||
    /timeout/i.test(messageText);
  if (isTimeout) {
    return `${fallback}：服务处理较慢，请点击重试`;
  }
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) {
    const normalizedDetail = detail.trim();
    if (normalizedDetail.includes('ultimate 数据尚未生成')) {
      return `${fallback}：当前管线尚未生成演出数据，本章节暂不能微调`;
    }
    return `${fallback}：${normalizedDetail}`;
  }
  return messageText && messageText !== fallback
    ? `${fallback}：${messageText}`
    : fallback;
}

/** 清理不自动消失的长耗时操作消息 */
function clearOperationMessage() {
  message.destroy(operationMessageKey);
}

/** 重置演出微调数据 */
function resetPerformanceState() {
  sceneRequestSequence += 1;
  beatRequestSequence += 1;
  saveRequestSequence += 1;
  regenerateRequestSequence += 1;
  stopRegeneratePolling();
  removeIframeLoadListener();
  clearOperationMessage();
  sceneSegments.value = [];
  sceneLoading.value = false;
  sceneError.value = '';
  activeSegmentIndex.value = -1;
  selectedBeatId.value = '';
  beatDetail.value = null;
  beatLoading.value = false;
  beatError.value = '';
  shotType.value = '';
  roleSettings.value = [];
  saveLoading.value = false;
  selectedSceneImageModel.value = '';
  generationTarget.value = '';
}

/** 查询当前章节的场景分段 */
async function fetchSceneSegments() {
  if (!props.taskId || !currentNodeId.value) {
    sceneError.value = '当前任务或章节不存在';
    message.warning(sceneError.value);
    return;
  }

  /** 当前场景请求序号 */
  const requestSequence = ++sceneRequestSequence;
  sceneLoading.value = true;
  sceneError.value = '';

  try {
    /** 场景分段接口响应 */
    const response = await getPerformanceScenesApi(
      props.taskId,
      currentNodeId.value,
      { skipErrorToast: true },
    );
    if (requestSequence !== sceneRequestSequence) {
      return;
    }

    sceneSegments.value = Array.isArray(response?.segments)
      ? response.segments.map((segment) => ({
          beats: Array.isArray(segment?.beats) ? segment.beats.map(String) : [],
          scene_id: segment?.scene_id || '',
        }))
      : [];
  } catch (error) {
    if (requestSequence !== sceneRequestSequence) {
      return;
    }
    console.error('获取演出微调场景失败:', error);
    sceneError.value = requestErrorText(error, '获取场景数据失败');
  } finally {
    if (requestSequence === sceneRequestSequence) {
      sceneLoading.value = false;
    }
  }
}

/**
 * 选择场景中的 beat
 * @param segmentIndex 场景分段索引
 * @param beatID beat ID
 */
async function handleBeatSelect(segmentIndex: number, beatID: string) {
  if (saveLoading.value) {
    return;
  }

  /** 是否重复点击当前 beat */
  const isCurrentBeat =
    activeSegmentIndex.value === segmentIndex &&
    selectedBeatId.value === beatID;
  if (isCurrentBeat && beatLoading.value) {
    return;
  }
  if (isCurrentBeat && beatDetail.value) {
    seekIframeAtBeat(beatID);
    return;
  }

  activeSegmentIndex.value = segmentIndex;
  selectedBeatId.value = beatID;
  beatDetail.value = null;
  beatError.value = '';
  shotType.value = '';
  roleSettings.value = [];
  await fetchBeatDetail(beatID);
  if (
    activeSegmentIndex.value === segmentIndex &&
    selectedBeatId.value === beatID &&
    beatDetail.value
  ) {
    seekIframeAtBeat(beatID);
  }
}

/**
 * 查询单个 beat 演出状态
 * @param beatID beat ID
 */
async function fetchBeatDetail(beatID: string) {
  if (!props.taskId || !currentNodeId.value) {
    beatError.value = '当前任务或章节不存在';
    message.warning(beatError.value);
    return;
  }

  /** 当前 beat 请求序号 */
  const requestSequence = ++beatRequestSequence;
  beatLoading.value = true;
  beatError.value = '';

  try {
    /** beat 演出状态接口响应 */
    const response = await getPerformanceBeatApi(
      props.taskId,
      currentNodeId.value,
      beatID,
      { skipErrorToast: true },
    );
    if (requestSequence !== beatRequestSequence) {
      return;
    }

    beatDetail.value = response;
    shotType.value = response?.scene_shot_size || '';
    roleSettings.value = Array.isArray(response?.characters)
      ? response.characters.map((character, index) => ({
          characterKey: character?.key || '',
          combo: character?.combo || '',
          emotion: character?.emotion || '',
          imageModel: '',
          key: `${character?.key || 'role'}-${index}`,
          name: character?.key || `角色${index + 1}`,
          position: normalizeCharacterPosition(character?.position || ''),
          shot: character?.shot_size || '',
        }))
      : [];
  } catch (error) {
    if (requestSequence !== beatRequestSequence) {
      return;
    }
    console.error('获取 Beat 演出状态失败:', error);
    beatError.value = requestErrorText(error, '获取画面详情失败');
  } finally {
    if (requestSequence === beatRequestSequence) {
      beatLoading.value = false;
    }
  }
}

/** 重试当前画面详情 */
function retryBeatDetail() {
  if (!selectedBeatId.value || beatLoading.value) {
    return;
  }
  void fetchBeatDetail(selectedBeatId.value);
}

/** 停止重生成状态轮询 */
function stopRegeneratePolling() {
  if (regeneratePollingTimer) {
    clearTimeout(regeneratePollingTimer);
    regeneratePollingTimer = null;
  }
  regeneratePollingDeadline = 0;
}

/**
 * 结束当前重生成操作
 * @param requestSequence 当前重生成请求序号
 */
function finishRegeneration(requestSequence: number) {
  if (requestSequence !== regenerateRequestSequence) {
    return;
  }
  stopRegeneratePolling();
  generationTarget.value = '';
  saveLoading.value = false;
  clearOperationMessage();
}

/** 生成成功后重新加载演出微调场景和当前 Beat 数据 */
async function refreshPerformanceDataAfterGeneration() {
  /** 生成前选中的 Beat ID */
  const beatID = selectedBeatId.value;
  await fetchSceneSegments();
  if (beatID && selectedBeatId.value === beatID) {
    await fetchBeatDetail(beatID);
    const beatIndex = Number(beatID);
    if (Number.isFinite(beatIndex)) {
      refreshIframeAtBeat(beatIndex);
    }
  }
}

/**
 * 轮询重生成状态
 * @param regenerateID 重生成任务 ID
 * @param requestSequence 当前重生成请求序号
 */
async function pollRegenerateStatus(
  regenerateID: string,
  requestSequence: number,
) {
  if (requestSequence !== regenerateRequestSequence) {
    return;
  }
  if (Date.now() >= regeneratePollingDeadline) {
    finishRegeneration(requestSequence);
    message.error('重生成超时，请稍后重试');
    return;
  }

  try {
    /** 重生成状态响应 */
    const response = await getRegenerateStatusApi(regenerateID, {
      skipErrorToast: true,
    });
    if (requestSequence !== regenerateRequestSequence) {
      return;
    }

    if (response?.status === 'success' || response?.status === 'partial') {
      stopRegeneratePolling();
      await refreshPerformanceDataAfterGeneration();
      if (requestSequence !== regenerateRequestSequence) {
        return;
      }
      finishRegeneration(requestSequence);
      if (response.status === 'partial') {
        message.warning('部分图片生成成功，演出微调数据已刷新');
      } else {
        message.success('重生成成功，演出微调数据已刷新');
      }
      return;
    }

    if (response?.status === 'failed' || response?.status === 'error') {
      finishRegeneration(requestSequence);
      message.error('重生成失败');
      return;
    }
  } catch (error: any) {
    console.error('查询重生成状态失败:', error);
    if (error?.response?.status === 404) {
      finishRegeneration(requestSequence);
      message.error('重生成状态已丢失，可能是服务重启，请重新发起生成');
      return;
    }
  }

  if (requestSequence === regenerateRequestSequence) {
    regeneratePollingTimer = setTimeout(() => {
      void pollRegenerateStatus(regenerateID, requestSequence);
    }, regeneratePollInterval);
  }
}

/**
 * 启动重生成状态轮询
 * @param regenerateID 重生成任务 ID
 * @param requestSequence 当前重生成请求序号
 */
function startRegeneratePolling(regenerateID: string, requestSequence: number) {
  stopRegeneratePolling();
  regeneratePollingDeadline = Date.now() + regenerateTimeoutDuration;
  message.loading({
    content: '重生成任务处理中，完成后会自动刷新',
    duration: 0,
    key: operationMessageKey,
  });
  void pollRegenerateStatus(regenerateID, requestSequence);
}

/** 执行当前场景换模型重生成 */
async function handleSceneGenerate() {
  if (props.published) {
    message.warning('作品已发布，不可修改');
    return;
  }
  /** 当前 Beat 使用的场景 ID */
  const sceneID = beatDetail.value?.scene_id || '';
  if (
    saveLoading.value ||
    !props.taskId ||
    !sceneID ||
    !selectedSceneImageModel.value
  ) {
    if (!saveLoading.value) {
      message.warning('当前场景缺少重生成参数');
    }
    return;
  }

  /** 当前场景重生成请求序号 */
  const requestSequence = ++regenerateRequestSequence;
  generationTarget.value = 'scene';
  saveLoading.value = true;
  message.loading({
    content: '正在提交场景重生成任务…',
    duration: 0,
    key: operationMessageKey,
  });
  try {
    /** 场景重生成响应 */
    const response = await regenerateSceneApi({
      task_id: props.taskId,
      material_type: 'scene',
      model_name: selectedSceneImageModel.value,
      scene_ids: [sceneID],
      user_id: userId.value,
      user_name: userName.value,
    }, { skipErrorToast: true });
    if (requestSequence !== regenerateRequestSequence) {
      return;
    }
    if (!response?.regenerate_id) {
      throw new Error('未获取到场景重生成任务ID');
    }
    startRegeneratePolling(response.regenerate_id, requestSequence);
  } catch (error) {
    if (requestSequence !== regenerateRequestSequence) {
      return;
    }
    console.error('场景重生成失败:', error);
    finishRegeneration(requestSequence);
    message.error(requestErrorText(error, '场景重生成失败'));
  }
}

/**
 * 执行当前角色换模型重生成
 * @param role 当前角色配置
 */
async function handleRoleGenerate(role: RoleSetting) {
  if (props.published) {
    message.warning('作品已发布，不可修改');
    return;
  }
  if (
    saveLoading.value ||
    !props.taskId ||
    !currentNodeId.value ||
    !role.characterKey ||
    !role.combo ||
    !role.emotion ||
    !role.imageModel
  ) {
    if (!saveLoading.value) {
      message.warning('当前角色缺少重生成参数');
    }
    return;
  }

  /** 当前角色素材 ID */
  const materialID = `${role.characterKey}_${role.combo}_${role.emotion}.png`;
  /** 当前角色重生成请求序号 */
  const requestSequence = ++regenerateRequestSequence;
  generationTarget.value = role.key;
  saveLoading.value = true;
  message.loading({
    content: `正在提交${role.name || '角色'}立绘重生成任务…`,
    duration: 0,
    key: operationMessageKey,
  });
  try {
    /** 角色重生成响应 */
    const response = await regenerateCharacterApi({
      force: true,
      material_id: materialID,
      model_name: role.imageModel,
      new_prompt: null,
      node_id: currentNodeId.value,
      session_no: null,
      task_id: props.taskId,
      user_id: userId.value,
      user_name: userName.value,
    }, { skipErrorToast: true });
    if (requestSequence !== regenerateRequestSequence) {
      return;
    }
    if (!response?.regenerate_id) {
      throw new Error('未获取到角色重生成任务ID');
    }
    startRegeneratePolling(response.regenerate_id, requestSequence);
  } catch (error) {
    if (requestSequence !== regenerateRequestSequence) {
      return;
    }
    console.error('角色重生成失败:', error);
    finishRegeneration(requestSequence);
    message.error(requestErrorText(error, '角色重生成失败'));
  }
}

/**
 * 保存当前 beat 所在连续场景段的景别
 * @param value 新场景景别
 */
async function handleSceneShotChange(value: number | string) {
  /** 接口要求的场景景别 */
  const sceneShotSize = String(value);
  /** 切换前已保存的场景景别 */
  const previousSceneShotSize = beatDetail.value?.scene_shot_size || '';
  if (props.published) {
    shotType.value = previousSceneShotSize;
    message.warning('作品已发布，不可修改');
    return;
  }
  if (!selectedSceneImageModel.value) {
    shotType.value = previousSceneShotSize;
    message.warning('请先选择场景模型，再切换景别');
    return;
  }
  /** 场景景别是否受支持 */
  const hasValidShot = shotTypeOptions.value.some(
    (option) => option.value === sceneShotSize,
  );

  if (
    !props.taskId ||
    !currentNodeId.value ||
    !selectedBeatId.value ||
    !beatDetail.value
  ) {
    shotType.value = previousSceneShotSize;
    message.warning('请先选择需要调整的画面');
    return;
  }
  if (!hasValidShot) {
    shotType.value = previousSceneShotSize;
    message.warning('请选择有效的场景景别');
    return;
  }
  if (sceneShotSize === previousSceneShotSize) {
    return;
  }

  /** 当前场景景别保存请求序号 */
  const requestSequence = ++saveRequestSequence;
  saveLoading.value = true;
  message.loading({
    content: '正在调整场景景别并刷新预览…',
    duration: 0,
    key: operationMessageKey,
  });
  try {
    /** 场景景别保存响应 */
    const response = await updateSceneShotApi(props.taskId, {
      task_id: props.taskId,
      beat_id: selectedBeatId.value,
      node_id: currentNodeId.value,
      scene_shot_size: sceneShotSize,
    }, { skipErrorToast: true });
    if (requestSequence !== saveRequestSequence) {
      return;
    }

    /** 后端最终写回的场景景别 */
    const savedSceneShotSize = response?.scene_shot_size || sceneShotSize;
    shotType.value = savedSceneShotSize;
    beatDetail.value.scene_shot_size = savedSceneShotSize;
    refreshIframeAtBeat(Number(selectedBeatId.value));
    message.success('场景景别已更新');
  } catch (error) {
    if (requestSequence !== saveRequestSequence) {
      return;
    }
    console.error('保存场景景别失败:', error);
    shotType.value = previousSceneShotSize;
    message.error(requestErrorText(error, '场景景别更新失败'));
  } finally {
    if (requestSequence === saveRequestSequence) {
      saveLoading.value = false;
      clearOperationMessage();
    }
  }
}

/**
 * 将接口站位规范为 PATCH 契约值
 * @param position 接口返回的角色站位
 */
function normalizeCharacterPosition(position: string): string {
  return position
    .trim()
    .replace(/([a-z\d])([A-Z])/g, '$1_$2')
    .replace(/[\s-]+/g, '_')
    .toLowerCase();
}

/** 组装立绘热更新 character_position */
function buildCharacterPosition(): string {
  /** 后端解析的立绘站位列表 */
  const characterList = roleSettings.value.map((role) => {
    /** 当前角色图片 stem */
    const imageStem = [
      role.characterKey,
      role.combo,
      role.emotion,
      role.shot,
    ].join('_');
    return `${imageStem}(${role.position})`;
  });

  return `[${characterList.join(',')}]`;
}

/** 清理实时预览 iframe load 监听 */
function removeIframeLoadListener() {
  if (iframeLoadTarget && iframeLoadHandler) {
    iframeLoadTarget.removeEventListener('load', iframeLoadHandler);
  }
  iframeLoadTarget = null;
  iframeLoadHandler = null;
}

/** 立即卸载两个游戏页面，确保隐藏 iframe 不继续播放音频。 */
function stopPreviewPlayback() {
  primaryPreviewIframeUrl.value = 'about:blank';
  secondaryPreviewIframeUrl.value = 'about:blank';
}

/**
 * 定位实时预览到指定 Beat，不重载 iframe
 * @param beatID Beat 标识
 */
function seekSpecificIframeAtBeat(
  iframe: HTMLIFrameElement | null,
  beatID: number | string,
) {
  /** Beat 数字索引 */
  const beatIndex = Number(beatID);
  if (!Number.isFinite(beatIndex) || !iframe?.src) {
    message.warning('当前画面无法用于游戏预览跳转');
    return;
  }

  try {
    /** iframe 的精确消息目标域 */
    const targetOrigin = new URL(iframe.src, window.location.href).origin;
    iframe.contentWindow?.postMessage(
      {
        data: {
          beat_index: beatIndex,
        },
        type: 'SEEK_TO_BEAT',
      },
      targetOrigin,
    );
  } catch (error) {
    console.error('实时预览跳转 Beat 失败:', error);
    message.warning('实时预览跳转失败');
  }
}

/** 定位当前显示的实时预览到指定 Beat */
function seekIframeAtBeat(beatID: number | string) {
  seekSpecificIframeAtBeat(previewIframeRef.value, beatID);
}

/**
 * 刷新 iframe 并在加载后跳回当前 beat
 * @param beatIndex beat 数字索引
 */
function refreshIframeAtBeat(beatIndex: number) {
  /** 实时预览 iframe */
  const iframe = previewIframeRef.value;
  /** 后台加载下一帧的 iframe */
  const nextIframe = isPrimaryPreviewIframeActive.value
    ? secondaryPreviewIframeRef.value
    : primaryPreviewIframeRef.value;
  if (!iframe?.src || !nextIframe) {
    message.warning('调整已保存，但当前无可刷新的实时预览');
    return;
  }

  /** 携带时间戳的预览地址，避免浏览器复用旧游戏页面缓存 */
  const refreshUrl = new URL(iframe.src, window.location.href);
  refreshUrl.searchParams.set('_refresh', String(Date.now()));

  removeIframeLoadListener();
  iframeLoadTarget = nextIframe;
  iframeLoadHandler = () => {
    const previousPrimaryActive = isPrimaryPreviewIframeActive.value;
    isPrimaryPreviewIframeActive.value = !isPrimaryPreviewIframeActive.value;
    // 明确向刚完成加载的新 iframe 发消息，避免 computed ref 更新时序把 SEEK
    // 错发给随后被置为 about:blank 的旧 iframe。
    seekSpecificIframeAtBeat(nextIframe, beatIndex);
    if (previousPrimaryActive) {
      primaryPreviewIframeUrl.value = 'about:blank';
    } else {
      secondaryPreviewIframeUrl.value = 'about:blank';
    }
    iframeLoadTarget = null;
    iframeLoadHandler = null;
  };
  nextIframe.addEventListener('load', iframeLoadHandler, { once: true });
  if (isPrimaryPreviewIframeActive.value) {
    secondaryPreviewIframeUrl.value = refreshUrl.toString();
  } else {
    primaryPreviewIframeUrl.value = refreshUrl.toString();
  }
}

/** 关闭弹窗 */
function handleCancel() {
  if (saveLoading.value) {
    return;
  }
  sceneRequestSequence += 1;
  beatRequestSequence += 1;
  saveRequestSequence += 1;
  regenerateRequestSequence += 1;
  stopRegeneratePolling();
  removeIframeLoadListener();
  clearOperationMessage();
  saveLoading.value = false;
  generationTarget.value = '';
  stopPreviewPlayback();
  open.value = false;
}

/** 执行当前 Beat 立绘热更新 */
async function handleConfirm() {
  if (props.published) {
    message.warning('作品已发布，不可修改');
    return;
  }
  if (saveLoading.value) {
    return;
  }
  if (
    !props.taskId ||
    !currentNodeId.value ||
    !selectedBeatId.value ||
    !beatDetail.value
  ) {
    message.warning('请先选择需要调整的画面');
    return;
  }

  /** 用于游戏跳转的 beat 数字索引 */
  const beatIndex = Number(selectedBeatId.value);
  if (!Number.isFinite(beatIndex)) {
    message.warning('当前画面无法用于游戏预览跳转');
    return;
  }

  /** 当前立绘热更新请求序号 */
  const requestSequence = ++saveRequestSequence;
  saveLoading.value = true;
  message.loading({
    content: '正在更新角色站位与景别…',
    duration: 0,
    key: operationMessageKey,
  });
  try {
    /** 本次请求期望写入的规范化立绘配置 */
    const requestedCharacterPosition = buildCharacterPosition();
    /** 立绘热更新接口响应 */
    const response = await updateGameCastApi({
      beat_id: selectedBeatId.value,
      character_position: requestedCharacterPosition,
      node_id: currentNodeId.value,
      task_id: props.taskId,
      user_id: userId.value,
      user_name: userName.value,
    }, { skipErrorToast: true });
    if (requestSequence !== saveRequestSequence) {
      return;
    }
    if (response?.status !== 'ok') {
      throw new Error('立绘热更新接口未返回成功状态');
    }
    if (!response.editor_recorded) {
      message.warning('角色站位与景别已更新，但编辑记录写入失败');
    }

    // 重新从游戏包读取当前 beat，而不是只刷新 iframe。这样既能把服务端最终值
    // 回显到控件，也能识别“接口返回成功但文件未真正生效”的异常。
    await fetchBeatDetail(selectedBeatId.value);
    if (!beatDetail.value) {
      throw new Error(beatError.value || '更新后无法读取画面详情');
    }
    const savedCharacterPosition =
      response?.character_position || requestedCharacterPosition;
    if (buildCharacterPosition() !== savedCharacterPosition) {
      throw new Error('更新后回读结果不一致，请重试');
    }
    refreshIframeAtBeat(beatIndex);
    message.success('角色站位与景别已更新');
  } catch (error) {
    if (requestSequence !== saveRequestSequence) {
      return;
    }
    console.error('保存演出微调失败:', error);
    message.error(requestErrorText(error, '角色站位与景别更新失败'));
  } finally {
    if (requestSequence === saveRequestSequence) {
      saveLoading.value = false;
      clearOperationMessage();
    }
  }
}

defineExpose({
  init,
});
</script>

<style scoped>
:global(.plot-tune-modal .ant-modal-content) {
  overflow: hidden;
  border: 1px solid rgba(145, 158, 205, 0.22);
  border-radius: 20px;
  background: #171a24;
  box-shadow: 0 28px 80px rgba(0, 0, 0, 0.48);
}

:global(.plot-tune-modal .ant-modal-header) {
  margin: 0;
  padding: 18px 24px;
  border-bottom: 1px solid rgba(145, 158, 205, 0.14);
  background: linear-gradient(135deg, #242836, #1b1f2b);
}

:global(.plot-tune-modal .ant-modal-title) {
  color: #e8ebf4;
}

:global(.plot-tune-modal .ant-modal-body) {
  padding: 0;
  background: #171a24;
}

:global(.plot-tune-modal .ant-modal-close) {
  top: 20px;
  color: #9199ad;
}

:global(.plot-tune-modal .ant-btn-primary) {
  border-color: #778bea;
  background: linear-gradient(135deg, #7188e5, #846fd6);
  box-shadow: none;
}

:global(.plot-tune-modal .ant-btn-primary:not(:disabled):hover) {
  border-color: #8799ee;
  background: linear-gradient(135deg, #8095eb, #927ee0);
}

:global(.plot-tune-modal .ant-btn-default) {
  border-color: rgba(151, 164, 211, 0.24);
  background: rgba(41, 46, 61, 0.9);
  color: #cbd1e1;
  box-shadow: none;
}

:global(.plot-tune-modal .ant-btn-default:not(:disabled):hover) {
  border-color: rgba(142, 159, 232, 0.56);
  background: rgba(65, 73, 103, 0.8);
  color: #f0f2fa;
}

:global(.plot-tune-modal .ant-select-selector),
:global(.plot-tune-modal .ant-segmented) {
  border-color: rgba(151, 164, 211, 0.22) !important;
  background: rgba(15, 18, 26, 0.72) !important;
  color: #e7eaf5 !important;
  box-shadow: none !important;
}

:global(.plot-tune-modal .ant-segmented-item-selected) {
  background: rgba(112, 132, 224, 0.2) !important;
  color: #b8c4f7 !important;
  box-shadow: none !important;
}

:global(.plot-tune-frame-popover) {
  max-width: calc(100vw - 32px);
}

:global(.plot-tune-frame-popover .ant-popover-inner) {
  max-width: 100%;
  overflow: hidden;
  border: 1px solid rgba(145, 158, 205, 0.22);
  border-radius: 14px;
  background: #202431;
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.42);
}

:global(.plot-tune-frame-popover .ant-popover-arrow::before) {
  background: #202431;
}

.plot-tune__modal-title {
  display: flex;
  align-items: center;
  gap: 12px;
}

.plot-tune__modal-title-icon {
  display: grid;
  width: 40px;
  height: 40px;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid rgba(145, 158, 205, 0.2);
  border-radius: 12px;
  background: rgba(104, 125, 220, 0.14);
  color: #aebaf3;
  font-size: 21px;
}

.plot-tune__modal-title > span:last-child {
  display: grid;
  gap: 2px;
}

.plot-tune__modal-title strong {
  font-size: 17px;
  font-weight: 700;
}

.plot-tune__modal-title small {
  color: #8d95a9;
  font-size: 12px;
  font-weight: 400;
}

.plot-tune__body {
  height: 78vh;
  overflow-y: auto;
  padding: 18px;
  background:
    radial-gradient(620px 240px at 0 0, rgba(103, 122, 211, 0.1), transparent 70%),
    #151821;
}

.plot-tune__content {
  display: flex;
  height: 100%;
  min-height: 0;
  flex-direction: column;
}

.plot-tune__chapter,
.plot-tune__panel {
  padding: 15px 16px;
  border: 1px solid rgba(145, 158, 205, 0.16);
  border-radius: 14px;
  background: #202431;
  color: #dce1ef;
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.18);
}

.plot-tune__chapter {
  position: relative;
  overflow: hidden;
  border-color: rgba(145, 158, 205, 0.18);
  background: linear-gradient(135deg, #252a39, #1e222f 70%);
}

.plot-tune__chapter::after {
  position: absolute;
  top: -60px;
  right: -30px;
  width: 170px;
  height: 130px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(116, 136, 224, 0.16), transparent 70%);
  content: '';
  pointer-events: none;
}

.plot-tune__chapter :deep(.ant-typography) {
  color: #e4e8f3 !important;
}

.plot-tune__chapter :deep(.ant-typography:last-child) {
  color: #9da5b8 !important;
  font-weight: 500 !important;
}

.plot-tune__operation-status {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
  padding: 11px 14px;
  border: 1px solid rgba(137, 154, 226, 0.24);
  border-radius: 12px;
  background: linear-gradient(90deg, rgba(75, 91, 164, 0.24), rgba(40, 46, 67, 0.68));
  color: #dce2f6;
}

.plot-tune__operation-status > div {
  display: grid;
  gap: 2px;
}

.plot-tune__operation-status strong {
  font-size: 13px;
}

.plot-tune__operation-status span {
  color: #9ba4ba;
  font-size: 12px;
}

.plot-tune__main {
  display: grid;
  min-height: 0;
  flex: 1;
  grid-template-columns: minmax(0, 3fr) minmax(340px, 2fr);
  gap: 18px;
  margin-top: 14px;
}

.plot-tune__preview-column {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  gap: 12px;
}

.plot-tune__preview-column > div:first-child {
  color: #d7dceb;
}

.plot-tune__preview {
  position: relative;
  min-height: 360px;
  flex: 1;
  overflow: hidden;
  border: 1px solid rgba(145, 158, 205, 0.18);
  border-radius: 14px;
  background: #292c36;
  box-shadow: 0 10px 26px rgba(45, 53, 73, 0.13), inset 0 0 16px rgba(0, 0, 0, 0.16);
}

.plot-tune__iframe {
  position: absolute;
  width: 100%;
  height: 100%;
  border: 0;
  inset: 0;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}

.plot-tune__iframe.is-active {
  z-index: 10;
  opacity: 1;
  pointer-events: auto;
}

.plot-tune__iframe.is-hidden {
  z-index: 0;
}

.plot-tune__controls {
  display: grid;
  min-width: 0;
  min-height: 0;
  align-content: start;
  gap: 14px;
  overflow-y: auto;
  padding-right: 5px;
  scrollbar-color: #4c5368 transparent;
}

.plot-tune__scene-section {
  padding-bottom: 14px;
}

.plot-tune__scene-section :deep(.text-foreground),
.plot-tune__panel :deep(.text-foreground) {
  color: #dbe0ed !important;
}

.plot-tune__scene-section :deep(.text-muted-foreground),
.plot-tune__panel :deep(.text-muted-foreground) {
  color: #8e97aa !important;
}

.plot-tune__scene-grid {
  display: grid;
  max-height: 184px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 9px;
  overflow-y: auto;
  padding: 1px 3px 2px 1px;
}

.plot-tune__scene-card {
  display: flex;
  min-height: 62px;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border: 1px solid rgba(145, 158, 205, 0.17);
  border-radius: 12px;
  background: #292e3d;
  color: #bcc4d7;
  cursor: pointer;
  font-size: 13px;
  text-align: left;
  transition: border-color 0.18s ease, background 0.18s ease, box-shadow 0.18s ease,
    transform 0.18s ease;
}

.plot-tune__scene-card:hover {
  border-color: rgba(139, 157, 231, 0.48);
  background: #333a50;
  transform: translateY(-1px);
}

.plot-tune__scene-card.is-active {
  border-color: rgba(137, 157, 238, 0.68);
  background: #38415b;
  color: #e2e7fa;
  box-shadow: 0 0 0 3px rgba(106, 128, 226, 0.1);
}

.plot-tune__scene-card-icon {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 10px;
  background: rgba(104, 125, 220, 0.15);
  color: #aebaf3;
  font-size: 17px;
}

.plot-tune__scene-card-copy {
  display: grid;
  min-width: 0;
  flex: 1;
  gap: 2px;
}

.plot-tune__scene-card-copy strong {
  font-size: 13px;
  font-weight: 700;
}

.plot-tune__scene-card-copy small {
  color: #8f98ab;
  font-size: 11px;
}

.plot-tune__scene-card-arrow {
  flex: 0 0 auto;
  color: #76819b;
  font-size: 16px;
}

.plot-tune-frame-picker {
  width: max-content;
  max-width: min(720px, calc(100vw - 4rem));
  overflow: hidden;
  padding: 2px;
  color: #d9deeb;
}

.plot-tune-frame-scroll {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  padding: 2px 2px 8px;
  scrollbar-color: #4c5368 transparent;
}

.plot-tune__beat-list {
  display: flex;
  width: max-content;
  align-items: stretch;
  gap: 10px;
  padding: 1px 2px;
}

.plot-tune__beat-card {
  position: relative;
  display: grid;
  width: 94px;
  min-height: 78px;
  flex: 0 0 auto;
  place-items: center;
  gap: 4px;
  padding: 15px 8px 9px;
  border: 1px solid rgba(145, 158, 205, 0.17);
  border-radius: 12px;
  outline: none;
  background: #292e3d;
  color: #b9c1d4;
  transition: border-color 0.18s ease, background 0.18s ease, box-shadow 0.18s ease,
    transform 0.18s ease;
}

.plot-tune__beat-card:hover {
  border-color: rgba(139, 157, 231, 0.48);
  background: #333a50;
  color: #dce2f7;
  transform: translateY(-1px);
}

.plot-tune__beat-card:focus-visible {
  box-shadow: 0 0 0 3px rgba(119, 139, 232, 0.2);
}

.plot-tune__beat-card.is-active {
  border-color: rgba(137, 157, 238, 0.68);
  background: #38415b;
  color: #e2e7fa;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
}

.plot-tune__beat-card:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.plot-tune__beat-index {
  position: absolute;
  top: 7px;
  left: 8px;
  color: #7f889c;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.plot-tune__beat-icon {
  display: grid;
  width: 29px;
  height: 29px;
  place-items: center;
  border-radius: 9px;
  background: rgba(104, 125, 220, 0.15);
  color: #aebaf3;
  font-size: 16px;
}

.plot-tune__beat-card.is-active .plot-tune__beat-icon {
  background: rgba(112, 132, 224, 0.28);
  color: #d9e0ff;
}

.plot-tune__beat-label {
  max-width: 100%;
  overflow: hidden;
  font-size: 11px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plot-tune__field-row {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 12px;
  padding: 10px;
  border: 1px solid rgba(145, 158, 205, 0.15);
  border-radius: 11px;
  background: #292e3d;
}

.plot-tune__role-card {
  display: grid;
  gap: 12px;
  padding: 13px;
  border: 1px solid rgba(145, 158, 205, 0.15);
  border-radius: 12px;
  background: #292e3d;
}

.plot-tune__role-card + .plot-tune__role-card {
  margin-top: 12px;
}

.plot-tune__role-fields {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(100px, 1fr) minmax(130px, 1fr) minmax(100px, 0.8fr);
  align-items: center;
  gap: 12px;
}

.plot-tune__frame-tag {
  margin: 0;
  border: 1px solid rgba(137, 154, 226, 0.26);
  border-radius: 999px;
  background: rgba(104, 125, 220, 0.14);
  color: #aebaf3;
}

.plot-tune__error-state {
  display: grid;
  gap: 9px;
  justify-items: start;
}

.plot-tune__error-state :deep(.ant-alert) {
  width: 100%;
  border-color: rgba(213, 169, 92, 0.28);
  background: rgba(105, 75, 27, 0.18);
}

.plot-tune__panel :deep(.model-generate) {
  align-items: center;
}

.plot-tune__panel :deep(.model-generate .ant-btn-primary) {
  border-color: #778bea;
  background: linear-gradient(135deg, #7188e5, #846fd6);
}

@media (max-width: 960px) {
  .plot-tune__body {
    height: auto;
    max-height: 76vh;
  }

  .plot-tune__main {
    grid-template-columns: 1fr;
  }

  .plot-tune__preview {
    aspect-ratio: 16 / 9;
    min-height: 0;
  }

  .plot-tune__scene-grid {
    grid-template-columns: 1fr;
  }

  .plot-tune__role-fields {
    grid-template-columns: 1fr;
  }
}
</style>
