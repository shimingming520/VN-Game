// 与后端 common/task_models.py 的常量一一对应。
// 取值写死是有意的:后端用它们做强校验,前端传别的值会直接 400。

/** ArtStyle.VALID_STYLES */
export const ART_STYLES = [
  { value: 'ink_chinese', label: '新中式水墨风' },
  { value: 'cyberpunk', label: '赛博朋克/霓虹' },
  { value: 'japanese', label: '唯美日系' },
  { value: 'dark_european', label: '欧式暗黑' },
  { value: 'modern_realism', label: '现代写实' },
]

export const ART_STYLE_LABELS = ART_STYLES.reduce((acc, item) => {
  acc[item.value] = item.label
  return acc
}, {})

/** StoryMode.VALID_MODES */
export const STORY_MODES = [
  { value: 'original', label: '原文适应' },
  { value: 'multi', label: '多剧情改写' },
  { value: 'chapter_branch', label: '章节分支' },
]

/** 后端 config 端点当前只放行这两种输出语言 */
export const OUTPUT_LANGUAGES = [
  { value: 'zh-CN', label: '简体中文' },
  { value: 'en-US', label: 'English' },
]

export const GAMEPLAYS = [
  { value: 'interactive_story', label: '互动故事' },
  { value: 'free_growth', label: '自由成长' },
]

/** Resolution.VALID_RESOLUTIONS 的展示文案 */
export const RESOLUTION_LABELS = {
  '1080p': '1080P',
  '2k': '2K',
  '4k': '4K',
}

export const RESOLUTION_OPTIONS = Object.entries(RESOLUTION_LABELS).map(
  ([value, label]) => ({ value, label }),
)

/**
 * 界面模板 = build 接口的 ui_preset(1-5)，与 UI_PRESET_MAP 里的 art_style 对应。
 * 没有设计稿切图,预览用 CSS 还原各风格的对话框观感,不引用缺失的 png。
 */
export const UI_PRESETS = [
  {
    preset: 1,
    artStyle: 'ink_chinese',
    name: '新中式水墨',
    desc: '宣纸质感、墨色描边，适合古风与仙侠题材',
  },
  {
    preset: 2,
    artStyle: 'cyberpunk',
    name: '赛博朋克',
    desc: '霓虹描边与高对比暗色，适合近未来题材',
  },
  {
    preset: 3,
    artStyle: 'japanese',
    name: '唯美日系',
    desc: '浅色半透明对话框，适合校园与恋爱题材',
  },
  {
    preset: 4,
    artStyle: 'dark_european',
    name: '欧式暗黑',
    desc: '深棕烫金边框，适合哥特与悬疑题材',
  },
  {
    preset: 5,
    artStyle: 'modern_realism',
    name: '现代写实',
    desc: '中性灰蓝面板，适合都市与写实题材',
  },
]

/** 场景景别,与 scene-shot 接口的 _VALID_SCENE_SHOT_SIZES 一致 */
export const SHOT_SIZES = [
  { value: 'LS', label: '远景 LS' },
  { value: 'MS', label: '中景 MS' },
  { value: 'CS', label: '近景 CS' },
]
