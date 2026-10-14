<template>
  <a-modal
    :open="open"
    :title="optionType === 'roles' ? '角色编辑' : '场景编辑'"
    @cancel="handleCancel"
    @close="handleClose"
    :after-close="handleAfterClose"
    :width="800"
    :closable="!isAssetOperationLocked"
    :keyboard="!isAssetOperationLocked"
    destroy-on-close
    :maskClosable="false"
    centered
    :footer="null"
  >
    <!-- Tabs 切换 -->
    <a-tabs v-model:activeKey="activeId" @change="handleTabChange">
      <a-tab-pane
        v-for="item in currentList"
        :key="item.id"
        :tab="item.name"
        :disabled="isAssetOperationLocked"
      >
        <div class="mb-8">
          <h2 class="mb-2 text-lg font-bold text-gray-800">
            {{ item.name }}
          </h2>
          <p class="text-sm leading-relaxed text-gray-600">
            {{ truncatedDescription }}
          </p>
        </div>

        <div class="asset-modal__layout">
          <div class="asset-modal__preview-column">
            <a-button
              v-if="canSwitchPreview"
              shape="circle"
              :disabled="isAssetOperationLocked"
              class="!absolute left-5 top-1/2 z-10 !flex !h-10 !w-10 !-translate-y-1/2 !items-center !justify-center !border-purple-200 !bg-white/95 !text-2xl !font-light !leading-none !text-purple-400 shadow-md transition-all hover:!-translate-x-0.5 hover:!border-purple-400 hover:!text-purple-500 hover:shadow-purple-300/50"
              @click.stop="handlePreviewChange('prev')"
            >
              ‹
            </a-button>

            <a-card
              hoverable
              class="asset-preview-card w-full overflow-hidden !rounded-sm !border-gray-300 shadow-sm transition-all hover:!border-purple-400 hover:shadow-lg hover:shadow-purple-300/50"
              :class="{
                'is-role': optionType === 'roles',
                'is-scene': optionType !== 'roles',
              }"
            >
              <div
                class="asset-preview-card__body"
              >
                <a-spin
                  v-if="loading"
                  size="large"
                  class="!flex h-full w-full items-center justify-center"
                />
                <template v-else>
                  <div
                    v-if="currentPreviewImage"
                    class="relative h-full w-full overflow-hidden bg-gradient-to-b from-slate-100 to-slate-300"
                  >
                    <a-image
                      :key="currentPreviewImageUrl"
                      :src="currentPreviewImageUrl"
                      :alt="`${currentRoleInfo?.name || (optionType === 'roles' ? '角色' : '场景')}图片`"
                      :wrapper-style="{ height: '100%', width: '100%' }"
                      wrapper-class-name="block h-full w-full"
                      decoding="async"
                      loading="lazy"
                      class="block h-full w-full object-cover object-top transition-opacity duration-300"
                      @error="handlePreviewImageError(currentPreviewImageUrl)"
                    >
                      <template #placeholder>
                        <div
                          role="status"
                          class="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-slate-100 text-xs font-medium text-slate-400"
                        >
                          <span
                            class="h-10 w-10 animate-pulse rounded-full bg-slate-200"
                          />
                          图片加载中...
                        </div>
                      </template>
                    </a-image>

                    <div
                      v-if="currentPreviewImageLoadState === 'error'"
                      class="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-gray-50 px-3 text-center"
                    >
                      <span class="text-xs font-medium text-gray-400">
                        图片加载失败
                      </span>
                      <button
                        type="button"
                        data-testid="preview-image-retry"
                        :disabled="isAssetOperationLocked"
                        class="border-primary/35 text-primary hover:bg-primary/10 rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50"
                        @click.stop="handlePreviewImageRetry"
                      >
                        重新加载
                      </button>
                    </div>
                  </div>
                  <a-empty
                    v-else
                    class="asset-preview-empty flex h-full items-center justify-center"
                    :description="
                      optionType === 'roles'
                        ? `${currentRoleInfo?.name || '人物'}AI绘`
                        : `${currentRoleInfo?.name || '场景'}AI绘`
                    "
                  />
                </template>
              </div>
            </a-card>

            <a-button
              v-if="canSwitchPreview"
              shape="circle"
              :disabled="isAssetOperationLocked"
              class="!absolute right-5 top-1/2 z-10 !flex !h-10 !w-10 !-translate-y-1/2 !items-center !justify-center !border-purple-200 !bg-white/95 !text-2xl !font-light !leading-none !text-purple-400 shadow-md transition-all hover:!translate-x-0.5 hover:!border-purple-400 hover:!text-purple-500 hover:shadow-purple-300/50"
              @click.stop="handlePreviewChange('next')"
            >
              ›
            </a-button>

            <div class="absolute right-2 top-2 z-10 flex items-center gap-2">
              <!-- 历史生图 -->
              <Icon
                icon="mdi:history"
                :aria-disabled="isAssetOperationLocked"
                :class="
                  isAssetOperationLocked
                    ? 'cursor-not-allowed opacity-50'
                    : 'cursor-pointer hover:-translate-y-0.5 hover:text-purple-600'
                "
                class="asset-modal__history"
                @click.stop="handleIconClick('history')"
              />

              <!-- 素材库 -->
              <!-- <Icon
                icon="mdi:import"
                class="h-7 w-7 cursor-pointer text-purple-500 drop-shadow-[0_2px_4px_rgba(139,92,246,0.18)] transition-all duration-200 hover:-translate-y-0.5 hover:text-purple-600"
                @click.stop="handleIconClick('library')"
              /> -->
            </div>
          </div>

          <div
            class="asset-modal__form-column"
          >
            <a-form layout="vertical" class="asset-detail-form shrink-0">
              <a-form-item
                :label="optionType === 'roles' ? '人物名称+状态' : '场景名称'"
              >
                <a-input
                  v-model:value="formData.nameStatus"
                  disabled
                  :placeholder="
                    optionType === 'roles' ? '人物名称-基础形象' : '场景名称'
                  "
                />
              </a-form-item>

              <a-form-item label="出现集数">
                <a-input
                  v-model:value="formData.episodes"
                  disabled
                  placeholder=""
                />
              </a-form-item>

              <a-form-item
                :label="optionType === 'roles' ? '形象描述' : '场景描述'"
              >
                <div
                  class="rounded-sm border border-gray-300 bg-white p-2 transition-colors focus-within:border-purple-400"
                >
                  <a-textarea
                    v-model:value="formData.description"
                    :placeholder="
                      optionType === 'roles'
                        ? '请输入人物描述'
                        : '请输入场景描述'
                    "
                    :rows="5"
                    :bordered="false"
                    :disabled="isAssetMutationLocked"
                    class="resize-none"
                  />
                  <ImageModelGenerateControl
                    v-model="selectedImageModel"
                    class="mt-2"
                    :disabled="isAssetMutationLocked"
                    :empty-description="
                      optionType === 'roles' ? '暂无图像模型' : '暂无场景模型'
                    "
                    :label="optionType === 'roles' ? '图像模型' : '场景模型'"
                    :loading="loading"
                    :models="imageModelOptions"
                    @generate="handleGenerate"
                  />
                </div>
              </a-form-item>
            </a-form>

            <!-- 音色选择 -->
            <div v-if="shouldShowVoiceSelection" class="min-h-0 space-y-3 pr-1">
              <div>音色选择</div>
              <div
                class="max-h-[400px] overflow-y-auto rounded-sm border border-gray-300 bg-white p-1"
              >
                <div
                  v-if="voiceLoading"
                  class="flex min-h-[120px] items-center justify-center"
                >
                  <a-spin />
                </div>
                <a-empty
                  v-else-if="voiceLoadError"
                  description="音色加载失败"
                />
                <a-empty
                  v-else-if="currentVoiceOptions.length === 0"
                  description="暂无推荐音色"
                />
                <template v-else>
                  <div
                    v-for="voice in currentVoiceOptions"
                    :key="voice.voice_id"
                    :aria-disabled="isAssetOperationLocked"
                    class="relative mb-3 flex h-[72px] items-center gap-6 rounded-sm border px-4 transition-all last:mb-0"
                    :class="[
                      selectedVoiceKey === voice.voice_id
                        ? 'border-2 border-purple-400 bg-purple-50 shadow-lg shadow-purple-300/50'
                        : 'border-gray-300',
                      isAssetOperationLocked
                        ? 'cursor-not-allowed opacity-70'
                        : 'cursor-pointer hover:border-purple-400 hover:shadow-md hover:shadow-purple-300/30',
                    ]"
                    @click="handleVoiceSelect(voice.voice_id)"
                  >
                    <div class="flex-1 truncate font-semibold text-gray-700">
                      {{ voice.display_name }}
                    </div>
                    <a-button
                      type="primary"
                      class="!px-6"
                      :disabled="isAssetOperationLocked"
                      :loading="auditionLoadingVoiceId === voice.voice_id"
                      @click.stop="handleVoiceAudition(voice)"
                    >
                      <template #icon>
                        <Icon
                          :icon="
                            auditionVoiceId === voice.voice_id &&
                            isAuditionPlaying
                              ? 'mdi:pause'
                              : 'mdi:play'
                          "
                        />
                      </template>
                      {{
                        auditionVoiceId === voice.voice_id && isAuditionPlaying
                          ? '暂停'
                          : '试听'
                      }}
                    </a-button>
                    <a-button
                      type="primary"
                      class="!px-6"
                      :loading="applyingVoiceId === voice.voice_id"
                      :disabled="
                        isAssetMutationLocked ||
                        currentVoiceId === voice.voice_id
                      "
                      @click.stop="handleVoiceApply(voice)"
                    >
                      {{
                        applyingVoiceId === voice.voice_id ? '正在应用' : '应用'
                      }}
                    </a-button>
                    <span
                      v-if="selectedVoiceKey === voice.voice_id"
                      class="absolute -right-1 -top-1 flex h-6 w-6 items-center justify-center rounded-full bg-purple-400 text-xs font-bold text-white"
                      >✓</span
                    >
                  </div>
                </template>
              </div>
            </div>
          </div>
        </div>
      </a-tab-pane>
    </a-tabs>
  </a-modal>

  <!-- 历史生图、素材库 -->
  <AssetImageSelectModal
    ref="assetImageSelectModalRef"
    :get-timestamped-image-url="props.getTimestampedImageUrl"
    :published="props.published"
    @select="handleAssetImageSelect"
  />
</template>

<script lang="ts" setup>
import { ref, reactive, computed, watch, onUnmounted, nextTick } from 'vue';
import { message } from 'ant-design-vue';
import {
  activateRegenVersionApi,
  getRegenHistoryApi,
  getRegenerateStatusApi,
  getVoiceRecommendApi,
  generateVoiceAuditionApi,
  regenerateCharacterApi,
  regenerateSceneApi,
  regenerateVoiceApi,
} from '@/api';
import { authState } from '@/store/auth';
import Icon from '../CompatIcon.vue';
import ImageModelGenerateControl from './ImageModelGenerateControl.vue';
import AssetImageSelectModal from './AssetImageSelectModal.vue';

const props = defineProps<{
  getTimestampedImageUrl: (imageUrl?: string) => string;
  published?: boolean;
  taskId?: string;
}>();

/** 通知父组件刷新资源列表 */
const emit = defineEmits<{
  success: [];
}>();

const userId = computed(
  () =>
    authState.user?.user_id ||
    authState.user?.id ||
    authState.user?.main_site_uid ||
    '',
);
const userName = computed(() => authState.user?.username || '');

const loading = ref(false); // 加载中
const open = ref(false);
/** 本次打开弹窗后是否成功启动过图片生成任务 */
const hasGeneratedInCurrentOpen = ref(false);
/** 本次打开弹窗后是否成功激活过资源版本 */
const hasActivatedVersionInCurrentOpen = ref(false);
const optionType = ref('roles');
const materialId = ref(''); // 当前角色素材id
const nodeId = ref(''); // 当前章节节点id
const sceneId = ref<string[]>([]); // 当前场景素材id

interface AssetVariant {
  episode_nos?: Array<number | string>;
  image?: string;
  material_id?: string;
  material_version?: number;
  node_id?: string;
  state?: string;
  status?: string;
  url?: string;
  [key: string]: any;
}

/** 预览图片加载状态 */
type PreviewImageLoadState = 'error' | 'loading';

const activePreviewIndex = ref(0); // 当前预览形象索引
/** 预览图片加载状态 */
const previewImageLoadStateMap = ref<Record<string, PreviewImageLoadState>>({});
/** 预览图片单图重试次数 */
const previewImageRetryMap = ref<Record<string, number>>({});
const selectedVoiceKey = ref(''); // 当前选中的音色
const currentVoiceId = ref(''); // 当前正在使用的音色ID
const currentVoiceOptions = ref<VoiceOption[]>([]); // 当前推荐音色选项
const voiceLoading = ref(false); // 推荐音色加载状态
const voiceLoadError = ref(false); // 推荐音色加载失败状态
const auditionVoiceId = ref(''); // 当前试听音色ID
const isAuditionPlaying = ref(false); // 当前试听音色是否正在播放
const applyingVoiceId = ref(''); // 当前正在应用的音色ID
const auditionLoadingVoiceId = ref(''); // 当前正在生成试听音频的音色ID
/** 当前选中的图像模型 */
const selectedImageModel = ref('');

/** 根据资源类型显示对应的图像模型选项 */
const imageModelOptions = computed(() =>
  optionType.value === 'roles'
    ? ['gpt-image-2', 'qwen-image-plus']
    : ['gpt-image-2', 'wan2.5-t2i-preview'],
);

/** 是否锁定资产操作，音色应用时不触发图片加载状态 */
const isAssetOperationLocked = computed(
  () => loading.value || Boolean(applyingVoiceId.value),
);
const isAssetMutationLocked = computed(
  () => props.published || isAssetOperationLocked.value,
);

// 轮询相关变量
let pollingTimer: ReturnType<typeof setInterval> | null = null;
let pollingTimeout: ReturnType<typeof setTimeout> | null = null;
let voiceRequestId = 0; // 推荐音色请求批次标识
let auditionAudio: HTMLAudioElement | null = null; // 当前试听音频实例

// 表单数据
const formData = reactive({
  nameStatus: '',
  episodes: '',
  description: '',
});

/**
 * 停止轮询
 */
function stopPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer);
    pollingTimer = null;
  }
  if (pollingTimeout) {
    clearTimeout(pollingTimeout);
    pollingTimeout = null;
  }
  applyingVoiceId.value = '';
}

/**
 * 开始轮询检查重生成状态
 * @param regenerateId 重生成任务ID
 */
function startPolling(regenerateId: string, submittedPrompt = '') {
  // const elapsedTime = 0; // 已过去的时间（秒）- 目前未直接使用，但保留概念
  const POLL_INTERVAL = 5000; // 轮询间隔：5秒
  const TIMEOUT_DURATION = 300000; // 超时时间：300秒-5分钟
  /** 轮询开始时选中的资源类型 */
  const pollingTargetType = optionType.value;
  /** 轮询开始时选中的角色形象 */
  const pollingTargetVariant = currentPreviewVariant.value;
  /** 轮询开始时选中的角色/场景，避免切换后更新错对象 */
  const pollingTargetAsset = currentRoleInfo.value;
  /** 本次真正提交给后端的提示词 */
  const pollingPrompt = submittedPrompt.trim();

  // 立即执行一次检查
  checkStatus();

  // 设置定时器，每5秒检查一次
  pollingTimer = setInterval(checkStatus, POLL_INTERVAL);

  // 设置超时定时器
  pollingTimeout = setTimeout(() => {
    stopPolling();
    loading.value = false;
    message.error('重生成超时，请稍后重试');
  }, TIMEOUT_DURATION);

  /**
   * 检查重生成状态
   */
  async function checkStatus() {
    try {
      const statusRes = await getRegenerateStatusApi(regenerateId, {
        skipErrorToast: true,
      });

      const isVoiceApply = Boolean(applyingVoiceId.value); // 是否为音色应用操作
      // 如果状态为 success，停止轮询
      if (statusRes.status === 'success') {
        /** 成功应用的音色ID */
        const appliedVoiceId = applyingVoiceId.value;
        if (appliedVoiceId) {
          selectedVoiceKey.value = appliedVoiceId;
          currentVoiceId.value = appliedVoiceId;
        }
        /** 重生成成功后的图片地址 */
        const regeneratedImagePath = statusRes?.items?.[0]?.path;
        if (!isVoiceApply && regeneratedImagePath) {
          if (pollingTargetType === 'roles' && pollingTargetVariant) {
            pollingTargetVariant.image = regeneratedImagePath;
            if (pollingPrompt) {
              const targetCombination = pollingTargetVariant.combination || '';
              for (const variant of pollingTargetAsset?.status || []) {
                if (!targetCombination || variant.combination === targetCombination) {
                  variant.positive_prompt = pollingPrompt;
                }
              }
              pollingTargetVariant.positive_prompt = pollingPrompt;
              pollingTargetAsset.positive_prompt = pollingPrompt;
            }
          } else if (pollingTargetType === 'scenes') {
            pollingTargetAsset.image = regeneratedImagePath;
            if (pollingPrompt) pollingTargetAsset.positive_prompt = pollingPrompt;
          }
          hasGeneratedInCurrentOpen.value = true;
        }
        stopPolling();
        loading.value = false;
        message.success(isVoiceApply ? '应用成功' : '重生成成功');
      }
      // 如果状态为 failed 或其他错误状态，也可以停止轮询
      else if (statusRes.status === 'failed' || statusRes.status === 'error') {
        stopPolling();
        loading.value = false;
        message.error(isVoiceApply ? '应用失败' : '重生成失败');
      }
      // 其他状态继续轮询
    } catch (error: any) {
      console.error('查询重生成状态失败:', error);
      if (error?.response?.status === 404) {
        stopPolling();
        loading.value = false;
        message.error('重生成状态已丢失，可能是服务重启，请重新发起生成');
      }
    }
  }
}

interface AssetItem {
  id?: string | number;
  name?: string;
  image?: string;
  speaker_id?: string;
  status?: AssetVariant[];
  [key: string]: any;
}

interface VoiceOption {
  age_category: string;
  audition_url: string;
  description: string;
  display_name: string;
  gender: string;
  tags: string[];
  voice_id: string;
}

interface AssetImageOption {
  assetId?: string | number;
  id: string;
  image?: string;
  isActive?: boolean;
  materialId?: string;
  materialVersion?: number;
  name: string;
  nodeId?: string;
  previewIndex?: number;
  ratio: '16:9' | '4:3';
}

type AssetImageSourceType = 'history' | 'library';

const currentList = ref<AssetItem[]>([]); // 当前角色/场景列表
const activeId = ref<number | string>(0); // 当前角色/场景ID
const assetImageSelectModalRef = ref<InstanceType<
  typeof AssetImageSelectModal
> | null>(null); // 图片选择弹窗引用

// 计算当前选中的角色/场景信息
const currentRoleInfo = computed(() => {
  return (
    currentList.value.find((item) => item?.id === activeId.value) || {
      id: '',
      name: '',
      image: '',
      status: [],
    }
  );
});

// 截取描述文本，最多200字
const truncatedDescription = computed(() => {
  const desc =
    currentPreviewVariant.value?.positive_prompt ||
    currentRoleInfo.value.positive_prompt ||
    currentRoleInfo.value.image_prompt ||
    '';
  if (desc.length <= 200) {
    return desc;
  }
  return desc.substring(0, 200) + '...';
});

// 当前角色的形象列表
const currentRoleVariants = computed(() => {
  return optionType.value === 'roles'
    ? currentRoleInfo.value?.status || []
    : [];
});

// 是否可以切换预览形象
const canSwitchPreview = computed(() => currentRoleVariants.value.length > 1);

// 当前预览形象数据
const currentPreviewVariant = computed<AssetVariant | null>(() => {
  if (optionType.value === 'roles') {
    return currentRoleVariants.value[activePreviewIndex.value] || null;
  }

  return {
    episode_nos: currentRoleInfo.value?.episode_nos,
    image: currentRoleInfo.value?.image,
    material_id: currentRoleInfo.value?.material_id,
    node_id: currentRoleInfo.value?.node_id,
  };
});

// 当前预览图片
const currentPreviewImage = computed(() => {
  const image =
    optionType.value === 'roles'
      ? currentPreviewVariant.value?.image
      : currentRoleInfo.value?.image;

  return image || '';
});

/** 当前预览图片请求地址 */
const currentPreviewImageUrl = computed(() => {
  const imageUrl = props.getTimestampedImageUrl(currentPreviewImage.value);
  const retryCount = previewImageRetryMap.value[imageUrl] || 0;

  if (!imageUrl || !retryCount) {
    return imageUrl;
  }

  const separator = imageUrl.includes('?') ? '&' : '?';
  return `${imageUrl}${separator}retry=${retryCount}`;
});

/** 当前预览图片加载状态 */
const currentPreviewImageLoadState = computed<PreviewImageLoadState>(() => {
  return (
    previewImageLoadStateMap.value[currentPreviewImageUrl.value] || 'loading'
  );
});

/** 是否显示音色选择 */
const shouldShowVoiceSelection = computed(() => {
  return (
    optionType.value === 'roles' &&
    currentPreviewVariant.value?.status === 'calm' &&
    Boolean(currentRoleInfo.value?.speaker_id)
  );
});

watch(
  [activeId, currentList, optionType],
  () => {
    activePreviewIndex.value = 0;
    syncCurrentAssetForm();
  },
  { immediate: true },
);

watch(activePreviewIndex, () => {
  syncCurrentAssetForm();
});

// 同步当前资源到右侧表单
function syncCurrentAssetForm() {
  const currentItem = currentRoleInfo.value;
  const currentVariant = currentPreviewVariant.value;

  if (optionType.value === 'roles') {
    if (!currentVariant) {
      formData.nameStatus = currentItem?.name || '';
      formData.episodes = currentItem?.episode_nos?.join('、') || '暂无记录';
      formData.description = currentItem?.positive_prompt || '';
      return;
    }

    formData.nameStatus =
      (currentItem?.name || '') +
      (currentVariant?.status ? '-' + currentVariant.status : '');
    formData.episodes =
      currentVariant?.episode_nos?.join('、') ||
      currentItem?.episode_nos?.join('、') ||
      '暂无记录';
    formData.description =
      currentVariant?.positive_prompt || currentItem?.positive_prompt || '';
    materialId.value = currentVariant?.material_id ?? '';
    nodeId.value = currentVariant?.node_id ?? '';
    return;
  }

  formData.nameStatus = currentItem?.name || '';
  formData.episodes = currentVariant?.episode_nos?.join('、') || '暂无记录';
  formData.description =
    currentItem?.positive_prompt || currentItem?.image_prompt || '';
  materialId.value = currentVariant?.material_id ?? '';
  nodeId.value = currentVariant?.node_id ?? '';
  sceneId.value = currentItem?.scene_id ? [String(currentItem.scene_id)] : [];
}

// 切换左侧预览形象
function handlePreviewChange(direction: 'next' | 'prev') {
  const variantCount = currentRoleVariants.value.length;

  if (variantCount <= 1 || isAssetOperationLocked.value) return;

  const nextIndex =
    direction === 'next'
      ? (activePreviewIndex.value + 1) % variantCount
      : (activePreviewIndex.value - 1 + variantCount) % variantCount;

  activePreviewIndex.value = nextIndex;
  handleCurrentVoiceOptions();
}

/**
 * 标记预览图片加载失败
 * @param imageUrl 当前图片请求地址
 */
function handlePreviewImageError(imageUrl: string) {
  if (!imageUrl) return;
  previewImageLoadStateMap.value[imageUrl] = 'error';
}

/** 重试加载当前预览图片 */
function handlePreviewImageRetry() {
  const imageUrl = props.getTimestampedImageUrl(currentPreviewImage.value);
  if (!imageUrl) return;
  previewImageRetryMap.value[imageUrl] =
    (previewImageRetryMap.value[imageUrl] || 0) + 1;
}

/**
 * 生成角色图片选项
 * @param item 角色资源
 * @param variant 角色形象
 * @param index 形象索引
 */
function buildRoleImageOption(
  item: AssetItem,
  variant: AssetVariant,
  index: number,
): AssetImageOption {
  const assetId = item?.id;
  const imageId = variant?.material_id || `${assetId || 'role'}-${index}`;

  return {
    assetId,
    id: String(imageId),
    image: variant?.image || item?.image || '',
    isActive: variant?.is_active === true,
    materialId: variant?.material_id,
    materialVersion: variant?.material_version,
    name: `${item?.name || '人物'}形象${index + 1}`,
    nodeId: variant?.node_id,
    previewIndex: index,
    ratio: '16:9',
  };
}

/**
 * 生成场景图片选项
 * @param item 场景资源
 * @param index 场景索引
 */
function buildSceneImageOption(
  item: AssetItem,
  index: number,
): AssetImageOption {
  const assetId = item?.id;

  return {
    assetId,
    id: String(assetId ?? `scene-${index}`),
    image: item?.image || '',
    isActive: item?.is_active === true,
    materialId: item?.material_id,
    materialVersion: item?.material_version,
    name: item?.name || `场景${index + 1}`,
    nodeId: item?.node_id,
    ratio: '4:3',
  };
}

/**
 * 获取历史生图图片列表
 */
async function getHistoryImageOptions(): Promise<AssetImageOption[]> {
  if (!props.taskId) {
    message.error('任务ID不存在');
    return [];
  }

  try {
    /** 历史生图接口响应 */
    const response = await getRegenHistoryApi(props.taskId, {
      material_id: currentPreviewVariant.value?.material_id,
      node_id: currentPreviewVariant.value?.node_id,
    });
    /** 历史生图记录 */
    const historyRecords: AssetVariant[] = Array.isArray(response?.records)
      ? response.records
      : [];

    return historyRecords.map((record, index) => ({
      assetId: currentRoleInfo.value?.id,
      id: `${record.material_id || 'history'}-${record.material_version ?? index}`,
      image: record.url || '',
      isActive: record.is_active === true,
      materialId: record.material_id,
      materialVersion: record.material_version,
      name: `版本 ${record.material_version ?? index + 1}`,
      nodeId: record.node_id,
      previewIndex: activePreviewIndex.value,
      ratio: optionType.value === 'roles' ? '4:3' : '16:9',
    }));
  } catch (error) {
    console.error('获取历史生图失败:', error);
    return [];
  }
}

/**
 * 获取素材库图片列表
 */
function getLibraryImageOptions(): AssetImageOption[] {
  if (optionType.value !== 'roles') {
    return currentList.value.map((item, index) =>
      buildSceneImageOption(item, index),
    );
  }

  return currentList.value.flatMap((item) => {
    const variants = item?.status || [];

    if (!variants.length) {
      return [
        {
          assetId: item?.id,
          id: String(item?.id ?? item?.name ?? 'role'),
          image: item?.image || '',
          isActive: item?.is_active === true,
          materialId: item?.material_id,
          materialVersion: item?.material_version,
          name: item?.name || '人物形象',
          nodeId: item?.node_id,
          previewIndex: 0,
          ratio: '4:3' as const,
        },
      ];
    }

    return variants.map((variant, index) =>
      buildRoleImageOption(item, variant, index),
    );
  });
}

/**
 * 获取当前选中图片ID
 */
function getSelectedImageId(): string {
  if (optionType.value === 'roles') {
    const currentVariant = currentPreviewVariant.value;
    return String(
      currentVariant?.material_id ||
        `${currentRoleInfo.value?.id || 'role'}-${activePreviewIndex.value}`,
    );
  }

  return String(currentRoleInfo.value?.id ?? '');
}

/**
 * 打开图片选择弹窗
 * @param sourceType 图片来源类型
 */
async function openAssetImageSelectModal(sourceType: AssetImageSourceType) {
  const items =
    sourceType === 'history'
      ? await getHistoryImageOptions()
      : getLibraryImageOptions();

  assetImageSelectModalRef.value?.init({
    items,
    selectedId: sourceType === 'history' ? '' : getSelectedImageId(),
    sourceType,
    targetType: optionType.value === 'roles' ? 'roles' : 'scenes',
  });
}

/**
 * 点击生图素材选择框
 * @param type 点击图标类型
 */
async function handleIconClick(type: AssetImageSourceType) {
  if (isAssetOperationLocked.value) {
    return;
  }

  await openAssetImageSelectModal(type);
}

/**
 * 选择弹窗图片后同步当前预览
 * @param item 当前选择的图片项
 */
async function handleAssetImageSelect(item: AssetImageOption) {
  if (props.published) {
    message.warning('作品已发布，不可修改');
    return;
  }
  if (!props.taskId) {
    message.error('任务ID不存在');
    return;
  }
  if (item.materialVersion === undefined || !item.nodeId || !item.materialId) {
    message.warning('当前形象缺少版本信息');
    return;
  }

  const paramsData = {
    task_id: props.taskId,
    // task_id: 'c70c14c52290',
    target_material_version: item.materialVersion,
    node_id: item.nodeId,
    material_id: item.materialId,
  }; // 激活指定版本请求参数

  try {
    await activateRegenVersionApi(props.taskId, paramsData);
    assetImageSelectModalRef.value?.setSelectedId(item.id);
    hasActivatedVersionInCurrentOpen.value = true;
    message.success('激活成功');
  } catch (error) {
    console.error('激活指定版本失败:', error);
    return;
  }

  if (item.assetId !== undefined && activeId.value !== item.assetId) {
    activeId.value = item.assetId;
    await nextTick();
  }

  if (optionType.value === 'roles') {
    activePreviewIndex.value = item.previewIndex || 0;
    if (item.image && currentPreviewVariant.value) {
      currentPreviewVariant.value.image = item.image;
    }
    syncCurrentAssetForm();
    handleCurrentVoiceOptions();
    return;
  }

  if (item.image) {
    currentRoleInfo.value.image = item.image;
  }
}

// 选择音色
function handleVoiceSelect(voiceId: string) {
  if (isAssetOperationLocked.value) return;
  selectedVoiceKey.value = voiceId;
}

/**
 * 应用当前音色
 * @param voice 当前音色数据
 */
async function handleVoiceApply(voice: VoiceOption) {
  if (props.published) {
    message.warning('作品已发布，不可修改');
    return;
  }
  const speakerId = currentRoleInfo.value?.speaker_id; // 当前角色说话人ID
  if (!speakerId) {
    message.warning('当前角色缺少说话人ID');
    return;
  }

  stopVoiceAudition();
  applyingVoiceId.value = voice.voice_id;
  let pollingStarted = false; // 是否已进入重生成状态轮询
  try {
    const response = await regenerateVoiceApi({
      task_id: props.taskId,
      // task_id: 'c70c14c52290',
      speaker_id: speakerId,
      voice_id: voice.voice_id,
      user_id: userId.value,
      user_name: userName.value,
    });
    if (response?.regenerate_id) {
      pollingStarted = true;
      startPolling(response.regenerate_id);
    } else {
      message.error('未获取到重生成任务ID');
    }
  } catch (error) {
    console.error('音色应用失败:', error);
    message.error('音色应用失败');
  } finally {
    if (!pollingStarted && applyingVoiceId.value === voice.voice_id) {
      applyingVoiceId.value = '';
    }
  }
}

/** 根据当前形象状态处理推荐音色 */
function handleCurrentVoiceOptions() {
  stopVoiceAudition();
  const speakerId = currentRoleInfo.value?.speaker_id; // 当前角色说话人ID

  if (!open.value || !shouldShowVoiceSelection.value || !speakerId) {
    voiceRequestId += 1;
    currentVoiceOptions.value = [];
    selectedVoiceKey.value = '';
    currentVoiceId.value = '';
    voiceLoading.value = false;
    voiceLoadError.value = false;
    return;
  }

  fetchVoiceOptions(speakerId);
}

/**
 * 播放或暂停音色试听
 * @param voice 当前音色数据
 */
async function handleVoiceAudition(voice: VoiceOption) {
  if (auditionVoiceId.value === voice.voice_id && auditionAudio) {
    if (isAuditionPlaying.value) {
      auditionAudio.pause();
      isAuditionPlaying.value = false;
      return;
    }

    const audio = auditionAudio; // 当前暂停中的试听音频
    try {
      await audio.play();
      if (auditionAudio === audio) {
        isAuditionPlaying.value = true;
      }
    } catch (error) {
      handleVoiceAuditionError(audio, error);
    }
    return;
  }

  stopVoiceAudition();

  let auditionUrl = voice.audition_url;
  if (!auditionUrl) {
    auditionLoadingVoiceId.value = voice.voice_id;
    try {
      const response = await generateVoiceAuditionApi({
        voice_id: voice.voice_id,
        gender: voice.gender,
        age_category: voice.age_category,
      });
      auditionUrl = response?.audition_url || '';
    } catch (error) {
      console.error('生成音色试听失败:', error);
      message.error('生成音色试听失败');
      return;
    } finally {
      auditionLoadingVoiceId.value = '';
    }
  }
  if (!auditionUrl) {
    message.warning('当前音色暂无试听音频');
    return;
  }

  voice.audition_url = auditionUrl;
  const audio = new Audio(auditionUrl); // 当前新建的试听音频
  auditionAudio = audio;
  auditionVoiceId.value = voice.voice_id;
  audio.onended = () => {
    if (auditionAudio !== audio) {
      return;
    }

    auditionAudio = null;
    auditionVoiceId.value = '';
    isAuditionPlaying.value = false;
  };
  audio.onerror = () => {
    handleVoiceAuditionError(audio);
  };

  try {
    await audio.play();
    if (auditionAudio === audio) {
      isAuditionPlaying.value = true;
    }
  } catch (error) {
    handleVoiceAuditionError(audio, error);
  }
}

/** 停止当前音色试听 */
function stopVoiceAudition() {
  if (auditionAudio) {
    auditionAudio.pause();
    auditionAudio.currentTime = 0;
    auditionAudio.onended = null;
    auditionAudio.onerror = null;
    auditionAudio = null;
  }

  auditionVoiceId.value = '';
  isAuditionPlaying.value = false;
}

/**
 * 处理音色试听失败
 * @param audio 试听音频实例
 * @param error 播放错误
 */
function handleVoiceAuditionError(audio: HTMLAudioElement, error?: unknown) {
  if (auditionAudio !== audio) {
    return;
  }

  stopVoiceAudition();
  message.error('音色试听失败');
  if (error) {
    console.error('音色试听失败:', error);
  }
}

/**
 * 获取当前角色的推荐音色
 * @param speakerId 角色说话人ID
 */
async function fetchVoiceOptions(speakerId: string) {
  const requestId = ++voiceRequestId; // 当前推荐音色请求批次
  currentVoiceOptions.value = [];
  selectedVoiceKey.value = '';
  currentVoiceId.value = '';
  voiceLoading.value = true;
  voiceLoadError.value = false;

  try {
    const response = await getVoiceRecommendApi({
      task_id: props.taskId,
      // task_id: 'c70c14c52290',
      speaker_id: speakerId,
    });

    if (requestId !== voiceRequestId) {
      return;
    }

    currentVoiceOptions.value = Array.isArray(response?.voices)
      ? response.voices
      : [];
    currentVoiceId.value = String(response?.current_voice_id || '');
    selectedVoiceKey.value = currentVoiceOptions.value.some(
      (voice) => voice.voice_id === currentVoiceId.value,
    )
      ? currentVoiceId.value
      : '';
  } catch (error) {
    if (requestId !== voiceRequestId) {
      return;
    }

    console.error('获取推荐音色失败:', error);
    currentVoiceOptions.value = [];
    selectedVoiceKey.value = '';
    currentVoiceId.value = '';
    voiceLoadError.value = true;
  } finally {
    if (requestId === voiceRequestId) {
      voiceLoading.value = false;
    }
  }
}

/** 角色 Tab 切换后处理推荐音色 */
function handleTabChange(_activeKey: string | number) {
  nextTick(() => {
    handleCurrentVoiceOptions();
  });
}

/**
 * 处理生成按钮点击
 */
async function handleGenerate() {
  if (props.published) {
    message.warning('作品已发布，不可修改');
    return;
  }
  if (!selectedImageModel.value) {
    message.warning('请选择图像模型');
    return;
  }

  const submittedPrompt = formData.description;
  loading.value = true;
  try {
    let res = null;
    if (optionType.value === 'roles') {
      res = await regenerateCharacterApi({
        task_id: props.taskId,
        // task_id: 'c70c14c52290',
        material_id: materialId.value,
        node_id: nodeId.value,
        new_prompt: submittedPrompt,
        model_name: selectedImageModel.value,
        user_id: userId.value,
        user_name: userName.value,
      });
    } else {
      const materialType = currentRoleInfo.value?.material_type || 'scene';
      if (!materialId.value) {
        message.warning('当前场景缺少素材 ID');
        loading.value = false;
        return;
      }
      res = await regenerateSceneApi({
        task_id: props.taskId,
        material_type: materialType,
        scene_ids: materialType === 'scene' ? sceneId.value : undefined,
        background_node_ids:
          materialType === 'background' && nodeId.value ? [nodeId.value] : undefined,
        new_prompts: {
          [materialId.value]: submittedPrompt,
        },
        model_name: selectedImageModel.value,
        user_id: userId.value,
        user_name: userName.value,
      });
    }

    // 存在 regenerate_id 时开始轮询状态
    if (res.regenerate_id) {
      startPolling(res.regenerate_id, submittedPrompt);
    } else {
      message.error('未获取到重生成任务ID');
      loading.value = false;
    }
  } catch (error) {
    console.error('重生成失败:', error);
    loading.value = false;
  }
}

//取消
function handleCancel() {
  if (isAssetOperationLocked.value) return;
  open.value = false;
  handleCurrentVoiceOptions();
}

/**
 * 处理点击右上角关闭图标
 */
function handleClose() {
  if (isAssetOperationLocked.value) return;
  // 停止轮询
  stopPolling();
  // 关闭模态框
  open.value = false;
  handleCurrentVoiceOptions();
}

/** 弹窗关闭后按本次生成状态通知父组件刷新 */
function handleAfterClose() {
  if (
    !hasGeneratedInCurrentOpen.value &&
    !hasActivatedVersionInCurrentOpen.value
  ) {
    return;
  }
  hasGeneratedInCurrentOpen.value = false;
  hasActivatedVersionInCurrentOpen.value = false;
  emit('success');
}

//初始化
function init(type: string, data: AssetItem[], defaultId?: number | string) {
  hasGeneratedInCurrentOpen.value = false;
  hasActivatedVersionInCurrentOpen.value = false;
  activePreviewIndex.value = 0;
  previewImageLoadStateMap.value = {};
  previewImageRetryMap.value = {};
  optionType.value = type;
  selectedImageModel.value = '';
  open.value = true;
  currentList.value = data;

  const firstId = data[0]?.id;

  // 根据传入的 id 绑定对应的项，如果没有传入则默认选中第一个
  if (defaultId !== undefined && defaultId !== null) {
    // 验证传入的 id 是否在数据中存在
    const targetItem = data.find((item) => item?.id === defaultId);

    if (targetItem) {
      activeId.value = defaultId;
    } else if (firstId !== undefined) {
      activeId.value = firstId;
    }
  } else if (firstId !== undefined) {
    // 没有传入 id，默认选中第一个
    activeId.value = firstId;
  }

  nextTick(() => {
    handleCurrentVoiceOptions();
  });
}

// 组件卸载时清理定时器
onUnmounted(() => {
  stopPolling();
  stopVoiceAudition();
});

defineExpose({
  init,
});
</script>
<style scoped>
.asset-modal__layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 28px;
}

.asset-modal__preview-column {
  position: relative;
  display: flex;
  align-items: flex-start;
}

.asset-preview-card {
  width: 100%;
  overflow: hidden;
  border-color: #343849;
  background: #10121a;
}

.asset-preview-card.is-role {
  aspect-ratio: 9 / 16;
}

.asset-preview-card.is-scene {
  aspect-ratio: 16 / 9;
}

.asset-preview-card__body {
  position: relative;
  display: flex;
  height: 100%;
  align-items: center;
  justify-content: center;
  background: #10121a;
}

.asset-preview-card__body :deep(.ant-image),
.asset-preview-card__body :deep(.ant-image-img) {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top;
}

.asset-modal__history {
  width: 28px;
  height: 28px;
  color: #8e83f6;
  cursor: pointer;
}

.asset-modal__form-column {
  display: flex;
  max-height: 490px;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow-x: hidden;
  overflow-y: auto;
  padding-right: 4px;
}

.asset-preview-card :deep(.ant-card-body) {
  height: 100%;
  padding: 0;
}

.asset-preview-empty :deep(.ant-empty-description) {
  color: #6b7280;
}

@media (max-width: 760px) {
  .asset-modal__layout {
    grid-template-columns: 1fr;
  }

  .asset-modal__preview-column {
    max-width: 320px;
    margin: 0 auto;
  }
}
</style>
