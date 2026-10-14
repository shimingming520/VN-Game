/** 7 阶段执行顺序 + 中文名（与创作侧管线一致）。 */
export const TASK_STAGE_FIELDS = Object.freeze([
  ['story_planner_status', '文本处理'],
  ['info_extractor_status', '信息提取'],
  ['char_designer_status', '角色设计'],
  ['scene_painter_status', '场景绘制'],
  ['audio_producer_status', '配音生成'],
  ['sound_arranger_status', '音效编排'],
  ['game_builder_status', '游戏打包'],
])

export const RESTART_MODES = Object.freeze({
  FAILED_ONLY: 'failed_only',
  RUNNING_AND_FAILED: 'running_and_failed',
})

function buildPlan(items, mode) {
  const completed = []
  const failed = []
  const cancelled = []
  const running = []
  const pending = []

  for (const { label, status } of items) {
    if (status === 'succeeded') completed.push(label)
    else if (status === 'failed') failed.push(label)
    else if (status === 'cancelled') cancelled.push(label)
    else if (status === 'running') running.push(label)
    else pending.push(label)
  }

  const restartable = items
    .filter(({ status }) => mode === RESTART_MODES.FAILED_ONLY
      ? status === 'failed'
      : status === 'running' || status === 'failed' || status === 'cancelled')
    .map(({ label }) => label)

  return {
    completed,
    failed,
    cancelled,
    running,
    pending,
    restartable,
    canRestart: restartable.length > 0,
  }
}

/** 生成任务重启预览；pending 阶段继续等待，不会被人工重置。 */
export function getTaskRestartPlan(record = {}, mode = RESTART_MODES.RUNNING_AND_FAILED) {
  return buildPlan(TASK_STAGE_FIELDS.map(([field, label]) => ({
    label,
    status: ({ 0: 'pending', 1: 'running', 2: 'succeeded', 3: 'failed' })[
      Number(record[field])
    ] || 'pending',
  })), mode)
}

/** 创作测试页使用中台 steps 展示结构，转换成同一套重启计划。 */
export function getCreationTaskRestartPlan(
  row = {},
  mode = RESTART_MODES.RUNNING_AND_FAILED,
) {
  const steps = Array.isArray(row.steps) ? row.steps : []
  const items = TASK_STAGE_FIELDS.map(([field, label]) => {
    const capability = field.replace(/_status$/, '')
    const status = steps.find((step) => step.capability === capability)?.status
    return {
      label,
      status: status === 'succeeded'
        ? 'succeeded'
        : status === 'failed'
          ? 'failed'
          : status === 'cancelled'
            ? 'cancelled'
          : ['running', 'publishing', 'dispatched'].includes(status)
            ? 'running'
            : 'pending',
    }
  })

  return buildPlan(items, mode)
}
