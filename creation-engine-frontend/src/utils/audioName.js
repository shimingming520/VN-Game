/**
 * BGM / 音效文件名 → 中文可读名
 * 原始命名:bgm_{情绪}_{描述}_{乐器?}_{起-止}.wav、sfx_{描述}_{序号}.wav
 * 处理:剥前缀 + 剥尾部编号 → 逐词查词典 → 拼中文;查不到的词保留原文。
 */

// BGM 情绪(出现在第一段,单独作为前缀标签)
const MOODS = {
  daily: '日常',
  suspense: '悬疑',
  battle: '战斗',
  sorrow: '悲伤',
  horror: '恐怖',
  tender: '温情',
  tragedy: '悲剧',
  climax: '高潮',
  crisis: '危机',
}

// 乐器(BGM 常见后缀)
const INSTRUMENTS = {
  guzheng: '古筝',
  erhu: '二胡',
  dizi: '笛子',
  pipa: '琵琶',
  taiko: '太鼓',
  strings: '弦乐',
  drones: '低鸣',
  full: '全奏',
  moody: '沉郁',
  dark: '暗色',
}

// 通用词典(BGM 描述 + 音效动作/物件)
const WORDS = {
  // 场景/事件
  wedding: '婚礼', prep: '准备', banquet: '宴席', festive: '喜庆', unease: '不安',
  morning: '清晨', night: '夜晚', stroll: '漫步', gentle: '柔和', quiet: '静谧',
  encounter: '相遇', playful: '嬉闹', banter: '打趣', flirtation: '调情',
  huating: '花厅', gomoku: '五子棋', game: '对局', chess: '棋局',
  confrontation: '对峙', standoff: '僵持', courtroom: '公堂', prowl: '潜行',
  mirror: '镜', reveal: '揭示', shatter: '碎裂', blood: '血', magic: '术法',
  talisman: '符', chaos: '混乱', escape: '逃脱', barrier: '屏障',
  massacre: '屠戮', father: '父亲', daughter: '女儿', captive: '囚', combing: '梳发',
  descent: '沉沦', madness: '疯狂', nightmare: '噩梦', memory: '记忆',
  dread: '惊惧', realization: '醒悟', despair: '绝望', lonely: '孤寂',
  relief: '释然', post: '之后', arrival: '登场', musheng: '慕笙',
  kiss: '吻', confession: '告白', dress: '衣裙', fitting: '试穿',
  hypnosis: '催眠', spirit: '灵', spiritual: '灵力', demon: '魔', tower: '塔',
  // 音效:物件
  door: '门', chain: '锁链', chair: '椅', table: '桌', cup: '杯', tea: '茶',
  chopsticks: '筷子', porcelain: '瓷器', candle: '烛', lantern: '灯笼',
  book: '书', page: '书页', paper: '纸', box: '盒', pouch: '荷包',
  fabric: '布料', clothing: '衣物', sleeve: '衣袖', umbrella: '伞',
  scissors: '剪刀', hair: '发', sword: '剑', armor: '甲', iron: '铁',
  jade: '玉', stone: '石', wooden: '木', golden: '金', rune: '符文',
  array: '阵', formation: '阵型', form: '成形', pagoda: '宝塔',
  firework: '烟花', flame: '火焰', blue: '蓝', water: '水', puddle: '水洼',
  wind: '风', ash: '灰烬', willow: '柳', branch: '枝', rope: '绳',
  roof: '屋顶', tiles: '瓦', pillar: '柱', floor: '地面', ground: '地',
  body: '身体', hand: '手', wrist: '手腕', shoulder: '肩', knees: '膝',
  system: '系统', notification: '提示', energy: '能量',
  embroidered: '绣球', ball: '球', comb: '梳', piece: '棋子', go: '棋',
  folding: '折', drawing: '抽出', draw: '抽',
  // 音效:动作
  open: '开启', close: '关闭', push: '推', pull: '拉', creak: '吱呀',
  slam: '砸上', drag: '拖动', scrape: '刮擦', rattle: '哗啦', shriek: '尖响',
  drop: '落下', fall: '坠落', falling: '坠落', collapse: '倒地', crash: '崩落',
  impact: '撞击', hit: '击打', strike: '击', clash: '交锋', slash: '劈斩',
  parry: '格挡', rebound: '弹回', launch: '发射', shoot: '射出',
  grab: '抓住', clench: '攥紧', release: '松开', retrieve: '取出',
  place: '放置', placedown: '放下', placement: '落子', put: '放下',
  down: '下', in: '入', into: '入', on: '上', to: '至', through: '穿过',
  away: '离去', half: '半', rough: '粗重', heavy: '沉重', cold: '冷',
  distant: '远处', invisible: '无形', air: '空气', tremor: '震颤',
  surge: '涌动', flow: '流动', splash: '溅落', gulp: '吞咽',
  burn: '燃烧', burst: '爆开', combustion: '燃起', ignite: '点燃',
  ignition: '点燃', disperse: '消散', injection: '注入', suction: '吸力',
  smash: '砸碎', crush: '碾碎', break: '断裂', tearing: '撕裂',
  snipping: '剪断', bind: '束缚', binding: '缚', sit: '坐下', stand: '起身',
  kneel: '跪下', run: '奔跑', running: '奔跑', footsteps: '脚步',
  scramble: '慌乱', slide: '滑动', swish: '挥动', tap: '轻敲',
  rustle: '窸窣', thud: '闷响', chime: '清鸣', laugh: '笑', howl: '嚎叫',
  cries: '呼喊', chant: '吟诵', incantation: '咒', sheathed: '入鞘',
  force: '力', battle_cries: '厮杀声',
}

// 英文虚词:直译反而拗口,直接丢弃
const STOP_WORDS = new Set(['on', 'in', 'into', 'to', 'through', 'down', 'the', 'a', 'of', 'and'])

function translateWord(w) {
  return WORDS[w] || INSTRUMENTS[w] || MOODS[w] || w
}

/**
 * 本地词典翻译(离线兜底,不依赖后端)
 * @param {string} name 原始文件名(可带路径/扩展名)
 * @returns {string} 中文可读名,失败时回退原名
 */
export function prettifyAudioNameLocal(name) {
  if (!name) return ''
  const base = name.split('/').pop().replace(/\.[^.]+$/, '')
  const m = base.match(/^(bgm|sfx|amb)_(.+)$/i)
  if (!m) return base
  const kind = m[1].toLowerCase()
  // 剥尾部编号:_12 或 _45-80
  let body = m[2].replace(/_\d+(-\d+)?$/, '')
  const parts = body.split('_').filter(Boolean)
  if (!parts.length) return base

  let mood = ''
  if (kind === 'bgm' && MOODS[parts[0]]) {
    mood = MOODS[parts[0]]
    parts.shift()
  }
  // 乐器词挪到末尾括注
  const instruments = []
  const rest = []
  for (const p of parts) {
    if (STOP_WORDS.has(p)) continue
    if (INSTRUMENTS[p]) instruments.push(INSTRUMENTS[p])
    else rest.push(translateWord(p))
  }
  let text = rest.join('')
  if (mood) text = text ? `${mood}·${text}` : mood
  if (instruments.length) text += `(${instruments.join('')})`
  return text || base
}

// ── LLM 翻译层(后端 qwen3.6-plus + 数据库缓存),支持任意语言 ──
// 本地词典作为兜底:LLM 未返回/未配置密钥时仍可读。
import { reactive } from 'vue'
import { translateAssetNamesApi } from '@/api'

const llmCache = reactive({})   // 原始文件名 → 中文译名
const pending = new Set()       // 正在请求中的名字,避免重复提交

function baseName(name) {
  return (name || '').split('/').pop()
}

/**
 * 展示用名称:优先 LLM 译名,其次本地词典。
 * 注意:LLM 结果异步到达,因为 llmCache 是 reactive,到达后模板会自动刷新。
 */
export function prettifyAudioName(name) {
  const key = baseName(name)
  return llmCache[key] || prettifyAudioNameLocal(name)
}

/**
 * 预热翻译:把当前列表里的音频文件名批量送后端翻译(已缓存/在途的自动跳过)。
 * @param {string[]} names 原始文件名数组
 */
export async function warmAudioNames(names) {
  const todo = []
  for (const n of names || []) {
    const key = baseName(n)
    if (!key || llmCache[key] || pending.has(key)) continue
    pending.add(key)
    todo.push(key)
  }
  if (!todo.length) return
  try {
    const res = await translateAssetNamesApi(todo)
    for (const [src, zh] of Object.entries(res || {})) {
      if (zh) llmCache[src] = zh
    }
  } catch {
    // 失败静默:继续用本地词典
  } finally {
    for (const k of todo) pending.delete(k)
  }
}
