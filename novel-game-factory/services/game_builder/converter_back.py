#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视觉小说素材转换脚本 - 第一版
基于音频、字幕、图片素材，支持所有字幕格式
"""
import random
import os
import re
import shutil
import json
import datetime
from pathlib import Path

try:
    from common.audio_languages import AUDIO_OUTPUT_LOCALES, DEFAULT_AUDIO_LANGUAGE
except ImportError:
    # 兼容直接执行 converter.py；不改变书籍源语言，只固定运行期首选语言。
    AUDIO_OUTPUT_LOCALES = ("en-US", "ja-JP")
    DEFAULT_AUDIO_LANGUAGE = "en-US"

# 在线版运行期只接受精确的 locale；不要在此加入短码或其他地区变体。
ONLINE_GAME_LANGUAGES = ("en-US", "ja-JP")

def choose_default_audio_language(languages, book_language=None):
    """在线版未传 ``?lang`` 时始终使用英语。"""
    return DEFAULT_AUDIO_LANGUAGE


class AtmosphereEffects:
    """视觉特效系统"""

    # 即时特效（触发后立即结束）
    INSTANT_EFFECTS = {
        'shake_camera_light', 'shake_camera_heavy', 'shake_sprite',
        'flash_red', 'flash_white', 'flash_green', 'flash_blue'
    }

    # 持续特效（需要手动开启/关闭）
    PERSISTENT_EFFECTS = {
        'zoom_vignette',
        'weather_wind', 'weather_snow', 'weather_rain', 'weather_fog',
        'weather_leaves', 'weather_ash', 'weather_dust',
        'overlay_blood', 'overlay_crack', 'overlay_water',
        'filter_cold', 'filter_warm', 'filter_sepia', 'filter_bw'
    }

    @staticmethod
    def load_atmosphere_config(source_dir):
        """加载atmophere.json配置（支持向上查找到根目录）"""
        source_path = Path(source_dir)

        # 首先在当前目录查找
        config_path = source_path / "atmophere.json"
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)

        # 如果当前目录没有，尝试在父目录查找（适用于节点目录的情况）
        parent_config_path = source_path.parent / "atmophere.json"
        if parent_config_path.exists():
            with open(parent_config_path, 'r', encoding='utf-8') as f:
                return json.load(f)

        return {}

    @staticmethod
    def get_effects_for_beat(atmosphere_config, chapter_id, beat_index, node_id=None):
        """获取指定beat的特效列表（按order排序）

        顶层 key 匹配优先级：node_id > chapter_{depth}。
        node_id 命中即用其配置，否则回退到 chapter_{depth}，
        以支持同 depth 多节点（如多个 What-If 节点）各自独立配特效。
        """
        beat_key = str(beat_index)
        candidate_keys = []
        if node_id:
            candidate_keys.append(node_id)
        candidate_keys.append(f"chapter_{chapter_id}")
        for chapter_key in candidate_keys:
            block = atmosphere_config.get(chapter_key)
            if block and beat_key in block:
                effects = block[beat_key]
                return sorted(effects, key=lambda x: x.get('order', 0))
        return []

    @staticmethod
    def generate_effect_library_js():
        """生成完整的JavaScript特效库"""
        return """
// ==================== 视觉特效系统 (Atmosphere Effects) ====================
const AtmosphereEffects = {
    activeEffects: new Set(),

    // 特效分类定义
    INSTANT_EFFECTS: new Set([
        'shake_camera_light', 'shake_camera_heavy', 'shake_sprite',
        'flash_red', 'flash_white', 'flash_green', 'flash_blue'
    ]),

    PERSISTENT_EFFECTS: new Set([
        'zoom_vignette',
        'weather_wind', 'weather_snow', 'weather_rain', 'weather_fog',
        'weather_leaves', 'weather_ash', 'weather_dust',
        'overlay_blood', 'overlay_crack', 'overlay_water',
        'filter_cold', 'filter_warm', 'filter_sepia', 'filter_bw'
    ]),

    // ========== 震动类 (Shake) - 即时触发 ==========
    shake_camera_light: function() {
        const game = document.querySelector('[data-screen="game"]');
        if (!game) return;
        game.style.animation = 'shake-light 0.3s ease';
        setTimeout(() => { game.style.animation = ''; }, 300);
    },

    shake_camera_heavy: function() {
        const game = document.querySelector('[data-screen="game"]');
        if (!game) return;
        game.style.animation = 'shake-heavy 0.5s ease';
        setTimeout(() => { game.style.animation = ''; }, 500);
    },

    shake_sprite: function() {
        const sprites = document.querySelectorAll('[data-character], [data-manual-character]');
        sprites.forEach(sprite => {
            sprite.style.animation = 'shake-sprite 0.4s ease';
            setTimeout(() => { sprite.style.animation = ''; }, 400);
        });
    },

    // ========== 闪烁类 (Flash) - 即时触发 ==========
    flash_red: function() {
        this._flash('#8B0000', 0.35, 180);
    },

    flash_white: function() {
        this._flash('#ffffff', 0.5, 120);
    },

    flash_green: function() {
        this._flash('#00ff88', 0.35, 180);
    },

    flash_blue: function() {
        this._flash('#0088ff', 0.35, 180);
    },

    _flash: function(color, opacity, duration) {
        let overlay = document.getElementById('atmo-flash-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'atmo-flash-overlay';
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 9999; pointer-events: none; opacity: 0;
                transition: opacity 0.05s ease;
            `;
            document.body.appendChild(overlay);
        }
        overlay.style.backgroundColor = color;
        overlay.style.opacity = opacity;
        setTimeout(() => { overlay.style.opacity = '0'; }, duration);
    },

    // ========== 镜头与暗角 (Zoom & Vignette) - 持续状态 ==========
    zoom_vignette: {
        start: function() {
            let overlay = document.getElementById('atmo-zoom-vignette');
            if (!overlay) {
                overlay = document.createElement('div');
                overlay.id = 'atmo-zoom-vignette';
                overlay.style.cssText = `
                    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                    z-index: 1; pointer-events: none;
                    box-shadow: inset 0 0 200px 100px rgba(0,0,0,0.75);
                    opacity: 0; transition: opacity 2s ease;
                `;
                document.body.appendChild(overlay);
            }
            setTimeout(() => { overlay.style.opacity = '1'; }, 50);

            const game = document.querySelector('[data-screen="game"]');
            if (game) {
                game.style.transition = 'transform 2s ease';
                game.style.transform = 'scale(1.12)';
            }
        },
        stop: function() {
            const overlay = document.getElementById('atmo-zoom-vignette');
            if (overlay) {
                overlay.style.opacity = '0';
                setTimeout(() => overlay.remove(), 2000);
            }
            const game = document.querySelector('[data-screen="game"]');
            if (game) {
                game.style.transform = 'scale(1)';
            }
        }
    },

    // ========== 天气类 (Weather) - 持续状态 ==========
    weather_snow: {
        start: function() {
            if (document.getElementById('atmo-weather-snow')) return;
            const canvas = document.createElement('canvas');
            canvas.id = 'atmo-weather-snow';
            canvas.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
            `;
            document.body.appendChild(canvas);

            const ctx = canvas.getContext('2d');
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;

            const particles = [];
            for (let i = 0; i < 120; i++) {
                particles.push({
                    x: Math.random() * canvas.width,
                    y: Math.random() * canvas.height,
                    r: Math.random() * 2.5 + 0.8,
                    d: Math.random() * 1 + 0.3,
                    drift: Math.random() * 0.5 - 0.25
                });
            }

            function animate() {
                if (!document.getElementById('atmo-weather-snow')) return;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
                ctx.beginPath();
                particles.forEach(p => {
                    ctx.moveTo(p.x, p.y);
                    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
                    p.y += p.d;
                    p.x += p.drift;
                    if (p.y > canvas.height) {
                        p.y = 0;
                        p.x = Math.random() * canvas.width;
                    }
                });
                ctx.fill();
                requestAnimationFrame(animate);
            }
            animate();
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-snow');
            if (el) el.remove();
        }
    },

    weather_rain: {
        start: function() {
            if (document.getElementById('atmo-weather-rain')) return;
            const canvas = document.createElement('canvas');
            canvas.id = 'atmo-weather-rain';
            canvas.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
            `;
            document.body.appendChild(canvas);

            const ctx = canvas.getContext('2d');
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;

            const drops = [];
            for (let i = 0; i < 150; i++) {
                drops.push({
                    x: Math.random() * canvas.width,
                    y: Math.random() * canvas.height,
                    l: Math.random() * 20 + 10,
                    s: Math.random() * 5 + 3
                });
            }

            function animate() {
                if (!document.getElementById('atmo-weather-rain')) return;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.strokeStyle = 'rgba(174, 194, 224, 0.5)';
                ctx.lineWidth = 1;
                ctx.beginPath();
                drops.forEach(d => {
                    ctx.moveTo(d.x, d.y);
                    ctx.lineTo(d.x, d.y + d.l);
                    d.y += d.s;
                    if (d.y > canvas.height) d.y = -d.l;
                });
                ctx.stroke();
                requestAnimationFrame(animate);
            }
            animate();
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-rain');
            if (el) el.remove();
        }
    },

    weather_fog: {
        start: function() {
            if (document.getElementById('atmo-weather-fog')) return;
            const overlay = document.createElement('div');
            overlay.id = 'atmo-weather-fog';
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
                background: linear-gradient(to bottom,
                    rgba(200,200,220,0.05) 0%,
                    rgba(200,200,220,0.25) 50%,
                    rgba(200,200,220,0.4) 100%);
                opacity: 0; transition: opacity 3s ease;
            `;
            document.body.appendChild(overlay);
            setTimeout(() => { overlay.style.opacity = '1'; }, 50);
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-fog');
            if (el) {
                el.style.opacity = '0';
                setTimeout(() => el.remove(), 3000);
            }
        }
    },

    weather_wind: {
        start: function() {
            if (document.getElementById('atmo-weather-wind')) return;
            const canvas = document.createElement('canvas');
            canvas.id = 'atmo-weather-wind';
            canvas.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
            `;
            document.body.appendChild(canvas);

            const ctx = canvas.getContext('2d');
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;

            const particles = [];
            for (let i = 0; i < 50; i++) {
                particles.push({
                    x: Math.random() * canvas.width,
                    y: Math.random() * canvas.height,
                    l: Math.random() * 30 + 20,
                    s: Math.random() * 8 + 5
                });
            }

            function animate() {
                if (!document.getElementById('atmo-weather-wind')) return;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
                ctx.lineWidth = 2;
                ctx.beginPath();
                particles.forEach(p => {
                    ctx.moveTo(p.x, p.y);
                    ctx.lineTo(p.x + p.l, p.y);
                    p.x += p.s;
                    if (p.x > canvas.width) p.x = -p.l;
                });
                ctx.stroke();
                requestAnimationFrame(animate);
            }
            animate();
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-wind');
            if (el) el.remove();
        }
    },

    weather_leaves: {
        start: function() {
            AtmosphereEffects._createParticleEffect('atmo-weather-leaves', 'rgba(200,150,80,0.7)', 60, true);
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-leaves');
            if (el) el.remove();
        }
    },

    weather_ash: {
        start: function() {
            AtmosphereEffects._createParticleEffect('atmo-weather-ash', 'rgba(100,100,100,0.6)', 80, false);
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-ash');
            if (el) el.remove();
        }
    },

    weather_dust: {
        start: function() {
            AtmosphereEffects._createParticleEffect('atmo-weather-dust', 'rgba(180,180,160,0.4)', 40, false);
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-dust');
            if (el) el.remove();
        }
    },

    _createParticleEffect: function(id, color, count, sway) {
        if (document.getElementById(id)) return;
        const canvas = document.createElement('canvas');
        canvas.id = id;
        canvas.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            z-index: 1; pointer-events: none;
        `;
        document.body.appendChild(canvas);

        const ctx = canvas.getContext('2d');
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;

        const particles = [];
        for (let i = 0; i < count; i++) {
            particles.push({
                x: Math.random() * canvas.width,
                y: Math.random() * canvas.height,
                r: Math.random() * 2 + 1,
                d: Math.random() * 0.5 + 0.3,
                sway: sway ? Math.random() * 2 - 1 : 0
            });
        }

        function animate() {
            if (!document.getElementById(id)) return;
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.fillStyle = color;
            ctx.beginPath();
            particles.forEach(p => {
                ctx.moveTo(p.x, p.y);
                ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
                p.y += p.d;
                p.x += p.sway * 0.5;
                if (p.y > canvas.height) {
                    p.y = 0;
                    p.x = Math.random() * canvas.width;
                }
            });
            ctx.fill();
            requestAnimationFrame(animate);
        }
        animate();
    },

    // ========== 屏幕遮罩类 (Screen Overlay) - 持续状态 ==========
    overlay_blood: {
        start: function() {
            if (document.getElementById('atmo-overlay-blood')) return;
            const overlay = document.createElement('div');
            overlay.id = 'atmo-overlay-blood';
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
                background: radial-gradient(circle at 50% 50%,
                    rgba(139,0,0,0) 20%,
                    rgba(139,0,0,0.3) 100%);
                opacity: 0; transition: opacity 1s ease;
            `;
            document.body.appendChild(overlay);
            setTimeout(() => { overlay.style.opacity = '1'; }, 50);
        },
        stop: function() {
            const el = document.getElementById('atmo-overlay-blood');
            if (el) {
                el.style.opacity = '0';
                setTimeout(() => el.remove(), 1000);
            }
        }
    },

    overlay_crack: {
        start: function() {
            if (document.getElementById('atmo-overlay-crack')) return;
            const overlay = document.createElement('div');
            overlay.id = 'atmo-overlay-crack';
            overlay.innerHTML = `
                <svg style="width:100%;height:100%;opacity:0.4" viewBox="0 0 100 100" preserveAspectRatio="none">
                    <path d="M50,0 L52,50 L100,50 M50,50 L30,100 M50,50 L70,100 M50,50 L20,60 M50,50 L80,40"
                          stroke="rgba(255,255,255,0.6)" stroke-width="0.3" fill="none"/>
                </svg>
            `;
            overlay.id = 'atmo-overlay-crack';
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
                opacity: 0; transition: opacity 0.4s ease;
            `;
            document.body.appendChild(overlay);
            setTimeout(() => { overlay.style.opacity = '1'; }, 50);
        },
        stop: function() {
            const el = document.getElementById('atmo-overlay-crack');
            if (el) {
                el.style.opacity = '0';
                setTimeout(() => el.remove(), 400);
            }
        }
    },

    overlay_water: {
        start: function() {
            if (document.getElementById('atmo-overlay-water')) return;
            const overlay = document.createElement('div');
            overlay.id = 'atmo-overlay-water';
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
                backdrop-filter: blur(2px);
                background: linear-gradient(to bottom,
                    rgba(200,220,255,0.08) 0%,
                    rgba(200,220,255,0.15) 100%);
                opacity: 0; transition: opacity 1.5s ease;
            `;
            document.body.appendChild(overlay);
            setTimeout(() => { overlay.style.opacity = '1'; }, 50);
        },
        stop: function() {
            const el = document.getElementById('atmo-overlay-water');
            if (el) {
                el.style.opacity = '0';
                setTimeout(() => el.remove(), 1500);
            }
        }
    },

    // ========== 全屏滤镜类 (Color Grading) - 持续状态 ==========
    filter_cold: {
        start: function() {
            if (document.getElementById('atmo-filter-cold')) return;
            const overlay = document.createElement('div');
            overlay.id = 'atmo-filter-cold';
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
                background: rgba(100,150,200,0.12);
                mix-blend-mode: multiply;
                opacity: 0; transition: opacity 2.5s ease;
            `;
            document.body.appendChild(overlay);
            setTimeout(() => { overlay.style.opacity = '1'; }, 50);
        },
        stop: function() {
            const el = document.getElementById('atmo-filter-cold');
            if (el) {
                el.style.opacity = '0';
                setTimeout(() => el.remove(), 2500);
            }
        }
    },

    filter_warm: {
        start: function() {
            if (document.getElementById('atmo-filter-warm')) return;
            const overlay = document.createElement('div');
            overlay.id = 'atmo-filter-warm';
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                z-index: 1; pointer-events: none;
                background: rgba(255,200,100,0.12);
                mix-blend-mode: overlay;
                opacity: 0; transition: opacity 2.5s ease;
            `;
            document.body.appendChild(overlay);
            setTimeout(() => { overlay.style.opacity = '1'; }, 50);
        },
        stop: function() {
            const el = document.getElementById('atmo-filter-warm');
            if (el) {
                el.style.opacity = '0';
                setTimeout(() => el.remove(), 2500);
            }
        }
    },

    filter_sepia: {
        start: function() {
            const game = document.querySelector('[data-screen="game"]');
            if (game) {
                game.style.transition = 'filter 1.5s ease';
                game.style.filter = 'sepia(0.8) contrast(0.9)';
            }
        },
        stop: function() {
            const game = document.querySelector('[data-screen="game"]');
            if (game) {
                game.style.filter = '';
            }
        }
    },

    filter_bw: {
        start: function() {
            const game = document.querySelector('[data-screen="game"]');
            if (game) {
                game.style.transition = 'filter 0.6s ease';
                game.style.filter = 'grayscale(1) contrast(1.05)';
            }
        },
        stop: function() {
            const game = document.querySelector('[data-screen="game"]');
            if (game) {
                game.style.filter = '';
            }
        }
    },

    // ========== 统一调用接口 ==========
    apply: function(effectType) {
        console.log('🎨 [AtmosphereEffect] Applying:', effectType);

        if (this.INSTANT_EFFECTS.has(effectType)) {
            // 即时特效：直接执行
            if (typeof this[effectType] === 'function') {
                this[effectType]();
            }
        } else if (this.PERSISTENT_EFFECTS.has(effectType)) {
            // 持续特效：检查是否已激活
            if (!this.activeEffects.has(effectType)) {
                if (this[effectType] && typeof this[effectType].start === 'function') {
                    this[effectType].start();
                    this.activeEffects.add(effectType);
                }
            }
        }
    },

    // 清理所有持续特效
    clearAll: function() {
        console.log('🧹 [AtmosphereEffect] Clearing all effects, active count:', this.activeEffects.size);
        this.activeEffects.forEach(effectType => {
            console.log('  🧹 Stopping:', effectType);
            if (this[effectType] && typeof this[effectType].stop === 'function') {
                this[effectType].stop();
            }
        });
        this.activeEffects.clear();
        console.log('✅ [AtmosphereEffect] All effects cleared');
    }
};

// 将 AtmosphereEffects 挂载到 window 对象，方便全局访问
window.AtmosphereEffects = AtmosphereEffects;

// CSS动画定义
(function() {
    const atmoStyles = document.createElement('style');
    atmoStyles.textContent = `
@keyframes shake-light {
    0%, 100% { transform: translate(0, 0); }
    25% { transform: translate(-2px, 2px); }
    50% { transform: translate(2px, -2px); }
    75% { transform: translate(-2px, -2px); }
}
@keyframes shake-heavy {
    0%, 100% { transform: translate(0, 0); }
    10% { transform: translate(-8px, 4px); }
    20% { transform: translate(8px, -4px); }
    30% { transform: translate(-8px, -4px); }
    40% { transform: translate(8px, 4px); }
    50% { transform: translate(-6px, -6px); }
    60% { transform: translate(6px, 6px); }
    70% { transform: translate(-4px, 4px); }
    80% { transform: translate(4px, -4px); }
    90% { transform: translate(-2px, 2px); }
}
@keyframes shake-sprite {
    0%, 100% { transform: translateX(0); }
    10%, 30%, 50%, 70%, 90% { transform: translateX(-4px); }
    20%, 40%, 60%, 80% { transform: translateX(4px); }
}
`;
    document.head.appendChild(atmoStyles);
})();
"""


class FinalConverter:
    def __init__(self, source_dir="novel-game-factory/storage/outputs",
                 template_dir="novel-game-factory/services/game_builder/game_template/shijuexiaoshuo",
                 output_dir="novel-game-factory/storage/built_games/shijuexiaoshuo-temp", images_base_dir=None,
                 domain=None, art_style="modern_realism", delete_local_assets=True, task_id=None,
                 enable_oss_upload=False, book_language="zh-CN", ui_mode="Other"):
        """
        初始化转换器
≥
        Args:
            source_dir: 素材源目录
            template_dir: 物语引擎模板目录
            output_dir: 输出目录
            images_base_dir: 图片基础目录，默认为source_dir.parent
            domain: 域名配置，默认为 localhost（可以是单个域名或多个域名，用空格分隔）
        """
        self.source_dir = Path(source_dir)
        self.template_dir = Path(template_dir)
        self.output_dir = Path(output_dir)
        if images_base_dir:
            self.images_base_dir = Path(images_base_dir)
        else:
            # 智能检测 images 目录位置：
            # 1. 优先检查 source_dir 下是否有 images 目录（游戏项目目录结构）
            # 2. 如果 source_dir 本身就是 outputs 目录，则使用 source_dir
            # 3. 否则使用父目录（兼容旧的 node_xxx 目录结构）
            if (self.source_dir / "images").exists():
                self.images_base_dir = self.source_dir
            elif self.source_dir.name == "outputs":
                self.images_base_dir = self.source_dir
            else:
                self.images_base_dir = self.source_dir.parent

        # 域名配置
        self.domain = domain if domain else "localhost"

        # 画面风格
        self.art_style = art_style if art_style else "modern_realism"

        # UI 风格只由 story_tree.UI_mode 决定，不再跟随 art_style。
        # 当前仅落地 Ancient；其余值统一视为 Other，等待后续视觉方案。
        self.ui_mode = "Ancient" if str(ui_mode).strip().lower() == "ancient" else "Other"

        # 书籍语言（默认语言兜底；运行期实际语言由前端 URL ?lang= 决定）
        self.book_language = book_language if book_language else "zh-CN"
        # speaker_id → 显示名 映射表（由 load_name_mappings 填充，默认语言）
        self.name_map = {}
        # speaker_id → 颜色 映射表（注册角色时填充）
        self.speaker_color = {}
        # 多语言：扫描得到的语言集合 / 默认语言 / 各语言字幕文本 / 各语言音频清单 / 各语言显示名
        self.languages = [self.book_language]
        self.default_language = DEFAULT_AUDIO_LANGUAGE
        self.i18n_text = {}        # lang -> {subtitle_id: 纯正文}
        self.voices_by_lang = {}   # lang -> {subtitle_id: 'xxx.wav'}
        self.names_by_lang = {}    # lang -> {speaker_id: 显示名}
        # 主调度 JSON 中的完整 beat 元信息。分支 C1 即使文件缺失，
        # 仍可用这份元信息决定选项界面场景和分支状态。
        self.master_beat_meta = {}  # beat_id_raw -> meta
        self.branch_control_content = {}  # numeric index -> C/C1 effects raw text

        # 资产统一保留在共享本地存储，不上传 OSS，也不删除本地文件。
        self.delete_local_assets = False
        self.enable_oss_upload = False

        # 主 JSON 中是否存在任何 system_text（决定是否在前端注入 system 按钮）
        # 由 create_per_node_projects 全局扫描后赋值并下发给子 converter
        self._has_system_text = True

        # task_id，用于回写 character_prompts 表
        self.task_id = task_id

        # 拼音到中文的映射（动态生成）
        self.pinyin_to_chinese = {
            "pangbai": "旁白",
            "Pangbai": "旁白",
            "nb": "旁白"
        }

        # 中文到ID的映射（动态生成）
        self.chinese_to_id = {
            "旁白": "nb"
        }

        # 颜色映射
        self.color_palette = [
            "#FF6B6B", "#4ECDC4", "#FFD166", "#06D6A0", "#118AB2"
        ]

        # 预编译正则表达式
        # 支持两种格式：scene_id-index（新）和 scene_id_index（旧）
        self.scene_patterns = [
            re.compile(r'(.+?)-(\d+)$'),  # 新格式：scene_id-index（连字符分隔）
            re.compile(r'(.+?)_(\d+)$')  # 旧格式：scene_id_index（下划线分隔）
        ]
        # 软分支格式：scene_id-index-A1 / scene_id-index-B3 / scene_id-index-C
        self.branch_pattern = re.compile(r'^(.+?)-(\d+)-([AB])(\d+)$')   # A/B 分支
        self.branch_c_pattern = re.compile(r'^(.+?)-(\d+)-C\d*$')         # C 结算文件（C / C1）
        # BGM：新格式 nodeid_start-end，旧格式 music_start_end
        self.music_pattern = re.compile(r'(.+?)_(\d+)-(\d+)$')
        self.music_pattern_legacy = re.compile(r'music_(\d+)_(\d+)')
        # SFX：nodeid_index
        self.sfx_pattern = re.compile(r'(.+?)_(\d+)$')
        self.subtitle_patterns = [
            # 修改点：将 [^：]+ 改为 [^:：]+ ，意思是"只要遇到任意一种冒号就停止匹配"
            # 这样可以防止跨越英文冒号去匹配后面的中文冒号
            re.compile(r'^([^:：]+)：(.*)$'),  # 中文冒号分隔
            re.compile(r'^([^:：]+):(.*)$'),  # 英文冒号分隔
            re.compile(r'^([^"]+)"(.*)"$')  # 英文双引号分隔
        ]

        # 反向映射：中文到拼音（用于快速查找）
        self.chinese_to_pinyin = {v: k for k, v in self.pinyin_to_chinese.items()}

        # 存储扫描到的素材
        self.audio_files = []
        self.subtitle_files = []
        self.scene_images = []
        self.character_images = []
        self.item_images = []
        self.music_files = []
        self.ambience_files = []
        self.sfx_files = []
        self.character_map = {}

        # 加载特效配置
        self.atmosphere_config = AtmosphereEffects.load_atmosphere_config(self.source_dir)
        if self.atmosphere_config:
            print(f"✅ 加载特效配置: {len(self.atmosphere_config)} 个章节")
        else:
            print("⚠️ 未找到atmophere.json，特效系统将不启用")

    def load_character_info(self):
        """加载角色信息，建立拼音到中文的映射"""
        try:
            # 尝试从JSON文件加载角色信息
            json_dir = self.source_dir.parent / "json" / "1_11"
            if not json_dir.exists():
                json_dir = self.source_dir.parent / "json"
                if not json_dir.exists():
                    print("未找到JSON目录，使用默认角色映射")
                    return

            # 查找角色JSON文件
            character_files = list(json_dir.glob("characters_*.json"))
            if not character_files:
                print("未找到角色JSON文件，使用默认角色映射")
                return

            # 读取最新的角色文件
            character_file = sorted(character_files)[-1]
            print(f"加载角色信息从: {character_file}")

            with open(character_file, 'r', encoding='utf-8') as f:
                characters = json.load(f)

            # 建立拼音到中文的映射
            for char in characters:
                chinese_name = char.get('name')
                if not chinese_name:
                    continue

                # 生成拼音（简单规则：中文转拼音）
                # 这里使用一个简单的映射表，实际项目中应该使用拼音转换库
                pinyin_mapping = {
                    "唐三": "tangsan",
                    "唐昊": "tanghao",
                    "老杰克": "laojieke",
                    "素云涛": "suyuntao"
                }

                pinyin_name = pinyin_mapping.get(chinese_name)
                if not pinyin_name:
                    # 如果没有映射，使用简单规则生成拼音
                    pinyin_name = chinese_name.lower().replace(" ", "")

                # 添加到映射
                self.pinyin_to_chinese[pinyin_name] = chinese_name

                # 为中文名生成ID（如果不存在）
                if chinese_name not in self.chinese_to_id:
                    # 直接使用完整的拼音作为角色的唯一ID，而不是使用前两个字母
                    full_pinyin_id = pinyin_name
                    candidate_id = full_pinyin_id
                    suffix = 1
                    existing_ids = set(self.chinese_to_id.values())
                    while candidate_id in existing_ids:
                        candidate_id = f"{full_pinyin_id}{suffix}"
                        suffix += 1
                    self.chinese_to_id[chinese_name] = candidate_id

            # 更新反向映射
            self.chinese_to_pinyin = {v: k for k, v in self.pinyin_to_chinese.items()}

            print(f"加载了 {len(characters)} 个角色信息")

        except Exception as e:
            print(f"加载角色信息时出错: {e}")
            import traceback
            traceback.print_exc()

    def _find_master_json(self):
        """寻找 outputs/{task_id}/{task_id}.json 这个总调度 JSON

        优先级：
        1. self.source_dir 下直接找 *.json（去掉 story_tree.json/atmophere.json/options.json 等已知非主表）
        2. self.source_dir.parent 下找
        3. 不存在则返回 None
        """
        ignore_names = {"story_tree.json", "atmophere.json", "options.json"}
        candidates_dirs = [self.source_dir, self.source_dir.parent]
        for d in candidates_dirs:
            if not d.exists():
                continue
            for jf in d.glob("*.json"):
                if jf.name in ignore_names:
                    continue
                # 简单校验：顶层是 dict，value 是 list of beat
                try:
                    with open(jf, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, dict) and data:
                        first_v = next(iter(data.values()))
                        if isinstance(first_v, list) and first_v and isinstance(first_v[0], dict) \
                                and 'beat_id' in first_v[0] and 'subtitle_id' in first_v[0]:
                            return jf, data
                except Exception:
                    continue
        return None, None

    def _resolve_chapter_key(self, master_data, node_dir_name):
        """根据节点目录名定位 master_data 中的章节 key

        - chapter_1 目录 → "chapter_1"
        - node_xxxx 目录 → "node_xxxx"（直接同名）
        - 否则尝试 startswith 匹配
        """
        if node_dir_name in master_data:
            return node_dir_name
        for k in master_data.keys():
            if k == node_dir_name or k.endswith(node_dir_name) or node_dir_name.endswith(k):
                return k
        return None

    @staticmethod
    def _parse_beat_id(beat_id):
        """解析 beat_id 字符串

        返回 (numeric_index, branch_letter, branch_seq)
        - "14" → (14, None, 0)
        - "151-A1" → (151, 'A', 1)
        - "151-B3" → (151, 'B', 3)
        - "151-C" / "151-C1" → (151, 'C', 0)
        """
        s = str(beat_id)
        m = re.match(r'^(\d+)-([AB])(\d+)$', s)
        if m:
            return int(m.group(1)), m.group(2), int(m.group(3))
        m = re.match(r'^(\d+)-C\d*$', s)
        if m:
            return int(m.group(1)), 'C', 0
        m = re.match(r'^(\d+)$', s)
        if m:
            return int(m.group(1)), None, 0
        return None, None, 0

    def load_name_mappings(self):
        """从 name_mappings.json 读取 speaker_id → 显示名 映射

        文件位置：images_base_dir 下（即 outputs/{task_id}/name_mappings.json）。
        结构：{ "change_name": {book_language: {speaker_id: 显示名}} }
        取 self.book_language 对应的子表存入 self.name_map。
        找不到文件或语言时静默回退（name_map 为空，display_name 回退原 id）。
        """
        try:
            candidates = [
                self.images_base_dir / "name_mappings.json",
                self.source_dir / "name_mappings.json",
            ]
            mapping_path = next((p for p in candidates if p.exists()), None)
            if mapping_path is None:
                print("⚠️ 未找到 name_mappings.json，说话人显示名将回退为 speaker_id")
                self.name_map = {}
                return

            with open(mapping_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 新结构的游戏显示名只读 change_name；旧扁平文件仍兼容。
            change_data = data.get("change_name") if isinstance(data, dict) else None
            if not isinstance(change_data, dict):
                change_data = data

            # 多语言：收集所有语言的 speaker_id → 显示名（供运行期按 URL lang 取）
            # change_name 结构：{ lang: {speaker_id: 名}, ..., "conversion": {...} }
            self.names_by_lang = {}
            for k, v in change_data.items():
                if k in {"conversion", "new-name"}:
                    continue
                if str(k) not in ONLINE_GAME_LANGUAGES:
                    continue
                if isinstance(v, dict) and all(isinstance(x, str) for x in v.values()):
                    self.names_by_lang[str(k)] = {str(a): str(b) for a, b in v.items()}

            preferred_language = next(
                (lang for lang in ONLINE_GAME_LANGUAGES
                 if isinstance(change_data.get(lang), dict)),
                None,
            )
            lang_map = change_data.get(preferred_language) if preferred_language else None
            if not isinstance(lang_map, dict):
                # 回退：若没有该语言键，尝试整体当成 speaker_id→名 的单层表
                if all(isinstance(v, str) for v in change_data.values()):
                    lang_map = change_data
                else:
                    print(f"⚠️ name_mappings.json 中无语言 {self.book_language}，显示名回退为 speaker_id")
                    self.name_map = {}
                    return

            self.name_map = {str(k): str(v) for k, v in lang_map.items()}
            print(f"✅ 加载说话人映射 ({self.book_language}): {len(self.name_map)} 个；全语言 {list(self.names_by_lang.keys())}")
        except Exception as e:
            print(f"⚠️ 加载 name_mappings.json 失败: {e}")
            self.name_map = {}

    @staticmethod
    def _is_narrator_speaker(speaker_id):
        """speaker_id 为空 / PangBai → 旁白（无具名）"""
        if not speaker_id:
            return True
        return str(speaker_id).strip().lower() == "pangbai"

    def display_name(self, speaker_id):
        """speaker_id → 显示名；旁白返回空串"""
        if self._is_narrator_speaker(speaker_id):
            return ""
        return self.name_map.get(speaker_id, speaker_id)

    # 5 槽位 → 距左边百分比
    SLOT_LEFT_PCT = {
        'left': 22,
        'left_center': 30,
        'center': 50,
        'right_center': 70,
        'right': 78,
    }

    @staticmethod
    def _split_top_level_csv(s):
        """按逗号切分顶层条目（括号内的逗号不切）"""
        items = []
        depth = 0
        cur = []
        for ch in s:
            if ch == '(':
                depth += 1
                cur.append(ch)
            elif ch == ')':
                depth = max(0, depth - 1)
                cur.append(ch)
            elif ch == ',' and depth == 0:
                items.append(''.join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            items.append(''.join(cur))
        return [it.strip() for it in items if it.strip()]

    def _parse_character_position(self, s):
        """解析 character_position 字段

        输入示例：`[zxy_1-1-1_calm_CS(left),lsb_1-1-1_clam_MS(center)]`
        返回：[{'stem': 'zxy_1-1-1_calm_CS', 'slot': 'left',
                'left_pct': 22, 'key': 'zxy'}, ...]
        - 括号外 = png 文件名（不含扩展名）
        - 括号内 = 槽位名（归一化小写，未知回退 center）
        - 角色身份 key = stem.split('_')[0]（用于跨 beat 持久化 diff）
        空串 / None → []
        """
        if not s:
            return []
        text = str(s).strip()
        if text.startswith('['):
            text = text[1:]
        if text.endswith(']'):
            text = text[:-1]
        text = text.strip()
        if not text:
            return []

        result = []
        for entry in self._split_top_level_csv(text):
            m = re.match(r'^(.+?)\((.+?)\)\s*$', entry)
            if m:
                stem = m.group(1).strip()
                slot = m.group(2).strip().lower()
            else:
                stem = entry.strip()
                slot = 'center'
            if not stem:
                continue
            left_pct = self.SLOT_LEFT_PCT.get(slot, self.SLOT_LEFT_PCT['center'])
            key = stem.split('_')[0]
            result.append({
                'stem': stem,
                'slot': slot,
                'left_pct': left_pct,
                'key': key,
            })
        return result

    def _resolve_scene_file(self, scene_id, shot_size, valid_scene_stems=None):
        return scene_id

    def _cast_to_js(self, cast):
        """把 _parse_character_position 的结果转成 JS 数组字面量

        供 StageManager.setCast 使用：[{stem, slot, leftPct, key}, ...]
        """
        items = []
        for c in (cast or []):
            items.append("{stem:%s,slot:%s,leftPct:%d,key:%s}" % (
                json.dumps(c['stem'], ensure_ascii=False),
                json.dumps(c['slot'], ensure_ascii=False),
                int(c['left_pct']),
                json.dumps(c['key'], ensure_ascii=False),
            ))
        return "[" + ",".join(items) + "]"

    def scan_assets(self):
        """扫描所有素材文件

        新逻辑：优先从 master JSON（{task_id}.json）按 beat 拿素材 ID，
        再去本节点目录下加后缀拼出真实文件路径。
        若找不到 master JSON，则回落到旧的文件名扫描方式。
        """
        print("扫描素材文件...")
        print(f"源目录: {self.source_dir}")
        print(f"图片基础目录: {self.images_base_dir}")

        # 加载 speaker_id → 显示名 映射
        self.load_name_mappings()

        # 清空现有列表
        self.audio_files = []
        self.subtitle_files = []
        self.scene_images = []
        self.character_images = []
        self.music_files = []
        self.ambience_files = []
        self.sfx_files = []
        self.valid_image_stems = set()
        self.master_beat_meta = {}
        self.branch_control_content = {}
        # 注意：不清除 character_map，因为它可能已由 load_character_info 加载

        master_json_path, master_data = self._find_master_json()

        if master_json_path is not None:
            print(f"✅ 检测到主调度 JSON: {master_json_path.name}")
            self._scan_assets_from_master_json(master_data)
        else:
            print("⚠️ 未检测到主调度 JSON，回落到旧的文件名扫描逻辑")
            self._scan_assets_legacy()

        # 场景图片和角色图片始终从磁盘扫描
        self._scan_image_assets()

        print(f"资源扫描完成: {len(self.audio_files)}个音频, {len(self.subtitle_files)}个字幕, "
              f"{len(self.scene_images)}个场景, {len(self.character_images)}个角色图片, "
              f"{len(self.music_files)}个BGM, {len(self.ambience_files)}个Ambience, {len(self.sfx_files)}个SFX")

    def _scan_assets_from_master_json(self, master_data):
        """基于主调度 JSON 构建 audio/subtitle/music/sfx 列表"""
        # 确定本节点对应的章节 key
        node_dir_name = self.source_dir.name
        chapter_key = self._resolve_chapter_key(master_data, node_dir_name)

        if chapter_key is None:
            print(f"⚠️ 主 JSON 中找不到章节 key（目录名={node_dir_name}），跳过 JSON 扫描")
            return

        beats = master_data.get(chapter_key, [])
        if not beats:
            print(f"⚠️ 章节 {chapter_key} 在主 JSON 中为空")
            return

        print(f"  章节 key: {chapter_key}, beats: {len(beats)}")

        # ── 多语言目录发现 ──
        # 上游结构：audios/{lang}-audio/、audios/{lang}-subtitle/（文件名跨语言相同）
        # bgm/sfx 通用。回退兼容旧结构 audios/audio、audios/subtitle。
        audios_root = self.source_dir / "audios"
        self.languages = []
        for lang in AUDIO_OUTPUT_LOCALES:
            d = audios_root / f"{lang}-subtitle"
            if d.is_dir():
                self.languages.append(lang)
        # 旧结构兼容：没有 {lang}-subtitle 时退回单语言 audios/subtitle
        legacy_mode = not self.languages
        if legacy_mode:
            self.languages = [DEFAULT_AUDIO_LANGUAGE]

        # 运行期默认英语；book_language 仍只负责源文本与角色显示名语义。
        self.default_language = choose_default_audio_language(
            self.languages, self.book_language
        )

        def _audio_dir_for(lang):
            return audios_root / "audio" if legacy_mode else audios_root / f"{lang}-audio"

        def _subtitle_dir_for(lang):
            return audios_root / "subtitle" if legacy_mode else audios_root / f"{lang}-subtitle"

        # 默认语言目录（驱动 beat 结构、index、meta）
        audio_dir = _audio_dir_for(self.default_language)
        subtitle_dir = _subtitle_dir_for(self.default_language)
        bgm_dir = audios_root / "bgm"
        sfx_dir = audios_root / "sfx"
        # Ambience（环境底音）目录
        ambience_dir = audios_root / "ambience"

        # 各语言字幕文本 / 音频存在性
        # self.i18n_text[lang][subtitle_id] = 纯正文（去 speaker 前缀）
        # self.voices_by_lang[lang][subtitle_id] = '{subtitle_id}.wav'（存在才记）
        self.i18n_text = {lang: {} for lang in self.languages}
        self.voices_by_lang = {lang: {} for lang in self.languages}
        print(f"  发现语言: {self.languages}（默认 {self.default_language}）")

        # BGM：按 bgm_id 出现的 beat 范围聚合 start/end
        bgm_ranges = {}   # bgm_id -> [min_idx, max_idx]
        main_bgm_ids = set()
        # Ambience（环境底音）：规则与 BGM 完全一致，按 ambience_id 聚合 beat 范围
        ambience_ranges = {}  # ambience_id -> [min_idx, max_idx]
        main_ambience_ids = set()
        # SFX：按 sfx_id 记录 beat 索引（通常每个 sfx_id 只在一个 beat 触发）
        sfx_index_map = {}  # sfx_id -> first numeric idx

        for beat in beats:
            beat_id = beat.get('beat_id')
            num_idx, branch_letter, branch_seq = self._parse_beat_id(beat_id)
            if num_idx is None:
                continue

            subtitle_id = beat.get('subtitle_id')
            speaker_id = beat.get('speaker_id')
            scene_id = beat.get('scene_id')
            bgm_id = beat.get('bgm_id')
            ambience_id = beat.get('ambience_id')
            sfx_id = beat.get('sfx_id')
            system_text = beat.get('system_text')
            nvl_mode = bool(beat.get('nvl_mode'))
            cg_mode = bool(beat.get('cg_mode'))
            scene_shot_size = beat.get('scene_shot_size') or ''
            character_position = beat.get('character_position') or ''

            # 软分支元信息（用于 organize_materials 把它们聚合成 choice_node）
            is_branch = branch_letter is not None
            meta = {
                'index': num_idx,
                'scene_id': scene_id if scene_id else 'default',
                'chapter': 1, 'scene': 1,
                'is_branch': is_branch,
                'branch': branch_letter,
                'branch_seq': branch_seq,
                'beat_id_raw': str(beat_id),
                'system_text': system_text,
                'nvl_mode': nvl_mode,
                'cg_mode': cg_mode,
                'speaker_id': speaker_id,
                'scene_shot_size': scene_shot_size,
                'character_position': character_position,
                'effects_a': beat.get('effects_a'),
                'effects_b': beat.get('effects_b'),
                'bgm_id': bgm_id,
                'ambience_id': ambience_id,
                'sfx_id': sfx_id,
            }
            self.master_beat_meta[str(beat_id)] = meta

            # ── 字幕 ──
            if subtitle_id:
                sub_path = subtitle_dir / f"{subtitle_id}.txt"
                if sub_path.exists():
                    self.subtitle_files.append({
                        'path': sub_path,
                        'name': sub_path.name,
                        'stem': subtitle_id,
                        'meta': meta,
                    })
                else:
                    print(f"  ⚠️ 字幕缺失: {sub_path}")

                # 多语言：收集每个语言的纯正文进 i18n_text
                # 软分支 seq==1 字幕含 `选项文本||正文`：拆开，正文存主 key，选项存 {sid}__opt
                for lang in self.languages:
                    lpath = _subtitle_dir_for(lang) / f"{subtitle_id}.txt"
                    if lpath.exists():
                        raw = self.read_subtitle_file(lpath)
                        plain = self._extract_plain_text(raw)
                        if '||' in plain:
                            opt_part, _, body_part = plain.partition('||')
                            self.i18n_text[lang][subtitle_id] = body_part.strip()
                            self.i18n_text[lang][f"{subtitle_id}__opt"] = opt_part.strip()
                        else:
                            self.i18n_text[lang][subtitle_id] = plain

                # C/C1 是分支控制文件，其 effects 与语言无关。只从当前游戏
                # 已声明的语言目录读取；构建器不得依赖任务名或备份目录约定。
                if is_branch and branch_letter == 'C':
                    control_raw = ''
                    for lang in [self.default_language] + [x for x in self.languages if x != self.default_language]:
                        candidate = _subtitle_dir_for(lang) / f"{subtitle_id}.txt"
                        if candidate.exists():
                            control_raw = self.read_subtitle_file(candidate)
                            if control_raw:
                                break
                    if control_raw:
                        self.branch_control_content[num_idx] = control_raw

            # ── 音频（PangBai 旁白没有音频）──
            if subtitle_id and speaker_id and speaker_id != 'PangBai':
                audio_path = audio_dir / f"{subtitle_id}.wav"
                if audio_path.exists():
                    self.audio_files.append({
                        'path': audio_path,
                        'name': audio_path.name,
                        'stem': subtitle_id,
                        'meta': meta,
                    })
                else:
                    print(f"  ⚠️ 音频缺失: {audio_path} (speaker={speaker_id})")

                # 多语言：记录每个语言该 beat 音频是否存在
                for lang in self.languages:
                    lpath = _audio_dir_for(lang) / f"{subtitle_id}.wav"
                    if lpath.exists():
                        self.voices_by_lang[lang][subtitle_id] = f"{subtitle_id}.wav"

            # ── BGM ──（聚合 beat 范围）
            if bgm_id:
                rng = bgm_ranges.setdefault(bgm_id, [num_idx, num_idx])
                if num_idx < rng[0]:
                    rng[0] = num_idx
                if num_idx > rng[1]:
                    rng[1] = num_idx
                if not is_branch:
                    main_bgm_ids.add(bgm_id)

            # ── Ambience（环境底音）──（规则同 BGM：聚合 beat 范围）
            if ambience_id:
                rng = ambience_ranges.setdefault(ambience_id, [num_idx, num_idx])
                if num_idx < rng[0]:
                    rng[0] = num_idx
                if num_idx > rng[1]:
                    rng[1] = num_idx
                if not is_branch:
                    main_ambience_ids.add(ambience_id)

            # ── SFX ──（一次性绑定到首次出现的 numeric beat）
            if sfx_id and sfx_id not in sfx_index_map:
                sfx_index_map[sfx_id] = num_idx

        # 写入 BGM 列表
        for bgm_id, (start_idx, end_idx) in bgm_ranges.items():
            bgm_path = bgm_dir / f"{bgm_id}.wav"
            if not bgm_path.exists():
                # 兜底：按尾部 _start-end 模糊匹配（兼容旧 uuid 命名）
                fallback = self._fuzzy_match_bgm_file(bgm_dir, bgm_id, start_idx, end_idx)
                if fallback is not None:
                    bgm_path = fallback
                    print(f"  ℹ️ BGM 名不匹配，按范围匹配到: {bgm_path.name} (期望 {bgm_id})")
            if bgm_path.exists():
                self.music_files.append({
                    'path': bgm_path,
                    'name': bgm_path.name,
                    'stem': bgm_path.stem,
                    'start': start_idx,
                    'end': end_idx,
                    'declared_id': bgm_id,
                    'main_timeline': bgm_id in main_bgm_ids,
                })
                print(f"  发现BGM: {bgm_path.name} (索引范围: {start_idx} - {end_idx})")
            else:
                print(f"  ⚠️ BGM 缺失: {bgm_dir / (bgm_id + '.wav')}")

        # 写入 Ambience（环境底音）列表（规则同 BGM）
        for ambience_id, (start_idx, end_idx) in ambience_ranges.items():
            amb_path = ambience_dir / f"{ambience_id}.wav"
            if not amb_path.exists():
                # 兜底：按尾部 _start-end 模糊匹配（兼容旧 uuid 命名）
                fallback = self._fuzzy_match_bgm_file(ambience_dir, ambience_id, start_idx, end_idx)
                if fallback is not None:
                    amb_path = fallback
                    print(f"  ℹ️ Ambience 名不匹配，按范围匹配到: {amb_path.name} (期望 {ambience_id})")
            if amb_path.exists():
                self.ambience_files.append({
                    'path': amb_path,
                    'name': amb_path.name,
                    'stem': amb_path.stem,
                    'start': start_idx,
                    'end': end_idx,
                    'declared_id': ambience_id,
                    'main_timeline': ambience_id in main_ambience_ids,
                })
                print(f"  发现Ambience: {amb_path.name} (索引范围: {start_idx} - {end_idx})")
            else:
                print(f"  ⚠️ Ambience 缺失: {ambience_dir / (ambience_id + '.wav')}")

        # 写入 SFX 列表
        for sfx_id, idx in sfx_index_map.items():
            sfx_path = sfx_dir / f"{sfx_id}.wav"
            if not sfx_path.exists():
                fallback = self._fuzzy_match_sfx_file(sfx_dir, sfx_id, idx)
                if fallback is not None:
                    sfx_path = fallback
                    print(f"  ℹ️ SFX 名不匹配，按尾部 index 匹配到: {sfx_path.name} (期望 {sfx_id})")
            if sfx_path.exists():
                self.sfx_files.append({
                    'path': sfx_path,
                    'name': sfx_path.name,
                    'stem': sfx_path.stem,
                    'index': idx,
                    'declared_id': sfx_id,
                })
                print(f"  发现SFX: {sfx_path.name} (索引: {idx})")
            else:
                print(f"  ⚠️ SFX 缺失: {sfx_dir / (sfx_id + '.wav')}")

    @staticmethod
    def _fuzzy_match_bgm_file(bgm_dir, bgm_id, start_idx, end_idx):
        """按尾部 _start-end 后缀在 bgm 目录里找匹配的文件（兼容旧命名）"""
        if not bgm_dir.exists():
            return None
        suffix = f"_{start_idx}-{end_idx}"
        for f in bgm_dir.glob("*.wav"):
            if f.stem.endswith(suffix):
                return f
        return None

    @staticmethod
    def _fuzzy_match_sfx_file(sfx_dir, sfx_id, idx):
        """按尾部 _index 后缀在 sfx 目录里找匹配的文件（兼容旧命名）"""
        if not sfx_dir.exists():
            return None
        suffix = f"_{idx}"
        for f in sfx_dir.glob("*.wav"):
            if f.stem.endswith(suffix):
                return f
        return None

    def _scan_assets_legacy(self):
        """旧的、按文件名扫描素材的方式（保留作回落）"""
        # 确定要扫描的节点目录列表
        node_dirs_to_scan = []
        if self.source_dir.name.startswith('node'):
            node_dirs_to_scan = [self.source_dir]
        else:
            for node_dir in self.source_dir.glob("node*"):
                if node_dir.is_dir():
                    node_dirs_to_scan.append(node_dir)

        if not node_dirs_to_scan:
            print("警告：未找到任何节点目录，将尝试直接扫描源目录下的 audios 文件夹")
            node_dirs_to_scan = [self.source_dir]

        # 扫描音频文件
        for node_dir in node_dirs_to_scan:
            audio_dir = node_dir / "audios" / "audio"
            if audio_dir.exists():
                for audio_file in audio_dir.glob("*.wav"):
                    self.audio_files.append({
                        'path': audio_file,
                        'name': audio_file.name,
                        'stem': audio_file.stem
                    })
            else:
                audio_dir_alt = node_dir / "audio"
                if audio_dir_alt.exists():
                    for audio_file in audio_dir_alt.glob("*.wav"):
                        self.audio_files.append({
                            'path': audio_file,
                            'name': audio_file.name,
                            'stem': audio_file.stem
                        })

        # 扫描字幕文件
        for node_dir in node_dirs_to_scan:
            subtitle_dir = node_dir / "audios" / "subtitle"
            if subtitle_dir.exists():
                for subtitle_file in subtitle_dir.glob("*.txt"):
                    self.subtitle_files.append({
                        'path': subtitle_file,
                        'name': subtitle_file.name,
                        'stem': subtitle_file.stem
                    })
            else:
                subtitle_dir_alt = node_dir / "subtitle"
                if subtitle_dir_alt.exists():
                    for subtitle_file in subtitle_dir_alt.glob("*.txt"):
                        self.subtitle_files.append({
                            'path': subtitle_file,
                            'name': subtitle_file.name,
                            'stem': subtitle_file.stem
                        })

        # BGM
        for node_dir in node_dirs_to_scan:
            for music_dir in [node_dir / "audios" / "bgm", node_dir / "audios" / "music", node_dir / "music"]:
                if music_dir.exists():
                    for music_file in music_dir.glob("*.wav"):
                        match = self.music_pattern.match(music_file.stem)
                        if match:
                            start_idx = int(match.group(2))
                            end_idx = int(match.group(3))
                            self.music_files.append({
                                'path': music_file,
                                'name': music_file.name,
                                'stem': music_file.stem,
                                'start': start_idx,
                                'end': end_idx
                            })
                        else:
                            match_legacy = self.music_pattern_legacy.match(music_file.stem)
                            if match_legacy:
                                start_idx = int(match_legacy.group(1))
                                end_idx = int(match_legacy.group(2))
                                self.music_files.append({
                                    'path': music_file,
                                    'name': music_file.name,
                                    'stem': music_file.stem,
                                    'start': start_idx,
                                    'end': end_idx
                                })
                    break

        # SFX
        for node_dir in node_dirs_to_scan:
            sfx_dir = node_dir / "audios" / "sfx"
            if sfx_dir.exists():
                for sfx_file in sfx_dir.glob("*.wav"):
                    match = self.sfx_pattern.match(sfx_file.stem)
                    if match:
                        sfx_idx = int(match.group(2))
                        self.sfx_files.append({
                            'path': sfx_file,
                            'name': sfx_file.name,
                            'stem': sfx_file.stem,
                            'index': sfx_idx
                        })

    def _scan_image_assets(self):
        """扫描场景图片和角色图片（共用 self.images_base_dir）"""
        scenes_dir = self.images_base_dir / "images" / "scenes"
        if scenes_dir.exists():
            for scene_file in scenes_dir.glob("*.png"):
                self.scene_images.append({
                    'path': scene_file,
                    'name': scene_file.name,
                    'stem': scene_file.stem
                })
        else:
            print(f"警告：场景图片目录不存在: {scenes_dir}")

        # CG 插画单独生成在 images/cg/，运行时作为无字幕 beat 的普通场景图，
        # 与其他场景图共用 assets/scenes/。
        cg_dir = self.images_base_dir / "images" / "cg"
        if cg_dir.exists():
            existing_stems = {s['stem'] for s in self.scene_images}
            for cg_file in cg_dir.glob("*.png"):
                if cg_file.stem in existing_stems:
                    continue
                self.scene_images.append({
                    'path': cg_file,
                    'name': cg_file.name,
                    'stem': cg_file.stem
                })

        characters_dir = self.images_base_dir / "images" / "characters"
        if characters_dir.exists():
            import re as _re
            for char_file in characters_dir.glob("*.png"):
                stem = char_file.stem
                self.valid_image_stems.add(stem)

                parts = _re.split(r'[-_]', stem)
                pinyin_base = parts[0] if parts else stem

                if pinyin_base not in self.pinyin_to_chinese:
                    self.pinyin_to_chinese[pinyin_base] = pinyin_base
                    self.chinese_to_pinyin = {v: k for k, v in self.pinyin_to_chinese.items()}

                chinese_name = self.pinyin_to_chinese.get(pinyin_base, pinyin_base)

                self.character_images.append({
                    'path': char_file,
                    'name': char_file.name,
                    'pinyin': pinyin_base,
                    'chinese': chinese_name,
                    'full_stem': stem
                })

                if chinese_name not in self.character_map:
                    if chinese_name not in self.chinese_to_id:
                        full_pinyin_id = pinyin_base
                        candidate_id = full_pinyin_id
                        suffix = 1
                        existing_ids = set(self.chinese_to_id.values())
                        while candidate_id in existing_ids:
                            candidate_id = f"{full_pinyin_id}{suffix}"
                            suffix += 1
                        self.chinese_to_id[chinese_name] = candidate_id

                    char_id = self.chinese_to_id[chinese_name]
                    self.character_map[chinese_name] = {
                        'id': char_id,
                        'pinyin': pinyin_base,
                        'image': char_file.name,
                        'color': self.color_palette[len(self.character_map) % len(self.color_palette)]
                    }
        else:
            print(f"警告：角色图片目录不存在: {characters_dir}")

    def parse_subtitle_content(self, content):
        """解析字幕内容，支持多种格式（已修改：保留原始前缀）"""
        if not content:
            return []

        lines = []
        for line in content.split('\n'):
            line = line.strip()
            if not line: continue

            speaker_raw = None
            text = None
            for pattern in self.subtitle_patterns:
                match = pattern.match(line)
                if match:
                    speaker_raw = match.group(1).strip()  # 这里拿到了 "tanghao_1_pingjing"
                    text = match.group(2).strip()
                    break

            if speaker_raw is None:
                speaker_raw = "旁白"
                text = line

            # 【关键修改】保留原始 key 用于查找图片
            raw_key = speaker_raw

            # 计算显示用的名字 (如果 raw_key 是文件名，我们只取第一部分作为名字)
            # 例如: "tanghao_1_pingjing" -> speaker_for_name = "tanghao"
            speaker_for_name = raw_key.split('_')[0] if '_' in raw_key else raw_key

            # 规范化中文名 (例如 "tanghao" -> "唐昊")
            speaker = self.normalize_speaker(speaker_for_name)

            lines.append({
                'speaker': speaker,  # 显示的名字：唐昊
                'raw_key': raw_key,  # 原始前缀：tanghao_1_pingjing (用于找图)
                'text': text,
                'type': 'dialogue' if speaker != '旁白' else 'narration'
            })

        return lines

    def _extract_plain_text(self, raw):
        """从字幕文件原始内容提取纯正文（去掉 speaker 前缀 + 清洗交互标记）

        字幕格式：`{speaker}：{text}`（每文件通常一行）。
        用于多语言 i18n：只要正文，不要 speaker（speaker 由 beat 的 speaker_id 决定）。
        清洗逻辑与对话渲染一致（去 QTE/TIMING/MEMORY/INTERACTION）。
        返回的文本不做 JS 转义（由 json.dumps 序列化时处理）。
        """
        if not raw:
            return ""
        import re
        parts = []
        for line in raw.split('\n'):
            line = line.strip()
            if not line:
                continue
            text = None
            for pattern in self.subtitle_patterns:
                m = pattern.match(line)
                if m:
                    text = m.group(2).strip()
                    break
            if text is None:
                text = line  # 无 speaker 前缀，整行即正文
            parts.append(text)
        text_display = ' '.join(parts).strip()

        # 清洗交互标记（与对话渲染保持一致）
        for tag in ("(QTE)", "（QTE）", "(TIMING)", "（TIMING）", "(MEMORY)", "（MEMORY）"):
            text_display = text_display.replace(tag, "")
        interaction_pattern = r'(?:[\(\[（\s]*)INTERACTION\s*[:：]\s*(.*)$'
        m = re.search(interaction_pattern, text_display, re.DOTALL | re.IGNORECASE)
        if m:
            text_display = text_display.replace(m.group(0), "")
        return text_display.strip()

        """
        智能拆分拼音字符串
        支持驼峰: TangSan -> ['tang', 'san']
        支持下划线: tang_san -> ['tang', 'san']
        支持混合: TangSan_01 -> ['tang', 'san']
        """
        import re
        # 1. 去掉结尾的数字 (如 TangSan1 -> TangSan)
        text = re.sub(r'\d+', '', text)

        # 2. 处理驼峰：在小写字母和大写字母之间插入空格
        # TangSan -> Tang San
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)

        # 3. 将下划线替换为空格
        text = text.replace('_', ' ')

        # 4. 分割并转小写
        parts = text.split()
        clean_parts = [p.lower() for p in parts if p.isalpha()]

        return clean_parts

    def pinyin_to_chinese_ime(self, pinyin_str):
        """利用输入法概率库反推中文"""
        try:
            from Pinyin2Hanzi import DefaultDagParams, dag

            # 1. 拆分拼音
            pinyin_list = self.split_pinyin_str(pinyin_str)

            if not pinyin_list:
                return pinyin_str

            # 2. 喂给算法
            dag_params = DefaultDagParams()
            result = dag(dag_params, pinyin_list, path_num=1)

            if result:
                return result[0].path[0]  # 返回概率最高的词
            else:
                return pinyin_str

        except ImportError:
            print("【提示】安装 pip install Pinyin2Hanzi 可开启自动中文识别")
            return pinyin_str
        except Exception:
            return pinyin_str

    def normalize_speaker(self, speaker_raw):
        """规范化说话人名称"""
        import re  # 确保 re 模块在函数作用域内可用
        # 去除空格
        speaker = speaker_raw.strip()

        if not speaker:
            return "旁白"

        # 首先检查是否已经是中文名（在角色映射中）
        if speaker in self.character_map:
            return speaker

        # 检查是否已经在中文ID映射中
        if speaker in self.chinese_to_id:
            return speaker

        # 检查是否是已知的拼音，转换为中文
        if speaker in self.pinyin_to_chinese:
            return self.pinyin_to_chinese[speaker]

        # 尝试提取拼音部分：如果说话人包含下划线，可能是二级标题如"tangsan_1_pingjing"
        # 提取第一个下划线之前的部分作为拼音候选
        if '_' in speaker:
            pinyin_candidate = speaker.split('_')[0]
            if pinyin_candidate in self.pinyin_to_chinese:
                return self.pinyin_to_chinese[pinyin_candidate]
            # 【核心】尝试使用输入法自动识别拼音候选
            ime_name = self.pinyin_to_chinese_ime(pinyin_candidate)
            if ime_name != pinyin_candidate and re.search(r'[一-鿿]', ime_name):
                # 缓存结果，避免重复计算
                self.pinyin_to_chinese[pinyin_candidate] = ime_name
                # 更新反向映射
                self.chinese_to_pinyin[ime_name] = pinyin_candidate
                # 同时添加到中文到ID映射（如果不存在）
                if ime_name not in self.chinese_to_id:
                    # 直接使用完整的拼音作为角色的唯一ID，而不是使用前两个字母
                    full_pinyin_id = pinyin_candidate
                    candidate_id = full_pinyin_id
                    suffix = 1
                    existing_ids = set(self.chinese_to_id.values())
                    while candidate_id in existing_ids:
                        candidate_id = f"{full_pinyin_id}{suffix}"
                        suffix += 1
                    self.chinese_to_id[ime_name] = candidate_id
                return ime_name
            # 如果拼音候选不在映射中，但看起来像拼音（只包含字母），则将其作为拼音添加映射
            # 中文名暂时使用拼音候选（后续可能被load_character_info覆盖）
            if pinyin_candidate.isalpha():
                self.pinyin_to_chinese[pinyin_candidate] = pinyin_candidate
                # 更新反向映射
                self.chinese_to_pinyin[pinyin_candidate] = pinyin_candidate
                # 同时添加到中文到ID映射（如果不存在）
                if pinyin_candidate not in self.chinese_to_id:
                    # 直接使用完整的拼音作为角色的唯一ID，而不是使用前两个字母
                    full_pinyin_id = pinyin_candidate
                    candidate_id = full_pinyin_id
                    suffix = 1
                    existing_ids = set(self.chinese_to_id.values())
                    while candidate_id in existing_ids:
                        candidate_id = f"{full_pinyin_id}{suffix}"
                        suffix += 1
                    self.chinese_to_id[pinyin_candidate] = candidate_id
                return pinyin_candidate

        # 【核心】尝试使用输入法自动识别整个字符串
        ime_name = self.pinyin_to_chinese_ime(speaker)
        if ime_name != speaker and re.search(r'[一-鿿]', ime_name):
            # 缓存结果，避免重复计算
            self.pinyin_to_chinese[speaker] = ime_name
            # 更新反向映射
            self.chinese_to_pinyin[ime_name] = speaker
            # 同时添加到中文到ID映射（如果不存在）
            if ime_name not in self.chinese_to_id:
                # 直接使用完整的拼音作为角色的唯一ID，而不是使用前两个字母
                full_pinyin_id = speaker
                candidate_id = full_pinyin_id
                suffix = 1
                existing_ids = set(self.chinese_to_id.values())
                while candidate_id in existing_ids:
                    candidate_id = f"{full_pinyin_id}{suffix}"
                    suffix += 1
                self.chinese_to_id[ime_name] = candidate_id
            return ime_name

        # 检查是否包含中文字符，如果是则当作中文名返回
        # 简单的中文判断：检查是否包含中文字符

        chinese_pattern = re.compile(r'[一-鿿]')
        if chinese_pattern.search(speaker):
            return speaker  # 包含中文，返回原中文名

        # 默认当作旁白
        return "旁白"

    def extract_scene_info(self, filename):
        """从文件名提取场景信息，支持软分支格式（scene_id-index-A1/B2/C）"""
        stem = Path(filename).stem

        # 优先检测软分支格式（必须在普通格式之前，否则会被 scene_id-index 误匹配）
        m = self.branch_pattern.match(stem)
        if m:
            return {
                'scene_id': m.group(1),
                'index': int(m.group(2)),
                'branch': m.group(3),       # 'A' 或 'B'
                'branch_seq': int(m.group(4)),  # 1, 2, 3...
                'is_branch': True,
                'chapter': 1, 'scene': 1
            }
        m = self.branch_c_pattern.match(stem)
        if m:
            return {
                'scene_id': m.group(1),
                'index': int(m.group(2)),
                'branch': 'C',
                'branch_seq': 0,
                'is_branch': True,
                'chapter': 1, 'scene': 1
            }

        for pattern in self.scene_patterns:
            match = pattern.match(stem)
            if match:
                if len(match.groups()) == 2:
                    return {
                        'scene_id': match.group(1),
                        'index': int(match.group(2)),
                        'chapter': 1, 'scene': 1
                    }
                elif len(match.groups()) == 3:
                    return {
                        'scene_id': f"scene_{match.group(1)}_{match.group(2)}",
                        'index': int(match.group(3)),
                        'chapter': int(match.group(1)),
                        'scene': int(match.group(2))
                    }

        return {'scene_id': 'default', 'index': 1, 'chapter': 1, 'scene': 1}

    def read_subtitle_file(self, subtitle_path):
        """读取字幕文件内容"""
        try:
            with open(subtitle_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except:
            return ""

    def _parse_branch_effects(self, content):
        """从 C 文件内容中提取 effects_a / effects_b，返回 {'a': {...}, 'b': {...}}"""
        import re as _re
        effects = {'a': {}, 'b': {}, 'legacy_flags_a': {}, 'legacy_flags_b': {}}
        # 匹配 <!-- effects_a={...} effects_b={...} -->
        m = _re.search(r'effects_a=(\{[^}]*\})', content)
        if m:
            try:
                effects['a'] = json.loads(m.group(1).replace("'", '"'))
            except Exception:
                pass
        m = _re.search(r'effects_b=(\{[^}]*\})', content)
        if m:
            try:
                effects['b'] = json.loads(m.group(1).replace("'", '"'))
            except Exception:
                pass
        for side in ('a', 'b'):
            m = _re.search(rf'legacy_flags_{side}=(\{{[^}}]*\}})', content)
            if m:
                try:
                    parsed = json.loads(m.group(1).replace("'", '"'))
                    if isinstance(parsed, dict):
                        effects[f'legacy_flags_{side}'] = {
                            str(key): int(value)
                            for key, value in parsed.items()
                            if str(key).strip() and int(value) > 0
                        }
                except Exception:
                    pass
        return effects

    def _normalize_branch_effects(self, value):
        """把主调度 JSON 的 effects 字段规范为数值字典。"""
        if value is None or value == '':
            return None
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                try:
                    import ast
                    parsed = ast.literal_eval(value)
                except Exception:
                    return None
        if not isinstance(parsed, dict):
            return None
        result = {}
        for key, amount in parsed.items():
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                continue
            result[str(key)] = amount
        return result

    def organize_materials(self):
        """组织素材，按索引排序；软分支 index 聚合为 choice_node"""
        print("组织素材（按索引排序，不分章）...")

        # ── 第一步：分桶 ──────────────────────────────────────────────
        # normal_audio/subtitle: {index: item}
        # branch_audio/subtitle: {index: {'A': {seq: item}, 'B': {seq: item}, 'C': item}}
        normal_audio    = {}
        normal_subtitle = {}
        branch_audio    = {}   # index -> branch -> seq -> item
        branch_subtitle = {}   # index -> branch -> seq -> item
        branch_c_content = dict(self.branch_control_content)  # index -> raw content string
        branch_meta = {}  # index -> branch -> seq -> master JSON meta
        for _meta in self.master_beat_meta.values():
            if not _meta.get('is_branch'):
                continue
            branch_meta.setdefault(_meta['index'], {}).setdefault(_meta['branch'], {})[_meta['branch_seq']] = _meta
        # 普通 beat 的 meta（system_text / nvl_mode 等），按 index 聚合
        beat_meta_by_index = {}

        for audio in self.audio_files:
            # 优先用 audio item 自带的 meta（来自 master JSON），否则回落到文件名解析
            info = audio.get('meta') or self.extract_scene_info(audio['name'])
            item = {
                'type': 'audio', 'file': audio['name'], 'id': audio['stem'],
                'index': info['index'], 'scene_id': info['scene_id'],
                'chapter': info.get('chapter', 1), 'scene': info.get('scene', 1),
                'path': audio['path'], 'is_audio': True,
                'meta': info,
            }
            if info.get('is_branch'):
                br = info['branch']
                if br == 'C':
                    pass  # C 文件只有字幕，音频忽略
                else:
                    branch_audio.setdefault(info['index'], {}).setdefault(br, {})[info['branch_seq']] = item
            else:
                normal_audio[info['index']] = item

        for subtitle in self.subtitle_files:
            info = subtitle.get('meta') or self.extract_scene_info(subtitle['name'])
            content = self.read_subtitle_file(subtitle['path'])
            if not content:
                continue
            if info.get('is_branch'):
                br = info['branch']
                idx = info['index']
                if br == 'C':
                    branch_c_content[idx] = content
                else:
                    # 解析 || 分隔：|| 前是选项文本，|| 后是实际对话
                    option_text = None
                    actual_content = content
                    if info['branch_seq'] == 1 and '||' in content:
                        parts = content.split('||', 1)
                        raw_option = parts[0].strip()
                        # 去掉说话人前缀（如 "旁白：" / "PangBai:" 等）
                        import re as _re
                        _m = _re.match(r'^[^:：]+[：:](.+)$', raw_option)
                        option_text = _m.group(1).strip() if _m else raw_option
                        actual_content = parts[1].strip()
                    dialogues = self.parse_subtitle_content(actual_content)
                    item = {
                        'type': 'subtitle', 'file': subtitle['name'],
                        'subtitle_id': subtitle.get('stem'),   # i18n key
                        'index': idx, 'scene_id': info['scene_id'],
                        'chapter': info.get('chapter', 1), 'scene': info.get('scene', 1),
                        'dialogues': dialogues, 'is_subtitle': True,
                        'option_text': option_text,  # 仅 seq==1 时有值
                        'meta': info,
                    }
                    branch_subtitle.setdefault(idx, {}).setdefault(br, {})[info['branch_seq']] = item
            else:
                dialogues = self.parse_subtitle_content(content)
                normal_subtitle[info['index']] = {
                    'type': 'subtitle', 'file': subtitle['name'],
                    'subtitle_id': subtitle.get('stem'),   # i18n key
                    'index': info['index'], 'scene_id': info['scene_id'],
                    'chapter': info.get('chapter', 1), 'scene': info.get('scene', 1),
                    'dialogues': dialogues, 'is_subtitle': True,
                    'meta': info,
                }

            # 把普通 beat 的 system_text / nvl_mode / cg_mode 记录下来（不论字幕还是没字幕）
            if not info.get('is_branch'):
                beat_meta_by_index[info['index']] = {
                    'beat_id_raw': info.get('beat_id_raw', ''),
                    'system_text': info.get('system_text'),
                    'nvl_mode': bool(info.get('nvl_mode')),
                    'cg_mode': bool(info.get('cg_mode')),
                    'scene_id': info.get('scene_id', 'default'),
                    'scene_shot_size': info.get('scene_shot_size', ''),
                    'character_position': info.get('character_position', ''),
                    'speaker_id': info.get('speaker_id'),
                    'effects_a': info.get('effects_a'),
                    'effects_b': info.get('effects_b'),
                    'sfx_id': info.get('sfx_id'),
                    'bgm_id': info.get('bgm_id'),
                    'ambience_id': info.get('ambience_id'),
                }

        # 即便某 beat 没有字幕（比如纯系统提示 beat），也把它的 meta 记下来
        for audio in self.audio_files:
            info = audio.get('meta')
            if info and not info.get('is_branch'):
                idx = info['index']
                if idx not in beat_meta_by_index:
                    beat_meta_by_index[idx] = {
                        'beat_id_raw': info.get('beat_id_raw', ''),
                        'system_text': info.get('system_text'),
                        'nvl_mode': bool(info.get('nvl_mode')),
                        'cg_mode': bool(info.get('cg_mode')),
                        'scene_id': info.get('scene_id', 'default'),
                        'scene_shot_size': info.get('scene_shot_size', ''),
                        'character_position': info.get('character_position', ''),
                        'speaker_id': info.get('speaker_id'),
                        'effects_a': info.get('effects_a'),
                        'effects_b': info.get('effects_b'),
                        'sfx_id': info.get('sfx_id'),
                        'bgm_id': info.get('bgm_id'),
                        'ambience_id': info.get('ambience_id'),
                    }

        # ── 第二步：组装线性列表 ──────────────────────────────────────
        all_indices = set(normal_audio.keys()) | set(normal_subtitle.keys()) | \
                      set(branch_audio.keys()) | set(branch_subtitle.keys())

        all_materials = []
        for idx in sorted(all_indices):
            if idx in branch_audio or idx in branch_subtitle:
                # pivot beat（与分支同索引的普通 beat）先入列，否则它的台词和语音会被丢掉
                pivot_audio = normal_audio.get(idx)
                pivot_subtitle = normal_subtitle.get(idx)
                if pivot_audio:
                    all_materials.append(pivot_audio)
                if pivot_subtitle:
                    all_materials.append(pivot_subtitle)

                # 聚合为 choice_node
                def _build_branch_materials(br_letter):
                    audio_seqs    = branch_audio.get(idx, {}).get(br_letter, {})
                    subtitle_seqs = branch_subtitle.get(idx, {}).get(br_letter, {})
                    meta_seqs = branch_meta.get(idx, {}).get(br_letter, {})
                    seqs = sorted(set(list(audio_seqs.keys()) + list(subtitle_seqs.keys()) + list(meta_seqs.keys())))
                    result = []
                    for s in seqs:
                        a = audio_seqs.get(s)
                        sub = subtitle_seqs.get(s)
                        result.append({
                            'audio': a, 'subtitle': sub, 'seq': s,
                            'meta': branch_meta.get(idx, {}).get(br_letter, {}).get(s)
                                    or (sub or {}).get('meta') or (a or {}).get('meta') or {},
                        })
                    return result

                branch_a_mats = _build_branch_materials('A')
                branch_b_mats = _build_branch_materials('B')

                # 选项文本从 seq==1 的字幕里取
                def _get_option_text(br_letter):
                    sub1 = branch_subtitle.get(idx, {}).get(br_letter, {}).get(1)
                    return sub1.get('option_text') if sub1 else None

                # seq==1 分支字幕的 subtitle_id（用于运行期按语言取选项文本）
                def _get_option_key(br_letter):
                    sub1 = branch_subtitle.get(idx, {}).get(br_letter, {}).get(1)
                    return sub1.get('subtitle_id') if sub1 else None

                control_meta = branch_meta.get(idx, {}).get('C', {}).get(1, {})
                pivot_meta = beat_meta_by_index.get(idx, {})
                raw_structured_a = control_meta.get('effects_a')
                raw_structured_b = control_meta.get('effects_b')
                if raw_structured_a is None:
                    raw_structured_a = pivot_meta.get('effects_a')
                if raw_structured_b is None:
                    raw_structured_b = pivot_meta.get('effects_b')
                structured_a = self._normalize_branch_effects(raw_structured_a)
                structured_b = self._normalize_branch_effects(raw_structured_b)
                control_content = branch_c_content.get(idx, '')
                effects = self._parse_branch_effects(control_content)
                legacy_flags = {
                    'a': effects.pop('legacy_flags_a', {}),
                    'b': effects.pop('legacy_flags_b', {}),
                }
                if structured_a is not None:
                    effects['a'] = structured_a
                if structured_b is not None:
                    effects['b'] = structured_b
                missing_effect_sides = []
                if structured_a is None and 'effects_a=' not in control_content:
                    missing_effect_sides.append('effects_a')
                if structured_b is None and 'effects_b=' not in control_content:
                    missing_effect_sides.append('effects_b')
                if missing_effect_sides:
                    print(
                        f"  ⚠️ [分支校验] 索引 {idx} 缺少 {', '.join(missing_effect_sides)}；"
                        "对应选项的数值效果为空，请在主调度 JSON 或当前游戏 C/C1 字幕中补齐。"
                    )

                # 选项界面优先使用 C/C1 自己的场景；否则沿用 pivot 场景。
                scene_id = control_meta.get('scene_id') or pivot_meta.get('scene_id') or 'default'
                scene_shot_size = control_meta.get('scene_shot_size') or pivot_meta.get('scene_shot_size') or ''

                all_materials.append({
                    'type': 'choice_node',
                    'index': idx,
                    'scene_id': scene_id,
                    'scene_shot_size': scene_shot_size,
                    'control_meta': control_meta,
                    'option_a_text': _get_option_text('A') or '选择A',
                    'option_b_text': _get_option_text('B') or '选择B',
                    'option_a_key': _get_option_key('A'),
                    'option_b_key': _get_option_key('B'),
                    'branch_a': branch_a_mats,
                    'branch_b': branch_b_mats,
                    'effects': effects,
                    'legacy_flags': legacy_flags,
                    'is_choice': True
                })
            else:
                # 普通素材
                audio = normal_audio.get(idx)
                subtitle = normal_subtitle.get(idx)
                if audio:
                    all_materials.append(audio)
                if subtitle:
                    all_materials.append(subtitle)

        if not all_materials:
            return {}

        # 按 index 再排一次（choice_node 和普通素材混排）
        all_materials.sort(key=lambda x: x['index'])

        # ── 收集需要复制的素材文件名（供 copy_assets_for_chapter 使用）──
        # 场景图：{scene_id}_{shot}.png（shot 空时退回 {scene_id}.png）
        # 立绘：character_position 里解析出的每个 png stem → {stem}.png
        needed_scene_files = set()
        needed_char_pngs = set()

        def _collect_from_meta(_meta):
            if not _meta:
                return
            _scene_id = _meta.get('scene_id')
            if _scene_id and _scene_id != 'default':
                _stem = self._resolve_scene_file(_scene_id, _meta.get('scene_shot_size'))
                needed_scene_files.add(f"{_stem}.png")
            _shot_suffixes = ('_CS', '_MS', '_LS')
            for _c in self._parse_character_position(_meta.get('character_position')):
                _stem = _c['stem']
                _base = next(((_stem[:-len(s)], s) for s in _shot_suffixes if _stem.endswith(s)), None)
                if _base:
                    for _s in _shot_suffixes:
                        needed_char_pngs.add(f"{_base[0]}{_s}.png")
                else:
                    needed_char_pngs.add(f"{_stem}.png")

        for _idx, _meta in beat_meta_by_index.items():
            _collect_from_meta(_meta)
        # 分支 beat 的 meta 不在 beat_meta_by_index 中，从字幕/音频条目补收
        for _f in self.subtitle_files:
            _collect_from_meta(_f.get('meta'))
        for _f in self.audio_files:
            _collect_from_meta(_f.get('meta'))

        scenes = {
            'all': {
                'chapter': 1, 'scene': 1,
                'materials': all_materials,
                'start_index': all_materials[0]['index'],
                'end_index': all_materials[-1]['index'],
                'beat_meta_by_index': beat_meta_by_index,  # 暴露给 generate_script_for_chapter
                'needed_scene_files': needed_scene_files,
                'needed_char_pngs': needed_char_pngs,
            }
        }

        choice_count = sum(1 for m in all_materials if m.get('is_choice'))
        audio_count  = sum(1 for m in all_materials if m.get('is_audio'))
        sub_count    = sum(1 for m in all_materials if m.get('is_subtitle'))
        print(f"组织完成：共 {len(all_materials)} 个素材（含 {choice_count} 个软分支节点），"
              f"索引范围 {all_materials[0]['index']} - {all_materials[-1]['index']}")
        print(f"  音频: {audio_count}, 字幕: {sub_count}, 软分支: {choice_count}")
        return scenes

    def copy_template(self):
        """复制模板"""
        print(f"复制模板...")

        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)

        shutil.copytree(self.template_dir, self.output_dir)
        print("模板复制完成")

    def copy_assets(self):
        """复制素材"""
        print("复制素材文件...")

        assets_dir = self.output_dir / "assets"
        voices_dir = assets_dir / "voices"
        scenes_dir = assets_dir / "scenes"
        characters_dir = assets_dir / "characters"
        music_dir = assets_dir / "music"
        sfx_dir = assets_dir / "sounds"

        voices_dir.mkdir(exist_ok=True)
        scenes_dir.mkdir(exist_ok=True)
        characters_dir.mkdir(exist_ok=True)
        music_dir.mkdir(exist_ok=True)
        sfx_dir.mkdir(exist_ok=True)

        # 复制音频
        for audio in self.audio_files:
            shutil.copy2(audio['path'], voices_dir / audio['name'])

        # 复制场景图片
        for scene in self.scene_images:
            shutil.copy2(scene['path'], scenes_dir / scene['name'])

        # 复制角色图片
        for char in self.character_images:
            shutil.copy2(char['path'], characters_dir / char['name'])

        # 复制背景音乐
        for music in self.music_files:
            shutil.copy2(music['path'], music_dir / music['name'])

        # 复制SFX
        for sfx in self.sfx_files:
            shutil.copy2(sfx['path'], sfx_dir / sfx['name'])

        print(
            f"复制完成: {len(self.audio_files)}音频, {len(self.scene_images)}场景, {len(self.character_images)}角色, {len(self.music_files)}BGM, {len(self.sfx_files)}SFX")

    def extract_used_assets_from_script(self, script_path):
        """从生成的 script.js 中提取实际使用的场景和角色"""
        import re

        used_scenes = set()
        used_characters = set()

        try:
            with open(script_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 提取 monogatari.assets('scenes', {...}) 中的场景
            scenes_match = re.search(r"monogatari\.assets\('scenes',\s*\{([^}]+)\}", content, re.DOTALL)
            if scenes_match:
                scenes_block = scenes_match.group(1)
                # 匹配 'scene_name': 'scene_file.png'
                for match in re.finditer(r"'([^']+)':\s*'([^']+\.png)'", scenes_block):
                    scene_file = match.group(2)
                    used_scenes.add(scene_file)

            # 提取 monogatari.characters({...}) 中的角色
            chars_match = re.search(r"monogatari\.characters\(\{(.+?)\}\);", content, re.DOTALL)
            if chars_match:
                chars_block = chars_match.group(1)
                # 匹配 sprites: { normal: 'character.png' }
                for match in re.finditer(r"normal:\s*'([^']+\.png)'", chars_block):
                    char_file = match.group(1)
                    used_characters.add(char_file)

            print(f"✅ 从 script.js 提取到: {len(used_scenes)} 个场景, {len(used_characters)} 个角色")
            return used_scenes, used_characters

        except Exception as e:
            print(f"⚠️ 解析 script.js 失败: {e}，将复制所有资源")
            return None, None

    def copy_assets_for_chapter(self, chapter_data, node_id=None):
        """复制本章素材（根据音频文件名推断需要的场景和角色）"""
        print("复制本章素材文件...")

        assets_dir = self.output_dir / "assets"
        voices_dir = assets_dir / "voices"
        scenes_dir = assets_dir / "scenes"
        characters_dir = assets_dir / "characters"
        music_dir = assets_dir / "music"
        sfx_dir = assets_dir / "sounds"
        ambience_dir = assets_dir / "ambience"

        voices_dir.mkdir(exist_ok=True)
        scenes_dir.mkdir(exist_ok=True)
        characters_dir.mkdir(exist_ok=True)
        music_dir.mkdir(exist_ok=True)
        sfx_dir.mkdir(exist_ok=True)
        ambience_dir.mkdir(exist_ok=True)

        # 收集本章用到的音频和字幕文件名
        chapter_materials = chapter_data.get('materials', [])
        chapter_audio_files = set()
        chapter_subtitle_files = set()

        for material in chapter_materials:
            if material.get('is_audio'):
                chapter_audio_files.add(material['file'])
            elif material.get('is_subtitle'):
                chapter_subtitle_files.add(material['file'])
            elif material.get('is_choice'):
                # 收集软分支节点里的音频和字幕文件名
                for br_mats in [material.get('branch_a', []), material.get('branch_b', [])]:
                    for bm in br_mats:
                        if bm.get('audio'):
                            chapter_audio_files.add(bm['audio']['file'])
                        if bm.get('subtitle'):
                            chapter_subtitle_files.add(bm['subtitle']['file'])

        # 获取本章的索引范围
        chap_start = chapter_data.get('start_index', 0)
        chap_end = chapter_data.get('end_index', 999999)

        # 复制本章音频：按语言分目录 assets/voices/{lang}/{file}.wav
        # 上游源目录 audios/{lang}-audio/（旧结构 audios/audio 时只有默认语言）
        audios_root = self.source_dir / "audios"
        legacy_audio = (audios_root / "audio")
        audio_count = 0
        chapter_subtitle_ids = {Path(n).stem for n in chapter_audio_files}
        for lang in self.languages:
            src_dir = legacy_audio if (legacy_audio.exists() and len(self.languages) == 1) else (audios_root / f"{lang}-audio")
            if not src_dir.exists():
                continue
            lang_voices_dir = voices_dir / lang
            lang_voices_dir.mkdir(parents=True, exist_ok=True)
            for sid in chapter_subtitle_ids:
                src = src_dir / f"{sid}.wav"
                if src.exists():
                    shutil.copy2(src, lang_voices_dir / f"{sid}.wav")
                    audio_count += 1

        # 字幕 .txt 仍随 assets 上传一份到 OSS（备份用，运行期实际读内联 window._i18n）
        # 按语言分目录 assets/subtitles/{lang}/{sid}.txt，与音频 voices/{lang} 对齐
        # 上游源目录 audios/{lang}-subtitle/（旧结构 audios/subtitle 时只有默认语言）
        subtitles_dir = assets_dir / "subtitles"
        legacy_subtitle = (audios_root / "subtitle")
        subtitle_count = 0
        chapter_sub_ids = {Path(n).stem for n in chapter_subtitle_files}
        for lang in self.languages:
            src_dir = legacy_subtitle if (legacy_subtitle.exists() and len(self.languages) == 1) else (audios_root / f"{lang}-subtitle")
            if not src_dir.exists():
                continue
            lang_sub_dir = subtitles_dir / lang
            lang_sub_dir.mkdir(parents=True, exist_ok=True)
            for sid in chapter_sub_ids:
                src = src_dir / f"{sid}.txt"
                if src.exists():
                    shutil.copy2(src, lang_sub_dir / f"{sid}.txt")
                    subtitle_count += 1

        # 需要的场景图片：来自 organize_materials 收集的 needed_scene_files
        # （已结合景别拼出 {scene_id}_{shot}.png；旧的「从音频名推断」会漏掉景别后缀）
        needed_scenes = set(chapter_data.get('needed_scene_files', set()))

        print(
            f"从 beat 字段收集到 {len(needed_scenes)} 个唯一场景文件（含景别）")

        # 复制场景图片（只复制本章需要的）
        scene_count = 0
        for scene in self.scene_images:
            if scene['name'] in needed_scenes:
                shutil.copy2(scene['path'], scenes_dir / scene['name'])
                scene_count += 1

        # 需要的角色立绘：来自 character_position 解析出的 png 名
        needed_characters = set(chapter_data.get('needed_char_pngs', set()))
        character_count = 0
        copied_characters = []
        for char in self.character_images:
            if char['name'] in needed_characters:
                shutil.copy2(char['path'], characters_dir / char['name'])
                character_count += 1
                copied_characters.append(char)

        # 复制本章需要的背景音乐
        # 逻辑：只要音乐范围 [music.start, music.end] 与本章范围 [chap_start, chap_end] 有重叠，就复制
        music_count = 0
        for music in self.music_files:
            if max(music['start'], chap_start) <= min(music['end'], chap_end):
                shutil.copy2(music['path'], music_dir / music['name'])
                music_count += 1

        # 复制本章需要的环境底音（规则同 BGM：范围有重叠就复制）
        ambience_count = 0
        copied_ambience = []
        for amb in self.ambience_files:
            if max(amb['start'], chap_start) <= min(amb['end'], chap_end):
                shutil.copy2(amb['path'], ambience_dir / amb['name'])
                ambience_count += 1
                copied_ambience.append(amb)

        # 复制本章需要的SFX（索引在本章范围内的）
        sfx_count = 0
        copied_sfx = []
        for sfx in self.sfx_files:
            if chap_start <= sfx['index'] <= chap_end:
                shutil.copy2(sfx['path'], sfx_dir / sfx['name'])
                sfx_count += 1
                copied_sfx.append(sfx)

        # 复制自定义章节背景图片（替换beijing.png）
        bg_count = 0
        if node_id:
            bg_source = self.images_base_dir / "images" / "backgrounds" / f"{node_id}.png"
            if bg_source.exists():
                images_dir = assets_dir / "images"
                images_dir.mkdir(exist_ok=True)
                shutil.copy2(bg_source, images_dir / "beijing.png")
                bg_count = 1
                print(f"✅ 复制背景图片: {bg_source} -> beijing.png")
            else:
                print(f"⚠️ 背景图片不存在: {bg_source}")

        print(
            f"复制完成: {audio_count}音频, {subtitle_count}字幕, {scene_count}场景(从{len(needed_scenes)}个需要), {character_count}角色(从{len(needed_characters)}个需要), {music_count}BGM, {ambience_count}Ambience, {sfx_count}SFX, {bg_count}自定义背景")

        # 返回实际复制的资源列表（用于生成资源声明）
        copied_scenes = [scene for scene in self.scene_images if scene['name'] in needed_scenes]
        copied_audios = [audio for audio in self.audio_files if audio['name'] in chapter_audio_files]
        copied_music = [music for music in self.music_files if
                        max(music['start'], chap_start) <= min(music['end'], chap_end)]

        return {
            'scenes': copied_scenes,
            'audios': copied_audios,
            'music': copied_music,
            'ambience': copied_ambience,
            'sfx': copied_sfx,
            'characters': copied_characters
        }

    def generate_character_definitions(self):
        """生成角色定义"""
        print("生成角色定义...")

        lines = []
        lines.append("// 角色定义")
        lines.append("monogatari.characters({")

        # 添加角色
        for chinese_name, char_info in self.character_map.items():
            if chinese_name == "旁白":
                continue  # 旁白单独处理

            char_id = char_info['id']
            color = char_info['color']
            image = char_info.get('image')

            # 转义单引号，避免 JavaScript 语法错误
            escaped_char_id = char_id.replace("'", "\\'")
            escaped_chinese_name = chinese_name.replace("'", "\\'")

            lines.append(f"    '{escaped_char_id}': {{")
            lines.append(f"        name: '{escaped_chinese_name}',")
            lines.append(f"        color: '{color}',")

            if image:
                # 转义图片文件名中的单引号
                escaped_image = image.replace("'", "\\'")
                lines.append(f"        sprites: {{")
                lines.append(f"            normal: '{escaped_image}'")
                lines.append(f"        }},")
                lines.append(f"        expressions: {{")
                lines.append(f"            normal: '{escaped_image}'")
                lines.append(f"        }}")

            lines.append(f"    }},")

        # 按 speaker_id 注册角色（用 name_mappings.json 的显示名）
        # 对话行用 `'{speaker_id} {text}'`，因此 monogatari.characters 必须含 speaker_id 键
        for i, (sid, disp) in enumerate(self.name_map.items()):
            if self._is_narrator_speaker(sid):
                continue
            color = self.color_palette[i % len(self.color_palette)]
            self.speaker_color[sid] = color
            escaped_sid = sid.replace("'", "\\'")
            escaped_disp = (disp or sid).replace("'", "\\'")
            lines.append(f"    '{escaped_sid}': {{")
            lines.append(f"        name: '{escaped_disp}',")
            lines.append(f"        color: '{color}'")
            lines.append(f"    }},")

        # 添加旁白角色
        lines.append(f"    'nb': {{")
        lines.append(f"        name: '',")
        lines.append(f"        color: '#FFFFFF'")
        lines.append(f"    }},")

        lines.append("});")
        lines.append("")

        return "\n".join(lines)

    def generate_asset_declarations(self, copied_assets=None):
        """生成资源声明（只声明实际复制的资源）"""
        print("生成资源声明...")

        lines = []

        # 如果没有传入copied_assets，使用全部资源（向后兼容）
        if copied_assets is None:
            scenes_to_declare = self.scene_images
            audios_to_declare = self.audio_files
            music_to_declare = self.music_files
            sfx_to_declare = self.sfx_files
            ambience_to_declare = self.ambience_files
        else:
            scenes_to_declare = copied_assets.get('scenes', [])
            audios_to_declare = copied_assets.get('audios', [])
            music_to_declare = copied_assets.get('music', [])
            sfx_to_declare = copied_assets.get('sfx', [])
            ambience_to_declare = copied_assets.get('ambience', [])

        # 场景资源（参与 Monogatari 预加载，loading 条只加载图片）
        lines.append("// 场景资源")
        lines.append("monogatari.assets('scenes', {")
        for scene in scenes_to_declare:
            escaped_stem = scene['stem'].replace("'", "\\'")
            escaped_name = scene['name'].replace("'", "\\'")
            lines.append(f"    '{escaped_stem}': '{escaped_name}',")
        lines.append("});")
        lines.append("")

        # 音频资源：不在此处声明，改为在游戏 Start 时动态注册，避免 loading 阶段下载全部音频
        # 实际注册逻辑见 generate_script_for_chapter 中的 window._allVoices
        lines.append("// 音频资源(延迟注册,见 window._allVoices)")
        lines.append("")

        # 背景音乐资源（参与预加载，文件数量少）
        lines.append("// 背景音乐资源")
        lines.append("monogatari.assets('music', {")
        for music in music_to_declare:
            escaped_stem = music['stem'].replace("'", "\\'")
            escaped_name = music['name'].replace("'", "\\'")
            lines.append(f"    '{escaped_stem}': '{escaped_name}',")
        lines.append("});")
        lines.append("")

        # SFX 资源
        lines.append("// 音效资源")
        lines.append("monogatari.assets('sounds', {")
        for sfx in sfx_to_declare:
            escaped_stem = sfx['stem'].replace("'", "\\'")
            escaped_name = sfx['name'].replace("'", "\\'")
            lines.append(f"    '{escaped_stem}': '{escaped_name}',")
        lines.append("});")
        lines.append("")

        # 环境底音资源（独立声道，不走 Monogatari 媒体系统，避免被 stop music/sound 误停）
        # 由 AmbiencePlayer 按 beat 范围播放；这里声明清单（file + 覆盖的 beat 范围）
        ambience_manifest = [
            {"stem": amb["stem"], "file": amb["name"],
             "start": amb["start"], "end": amb["end"]}
            for amb in ambience_to_declare
        ]
        lines.append("// 环境底音资源（自建声道，与 BGM/SFX 并存互不干扰）")
        lines.append(f"window._ambienceManifest = {json.dumps(ambience_manifest, ensure_ascii=False)};")
        lines.append("")

        return "\n".join(lines)

    # ==================== 旧版单章模式方法（已废弃，不再使用） ====================
    # 注意：以下方法已被 generate_script_for_chapter() 替代
    # 保留代码仅供参考，实际运行中不会被调用
    # 如需使用请修复第890-902行的变量未定义问题（node_data, folder_mapping, chap_id）

    # def generate_script(self, scenes):
    #     """生成游戏脚本（音频字幕同步）- 按索引顺序，忽略一二级标题"""
    #     print("生成游戏脚本（按索引顺序）...")
    #
    #     lines = []
    #     lines.append("// 游戏脚本 - 按索引顺序播放(忽略一二级标题)")
    #     lines.append("monogatari.script({")
    #
    #     # Start标签
    #     lines.append("    'Start': [")
    #     lines.append("        'show scene #f7f6f6 with fadeIn',")
    #
    #     if scenes:
    #         # 只有一个场景 'all'
    #         lines.append(f"        'jump Sceneall',")
    #     else:
    #         lines.append(f"        'end',")
    #
    #     lines.append("    ],")
    #     lines.append("")
    #
    #     # 只有一个场景：'all'
    #     scene_label = "Sceneall"
    #     lines.append(f"    // 所有素材按索引顺序播放")
    #     lines.append(f"    '{scene_label}': [")
    #
    #     # 初始场景图片（默认背景）
    #     lines.append(f"        'show scene #f7f6f6 with fadeIn',")
    #
    #     # 重置角色状态
    #     # 修正：调用 StageManager.reset()
    #     lines.append(
    #         f"        {{'Function':{{ 'Apply': () => {{ StageManager.reset(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
    #
    #     # 获取所有素材（只有一个场景 'all'）
    #     scene_data = scenes.get('all')
    #     if not scene_data:
    #         lines.append(f"        'end',")
    #         lines.append(f"    ],")
    #         lines.append("")
    #         lines.append("});")
    #         return "\n".join(lines)
    #
    #     materials = scene_data['materials']
    #
    #     # 按索引分组音频和字幕
    #     audio_by_index = {}
    #     subtitle_by_index = {}
    #     for material in materials:
    #         idx = material.get('index', 0)
    #         if material.get('is_audio'):
    #             audio_by_index[idx] = material
    #         elif material.get('is_subtitle'):
    #             subtitle_by_index[idx] = material
    #
    #     # 创建配对并排序（只按索引）
    #     audio_subtitle_pairs = []
    #     for idx in sorted(set(list(audio_by_index.keys()) + list(subtitle_by_index.keys()))):
    #         audio = audio_by_index.get(idx)
    #         subtitle = subtitle_by_index.get(idx)
    #         audio_subtitle_pairs.append({
    #             'audio': audio,
    #             'subtitle': subtitle,
    #             'index': idx,
    #             'scene_id': audio.get('scene_id') if audio else subtitle.get('scene_id') if subtitle else 'default',
    #             'chapter': audio.get('chapter') if audio else subtitle.get('chapter') if subtitle else 1,
    #             'scene': audio.get('scene') if audio else subtitle.get('scene') if subtitle else 1
    #         })
    #
    #     # 获取总步数和节点ID用于进度上报
    #     # 警告：以下变量未定义，会导致 NameError
    #     total_steps = len(audio_subtitle_pairs)
    #     node_id = node_data.get('id', '') if node_data else ''  # ❌ node_data 未定义
    #     if not node_id and folder_mapping:  # ❌ folder_mapping 未定义
    #         node_id = folder_mapping.get(chap_id, '')  # ❌ chap_id 未定义
    #
    #     # 背景音乐排序
    #     sorted_music = sorted(self.music_files, key=lambda x: x['start'])
    #
    #     # 跟踪当前状态
    #     current_scene_image = None  # 当前显示的场景图片ID
    #     current_bgm = None  # 当前播放的背景音乐
    #
    #     # 初始化进度上报系统和错误上报系统
    #     lines.append(f"        {{'Function':{{ 'Apply': () => {{ window.GameProgressReporter.init({total_steps}, '{chap_id}', '{node_id}'); window.GameProgressReporter.startAutoSave(); window.ErrorReporter.init('{chap_id}', '{node_id}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
    #
    #     # 按索引顺序生成脚本
    #     for pair in audio_subtitle_pairs:
    #         audio = pair['audio']
    #         subtitle = pair['subtitle']
    #         chapter = pair['chapter']
    #         scene = pair['scene']
    #         idx = pair['index']
    #
    #         # 获取当前素材的真实场景ID
    #         current_scene_id = audio.get('scene_id') if audio else subtitle.get('scene_id') if subtitle else 'default'
    #
    #         # 0. 背景音乐处理
    #         # 检查是否需要停止当前背景音乐
    #         if current_bgm is not None and idx > current_bgm['end']:
    #             bgm_stem = current_bgm['stem']
    #             lines.append(f"        'stop music {bgm_stem}',")
    #             current_bgm = None
    #
    #         # 检查是否需要开始新的背景音乐
    #         if current_bgm is None:
    #             for music in sorted_music:
    #                 if music['start'] <= idx <= music['end']:
    #                     music_stem = music['stem']
    #                     lines.append(f"        'play music {music_stem}',")
    #                     current_bgm = music
    #                     break
    #
    #         # 1. 场景图片处理
    #         # 优先使用素材自带的场景ID来匹配场景图片
    #         scene_image_id = current_scene_id
    #         has_scene_image = any(img['stem'] == scene_image_id for img in self.scene_images)
    #
    #         if has_scene_image and scene_image_id != current_scene_image:
    #             lines.append(f"        'show scene {scene_image_id} with fadeIn',")
    #             lines.append(f"        {{'Function':{{ 'Apply': () => {{ startBackgroundMotion(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
    #             lines.append(f"        'wait 800',")
    #             current_scene_image = scene_image_id
    #         elif not has_scene_image and current_scene_image is None:
    #             lines.append(f"        'show scene #f7f6f6 with fadeIn',")
    #             lines.append(f"        'wait 500',")
    #             current_scene_image = "#f7f6f6"
    #
    #         # 2. 对话处理
    #         if subtitle and subtitle.get('dialogues'):
    #             for dialogue in subtitle['dialogues']:
    #                 speaker = dialogue['speaker']
    #                 text = dialogue['text']
    #
    #                 # 清理文本
    #                 text_clean = text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"").replace("`", "\\`").replace("\n", " ").replace("\r", "")
    #
    #                 # 【核心修改点】: 无论是谁说话，都先调用 updateStage
    #                 lines.append(f"        {{'Function':{{ 'Apply': () => {{ updateStage('{speaker}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
    #
    #                 # 播放音频
    #                 if audio:
    #                     audio_id = audio['id']
    #                     lines.append(f"        'play voice {audio_id}',")
    #
    #                 # 获取显示的ID
    #                 char_id_for_text = self.chinese_to_id.get(speaker, 'nb')
    #                 if speaker == '旁白':
    #                     char_id_for_text = 'nb'
    #
    #                 # 等待时间
    #                 wait_time = 1500 if audio else 1000
    #                 lines.append(f"        'wait {wait_time}',")
    #
    #                 # 显示文本行
    #                 if char_id_for_text == 'nb':
    #                     lines.append(f"        '{text_clean}',")
    #                 else:
    #                     lines.append(f"        '{char_id_for_text} {text_clean}',")
    #
    #         else:
    #             # 纯音频
    #             if audio:
    #                 lines.append(f"        'play voice {audio['id']}',")
    #                 lines.append(f"        'wait 1000',")
    #
    #     # 场景结束
    #     lines.append(f"        '游戏结束',")
    #     lines.append(f"        'end',")
    #
    #     lines.append(f"    ],")
    #     lines.append("")
    #
    #     lines.append("});")
    #
    #     return "\n".join(lines)

    # 修改函数签名，接收 node_data 和 folder_mapping
    def generate_script_for_chapter(self, chapter_data, node_data=None, folder_mapping=None, game_project=None,
                                    hook_id=None, player_variables=None, branch_thresholds=None, is_root_node=False):
        """生成本章游戏脚本（大道至简版：每一步都显式指定背景，逻辑与人物一致）"""
        chap_id = str(node_data.get('depth', 1)) if node_data else '1'
        print(f"生成第 {chap_id} 章游戏脚本...")

        lines = []

        # 1. 数据准备
        materials = chapter_data.get('materials', [])
        beat_meta_by_index = chapter_data.get('beat_meta_by_index', {}) or {}
        audio_by_index = {}
        subtitle_by_index = {}
        choice_by_index = {}   # choice_node 单独存
        for material in materials:
            idx = material.get('index', 0)
            if material.get('is_choice'):
                choice_by_index[idx] = material
            elif material.get('is_audio'):
                audio_by_index[idx] = material
            elif material.get('is_subtitle'):
                subtitle_by_index[idx] = material

        # 计算每个普通 beat 应显示的累计 system_text（系统面板内容）
        # 软分支 beat 沿用 pivot 索引上次的 system_text
        system_text_by_index = {}
        running_system_text = ""
        all_normal_indices = sorted(set(list(audio_by_index.keys()) + list(subtitle_by_index.keys()) + list(beat_meta_by_index.keys())))
        for idx in all_normal_indices:
            meta = beat_meta_by_index.get(idx, {})
            st = meta.get('system_text')
            if st is not None and str(st).strip() != "":
                running_system_text = str(st)
            system_text_by_index[idx] = running_system_text

        # SFX 按 index 建立映射（一个 index 可能有多个 SFX，用列表）
        sfx_by_index = {}
        sfx_by_id = {}
        for sfx in self.sfx_files:
            sfx_idx = sfx['index']
            sfx_by_index.setdefault(sfx_idx, []).append(sfx)
            if sfx.get('declared_id'):
                sfx_by_id[sfx['declared_id']] = sfx

        # ==================== 核心修正：预处理场景状态 ====================
        audio_subtitle_pairs = []
        current_active_scene = "default"
        valid_scene_stems = {s['stem'] for s in self.scene_images}

        all_indices = sorted(set(
            list(audio_by_index.keys()) +
            list(subtitle_by_index.keys()) +
            list(choice_by_index.keys())
        ))

        def _resolve_active_scene(raw_scene_id, shot):
            nonlocal current_active_scene
            if raw_scene_id and raw_scene_id != 'default':
                _resolved = self._resolve_scene_file(raw_scene_id, shot, valid_scene_stems)
                if _resolved in valid_scene_stems:
                    current_active_scene = _resolved
            return current_active_scene

        for idx in all_indices:
            if idx in choice_by_index:
                # pivot beat：与分支共用索引的普通 beat，先作为独立一步播完再进选项
                pivot_audio = audio_by_index.get(idx)
                pivot_subtitle = subtitle_by_index.get(idx)
                if pivot_audio or pivot_subtitle:
                    _p_scene = (pivot_audio or pivot_subtitle).get('scene_id', 'default')
                    _p_shot = beat_meta_by_index.get(idx, {}).get('scene_shot_size', '')
                    audio_subtitle_pairs.append({
                        'audio': pivot_audio,
                        'subtitle': pivot_subtitle,
                        'index': idx,
                        'label': f"Material_{idx}",
                        'scene_to_show': _resolve_active_scene(_p_scene, _p_shot),
                        'chapter': 1, 'scene': 1,
                    })

                cn = choice_by_index[idx]
                raw_scene_id = cn.get('scene_id', 'default')
                _cn_shot = cn.get('scene_shot_size', '')
                # pivot 已占用 Material_{idx}，选项界面用独立 label 避免冲突
                _choice_label = f"Choice_{idx}" if (pivot_audio or pivot_subtitle) else f"Material_{idx}"
                _choice_scene = _resolve_active_scene(raw_scene_id, _cn_shot)
                # A/B 各自维护场景状态，不能把 A1 的背景复用于整条分支。
                for _branch_key in ('branch_a', 'branch_b'):
                    _branch_scene = _choice_scene
                    for _bm in cn.get(_branch_key) or []:
                        _bm_meta = _bm.get('meta') or {}
                        _raw = _bm_meta.get('scene_id')
                        if _raw and _raw != 'default':
                            _resolved = self._resolve_scene_file(
                                _raw, _bm_meta.get('scene_shot_size', ''), valid_scene_stems
                            )
                            if _resolved in valid_scene_stems:
                                _branch_scene = _resolved
                        _bm['scene_to_show'] = _branch_scene

                audio_subtitle_pairs.append({
                    'audio': None,
                    'subtitle': None,
                    'index': idx,
                    'label': _choice_label,
                    'scene_to_show': _choice_scene,
                    'chapter': 1, 'scene': 1,
                    'choice_node': cn   # 标记为软分支节点
                })
                continue

            audio = audio_by_index.get(idx)
            subtitle = subtitle_by_index.get(idx)

            # 获取这一步原始数据里定义的场景ID
            raw_scene_id = audio.get('scene_id') if audio else subtitle.get('scene_id') if subtitle else 'default'

            # 结合景别拼出真实场景 stem（找不到 _shot 文件时回退原名）
            _shot = beat_meta_by_index.get(idx, {}).get('scene_shot_size', '')

            audio_subtitle_pairs.append({
                'audio': audio,
                'subtitle': subtitle,
                'index': idx,
                'label': f"Material_{idx}",
                'scene_to_show': _resolve_active_scene(raw_scene_id, _shot),  # 【关键】这一步必须显示的图
                'chapter': audio.get('chapter') if audio else subtitle.get('chapter') if subtitle else 1,
                'scene': audio.get('scene') if audio else subtitle.get('scene') if subtitle else 1
            })
        # ================================================================

        total_steps = audio_subtitle_pairs[-1]['index'] if audio_subtitle_pairs else 0
        min_steps = audio_subtitle_pairs[0]['index'] if audio_subtitle_pairs else 0

        node_id = node_data.get('id', '') if node_data else ''
        if not node_id and folder_mapping:
            node_id = folder_mapping.get(chap_id, '')

        # ==================== 渐进式预加载器 ====================
        scene_name_map = {s['stem']: s['name'] for s in self.scene_images}
        manifest_entries = []
        for pair in audio_subtitle_pairs:
            audio_name = pair['audio']['file'] if pair['audio'] else None
            scene_stem = pair['scene_to_show']
            scene_file = scene_name_map.get(scene_stem, scene_stem + '.png')
            entry = f"{{i:{pair['index']},k:'M_{pair['index']}_{len(manifest_entries)}'"
            if audio_name:
                escaped_audio = audio_name.replace("'", "\\'")
                entry += f",a:'{escaped_audio}'"
            escaped_scene = scene_file.replace("'", "\\'")
            entry += f",s:'{escaped_scene}'}}"
            manifest_entries.append(entry)
            if pair.get('choice_node'):
                for _br in ('A', 'B'):
                    for _bm in pair['choice_node'].get(f'branch_{_br.lower()}', []) or []:
                        _ba = _bm.get('audio')
                        _bs = _bm.get('scene_to_show') or pair['scene_to_show']
                        _bf = scene_name_map.get(_bs, _bs + '.png')
                        _key = f"B_{pair['index']}_{_br}_{_bm.get('seq', 0)}"
                        _entry = f"{{i:{pair['index']},k:'{_key}'"
                        if _ba:
                            _entry += ",a:'" + _ba['file'].replace("'", "\\'") + "'"
                        _entry += ",s:'" + _bf.replace("'", "\\'") + "'}"
                        manifest_entries.append(_entry)

        lines.append(f"// 游戏脚本 - 第 {chap_id} 章")
        lines.append("")
        lines.append("// ==================== 渐进式资源预加载器 ====================")
        lines.append(f"window._assetManifest = [{','.join(manifest_entries)}];")
        lines.append(f"window._materialIndices = {json.dumps(sorted(all_indices))};")
        lines.append("")
        lines.append("window.ProgressiveLoader = {")
        lines.append("    WINDOW: 10,")
        lines.append("    loaded: new Set(),")
        lines.append("    load: function(fromIdx, count) {")
        lines.append("        var end = fromIdx + count;")
        lines.append("        var self = this;")
        lines.append("        window._assetManifest.forEach(function(item) {")
        lines.append("            var assetKey = item.k || item.i;")
        lines.append("            if (item.i >= fromIdx && item.i < end && !self.loaded.has(assetKey)) {")
        lines.append("                self.loaded.add(assetKey);")
        lines.append(
            "                if (item.a) { var au = new Audio(); au.preload = 'auto'; au.src = 'assets/voices/' + (window._lang||'') + '/' + item.a; }")
        lines.append("                if (item.s) { var im = new Image(); im.src = 'assets/scenes/' + item.s; }")
        lines.append("            }")
        lines.append("        });")
        lines.append("    },")
        lines.append("    step: function(idx) { this.load(idx + 1, this.WINDOW); }")
        lines.append("};")
        lines.append("")

        # ── 收集本章用到的 subtitle_id（= i18n key / 音频 stem）──
        chapter_sids = []
        _seen_sid = set()
        def _add_sid(sid):
            if sid and sid not in _seen_sid:
                _seen_sid.add(sid)
                chapter_sids.append(sid)
        for pair in audio_subtitle_pairs:
            if pair.get('choice_node'):
                cn = pair['choice_node']
                # 选项文本 key（{sid}__opt）
                _add_sid(f"{cn.get('option_a_key')}__opt" if cn.get('option_a_key') else None)
                _add_sid(f"{cn.get('option_b_key')}__opt" if cn.get('option_b_key') else None)
                for br_mats in [cn.get('branch_a', []), cn.get('branch_b', [])]:
                    for bm in br_mats:
                        a = bm.get('audio')
                        s = bm.get('subtitle')
                        if a:
                            _add_sid(a['id'])
                        if s:
                            _add_sid(s.get('subtitle_id') or s.get('stem') or s.get('meta', {}).get('beat_id_raw'))
            else:
                if pair.get('audio'):
                    _add_sid(pair['audio']['id'])
                if pair.get('subtitle'):
                    _add_sid(pair['subtitle'].get('stem'))

        # ── 多语言数据：按语言分组的音频清单 / 字幕文本 / 角色显示名 ──
        # 字幕文本：直接用本节点扫描到的全部 i18n_text（保证每个占位 key 都有译文，文本体积小）。
        # 音频清单：按本章实际用到的 key 过滤（音频大，避免多注册）。
        voices_by_lang_out = {}
        i18n_out = {}
        for lang in self.languages:
            vmap = {}
            for sid in chapter_sids:
                if sid in self.voices_by_lang.get(lang, {}):
                    vmap[sid] = self.voices_by_lang[lang][sid]
            voices_by_lang_out[lang] = vmap
            i18n_out[lang] = dict(self.i18n_text.get(lang, {}))

        # 在线版运行期只暴露规范 locale；旧素材目录中即使存在
        # zh-CN/en/ja 等语言目录，也不能让它们进入前端语言解析。
        runtime_languages = [
            lang for lang in ONLINE_GAME_LANGUAGES if lang in self.languages
        ] or [DEFAULT_AUDIO_LANGUAGE]
        all_langs_js = json.dumps(runtime_languages, ensure_ascii=False)
        default_lang_js = json.dumps(self.default_language, ensure_ascii=False)
        voices_js = json.dumps(voices_by_lang_out, ensure_ascii=False)
        i18n_js = json.dumps(i18n_out, ensure_ascii=False)
        names_js = json.dumps(self.names_by_lang, ensure_ascii=False)

        lines.append("// ==================== 多语言数据（运行期按 ?lang= 取）====================")
        lines.append(f"window._allLangs = {all_langs_js};")
        lines.append(f"window._defaultLang = {default_lang_js};")
        lines.append("window._resolveGameLang = function(raw){")
        # URL locale 必须精确匹配；不裁剪空白，避免 `` en-US `` 被误认成合法值。
        lines.append("    var value = String(raw || '');")
        lines.append("    if (value !== 'en-US' && value !== 'ja-JP') return 'en-US';")
        lines.append("    return value;")
        lines.append("};")
        lines.append("window._lang = (function(){")
        lines.append("    try {")
        lines.append("        var params = new URLSearchParams(location.search);")
        lines.append("        var q = params.get('lang');")
        lines.append("        if (q) return window._resolveGameLang(q);")
        lines.append("    } catch(e){}")
        lines.append("    return window._resolveGameLang(window._defaultLang);")
        lines.append("})();")
        lines.append(f"window._voicesByLang = {voices_js};")
        lines.append(f"window._i18n = {i18n_js};")
        lines.append(f"window._namesByLang = {names_js};")
        lines.append("// 当前语言的音频清单（Start 时注册到 Monogatari）")
        lines.append("window._allVoices = window._voicesByLang[window._lang] || window._voicesByLang[window._defaultLang] || {};")
        lines.append("// 运行期按当前语言取字幕文本（NVL/CG 等 Function 内用）")
        lines.append("window.__i18nText = function(key){")
        lines.append("    var d = window._i18n[window._lang] || {}; var fb = window._i18n[window._defaultLang] || {};")
        lines.append("    return (d[key] != null) ? d[key] : (fb[key] || '');")
        lines.append("};")
        lines.append("// 自定义 UI 文案（引擎内置按钮之外的按钮，如 Restart/Continue/system）")
        lines.append("window._uiText = {")
        lines.append("    'ja-JP': { restart:'最初から', cont:'続ける', quit:'終了', system:'システム', clickContinue:'クリックして続ける', quickMenu:'クイックメニュー', show:'表示', hide:'非表示', restoreDefaults:'初期設定に戻す' },")
        lines.append("    'en-US': { restart:'Restart', cont:'Continue', quit:'Quit', system:'System', clickContinue:'click to continue', quickMenu:'Quick Menu', show:'Show', hide:'Hide', restoreDefaults:'Restore Defaults' }")
        lines.append("};")
        lines.append("window.__ui = function(key){")
        lines.append("    var m = window._uiText[window._lang] || window._uiText[window._defaultLang] || window._uiText['en-US'] || {};")
        lines.append("    return m[key] != null ? m[key] : key;")
        lines.append("};")
        lines.append("window.__setUiButtonLabel = function(button, text){")
        lines.append("    if (!button) return;")
        lines.append("    var label = button.querySelector('[data-string], [data-content=\"label\"], .button__label, .label');")
        lines.append("    if (label) {")
        lines.append("        label.removeAttribute('data-string');")
        lines.append("        label.textContent = text;")
        lines.append("    } else {")
        lines.append("        var walker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT);")
        lines.append("        var node = null, candidate = null;")
        lines.append("        while ((node = walker.nextNode())) { if (String(node.nodeValue || '').trim()) candidate = node; }")
        lines.append("        if (candidate) candidate.nodeValue = text;")
        lines.append("        else { label = document.createElement('span'); label.className = 'custom-ui-label'; label.textContent = text; button.appendChild(label); }")
        lines.append("    }")
        lines.append("    button.removeAttribute('data-string');")
        lines.append("    button.setAttribute('aria-label', text);")
        lines.append("};")
        lines.append("window.refreshGameUiLanguage = function(){")
        lines.append("    document.documentElement.lang = window._lang || window._defaultLang || 'en-US';")
        lines.append("    var engineLanguage = {'zh-CN':'简体中文','ja-JP':'日本語','en-US':'English'}[window._lang] || 'English';")
        lines.append("    try {")
        lines.append("        monogatari.preference('Language', engineLanguage);")
        lines.append("        if (typeof monogatari.localize === 'function') monogatari.localize();")
        lines.append("    } catch (e) { console.warn('[i18n] refresh engine UI err', e); }")
        lines.append("    window.__setUiButtonLabel(document.querySelector('.restart-button'), window.__ui('restart'));")
        lines.append("    window.__setUiButtonLabel(document.querySelector('.continue-button'), window.__ui('cont'));")
        lines.append("    window.__setUiButtonLabel(document.querySelector('.main-quit-button'), window.__ui('quit'));")
        lines.append("    var system = document.getElementById('system-toggle');")
        lines.append("    if (system) { system.textContent = window.__ui('system'); system.title = window.__ui('system'); }")
        lines.append("    if (window.AncientUI && window.AncientUI.refreshSettingsText) window.AncientUI.refreshSettingsText();")
        lines.append("};")
        lines.append("")
        # ── i18n 脚本预处理器：init 前把 _script 里的占位 ␟key␟ 换成当前语言文本 ──
        lines.append("window.I18nScript = {")
        lines.append("    MARK: '\\u241f',")
        lines.append("    apply: function() {")
        lines.append("        try {")
        lines.append("            var re = /\\u241f(.+?)\\u241f/;")
        lines.append("            var dict = window._i18n[window._lang] || window._i18n[window._defaultLang] || {};")
        lines.append("            var fb = window._i18n[window._defaultLang] || {};")
        lines.append("            var script = (window.monogatari && monogatari._script) ? monogatari._script : null;")
        lines.append("            if (!script) return;")
        lines.append("            var resolve = function(s){")
        lines.append("                if (typeof s !== 'string') return s;")
        lines.append("                return s.replace(/\\u241f(.+?)\\u241f/g, function(_, k){ return (dict[k]!=null)?dict[k]:(fb[k]||''); });")
        lines.append("            };")
        lines.append("            Object.keys(script).forEach(function(label){")
        lines.append("                var arr = script[label];")
        lines.append("                if (!Array.isArray(arr)) return;")
        lines.append("                for (var i=0;i<arr.length;i++){")
        lines.append("                    var row = arr[i];")
        lines.append("                    if (typeof row === 'string'){")
        lines.append("                        if (row.indexOf('\\u241f') >= 0) arr[i] = resolve(row);")
        lines.append("                    } else if (row && typeof row === 'object' && row.Choice){")
        lines.append("                        Object.keys(row.Choice).forEach(function(ck){")
        lines.append("                            var opt = row.Choice[ck];")
        lines.append("                            if (opt && typeof opt.Text === 'string') opt.Text = resolve(opt.Text);")
        lines.append("                        });")
        lines.append("                    }")
        lines.append("                }")
        lines.append("            });")
        lines.append("        } catch(e){ console.warn('[I18nScript] apply error', e); }")
        lines.append("    }")
        lines.append("};")
        lines.append("")
        lines.append("// ==================== 稳定自动播放计时器 ====================")
        lines.append("// 替代引擎未保存 timeout ID 的原生循环，保证语音结束后重置计时且不会产生幽灵定时器。")
        lines.append("window.SmartAutoPlay = {")
        lines.append("    _enabled: false,")
        lines.append("    _timer: null,")
        lines.append("    _advancing: false,")
        lines.append("    _paused: false,")
        lines.append("    _waitingAudio: null,")
        lines.append("    _audioWatchdog: null,")
        lines.append("    _lifecycleBound: false,")
        lines.append("    _installed: false,")
        lines.append("    readingDelay: function(text){")
        lines.append("        text = String(text || '').replace(/<[^>]*>/g, ' ').replace(/\\s+/g, ' ').trim();")
        lines.append("        if (!text) return 5000;")
        lines.append("        var cjk = (text.match(/[\\u3400-\\u4DBF\\u4E00-\\u9FFF\\u3040-\\u30FF\\uAC00-\\uD7AF]/g) || []).length;")
        lines.append("        var words = (text.match(/[A-Za-z0-9]+(?:['’\\-][A-Za-z0-9]+)*/g) || []).length;")
        lines.append("        var seconds = 1.2 + (cjk / 6) + (words / 2.8);")
        lines.append("        return Math.max(2500, Math.min(15000, seconds * 1000));")
        lines.append("    },")
        lines.append("    delay: function(){")
        lines.append("        var rate = window.VoicePlaybackRate ? Number(window.VoicePlaybackRate._rate) : 1;")
        lines.append("        if (!isFinite(rate) || rate < 0.5 || rate > 2) rate = 1;")
        lines.append("        var text = window.AutoVoiceAdvance ? window.AutoVoiceAdvance.readingText() : '';")
        lines.append("        return Math.max(100, this.readingDelay(text) / rate);")
        lines.append("    },")
        lines.append("    isEnabled: function(){ return this._enabled; },")
        lines.append("    currentVoiceAudio: function(){")
        lines.append("        var voiceId = window.AutoVoiceAdvance ? window.AutoVoiceAdvance._voiceId : ''; if (!voiceId) return null;")
        lines.append("        var audio = null; try { audio = monogatari.mediaPlayer('voice', voiceId); } catch(e) {}")
        lines.append("        if (!audio || audio.ended || audio.error || audio.paused) return null;")
        lines.append("        return audio;")
        lines.append("    },")
        lines.append("    clearTimer: function(){")
        lines.append("        if (this._timer != null) clearTimeout(this._timer);")
        lines.append("        this._timer = null;")
        lines.append("        try { monogatari.global('_auto_play_timer', this._enabled ? -1 : null); } catch(e) {}")
        lines.append("    },")
        lines.append("    clearAudioWatchdog: function(){")
        lines.append("        if (this._audioWatchdog != null) clearTimeout(this._audioWatchdog);")
        lines.append("        this._audioWatchdog = null;")
        lines.append("    },")
        lines.append("    updateUI: function(){")
        lines.append("        var button = document.querySelector('quick-menu [data-action=\"auto-play\"]'); if (!button) return;")
        lines.append("        var label = button.querySelector('[data-string]');")
        lines.append("        if (label) { try { label.textContent = monogatari.string(this._enabled ? 'Stop' : 'AutoPlay'); } catch(e) {} }")
        lines.append("        var icon = button.querySelector('svg, [data-icon], .fa-play-circle, .fa-stop-circle');")
        lines.append("        if (icon) { var span = document.createElement('span'); span.className = this._enabled ? 'fas fa-stop-circle' : 'fas fa-play-circle'; icon.replaceWith(span); }")
        lines.append("    },")
        lines.append("    finishAdvance: function(){")
        lines.append("        this._advancing = false;")
        lines.append("        if (this._enabled) this.schedule();")
        lines.append("    },")
        lines.append("    proceed: function(){")
        lines.append("        if (!this._enabled || this._paused || this._waitingAudio || this._advancing) return;")
        lines.append("        this.clearTimer(); this._advancing = true;")
        lines.append("        var self = this;")
        lines.append("        Promise.resolve(monogatari.proceed({userInitiated:false, skip:false, autoPlay:true}))")
        lines.append("            .then(function(){ self.finishAdvance(); })")
        lines.append("            .catch(function(){ self.finishAdvance(); });")
        lines.append("    },")
        lines.append("    schedule: function(){")
        lines.append("        if (!this._enabled || this._paused || this._waitingAudio) return;")
        lines.append("        if (this._timer != null) clearTimeout(this._timer);")
        lines.append("        var self = this;")
        lines.append("        this._timer = setTimeout(function(){ self._timer = null; self.proceed(); }, this.delay());")
        lines.append("        try { monogatari.global('_auto_play_timer', this._timer); } catch(e) {}")
        lines.append("    },")
        lines.append("    set: function(enabled){")
        lines.append("        this._enabled = !!enabled; this._advancing = false; this._paused = false; this._waitingAudio = null; this.clearTimer(); this.clearAudioWatchdog();")
        lines.append("        if (this._enabled) {")
        lines.append("            var activeVoice = this.currentVoiceAudio();")
        lines.append("            if (activeVoice) this.waitForAudio(activeVoice); else this.schedule();")
        lines.append("        }")
        lines.append("        this.updateUI();")
        lines.append("    },")
        lines.append("    pauseForScreen: function(){")
        lines.append("        if (!this._enabled) return;")
        lines.append("        this._paused = true; this.clearTimer();")
        lines.append("    },")
        lines.append("    resumeFromScreen: function(){")
        lines.append("        if (!this._enabled || !this._paused) return;")
        lines.append("        this._paused = false; this.schedule();")
        lines.append("    },")
        lines.append("    advanceFromVoice: function(){")
        lines.append("        if (!this._enabled) return;")
        lines.append("        this.proceed();")
        lines.append("    },")
        lines.append("    waitForAudio: function(audio){")
        lines.append("        if (!this._enabled || !audio) return;")
        lines.append("        if (audio.error) { this.schedule(); return; }")
        lines.append("        if (audio.ended) { this.proceed(); return; }")
        lines.append("        if (this._waitingAudio === audio) return;")
        lines.append("        this.clearAudioWatchdog();")
        lines.append("        this._waitingAudio = audio; this.clearTimer();")
        lines.append("        var self = this; var finish = function(advance){")
        lines.append("            if (self._waitingAudio !== audio) return;")
        lines.append("            self._waitingAudio = null; self.clearAudioWatchdog();")
        lines.append("            if (!self._enabled || self._paused) return;")
        lines.append("            if (advance) self.proceed(); else self.schedule();")
        lines.append("        };")
        lines.append("        audio.addEventListener('ended', function(){ finish(true); }, {once:true});")
        lines.append("        audio.addEventListener('error', function(){ finish(false); }, {once:true});")
        lines.append("        audio.addEventListener('abort', function(){ finish(false); }, {once:true});")
        lines.append("        audio.addEventListener('emptied', function(){ finish(false); }, {once:true});")
        lines.append("        var idleChecks = 0; var check = function(){")
        lines.append("            if (self._waitingAudio !== audio || !self._enabled) return;")
        lines.append("            if (audio.ended) { finish(true); return; }")
        lines.append("            if (audio.error) { finish(false); return; }")
        lines.append("            if (audio.paused) {")
        lines.append("                idleChecks += 1;")
        lines.append("                if (Number(audio.currentTime || 0) > 0 || idleChecks >= 6) { finish(false); return; }")
        lines.append("            } else { idleChecks = 0; }")
        lines.append("            self._audioWatchdog = setTimeout(check, 500);")
        lines.append("        };")
        lines.append("        self._audioWatchdog = setTimeout(check, 500);")
        lines.append("    },")
        lines.append("    cancelWaitingAudio: function(audio){")
        lines.append("        if (audio && this._waitingAudio !== audio) return;")
        lines.append("        if (!this._waitingAudio) return;")
        lines.append("        this._waitingAudio = null; this.clearAudioWatchdog(); if (this._enabled && !this._paused) this.schedule();")
        lines.append("    },")
        lines.append("    voiceFailed: function(token, voiceId){")
        lines.append("        if (!this._enabled || (window.AutoVoiceAdvance && window.AutoVoiceAdvance._token !== token)) return;")
        lines.append("        try { monogatari.removeMediaPlayer('voice', voiceId); } catch(e) {}")
        lines.append("        this.schedule();")
        lines.append("    },")
        lines.append("    bindLifecycle: function(){")
        lines.append("        if (this._lifecycleBound) return;")
        lines.append("        this._lifecycleBound = true; var self = this;")
        lines.append("        document.addEventListener('click', function(event){")
        lines.append("            var target = event.target && event.target.closest ? event.target : null; if (!target) return;")
        lines.append("            var opener = target.closest('[data-action=\"open-screen\"], [data-action=\"dialog-log\"], [data-action=\"end\"]');")
        lines.append("            if (opener) self.pauseForScreen();")
        lines.append("            var back = target.closest('[data-action=\"back\"]');")
        lines.append("            if (back && back.closest('[data-screen]:not([data-screen=\"game\"])')) setTimeout(function(){ self.resumeFromScreen(); }, 0);")
        lines.append("            if (target.closest('[data-action=\"dismiss-alert\"]')) setTimeout(function(){ self.resumeFromScreen(); }, 0);")
        lines.append("            if (target.closest('[data-action=\"quit\"]')) self.set(false);")
        lines.append("        }, true);")
        lines.append("        document.addEventListener('keydown', function(event){")
        lines.append("            var tag = event.target && event.target.tagName ? event.target.tagName.toLowerCase() : '';")
        lines.append("            if (tag === 'input' || tag === 'textarea' || tag === 'select' || (event.target && event.target.isContentEditable)) return;")
        lines.append("            var key = String(event.key || '').toLowerCase();")
        lines.append("            if (key === 'escape' || (event.shiftKey && (key === 's' || key === 'l' || key === 'q'))) self.pauseForScreen();")
        lines.append("        }, true);")
        lines.append("    },")
        lines.append("    install: function(){")
        lines.append("        if (this._installed || !window.monogatari) return;")
        lines.append("        var self = this;")
        lines.append("        monogatari.autoPlay = function(enabled){ self.set(!!enabled); };")
        lines.append("        try { monogatari.global('_auto_play_timer', null); } catch(e) {}")
        lines.append("        this.bindLifecycle();")
        lines.append("        this._installed = true;")
        lines.append("    }")
        lines.append("};")
        lines.append("")
        lines.append("// ==================== 自动播放速度（同时控制语音倍速） ====================")
        lines.append("window.VoicePlaybackRate = {")
        lines.append("    _rate: 1,")
        lines.append("    _storageKey: 'VisualNovel_AutoPlayVoiceSpeed',")
        lines.append("    load: function(){")
        lines.append("        var value = NaN;")
        lines.append("        try { value = Number(localStorage.getItem(this._storageKey)); } catch(e) {}")
        lines.append("        if (!isFinite(value) || value < 0.5 || value > 2) { try { value = Number(monogatari.preference('AutoPlaySpeed')); } catch(e) {} }")
        lines.append("        if (!isFinite(value) || value < 0.5 || value > 2) { try { value = Number(localStorage.getItem('VisualNovel_VoicePlaybackRate')); } catch(e) {} }")
        lines.append("        if (!isFinite(value) || value < 0.5 || value > 2) value = 1;")
        lines.append("        return Math.max(0.5, Math.min(2, Math.round(value * 10) / 10));")
        lines.append("    },")
        lines.append("    apply: function(audio){")
        lines.append("        if (!audio) return;")
        lines.append("        try { audio.defaultPlaybackRate = this._rate; audio.playbackRate = this._rate; audio.preservesPitch = true; audio.webkitPreservesPitch = true; } catch(e) {}")
        lines.append("    },")
        lines.append("    applyActive: function(){")
        lines.append("        var players = [];")
        lines.append("        try { players = monogatari.mediaPlayers('voice') || []; } catch(e) {}")
        lines.append("        for (var i = 0; i < players.length; i++) this.apply(players[i]);")
        lines.append("        if (window.BackVoicePlayer && window.BackVoicePlayer._audio) this.apply(window.BackVoicePlayer._audio);")
        lines.append("    },")
        lines.append("    set: function(value){")
        lines.append("        value = Number(value); if (!isFinite(value)) value = 1;")
        lines.append("        this._rate = Math.max(0.5, Math.min(2, Math.round(value * 10) / 10));")
        lines.append("        try { localStorage.setItem(this._storageKey, String(this._rate)); } catch(e) {}")
        lines.append("        try { localStorage.removeItem('VisualNovel_VoicePlaybackRate'); } catch(e) {}")
        lines.append("        try { monogatari.preference('AutoPlaySpeed', this._rate); } catch(e) {}")
        lines.append("        try { monogatari.preference('VoicePlaybackRate', this._rate); } catch(e) {}")
        lines.append("        if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) window.SmartAutoPlay.schedule();")
        lines.append("        this.applyActive(); this.updateUI();")
        lines.append("    },")
        lines.append("    patchEngine: function(){")
        lines.append("        if (!window.monogatari || monogatari._voicePlaybackRatePatched) return;")
        lines.append("        var self = this; var original = monogatari.mediaPlayer.bind(monogatari);")
        lines.append("        monogatari.mediaPlayer = function(type, key, player){")
        lines.append("            var result = original(type, key, player);")
        lines.append("            if (type === 'voice') self.apply(player || result);")
        lines.append("            return result;")
        lines.append("        };")
        lines.append("        monogatari._voicePlaybackRatePatched = true;")
        lines.append("    },")
        lines.append("    mountUI: function(){")
        lines.append("        var root = document.querySelector('settings-screen [data-settings=\"auto-play-speed\"]');")
        lines.append("        if (!root) return;")
        lines.append("        document.querySelectorAll('settings-screen [data-settings=\"voice-playback-rate\"]').forEach(function(node){ node.remove(); });")
        lines.append("        var oldInput = root.querySelector('[data-action=\"set-auto-play-speed\"]'); if (!oldInput) return;")
        lines.append("        var input = oldInput.cloneNode(true); oldInput.replaceWith(input);")
        lines.append("        input.min = '0.5'; input.max = '2'; input.step = '0.1'; input.value = String(this._rate);")
        lines.append("        var self = this; input.addEventListener('input', function(){ self.set(this.value); }); input.addEventListener('change', function(){ self.set(this.value); });")
        lines.append("        this.updateUI();")
        lines.append("    },")
        lines.append("    updateUI: function(){")
        lines.append("        var input = document.querySelector('settings-screen [data-action=\"set-auto-play-speed\"]');")
        lines.append("        if (input) input.value = String(this._rate);")
        lines.append("    },")
        lines.append("    init: function(){ this._rate = this.load(); this.patchEngine(); this.mountUI(); this.set(this._rate); }")
        lines.append("};")
        lines.append("")
        lines.append("// ==================== 自动播放：语音结束后立即前进 ====================")
        lines.append("// 有语音时由 ended 事件驱动；无语音时按共享速度倍率计算默认等待时间。")
        lines.append("window.AutoVoiceAdvance = {")
        lines.append("    _token: '',")
        lines.append("    _voiceId: '',")
        lines.append("    _textKey: '',")
        lines.append("    _fallbackText: '',")
        lines.append("    _advancing: false,")
        lines.append("    reset: function(){ this._token = ''; this._voiceId = ''; this._textKey = ''; this._fallbackText = ''; this._advancing = false; },")
        lines.append("    enter: function(token, voiceId, textKey, fallbackText){")
        lines.append("        if (window.SmartAutoPlay) window.SmartAutoPlay.cancelWaitingAudio();")
        lines.append("        this._token = String(token || '');")
        lines.append("        this._voiceId = String(voiceId || '');")
        lines.append("        this._textKey = String(textKey || '');")
        lines.append("        this._fallbackText = String(fallbackText || '');")
        lines.append("        if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) window.SmartAutoPlay.schedule();")
        lines.append("    },")
        lines.append("    readingText: function(){")
        lines.append("        var text = '';")
        lines.append("        if (this._textKey && typeof window.__i18nText === 'function') { try { text = window.__i18nText(this._textKey) || ''; } catch(e) {} }")
        lines.append("        if (!text || text === this._textKey) text = this._fallbackText;")
        lines.append("        if (!text) {")
        lines.append("            var el = document.querySelector('#nvl-overlay.open .nvl-text, text-box [data-content=\"dialog\"], [data-component=\"text-box\"] [data-content=\"dialog\"]');")
        lines.append("            if (el) text = el.textContent || '';")
        lines.append("        }")
        lines.append("        return String(text || '').trim();")
        lines.append("    },")
        lines.append("    cancel: function(token){")
        lines.append("        token = String(token || '');")
        lines.append("        if (!token || this._token === token) {")
        lines.append("            if (window.SmartAutoPlay) window.SmartAutoPlay.cancelWaitingAudio();")
        lines.append("            this._token = ''; this._voiceId = ''; this._textKey = ''; this._fallbackText = '';")
        lines.append("        }")
        lines.append("    },")
        lines.append("    isEnabled: function(){")
        lines.append("        if (window.SmartAutoPlay) return window.SmartAutoPlay.isEnabled();")
        lines.append("        try { return !!monogatari.global('_auto_play_timer'); } catch(e) { return false; }")
        lines.append("    },")
        lines.append("    watch: function(token, voiceId){")
        lines.append("        token = String(token || ''); voiceId = String(voiceId || '');")
        lines.append("        if (!voiceId) return;")
        lines.append("        if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) window.SmartAutoPlay.clearTimer();")
        lines.append("        var self = this; var attempts = 0;")
        lines.append("        var attach = function(){")
        lines.append("            if (self._token !== token || self._voiceId !== voiceId) return;")
        lines.append("            var audio = null;")
        lines.append("            try { audio = monogatari.mediaPlayer('voice', voiceId); } catch(e) {}")
        lines.append("            if (!audio) {")
        lines.append("                if (++attempts < 20) setTimeout(attach, 10);")
        lines.append("                else if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) window.SmartAutoPlay.schedule();")
        lines.append("                return;")
        lines.append("            }")
        lines.append("            var mark = 'autoVoiceAdvanceToken';")
        lines.append("            if (audio.dataset && audio.dataset[mark] === token) return;")
        lines.append("            if (audio.dataset) audio.dataset[mark] = token;")
        lines.append("            if (window.SmartAutoPlay) window.SmartAutoPlay.waitForAudio(audio);")
        lines.append("            audio.addEventListener('error', function(){")
        lines.append("                if (self._token !== token || self._voiceId !== voiceId) return;")
        lines.append("                if (window.SmartAutoPlay) window.SmartAutoPlay.voiceFailed(token, voiceId);")
        lines.append("            }, {once: true});")
        lines.append("        };")
        lines.append("        setTimeout(attach, 0);")
        lines.append("    },")
        lines.append("    advance: function(token){")
        lines.append("        if (this._advancing || this._token !== token || !this.isEnabled()) return;")
        lines.append("        if (window.SmartAutoPlay) { window.SmartAutoPlay.advanceFromVoice(); return; }")
        lines.append("        monogatari.proceed({userInitiated:false, skip:false, autoPlay:true}).catch(function(){});")
        lines.append("    }")
        lines.append("};")
        lines.append("")
        lines.append("// ==================== 回退语音重播 ====================")
        lines.append("// 记录玩家实际经过的剧情节点；Reverse 弹出当前节点并重播上一节点语音。")
        lines.append("window.BackVoicePlayer = {")
        lines.append("    _stack: [],")
        lines.append("    _audio: null,")
        lines.append("    stopEngineVoices: function(){")
        lines.append("        try { (monogatari.mediaPlayers('voice') || []).forEach(function(audio){ try { audio.pause(); audio.currentTime = 0; } catch(e) {} }); } catch(e) {}")
        lines.append("    },")
        lines.append("    stop: function(){")
        lines.append("        this.stopEngineVoices();")
        lines.append("        if (this._audio) {")
        lines.append("            if (window.SmartAutoPlay) window.SmartAutoPlay.cancelWaitingAudio(this._audio);")
        lines.append("            try { this._audio.pause(); this._audio.currentTime = 0; this._audio.removeAttribute('src'); this._audio.load(); } catch(e) {}")
        lines.append("            this._audio = null;")
        lines.append("        }")
        lines.append("    },")
        lines.append("    reset: function(){ this.stop(); this._stack = []; },")
        lines.append("    enter: function(token, voiceId){")
        lines.append("        this.stop();")
        lines.append("        token = String(token || ''); voiceId = String(voiceId || '');")
        lines.append("        var last = this._stack.length ? this._stack[this._stack.length - 1] : null;")
        lines.append("        if (last && last.token === token) { last.voiceId = voiceId; return; }")
        lines.append("        this._stack.push({token: token, voiceId: voiceId});")
        lines.append("    },")
        lines.append("    play: function(voiceId){")
        lines.append("        this.stop();")
        lines.append("        voiceId = String(voiceId || '');")
        lines.append("        if (!voiceId) { if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) window.SmartAutoPlay.schedule(); return; }")
        lines.append("        var maps = window._voicesByLang || {};")
        lines.append("        var lang = window._lang || window._defaultLang || '';")
        lines.append("        var dict = maps[lang] || {}; var file = dict[voiceId];")
        lines.append("        if (!file) { lang = window._defaultLang || lang; dict = maps[lang] || {}; file = dict[voiceId]; }")
        lines.append("        if (!file) { console.warn('[BackVoicePlayer] 语音不存在:', voiceId); if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) window.SmartAutoPlay.schedule(); return; }")
        lines.append("        var audio = new Audio();")
        lines.append("        audio.preload = 'auto';")
        lines.append("        audio.src = 'assets/voices/' + lang + '/' + file;")
        lines.append("        try { var volume = monogatari.preference('Volume'); if (volume && volume.Voice != null) audio.volume = Number(volume.Voice); } catch(e) {}")
        lines.append("        if (window.VoicePlaybackRate) window.VoicePlaybackRate.apply(audio);")
        lines.append("        var self = this;")
        lines.append("        audio.addEventListener('ended', function(){ if (self._audio === audio) self._audio = null; });")
        lines.append("        audio.addEventListener('error', function(){ if (self._audio === audio) self._audio = null; });")
        lines.append("        this._audio = audio;")
        lines.append("        if (window.SmartAutoPlay) window.SmartAutoPlay.waitForAudio(audio);")
        lines.append("        var p = null;")
        lines.append("        try { p = audio.play(); } catch(e) {")
        lines.append("            console.warn('[BackVoicePlayer] 重播失败:', e);")
        lines.append("            if (window.SmartAutoPlay) window.SmartAutoPlay.cancelWaitingAudio(audio);")
        lines.append("            if (self._audio === audio) self._audio = null;")
        lines.append("            return;")
        lines.append("        }")
        lines.append("        if (p && typeof p.catch === 'function') p.catch(function(e){")
        lines.append("            console.warn('[BackVoicePlayer] 重播失败:', e);")
        lines.append("            if (window.SmartAutoPlay) window.SmartAutoPlay.cancelWaitingAudio(audio);")
        lines.append("            if (self._audio === audio) self._audio = null;")
        lines.append("        });")
        lines.append("    },")
        lines.append("    reverse: function(token){")
        lines.append("        this.stop(); token = String(token || '');")
        lines.append("        var last = this._stack.length ? this._stack[this._stack.length - 1] : null;")
        lines.append("        if (last && last.token === token) {")
        lines.append("            this._stack.pop();")
        lines.append("        } else {")
        lines.append("            for (var i = this._stack.length - 1; i >= 0; i--) {")
        lines.append("                if (this._stack[i].token === token) { this._stack.splice(i); break; }")
        lines.append("            }")
        lines.append("        }")
        lines.append("        var prev = this._stack.length ? this._stack[this._stack.length - 1] : null;")
        lines.append("        this.play(prev ? prev.voiceId : '');")
        lines.append("    },")
        lines.append("    reverseDeferred: function(token){")
        lines.append("        var self = this; var run = function(){ self.reverse(token); };")
        lines.append("        if (window.requestAnimationFrame) requestAnimationFrame(function(){ requestAnimationFrame(run); }); else setTimeout(run, 0);")
        lines.append("    },")
        lines.append("    seek: function(token, voiceId){")
        lines.append("        this.stop(); token = String(token || ''); voiceId = String(voiceId || '');")
        lines.append("        for (var i = this._stack.length - 1; i >= 0; i--) {")
        lines.append("            if (this._stack[i].token === token) { this._stack = this._stack.slice(0, i + 1); if (voiceId) this._stack[i].voiceId = voiceId; return; }")
        lines.append("        }")
        lines.append("        this._stack.push({token: token, voiceId: voiceId});")
        lines.append("    }")
        lines.append("};")
        lines.append("")

        lines.append("// ==================== 分支数值结算账本 ====================")
        lines.append("// 选项直接进入分支首句，避免额外 ChoiceExec label 破坏 Back 的画面/语音对齐。")
        lines.append("window.BranchEffectLedger = {")
        lines.append("    _applied: new Set(),")
        lines.append("    _change: function(fx, legacyFlags, sign){ window._playerVariables=window._playerVariables||{}; window._playerVariableChoices=window._playerVariableChoices||{}; window._playerLegacyFlags=window._playerLegacyFlags||{}; Object.entries(fx||{}).forEach(function(e){ var key=e[0], delta=sign*Number(e[1]||0); window._playerVariables[key]=Math.max(0, Math.min(100, Number(window._playerVariables[key]||0)+delta)); var stat=window._playerVariableChoices[key]||(window._playerVariableChoices[key]={positive:0,effective:0}); if(sign>0&&Number(e[1]||0)!==0){stat.effective+=1;if(Number(e[1])>0)stat.positive+=1;} else if(sign<0&&Number(e[1]||0)!==0){stat.effective=Math.max(0,stat.effective-1);if(Number(e[1])>0)stat.positive=Math.max(0,stat.positive-1);} }); Object.entries(legacyFlags||{}).forEach(function(e){ var key=e[0], delta=sign*Math.max(0,Number(e[1]||0)); window._playerLegacyFlags[key]=Math.max(0,Number(window._playerLegacyFlags[key]||0)+delta); }); if(window.updateStatsUI)window.updateStatsUI(); if(window._syncPlayerVars)window._syncPlayerVars(); },")
        lines.append("    apply: function(key, fx, legacyFlags){ key=String(key); if(this._applied.has(key))return; this._applied.add(key); this._change(fx,legacyFlags,1); },")
        lines.append("    reverse: function(key, fx, legacyFlags){ key=String(key); if(!this._applied.has(key))return; this._applied.delete(key); this._change(fx,legacyFlags,-1); },")
        lines.append("    mark: function(key){ this._applied.add(String(key)); },")
        lines.append("    clear: function(){ this._applied.clear(); }")
        lines.append("};")
        lines.append("")

        lines.append("// ==================== 软分支精确回退 ====================")
        lines.append("// Monogatari 的原生 Back 无法可靠回滚 Choice + jump 历史；分支内改为明确跳转到上一句。")
        lines.append("window.BranchNavigator = {")
        lines.append("    _state: null,")
        lines.append("    _busy: false,")
        lines.append("    enter: function(state){ this._state = state || null; this._busy = false; },")
        lines.append("    clear: function(){ this._state = null; this._busy = false; },")
        lines.append("    stopVoice: function(){")
        lines.append("        if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.reset();")
        lines.append("        if (window.SmartAutoPlay) window.SmartAutoPlay.cancelWaitingAudio();")
        lines.append("        if (window.BackVoicePlayer) window.BackVoicePlayer.stop();")
        lines.append("        try { (monogatari.mediaPlayers('voice') || []).forEach(function(audio){ try { audio.pause(); audio.currentTime = 0; } catch(e) {} }); } catch(e) {}")
        lines.append("    },")
        lines.append("    go: function(target){")
        lines.append("        if (!target || !monogatari.script || typeof monogatari.script(target) === 'undefined') return Promise.reject('Branch label does not exist: ' + target);")
        lines.append("        try { if(monogatari.stopAmbient) monogatari.stopAmbient(); } catch(e) {}")
        lines.append("        try { if(monogatari.showScreen) monogatari.showScreen('game'); } catch(e) {}")
        lines.append("        monogatari.state({step:0,label:target});")
        lines.append("        try { if(monogatari.action) monogatari.action('Dialog').reset(); } catch(e) {}")
        lines.append("        var actions = monogatari.label();")
        lines.append("        return monogatari.run(actions[0]);")
        lines.append("    },")
        lines.append("    pruneBranchHistory: function(choiceLabel){")
        lines.append("        try {")
        lines.append("            var jumps = monogatari.history('jump') || [];")
        lines.append("            var labels = monogatari.history('label') || [];")
        lines.append("            while (jumps.length) {")
        lines.append("                var item = jumps[jumps.length - 1] || {};")
        lines.append("                var source = String(item.source && item.source.label || '');")
        lines.append("                var destination = String(item.destination && item.destination.label || '');")
        lines.append("                var belongs = source === choiceLabel || destination === choiceLabel || thisLabel(source) || thisLabel(destination);")
        lines.append("                if (!belongs) break;")
        lines.append("                jumps.pop(); if (labels.length) labels.pop();")
        lines.append("            }")
        lines.append("        } catch(e) { console.warn('[BranchNavigator] 清理分支历史失败:', e); }")
        lines.append("    },")
        lines.append("    back: function(){")
        lines.append("        var state = this._state; if (!state || this._busy) return false;")
        lines.append("        var target = String(state.previousLabel || state.choiceLabel || ''); if (!target) return false;")
        lines.append("        this._busy = true; this.stopVoice();")
        lines.append("        if (state.mode === 'branch') {")
        lines.append("            if (state.isFirst) {")
        lines.append("                if (window.BranchEffectLedger) window.BranchEffectLedger.reverse(state.effectKey, state.effects || {}, state.legacyFlags || {});")
        lines.append("                if (window.BranchProgress) window.BranchProgress.clear();")
        lines.append("                this._state = null;")
        lines.append("            } else if (window.BranchProgress) {")
        lines.append("                window.BranchProgress.set(state.pivot, state.branch, state.previousSeq, target);")
        lines.append("            }")
        lines.append("        } else if (state.mode === 'choice') {")
        lines.append("            if (window.BranchProgress) window.BranchProgress.clear();")
        lines.append("            document.querySelectorAll('[data-component=\"choice-container\"], choice-container').forEach(function(node){ try { node.remove(); } catch(e) {} });")
        lines.append("            this.pruneBranchHistory(String(state.label || state.choiceLabel || ''));")
        lines.append("            this._state = null;")
        lines.append("        }")
        lines.append("        if (window.BackVoicePlayer) window.BackVoicePlayer.seek(target, state.previousVoice || '');")
        lines.append("        try { if(monogatari.action) monogatari.action('Dialog').reset(); } catch(e) {}")
        lines.append("        try { if(monogatari.global) monogatari.global('block', false); } catch(e) {}")
        lines.append("        var self = this;")
        lines.append("        setTimeout(function(){")
        lines.append("            try { var result = self.go(target); if(result && result.catch) result.catch(function(e){ self._busy=false; console.error('[BranchNavigator] 分支回退失败:',e); }); }")
        lines.append("            catch(e) { self._busy = false; console.error('[BranchNavigator] 分支回退失败:', e); }")
        lines.append("        }, 0);")
        lines.append("        return true;")
        lines.append("    },")
        lines.append("    install: function(){")
        lines.append("        if (this._installed) return; var self = this;")
        lines.append("        document.addEventListener('click', function(event){")
        lines.append("            var target = event.target; if (!target || !target.closest) return;")
        lines.append("            var back = target.closest('[data-action=\"back\"]'); if (!back || !self._state) return;")
        lines.append("            if (back.closest('[data-screen]:not([data-screen=\"game\"])')) return;")
        lines.append("            event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation(); self.back();")
        lines.append("        }, true);")
        lines.append("        this._installed = true;")
        lines.append("    }")
        lines.append("};")
        lines.append("window.BranchNavigator.install();")
        lines.append("")

        _branch_labels = []
        for _cn in choice_by_index.values():
            _pivot = _cn.get('index')
            for _letter, _key in (('A', 'branch_a'), ('B', 'branch_b')):
                for _bm in _cn.get(_key, []) or []:
                    _branch_labels.append(f"Branch{_letter}_{_pivot}_{_bm.get('seq')}")
        _branch_labels_js = json.dumps(_branch_labels, ensure_ascii=False)
        _branch_storage_key_js = json.dumps(f"VisualNovel_BranchProgress_{chap_id}_{node_id}", ensure_ascii=False)
        lines.append("// ==================== 软分支精确进度 ====================")
        lines.append("window._branchLabels = new Set(" + _branch_labels_js + ");")
        lines.append("window.BranchProgress = {")
        lines.append(f"    _storageKey: {_branch_storage_key_js},")
        lines.append("    _state: null,")
        lines.append("    valid: function(state){ return !!(state && Number.isFinite(Number(state.pivot)) && thisLabel(state.label)); },")
        lines.append("    load: function(){ try { var s = JSON.parse(localStorage.getItem(this._storageKey) || 'null'); return this.valid(s) ? s : null; } catch(e) { return null; } },")
        lines.append("    get: function(){ return this._state || (this._state = this.load()); },")
        lines.append("    set: function(pivot, branch, seq, label){ var s = {pivot:Number(pivot),branch:String(branch),seq:Number(seq),label:String(label)}; if(!this.valid(s)) return; this._state=s; try{localStorage.setItem(this._storageKey,JSON.stringify(s));}catch(e){} },")
        lines.append("    clear: function(){ this._state=null; try{localStorage.removeItem(this._storageKey);}catch(e){} },")
        lines.append("    resume: function(progress){ var external=progress&&progress.branch_state; var local=this.get(); var state=this.valid(external)?external:local; if(!this.valid(state)) return null; if(progress&&progress.current_index&&Number(progress.current_index)!==Number(state.pivot)) return null; this._state=state; return state; }")
        lines.append("};")
        lines.append("function thisLabel(label){ return window._branchLabels && window._branchLabels.has(String(label || '')); }")
        lines.append("")
        lines.append("monogatari.script({")

        # ==================== Start 标签 ====================
        # 序列化初始数值供 JS 使用
        init_vars_js = json.dumps(player_variables or {}, ensure_ascii=False)
        lines.append("    'Start': [")
        lines.append("        {'Function':{ ")
        lines.append("            'Apply': () => { ")
        lines.append("                console.log('🎬 [Start] ========== Start ==========');")
        lines.append(
            f"                if (window.GameProgressReporter) window.GameProgressReporter.init({total_steps}, '{chap_id}', '{node_id}');")
        lines.append(f"                if (window.ErrorReporter) window.ErrorReporter.init('{chap_id}', '{node_id}');")
        lines.append("                try {")
        lines.append("                    monogatari.global('auto_play', false);")
        lines.append("                    monogatari.global('skipping', false);")
        lines.append("                    document.body.removeAttribute('data-action');")
        lines.append("                } catch (e) {}")
        lines.append("                StageManager.reset();")
        lines.append("                if (window.SmartAutoPlay) window.SmartAutoPlay.set(false);")
        lines.append("                if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.reset();")
        lines.append("                if (window.BackVoicePlayer) window.BackVoicePlayer.reset();")
        lines.append("                if (window.BranchNavigator) window.BranchNavigator.clear();")
        lines.append("                if (window.BranchProgress) window.BranchProgress.clear();")
        lines.append("                if (window.BranchEffectLedger) window.BranchEffectLedger.clear();")
        lines.append("                if (window.AmbiencePlayer) window.AmbiencePlayer.stop();")
        lines.append(f"                // ── 数值初始化 ──")
        lines.append(f"                const _initVars = {init_vars_js};")
        lines.append(f"                // 优先级:URL参数 > localStorage章节入口快照 > tree.json初始值")
        lines.append(f"                (function() {{")
        lines.append(f"                    let vars = null;")
        lines.append(f"                    let choiceStats = null;")
        lines.append(f"                    let legacyFlags = null;")
        lines.append(f"                    //  1. 尝试从 URL 参数读取(章节跳转时携带)")
        lines.append(f"                    try {{")
        lines.append(f"                        const urlParams = new URLSearchParams(window.location.search);")
        lines.append(f"                        const varsParam = urlParams.get('vars');")
        lines.append(f"                        if (varsParam) vars = JSON.parse(decodeURIComponent(varsParam));")
        lines.append(f"                        const choiceStatsParam = urlParams.get('choice_stats');")
        lines.append(f"                        if (choiceStatsParam) choiceStats = JSON.parse(decodeURIComponent(choiceStatsParam));")
        lines.append(f"                        const legacyFlagsParam = urlParams.get('legacy_flags');")
        lines.append(f"                        if (legacyFlagsParam) legacyFlags = JSON.parse(decodeURIComponent(legacyFlagsParam));")
        lines.append(f"                    }} catch(e) {{}}")
        lines.append(f"                    //  2. 尝试从 localStorage 章节入口快照读取(刷新/重启后恢复)")
        lines.append(f"                    if (!vars) {{")
        lines.append(f"                        try {{")
        lines.append(f"                            const snap = localStorage.getItem('_chapterEntryVars');")
        lines.append(f"                            if (snap) vars = JSON.parse(snap);")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                    }}")
        lines.append(f"                    if (!choiceStats) {{")
        lines.append(f"                        try {{")
        lines.append(f"                            const snap = localStorage.getItem('_chapterEntryChoiceStats');")
        lines.append(f"                            if (snap) choiceStats = JSON.parse(snap);")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                    }}")
        lines.append(f"                    if (!legacyFlags) {{")
        lines.append(f"                        try {{")
        lines.append(f"                            const snap = localStorage.getItem('_chapterEntryLegacyFlags');")
        lines.append(f"                            if (snap) legacyFlags = JSON.parse(snap);")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                    }}")
        lines.append(f"                    //  3. 兜底:tree.json 初始值")
        lines.append(f"                    if (!vars) vars = Object.assign({{}}, _initVars);")
        lines.append(f"                    window._playerVariables = vars;")
        lines.append(f"                    window._playerVariableChoices = choiceStats || {{}};")
        lines.append(f"                    window._playerLegacyFlags = legacyFlags || {{}};")
        lines.append(f"                    // 保存章节入口快照(防 restart 污染)")
        lines.append(f"                    try {{ localStorage.setItem('_chapterEntryVars', JSON.stringify(vars)); }} catch(e) {{}}")
        lines.append(f"                    try {{ localStorage.setItem('_chapterEntryChoiceStats', JSON.stringify(window._playerVariableChoices)); }} catch(e) {{}}")
        lines.append(f"                    try {{ localStorage.setItem('_chapterEntryLegacyFlags', JSON.stringify(window._playerLegacyFlags)); }} catch(e) {{}}")
        lines.append(f"                    window._chapterStartStats = Object.assign({{}}, vars);")
        lines.append(f"                    // 同步到 Monogatari storage")
        lines.append(f"                    if (window._syncPlayerVars) window._syncPlayerVars();")
        lines.append(f"                    if (window.updateStatsUI) window.updateStatsUI();")
        lines.append(f"                    console.log('📊 [Start] 数值初始化完成:', vars);")
        lines.append(f"                }})();")
        lines.append("                if (window.GameProgressReporter) {")
        lines.append("                    window.GameProgressReporter.startAutoSave();")
        lines.append(f"                    window.GameProgressReporter.update({min_steps});")
        lines.append("                }")
        lines.append("                ")
        lines.append("                // [Back按钮修复]Start标签禁用Back按钮")
        lines.append("                setTimeout(() => {")
        lines.append("                    const backBtn = document.querySelector('[data-action=\"back\"]');")
        lines.append("                    if (backBtn) {")
        lines.append("                        backBtn.setAttribute('disabled', 'true');")
        lines.append("                        backBtn.style.opacity = '0.3';")
        lines.append("                        backBtn.style.pointerEvents = 'none';")
        lines.append("                        backBtn.style.cursor = 'not-allowed';")
        lines.append("                        console.log('🚫 [Start-Apply] 禁用Back按钮');")
        lines.append("                    }")
        lines.append("                }, 150);")
        lines.append("                ")
        lines.append("                // 注册全部音频到 Monogatari(不触发预加载,仅建立 stem->file 映射)")
        lines.append("                // 多语言：把语音目录指向当前语言子目录 voices/{lang}")
        lines.append("                try {")
        lines.append("                    const _ap = monogatari.setting('AssetsPath') || {};")
        lines.append("                    _ap.voices = 'voices/' + (window._lang || window._defaultLang || '');")
        lines.append("                    monogatari.setting('AssetsPath', _ap);")
        lines.append("                } catch(e){ console.warn('set voices path err', e); }")
        lines.append(
            "                if (window._allVoices) { monogatari.assets('voices', window._allVoices); window._allVoices = null; }")
        lines.append(f"                // 预加载前10步音频")
        lines.append(f"                if (window.ProgressiveLoader) window.ProgressiveLoader.load({min_steps}, 10);")
        lines.append("                ")
        lines.append("                return true;")
        lines.append("            }, ")
        lines.append("            'Reverse': () => { ")
        lines.append("                // [Back按钮修复]回退到Start时也禁用Back")
        lines.append("                setTimeout(() => {")
        lines.append("                    const backBtn = document.querySelector('[data-action=\"back\"]');")
        lines.append("                    if (backBtn) {")
        lines.append("                        backBtn.setAttribute('disabled', 'true');")
        lines.append("                        backBtn.style.opacity = '0.3';")
        lines.append("                        backBtn.style.pointerEvents = 'none';")
        lines.append("                        backBtn.style.cursor = 'not-allowed';")
        lines.append("                        console.log('🚫 [Start-Reverse] 禁用Back按钮');")
        lines.append("                    }")
        lines.append("                }, 150);")
        lines.append("                return true;")
        lines.append("            } ")
        lines.append("        }},")
        lines.append(f"        'jump Material_{min_steps}',")
        lines.append("    ],")
        lines.append("")

        # ==================== ContinueStart 标签 ====================
        lines.append("    'ContinueStart': [")
        lines.append("        {'Function':{ ")
        lines.append("            'Apply': () => { ")
        lines.append("                console.log('📥 [ContinueStart] ========== Continue ==========');")
        lines.append(
            f"                if (window.GameProgressReporter) window.GameProgressReporter.init({total_steps}, '{chap_id}', '{node_id}');")
        lines.append(f"                if (window.ErrorReporter) window.ErrorReporter.init('{chap_id}', '{node_id}');")
        lines.append("                try {")
        lines.append("                    monogatari.global('auto_play', false);")
        lines.append("                    monogatari.global('skipping', false);")
        lines.append("                    document.body.removeAttribute('data-action');")
        lines.append("                } catch (e) {}")
        lines.append("                StageManager.reset();")
        lines.append("                if (window.SmartAutoPlay) window.SmartAutoPlay.set(false);")
        lines.append("                if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.reset();")
        lines.append("                if (window.BackVoicePlayer) window.BackVoicePlayer.reset();")
        lines.append("                if (window.BranchNavigator) window.BranchNavigator.clear();")
        lines.append("                if (window.AmbiencePlayer) window.AmbiencePlayer.stop();")
        lines.append("                ")
        lines.append("                let targetIndex = 1;")
        lines.append("                if (window._progressData && window._progressData.current_index) {")
        lines.append("                    targetIndex = window._progressData.current_index;")
        lines.append(
            f"                    if (targetIndex > {total_steps} || targetIndex < {min_steps}) targetIndex = {min_steps};")
        lines.append("                }")
        lines.append("                ")
        lines.append("                if (window.GameProgressReporter) {")
        lines.append("                    window.GameProgressReporter.startAutoSave();")
        lines.append("                    window.GameProgressReporter.update(targetIndex);")
        lines.append("                }")
        lines.append("                ")
        lines.append("                window._continueTargetIndex = targetIndex;")
        lines.append("                const branchState = window.BranchProgress ? window.BranchProgress.resume(window._progressData || {}) : null;")
        lines.append("                if (branchState && window.BranchEffectLedger) window.BranchEffectLedger.mark(branchState.pivot + '_' + branchState.branch);")
        lines.append("                window._continueTargetLabel = branchState ? branchState.label : `Material_${targetIndex}`;")
        lines.append("                return true;")
        lines.append("            }, ")
        lines.append("            'Reverse': () => { return true; } ")
        lines.append("        }},")
        # 简单直接跳转，无需任何额外逻辑，因为目标Material里包含了背景指令
        lines.append("        {'Function':{ ")
        lines.append("            'Apply': () => { ")
        lines.append("                return `jump ${window._continueTargetLabel || 'Material_1'}`;")
        lines.append("            }, ")
        lines.append("            'Reverse': () => { return true; } ")
        lines.append("        }},")
        lines.append("    ],")
        lines.append("")

        # ==================== 素材循环 ====================
        sorted_music = sorted(self.music_files, key=lambda x: x['start'])
        main_music = [m for m in sorted_music if m.get('main_timeline', True)]
        music_by_id = {m['declared_id']: m for m in sorted_music if m.get('declared_id')}
        current_bgm = None
        force_music_resync = False
        # 环境底音：独立声道（AmbiencePlayer），与 BGM/SFX 并存互不干扰
        sorted_ambience = sorted(self.ambience_files, key=lambda x: x['start'])
        main_ambience = [a for a in sorted_ambience if a.get('main_timeline', True)]
        ambience_by_id = {a['declared_id']: a for a in sorted_ambience if a.get('declared_id')}

        # 记录上一步实际显示的图，用来判断要不要加特效
        previous_shown_image = None
        # 记录上一个普通 beat 的立绘 cast（JS 字面量），供本 beat 的 Reverse 回退恢复
        prev_cast_js = "[]"

        for pair_idx, pair in enumerate(audio_subtitle_pairs):
            idx = pair['index']
            audio = pair['audio']
            subtitle = pair['subtitle']
            scene_to_show = pair['scene_to_show']
            cur_label = pair.get('label') or f"Material_{idx}"
            next_pair = audio_subtitle_pairs[pair_idx + 1] if pair_idx + 1 < len(audio_subtitle_pairs) else None
            next_label = (next_pair.get('label') or f"Material_{next_pair['index']}") if next_pair else "EndChapter"

            # 本 beat 应播放的环境底音（绝对状态，非增量指令）：
            # Apply/Reverse 都同步到这个值，才能保证 back 回退、进度条 seek、读档时都正确。
            _pair_meta = beat_meta_by_index.get(idx, {})
            cur_ambience = ambience_by_id.get(_pair_meta.get('ambience_id')) if _pair_meta.get('ambience_id') else None
            if cur_ambience is None:
                cur_ambience = next((a for a in main_ambience if a['start'] <= idx <= a['end']), None)
            ambience_js = json.dumps(cur_ambience['name'] if cur_ambience else '', ensure_ascii=False)

            # ==================== 软分支节点 ====================
            if pair.get('choice_node'):
                cn = pair['choice_node']

                effects = cn.get('effects', {'a': {}, 'b': {}})
                effects_a_js = json.dumps(effects.get('a', {}), ensure_ascii=False)
                effects_b_js = json.dumps(effects.get('b', {}), ensure_ascii=False)
                legacy_flags = cn.get('legacy_flags', {'a': {}, 'b': {}})
                legacy_flags_a_js = json.dumps(legacy_flags.get('a', {}), ensure_ascii=False)
                legacy_flags_b_js = json.dumps(legacy_flags.get('b', {}), ensure_ascii=False)

                opt_a = cn['option_a_text'].replace("'", "\\'")
                opt_b = cn['option_b_text'].replace("'", "\\'")
                # 选项文本 i18n 占位（运行期按语言替换）；默认语言无该译文则回退原文本
                _MARK = '␟'
                opt_a_key = cn.get('option_a_key')
                opt_b_key = cn.get('option_b_key')
                _dl_i18n = self.i18n_text.get(self.default_language, {})
                _opt_a_has = bool(opt_a_key) and (f"{opt_a_key}__opt" in _dl_i18n)
                _opt_b_has = bool(opt_b_key) and (f"{opt_b_key}__opt" in _dl_i18n)
                opt_a_ph = f"{_MARK}{opt_a_key}__opt{_MARK}" if _opt_a_has else opt_a
                opt_b_ph = f"{_MARK}{opt_b_key}__opt{_MARK}" if _opt_b_has else opt_b
                _a_first_seq = (cn.get('branch_a') or [{'seq': 1}])[0]['seq']
                _b_first_seq = (cn.get('branch_b') or [{'seq': 1}])[0]['seq']

                # 选项页的上一层是同索引 pivot 主线台词。将它也纳入精确导航，
                # 避免从选项回退时只恢复 pivot 语音，却残留 A/B 分支字幕。
                _pivot_pair = audio_subtitle_pairs[pair_idx - 1] if pair_idx > 0 else None
                _has_pivot_pair = bool(
                    _pivot_pair
                    and _pivot_pair.get('index') == idx
                    and not _pivot_pair.get('choice_node')
                )
                _choice_prev_pair = _pivot_pair if _has_pivot_pair else (
                    audio_subtitle_pairs[pair_idx - 1] if pair_idx > 0 else None
                )
                _pivot_label = (
                    _choice_prev_pair.get('label') or f"Material_{_choice_prev_pair['index']}"
                    if _choice_prev_pair else ''
                )
                _pivot_audio = _choice_prev_pair.get('audio') if _choice_prev_pair else None
                _pivot_voice_js = json.dumps(_pivot_audio['id'] if _pivot_audio else '', ensure_ascii=False)
                _pivot_label_js = json.dumps(_pivot_label, ensure_ascii=False)
                _choice_nav_state_js = (
                    "{mode:'choice',pivot:" + str(idx)
                    + ",label:" + json.dumps(cur_label, ensure_ascii=False)
                    + ",previousLabel:" + _pivot_label_js
                    + ",previousVoice:" + _pivot_voice_js + "}"
                ) if _pivot_label else "null"

                # 选项界面 label（有 pivot 时为 Choice_N，否则 Material_N）
                _choice_token_js = json.dumps(cur_label, ensure_ascii=False)
                lines.append(f"    '{cur_label}': [")
                lines.append(
                    f"        {{'Function':{{ 'Apply': () => {{ if(window.BranchNavigator) window.BranchNavigator.enter({_choice_nav_state_js}); "
                    f"if(window.BranchProgress) window.BranchProgress.clear(); "
                    f"if(window.BackVoicePlayer) window.BackVoicePlayer.enter({_choice_token_js}, ''); "
                    f"if(window.AmbiencePlayer) window.AmbiencePlayer.sync({ambience_js}); return true; }}, "
                    f"'Reverse': () => {{ if(window.AmbiencePlayer) window.AmbiencePlayer.sync({ambience_js}); "
                    f"if(window.BackVoicePlayer) window.BackVoicePlayer.reverseDeferred({_choice_token_js}); return true; }} }} }},")
                # 背景
                has_scene_image = any(img['stem'] == scene_to_show for img in self.scene_images)
                transition_suffix = "" if scene_to_show == previous_shown_image else " with fadeIn"
                if scene_to_show != previous_shown_image:
                    previous_shown_image = scene_to_show
                if has_scene_image:
                    lines.append(f"        'show scene {scene_to_show}{transition_suffix}',")
                else:
                    lines.append(f"        'show scene #000000{transition_suffix}',")
                # 选项
                lines.append(f"        {{")
                lines.append(f"            'Choice': {{")
                lines.append(f"                'BranchA_{idx}': {{")
                lines.append(f"                    'Text': '{opt_a_ph}',")
                lines.append(f"                    'Do': 'jump BranchA_{idx}_{_a_first_seq}',")
                lines.append(f"                }},")
                lines.append(f"                'BranchB_{idx}': {{")
                lines.append(f"                    'Text': '{opt_b_ph}',")
                lines.append(f"                    'Do': 'jump BranchB_{idx}_{_b_first_seq}',")
                lines.append(f"                }},")
                lines.append(f"            }}")
                lines.append(f"        }},")
                lines.append(f"    ],")
                lines.append(f"")

                # 分支 labels
                for br_letter, br_mats in [('A', cn.get('branch_a', [])), ('B', cn.get('branch_b', []))]:
                    # 每条分支单独跟踪立绘状态。第一个分支 beat 的前置状态，
                    # 就是进入选项前的主线立绘；后续 beat 则指向同分支上一句。
                    # 这样 Back 重播上一句语音时，能同步恢复对应立绘。
                    _branch_prev_cast_js = prev_cast_js
                    _branch_prev_scene = scene_to_show
                    _branch_system_text = system_text_by_index.get(idx, '')
                    _branch_prev_cg_js = "false"
                    _branch_prev_cg_file_js = "''"
                    _branch_prev_nvl = False
                    _branch_music = current_bgm
                    _branch_effects_js = effects_a_js if br_letter == 'A' else effects_b_js
                    _branch_legacy_flags_js = legacy_flags_a_js if br_letter == 'A' else legacy_flags_b_js
                    _branch_effect_key_js = json.dumps(f"{idx}_{br_letter}", ensure_ascii=False)
                    _branch_path_label = f"Branch{br_letter}_{idx}_{br_mats[0]['seq']}" if br_mats else f"Branch{br_letter}_{idx}_1"
                    _branch_path_label_js = json.dumps(_branch_path_label, ensure_ascii=False)
                    for bm_idx, bm in enumerate(br_mats):
                        seq = bm['seq']
                        b_audio = bm.get('audio')
                        b_sub = bm.get('subtitle')
                        label = f"Branch{br_letter}_{idx}_{seq}"
                        lines.append(f"    '{label}': [")

                        # 分支 beat 的立绘必须先同步，再执行 show scene / voice。
                        # 否则引擎清空上一句对话框后，旧人物还会短暂保留，视觉上像
                        # “跳到对话框上层后才退场”。
                        _bm_meta = bm.get('meta') or (b_sub.get('meta') if b_sub else None) or (b_audio.get('meta') if b_audio else None) or {}
                        # CG 不再使用全屏覆盖层：按普通无字幕 beat 处理。
                        # 需在构造自动播放文本之前确定，避免 CG 字幕被当作等待文本。
                        _b_is_cg_beat = bool(_bm_meta.get('cg_mode'))
                        _branch_token_js = json.dumps(label, ensure_ascii=False)
                        _branch_voice_js = json.dumps(b_audio['id'] if b_audio else '', ensure_ascii=False)
                        _branch_text_key_js = json.dumps(
                            b_sub.get('subtitle_id', '') if b_sub and not _b_is_cg_beat else '',
                            ensure_ascii=False,
                        )
                        _branch_fallback_text_js = json.dumps(
                            ' '.join(str(d.get('text', '')).strip() for d in (b_sub.get('dialogues') or []) if str(d.get('text', '')).strip())
                            if b_sub and not _b_is_cg_beat else '',
                            ensure_ascii=False,
                        )
                        _b_scene = bm.get('scene_to_show') or _branch_prev_scene
                        _b_has_scene = _b_scene in valid_scene_stems
                        _b_is_nvl = bool(_bm_meta.get('nvl_mode'))
                        _b_cast = self._parse_character_position(_bm_meta.get('character_position')) if not _b_is_nvl else []
                        _b_cast_js = self._cast_to_js(_b_cast)
                        # 显式关闭历史 CG overlay，防止从旧存档/旧版脚本回退时残留。
                        _b_cg_js = "false"
                        _b_cg_file_js = "''"
                        _b_ambience = ambience_by_id.get(_bm_meta.get('ambience_id')) if _bm_meta.get('ambience_id') else None
                        _b_ambience_js = json.dumps(
                            _b_ambience['name'] if _b_ambience else (cur_ambience['name'] if cur_ambience else ''),
                            ensure_ascii=False,
                        )
                        lines.append(f"        {{'Function':{{ 'Apply': () => {{ if(window.AmbiencePlayer) window.AmbiencePlayer.sync({_b_ambience_js}); return true; }}, 'Reverse': () => {{ if(window.AmbiencePlayer) window.AmbiencePlayer.sync({_b_ambience_js}); return true; }} }} }},")

                        _desired_branch_music = music_by_id.get(_bm_meta.get('bgm_id')) if _bm_meta.get('bgm_id') else _branch_music
                        if _desired_branch_music is not _branch_music:
                            if _branch_music:
                                lines.append(f"        'stop music {_branch_music['stem']} with fade 1',")
                            if _desired_branch_music:
                                lines.append(f"        'play music {_desired_branch_music['stem']} with fade 1 loop',")
                            _branch_music = _desired_branch_music
                        _prev_system_text_js = json.dumps(_branch_system_text, ensure_ascii=False)
                        if _bm_meta.get('system_text') is not None and str(_bm_meta.get('system_text')).strip():
                            _branch_system_text = str(_bm_meta.get('system_text'))
                        _b_system_text_js = json.dumps(_branch_system_text, ensure_ascii=False)
                        _branch_prev_label = (
                            f"Branch{br_letter}_{idx}_{br_mats[bm_idx - 1]['seq']}"
                            if bm_idx > 0 else cur_label
                        )
                        _branch_prev_label_js = json.dumps(_branch_prev_label, ensure_ascii=False)
                        _branch_prev_seq = br_mats[bm_idx - 1]['seq'] if bm_idx > 0 else 0
                        _branch_prev_audio = br_mats[bm_idx - 1].get('audio') if bm_idx > 0 else None
                        _branch_prev_voice_js = json.dumps(
                            _branch_prev_audio['id'] if _branch_prev_audio else '', ensure_ascii=False
                        )
                        _branch_nav_state_js = (
                            "{mode:'branch',pivot:" + str(idx)
                            + ",branch:" + json.dumps(br_letter, ensure_ascii=False)
                            + ",seq:" + str(seq)
                            + ",label:" + _branch_token_js
                            + ",previousSeq:" + str(_branch_prev_seq)
                            + ",previousLabel:" + _branch_prev_label_js
                            + ",previousVoice:" + _branch_prev_voice_js
                            + ",choiceLabel:" + _choice_token_js
                            + ",isFirst:" + ("true" if bm_idx == 0 else "false")
                            + ",effectKey:" + _branch_effect_key_js
                            + ",effects:" + _branch_effects_js
                            + ",legacyFlags:" + _branch_legacy_flags_js + "}"
                        )
                        _effect_apply_js = (
                            f"if(window.BranchEffectLedger) window.BranchEffectLedger.apply({_branch_effect_key_js}, {_branch_effects_js}, {_branch_legacy_flags_js}); "
                            if bm_idx == 0 else ""
                        )
                        _effect_reverse_js = (
                            f"if(window.BranchEffectLedger) window.BranchEffectLedger.reverse({_branch_effect_key_js}, {_branch_effects_js}, {_branch_legacy_flags_js}); "
                            if bm_idx == 0 else ""
                        )
                        lines.append(
                            f"        {{'Function':{{ 'Apply': () => {{ "
                            f"{_effect_apply_js}"
                            f"if(window.BranchProgress) window.BranchProgress.set({idx}, '{br_letter}', {seq}, {_branch_token_js}); "
                            f"if(window.BranchNavigator) window.BranchNavigator.enter({_branch_nav_state_js}); "
                            f"if(window.AutoVoiceAdvance) window.AutoVoiceAdvance.enter({_branch_token_js}, {_branch_voice_js}, {_branch_text_key_js}, {_branch_fallback_text_js}); "
                            f"if(window.BackVoicePlayer) window.BackVoicePlayer.enter({_branch_token_js}, {_branch_voice_js}); "
                            f"if(window.SystemPanel) window.SystemPanel.setText({_b_system_text_js}); "
                            f"if(window.CgOverlay) window.CgOverlay.setMode({_b_cg_js}, {_b_cg_file_js}); "
                            f"if(window.NvlOverlay && !{str(_b_is_nvl).lower()}) window.NvlOverlay.hide(); "
                            f"StageManager.setCast({_b_cast_js}); return true; }}, "
                            f"'Reverse': () => {{ if(window.AutoVoiceAdvance) window.AutoVoiceAdvance.cancel({_branch_token_js}); "
                            f"{_effect_reverse_js}"
                            f"if(window.BranchProgress) {{ if({str(bm_idx > 0).lower()}) window.BranchProgress.set({idx}, '{br_letter}', {_branch_prev_seq}, {_branch_prev_label_js}); else window.BranchProgress.clear(); }} "
                            f"if(window.SystemPanel) window.SystemPanel.setText({_prev_system_text_js}); "
                            f"if(window.CgOverlay) window.CgOverlay.setMode({_branch_prev_cg_js}, {_branch_prev_cg_file_js}); "
                            f"if(window.NvlOverlay && !{str(_branch_prev_nvl).lower()}) window.NvlOverlay.hide(); "
                            f"StageManager.setCast({_branch_prev_cast_js}); "
                            f"if(window.BackVoicePlayer) window.BackVoicePlayer.reverseDeferred({_branch_token_js}); return true; }} }} }},")

                        # 每个分支 beat 使用自己的 scene_id / scene_shot_size。
                        if _b_has_scene:
                            lines.append(f"        'show scene {_b_scene}',")
                        else:
                            lines.append(f"        'show scene #000000',")

                        # 分支语音使用独立播放器，不写入 Monogatari 原生 voice history。
                        # 这样回退时不会把 pivot（如 86）的语音错误恢复到 A/B 字幕上；
                        # 当前分支句没有语音时也会明确停止旧语音并保持静音。
                        lines.append(
                            f"        {{'Function':{{ 'Apply': () => {{ if(window.BackVoicePlayer) window.BackVoicePlayer.play({_branch_voice_js}); return true; }}, 'Reverse': () => {{ if(window.BackVoicePlayer) window.BackVoicePlayer.stop(); return true; }} }} }},")

                        _branch_sfx_id = _bm_meta.get('sfx_id')
                        _branch_sfx = sfx_by_id.get(_branch_sfx_id) if _branch_sfx_id else None
                        if _branch_sfx:
                            lines.append(f"        'play sound {_branch_sfx['stem'].replace(chr(39), chr(92) + chr(39))}',")

                        # 对话
                        has_blocking = False

                        if b_sub and b_sub.get('dialogues') and not _b_is_cg_beat:
                            _b_speaker_id = _bm_meta.get('speaker_id')
                            _b_key = b_sub.get('subtitle_id') or ''
                            _MARK = '␟'
                            for dialogue in b_sub['dialogues']:
                                text = dialogue['text']
                                text_clean = text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').strip()
                                _b_has_i18n = bool(_b_key) and (_b_key in self.i18n_text.get(self.default_language, {}))
                                _b_placeholder = f"{_MARK}{_b_key}{_MARK}" if _b_has_i18n else text_clean

                                if text_clean:
                                    if _b_is_nvl:
                                        _b_key_js = json.dumps(_b_key, ensure_ascii=False)
                                        lines.append(
                                            f"        {{'Function':{{ 'Apply': () => {{ if(window.NvlOverlay) window.NvlOverlay.show(window.__i18nText({_b_key_js})); return true; }}, 'Reverse': () => {{ if(window.NvlOverlay) window.NvlOverlay.hide(); return true; }} }} }},")
                                        lines.append(f"        '{_b_placeholder}',")
                                    elif self._is_narrator_speaker(_b_speaker_id):
                                        lines.append(f"        '{_b_placeholder}',")
                                    else:
                                        esc_bsid = str(_b_speaker_id).replace("'", "\\'")
                                        lines.append(f"        '{esc_bsid} {_b_placeholder}',")
                                    has_blocking = True
                                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ if(monogatari.global){{ monogatari.global('auto_play',false); monogatari.global('skipping',false); }} return true; }}, 'Reverse': () => {{ return true; }} }} }},")

                        if not has_blocking:
                            lines.append(f"        'nb ...',")

                        # 每个 beat 使用独立 label；分支专用回退控制器负责明确跳回上一句。
                        if bm_idx + 1 < len(br_mats):
                            _branch_next_label = f"Branch{br_letter}_{idx}_{br_mats[bm_idx + 1]['seq']}"
                            lines.append(f"        'jump {_branch_next_label}',")
                            lines.append(f"    ],")
                            lines.append(f"")
                        else:
                            # 分支结束，收束回主线
                            # 收束点强制重置舞台；Back 回到分支末句时恢复末句立绘。
                            lines.append(f"        {{'Function':{{ 'Apply': () => {{ StageManager.reset(); return true; }}, 'Reverse': () => {{ StageManager.setCast({_b_cast_js}); return true; }} }} }},")
                            lines.append(f"        'jump {next_label}',")
                            lines.append(f"    ],")
                            lines.append(f"")
                        _branch_prev_cast_js = _b_cast_js
                        _branch_prev_scene = _b_scene
                        _branch_prev_cg_js = _b_cg_js
                        _branch_prev_cg_file_js = _b_cg_file_js
                        _branch_prev_nvl = _b_is_nvl

                # A/B 的末场景可能不同；收束后的普通 beat 必须重新执行安全过渡。
                previous_shown_image = None
                current_bgm = None
                force_music_resync = True
                continue  # choice_node 处理完毕，跳过后续普通逻辑

            # 【Back按钮修复】计算前一个索引（用于Reverse函数）
            prev_idx = audio_subtitle_pairs[pair_idx - 1]['index'] if pair_idx > 0 else idx

            # 序章钩子判断（只对传入了 hook_id 的章节生效）
            is_hook_beat = (hook_id is not None) and (idx <= hook_id)
            is_first_hook_beat = is_hook_beat and (
                        pair_idx == 0 or audio_subtitle_pairs[pair_idx - 1]['index'] > hook_id)
            is_last_hook_beat = is_hook_beat and (
                        pair_idx == len(audio_subtitle_pairs) - 1 or audio_subtitle_pairs[pair_idx + 1][
                    'index'] > hook_id)

            # 取本 beat 应显示的累计 system_text 与 nvl_mode / cg_mode（来自 master JSON）
            current_sys_text = system_text_by_index.get(idx, "")
            current_sys_text_js = json.dumps(current_sys_text, ensure_ascii=False)
            beat_meta = beat_meta_by_index.get(idx, {})
            is_nvl = bool(beat_meta.get('nvl_mode'))
            # CG 不再启用全屏覆盖层，统一按普通无字幕 beat 处理：
            # 使用正常 show scene / StageManager 流程，只跳过字幕渲染。
            is_cg_beat = bool(beat_meta.get('cg_mode'))
            is_cg_js = "false"
            cg_file_js = "''"

            # 提前计算场景与立绘状态。若本 beat 不换场，立绘会在第一个 Function
            # 中立即同步，避免对话框已清空而旧人物仍滞留到 show scene 之后。
            has_scene_image = any(img['stem'] == scene_to_show for img in self.scene_images)
            scene_target = scene_to_show if has_scene_image else '#000000'
            is_scene_change = (scene_to_show != previous_shown_image)
            if is_scene_change:
                previous_shown_image = scene_to_show

            is_normal_mode = (not is_hook_beat) and (not is_nvl)
            cast = self._parse_character_position(beat_meta.get('character_position')) if is_normal_mode else []
            cast_js = self._cast_to_js(cast)

            lines.append(f"    '{cur_label}': [")
            _beat_id_raw = beat_meta.get('beat_id_raw', '')
            if _beat_id_raw:
                lines.append(f"        // @beat {_beat_id_raw}")

            # 1. 状态重置 (UI更新) + 特效清理
            lines.append(f"        {{'Function':{{ ")
            lines.append(f"            'Apply': () => {{")
            lines.append(f"                // 清理上一个beat的持续特效")
            lines.append(f"                if (window.AtmosphereEffects) window.AtmosphereEffects.clearAll();")
            lines.append(f"                if (window.BranchNavigator) window.BranchNavigator.clear();")
            lines.append(f"                if (window.BranchProgress) window.BranchProgress.clear();")
            current_voice_js = json.dumps(audio['id'] if audio else '', ensure_ascii=False)
            current_token_js = json.dumps(cur_label, ensure_ascii=False)
            current_text_key_js = json.dumps(
                subtitle.get('subtitle_id', '') if subtitle and not is_cg_beat else '',
                ensure_ascii=False,
            )
            current_fallback_text_js = json.dumps(
                ' '.join(str(d.get('text', '')).strip() for d in (subtitle.get('dialogues') or []) if str(d.get('text', '')).strip())
                if subtitle and not is_cg_beat else '',
                ensure_ascii=False,
            )
            lines.append(
                f"                if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.enter({current_token_js}, {current_voice_js}, {current_text_key_js}, {current_fallback_text_js});")
            lines.append(
                f"                if (window.BackVoicePlayer) window.BackVoicePlayer.enter({current_token_js}, {current_voice_js});")
            if not is_scene_change:
                # 同场景时先于音乐、音效、show scene 同步人物，杜绝退场残留帧。
                lines.append(f"                StageManager.setCast({cast_js if is_normal_mode else '[]'});")
            lines.append(f"                if (monogatari.global) {{")
            lines.append(f"                    monogatari.global('auto_play', false);")
            lines.append(f"                    monogatari.global('skipping', false);")
            lines.append(f"                }}")
            lines.append(f"                document.body.removeAttribute('data-action');")
            lines.append(f"                ")
            lines.append(f"                // [Back按钮修复]控制Back按钮的启用/禁用状态")
            lines.append(f"                setTimeout(() => {{")
            lines.append(f"                    const currentIdx = {idx};")
            lines.append(f"                    const startIdx = window._sessionStartIndex || 1;")
            lines.append(f"                    const backBtn = document.querySelector('[data-action=\"back\"]');")
            lines.append(f"                    ")
            lines.append(
                f"                    console.log(`[Back检查-Apply] Material_${{currentIdx}}: 当前=${{currentIdx}}, 起始=${{startIdx}}`);")
            lines.append(f"                    ")
            lines.append(f"                    if (backBtn) {{")
            lines.append(f"                        if (currentIdx <= startIdx) {{")
            lines.append(f"                            backBtn.setAttribute('disabled', 'true');")
            lines.append(f"                            backBtn.style.opacity = '0.3';")
            lines.append(f"                            backBtn.style.pointerEvents = 'none';")
            lines.append(f"                            backBtn.style.cursor = 'not-allowed';")
            lines.append(
                f"                            console.log(`🚫 [Back-Apply] Material_${{currentIdx}}: ${{currentIdx}} <= ${{startIdx}} → 禁用`);")
            lines.append(f"                        }} else {{")
            lines.append(f"                            backBtn.removeAttribute('disabled');")
            lines.append(f"                            backBtn.style.opacity = '1';")
            lines.append(f"                            backBtn.style.pointerEvents = 'auto';")
            lines.append(f"                            backBtn.style.cursor = 'pointer';")
            lines.append(
                f"                            console.log(`✅ [Back-Apply] Material_${{currentIdx}}: ${{currentIdx}} > ${{startIdx}} → 启用`);")
            lines.append(f"                        }}")
            lines.append(f"                    }} else {{")
            lines.append(
                f"                        console.warn(`⚠️ [Back-Apply] Material_${{currentIdx}}: 找不到Back按钮`);")
            lines.append(f"                    }}")
            lines.append(f"                }}, 150);")
            lines.append(f"                ")
            lines.append(f"                if(window.GameProgressReporter) window.GameProgressReporter.update({idx});")
            lines.append(f"                if(window.ProgressiveLoader) window.ProgressiveLoader.step({idx});")
            lines.append(f"                // ── 系统面板同步 ──")
            lines.append(f"                if (window.SystemPanel) window.SystemPanel.setText({current_sys_text_js});")
            lines.append(f"                // ── CG 模式：连续 CG 走交叉淡入，避免突兀 ──")
            lines.append(f"                if (window.CgOverlay) window.CgOverlay.setMode({is_cg_js}, {cg_file_js});")
            lines.append(f"                // ── 环境底音同步（同文件不打断，不同/无则切换或停止）──")
            lines.append(f"                if (window.AmbiencePlayer) window.AmbiencePlayer.sync({ambience_js});")
            lines.append(f"                // ── NVL 模式：仅在本 beat 非 NVL 时关闭，连续 NVL 之间不闪 ──")
            if not is_nvl:
                lines.append(f"                if (window.NvlOverlay) window.NvlOverlay.hide();")
            lines.append(f"                return true;")
            lines.append(f"            }}, ")
            lines.append(f"            'Reverse': () => {{")
            lines.append(
                f"                if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.cancel({current_token_js});")
            lines.append(
                f"                if (window.BackVoicePlayer) window.BackVoicePlayer.reverse({current_token_js});")
            if not is_scene_change:
                lines.append(f"                StageManager.setCast({prev_cast_js});")
            lines.append(f"                // [Back按钮修复]回退时检查前一索引的Back按钮状态")
            lines.append(f"                setTimeout(() => {{")
            lines.append(f"                    const prevIdx = {prev_idx};  // 回退后到达的索引")
            lines.append(f"                    const startIdx = window._sessionStartIndex || 1;")
            lines.append(f"                    const backBtn = document.querySelector('[data-action=\"back\"]');")
            lines.append(f"                    ")
            lines.append(
                f"                    console.log(`[Back检查-Reverse] 从Material_{idx}回退到Material_${{prevIdx}}: 目标=${{prevIdx}}, 起始=${{startIdx}}`);")
            lines.append(f"                    ")
            lines.append(f"                    if (backBtn) {{")
            lines.append(f"                        if (prevIdx <= startIdx) {{")
            lines.append(f"                            backBtn.setAttribute('disabled', 'true');")
            lines.append(f"                            backBtn.style.opacity = '0.3';")
            lines.append(f"                            backBtn.style.pointerEvents = 'none';")
            lines.append(f"                            backBtn.style.cursor = 'not-allowed';")
            lines.append(
                f"                            console.log(`🚫 [Back-Reverse] 到达Material_${{prevIdx}}: ${{prevIdx}} <= ${{startIdx}} → 禁用`);")
            lines.append(f"                        }} else {{")
            lines.append(f"                            backBtn.removeAttribute('disabled');")
            lines.append(f"                            backBtn.style.opacity = '1';")
            lines.append(f"                            backBtn.style.pointerEvents = 'auto';")
            lines.append(f"                            backBtn.style.cursor = 'pointer';")
            lines.append(
                f"                            console.log(`✅ [Back-Reverse] 到达Material_${{prevIdx}}: ${{prevIdx}} > ${{startIdx}} → 启用`);")
            lines.append(f"                        }}")
            lines.append(f"                    }}")
            lines.append(f"                }}, 150);")
            lines.append(f"                // ── 系统面板同步（Reverse 时也要同步到本 beat 应显示的状态）──")
            lines.append(f"                if (window.SystemPanel) window.SystemPanel.setText({current_sys_text_js});")
            lines.append(f"                // ── CG 模式：Reverse 时同步本 beat 的 CG 状态 ──")
            lines.append(f"                if (window.CgOverlay) window.CgOverlay.setMode({is_cg_js}, {cg_file_js});")
            lines.append(f"                // ── 环境底音：Reverse 时同步回本 beat 应有的底音 ──")
            lines.append(f"                if (window.AmbiencePlayer) window.AmbiencePlayer.sync({ambience_js});")
            lines.append(f"                // ── NVL：本 beat 非 NVL 时才关闭，连续 NVL 回退不闪 ──")
            if not is_nvl:
                lines.append(f"                if (window.NvlOverlay) window.NvlOverlay.hide();")
            lines.append(f"                return true;")
            lines.append(f"            }}}}")
            lines.append(f"        }},")

            # 序章钩子：第一个 hook beat 时，最先进入钩子模式（在音乐/场景之前，确保text-box立即隐藏）
            if is_first_hook_beat:
                lines.append(
                    f"        {{'Function':{{ 'Apply': () => {{ return HookManager.enter(); }}, 'Reverse': () => {{ return true; }} }} }},")

            # 2. 音乐控制
            if force_music_resync:
                lines.append("        'stop music with fade 1',")
                force_music_resync = False
            if current_bgm is not None and idx > current_bgm['end']:
                bgm_stem = current_bgm['stem']
                lines.append(f"        'stop music {bgm_stem} with fade 3',")
                current_bgm = None
            if current_bgm is None:
                _declared_music = music_by_id.get(beat_meta.get('bgm_id')) if beat_meta.get('bgm_id') else None
                _music_candidates = [_declared_music] if _declared_music else main_music
                for music in _music_candidates:
                    if music and (_declared_music or music['start'] <= idx <= music['end']):
                        music_stem = music['stem']
                        lines.append(f"        'play music {music_stem} with fade 3 loop',")
                        current_bgm = music
                        break

            # 2b. 停止之前的所有SFX（SFX只绑定一个索引，进入下一步立即停）
            # 弃用指定ID停止（防止快进、跨步跳跃时漏掉指令），直接使用无参数的 'stop sound' 全局强杀所有残留音效
            lines.append(f"        'stop sound',")

            # 3. 场景控制 (核心逻辑：每一步都显式指定背景)
            if is_normal_mode and is_scene_change:
                # 换场景：0.3s 黑幕遮住 → 黑幕下同时换场景+立绘 → 黑幕淡出
                lines.append(f"        {{'Function':{{ 'Apply': async () => {{ await SceneFX.cover(); return true; }}, 'Reverse': () => {{ SceneFX.clear(); return true; }} }} }},")
                lines.append(f"        'show scene {scene_target}',")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ StageManager.setCast({cast_js}); return true; }}, 'Reverse': () => {{ StageManager.setCast({prev_cast_js}); return true; }} }} }},")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ SceneFX.reveal(); return true; }}, 'Reverse': () => {{ SceneFX.clear(); return true; }} }} }},")
            elif is_normal_mode:
                # 同场景立绘已在本 beat 的第一个 Function 中同步。
                lines.append(f"        'show scene {scene_target}',")
            else:
                # hook/nvl/cg：不放立绘；但需清空立绘，且 back 回退时恢复上一 beat 的立绘
                transition_suffix = " with fadeIn" if is_scene_change else ""
                lines.append(f"        'show scene {scene_target}{transition_suffix}',")
                if is_scene_change:
                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ StageManager.setCast([]); return true; }}, 'Reverse': () => {{ StageManager.setCast({prev_cast_js}); return true; }} }} }},")

            # 记录本 beat 的 cast，供下一个 beat 的 Reverse 回退使用
            prev_cast_js = cast_js if is_normal_mode else "[]"

            # 4. 播放音频
            if audio:
                lines.append(f"        'play voice {audio['id']}',")
                lines.append(
                    f"        {{'Function':{{ 'Apply': () => {{ if(window.AutoVoiceAdvance) window.AutoVoiceAdvance.watch({current_token_js}, {current_voice_js}); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

            # 4b. 播放SFX（与voice并行，不阻塞）
            _normal_sfx_id = beat_meta.get('sfx_id')
            _normal_sfx_items = ([sfx_by_id[_normal_sfx_id]] if _normal_sfx_id in sfx_by_id else [])
            # legacy 文件扫描没有 declared_id 时才按数字索引回退。
            if not _normal_sfx_items and not _normal_sfx_id:
                _normal_sfx_items = [s for s in sfx_by_index.get(idx, []) if not s.get('declared_id')]
            for sfx in _normal_sfx_items:
                escaped_sfx_stem = sfx['stem'].replace("'", "\\'")
                lines.append(f"        'play sound {escaped_sfx_stem}',")

            # 4c. 特效插入（在音频播放后，对话显示前）
            effects = AtmosphereEffects.get_effects_for_beat(
                self.atmosphere_config, chap_id, idx, node_id
            )
            if effects:
                for effect in effects:
                    effect_type = effect['effect_type']
                    note = effect.get('note', '').replace("'", "\\'").replace("`", "\\`")
                    lines.append(f"        // 🎨 特效: {note}")
                    lines.append(
                        f"        {{'Function': {{"
                        f"'Apply': () => {{ AtmosphereEffects.apply('{effect_type}'); return true; }}, "
                        f"'Reverse': () => {{ return true; }}"
                        f"}}}},")

            # 5. 对话处理
            has_blocking_action = False
            if subtitle and subtitle.get('dialogues') and not is_cg_beat:
                # i18n key（每个字幕文件单行 → 一个 key；运行期按 window._lang 取文本）
                sub_key = subtitle.get('subtitle_id') or ''
                MARK = '␟'
                for dialogue in subtitle['dialogues']:
                    speaker = dialogue['speaker']
                    raw_key = dialogue.get('raw_key')
                    text = dialogue['text']

                    # 转义 speaker 中的单引号，避免 JavaScript 语法错误
                    escaped_speaker = speaker.replace("'", "\\'")

                    # 清洗文本（hook beat 和普通 beat 共用）
                    text_display = text
                    if "(QTE)" in text_display: text_display = text_display.replace("(QTE)", "").replace("（QTE）", "")
                    if "(TIMING)" in text_display: text_display = text_display.replace("(TIMING)", "").replace(
                        "（TIMING）", "")
                    if "(MEMORY)" in text_display: text_display = text_display.replace("(MEMORY)", "").replace(
                        "（MEMORY）", "")
                    import re
                    interaction_pattern = r'(?:[\(\[（\s]*)INTERACTION\s*[:：]\s*(.*)$'
                    interaction_match = re.search(interaction_pattern, text_display, re.DOTALL | re.IGNORECASE)
                    if interaction_match:
                        text_display = text_display.replace(interaction_match.group(0), "")
                    text_clean = text_display.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"").strip()

                    # i18n 占位：␟{key}␟，运行期 I18nScript.apply() 按语言替换。
                    # 仅当默认语言确实收集到该 key 的译文时才用占位；否则直接嵌原文本，
                    # 避免 legacy 扫描或缺译文时占位被替换成空白。
                    _has_i18n = bool(sub_key) and (sub_key in self.i18n_text.get(self.default_language, {}))
                    placeholder = f"{MARK}{sub_key}{MARK}" if _has_i18n else text_clean

                    if is_hook_beat:
                        # ---- 序章钩子模式（已废弃，保留兼容）：文本走 HookManager ----
                        if text_clean:
                            lines.append(
                                f"        {{'Function':{{ 'Apply': () => {{ return HookManager.setText('{text_clean}'); }}, 'Reverse': () => {{ return true; }} }} }},")
                            lines.append(f"        'nb {text_clean}',")
                            has_blocking_action = True
                    elif is_nvl:
                        # ---- NVL 全屏模式：文本走 NvlOverlay，运行期从 _i18n 取 ----
                        if text_clean:
                            key_js = json.dumps(sub_key, ensure_ascii=False)
                            lines.append(
                                f"        {{'Function':{{ 'Apply': () => {{ if(window.NvlOverlay) window.NvlOverlay.show(window.__i18nText({key_js})); return true; }}, 'Reverse': () => {{ if(window.NvlOverlay) window.NvlOverlay.hide(); return true; }} }} }},")
                            lines.append(f"        '{placeholder}',")
                            has_blocking_action = True
                    else:
                        # ---- 普通模式 / CG 模式：说话人由 beat 的 speaker_id 驱动 ----
                        # CG 只是背景换成全屏大图并隐藏立绘，对话框行为与普通模式一致
                        beat_speaker_id = beat_meta.get('speaker_id')
                        if text_clean:
                            if self._is_narrator_speaker(beat_speaker_id):
                                # 旁白：裸占位行，who 区域为空（不显示任何说话人名）
                                lines.append(f"        '{placeholder}',")
                            else:
                                esc_sid = str(beat_speaker_id).replace("'", "\\'")
                                lines.append(f"        '{esc_sid} {placeholder}',")
                            has_blocking_action = True

                            # 【关键回归】必须有这个 Function 来暂停，否则字幕会一闪而过
                            lines.append(f"        {{'Function':{{ 'Apply': () => {{")
                            lines.append(f"            if (monogatari.global) {{")
                            lines.append(f"                monogatari.global('auto_play', false);")
                            lines.append(f"                monogatari.global('skipping', false);")
                            lines.append(f"            }}")
                            lines.append(f"            return true;")
                            lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")

            # 序章钩子：最后一个 hook beat 的所有对话结束后触发觉醒过渡
            if is_last_hook_beat:
                lines.append(
                    f"        {{'Function':{{ 'Apply': () => {{ return HookManager.exit(); }}, 'Reverse': () => {{ return true; }} }} }},")
                # 把 Back 按钮的尽头设置为序章结束后的第一个正常 beat，禁止 back 回序章
                if pair_idx + 1 < len(audio_subtitle_pairs):
                    first_normal_idx = audio_subtitle_pairs[pair_idx + 1]['index']
                    lines.append(
                        f"        {{'Function':{{ 'Apply': () => {{ window._sessionStartIndex = {first_normal_idx}; return true; }}, 'Reverse': () => {{ return true; }} }} }},")

            # 保底空文本
            if not has_blocking_action:
                lines.append(f"        'nb ...',")

            # 进度上报
            lines.append(
                f"        {{'Function':{{ 'Apply': () => {{ if(window.GameProgressReporter) window.GameProgressReporter.sendReport('step_progress'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

            # 跳转
            lines.append(f"        'jump {next_label}',")

            lines.append(f"    ],")
            lines.append("")

        # 结尾逻辑
        lines.append(f"    'EndChapter': [")
        lines.append(f"        'stop sound',")  # 确保进入选择界面时所有音效停止
        lines.append(f"        {{'Function':{{ 'Apply': () => {{ if(window.AmbiencePlayer) window.AmbiencePlayer.stop(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")  # 停环境底音
        lines.append(f"        'Chapter Complete',")  # 改为英文
        if node_data and node_data.get('children') and folder_mapping:
            children = node_data['children']
            # 检测是否为结局自动路由（所有 choice_text 均为空）
            all_empty = all(not c.get('choice_text', '').strip() for c in children)
            if all_empty:
                # 结局前最后一章：按最大变量值自动路由，不展示选择菜单
                conditional_endings = [c for c in children if c.get('branch_variable')]
                fallback_ending    = next((c for c in children if not c.get('branch_variable')), None)

                lines.append(f"        {{")
                lines.append(f"            'Function': {{")
                lines.append(f"                'Apply': () => {{")
                lines.append(f"                    const vars = window._playerVariables || {{}};")
                lines.append(f"                    const choiceStats = window._playerVariableChoices || {{}};")
                lines.append(f"                    const legacyFlags = window._playerLegacyFlags || {{}};")
                lines.append(f"                    const varsStr = encodeURIComponent(JSON.stringify(vars));")
                lines.append(f"                    const choiceStatsStr = encodeURIComponent(JSON.stringify(choiceStats));")
                lines.append(f"                    const legacyFlagsStr = encodeURIComponent(JSON.stringify(legacyFlags));")
                lines.append(f"                    function _goto(path, nodeId) {{")
                lines.append(f"                        const sep = path.includes('?') ? '&' : '?';")
                lines.append(f"                        const targetUrl = path + sep + 'vars=' + varsStr + '&choice_stats=' + choiceStatsStr + '&legacy_flags=' + legacyFlagsStr;")
                lines.append(f"                        if (window.StoryNavigator && window.StoryNavigator.request) {{")
                lines.append(f"                            window.StoryNavigator.request(targetUrl, nodeId);")
                lines.append(f"                        }} else if (!window._canEnterStandalone || window._canEnterStandalone(targetUrl)) {{")
                lines.append(f"                            window.location.href = targetUrl;")
                lines.append(f"                        }}")
                lines.append(f"                    }}")
                lines.append(f"                    // 找出数值最高的变量，并要求归一化倾向与领先差值达标")
                lines.append(f"                    let maxVar = null, maxVal = -Infinity;")
                lines.append(f"                    for (const [k, v] of Object.entries(vars)) {{")
                lines.append(f"                        if (v > maxVal) {{ maxVal = v; maxVar = k; }}")
                lines.append(f"                    }}")
                lines.append(f"                    console.log('🎯 [EndChapter] 最高变量:', maxVar, maxVal);")
                # 按 branch_variable 匹配，检查最高变量是否达到阈值
                for child in conditional_endings:
                    bv  = child['branch_variable']
                    thr = child['branch_threshold']
                    route_meta = (branch_thresholds or {}).get(child['child_id'], {})
                    ratio_threshold = float(route_meta.get('ratio_threshold', 0.60))
                    margin_ratio = float(route_meta.get('margin_ratio', 0.10))
                    minimum_effective_choices = int(
                        route_meta.get('minimum_effective_choices', 3)
                    )
                    route_enabled = bool(route_meta.get('route_enabled', True))
                    required_legacy_flags = route_meta.get('required_legacy_flags', []) or []
                    required_legacy_flags_js = json.dumps(required_legacy_flags, ensure_ascii=False)
                    tf  = (folder_mapping.get(child['child_id'], '') or '').replace("'", "\\'")
                    base_url = f"/{game_project + '/' if game_project else ''}{tf}/"
                    lines.append(f"                    {{")
                    lines.append(f"                    const _stat_{bv} = choiceStats['{bv}'] || {{positive:0,effective:0}};")
                    lines.append(f"                    const positive = Number(_stat_{bv}.positive || 0), effective = Number(_stat_{bv}.effective || 0);")
                    lines.append(f"                    const ratioThreshold = {ratio_threshold};")
                    lines.append(f"                    const marginRatio = {margin_ratio};")
                    lines.append(f"                    const minimumEffectiveChoices = {minimum_effective_choices};")
                    lines.append(f"                    const routeEnabled = {str(route_enabled).lower()};")
                    lines.append(f"                    const requiredLegacyFlags = {required_legacy_flags_js};")
                    lines.append(f"                    const legacyFlagsSatisfied = requiredLegacyFlags.every(flag => Number(legacyFlags[flag] || 0) > 0);")
                    lines.append(f"                    const tendency = effective > 0 ? positive / effective : 0;")
                    lines.append(f"                    const competitorTendency = Math.max(...Object.entries(choiceStats).filter(([k]) => k !== '{bv}').map(([,stat]) => {{ const e=Number((stat||{{}}).effective||0); return e>0 ? Number((stat||{{}}).positive||0)/e : 0; }}), 0);")
                    lines.append(f"                    const routeValue = Number(vars['{bv}'] || 0);")
                    lines.append(f"                    if (routeEnabled && routeValue >= {thr} && effective >= minimumEffectiveChoices && tendency >= ratioThreshold && tendency - competitorTendency >= marginRatio && legacyFlagsSatisfied) {{")
                    lines.append(f"                        console.log('✅ [EndChapter] 路由到结局: {tf}');")
                    child_node_id_js = json.dumps(child['child_id'], ensure_ascii=False)
                    lines.append(f"                        _goto('{base_url}', {child_node_id_js}); return true;")
                    lines.append(f"                    }}")
                    lines.append(f"                    }}")
                if fallback_ending:
                    tf  = (folder_mapping.get(fallback_ending['child_id'], '') or '').replace("'", "\\'")
                    base_url = f"/{game_project + '/' if game_project else ''}{tf}/"
                    lines.append(f"                    // 兜底:原著结局")
                    lines.append(f"                    console.log('📖 [EndChapter] 兜底原著结局: {tf}');")
                    fallback_node_id_js = json.dumps(fallback_ending['child_id'], ensure_ascii=False)
                    lines.append(f"                    _goto('{base_url}', {fallback_node_id_js}); return true;")
                lines.append(f"                }},")
                lines.append(f"                'Reverse': () => {{ return true; }}")
                lines.append(f"            }}")
                lines.append(f"        }},")
                lines.append(f"    ],")
                lines.append(f"")
            else:
                # 普通章节或 What-If 岔口：显示选择菜单
                lines.append(f"        'Please make your choice:',")
                lines.append(f"        {{")
                lines.append(f"            'Choice': {{")
                for child in children:
                    choice_text = child['choice_text']
                    escaped_choice_text = choice_text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"")
                    child_node_id = child['child_id']
                    escaped_child_node_id = child_node_id.replace("'", "\\'")
                    target_folder = folder_mapping.get(child_node_id)
                    if target_folder:
                        lines.append(f"                '{escaped_child_node_id}': {{")
                        lines.append(f"                    'Text': '{escaped_choice_text}',")
                        lines.append(f"                    'Do': 'jump WaitForJump_{escaped_child_node_id}',")
                        lines.append(f"                }},")
                lines.append(f"            }}")
                lines.append(f"        }},")
                lines.append(f"        // [关键]Choice后添加永久阻塞,确保即使用户不点击,游戏也不会结束")
                lines.append(f"        {{'Function':{{")
                lines.append(f"            'Apply': () => {{")
                lines.append(f"                console.log('⏸️ [EndChapter] 进入永久等待状态，游戏不会结束');")
                lines.append(f"                if (monogatari.global) {{")
                lines.append(f"                    monogatari.global('auto_play', false);")
                lines.append(f"                    monogatari.global('skipping', false);")
                lines.append(f"                }}")
                lines.append(f"                return new Promise(() => {{}});")
                lines.append(f"            }},")
                lines.append(f"            'Reverse': () => {{ return true; }}")
                lines.append(f"        }}}},")
                lines.append(f"    ],")
                lines.append(f"")

                # 为每个选择创建WaitForJump标签
                for child in children:
                    choice_text = child['choice_text']
                    escaped_choice_text = choice_text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"")
                    child_node_id = child['child_id']
                    escaped_child_node_id = child_node_id.replace("'", "\\'")
                    target_folder = folder_mapping.get(child_node_id)
                    escaped_target_folder = target_folder.replace("'", "\\'") if target_folder else ''
                    if target_folder:
                        lines.append(f"    'WaitForJump_{escaped_child_node_id}': [")
                    lines.append(f"        {{'Function':{{")
                    lines.append(f"            'Apply': () => {{")
                    lines.append(f"                console.log('🎯 [Choice] ========== 用户点击选择 ==========');")
                    lines.append(f"                console.log('🎯 [Choice] 选择文本: {escaped_choice_text}');")
                    lines.append(f"                console.log('🎯 [Choice] 目标节点: {escaped_child_node_id}');")
                    lines.append(f"                console.log('🎯 [Choice] 目标文件夹: {escaped_target_folder}');")
                    lines.append(f"                console.log('🎯 [Choice] 当前时间:', new Date().toISOString());")
                    lines.append(
                        f"                const _baseUrl = '/{game_project + '/' if game_project else ''}{escaped_target_folder}/';")
                    lines.append(f"                const _varsStr = encodeURIComponent(JSON.stringify(window._playerVariables || {{}}));")
                    lines.append(f"                const _choiceStatsStr = encodeURIComponent(JSON.stringify(window._playerVariableChoices || {{}}));")
                    lines.append(f"                const _legacyFlagsStr = encodeURIComponent(JSON.stringify(window._playerLegacyFlags || {{}}));")
                    lines.append(f"                const _langStr = encodeURIComponent(window._lang || window._defaultLang || '');")
                    lines.append(f"                const targetUrl = _baseUrl + '?vars=' + _varsStr + '&choice_stats=' + _choiceStatsStr + '&legacy_flags=' + _legacyFlagsStr + '&lang=' + _langStr;")
                    lines.append(f"                console.log('🎯 [Choice] 目标URL:', targetUrl);")
                    lines.append(f"                console.log('🎯 [Choice] 当前页面URL:', window.location.href);")
                    lines.append(f"                const requestMsg = {{")
                    lines.append(f"                    type: 'CHAPTER_REQUEST',")
                    lines.append(f"                    data: {{")
                    lines.append(f"                        target_node_id: '{escaped_child_node_id}',")
                    lines.append(f"                        target_url: targetUrl,")
                    lines.append(f"                        timestamp: Date.now()")
                    lines.append(f"                    }}")
                    lines.append(f"                }};")
                    lines.append(
                        f"                console.log('📤 [Choice] 准备发送消息:', JSON.stringify(requestMsg, null, 2));")
                    lines.append(
                        f"                console.log('📤 [Choice] 消息大小:', JSON.stringify(requestMsg).length, 'bytes');")
                    lines.append(f"                console.log('🔍 [Choice] window.parent存在?', !!window.parent);")
                    lines.append(f"                console.log('🔍 [Choice] 是iframe?', window.parent !== window);")
                    lines.append(f"                console.log('🔍 [Choice] window.top存在?', !!window.top);")
                    lines.append(
                        f"                console.log('🔍 [Choice] 是否跨域?', (() => {{ try {{ return window.parent.location.href; }} catch(e) {{ return '跨域限制'; }} }})());")
                    lines.append(f"                if (window.parent && window.parent !== window) {{")
                    lines.append(f"                    try {{")
                    lines.append(f"                        console.log('📤 [Choice] 开始发送postMessage...');")
                    lines.append(f"                        window.parent.postMessage(requestMsg, '*');")
                    lines.append(f"                        console.log('✅ [Choice] postMessage调用成功');")
                    lines.append(f"                        setTimeout(() => {{")
                    lines.append(
                        f"                            console.log('⏰ [Choice] 500ms后检查：消息已发送，等待响应中...');")
                    lines.append(f"                        }}, 500);")
                    lines.append(f"                    }} catch (e) {{")
                    lines.append(f"                        console.error('❌ [Choice] 发送消息失败:', e);")
                    lines.append(f"                        console.error('❌ [Choice] 错误堆栈:', e.stack);")
                    lines.append(f"                    }}")
                    lines.append(f"                }} else {{")
                    lines.append(f"                    if (!window._canEnterStandalone(targetUrl)) return false;")
                    lines.append(f"                    console.log('🔧 [Debug] 独立模式（非iframe），直接跳转');")
                    lines.append(f"                    window.location.href = targetUrl;")
                    lines.append(f"                    return true;")
                    lines.append(f"                }}")
                    lines.append(f"                const messageHandler = (event) => {{")
                    lines.append(f"                    console.log('📨 [Choice] 收到消息:', event.data);")
                    lines.append(f"                    console.log('📨 [Choice] 消息来源:', event.origin);")
                    lines.append(f"                    console.log('📨 [Choice] 消息类型:', event.data?.type);")
                    lines.append(f"                    if (event.data && event.data.type === 'CHAPTER_PERMIT') {{")
                    lines.append(f"                        console.log('📥 [Choice] ✅ 收到CHAPTER_PERMIT响应');")
                    lines.append(
                        f"                        console.log('📥 [Choice] 完整响应数据:', JSON.stringify(event.data, null, 2));")
                    lines.append(f"                        window.removeEventListener('message', messageHandler);")
                    lines.append(f"                        const permit = event.data.data;")
                    lines.append(f"                        ")
                    lines.append(f"                        // [关键]无论权限通过还是被拒绝,都先模拟点击Back一x")
                    lines.append(f"                        setTimeout(() => {{")
                    lines.append(
                        f"                            console.log('⬅️ [Choice] 模拟点击Back按钮，回到选择界面');")
                    lines.append(
                        f"                            const backBtn = document.querySelector('[data-action=\"back\"]');")
                    lines.append(f"                            if (backBtn) backBtn.click();")
                    lines.append(f"                        }}, 100);")
                    lines.append(f"                        ")
                    lines.append(f"                        if (permit && permit.allowed === true) {{")
                    lines.append(
                        f"                            console.log('✅ [Choice] 权限通过 (allowed=true)，准备跳转');")
                    lines.append(f"                            window.showToast('Loading...');")  # 改为英文
                    lines.append(
                        f"                            const jumpUrl = targetUrl.includes('?') ? targetUrl + '&_t=' + Date.now() : targetUrl + '?_t=' + Date.now();")
                    lines.append(f"                            console.log('🚀 [Choice] 最终跳转URL:', jumpUrl);")
                    lines.append(f"                            console.log('🚀 [Choice] 500ms后执行跳转...');")
                    lines.append(f"                            setTimeout(() => {{ ")
                    lines.append(
                        f"                                console.log('🚀 [Choice] 执行跳转: window.location.href =', jumpUrl);")
                    lines.append(f"                                window.location.href = jumpUrl; ")
                    lines.append(f"                            }}, 500);")
                    lines.append(f"                        }} else {{")
                    lines.append(
                        f"                            console.log('❌ [Choice] 权限被拒绝 (allowed=false)，停留在当前页面');")
                    lines.append(f"                            console.log('❌ [Choice] permit对象:', permit);")
                    lines.append(f"                            window.showToast('Access Denied');")  # 改为英文
                    lines.append(f"                        }}")
                    lines.append(f"                    }}")
                    lines.append(f"                }};")
                    lines.append(f"                window.addEventListener('message', messageHandler);")
                    lines.append(f"                console.log('✅ [Choice] 消息监听器已设置');")
                    lines.append(f"                setTimeout(() => {{")
                    lines.append(
                        f"                    console.warn('⚠️ [Choice] 10秒内未收到CHAPTER_PERMIT响应，可能父窗口未处理消息');")
                    lines.append(f"                }}, 10000);")
                    lines.append(f"                return true;")
                    lines.append(f"            }},")
                    lines.append(f"            'Reverse': () => {{ return true; }}")
                    lines.append(f"        }}}},")
                    lines.append(f"        // [修改]不再添加永久阻塞,让脚本自然结束后停留在EndChapter")
                    lines.append(f"        'jump EndChapter',  // 跳回EndChapter,保持选择界面显示")
                    lines.append(f"    ],")
                    lines.append(f"")
        else:
            # 真正的最终节点：Ancient 展示终幕与当前数值，Other 直接结束。
            lines.append(f"        {{'Function':{{ 'Apply': () => {{")
            lines.append(f"            if (window.AncientEndScreen) return window.AncientEndScreen.show();")
            lines.append(f"            return true;")
            lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
            lines.append(f"        'end',")
            lines.append(f"    ],")

        lines.append("});")

        return "\n".join(lines)

    def _get_ui_theme_css(self) -> str:
        """按 story_tree.UI_mode 返回游戏 UI；art_style 不再参与界面选型。"""
        if self.ui_mode != "Ancient":
            return ""

        return r"""
/* ==================== Ancient UI ==================== */
@font-face {
    font-family: 'Source Sans 3 Local';
    src: url('assets/fonts/SourceSans3-Variable.ttf') format('truetype');
    font-style: normal;
    font-weight: 200 900;
    font-display: swap;
}
@font-face {
    font-family: 'Inter Local';
    src: url('assets/fonts/Inter-Variable.ttf') format('truetype');
    font-style: normal;
    font-weight: 100 900;
    font-display: swap;
}
@font-face {
    font-family: 'Kalam Local';
    src: url('assets/fonts/Kalam-Regular.ttf') format('truetype');
    font-style: normal;
    font-weight: 400;
    font-display: swap;
}

body.ui-mode-ancient {
    --ancient-ink: #070706;
    --ancient-ink-soft: rgba(8, 8, 7, 0.82);
    --ancient-paper: #eee8db;
    --ancient-paper-muted: rgba(238, 232, 219, 0.72);
    --ancient-gold: #b79b69;
    --ancient-gold-soft: rgba(183, 155, 105, 0.38);
    --ancient-line: rgba(226, 211, 180, 0.24);
    --ancient-source-font: 'Source Sans 3 Local', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    --ancient-ui-font: 'Inter Local', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    color: var(--ancient-paper);
    font-family: var(--ancient-source-font);
    font-weight: 400;
}

body.ui-mode-ancient button,
body.ui-mode-ancient [data-ui="who"],
body.ui-mode-ancient h1,
body.ui-mode-ancient h2,
body.ui-mode-ancient h3,
body.ui-mode-ancient .badge {
    font-family: var(--ancient-ui-font) !important;
}

/* 首页：参考图的左侧纵向菜单，取消旧五主题的胶囊和高对比按钮。 */
body.ui-mode-ancient main-screen,
body.ui-mode-ancient [data-screen="main"] {
    isolation: isolate;
    background-color: #080807 !important;
    background-position: center center !important;
}
body.ui-mode-ancient main-screen::before,
body.ui-mode-ancient [data-screen="main"]::before {
    content: '';
    position: absolute;
    inset: 0;
    z-index: 0;
    pointer-events: none;
    background:
        linear-gradient(90deg, rgba(3,3,3,.90) 0%, rgba(3,3,3,.62) 27%, rgba(3,3,3,.18) 61%, rgba(3,3,3,.42) 100%),
        linear-gradient(180deg, rgba(0,0,0,.16), rgba(0,0,0,.54));
}
body.ui-mode-ancient main-menu {
    position: absolute !important;
    left: clamp(2rem, 8vw, 9rem) !important;
    top: 50% !important;
    bottom: auto !important;
    width: min(25rem, 38vw) !important;
    transform: translateY(-50%) !important;
    display: flex !important;
    flex-direction: column !important;
    align-items: flex-start !important;
    justify-content: center !important;
    gap: clamp(.45rem, 1.4vh, 1rem) !important;
    z-index: 2;
}
body.ui-mode-ancient main-menu button,
body.ui-mode-ancient [data-screen="main"] main-menu button {
    position: relative !important;
    width: auto !important;
    min-width: min(19rem, 30vw) !important;
    height: auto !important;
    margin: 0 !important;
    padding: .42rem 1.1rem .42rem .2rem !important;
    overflow: visible !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(239, 234, 222, .78) !important;
    font-family: var(--ancient-ui-font) !important;
    font-size: clamp(1.25rem, 2.5vw, 2.25rem) !important;
    font-weight: 500 !important;
    letter-spacing: .035em !important;
    text-align: left !important;
    text-transform: none !important;
    text-shadow: 0 2px 12px rgba(0,0,0,.92) !important;
    animation: none !important;
    transition: color .22s ease, transform .22s ease, text-shadow .22s ease !important;
}
body.ui-mode-ancient main-menu button::before {
    content: '' !important;
    position: absolute !important;
    left: -1rem !important;
    top: 50% !important;
    width: .48rem !important;
    height: .48rem !important;
    background: var(--ancient-gold) !important;
    opacity: 0 !important;
    transform: translateY(-50%) rotate(45deg) scale(.45) !important;
    transition: opacity .2s ease, transform .2s ease !important;
}
body.ui-mode-ancient main-menu button:hover,
body.ui-mode-ancient main-menu button:focus-visible {
    color: #fffaf0 !important;
    transform: translateX(.6rem) !important;
    text-shadow: 0 0 18px rgba(226, 201, 153, .34), 0 2px 12px #000 !important;
}
body.ui-mode-ancient main-menu button:hover::before,
body.ui-mode-ancient main-menu button:focus-visible::before {
    opacity: .9 !important;
    transform: translateY(-50%) rotate(45deg) scale(1) !important;
}
body.ui-mode-ancient main-menu button > span:first-child {
    width: 1.25em;
    margin-right: .42em;
    color: var(--ancient-gold);
    font-size: .62em;
    opacity: .58;
    text-align: center;
    filter: drop-shadow(0 0 5px rgba(183,155,105,.26));
}
main-menu [data-action="help"],
main-menu [data-open="help"],
main-menu button.is-help-button,
main-menu button:has([data-string="Help"]),
main-menu button:has([data-string="help"]) {
    display: none !important;
}

/* ADV 对话与快捷菜单。 */
body.ui-mode-ancient [data-component="text-box"] {
    background: transparent !important;
    border: 0 !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
    overflow: visible !important;
}
body.ui-mode-ancient [data-component="text-box"]::before {
    content: '';
    display: none !important;
    position: absolute !important;
    inset: 0 auto 0 0 !important;
    width: 20vw !important;
    height: auto !important;
    background: linear-gradient(90deg, rgba(2,2,2,.64), rgba(2,2,2,.34) 62%, transparent) !important;
    backdrop-filter: blur(11px) brightness(.68) saturate(.72) !important;
    pointer-events: none !important;
    z-index: 0 !important;
}
body.ui-mode-ancient [data-component="text-box"]::after {
    content: '' !important;
    display: none !important;
    position: absolute !important;
    inset: 0 0 0 auto !important;
    width: 20vw !important;
    height: auto !important;
    background: linear-gradient(270deg, rgba(2,2,2,.64), rgba(2,2,2,.34) 62%, transparent) !important;
    backdrop-filter: blur(11px) brightness(.68) saturate(.72) !important;
    pointer-events: none !important;
    z-index: 0 !important;
}
body.ui-mode-ancient [data-component="text-box"] [data-content="text"] {
    grid-column: 1 / -1 !important;
    width: 60vw !important;
    max-width: 60vw !important;
    margin: 0 auto !important;
    padding: 0 !important;
    position: relative !important;
    z-index: 2 !important;
}
body.ui-mode-ancient [data-component="text-box"] [data-content="name"] {
    grid-column: 1 / -1 !important;
    width: 60vw !important;
    max-width: 60vw !important;
    margin: 0 auto !important;
    position: relative !important;
    z-index: 2 !important;
}
body.ui-mode-ancient [data-component="text-box"] p {
    width: 100% !important;
    margin: 0 !important;
    padding: .7rem 0 1.1rem !important;
    color: rgba(247,243,233,.92) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(1.1rem, 2.15vw, 1.6rem) !important;
    font-weight: 400 !important;
    line-height: 1.72 !important;
    letter-spacing: .025em !important;
    text-shadow: 0 2px 6px rgba(0,0,0,.8) !important;
}
body.ui-mode-ancient [data-ui="who"] {
    width: auto !important;
    min-width: 0 !important;
    margin: 0 !important;
    padding: .2rem 0 !important;
    color: #f3e5c8 !important;
    background: transparent !important;
    border: 0 !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    font-family: var(--ancient-ui-font) !important;
    font-size: clamp(.95rem, 1.75vw, 1.28rem) !important;
    font-weight: 600 !important;
    letter-spacing: .11em !important;
    text-shadow: 0 2px 8px rgba(0,0,0,.95), 0 0 8px rgba(183,155,105,.22) !important;
}
body.ui-mode-ancient [data-ui="who"].speaker-empty,
body.ui-mode-ancient [data-ui="who"][hidden],
body.ui-mode-ancient [data-content="name"].speaker-empty {
    display: none !important;
}
body.ui-mode-ancient [data-component="quick-menu"] {
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
}
body.ui-mode-ancient [data-component="quick-menu"] span {
    color: rgba(226,214,190,.58) !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    font-family: var(--ancient-ui-font) !important;
    font-weight: 500 !important;
    transition: color .2s ease, filter .2s ease, transform .2s ease !important;
}
body.ui-mode-ancient [data-component="quick-menu"] span:hover {
    color: #f4e7cb !important;
    filter: drop-shadow(0 0 5px rgba(183,155,105,.48));
    transform: translateY(-1px);
}
body.ui-mode-ancient [data-component="quick-menu"] .fa,
body.ui-mode-ancient [data-component="quick-menu"] .fas,
body.ui-mode-ancient [data-component="quick-menu"] .far {
    font-size: .86em !important;
    -webkit-text-stroke: .2px rgba(239,225,197,.4);
}
/* 游戏快捷菜单不再展示 Hide；显示/隐藏快捷菜单统一放到 Settings 中。 */
body.ui-mode-ancient [data-component="quick-menu"] [data-action="distraction-free"] {
    display: none !important;
}

/* 剧情选择：不规则墨云按钮，三选时上中一个、下方左右两个。 */
body.ui-mode-ancient:has(choice-container) [data-component="text-box"],
body.ui-mode-ancient:has(choice-container) [data-ui="who"],
body.ui-mode-ancient:has(choice-container) [data-component="quick-menu"] {
    opacity: 0 !important;
    pointer-events: none !important;
}
body.ui-mode-ancient choice-container,
body.ui-mode-ancient [data-component="choice-container"] {
    position: fixed !important;
    inset: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    max-height: none !important;
    margin: 0 !important;
    transform: none !important;
    z-index: 120 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 7vh 5vw !important;
    background: radial-gradient(circle at center, rgba(0,0,0,.08), rgba(0,0,0,.25));
    pointer-events: none;
}
body.ui-mode-ancient choice-container [data-content="wrapper"],
body.ui-mode-ancient [data-component="choice-container"] [data-content="wrapper"] {
    width: min(94vw, 92rem) !important;
    display: grid !important;
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    gap: clamp(.8rem, 3vh, 2.1rem) clamp(1rem, 4vw, 4rem) !important;
    align-items: center !important;
    pointer-events: auto;
}
body.ui-mode-ancient choice-container button,
body.ui-mode-ancient [data-component="choice-container"] button {
    position: relative !important;
    width: 100% !important;
    min-height: clamp(4.25rem, 10vh, 7.2rem) !important;
    margin: 0 !important;
    padding: 1.1rem 2.5rem !important;
    border: 0 !important;
    border-radius: 0 !important;
    background:
        radial-gradient(ellipse at 50% 45%, rgba(24,22,18,.95) 0%, rgba(6,6,5,.97) 58%, rgba(0,0,0,.20) 72%, transparent 76%) !important;
    box-shadow: none !important;
    color: rgba(247,240,225,.94) !important;
    font-family: var(--ancient-ui-font) !important;
    font-size: clamp(1.02rem, 2.0vw, 1.55rem) !important;
    font-weight: 500 !important;
    line-height: 1.45 !important;
    letter-spacing: .035em !important;
    text-shadow: 0 2px 6px #000 !important;
    clip-path: polygon(3% 30%, 9% 17%, 24% 12%, 36% 5%, 55% 10%, 74% 6%, 92% 20%, 98% 45%, 94% 68%, 82% 78%, 66% 91%, 47% 86%, 29% 94%, 12% 79%, 1% 59%);
    transition: color .2s ease, transform .2s ease, filter .2s ease !important;
}
body.ui-mode-ancient choice-container button::after,
body.ui-mode-ancient [data-component="choice-container"] button::after {
    content: '';
    position: absolute;
    inset: 18% 9%;
    border-top: 1px solid rgba(183,155,105,.13);
    border-bottom: 1px solid rgba(183,155,105,.10);
    pointer-events: none;
}
body.ui-mode-ancient choice-container button:hover,
body.ui-mode-ancient choice-container button:focus-visible,
body.ui-mode-ancient [data-component="choice-container"] button:hover,
body.ui-mode-ancient [data-component="choice-container"] button:focus-visible {
    color: #fff6df !important;
    transform: translateY(-3px) scale(1.018) !important;
    filter: drop-shadow(0 0 13px rgba(183,155,105,.34)) brightness(1.16) !important;
}
body.ui-mode-ancient choice-container button:only-child,
body.ui-mode-ancient [data-component="choice-container"] button:only-child,
body.ui-mode-ancient choice-container button:first-child:nth-last-child(3),
body.ui-mode-ancient [data-component="choice-container"] button:first-child:nth-last-child(3) {
    grid-column: 1 / -1 !important;
    width: min(60%, 52rem) !important;
    justify-self: center !important;
}

/* 设置页：黑金水墨单列。左侧只保留一条阅读动线，右侧展示当前章节画面。 */
body.ui-mode-ancient settings-screen {
    position: relative !important;
    isolation: isolate;
    overflow: hidden !important;
    padding: 0 !important;
    color: var(--ancient-paper) !important;
    background:
        radial-gradient(ellipse at 24% 46%, rgba(87,65,34,.10) 0%, transparent 42%),
        repeating-linear-gradient(116deg, rgba(255,255,255,.012) 0 1px, transparent 1px 7px),
        #050505 !important;
}
body.ui-mode-ancient settings-screen::before {
    content: '';
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: 48%;
    z-index: -2;
    background-image:
        linear-gradient(rgba(61,37,10,.06), rgba(20,13,7,.16)),
        var(--ancient-settings-cover, none);
    background-size: cover;
    background-position: center center;
    filter: blur(1.8px) brightness(.55) contrast(1.05) saturate(.74) sepia(.13);
    transform: scale(1.025);
}
body.ui-mode-ancient settings-screen::after {
    content: '';
    position: fixed;
    inset: 0;
    z-index: -1;
    background:
        linear-gradient(90deg,
            #050505 0%,
            rgba(5,5,5,.99) 43%,
            rgba(5,5,5,.88) 49%,
            rgba(5,5,5,.22) 59%,
            rgba(5,5,5,.10) 82%,
            rgba(5,5,5,.38) 100%),
        linear-gradient(180deg, rgba(0,0,0,.12), transparent 22%, transparent 72%, rgba(0,0,0,.55));
    pointer-events: none;
}
body.ui-mode-ancient settings-screen > h2 {
    position: fixed !important;
    top: clamp(.8rem, 1.8vh, 1.8rem) !important;
    right: 4.2vw !important;
    width: 40vw !important;
    margin: 0 !important;
    color: rgba(244,239,228,.86) !important;
    font-family: 'Kalam Local', var(--ancient-ui-font) !important;
    font-size: clamp(2.7rem, 5.2vw, 5.4rem) !important;
    font-weight: 400 !important;
    line-height: 1 !important;
    letter-spacing: .015em !important;
    text-align: right !important;
    text-shadow: 0 4px 22px rgba(0,0,0,.96) !important;
    transform: rotate(-1.2deg);
}
body.ui-mode-ancient settings-screen > [data-action="back"],
body.ui-mode-ancient save-screen > [data-action="back"],
body.ui-mode-ancient load-screen > [data-action="back"] {
    top: clamp(.7rem, 1.8vh, 1.5rem) !important;
    left: clamp(1rem, 2.2vw, 2.6rem) !important;
    width: clamp(3.6rem, 6vw, 6.2rem) !important;
    height: clamp(3rem, 6vh, 5.2rem) !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    color: rgba(238,232,219,.72) !important;
    box-shadow: none !important;
    font-size: clamp(2rem, 4vw, 3.8rem) !important;
    filter: drop-shadow(0 3px 7px rgba(0,0,0,.88));
    transition: color .2s ease, transform .2s ease, filter .2s ease !important;
}
body.ui-mode-ancient settings-screen > [data-action="back"]:hover,
body.ui-mode-ancient save-screen > [data-action="back"]:hover,
body.ui-mode-ancient load-screen > [data-action="back"]:hover {
    color: #fff5de !important;
    transform: translateX(-5px) scale(1.04) !important;
    filter: drop-shadow(0 0 9px rgba(214,184,126,.28));
}
body.ui-mode-ancient settings-screen > .row {
    position: absolute !important;
    top: clamp(5.2rem, 11.8vh, 8.4rem) !important;
    left: clamp(2.5rem, 6.2vw, 7.2rem) !important;
    width: min(49vw, 59rem) !important;
    height: calc(100vh - clamp(6.5rem, 14.8vh, 10rem)) !important;
    margin: 0 !important;
    padding: clamp(2rem, 3.8vh, 3.2rem) clamp(1rem, 2.4vw, 2.8rem) clamp(1.2rem, 2.4vh, 2rem) !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: space-evenly !important;
    gap: clamp(.35rem, 1.05vh, .9rem) !important;
    overflow-y: auto !important;
    text-align: left !important;
    scrollbar-width: thin;
    scrollbar-color: rgba(183,155,105,.34) transparent;
}
body.ui-mode-ancient settings-screen > .row::before {
    content: '';
    position: absolute;
    top: .5rem;
    left: 0;
    width: 100%;
    height: 1px;
    background: linear-gradient(90deg, rgba(234,220,191,.68), rgba(183,155,105,.25) 72%, transparent);
    box-shadow: 0 0 8px rgba(183,155,105,.12);
}
body.ui-mode-ancient settings-screen > .row::after {
    content: '';
    position: fixed;
    top: 12vh;
    bottom: 8vh;
    left: 55.3vw;
    width: 1px;
    background: linear-gradient(180deg, transparent, rgba(183,155,105,.22) 14%, rgba(238,222,190,.12) 72%, transparent);
    opacity: .58;
    pointer-events: none;
}
body.ui-mode-ancient settings-screen > .row > .row__column {
    display: contents !important;
}
body.ui-mode-ancient settings-screen [data-settings] {
    width: 100% !important;
    min-height: clamp(3.25rem, 6.6vh, 5.1rem) !important;
    flex: 0 0 auto !important;
    margin: 0 !important;
    padding: .52rem 0 !important;
    display: grid !important;
    grid-template-columns: minmax(9.5rem, 13.5rem) minmax(12rem, 1fr) !important;
    align-items: center !important;
    column-gap: clamp(1rem, 2.6vw, 3rem) !important;
    border: 0 !important;
    text-align: left !important;
    color: rgba(238,232,219,.80) !important;
}
body.ui-mode-ancient settings-screen [data-settings="language"] { order: 0; }
body.ui-mode-ancient settings-screen [data-settings="text-speed"] { order: 1; }
body.ui-mode-ancient settings-screen [data-settings="auto-play-speed"] { order: 2; }
body.ui-mode-ancient settings-screen [data-settings="audio"] { order: 3; }
body.ui-mode-ancient settings-screen [data-settings="resolution"] { display: none !important; }
body.ui-mode-ancient settings-screen [data-settings="ancient-quick-menu"] { order: -2; }
body.ui-mode-ancient settings-screen [data-settings] > h3 {
    grid-column: 1 !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    color: rgba(239,233,219,.84) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(.92rem, 1.35vw, 1.12rem) !important;
    font-weight: 400 !important;
    line-height: 1.35 !important;
    letter-spacing: .035em !important;
    text-transform: none !important;
    text-shadow: 0 2px 7px rgba(0,0,0,.9) !important;
}
body.ui-mode-ancient settings-screen [data-settings] > input[type="range"],
body.ui-mode-ancient settings-screen [data-settings] > .horizontal,
body.ui-mode-ancient settings-screen [data-settings] > [data-content="wrapper"] {
    grid-column: 2 !important;
    width: 100% !important;
    margin: 0 !important;
}
body.ui-mode-ancient settings-screen [data-settings="language"] > [data-content="wrapper"] {
    position: relative !important;
}
body.ui-mode-ancient settings-screen [data-settings="language"] [data-select] {
    position: absolute !important;
    top: 50% !important;
    right: 1rem !important;
    z-index: 2;
    color: rgba(232,211,170,.70) !important;
    transform: translateY(-50%);
    pointer-events: none;
}
body.ui-mode-ancient settings-screen .ancient-settings-choice {
    grid-column: 2 !important;
    width: 100%;
    min-height: clamp(2.5rem, 4.8vh, 3.3rem);
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-items: stretch;
    gap: .38rem;
    padding: .1rem .35rem;
    background: radial-gradient(ellipse at center, rgba(71,67,59,.46), rgba(27,26,23,.54) 65%, transparent 73%);
}
body.ui-mode-ancient settings-screen .ancient-settings-choice button {
    position: relative;
    min-width: 0 !important;
    min-height: 2.25rem !important;
    margin: 0 !important;
    padding: .35rem .65rem !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(238,232,219,.58) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(.88rem, 1.3vw, 1.08rem) !important;
    font-weight: 400 !important;
    letter-spacing: .025em !important;
    text-shadow: 0 2px 6px #000 !important;
    transition: color .18s ease, background .18s ease, filter .18s ease !important;
}
body.ui-mode-ancient settings-screen .ancient-settings-choice button::after {
    content: '';
    position: absolute;
    left: 18%;
    right: 18%;
    bottom: .14rem;
    height: 1px;
    background: transparent;
    transition: background .18s ease, box-shadow .18s ease;
}
body.ui-mode-ancient settings-screen .ancient-settings-choice button:hover,
body.ui-mode-ancient settings-screen .ancient-settings-choice button.is-active {
    color: rgba(255,247,230,.94) !important;
    background: radial-gradient(ellipse, rgba(104,93,72,.34), transparent 72%) !important;
    filter: drop-shadow(0 0 5px rgba(183,155,105,.18));
}
body.ui-mode-ancient settings-screen .ancient-settings-choice button.is-active::after {
    background: rgba(229,202,151,.66);
    box-shadow: 0 0 7px rgba(213,181,119,.35);
}
body.ui-mode-ancient settings-screen span {
    color: rgba(238,232,219,.74) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(.9rem, 1.3vw, 1.08rem) !important;
    text-shadow: 0 2px 7px rgba(0,0,0,.85);
}
body.ui-mode-ancient settings-screen input[type="range"] {
    width: 100% !important;
    height: 1.65rem !important;
    margin: 0 !important;
    accent-color: var(--ancient-gold) !important;
    background: transparent !important;
    cursor: pointer;
    -webkit-appearance: none;
}
body.ui-mode-ancient settings-screen input[type="range"]::-webkit-slider-runnable-track {
    height: 3px;
    border-radius: 0;
    background:
        linear-gradient(90deg, transparent 0%, rgba(183,155,105,.46) 2%, rgba(239,226,198,.74) 50%, rgba(183,155,105,.36) 98%, transparent 100%);
    box-shadow: 0 1px rgba(0,0,0,.82), 0 0 6px rgba(183,155,105,.08);
}
body.ui-mode-ancient settings-screen input[type="range"]::-webkit-slider-thumb {
    width: 17px;
    height: 17px;
    margin-top: -7px;
    border: 1px solid rgba(255,235,191,.92);
    border-radius: 3px 9px 3px 9px;
    background: radial-gradient(circle at 38% 34%, #f7ddaa, #bc9558 54%, #6d4d27 100%);
    box-shadow: 0 0 6px #e9c785, 0 0 15px rgba(224,190,127,.52), 0 2px 4px #000;
    transform: rotate(45deg);
    -webkit-appearance: none;
    transition: filter .15s ease, transform .15s ease;
}
body.ui-mode-ancient settings-screen input[type="range"]:hover::-webkit-slider-thumb {
    filter: brightness(1.18);
    transform: rotate(45deg) scale(1.08);
}
body.ui-mode-ancient settings-screen select,
body.ui-mode-ancient settings-screen input[type="text"] {
    width: 100% !important;
    min-height: clamp(2.5rem, 4.8vh, 3.3rem) !important;
    padding: .45rem 2.4rem .45rem 1rem !important;
    border: 0 !important;
    border-radius: 0 !important;
    background:
        radial-gradient(ellipse at center, rgba(80,73,62,.52) 0%, rgba(35,33,29,.62) 63%, transparent 72%) !important;
    color: rgba(246,239,224,.88) !important;
    font-family: var(--ancient-source-font) !important;
    font-weight: 400 !important;
    font-size: clamp(.94rem, 1.35vw, 1.12rem) !important;
    text-align: center !important;
    text-shadow: 0 2px 6px rgba(0,0,0,.94) !important;
    box-shadow: none !important;
    outline: none !important;
    cursor: pointer;
}
body.ui-mode-ancient settings-screen [data-settings="audio"] {
    grid-template-rows: repeat(4, minmax(3.25rem, 6.6vh)) !important;
    margin-top: clamp(.45rem, 1.2vh, 1rem) !important;
    padding: clamp(.7rem, 1.4vh, 1.2rem) 0 0 !important;
    border-top: 1px solid rgba(226,211,180,.08) !important;
}
body.ui-mode-ancient settings-screen [data-settings="audio"] > h3 {
    display: none !important;
}
body.ui-mode-ancient settings-screen [data-settings="audio"] > span {
    grid-column: 1 !important;
    align-self: center !important;
    margin: 0 !important;
    padding: 0 !important;
    color: rgba(239,233,219,.84) !important;
}
body.ui-mode-ancient settings-screen [data-settings="audio"] > input[type="range"] {
    grid-column: 2 !important;
    align-self: center !important;
}
body.ui-mode-ancient settings-screen .ancient-settings-reset {
    position: fixed !important;
    right: 6.8vw !important;
    bottom: 4.8vh !important;
    z-index: 4;
    min-width: clamp(11rem, 17vw, 18rem) !important;
    min-height: 3.2rem !important;
    padding: .7rem 2rem !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: radial-gradient(ellipse, rgba(75,71,63,.68), rgba(27,26,23,.58) 65%, transparent 72%) !important;
    box-shadow: none !important;
    color: rgba(245,238,224,.82) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(.92rem, 1.4vw, 1.16rem) !important;
    font-weight: 400 !important;
    letter-spacing: .035em !important;
    text-shadow: 0 2px 8px #000 !important;
    transition: color .2s ease, filter .2s ease, transform .2s ease !important;
}
body.ui-mode-ancient settings-screen .ancient-settings-reset:hover {
    color: #fff4dc !important;
    filter: brightness(1.18) drop-shadow(0 0 8px rgba(183,155,105,.24));
    transform: translateY(-2px);
}
body.ancient-quick-menu-hidden [data-component="quick-menu"] {
    display: none !important;
}
/* Settings 选择 Hide 时同步收起数值入口；System 入口及两者自身开关状态不受影响。 */
body.ui-mode-ancient.ancient-quick-menu-hidden #stats-toggle {
    display: none !important;
}

/* 通用确认/退出弹窗：横向墨迹面板。 */
body.ui-mode-ancient alert-modal.modal {
    padding: 7vh 4vw !important;
    background: rgba(0,0,0,.58) !important;
    backdrop-filter: blur(4px) brightness(.72) !important;
}
body.ui-mode-ancient alert-modal.modal.modal--active {
    position: fixed !important;
    inset: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    margin: 0 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    transform: none !important;
    z-index: 9800 !important;
}
body.ui-mode-ancient message-modal.modal.modal--active {
    position: fixed !important;
    inset: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    margin: 0 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    transform: none !important;
    z-index: 9800 !important;
}
body.ui-mode-ancient alert-modal .modal__content {
    position: relative !important;
    width: min(92vw, 70rem) !important;
    min-height: clamp(14rem, 38vh, 24rem) !important;
    margin: auto !important;
    padding: clamp(2.8rem, 7vh, 5rem) clamp(2rem, 8vw, 8rem) 2.3rem !important;
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    border: 0 !important;
    border-radius: 0 !important;
    background:
        radial-gradient(ellipse at center, rgba(18,17,15,.98) 0%, rgba(8,8,7,.98) 65%, transparent 73%) !important;
    box-shadow: none !important;
    clip-path: polygon(2% 21%, 9% 13%, 22% 15%, 35% 8%, 50% 12%, 65% 7%, 81% 13%, 97% 8%, 99% 33%, 96% 57%, 99% 79%, 85% 87%, 69% 83%, 52% 91%, 34% 86%, 19% 92%, 3% 82%, 5% 57%);
    color: var(--ancient-paper) !important;
}
body.ui-mode-ancient alert-modal.modal--active > .modal__content,
body.ui-mode-ancient message-modal.modal--active > .modal__content {
    position: fixed !important;
    top: 50vh !important;
    left: 50vw !important;
    margin: 0 !important;
    transform: translate(-50%, -50%) !important;
}
body.ui-mode-ancient alert-modal .modal__content::before {
    content: 'TIPS';
    margin-bottom: 1.35rem;
    color: #f3eee4;
    font-family: var(--ancient-ui-font);
    font-size: clamp(1rem, 1.9vw, 1.45rem);
    font-weight: 600;
    letter-spacing: .28em;
}
body.ui-mode-ancient alert-modal [data-content="message"],
body.ui-mode-ancient alert-modal [data-content="context"] {
    max-width: 46rem;
    margin: .25rem auto !important;
    color: rgba(244,239,228,.86) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(1rem, 1.8vw, 1.3rem) !important;
    font-weight: 400 !important;
    line-height: 1.65 !important;
    text-align: center !important;
}
body.ui-mode-ancient alert-modal [data-content="actions"] {
    width: min(100%, 36rem);
    margin-top: clamp(1.2rem, 3vh, 2.2rem) !important;
    display: flex !important;
    justify-content: center !important;
    gap: clamp(.8rem, 3vw, 2.2rem) !important;
}
body.ui-mode-ancient alert-modal [data-content="actions"] button {
    min-width: 9rem !important;
    padding: .7rem 2rem !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: radial-gradient(ellipse, rgba(94,91,84,.86), rgba(39,38,35,.88) 66%, transparent 72%) !important;
    box-shadow: none !important;
    color: rgba(248,243,232,.90) !important;
    font-size: clamp(.92rem, 1.55vw, 1.14rem) !important;
    font-weight: 500 !important;
    clip-path: polygon(3% 30%, 13% 14%, 34% 18%, 52% 8%, 77% 16%, 96% 26%, 91% 75%, 71% 82%, 48% 91%, 25% 82%, 6% 70%);
    transition: filter .2s ease, transform .2s ease !important;
}
body.ui-mode-ancient alert-modal [data-content="actions"] button:hover {
    filter: brightness(1.3) drop-shadow(0 0 7px rgba(183,155,105,.35));
    transform: translateY(-2px);
}

/* 数值：固定图标、数值、名称三列。 */
body.ui-mode-ancient #stats-toggle,
body.ui-mode-ancient #system-toggle {
    border: 0 !important;
    background: transparent !important;
    color: rgba(238,224,196,.76) !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
    font-family: var(--ancient-ui-font) !important;
    text-shadow: 0 2px 7px rgba(0,0,0,.92) !important;
}
body.ui-mode-ancient #stats-toggle:hover,
body.ui-mode-ancient #system-toggle:hover {
    color: #fff0ce !important;
    background: transparent !important;
    filter: drop-shadow(0 0 6px rgba(214,185,128,.36));
}
body.ui-mode-ancient #stats-panel {
    min-width: 12.5rem !important;
    padding: .8rem 1rem !important;
    border: 1px solid rgba(183,155,105,.30) !important;
    border-radius: 1px !important;
    background: linear-gradient(120deg, rgba(9,9,8,.90), rgba(24,19,13,.78)) !important;
    box-shadow: 0 10px 28px rgba(0,0,0,.42), inset 0 0 20px rgba(183,155,105,.035) !important;
    backdrop-filter: blur(9px) !important;
}
body.ui-mode-ancient #stats-panel .stat-row {
    display: grid !important;
    grid-template-columns: 1.45rem 3rem minmax(5rem, 1fr) !important;
    gap: .42rem !important;
    align-items: center !important;
    margin: .32rem 0 !important;
    color: rgba(238,232,219,.82) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(.86rem, 1.35vw, 1rem) !important;
}
body.ui-mode-ancient #stats-panel .stat-icon {
    color: rgba(228,216,191,.74);
    font-size: 1.15rem;
    text-align: center;
    filter: drop-shadow(0 1px 3px #000);
}
body.ui-mode-ancient #stats-panel .stat-val {
    color: #f2eadb !important;
    font-variant-numeric: tabular-nums;
    font-weight: 500 !important;
}
body.ui-mode-ancient #stats-panel .stat-label {
    margin: 0 !important;
    color: rgba(238,232,219,.70) !important;
    text-align: left;
}

/* Ancient 界面不展示顶部进度条；内部进度记录、存档和跳转逻辑保持不变。 */
body.ui-mode-ancient #game-progress-container {
    display: none !important;
    top: .55rem !important;
    left: 7vw !important;
    width: 86vw !important;
    height: 2px !important;
    overflow: visible !important;
    background: linear-gradient(90deg, transparent, rgba(183,155,105,.20) 7%, rgba(238,226,201,.24) 50%, rgba(183,155,105,.20) 93%, transparent) !important;
    box-shadow: none !important;
}
body.ui-mode-ancient #game-progress-fill {
    position: relative;
    height: 100% !important;
    background: linear-gradient(90deg, rgba(183,155,105,.22), #d2b77f) !important;
    box-shadow: 0 0 7px rgba(183,155,105,.38) !important;
    transition: width .42s ease !important;
}
body.ui-mode-ancient #game-progress-fill::after {
    content: '';
    position: absolute;
    right: -.3rem;
    top: 50%;
    width: .48rem;
    height: .48rem;
    border: 1px solid rgba(241,220,177,.88);
    background: #8e7449;
    box-shadow: 0 0 8px rgba(221,190,128,.64);
    transform: translateY(-50%) rotate(45deg);
}

/* 存档 / 读档。 */
body.ui-mode-ancient save-screen,
body.ui-mode-ancient load-screen {
    overflow: auto !important;
    padding: clamp(4.5rem, 10vh, 7rem) 5vw 4vh !important;
    background:
        radial-gradient(circle at 70% 15%, rgba(73,54,26,.11), transparent 26%),
        linear-gradient(140deg, #080807, #11100d 55%, #070706) !important;
    color: var(--ancient-paper) !important;
}
body.ui-mode-ancient load-screen > h2,
body.ui-mode-ancient save-screen > h2 {
    margin: 0 0 1.5rem !important;
    color: rgba(242,231,208,.88) !important;
    font-size: clamp(1.7rem, 3vw, 2.7rem) !important;
    font-weight: 500 !important;
    letter-spacing: .1em !important;
}
body.ui-mode-ancient save-screen > .horizontal {
    margin: 0 auto 2rem !important;
    gap: .75rem !important;
}
body.ui-mode-ancient save-screen input[data-content="slot-name"] {
    min-width: min(60vw, 30rem) !important;
    border: 1px solid rgba(183,155,105,.34) !important;
    border-radius: 1px !important;
    background: rgba(5,5,4,.62) !important;
    color: var(--ancient-paper) !important;
    font-family: var(--ancient-source-font) !important;
}
body.ui-mode-ancient save-screen [data-action="save"] {
    border: 1px solid rgba(183,155,105,.42) !important;
    border-radius: 1px !important;
    background: rgba(43,35,23,.72) !important;
    color: #f0e3c9 !important;
    font-weight: 500 !important;
}
body.ui-mode-ancient slot-container {
    width: 100%;
    display: grid !important;
    grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
    gap: clamp(.8rem, 2.2vw, 1.8rem) !important;
}
body.ui-mode-ancient save-slot {
    position: relative !important;
    width: 100% !important;
    max-width: none !important;
    min-height: 12rem !important;
    overflow: hidden !important;
    border: 1px solid rgba(183,155,105,.28) !important;
    border-radius: 1px !important;
    background: rgba(8,8,7,.78) !important;
    box-shadow: 0 10px 24px rgba(0,0,0,.34), inset 0 0 20px rgba(183,155,105,.03) !important;
    transition: transform .2s ease, border-color .2s ease !important;
}
body.ui-mode-ancient save-slot:hover {
    transform: translateY(-3px);
    border-color: rgba(207,178,121,.52) !important;
}
body.ui-mode-ancient save-slot [data-content="background"] {
    min-height: 9rem;
    filter: brightness(.56) saturate(.72) sepia(.08);
    background-size: cover !important;
    background-position: center !important;
}
body.ui-mode-ancient save-slot .badge {
    position: absolute;
    left: .65rem;
    top: .6rem;
    z-index: 2;
    max-width: calc(100% - 3.2rem);
    overflow: hidden;
    color: rgba(246,237,219,.90) !important;
    background: rgba(5,5,4,.66) !important;
    border: 1px solid rgba(183,155,105,.22) !important;
    border-radius: 1px !important;
    text-overflow: ellipsis;
    white-space: nowrap;
}
body.ui-mode-ancient save-slot figcaption {
    padding: .65rem .8rem !important;
    color: rgba(229,220,202,.62) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: .82rem !important;
}
body.ui-mode-ancient save-slot [data-delete] {
    position: absolute !important;
    top: .55rem !important;
    right: .55rem !important;
    z-index: 3;
    width: 1.8rem !important;
    height: 1.8rem !important;
    padding: 0 !important;
    border: 1px solid rgba(183,155,105,.28) !important;
    border-radius: 50% !important;
    background: rgba(5,5,4,.70) !important;
    color: rgba(238,225,199,.68) !important;
}

/* 对话记录：深色卷轴式阅读面板。 */
body.ui-mode-ancient dialog-log.modal {
    background: rgba(0,0,0,.62) !important;
    backdrop-filter: blur(6px) !important;
}
body.ui-mode-ancient dialog-log .modal__content {
    width: min(88vw, 68rem) !important;
    height: min(82vh, 50rem) !important;
    padding: clamp(1.4rem, 4vw, 3.2rem) !important;
    border: 1px solid rgba(183,155,105,.30) !important;
    border-radius: 1px !important;
    background:
        linear-gradient(90deg, rgba(183,155,105,.05), transparent 10%, transparent 90%, rgba(183,155,105,.05)),
        rgba(9,9,8,.95) !important;
    box-shadow: 0 24px 70px rgba(0,0,0,.62), inset 0 0 32px rgba(183,155,105,.025) !important;
    color: var(--ancient-paper) !important;
}
body.ui-mode-ancient dialog-log [data-content="log"] {
    padding: 0 1rem 1rem !important;
    scrollbar-color: rgba(183,155,105,.38) transparent;
}
body.ui-mode-ancient dialog-log [data-spoke] {
    padding: .9rem .25rem !important;
    border-bottom: 1px solid rgba(183,155,105,.13);
}
body.ui-mode-ancient dialog-log [data-spoke] span {
    color: #ddc693 !important;
    font-family: var(--ancient-ui-font) !important;
    font-size: .94rem !important;
    font-weight: 600 !important;
    letter-spacing: .08em;
}
body.ui-mode-ancient dialog-log [data-spoke] p {
    margin: .38rem 0 0 !important;
    color: rgba(239,234,222,.82) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(.94rem, 1.35vw, 1.08rem) !important;
    font-weight: 400 !important;
    line-height: 1.72 !important;
}
body.ui-mode-ancient dialog-log [data-action="dialog-log"] {
    min-width: 8rem;
    margin: 1rem auto 0 !important;
    border: 1px solid rgba(183,155,105,.34) !important;
    border-radius: 1px !important;
    background: rgba(31,26,18,.78) !important;
    color: #eadbbd !important;
    font-weight: 500 !important;
}

body.ui-mode-ancient [data-component="centered-dialog"],
body.ui-mode-ancient #nvl-overlay .nvl-text,
body.ui-mode-ancient #custom-toast {
    font-family: var(--ancient-source-font) !important;
    font-weight: 400 !important;
}
body.ui-mode-ancient #custom-toast {
    border: 1px solid rgba(183,155,105,.30) !important;
    border-radius: 1px !important;
    background: rgba(8,8,7,.88) !important;
    color: rgba(242,232,212,.90) !important;
    box-shadow: 0 8px 25px rgba(0,0,0,.42) !important;
}

/* 终幕页。 */
body.ui-mode-ancient #ancient-end-screen {
    position: fixed;
    inset: 0;
    z-index: 9900;
    display: none;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background:
        radial-gradient(circle at 50% 45%, rgba(52,39,20,.12), transparent 32%),
        #030303;
    color: #eee8db;
    cursor: pointer;
}
body.ui-mode-ancient #ancient-end-screen.open {
    display: flex;
    animation: ancientEndFade .8s ease both;
}
body.ui-mode-ancient #ancient-end-screen .ancient-end-inner {
    width: min(88vw, 72rem);
    text-align: center;
}
body.ui-mode-ancient #ancient-end-screen .ancient-end-title {
    margin: 0 0 clamp(3rem, 10vh, 7rem);
    font-family: 'Kalam Local', var(--ancient-ui-font);
    font-size: clamp(4rem, 11vw, 9rem);
    font-weight: 400;
    letter-spacing: .02em;
    color: #f3eee4;
    text-shadow: 0 5px 28px rgba(0,0,0,.95);
}
body.ui-mode-ancient #ancient-end-screen .ancient-end-stats {
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: clamp(1rem, 5vw, 5rem);
}
body.ui-mode-ancient #ancient-end-screen .ancient-end-stat {
    min-width: 9rem;
    display: grid;
    grid-template-columns: 1.5rem auto auto;
    gap: .48rem;
    align-items: center;
    color: rgba(236,226,207,.74);
    font-family: var(--ancient-source-font);
}
body.ui-mode-ancient #ancient-end-screen .ancient-end-hint {
    margin-top: clamp(3rem, 10vh, 7rem);
    color: rgba(238,232,219,.50);
    font-family: var(--ancient-source-font);
    font-size: clamp(.88rem, 1.4vw, 1.05rem);
    letter-spacing: .08em;
    animation: ancientHintPulse 1.8s ease-in-out infinite;
}
@keyframes ancientEndFade { from { opacity: 0; } to { opacity: 1; } }
@keyframes ancientHintPulse { 0%,100% { opacity: .4; } 50% { opacity: .85; } }

@media (max-width: 800px) {
    body.ui-mode-ancient main-menu {
        left: 7vw !important;
        width: 52vw !important;
    }
    body.ui-mode-ancient main-menu button {
        min-width: 46vw !important;
        font-size: clamp(1rem, 4.2vw, 1.6rem) !important;
    }
    body.ui-mode-ancient settings-screen {
        padding: 0 !important;
    }
    body.ui-mode-ancient settings-screen::before,
    body.ui-mode-ancient settings-screen::after,
    body.ui-mode-ancient settings-screen > h2 {
        display: none !important;
    }
    body.ui-mode-ancient settings-screen > .row {
        top: 4.5rem !important;
        left: 4vw !important;
        width: 92vw !important;
        height: calc(100vh - 5rem) !important;
        padding: 1.4rem 2vw 1rem !important;
        justify-content: flex-start !important;
        gap: .45rem !important;
    }
    body.ui-mode-ancient settings-screen > .row::after,
    body.ui-mode-ancient settings-screen .ancient-settings-reset {
        display: none !important;
    }
    body.ui-mode-ancient settings-screen [data-settings] {
        grid-template-columns: minmax(7.5rem, 34vw) minmax(0, 1fr) !important;
        column-gap: .8rem !important;
    }
    body.ui-mode-ancient choice-container [data-content="wrapper"],
    body.ui-mode-ancient [data-component="choice-container"] [data-content="wrapper"] {
        grid-template-columns: 1fr !important;
        gap: .65rem !important;
    }
    body.ui-mode-ancient choice-container button,
    body.ui-mode-ancient [data-component="choice-container"] button,
    body.ui-mode-ancient choice-container button:first-child:nth-last-child(3),
    body.ui-mode-ancient [data-component="choice-container"] button:first-child:nth-last-child(3) {
        grid-column: 1 !important;
        width: 100% !important;
        min-height: 3.8rem !important;
    }
    body.ui-mode-ancient slot-container {
        grid-template-columns: 1fr !important;
    }
}
"""

        # Legacy 五主题定义保留在源码中仅为兼容历史 diff；上方已直接返回，运行时不再使用。
        themes = {

            # ── 新中式水墨 ──────────────────────────────────────────────
            "ink_chinese": """
/* === 水墨风 UI === */
[data-component="text-box"] {
    background: rgba(245, 238, 220, 0.82) !important;
    border: 1px solid rgba(80, 50, 20, 0.35) !important;
    border-top: 2px solid rgba(80, 50, 20, 0.55) !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.6), 0 -2px 18px rgba(0,0,0,0.18) !important;
    backdrop-filter: blur(6px);
}
[data-component="text-box"] p {
    color: #2a1a08 !important;
    font-family: 'Noto Serif SC', 'STSong', 'SimSun', serif !important;
    font-size: 1.2rem !important;
    letter-spacing: 0.08em !important;
    line-height: 1.85 !important;
    text-shadow: none !important;
}
[data-ui="who"] {
    color: #f5e8c0 !important;
    background: rgba(80, 50, 20, 0.78) !important;
    border: 1px solid rgba(180, 140, 80, 0.6) !important;
    box-shadow: 2px 2px 8px rgba(0,0,0,0.3) !important;
    font-family: 'Noto Serif SC', 'STKaiti', serif !important;
    letter-spacing: 0.2em !important;
    font-size: 0.95rem !important;
}
[data-component="quick-menu"] {
    background: rgba(245, 238, 220, 0.75) !important;
    border-top: 1px solid rgba(80, 50, 20, 0.25) !important;
}
[data-component="quick-menu"] span { color: #4a3010 !important; }
[data-component="quick-menu"] span:hover { color: #1a0a00 !important; }
[data-component="choice-container"] button {
    background: rgba(245, 238, 220, 0.92) !important;
    border: 1px solid rgba(80, 50, 20, 0.5) !important;
    color: #2a1a08 !important;
    font-family: 'Noto Serif SC', serif !important;
    letter-spacing: 0.15em !important;
    font-size: 1.05rem !important;
    box-shadow: 2px 2px 8px rgba(0,0,0,0.15) !important;
    transition: all 0.3s ease !important;
}
[data-component="choice-container"] button:hover {
    background: rgba(80, 50, 20, 0.85) !important;
    color: #f5e8c0 !important;
    border-color: rgba(180, 140, 80, 0.8) !important;
    box-shadow: 3px 3px 12px rgba(0,0,0,0.3) !important;
}
[data-component="centered-dialog"] {
    color: #2a1a08 !important;
    font-family: 'Noto Serif SC', 'STKaiti', serif !important;
    font-size: 1.15rem !important;
    letter-spacing: 0.2em !important;
}
[data-screen="main"] button {
    background: linear-gradient(135deg, rgba(80,50,20,0.85) 0%, rgba(120,80,30,0.9) 100%) !important;
    border: 1px solid rgba(180,140,80,0.6) !important;
    color: #f5e8c0 !important;
    font-family: 'Noto Serif SC', serif !important;
    letter-spacing: 0.25em !important;
    box-shadow: 2px 2px 12px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.15) !important;
    text-shadow: 0 1px 3px rgba(0,0,0,0.5) !important;
    border-radius: 4px !important;
}
[data-screen="main"] button:hover {
    background: linear-gradient(135deg, rgba(120,80,30,0.95) 0%, rgba(80,50,20,0.9) 100%) !important;
    box-shadow: 3px 3px 18px rgba(0,0,0,0.5), 0 0 8px rgba(180,140,80,0.3) !important;
    border-color: rgba(220,180,100,0.8) !important;
}
""",

            # ── 赛博朋克/霓虹 ────────────────────────────────────────────
            "cyberpunk": """
/* === 赛博朋克 UI === */
[data-component="text-box"] {
    background: rgba(5, 8, 20, 0.88) !important;
    border: 1px solid #00e5ff !important;
    border-top: 2px solid #ff2d78 !important;
    box-shadow: 0 0 12px rgba(0,229,255,0.25), inset 0 0 30px rgba(0,229,255,0.04) !important;
    backdrop-filter: blur(8px);
}
[data-component="text-box"] p {
    color: #c8f0ff !important;
    font-family: 'Share Tech Mono', 'Courier New', monospace !important;
    font-size: 1.15rem !important;
    letter-spacing: 0.04em !important;
    line-height: 1.75 !important;
    text-shadow: 0 0 8px rgba(0,229,255,0.5) !important;
}
[data-ui="who"] {
    color: #ff2d78 !important;
    background: rgba(255, 45, 120, 0.15) !important;
    border: 1px solid #ff2d78 !important;
    box-shadow: 0 0 10px rgba(255,45,120,0.4) !important;
    font-family: 'Share Tech Mono', monospace !important;
    letter-spacing: 0.15em !important;
    text-shadow: 0 0 8px rgba(255,45,120,0.8) !important;
    text-transform: uppercase !important;
}
[data-component="quick-menu"] {
    background: rgba(5, 8, 20, 0.9) !important;
    border-top: 1px solid rgba(0,229,255,0.3) !important;
}
[data-component="quick-menu"] span { color: #00e5ff !important; }
[data-component="quick-menu"] span:hover { color: #ff2d78 !important; }
[data-component="choice-container"] button {
    background: rgba(5, 8, 20, 0.9) !important;
    border: 1px solid #00e5ff !important;
    color: #00e5ff !important;
    font-family: 'Share Tech Mono', monospace !important;
    letter-spacing: 0.12em !important;
    font-size: 1.0rem !important;
    text-shadow: 0 0 8px rgba(0,229,255,0.6) !important;
    box-shadow: 0 0 10px rgba(0,229,255,0.2), inset 0 0 15px rgba(0,229,255,0.03) !important;
    transition: all 0.2s ease !important;
    text-transform: uppercase !important;
}
[data-component="choice-container"] button:hover {
    background: rgba(0, 229, 255, 0.12) !important;
    border-color: #ff2d78 !important;
    color: #ff2d78 !important;
    text-shadow: 0 0 10px rgba(255,45,120,0.8) !important;
    box-shadow: 0 0 15px rgba(255,45,120,0.3) !important;
}
[data-component="centered-dialog"] {
    color: #00e5ff !important;
    font-family: 'Share Tech Mono', monospace !important;
    font-size: 1.1rem !important;
    text-shadow: 0 0 12px rgba(0,229,255,0.6) !important;
    letter-spacing: 0.1em !important;
}
[data-screen="main"] button {
    background: rgba(5, 8, 20, 0.88) !important;
    border: 1px solid #00e5ff !important;
    border-top: 2px solid #ff2d78 !important;
    color: #00e5ff !important;
    font-family: 'Share Tech Mono', monospace !important;
    letter-spacing: 0.18em !important;
    text-transform: uppercase !important;
    text-shadow: 0 0 8px rgba(0,229,255,0.8) !important;
    box-shadow: 0 0 12px rgba(0,229,255,0.2), inset 0 0 20px rgba(0,229,255,0.04) !important;
    border-radius: 2px !important;
}
[data-screen="main"] button:hover {
    background: rgba(0, 229, 255, 0.1) !important;
    border-color: #ff2d78 !important;
    color: #ff2d78 !important;
    text-shadow: 0 0 10px rgba(255,45,120,0.9) !important;
    box-shadow: 0 0 18px rgba(255,45,120,0.35) !important;
}
""",

            # ── 唯美日系 ─────────────────────────────────────────────────
            "japanese": """
/* === 日系 UI === */
[data-component="text-box"] {
    background: rgba(255, 252, 255, 0.86) !important;
    border: 1px solid rgba(220, 180, 200, 0.6) !important;
    border-top: 2px solid rgba(220, 150, 180, 0.7) !important;
    box-shadow: 0 -4px 20px rgba(220,150,180,0.15), inset 0 1px 0 rgba(255,255,255,0.9) !important;
    backdrop-filter: blur(10px);
}
[data-component="text-box"] p {
    color: #3a2535 !important;
    font-family: 'Noto Sans JP', 'Hiragino Kaku Gothic Pro', 'Yu Gothic', sans-serif !important;
    font-size: 1.18rem !important;
    letter-spacing: 0.05em !important;
    line-height: 1.9 !important;
}
[data-ui="who"] {
    color: #fff !important;
    background: linear-gradient(135deg, rgba(240,180,200,0.9), rgba(220,150,180,0.85)) !important;
    border: 1px solid rgba(255,255,255,0.7) !important;
    box-shadow: 0 2px 12px rgba(220,150,180,0.4) !important;
    font-family: 'Noto Sans JP', sans-serif !important;
    letter-spacing: 0.18em !important;
    text-shadow: 0 1px 4px rgba(180,80,120,0.5) !important;
}
[data-component="quick-menu"] {
    background: rgba(255, 252, 255, 0.82) !important;
    border-top: 1px solid rgba(220,150,180,0.3) !important;
}
[data-component="quick-menu"] span { color: #c06080 !important; }
[data-component="quick-menu"] span:hover { color: #e890b8 !important; }
[data-component="choice-container"] button {
    background: rgba(255, 252, 255, 0.88) !important;
    border: 1px solid rgba(220, 150, 180, 0.6) !important;
    color: #3a2535 !important;
    font-family: 'Noto Sans JP', sans-serif !important;
    letter-spacing: 0.12em !important;
    font-size: 1.0rem !important;
    box-shadow: 0 2px 12px rgba(220,150,180,0.2) !important;
    transition: all 0.3s ease !important;
}
[data-component="choice-container"] button:hover {
    background: linear-gradient(135deg, rgba(240,180,200,0.9), rgba(220,150,180,0.85)) !important;
    color: #fff !important;
    border-color: rgba(255,255,255,0.7) !important;
    box-shadow: 0 4px 18px rgba(220,150,180,0.45) !important;
}
[data-component="centered-dialog"] {
    color: #3a2535 !important;
    font-family: 'Noto Sans JP', sans-serif !important;
    font-size: 1.1rem !important;
    letter-spacing: 0.15em !important;
    text-shadow: 0 0 15px rgba(220,150,180,0.3) !important;
}
[data-screen="main"] button {
    background: linear-gradient(135deg, rgba(240,180,200,0.88) 0%, rgba(220,150,180,0.82) 100%) !important;
    border: 1px solid rgba(255,255,255,0.6) !important;
    color: #fff !important;
    font-family: 'Noto Sans JP', sans-serif !important;
    letter-spacing: 0.2em !important;
    text-shadow: 0 1px 4px rgba(180,80,120,0.5) !important;
    box-shadow: 0 4px 18px rgba(220,150,180,0.35) !important;
    border-radius: 30px !important;
}
[data-screen="main"] button:hover {
    background: linear-gradient(135deg, rgba(220,150,180,0.95) 0%, rgba(200,120,160,0.9) 100%) !important;
    box-shadow: 0 6px 24px rgba(220,150,180,0.55) !important;
    border-color: rgba(255,255,255,0.9) !important;
}
""",

            # ── 欧式暗黑 ─────────────────────────────────────────────────
            "dark_european": """
/* === 欧式暗黑 UI === */
[data-component="text-box"] {
    background: rgba(18, 12, 8, 0.92) !important;
    border: 1px solid rgba(160, 120, 50, 0.5) !important;
    border-top: 2px solid rgba(200, 160, 70, 0.7) !important;
    box-shadow: 0 0 0 1px rgba(80,50,10,0.4), inset 0 0 40px rgba(0,0,0,0.5) !important;
    backdrop-filter: blur(4px);
}
[data-component="text-box"] p {
    color: #d4c4a0 !important;
    font-family: 'IM Fell English', 'Palatino Linotype', 'Book Antiqua', serif !important;
    font-size: 1.18rem !important;
    letter-spacing: 0.04em !important;
    line-height: 1.85 !important;
    text-shadow: 0 1px 3px rgba(0,0,0,0.8) !important;
}
[data-ui="who"] {
    color: #c8a84a !important;
    background: rgba(30, 20, 8, 0.95) !important;
    border: 1px solid rgba(200, 160, 70, 0.6) !important;
    box-shadow: 0 0 8px rgba(200,160,70,0.2), inset 0 0 10px rgba(0,0,0,0.5) !important;
    font-family: 'IM Fell English', 'Palatino Linotype', serif !important;
    letter-spacing: 0.15em !important;
    text-shadow: 0 0 10px rgba(200,160,70,0.4) !important;
}
[data-component="quick-menu"] {
    background: rgba(18, 12, 8, 0.9) !important;
    border-top: 1px solid rgba(160,120,50,0.3) !important;
}
[data-component="quick-menu"] span { color: #c8a84a !important; }
[data-component="quick-menu"] span:hover { color: #e8c870 !important; }
[data-component="choice-container"] button {
    background: rgba(18, 12, 8, 0.95) !important;
    border: 1px solid rgba(200, 160, 70, 0.5) !important;
    color: #c8a84a !important;
    font-family: 'IM Fell English', 'Palatino Linotype', serif !important;
    letter-spacing: 0.15em !important;
    font-size: 1.0rem !important;
    text-shadow: 0 0 8px rgba(200,168,74,0.3) !important;
    box-shadow: 0 0 8px rgba(200,160,70,0.15), inset 0 0 20px rgba(0,0,0,0.4) !important;
    transition: all 0.3s ease !important;
}
[data-component="choice-container"] button:hover {
    background: rgba(200, 160, 70, 0.15) !important;
    border-color: rgba(200, 160, 70, 0.9) !important;
    color: #e8c870 !important;
    text-shadow: 0 0 12px rgba(200,168,74,0.7) !important;
    box-shadow: 0 0 15px rgba(200,160,70,0.3) !important;
}
[data-component="centered-dialog"] {
    color: #c8a84a !important;
    font-family: 'IM Fell English', 'Palatino Linotype', serif !important;
    font-size: 1.1rem !important;
    letter-spacing: 0.18em !important;
    text-shadow: 0 0 12px rgba(200,168,74,0.35) !important;
}
[data-screen="main"] button {
    background: rgba(18, 12, 8, 0.95) !important;
    border: 1px solid rgba(200, 160, 70, 0.55) !important;
    color: #c8a84a !important;
    font-family: 'IM Fell English', 'Palatino Linotype', serif !important;
    letter-spacing: 0.2em !important;
    text-shadow: 0 0 10px rgba(200,168,74,0.4) !important;
    box-shadow: 0 0 10px rgba(200,160,70,0.15), inset 0 0 30px rgba(0,0,0,0.5) !important;
    border-radius: 2px !important;
}
[data-screen="main"] button:hover {
    background: rgba(200, 160, 70, 0.12) !important;
    border-color: rgba(200, 160, 70, 0.9) !important;
    color: #e8c870 !important;
    text-shadow: 0 0 14px rgba(200,168,74,0.8) !important;
    box-shadow: 0 0 18px rgba(200,160,70,0.3) !important;
}
""",

            # ── 现代写实 ─────────────────────────────────────────────────
            "modern_realism": """
/* === 现代写实 UI === */
[data-component="text-box"] {
    background: rgba(10, 10, 10, 0.82) !important;
    border: none !important;
    border-top: 1px solid rgba(255,255,255,0.12) !important;
    box-shadow: none !important;
    backdrop-filter: blur(12px);
}
[data-component="text-box"] p {
    color: #f0f0f0 !important;
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif !important;
    font-size: 1.15rem !important;
    letter-spacing: 0.02em !important;
    line-height: 1.8 !important;
}
[data-ui="who"] {
    color: #ffffff !important;
    background: rgba(255,255,255,0.1) !important;
    border: 1px solid rgba(255,255,255,0.2) !important;
    box-shadow: none !important;
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif !important;
    letter-spacing: 0.08em !important;
    font-weight: 500 !important;
}
[data-component="quick-menu"] {
    background: rgba(10, 10, 10, 0.75) !important;
    border-top: 1px solid rgba(255,255,255,0.08) !important;
}
[data-component="quick-menu"] span { color: #aaa !important; }
[data-component="quick-menu"] span:hover { color: #fff !important; }
[data-component="choice-container"] button {
    background: rgba(255, 255, 255, 0.08) !important;
    border: 1px solid rgba(255, 255, 255, 0.18) !important;
    color: #f0f0f0 !important;
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif !important;
    letter-spacing: 0.06em !important;
    font-size: 1.0rem !important;
    backdrop-filter: blur(8px) !important;
    transition: all 0.25s ease !important;
}
[data-component="choice-container"] button:hover {
    background: rgba(255, 255, 255, 0.16) !important;
    border-color: rgba(255, 255, 255, 0.45) !important;
    color: #fff !important;
}
[data-component="centered-dialog"] {
    color: #e8e8e8 !important;
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif !important;
    font-size: 1.1rem !important;
    letter-spacing: 0.06em !important;
}
[data-screen="main"] button {
    background: rgba(255, 255, 255, 0.08) !important;
    border: 1px solid rgba(255, 255, 255, 0.2) !important;
    color: #f0f0f0 !important;
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif !important;
    letter-spacing: 0.1em !important;
    font-weight: 400 !important;
    backdrop-filter: blur(12px) !important;
    box-shadow: none !important;
    border-radius: 6px !important;
    text-transform: none !important;
}
[data-screen="main"] button:hover {
    background: rgba(255, 255, 255, 0.16) !important;
    border-color: rgba(255, 255, 255, 0.5) !important;
    color: #fff !important;
    box-shadow: 0 4px 20px rgba(0,0,0,0.3) !important;
}
""",
        }
        theme_css = themes.get(self.art_style, themes["modern_realism"])

        # 动态字号覆盖：按浏览器宽度自动伸缩（clamp 的中间项 vw 随分辨率变化）。
        # 放在主题 CSS 之后、选择器相同，覆盖前面写死的 font-size。大屏放大、小屏不溢出。
        dynamic_font_css = """
/* === 对话区动态字号（响应分辨率，覆盖各主题写死值）=== */
[data-component="text-box"] p {
    font-size: clamp(1.25rem, 2.6vw, 1.75rem) !important;
}
[data-ui="who"] {
    font-size: clamp(1.0rem, 1.9vw, 1.35rem) !important;
}
[data-component="choice-container"] button {
    font-size: clamp(1.05rem, 2.0vw, 1.4rem) !important;
}
[data-component="centered-dialog"] {
    font-size: clamp(1.15rem, 2.3vw, 1.6rem) !important;
}
"""
        return theme_css + dynamic_font_css

    def _get_hook_css(self) -> str:
        """序章钩子只区分 Ancient / Other，不再读取 art_style。"""

        # 各风格共用的结构性 CSS（overlay/cursor/flash 骨架不变，只改视觉）
        common = """
/* 全屏黑幕 */
#hook-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    z-index: 800;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    opacity: 0; pointer-events: none;
    transition: opacity 0.6s ease;
}
body.hook-active #hook-overlay  { opacity: 1; pointer-events: auto; }
body.hook-exiting #hook-overlay { opacity: 0; pointer-events: none; transition: opacity 1.2s ease; }
#hook-hint {
    margin-top: 2em; font-size: 0.72rem;
    letter-spacing: 0.3em; text-transform: uppercase; font-family: sans-serif;
    opacity: 0; transition: opacity 1s ease 1.5s;
}
body.hook-active #hook-hint { opacity: 1; }
body.hook-active [data-component="text-box"],
body.hook-active [data-ui="who"],
body.hook-active [data-component="quick-menu"],
body.hook-active game-screen {
    opacity: 0 !important; pointer-events: none !important; transition: opacity 0.4s ease;
}
body.hook-done [data-component="text-box"],
body.hook-done [data-ui="who"],
body.hook-done [data-component="quick-menu"],
body.hook-done game-screen {
    opacity: 1 !important; pointer-events: auto !important; transition: opacity 0.8s ease 0.2s;
}
#hook-flash {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    z-index: 9998; opacity: 0; pointer-events: none; transition: opacity 0.07s ease;
}
/* 段落容器：堆叠模式 */
#hook-text-content {
    display: flex; flex-direction: column; align-items: center;
    width: 100%; transition: opacity 0.4s ease;
}
#hook-text-content p {
    margin: 0 0 0.6em 0; padding: 0;
    opacity: 0; animation: hookFadeIn 0.5s ease forwards;
}
@keyframes hookFadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
"""

        if self.ui_mode == "Ancient":
            return common + """
body.ui-mode-ancient #hook-overlay {
    background: radial-gradient(circle at center, rgba(39,30,17,.14), transparent 38%), #030303;
}
body.ui-mode-ancient #hook-text-content { max-width: 68%; }
body.ui-mode-ancient #hook-text-content p {
    color: rgba(241,235,222,.90);
    font-size: clamp(1.15rem, 2.45vw, 1.82rem);
    font-family: 'Source Sans 3 Local', 'PingFang SC', sans-serif;
    font-weight: 400;
    letter-spacing: .08em;
    line-height: 1.95;
    text-align: center;
    text-shadow: 0 2px 18px rgba(0,0,0,.92);
}
body.ui-mode-ancient #hook-cursor {
    display: inline-block;
    width: 2px;
    height: 1.08em;
    margin-left: 4px;
    vertical-align: middle;
    background: #c6a86d;
    box-shadow: 0 0 7px rgba(198,168,109,.52);
    animation: hookBlink 1.1s step-end infinite;
    opacity: 0;
}
body.ui-mode-ancient.hook-active #hook-cursor { opacity: 1; }
body.ui-mode-ancient #hook-hint {
    color: rgba(225,211,184,.30);
    font-family: 'Inter Local', sans-serif;
}
body.ui-mode-ancient #hook-flash { background: #e8dcc3; }
@keyframes hookBlink { 0%,100% { opacity:1; } 50% { opacity:0; } }
"""

        return common + """
#hook-overlay { background: #000; }
#hook-text-content { max-width: 72%; }
#hook-text-content p {
    color: #eee;
    font-size: clamp(1.2rem, 2.5vw, 1.85rem);
    font-family: sans-serif;
    line-height: 1.9;
    text-align: center;
}
#hook-cursor {
    display: inline-block; width: 2px; height: 1.1em;
    background: #eee; margin-left: 4px; vertical-align: middle;
    animation: hookBlink 1s step-end infinite; opacity: 0;
}
body.hook-active #hook-cursor { opacity: 1; }
#hook-hint { color: rgba(255,255,255,.25); }
#hook-flash { background: #fff; }
@keyframes hookBlink { 0%,100% { opacity:1; } 50% { opacity:0; } }
"""

        # 历史 art_style 变体已失效；上方 Ancient / Other 会提前返回。
        variants = {

            # ── 水墨风：宣纸米白底，墨色文字，毛笔字感 ──────────────────
            "ink_chinese": common + """
#hook-overlay { background: #f5eedc; }
#hook-text-content { max-width: 68%; }
#hook-text-content p {
    color: #1a0e04;
    font-size: clamp(1.2rem, 2.5vw, 1.8rem);
    font-family: 'Noto Serif SC', 'STKaiti', 'KaiTi', serif;
    letter-spacing: 0.22em; line-height: 2.2;
    text-align: center;
    text-shadow: 1px 1px 0 rgba(0,0,0,0.08);
}
#hook-cursor {
    display: inline-block; width: 1px; height: 1.1em;
    background: #4a2e10; margin-left: 3px; vertical-align: middle;
    animation: hookBlink 1.2s step-end infinite; opacity: 0;
}
body.hook-active #hook-cursor { opacity: 1; }
#hook-hint { color: rgba(80,50,20,0.35); }
#hook-flash { background: #f5eedc; }
@keyframes hookBlink { 0%,100%{opacity:1} 50%{opacity:0} }
""",

            # ── 赛博朋克：纯黑底，霓虹青文字，故障闪烁 ──────────────────
            "cyberpunk": common + """
#hook-overlay { background: #020408; }
#hook-text-content { max-width: 72%; }
#hook-text-content p {
    color: #00e5ff;
    font-size: clamp(1.1rem, 2.4vw, 1.75rem);
    font-family: 'Share Tech Mono', 'Courier New', monospace;
    letter-spacing: 0.1em; line-height: 1.8;
    text-align: center;
    text-shadow: 0 0 10px rgba(0,229,255,0.8), 0 0 30px rgba(0,229,255,0.3);
}
#hook-cursor {
    display: inline-block; width: 8px; height: 1.1em;
    background: #ff2d78; margin-left: 4px; vertical-align: middle;
    animation: hookBlink 0.7s step-end infinite; opacity: 0;
    box-shadow: 0 0 8px #ff2d78;
}
body.hook-active #hook-cursor { opacity: 1; }
#hook-hint { color: rgba(0,229,255,0.25); font-family: monospace; }
#hook-flash { background: #00e5ff; }
@keyframes hookBlink { 0%,100%{opacity:1} 50%{opacity:0} }
""",

            # ── 日系：淡樱花渐变底，深色文字，柔和 ──────────────────────
            "japanese": common + """
#hook-overlay { background: linear-gradient(160deg, #1a0a12 0%, #0d0810 100%); }
#hook-text-content { max-width: 70%; }
#hook-text-content p {
    color: #f8e8f0;
    font-size: clamp(1.15rem, 2.5vw, 1.8rem);
    font-family: 'Noto Sans JP', 'Hiragino Kaku Gothic Pro', sans-serif;
    letter-spacing: 0.12em; line-height: 2.0;
    text-align: center;
    text-shadow: 0 0 20px rgba(220,150,180,0.4);
}
#hook-cursor {
    display: inline-block; width: 2px; height: 1.1em;
    background: #e890b8; margin-left: 4px; vertical-align: middle;
    animation: hookBlink 1s step-end infinite; opacity: 0;
    box-shadow: 0 0 6px rgba(232,144,184,0.8);
}
body.hook-active #hook-cursor { opacity: 1; }
#hook-hint { color: rgba(232,144,184,0.3); }
#hook-flash { background: #fff0f5; }
@keyframes hookBlink { 0%,100%{opacity:1} 50%{opacity:0} }
""",

            # ── 欧式暗黑：极深棕黑底，金色文字，古典感 ──────────────────
            "dark_european": common + """
#hook-overlay { background: #0c0804; }
#hook-text-content { max-width: 68%; }
#hook-text-content p {
    color: #c8a84a;
    font-size: clamp(1.15rem, 2.5vw, 1.85rem);
    font-family: 'IM Fell English', 'Palatino Linotype', 'Book Antiqua', serif;
    letter-spacing: 0.1em; line-height: 1.95;
    text-align: center;
    text-shadow: 0 0 15px rgba(200,168,74,0.35), 0 1px 3px rgba(0,0,0,0.9);
}
#hook-cursor {
    display: inline-block; width: 2px; height: 1.1em;
    background: #c8a84a; margin-left: 4px; vertical-align: middle;
    animation: hookBlink 1.1s step-end infinite; opacity: 0;
    box-shadow: 0 0 8px rgba(200,168,74,0.6);
}
body.hook-active #hook-cursor { opacity: 1; }
#hook-hint { color: rgba(200,168,74,0.2); font-family: serif; letter-spacing: 0.4em; }
#hook-flash { background: #c8a84a; }
@keyframes hookBlink { 0%,100%{opacity:1} 50%{opacity:0} }
""",

            # ── 现代写实：纯黑底，白色大字，电影感 ──────────────────────
            "modern_realism": common + """
#hook-overlay { background: #000; }
#hook-text-content { max-width: 72%; }
#hook-text-content p {
    color: #e8e8e8;
    font-size: clamp(1.3rem, 2.8vw, 2rem);
    font-family: 'Georgia', 'Noto Serif SC', serif;
    letter-spacing: 0.12em; line-height: 1.9;
    text-align: center;
    text-shadow: 0 0 40px rgba(255,255,255,0.08);
}
#hook-cursor {
    display: inline-block; width: 2px; height: 1.1em;
    background: #e8e8e8; margin-left: 4px; vertical-align: middle;
    animation: hookBlink 1s step-end infinite; opacity: 0;
}
body.hook-active #hook-cursor { opacity: 1; }
#hook-hint { color: rgba(255,255,255,0.22); }
#hook-flash { background: #fff; }
@keyframes hookBlink { 0%,100%{opacity:1} 50%{opacity:0} }
""",
        }
        return variants.get(self.art_style, variants["modern_realism"])

    def generate_visual_styles(self):
        """生成视觉特效所需的CSS（居中单角色舞台）"""
        base_css = """
/* ==================== 舞台调度 CSS ==================== */

/* 1. 舞台容器 */
game-screen {
    overflow: hidden;
    position: relative;
}

/* 人物层必须和游戏 UI 位于同一个层叠上下文。
   StageManager 会把该层直接挂到 game-screen（但放在 visuals 外），
   因而场景重绘不会删除它，人物也不会越过文本框。 */
#manual-stage-layer {
    z-index: 5 !important;
}

game-screen [data-component="text-box"] {
    z-index: 10 !important;
}

game-screen [data-component="quick-menu"] {
    z-index: 11 !important;
}

/* 2. 角色基础样式（居中布局）
   人物容器 absolute 定位在铺满 game-screen 的持久层中，尺寸继续使用 vh，
   因此既保持原有构图比例，也不会进入 body 的独立层叠上下文。
   人物层 z-index:5，低于 text-box(10) / quick-menu(11)。
*/
.character-image {
    display: block !important;
    visibility: visible !important;
    height: 100% !important;
    width: auto !important;
    max-width: none !important;
    transition: opacity 0.5s ease;
    will-change: opacity;
    pointer-events: none;
}

/* 立绘容器层级：低于 text-box(z-index:10)，防止 back/reverse 时立绘盖住文本框 */
.manual-character-container {
    z-index: 5 !important;
}

/* ==================== 存档槽位限制 ==================== */
/* 隐藏超过3个的存档槽位 - 使用多种选择器确保生效 */
save-screen [data-save]:nth-child(n+4),
load-screen [data-save]:nth-child(n+4),
save-screen .save-slot:nth-child(n+4),
load-screen .save-slot:nth-child(n+4),
save-screen > *:nth-child(n+4),
load-screen > *:nth-child(n+4) {
    display: none !important;
    visibility: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
    position: absolute !important;
    left: -9999px !important;
}

/* 3. 状态定义
   水平居中由图片自身的 translateX(-50%) 负责，容器只保存站位锚点；
   状态切换只改变透明度和滤镜。 */
.state-active {
    opacity: 1;
    filter: brightness(100%);
    z-index: 20;
}

.state-inactive {
    opacity: 0.7;
    filter: brightness(75%) grayscale(15%);
    z-index: 10;
}

.state-dimmed {
    opacity: 0.6;
    filter: brightness(55%);
    z-index: 5;
}

/* 4. 进场/离场动画（淡入：从下方 30px 浮上） */
.state-hidden {
    opacity: 0;
    transform: translateY(30px);
    pointer-events: none;
}

/* 5. 非游戏场景（主菜单/Save/Load/Settings 等）隐藏立绘。
   现在人物层在 game-screen 内，本规则保留作旧存档/热更新兼容兜底。 */
body:not(.stage-active) .manual-character-container {
    display: none !important;
}

/* 全局滤镜层 */
#cinematic-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    pointer-events: none; z-index: 999; opacity: 0.3;
    mix-blend-mode: overlay; background: #ff9900;
    transition: all 1s ease; display: none;
}
.bg-pan-zoom { animation: ken-burns 20s infinite alternate; }
@keyframes ken-burns { 0% {transform: scale(1);} 100% {transform: scale(1.1) translate(-2%, -1%);} }

/* ==================== 小游戏样式 ==================== */
#qte-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0, 0, 0, 0.85); z-index: 50;
    display: flex; flex-direction: column; justify-content: center; align-items: center;
    opacity: 0; transition: opacity 0.3s; pointer-events: all;
}
.qte-container { text-align: center; color: white; transform: scale(0.9); transition: transform 0.2s; }
.qte-title { font-size: 2rem; margin-bottom: 20px; text-shadow: 0 0 10px #00e5ff; font-family: sans-serif; }
.qte-btn {
    width: 120px; height: 120px; border-radius: 50%;
    background: radial-gradient(circle, #00d2ff, #3a7bd5);
    border: 4px solid #fff; margin: 0 auto;
    display: flex; justify-content: center; align-items: center;
    font-size: 1.5rem; cursor: pointer; box-shadow: 0 0 20px #00d2ff; user-select: none;
    font-weight: bold;
}
.qte-btn:active { transform: scale(0.95); }
.timing-bar-container {
    width: 300px; height: 30px; background: #333; border: 2px solid #fff;
    border-radius: 15px; position: relative; overflow: hidden; margin: 20px auto;
}
.timing-zone { position: absolute; top: 0; height: 100%; background: #00ff00; opacity: 0.6; }
.timing-cursor { position: absolute; top: -2px; height: 100%; width: 4px; background: #fff; left: 0; }
.rune-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 20px; }
.rune-cell {
    width: 60px; height: 60px; border: 2px solid #aaa; background: rgba(255,255,255,0.1);
    cursor: pointer; transition: all 0.2s;
}
.rune-active { background: #00d2ff; box-shadow: 0 0 15px #00d2ff; border-color: #fff; }
.rune-success { background: #4caf50 !important; }
.rune-fail { background: #f44336 !important; }
#custom-toast {
    position: fixed;
    top: 15%; /* 屏幕上方 15% 处 */
    left: 50%;
    transform: translateX(-50%);
    background: rgba(0, 0, 0, 0.7); /* 半透明黑底 */
    color: #fff;
    padding: 12px 24px;
    border-radius: 50px; /* 圆角胶囊状 */
    font-size: 1.1rem;
    font-family: sans-serif;
    z-index: 10000;
    opacity: 0;
    transition: opacity 0.5s ease;
    pointer-events: none; /* 点击穿透 */
    box-shadow: 0 4px 15px rgba(0,0,0,0.3);
    border: 1px solid rgba(255,255,255,0.2);
    text-shadow: 0 1px 2px black;
}

/* ==================== 游戏进度条样式 ==================== */
#game-progress-container {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 4px;
    z-index: 99999;
    background: rgba(255, 255, 255, 0.2);
}

#game-progress-fill {
    height: 100%;
    width: 0%;
    background: #00e5ff;
    box-shadow: 0 0 10px #00e5ff, 0 0 20px rgba(0, 229, 255, 0.5);
    transition: width 0.3s ease;
}

/* ==================== 数值面板 (Stats Panel) ==================== */
main-menu [data-action="help"],
main-menu [data-open="help"],
main-menu button.is-help-button,
main-menu button:has([data-string="Help"]),
main-menu button:has([data-string="help"]) {
    display: none !important;
}
#stats-toggle {
    position: fixed;
    top: 12px;
    right: 12px;
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: rgba(0,0,0,0.55);
    border: 1px solid rgba(255,255,255,0.25);
    color: rgba(255,255,255,0.7);
    font-size: 16px;
    line-height: 32px;
    text-align: center;
    cursor: pointer;
    z-index: 9000;
    user-select: none;
    transition: background 0.2s;
}
#stats-toggle:hover { background: rgba(0,0,0,0.8); }
#stats-panel {
    position: fixed;
    top: 50px;
    right: 12px;
    min-width: 140px;
    background: rgba(0,0,0,0.72);
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 8px;
    padding: 10px 14px;
    z-index: 9000;
    display: none;
    backdrop-filter: blur(8px);
}
#stats-panel.open { display: block; }
body:not(.gameplay-active) #stats-toggle,
body:not(.gameplay-active) #stats-panel {
    display: none !important;
}
#stats-panel .stat-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 4px 0;
    font-size: 0.82rem;
    color: rgba(255,255,255,0.85);
    font-family: monospace;
    letter-spacing: 0.04em;
}
#stats-panel .stat-label { color: rgba(255,255,255,0.5); margin-right: 10px; }
#stats-panel .stat-val { font-weight: bold; color: #fff; }

/* ==================== 序章钩子 (Hook State) ==================== */
"""
        return base_css + self._get_hook_css() + self._get_ui_theme_css() + self._get_system_panel_css() + self._get_nvl_overlay_css() + self._get_cg_overlay_css()

    def _get_system_panel_css(self) -> str:
        """系统按钮 + 全屏系统面板；颜色仅按 UI_mode 分流。"""
        if self.ui_mode == "Ancient":
            accent, panel_bg, border, text_color = (
                "#ead9b7",
                "rgba(7,7,6,0.95)",
                "rgba(183,155,105,0.42)",
                "rgba(238,232,219,0.84)",
            )
            panel_font = "'Source Sans 3 Local', 'PingFang SC', sans-serif"
        else:
            accent, panel_bg, border, text_color = (
                "#f0f0f0",
                "rgba(20,20,24,0.94)",
                "rgba(255,255,255,0.24)",
                "#f0f0f0",
            )
            panel_font = "sans-serif"
        return f"""
/* ==================== 系统按钮 (System Toggle) ==================== */
#system-toggle {{
    position: fixed;
    top: 12px;
    right: 56px;
    height: 32px;
    padding: 0 12px;
    border-radius: 16px;
    background: rgba(0,0,0,0.55);
    border: 1px solid rgba(255,255,255,0.25);
    color: rgba(255,255,255,0.85);
    font-size: 13px;
    line-height: 32px;
    text-align: center;
    cursor: pointer;
    z-index: 9100;
    user-select: none;
    transition: background 0.2s;
    letter-spacing: 0.12em;
}}
#system-toggle:hover {{ background: rgba(0,0,0,0.85); }}

/* ==================== 系统面板 全屏 ==================== */
#system-panel-overlay {{
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: {panel_bg};
    backdrop-filter: blur(10px);
    z-index: 9500;
    display: none;
    color: {text_color};
    font-family: {panel_font};
    padding: 6vh 10vw;
    box-sizing: border-box;
    overflow-y: auto;
}}
#system-panel-overlay.open {{ display: block; }}
#system-panel-overlay .sys-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid {border};
    padding-bottom: 16px;
    margin-bottom: 28px;
    color: {accent};
}}
#system-panel-overlay .sys-title {{
    font-size: 1.6rem;
    letter-spacing: 0.3em;
    font-weight: 600;
}}
#system-panel-overlay .sys-close {{
    width: 36px; height: 36px;
    border-radius: 50%;
    border: 1px solid {border};
    background: transparent;
    color: {accent};
    font-size: 22px;
    cursor: pointer;
    line-height: 32px;
}}
#system-panel-overlay .sys-close:hover {{ background: {border}; color: #fff; }}
#system-panel-overlay .sys-body {{
    font-size: 1.05rem;
    line-height: 2;
    letter-spacing: 0.06em;
    white-space: pre-wrap;
    text-align: left;
    color: {text_color};
}}
#system-panel-overlay .sys-empty {{
    color: rgba(255,255,255,0.4);
    text-align: center;
    margin-top: 20vh;
    font-size: 1rem;
    letter-spacing: 0.2em;
}}
"""

    def _get_nvl_overlay_css(self) -> str:
        """NVL 全屏独白模式：纯黑底 + 白字打字机，文字限定在屏幕 22%~78% 横向区间。"""
        return """
/* ==================== NVL 全屏独白（黑底白字打字机）==================== */
#nvl-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: #000;
    z-index: 9700;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 28vh;
    box-sizing: border-box;
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    cursor: pointer;
    will-change: opacity;
    /* 关键：用 visibility+opacity 切换，避免 display:none 造成的整页重排卡顿。
       关闭时 visibility 延后到淡出结束再切；开启时立即可见。 */
    transition: opacity 0.3s ease, visibility 0s linear 0.3s;
}
#nvl-overlay.open {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
    transition: opacity 0.3s ease;
}
/* 文字带：屏幕左 22% ~ 右 78%（宽 56%，居中），其余区域不出现文字 */
#nvl-overlay .nvl-text {
    width: 56%;
    max-height: 64vh;
    overflow-y: auto;
    color: #fff;
    font-size: clamp(1.2rem, 2.4vw, 1.85rem);
    line-height: 2.0;
    letter-spacing: 0.08em;
    text-align: left;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: 'Noto Serif SC', 'Georgia', serif;
    text-shadow: 0 1px 12px rgba(0, 0, 0, 0.6);
}
#nvl-overlay .nvl-cursor {
    display: inline-block;
    width: 2px;
    height: 1.05em;
    background: #fff;
    margin-left: 3px;
    vertical-align: text-bottom;
    animation: nvlBlink 1s step-end infinite;
}
@keyframes nvlBlink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
/* NVL 激活时隐藏 ADV 对话框/名字/快捷菜单，杜绝淡出瞬间的残留文字 */
body.nvl-active [data-component="text-box"],
body.nvl-active [data-ui="who"],
body.nvl-active [data-component="quick-menu"] {
    opacity: 0 !important;
    pointer-events: none !important;
    transition: opacity 0.2s ease;
}
"""

    def _get_cg_overlay_css(self) -> str:
        """CG 全屏模式：场景大图铺满 + 保留对话框，隐藏立绘和其他 UI。连续 CG 走双层 img 交叉淡入淡出。"""
        return """
/* ==================== CG 全屏模式 ==================== */
#cg-overlay {
    /* 和 text-box 放在同一个 game-screen 层叠上下文中：
       CG 盖住普通场景，但必须位于 text-box(z-index:10) 下方。 */
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    background: #000;
    z-index: 4;
    display: none;
    cursor: pointer;
    overflow: hidden;
}
#cg-overlay.open { display: block; }
#cg-overlay .cg-img {
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    object-fit: cover;
    object-position: center center;
    opacity: 0;
    transition: opacity 0.7s ease;
    will-change: opacity;
    pointer-events: none;
}
#cg-overlay .cg-img.cg-img-current { opacity: 1; }

/* CG 激活时隐藏立绘和辅助 UI；对话框保留（见下方规则） */
body.cg-active [data-component="quick-menu"],
body.cg-active .manual-character-container,
body.cg-active [data-character],
body.cg-active #game-progress-container,
body.cg-active #stats-toggle,
body.cg-active #stats-panel,
body.cg-active #system-toggle,
body.cg-active #system-panel-overlay {
    opacity: 0 !important;
    pointer-events: none !important;
    transition: opacity 0.4s ease;
}

/* 对话框与说话人名浮在 CG 大图之上。使用 !important 防止模板样式覆盖。 */
body.cg-active [data-component="text-box"] {
    z-index: 10 !important;
    opacity: 1 !important;
    visibility: visible !important;
}
"""

    def generate_utility_code(self, chapter_data=None, node_id='', depth=1, include_character_maps=True):
        """生成工具代码（修复版：修复 startMemory 中的语法错误）

        Args:
            chapter_data: 章节数据
            node_id: 节点ID
            depth: 深度
            include_character_maps: 是否包含characterIdMap和characterFileMap声明（默认True）
        """
        print("生成工具代码...")

        import json

        # 获取章节ID - 使用depth而不是chapter_data.get('chapter')，保持与main.js一致
        chap_id = str(depth)

        character_id_mapping = {name: info['id'] for name, info in self.character_map.items() if name != "旁白"}
        character_file_mapping = {info['id']: info['image'] for name, info in self.character_map.items() if
                                  name != "旁白"}

        # Serialize to valid JS objects
        character_id_mapping_js = json.dumps(character_id_mapping, ensure_ascii=False)
        character_file_mapping_js = json.dumps(character_file_mapping, ensure_ascii=False)
        task_id_js = json.dumps(str(self.task_id or ''), ensure_ascii=False)
        chapter_id_js = json.dumps(chap_id, ensure_ascii=False)
        node_id_js = json.dumps(str(node_id or ''), ensure_ascii=False)

        css_content = self.generate_visual_styles()

        lines = []

        # 注入特效库
        lines.append(AtmosphereEffects.generate_effect_library_js())

        # 系统面板开关：全文 JSON 没有任何 system_text 时关闭，前端不渲染按钮和面板
        lines.append(f"window._enableSystemPanel = {str(bool(self._has_system_text)).lower()};")
        lines.append(f"window._uiMode = {json.dumps(self.ui_mode, ensure_ascii=False)};")

        lines.append(f"""
// ==================== 主站退出桥接 ====================
// 仅游戏首页的 ESC / Quit 会发送 GAME_EXIT_REQUEST。
// 游戏过程中与 Settings 内的 ESC 仍完全交给 Monogatari 自身处理。
window.GameHostBridge = {{
    _lastExitAt: 0,
    _isVisible: function(selector) {{
        var element = document.querySelector(selector);
        if (!element || element.hidden) return false;
        var style = window.getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
    }},
    isMainMenuActive: function() {{
        var mainVisible = this._isVisible('main-screen, [data-screen="main"]');
        var gameVisible = this._isVisible('game-screen, [data-screen="game"]');
        var settingsVisible = this._isVisible('settings-screen, [data-screen="settings"]');
        return mainVisible && !gameVisible && !settingsVisible;
    }},
    requestExit: function(source) {{
        var now = Date.now();
        if (now - this._lastExitAt < 350) return false;
        this._lastExitAt = now;
        var data = {{
            protocol_version: 1,
            task_id: {task_id_js},
            chapter_id: {chapter_id_js},
            node_id: {node_id_js},
            source: String(source || 'main_menu'),
            timestamp: now
        }};
        document.dispatchEvent(new CustomEvent('game-exit-request', {{ detail: data }}));
        if (window.parent && window.parent !== window) {{
            window.parent.postMessage({{ type: 'GAME_EXIT_REQUEST', data: data }}, '*');
            console.log('📤 [GameHostBridge] GAME_EXIT_REQUEST', data);
            return true;
        }}
        console.info('[GameHostBridge] standalone mode, exit request emitted locally', data);
        return false;
    }}
}};

document.addEventListener('keydown', function(event) {{
    if (event.key !== 'Escape' || event.repeat || !window.GameHostBridge.isMainMenuActive()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    window.GameHostBridge.requestExit('main_menu_escape');
}}, true);

// ==================== 禁用左右方向键 ====================
document.addEventListener('keydown', function(e) {{
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {{
        e.preventDefault();
        e.stopPropagation();
        console.log('🚫 方向键已被禁用');
    }}
}}, true);

// ==================== 样式注入 ====================
window.addEventListener('load', function() {{
    const customStyles = `{css_content}`;
    const styleSheet = document.createElement("style");
    styleSheet.textContent = customStyles;
    document.head.appendChild(styleSheet);

    if (document.body) {{
        document.body.classList.toggle('ui-mode-ancient', window._uiMode === 'Ancient');
        document.body.classList.toggle('ui-mode-other', window._uiMode !== 'Ancient');
    }}

    const overlayDiv = document.createElement("div");
    overlayDiv.id = "cinematic-overlay";
    if (document.body) document.body.appendChild(overlayDiv);

    // ── 数值面板 DOM ──
    const statsToggle = document.createElement("div");
    statsToggle.id = "stats-toggle";
    statsToggle.title = "Stats";
    statsToggle.textContent = "◈";
    document.body.appendChild(statsToggle);

    const statsPanel = document.createElement("div");
    statsPanel.id = "stats-panel";
    document.body.appendChild(statsPanel);

    statsToggle.addEventListener("click", function() {{
        statsPanel.classList.toggle("open");
        if (window.updateStatsUI) window.updateStatsUI();
    }});

    // 首页不展示数值；Ancient / Other 都统一只在实际游戏画面中展示。
    window.GameplayUiState = {{
        _queued: false,
        hideMainHelp: function() {{
            const menu = document.querySelector('main-menu');
            if (!menu) return;
            Array.from(menu.querySelectorAll('button')).forEach((button) => {{
                const marker = [
                    button.getAttribute('data-action'),
                    button.getAttribute('data-open'),
                    button.getAttribute('data-screen'),
                    button.getAttribute('aria-label'),
                    button.textContent
                ].filter(Boolean).join(' ').trim().toLowerCase();
                if (/(^|\\s)(help|帮助|ヘルプ)(\\s|$)/i.test(marker)) {{
                    button.classList.add('is-help-button');
                    button.hidden = true;
                }}
            }});
        }},
        syncSpeakerNames: function() {{
            document.querySelectorAll('[data-ui="who"]').forEach((who) => {{
                const empty = !String(who.textContent || '').trim();
                if (who.classList.contains('speaker-empty') !== empty) who.classList.toggle('speaker-empty', empty);
                if (who.hidden !== empty) who.hidden = empty;
                const host = who.closest('[data-content="name"]');
                if (host && host.classList.contains('speaker-empty') !== empty) host.classList.toggle('speaker-empty', empty);
            }});
        }},
        centerPromptModals: function() {{
            document.querySelectorAll('alert-modal.modal--active, message-modal.modal--active').forEach((modal) => {{
                const force = (name, value) => modal.style.setProperty(name, value, 'important');
                force('position', 'fixed');
                force('inset', '0');
                force('width', '100vw');
                force('height', '100vh');
                force('margin', '0');
                force('display', 'flex');
                force('align-items', 'center');
                force('justify-content', 'center');
                force('transform', 'none');
                force('z-index', '9800');
                const content = modal.querySelector('.modal__content');
                if (content) {{
                    content.style.setProperty('position', 'fixed', 'important');
                    content.style.setProperty('top', '50vh', 'important');
                    content.style.setProperty('left', '50vw', 'important');
                    content.style.setProperty('margin', '0', 'important');
                    content.style.setProperty('transform', 'translate(-50%, -50%)', 'important');
                }}
            }});
        }},
        sync: function() {{
            this.hideMainHelp();
            this.syncSpeakerNames();
            this.centerPromptModals();
            const gameActive = !!document.querySelector('game-screen.active, [data-screen="game"].active');
            document.body.classList.toggle('gameplay-active', gameActive);
            if (!gameActive) {{
                statsPanel.classList.remove('open');
            }} else if (window._openStatsWhenGameplayResumes) {{
                window._openStatsWhenGameplayResumes = false;
                statsPanel.classList.add('open');
                if (window.updateStatsUI) window.updateStatsUI();
            }}
        }},
        queue: function() {{
            if (this._queued) return;
            this._queued = true;
            requestAnimationFrame(() => {{
                this._queued = false;
                this.sync();
            }});
        }},
        init: function() {{
            this.sync();
            const root = document.getElementById('monogatari') || document.body;
            const observer = new MutationObserver(() => this.queue());
            observer.observe(root, {{
                subtree: true,
                childList: true,
                characterData: true,
                attributes: true,
                attributeFilter: ['class', 'hidden', 'aria-hidden', 'data-screen']
            }});
            document.addEventListener('click', () => this.queue(), true);
        }}
    }};
    window.GameplayUiState.init();

    // ── 系统面板 DOM（全屏）──
    // 当主 JSON 全文不含 system_text 时，跳过创建按钮和面板（window._enableSystemPanel 由生成器写入）
    if (window._enableSystemPanel !== false) {{
    const systemToggle = document.createElement("div");
    systemToggle.id = "system-toggle";
    systemToggle.title = (window.__ui ? window.__ui('system') : 'system');
    systemToggle.textContent = (window.__ui ? window.__ui('system') : 'system');
    document.body.appendChild(systemToggle);

    const systemPanelOverlay = document.createElement("div");
    systemPanelOverlay.id = "system-panel-overlay";
    systemPanelOverlay.innerHTML = `
        <div class="sys-header">
            <span class="sys-title">SYSTEM</span>
            <button class="sys-close" aria-label="close">×</button>
        </div>
        <div class="sys-body"></div>
    `;
    document.body.appendChild(systemPanelOverlay);

    systemToggle.addEventListener("click", function() {{
        systemPanelOverlay.classList.add("open");
        if (window.SystemPanel) window.SystemPanel.render();
    }});
    systemPanelOverlay.querySelector(".sys-close").addEventListener("click", function() {{
        systemPanelOverlay.classList.remove("open");
    }});
    }}
    if (window.refreshGameUiLanguage) window.refreshGameUiLanguage();

    // ── NVL 全屏独白 DOM（黑底白字打字机）──
    const nvlOverlay = document.createElement("div");
    nvlOverlay.id = "nvl-overlay";
    nvlOverlay.innerHTML = `<div class="nvl-text"></div>`;
    document.body.appendChild(nvlOverlay);
    // 点 NVL 任意处：若正在打字则立即补全；否则转发给底层 text-box 推进对话
    nvlOverlay.addEventListener("click", function() {{
        if (window.NvlOverlay && window.NvlOverlay.isTyping()) {{
            window.NvlOverlay.finishTyping();
            return;
        }}
        const tb = document.querySelector('[data-component="text-box"]');
        if (tb) tb.click();
    }});

    // ── CG 全屏 DOM（双层 img 用于交叉淡入淡出）──
    const cgOverlay = document.createElement("div");
    cgOverlay.id = "cg-overlay";
    cgOverlay.innerHTML = `
        <img class="cg-img" data-cg-slot="a" alt="">
        <img class="cg-img" data-cg-slot="b" alt="">
    `;
    // CG 必须与 text-box 位于同一个层叠上下文中，否则 body 直属的高层遮罩
    // 会把 game-screen 内的字幕盖住，子元素再高的 z-index 也无法跨越父级。
    const cgHost = document.querySelector("game-screen") || document.body;
    cgHost.appendChild(cgOverlay);
    // 点 CG 非字幕区域时转发到 text-box（推进对话）
    cgOverlay.addEventListener("click", function() {{
        const tb = document.querySelector('[data-component="text-box"]');
        if (tb) tb.click();
    }});

    // ── Ancient 专属 UI 装饰器：设置页场景封面与最终章节终幕 ──
    if (window._uiMode === 'Ancient') {{
        const endScreen = document.createElement('div');
        endScreen.id = 'ancient-end-screen';
        endScreen.setAttribute('aria-hidden', 'true');
        endScreen.innerHTML = `
            <div class="ancient-end-inner">
                <div class="ancient-end-title">The End</div>
                <div class="ancient-end-stats"></div>
                <div class="ancient-end-hint">Press any key to continue</div>
            </div>
        `;
        document.body.appendChild(endScreen);

        window.AncientUI = {{
            _lastSceneImage: '',

            escape: function(value) {{
                return String(value == null ? '' : value)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#039;');
            }},

            captureScene: function() {{
                const selectors = [
                    'game-screen [data-ui="background"] img',
                    'game-screen [data-scene] img',
                    'game-screen [data-content="visuals"] img[data-scene]',
                    'game-screen [data-content="visuals"] [data-scene] img',
                    'game-screen img[data-scene]'
                ];
                for (const selector of selectors) {{
                    const items = Array.from(document.querySelectorAll(selector));
                    const visible = items.reverse().find((el) => {{
                        const style = getComputedStyle(el);
                        return el.src && style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
                    }});
                    if (visible && visible.src) {{
                        this._lastSceneImage = `url("${{visible.src}}")`;
                        return this._lastSceneImage;
                    }}
                }}

                const backgroundCandidates = [
                    document.querySelector('game-screen [data-ui="background"]'),
                    document.querySelector('game-screen [data-scene]'),
                    document.querySelector('main-screen'),
                    document.querySelector('[data-screen="main"]')
                ].filter(Boolean);
                for (const el of backgroundCandidates) {{
                    const bg = getComputedStyle(el).backgroundImage;
                    if (bg && bg !== 'none') {{
                        this._lastSceneImage = bg;
                        return bg;
                    }}
                }}
                return this._lastSceneImage;
            }},

            applySettingsCover: function() {{
                const settings = document.querySelector('settings-screen');
                if (!settings) return;
                const image = this.captureScene() || this._lastSceneImage;
                if (image) settings.style.setProperty('--ancient-settings-cover', image);
            }},

            setQuickMenuVisible: function(visible) {{
                visible = visible !== false;
                const wasHidden = document.body.classList.contains('ancient-quick-menu-hidden');
                document.body.classList.toggle('ancient-quick-menu-hidden', !visible);
                try {{ localStorage.setItem('VisualNovel_AncientQuickMenuVisible', visible ? '1' : '0'); }} catch (e) {{}}
                const panel = document.getElementById('stats-panel');
                if (!visible) {{
                    window._openStatsWhenGameplayResumes = false;
                    if (panel) panel.classList.remove('open');
                }} else if (wasHidden) {{
                    const gameActive = !!document.querySelector('game-screen.active, [data-screen="game"].active');
                    if (gameActive) {{
                        if (panel) panel.classList.add('open');
                        if (window.updateStatsUI) window.updateStatsUI();
                    }} else {{
                        window._openStatsWhenGameplayResumes = true;
                    }}
                }}
                this.syncSettingsControls();
            }},

            syncSettingsControls: function() {{
                const quickVisible = !document.body.classList.contains('ancient-quick-menu-hidden');
                document.querySelectorAll('[data-ancient-quick]').forEach((button) => {{
                    button.classList.toggle('is-active', button.dataset.ancientQuick === (quickVisible ? 'show' : 'hide'));
                }});
            }},

            refreshSettingsText: function() {{
                const quick = document.querySelector('[data-settings="ancient-quick-menu"] > h3');
                if (quick) quick.textContent = window.__ui('quickMenu');
                const labels = {{
                    show: window.__ui('show'),
                    hide: window.__ui('hide')
                }};
                document.querySelectorAll('[data-ancient-label]').forEach((button) => {{
                    button.textContent = labels[button.dataset.ancientLabel] || button.dataset.ancientLabel;
                }});
                const reset = document.querySelector('.ancient-settings-reset');
                if (reset) reset.textContent = window.__ui('restoreDefaults');
                this.syncSettingsControls();
            }},

            restoreSettingsDefaults: function() {{
                const dispatch = function(input) {{
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }};
                document.querySelectorAll('settings-screen [data-action="set-volume"]').forEach((input) => {{
                    input.value = '0.8';
                    dispatch(input);
                }});
                const textSpeed = document.querySelector('settings-screen [data-action="set-text-speed"]');
                if (textSpeed) {{ textSpeed.value = String(Math.round((Number(textSpeed.min || 1) + Number(textSpeed.max || 50)) / 2)); dispatch(textSpeed); }}
                if (window.VoicePlaybackRateController) window.VoicePlaybackRateController.set(1);
                this.setQuickMenuVisible(true);
                this.syncSettingsControls();
            }},

            decorateSettings: function() {{
                const settings = document.querySelector('settings-screen');
                const row = settings && settings.querySelector(':scope > .row');
                if (!settings || !row) return;

                // Monogatari 默认把设置拆成左右两列；Ancient 模式主动拍平成单列，
                // 避免不同分辨率下重新掉回第二排或左右双栏。
                Array.from(row.querySelectorAll(':scope > .row__column')).forEach((column) => {{
                    Array.from(column.children).forEach((item) => row.appendChild(item));
                    column.remove();
                }});

                if (!row.querySelector('[data-settings="ancient-quick-menu"]')) {{
                    const makeChoice = (kind, options) => {{
                        const item = document.createElement('div');
                        item.setAttribute('data-settings', 'ancient-' + kind);
                        const title = document.createElement('h3');
                        const controls = document.createElement('div');
                        controls.className = 'ancient-settings-choice';
                        options.forEach((option) => {{
                            const button = document.createElement('button');
                            button.type = 'button';
                            button.dataset.ancientLabel = option;
                            button.dataset.ancientQuick = option;
                            controls.appendChild(button);
                        }});
                        item.append(title, controls);
                        return item;
                    }};

                    const quickItem = makeChoice('quick-menu', ['show', 'hide']);
                    row.append(quickItem);
                    quickItem.addEventListener('click', (event) => {{
                        const button = event.target.closest('[data-ancient-quick]');
                        if (!button) return;
                        this.setQuickMenuVisible(button.dataset.ancientQuick === 'show');
                    }});
                }}

                if (!settings.querySelector('.ancient-settings-reset')) {{
                    const reset = document.createElement('button');
                    reset.type = 'button';
                    reset.className = 'ancient-settings-reset';
                    reset.addEventListener('click', () => this.restoreSettingsDefaults());
                    settings.appendChild(reset);
                }}

                this.refreshSettingsText();
            }},

            init: function() {{
                let quickVisible = true;
                try {{ quickVisible = localStorage.getItem('VisualNovel_AncientQuickMenuVisible') !== '0'; }} catch (e) {{}}
                this.setQuickMenuVisible(quickVisible);
                this.captureScene();
                this.applySettingsCover();
                this.decorateSettings();
                let queued = false;
                const refresh = () => {{
                    if (queued) return;
                    queued = true;
                    requestAnimationFrame(() => {{
                        queued = false;
                        this.captureScene();
                        this.applySettingsCover();
                        this.decorateSettings();
                    }});
                }};
                document.addEventListener('click', () => setTimeout(refresh, 0), true);
                const observer = new MutationObserver(refresh);
                const game = document.querySelector('game-screen');
                if (game) observer.observe(game, {{ childList: true, subtree: true, attributes: true, attributeFilter: ['src', 'style', 'data-scene'] }});
            }}
        }};

        window.AncientEndScreen = {{
            _resolve: null,
            _armedAt: 0,
            _finish: function() {{
                if (!this._resolve || Date.now() < this._armedAt) return;
                const resolve = this._resolve;
                this._resolve = null;
                endScreen.classList.remove('open');
                endScreen.setAttribute('aria-hidden', 'true');
                document.removeEventListener('keydown', this._keyHandler, true);
                // 终幕继续属于用户主动结束，允许现有 AntiEnd 防护放行一次 end。
                window._userQuitFlag = true;
                resolve(true);
            }},
            _keyHandler: function(event) {{
                if (event.key === 'Tab' || event.key === 'Shift' || event.key === 'Control' || event.key === 'Alt' || event.key === 'Meta') return;
                event.preventDefault();
                window.AncientEndScreen._finish();
            }},
            show: function() {{
                if (window._uiMode !== 'Ancient') return Promise.resolve(true);
                const vars = window._playerVariables || {{}};
                const stats = endScreen.querySelector('.ancient-end-stats');
                const title = endScreen.querySelector('.ancient-end-title');
                const hint = endScreen.querySelector('.ancient-end-hint');
                const lang = String(window._lang || document.documentElement.lang || '').toLowerCase();
                const zh = lang.indexOf('zh') === 0;
                if (title) title.textContent = zh ? '终 章' : 'The End';
                if (hint) hint.textContent = zh ? '点击或按任意键继续' : 'Press any key to continue';
                if (stats) {{
                    stats.innerHTML = Object.entries(vars).map(([key, value]) => {{
                        const icon = window.getStatIcon ? window.getStatIcon(key) : '✧';
                        return `<div class="ancient-end-stat"><span>${{icon}}</span><span>${{window.AncientUI.escape(key)}}</span><strong>${{window.AncientUI.escape(value)}}</strong></div>`;
                    }}).join('');
                }}
                endScreen.classList.add('open');
                endScreen.setAttribute('aria-hidden', 'false');
                this._armedAt = Date.now() + 350;
                document.addEventListener('keydown', this._keyHandler, true);
                return new Promise((resolve) => {{ this._resolve = resolve; }});
            }}
        }};

        endScreen.addEventListener('click', () => window.AncientEndScreen._finish());
        window.AncientUI.init();
    }}
}});

// ==================== 系统面板控制器 (SystemPanel) ====================
window.SystemPanel = {{
    _text: "",
    setText: function(text) {{
        this._text = (text == null) ? "" : String(text);
        // 如果面板正打开就立即刷新
        const overlay = document.getElementById('system-panel-overlay');
        if (overlay && overlay.classList.contains('open')) {{
            this.render();
        }}
    }},
    getText: function() {{ return this._text; }},
    render: function() {{
        const overlay = document.getElementById('system-panel-overlay');
        if (!overlay) return;
        const body = overlay.querySelector('.sys-body');
        if (!body) return;
        const t = this._text;
        if (!t || !t.trim()) {{
            body.innerHTML = '<div class="sys-empty">— No system messages —</div>';
        }} else {{
            // 1) 先转义 HTML 防 XSS；2) 把 \\n 显式替换为 <br>，连续多个 \\n 当段间留白
            const escaped = String(t)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/\\r\\n/g, '\\n')
                .replace(/\\n{{2,}}/g, '<br><br>')
                .replace(/\\n/g, '<br>');
            body.innerHTML = '<div>' + escaped + '</div>';
        }}
    }}
}};

// ==================== NVL 全屏独白控制器 (NvlOverlay) ====================
// 黑底白字打字机。show(text) 打开并逐字显示；hide() 关闭。
// 用 body.nvl-active + overlay.open 切 visibility/opacity（不碰 display，切换零卡顿）。
window.NvlOverlay = {{
    _typeTimer: null,
    _fullText: "",
    _typing: false,

    show: function(text) {{
        const overlay = document.getElementById('nvl-overlay');
        if (!overlay) return;
        const tEl = overlay.querySelector('.nvl-text');
        this._fullText = (text == null) ? "" : String(text);

        // 先切到激活态（ADV 对话框由 CSS 隐藏），overlay 立即淡入
        document.body.classList.add('nvl-active');
        overlay.classList.add('open');

        // 打字机：清空后逐字追加，末尾挂闪烁光标
        if (this._typeTimer) {{ clearTimeout(this._typeTimer); this._typeTimer = null; }}
        if (!tEl) return;
        tEl.textContent = '';
        const chars = [...this._fullText];
        let i = 0;
        this._typing = true;
        const speed = Math.max(18, Math.min(45, 1600 / Math.max(1, chars.length)));
        const type = () => {{
            if (i < chars.length) {{
                tEl.textContent += chars[i++];
                this._typeTimer = setTimeout(type, speed);
            }} else {{
                this._typing = false;
                this._typeTimer = null;
                this._appendCursor(tEl);
            }}
        }};
        this._typeTimer = setTimeout(type, 120);
    }},

    _appendCursor: function(tEl) {{
        if (!tEl || tEl.querySelector('.nvl-cursor')) return;
        const cur = document.createElement('span');
        cur.className = 'nvl-cursor';
        tEl.appendChild(cur);
    }},

    // 点击时若还在打字：立即补全全文
    finishTyping: function() {{
        if (!this._typing) return;
        if (this._typeTimer) {{ clearTimeout(this._typeTimer); this._typeTimer = null; }}
        this._typing = false;
        const overlay = document.getElementById('nvl-overlay');
        const tEl = overlay && overlay.querySelector('.nvl-text');
        if (tEl) {{ tEl.textContent = this._fullText; this._appendCursor(tEl); }}
    }},

    isTyping: function() {{ return !!this._typing; }},

    hide: function() {{
        const overlay = document.getElementById('nvl-overlay');
        if (this._typeTimer) {{ clearTimeout(this._typeTimer); this._typeTimer = null; }}
        this._typing = false;
        document.body.classList.remove('nvl-active');
        if (!overlay) return;
        overlay.classList.remove('open');
        const tEl = overlay.querySelector('.nvl-text');
        if (tEl) tEl.textContent = '';
    }},

    isOpen: function() {{
        const overlay = document.getElementById('nvl-overlay');
        return !!(overlay && overlay.classList.contains('open'));
    }}
}};

// ==================== CG 全屏控制器 (CgOverlay) ====================
// setMode(active, imageFile) 唯一入口：
//   active=false: 关闭 CG 模式，UI 恢复
//   active=true:  开启 CG 模式；连续 active 但换图时走双层 img 交叉淡入淡出
window.CgOverlay = {{
    _activeSlot: null,         // 'a' / 'b' / null —— 当前显示在前景的图层
    _currentFile: '',          // 当前已加载的图片文件名

    _getSlots: function() {{
        const overlay = document.getElementById('cg-overlay');
        if (!overlay) return null;
        return {{
            overlay: overlay,
            a: overlay.querySelector('[data-cg-slot="a"]'),
            b: overlay.querySelector('[data-cg-slot="b"]'),
        }};
    }},

    setMode: function(active, imageFile) {{
        const slots = this._getSlots();
        if (!slots) return;
        const file = imageFile || '';

        if (!active) {{
            // 关闭 CG 模式
            if (document.body.classList.contains('cg-active')) {{
                document.body.classList.remove('cg-active');
                slots.overlay.classList.remove('open');
                slots.a.classList.remove('cg-img-current');
                slots.b.classList.remove('cg-img-current');
                this._activeSlot = null;
                this._currentFile = '';
            }}
            return;
        }}

        // 开启 / 维持 CG 模式
        document.body.classList.add('cg-active');
        slots.overlay.classList.add('open');

        // 同一张图：什么都不做，避免重复触发淡入
        if (file && file === this._currentFile && this._activeSlot) return;

        // 首x进入 CG 或切换图片：用空闲图层加载新图，交叉淡入
        const nextSlotKey = (this._activeSlot === 'a') ? 'b' : 'a';
        const nextImg = slots[nextSlotKey];
        const prevImg = (this._activeSlot === 'a') ? slots.a : (this._activeSlot === 'b' ? slots.b : null);

        if (!file) {{
            // 没有图片就保持纯黑底（已经显示），只切状态
            this._activeSlot = nextSlotKey;
            this._currentFile = '';
            return;
        }}

        const onReady = () => {{
            // 双 rAF 确保浏览器已应用 src，再触发 transition
            requestAnimationFrame(() => {{
                requestAnimationFrame(() => {{
                    nextImg.classList.add('cg-img-current');
                    if (prevImg && prevImg !== nextImg) {{
                        prevImg.classList.remove('cg-img-current');
                    }}
                }});
            }});
        }};

        // 设置 src 并等待加载（已缓存时立即触发）
        nextImg.onload = onReady;
        nextImg.onerror = onReady;
        nextImg.src = 'assets/scenes/' + file;
        // 部分缓存命中场景 onload 不再触发，兜底直接 ready
        if (nextImg.complete && nextImg.naturalWidth > 0) {{
            onReady();
        }}

        this._activeSlot = nextSlotKey;
        this._currentFile = file;
    }},

    isActive: function() {{
        return document.body.classList.contains('cg-active');
    }}
}};

// ==================== 环境底音播放器 (AmbiencePlayer) ====================
// 独立于 Monogatari 媒体系统的自建 <audio loop>，与 BGM(music)/SFX(sound) 三条声道
// 完全隔离：'stop music' / 'stop sound' 都不会影响它，可三者同时播放。
window.AmbiencePlayer = {{
    _audio: null,
    _currentFile: '',
    _volume: 0.35,   // 环境底音默认音量（低于 BGM，作为背景垫底）

    _ensure: function() {{
        if (this._audio) return this._audio;
        const a = new Audio();
        a.loop = true;
        a.preload = 'auto';
        a.volume = this._volume;
        this._audio = a;
        // 首次用户交互时解锁自动播放（浏览器 autoplay 限制）
        const unlock = () => {{
            if (this._audio && this._audio.paused && this._audio.src) {{
                this._audio.play().catch(() => {{}});
            }}
        }};
        document.addEventListener('click', unlock, {{ once: false }});
        return a;
    }},

    // file: 环境底音文件名（如 'xxx.wav'）；同一文件重复调用不打断
    play: function(file) {{
        if (!file) return;
        const a = this._ensure();
        if (file === this._currentFile && !a.paused) return;
        this._currentFile = file;
        // 复用 Monogatari 的资源根路径：本地为 assets，生产环境由 options.js 指向 OSS/CDN。
        // ambience 仍是独立新增目录，不需要在构建阶段做额外字符串替换。
        let assetRoot = 'assets';
        try {{ assetRoot = monogatari.setting('AssetsPath').root || assetRoot; }} catch(e) {{}}
        a.src = String(assetRoot).replace(/\/+$/, '') + '/ambience/' + file;
        a.volume = this._volume;
        const p = a.play();
        if (p && p.catch) p.catch(() => {{}});   // autoplay 被拦截时静默，等首次点击解锁
    }},

    // 绝对状态同步：file 为空表示此刻不该有底音。
    // 每个 beat 的 Apply/Reverse 都调用，因此 back 回退、进度条 seek、读档都能自愈。
    sync: function(file) {{
        if (!file) {{ if (this._currentFile) this.stop(); return; }}
        this.play(file);
    }},

    stop: function() {{
        if (!this._audio) return;
        try {{ this._audio.pause(); this._audio.currentTime = 0; }} catch(e) {{}}
        this._audio.removeAttribute('src');
        this._currentFile = '';
    }},

    setVolume: function(v) {{
        this._volume = Math.max(0, Math.min(1, v));
        if (this._audio) this._audio.volume = this._volume;
    }},

    isPlaying: function() {{
        return !!(this._audio && !this._audio.paused && this._audio.src);
    }}
}};

// ==================== 数值同步与UI ====================
window._syncPlayerVars = function() {{
    try {{
        if (monogatari && monogatari.storage) {{
            const s = monogatari.storage();
            s.playerVars = Object.assign({{}}, window._playerVariables || {{}});
            s.playerVariableChoices = JSON.parse(JSON.stringify(window._playerVariableChoices || {{}}));
            s.playerLegacyFlags = Object.assign({{}}, window._playerLegacyFlags || {{}});
            monogatari.storage(s);
        }}
    }} catch(e) {{}}
    try {{
        localStorage.setItem('_playerVarsCurrent', JSON.stringify(window._playerVariables || {{}}));
        localStorage.setItem('_playerVariableChoicesCurrent', JSON.stringify(window._playerVariableChoices || {{}}));
        localStorage.setItem('_playerLegacyFlagsCurrent', JSON.stringify(window._playerLegacyFlags || {{}}));
    }} catch(e) {{}}
}};

window.getStatIcon = function(key) {{
    const raw = String(key == null ? '' : key);
    const normalized = raw.toLowerCase();
    if (/(好感|亲密|羁绊|爱意|affection|favor|favour|bond|love|romance)/i.test(normalized)) return '♡';
    if (/(命运|天命|宿命|fate|destiny)/i.test(normalized)) return '✦';
    if (/(莲|lotus)/i.test(normalized)) return '❋';
    if (/(勇气|武力|战力|courage|power|strength|battle)/i.test(normalized)) return '⚔';
    if (/(智慧|谋略|学识|wisdom|mind|intellect)/i.test(normalized)) return '◇';
    const icons = ['✧', '❖', '☾', '❀', '◇', '⌁'];
    let hash = 0;
    for (let i = 0; i < raw.length; i++) hash = ((hash << 5) - hash + raw.charCodeAt(i)) | 0;
    return icons[Math.abs(hash) % icons.length];
}};

window._escapeStatText = function(value) {{
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}};

window.updateStatsUI = function() {{
    const panel = document.getElementById('stats-panel');
    if (!panel) return;
    const vars = window._playerVariables || {{}};
    panel.innerHTML = Object.entries(vars).map(([k, v]) => {{
        const icon = window.getStatIcon(k);
        return `<div class="stat-row"><span class="stat-icon" aria-hidden="true">${{icon}}</span><span class="stat-val">${{window._escapeStatText(v)}}</span><span class="stat-label">${{window._escapeStatText(k)}}</span></div>`;
    }}).join('');
}};

// ==================== 试玩章节限制(非iframe只开放前两章) ====================
// iframe 模式交父页 CHAPTER_PERMIT 控制;独立打开时 depth>2 拦截。
// depth 从目标 URL 里的 chapter_{{depth}}_ 解析(parseInt 遇非数字自动停)。
window._canEnterStandalone = function(url) {{
    if (window.parent && window.parent !== window) return true;
    var depth = 1;
    var mk = (url || '').indexOf('chapter_');
    if (mk >= 0) {{
        depth = parseInt(url.slice(mk + 8), 10) || 1;
    }}
    if (depth > 2) {{
        if (window.showToast) window.showToast('试玩仅开放前两章,敬请期待完整版');
        else alert('试玩仅开放前两章,敬请期待完整版');
        return false;
    }}
    return true;
}};

// ==================== 路由导航器 ====================
window.StoryNavigator = {{
    pendingJumpUrl: null,  // 保存待跳转的 URL

    withLanguage: function(targetUrl) {{
        try {{
            const parsed = new URL(targetUrl, window.location.origin);
            const lang = window._lang || window._defaultLang || '';
            if (lang) parsed.searchParams.set('lang', lang);
            return parsed.origin === window.location.origin
                ? parsed.pathname + parsed.search + parsed.hash
                : parsed.toString();
        }} catch (e) {{
            const sep = String(targetUrl).includes('?') ? '&' : '?';
            return String(targetUrl) + sep + 'lang=' + encodeURIComponent(window._lang || window._defaultLang || '');
        }}
    }},

    request: function(targetUrl, targetNodeId) {{
        targetUrl = this.withLanguage(targetUrl);
        // 保存目标 URL
        this.pendingJumpUrl = targetUrl;

        // 发送跳转申请
        const requestMsg = {{
            "type": "CHAPTER_REQUEST",
            "data": {{
                "target_node_id": targetNodeId || '',
                "target_url": targetUrl,
                "timestamp": Date.now()
            }}
        }};
        console.log("📤 [Iframe] 申请跳转:", requestMsg);

        if (window.parent && window.parent !== window) {{
            // Iframe 模式:发送消息并等待回信
            window.parent.postMessage(requestMsg, '*');
        }} else {{
            // 独立调试模式:直接跳转(试玩限制:非iframe仅前两章)
            if (!window._canEnterStandalone(targetUrl)) return;
            console.log("🔧 [Debug] 独立模式直接跳转");
            window.location.href = targetUrl;
        }}
    }},

    go: function(targetFolderName) {{
        const targetUrl = "/" + targetFolderName + "/";
        this.request(targetUrl, targetFolderName);
    }}
}};

// ==================== 监听跳转许可 ====================
window.addEventListener('message', function(event) {{
    if (event.data && event.data.type === "CHAPTER_PERMIT") {{
        try {{
            console.log("📥 [Iframe] 收到跳转许可");
            const permit = event.data.data;

            // 检查是否允许跳转
            if (permit && permit.allowed === true) {{
                // 获取之前保存的目标 URL
                const targetUrl = window.StoryNavigator.pendingJumpUrl;

                if (targetUrl) {{
                    window.showToast("正在前往...");
                    const jumpUrl = targetUrl.includes('?')
                        ? targetUrl + '&_t=' + Date.now()
                        : targetUrl + '?_t=' + Date.now();

                    setTimeout(() => {{ window.location.href = jumpUrl; }}, 300);
                }}
            }} else {{
                console.log("❌ 跳转被拒绝");
                window.showToast("无权访问此章节");
            }}
        }} catch (e) {{
            console.error("❌ 跳转失败:", e);
        }}
    }}
}});

// ==================== 进度条跳转 ====================
window.addEventListener('message', function(event) {{
    if (!event.data || event.data.type !== 'SEEK_TO_BEAT') return;
    const beatIndex = event.data.data && event.data.data.beat_index;
    if (beatIndex == null || !window._materialIndices) return;
    const indices = window._materialIndices;
    let target = indices[0];
    for (let i = 0; i < indices.length; i++) {{
        if (indices[i] <= beatIndex) target = indices[i];
        else break;
    }}
    const doJump = () => {{
        try {{ monogatari.run(`jump Material_${{target}}`); }}
        catch (e) {{ console.error('[SEEK_TO_BEAT] jump failed', e); }}
    }};
    if (!monogatari.global('playing')) {{
        monogatari.global('playing', true);
        monogatari.onStart().then(() => {{
            monogatari.element().find('[data-screen]').each((screen) => {{ screen.setState({{ open: false }}); }});
            monogatari.element().find('[data-screen="game"]').get(0).setState({{ open: true }});
            doJump();
        }});
    }} else {{
        doJump();
    }}
}});

// ==================== 保存/加载管理器 (修复版:直接读取LocalStorage) ====================
window.SaveLoadManager = {{
    // 发送所有存档信息到 parent
    sendSavesToParent: function() {{
        try {{
            const saveData = [];

            // 方法1: 直接从 LocalStorage 读取所有存档
            for (let i = 0; i < localStorage.length; i++) {{
                const key = localStorage.key(i);
                // 匹配 Monogatari 存档键名格式
                if (key && (key.includes('Save') || key.includes('save') ||
                           key.includes('Slot') || key.includes('slot'))) {{
                    try {{
                        const value = localStorage.getItem(key);
                        const saveInfo = JSON.parse(value);

                        console.log('📦 [SaveLoadManager] 找到存档:', key, saveInfo);

                        saveData.push({{
                            slot: key,
                            label: saveInfo.label || key,
                            gameState: saveInfo.data || saveInfo,
                            screenshot: saveInfo.screenshot || '',
                            timestamp: saveInfo.date || Date.now()
                        }});
                    }} catch (parseError) {{
                        console.warn('⚠️ 解析存档失败:', key, parseError);
                    }}
                }}
            }}

            console.log('📊 [SaveLoadManager] 共找到', saveData.length, '个存档');

            // 发送存档数据
            const saveMessage = {{
                type: "SAVE_DATA_UPDATE",
                data: {{
                    chapter_id: "{chap_id}",
                    node_id: "{node_id}",
                    saves: saveData,
                    timestamp: Date.now()
                }}
            }};

            if (window.parent && window.parent !== window) {{
                window.parent.postMessage(saveMessage, '*');
                console.log("📤 [SaveLoadManager] 存档同步完毕，发送数据:", saveMessage);

                // 同时触发自动进度保存
                if (window.GameProgressReporter) {{
                    window.GameProgressReporter.sendReport('manual_save');
                }}
            }}
        }} catch (e) {{
            console.error("❌ 发送存档信息失败:", e);
        }}
    }},

    // 监听加载请求
    listenForLoad: function() {{
        window.addEventListener('message', function(event) {{
            if (event.data && event.data.type === "LOAD_SAVE_DATA") {{
                try {{
                    console.log("📥 [SaveLoadManager] 收到云端存档数据:", event.data);
                    const saveData = event.data.data;

                    // 如果数据为空或没有存档,跳过加载
                    if (!saveData || !saveData.saves || saveData.saves.length === 0) {{
                        console.log("⚠️ 没有存档数据");
                        return;
                    }}

                    // 等待 Monogatari 初始化完成
                    const loadSaves = () => {{
                        if (!monogatari || !monogatari.storage) {{
                            console.log("⏳ Monogatari 未初始化，等待...");
                            setTimeout(loadSaves, 100);
                            return;
                        }}

                        const storage = monogatari.storage();
                        console.log("📦 当前 storage 状态:", storage);

                        if (!storage.saves) storage.saves = {{}};

                        console.log("📦 开始加载存档，共 " + saveData.saves.length + " 个");

                        // [关键修复]同时写入 localStorage 和 storage 对象
                        for (const save of saveData.saves) {{
                            if (save.slot && save.gameState) {{
                                console.log("  - 加载存档槽位:", save.slot);
                                console.log("  - 存档数据:", save.gameState);

                                //  1. 写入 localStorage(Monogatari 从这里读取)
                                try {{
                                    localStorage.setItem(save.slot, JSON.stringify(save.gameState));
                                    console.log("  ✅ 存档已写入 localStorage:", save.slot);
                                }} catch (e) {{
                                    console.error("  ❌ 写入 localStorage 失败:", e);
                                }}

                                //  2. 同时更新 storage 对象(Load 界面从这里读取)
                                storage.saves[save.slot] = save.gameState;
                                console.log("  ✅ 存档已更新到 storage.saves:", save.slot);
                            }}
                        }}

                        //  3. 保存回 storage
                        monogatari.storage(storage);

                        //  4. 从存档恢复 _playerVariables
                        if (storage.playerVars) {{
                            window._playerVariables = Object.assign({{}}, storage.playerVars);
                            if (window.updateStatsUI) window.updateStatsUI();
                            console.log("📊 [SaveLoadManager] 数值已从存档恢复:", window._playerVariables);
                        }}
                        if (storage.playerVariableChoices) {{
                            window._playerVariableChoices = JSON.parse(JSON.stringify(storage.playerVariableChoices));
                            console.log("📊 [SaveLoadManager] 倾向统计已从存档恢复:", window._playerVariableChoices);
                        }}
                        if (storage.playerLegacyFlags) {{
                            window._playerLegacyFlags = Object.assign({{}}, storage.playerLegacyFlags);
                            console.log("📊 [SaveLoadManager] 遗产事件已从存档恢复:", window._playerLegacyFlags);
                        }}

                        console.log("✅ 云端存档已写入 localStorage 和 storage");

                        // 验证写入结果
                        const savedKeys = Object.keys(localStorage).filter(k => k.includes('Save'));
                        console.log("📦 localStorage 中的存档 keys:", savedKeys);
                        console.log("📦 storage.saves 中的存档 keys:", Object.keys(storage.saves));

                        // [关键]强制刷新 Load 界面
                        // 方法:触发 Monogatari 的内部事件,让它重新读取存档
                        setTimeout(() => {{
                            // 触发 storage 变化事件
                            const storageEvent = new Event('storage');
                            window.dispatchEvent(storageEvent);
                            console.log("🔄 已触发 storage 事件，Load 界面应该会刷新");
                        }}, 100);

                        window.showToast("存档已同步");
                    }};

                    loadSaves();
                }} catch (e) {{
                    console.error("❌ 加载存档失败:", e);
                    console.error("错误堆栈:", e.stack);
                }}
            }}
        }});
    }},

    init: function() {{
        this.listenForLoad();
        console.log("SaveLoadManager initialized (Passive Mode)");
    }}
}};

// ==================== 进度管理器 ====================
window.ProgressManager = {{
    // 请求初始进度
    requestInitialProgress: function() {{
        if (window.parent && window.parent !== window) {{
            const requestMsg = {{
                type: "REQUEST_INITIAL_PROGRESS",
                data: {{
                    chapter_id: "{chap_id}",
                    node_id: "{node_id}",
                    timestamp: Date.now()
                }}
            }};
            window.parent.postMessage(requestMsg, '*');
            console.log("📤 请求初始进度:", requestMsg);
        }}
    }},

    // 监听初始进度返回
    listenForInitialProgress: function() {{
        window.addEventListener('message', function(event) {{
            if (event.data && event.data.type === "INITIAL_PROGRESS") {{
                try {{
                    console.log("📥 收到初始进度:", event.data);
                    const progressData = event.data.data;

                    // 如果有进度数据,更新游戏进度报告器
                    if (progressData && progressData.current_index && window.GameProgressReporter) {{
                        window.GameProgressReporter.current = progressData.current_index;
                        window.GameProgressReporter.update(progressData.current_index);
                        console.log("✅ 进度已恢复:", progressData);
                    }}
                    if (progressData && progressData.player_variables &&
                            Object.keys(progressData.player_variables).length > 0) {{
                        window._playerVariables = Object.assign({{}}, progressData.player_variables);
                        if (window._syncPlayerVars) window._syncPlayerVars();
                        if (window.updateStatsUI) window.updateStatsUI();
                    }}
                    if (progressData && progressData.player_variable_choices) {{
                        window._playerVariableChoices = JSON.parse(JSON.stringify(progressData.player_variable_choices));
                        if (window._syncPlayerVars) window._syncPlayerVars();
                    }}
                    if (progressData && progressData.player_legacy_flags) {{
                        window._playerLegacyFlags = Object.assign({{}}, progressData.player_legacy_flags);
                        if (window._syncPlayerVars) window._syncPlayerVars();
                    }}
                    if (progressData && progressData.system_text && window.SystemPanel) {{
                        window.SystemPanel.setText(progressData.system_text);
                    }}
                }} catch (e) {{
                    console.error("处理初始进度失败:", e);
                }}
            }}
        }});
    }},

    // 请求存档数据
    requestSaveData: function() {{
        if (window.parent && window.parent !== window) {{
            const requestMsg = {{
                type: "REQUEST_SAVE_DATA",
                data: {{
                    chapter_id: "{chap_id}",
                    node_id: "{node_id}",
                    timestamp: Date.now()
                }}
            }};
            window.parent.postMessage(requestMsg, '*');
            console.log("📤 请求存档数据:", requestMsg);
        }}
    }},

    init: function() {{
        this.listenForInitialProgress();
        // 页面加载时先请求进度
        this.requestInitialProgress();
        // 延迟500ms后请求存档(确保进度请求先处理)
        setTimeout(() => {{
            this.requestSaveData();
        }}, 500);
        console.log("ProgressManager initialized");
    }}
}};

// ==================== 游戏进度上报系统 ====================
window.GameProgressReporter = {{
    total: 0,
    current: 0,
    chapId: '',
    nodeId: '',
    autoSaveTimer: null,

    init: function(total, chapId, nodeId) {{
        this.total = total;
        this.current = 0;
        this.chapId = chapId;
        this.nodeId = nodeId;
        this._disableReport = false; // 初始化进度上报开关

        // 检查并创建进度条 DOM
        if (!document.getElementById('game-progress-container')) {{
            const container = document.createElement('div');
            container.id = 'game-progress-container';

            const fill = document.createElement('div');
            fill.id = 'game-progress-fill';

            container.appendChild(fill);
            document.body.appendChild(container);
        }}

        console.log('Progress Reporter Initialized:', {{ total, chapId, nodeId }});
    }},

    update: function(index) {{
        // [修复]限制index范围,防止父端发来错误数据
        const safeIndex = Math.max(0, Math.min(this.total, index));
        this.current = safeIndex;
        const pct = (safeIndex / this.total) * 100;
        const fillEl = document.getElementById('game-progress-fill');
        if (fillEl) {{
            fillEl.style.width = pct + '%';
        }}
    }},

    sendReport: function(triggerSource) {{
        // [还原]移除了熔断机制,允许玩家正常快进
        if (this.total === 0) {{ return; }}
        if (this._disableReport) {{ return; }}

        // [修复]确保current不超过total,防止发送错误数据给父端
        const safeCurrent = Math.max(0, Math.min(this.total, this.current));
        const safePercent = Math.min(100, parseFloat(((safeCurrent / this.total) * 100).toFixed(2)));

        const msg = {{
            "type": "GAME_PROGRESS_UPDATE",
            "data": {{
                "chapter_id": this.chapId,
                "node_id": this.nodeId,
                "progress_percent": safePercent,
                "current_index": safeCurrent,
                "total_count": this.total,
                "timestamp": Date.now(),
                "trigger_source": triggerSource,
                "player_variables": window._playerVariables || {{}},
                "player_variable_choices": window._playerVariableChoices || {{}},
                "player_legacy_flags": window._playerLegacyFlags || {{}},
                "branch_state": (window.BranchProgress && window.BranchProgress.get()) || null,
                "system_text": (window.SystemPanel && window.SystemPanel.getText()) || ""
            }}
        }};

        // 限制日志输出频率,避免控制台刷屏,但不阻止逻辑
        if (triggerSource !== 'material_click') {{
             console.log('Progress Sent:', triggerSource, msg);
        }}

        window.parent.postMessage(msg, '*');
    }},

    startAutoSave: function() {{
        // 每30s自动上报进度
        if (this.autoSaveTimer) {{
            clearInterval(this.autoSaveTimer);
        }}
        this.autoSaveTimer = setInterval(() => {{
            this.sendReport('auto_save');
        }}, 30000);

        // 监听页面关闭/刷新
        window.addEventListener('beforeunload', () => {{
            this.sendReport('user_exit');
        }});

        console.log('Auto-report started: 30s interval');
    }}
}};

// ==================== 错误上报系统 ====================
window.ErrorReporter = {{
    chapId: '',
    nodeId: '',

    init: function(chapId, nodeId) {{
        this.chapId = chapId;
        this.nodeId = nodeId;

        // 捕获全局错误
        window.addEventListener('error', (event) => {{
            this.reportError({{
                type: 'runtime_error',
                message: event.message,
                filename: event.filename,
                lineno: event.lineno,
                colno: event.colno
            }});
        }});

        // 捕获未处理的 Promise rejection
        window.addEventListener('unhandledrejection', (event) => {{
            this.reportError({{
                type: 'promise_rejection',
                message: event.reason ? event.reason.toString() : 'Unknown error'
            }});
        }});

        console.log('ErrorReporter initialized for:', chapId, nodeId);
    }},

    reportError: function(errorInfo) {{
        const msg = {{
            "type": "ERROR_REPORT",
            "data": {{
                "chapter_id": this.chapId,
                "node_id": this.nodeId,
                "error": errorInfo,
                "timestamp": Date.now(),
                "userAgent": navigator.userAgent
            }}
        }};

        if (window.parent && window.parent !== window) {{
            window.parent.postMessage(msg, '*');
            console.error('❌ Error reported to parent:', msg);
        }} else {{
            console.error('❌ Error (standalone mode):', errorInfo);
        }}
    }}
}};

// ==================== 自定义提示系统 (新增) ====================
window.showToast = function(text) {{
    // 如果已存在,先移除
    const old = document.getElementById('custom-toast');
    if (old) old.remove();

    const toast = document.createElement('div');
    toast.id = 'custom-toast';
    toast.innerText = text;
    document.body.appendChild(toast);

    // 强制重绘触发 transition
    toast.offsetHeight; 

    // 显示
    toast.style.opacity = '1';

    // after 2s, hide
    setTimeout(() => {{
        if (toast) {{
            toast.style.opacity = '0';
            setTimeout(() => {{ if(toast.parentNode) toast.remove(); }}, 500);
        }}
    }}, 2000);
}};
// ==================== 小游戏引擎 ====================

window.MiniGame = {{
    activeResolve: null, // [核心修改]用于存储当前正在进行的 Promise 句柄

    createOverlay: function(html) {{
        const overlay = document.createElement('div');
        overlay.id = 'qte-overlay';

        // 右上角强制关闭按钮
        const closeBtn = document.createElement('div');
        closeBtn.innerHTML = '×';
        closeBtn.style.cssText = 'position:absolute; top:20px; right:20px; color:white; font-size:40px; cursor:pointer; z-index:100; opacity:0.6;';
        closeBtn.onmouseover = () => closeBtn.style.opacity = '1';
        closeBtn.onmouseout = () => closeBtn.style.opacity = '0.6';
        closeBtn.onclick = () => {{
            this.cleanup(); // 点击关闭也触发清理
        }};

        overlay.innerHTML = html;
        overlay.appendChild(closeBtn);

        document.body.appendChild(overlay);
        setTimeout(() => overlay.style.opacity = '1', 50);
        return overlay;
    }},

    closeOverlay: function(overlay, result, resolve) {{
        overlay.style.opacity = '0';
        setTimeout(() => {{ 
            if(overlay) overlay.remove(); 
            resolve(result); 
            this.activeResolve = null; // [核心修改]正常结束,清空句柄
        }}, 300);
    }},

    // ================= [核心修改]强制清理并解锁引擎 =================
    cleanup: function() {{
        //  1. 移除 UI
        const overlay = document.getElementById('qte-overlay');
        if (overlay) overlay.remove();

        const toast = document.getElementById('custom-toast');
        if (toast) toast.remove();

        //  2. [关键]强制解决 Promise,解锁引擎卡死状态
        if (this.activeResolve) {{
            console.log("强制结束小游戏 Promise，解锁引擎");
            this.activeResolve(true); // 视为通过,让剧情继续,解开死锁
            this.activeResolve = null;
        }}
    }},
    // ==============================================================

    //  1. 连点游戏
    startClicker: function(target=8, time=3, label="Burst!") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // [记录]保存句柄

            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">Rapid Click Challenge</div>
                    <div class="qte-btn" id="qte-btn">${{label}}</div>
                    <div style="margin-top:10px; font-size:1.5rem" id="qte-count">0 / ${{target}}</div>
                </div>`);

            let clicks = 0;
            const btn = overlay.querySelector('#qte-btn');
            const count = overlay.querySelector('#qte-count');

            btn.onclick = (e) => {{
                e.preventDefault(); clicks++;
                count.innerText = `${{clicks}} / ${{target}}`;
                btn.style.transform = 'scale(0.95)';
                setTimeout(()=>btn.style.transform='scale(1)', 50);
                if(clicks >= target) this.closeOverlay(overlay, true, resolve);
            }};
            setTimeout(() => {{ if(document.body.contains(overlay)) this.closeOverlay(overlay, false, resolve); }}, time * 1000);
        }});
    }},

    //  2. 精准判定
    startTiming: function(speed=2, difficulty=20, label="Perfect Strike") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // [记录]保存句柄

            const zoneLeft = 20 + Math.random() * 50;
            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">${{label}}</div>
                    <div class="timing-bar-container">
                        <div class="timing-zone" style="left:${{zoneLeft}}%; width:${{difficulty}}%"></div>
                        <div class="timing-cursor" id="t-cursor"></div>
                    </div>
                    <div class="qte-btn" id="t-btn" style="width:80px;height:80px;font-size:1rem">Lock</div>
                </div>`);

            const cursor = overlay.querySelector('#t-cursor');
            let pos = 0, dir = 1, active = true;

            function anim() {{
                if(!active) return;
                pos += dir * speed;
                if(pos >= 100 || pos <= 0) dir *= -1;
                cursor.style.left = pos + '%';
                requestAnimationFrame(anim);
            }}
            requestAnimationFrame(anim);

            overlay.querySelector('#t-btn').onclick = () => {{
                active = false;
                const success = (pos >= zoneLeft && pos <= zoneLeft + difficulty);
                const btn = overlay.querySelector('#t-btn');
                btn.style.background = success ? '#4caf50' : '#f44336';
                btn.innerText = success ? 'Success' : 'Failed';
                setTimeout(() => this.closeOverlay(overlay, success, resolve), 800);
            }};
        }});
    }},

    //  3. 记忆阵法
    startMemory: function(count=4, label="Memory Challenge") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // [记录]保存句柄

            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">${{label}}</div>
                    <div class="rune-grid" id="rune-grid">
                        ${{Array(9).fill(0).map((_,i)=>`<div class="rune-cell" data-i="${{i}}"></div>`).join('')}}
                    </div>
                    <div id="mem-msg" style="margin-top:20px">Watch the sequence...</div>
                </div>`);

            const cells = overlay.querySelectorAll('.rune-cell');
            let seq = [], userSeq = [];
            while(seq.length < count) {{
                let r = Math.floor(Math.random()*9);
                if(seq.length===0 || seq[seq.length-1]!==r) seq.push(r);
            }}

            const playSeq = async () => {{
                for(let i of seq) {{
                    if (!this.activeResolve) return; // 如果中途退出了,停止动画
                    await new Promise(r=>setTimeout(r, 500));
                    if (!this.activeResolve) return; // 再x检查
                    cells[i].classList.add('rune-active');
                    await new Promise(r=>setTimeout(r, 300));
                    cells[i].classList.remove('rune-active');
                }}
                if (overlay.querySelector('#mem-msg')) overlay.querySelector('#mem-msg').innerText = "Click in order";
            }};
            setTimeout(playSeq, 500);

            cells.forEach(c => c.onclick = () => {{
                if(overlay.querySelector('#mem-msg').innerText !== "Click in order") return;
                const idx = parseInt(c.dataset.i);
                c.classList.add('rune-active');
                setTimeout(()=>c.classList.remove('rune-active'), 100);

                if(idx === seq[userSeq.length]) {{
                    userSeq.push(idx);
                    if(userSeq.length === count) this.closeOverlay(overlay, true, resolve);
                }} else {{
                    c.classList.add('rune-fail');
                    this.closeOverlay(overlay, false, resolve);
                }}
            }});
        }});
    }}
}};
""")

        # 只在需要时添加characterIdMap和characterFileMap声明
        if include_character_maps:
            lines.append(f"""
const characterIdMap = {character_id_mapping_js};
const characterFileMap = {character_file_mapping_js};
""")

        lines.append("""
// ==================== 核心:舞台管理器 (StageManager) ====================
// 多槽位舞台：按角色 key 维护一组立绘，setCast 做增/删/移位 diff。
// 同槽位同角色保持（不重新淡入）；消失的角色直接移除；新增的角色淡入入场。
const StageManager = {
    _screenObserver: null,
    // key -> { container, img, stem, slot, leftPct }
    _cast: {},

    // 监听 game-screen 的可见性，给 body 加/去 .stage-active。
    // 人物层现已位于 game-screen 内；该状态继续保留用于旧存档兼容。
    _observeScreen() {
        if (this._screenObserver) return;
        const sync = () => {
            const gs = document.querySelector('game-screen');
            if (!gs) { document.body.classList.remove('stage-active'); return; }
            const visible = (gs.style.display !== 'none') &&
                            (gs.offsetParent !== null || gs.hasAttribute('open'));
            document.body.classList.toggle('stage-active', visible);
        };
        sync();
        const gs = document.querySelector('game-screen');
        if (!gs) { setTimeout(() => this._observeScreen(), 200); return; }
        this._screenObserver = new MutationObserver(sync);
        this._screenObserver.observe(gs, { attributes: true, attributeFilter: ['style', 'open', 'class'] });
        document.querySelectorAll('[data-screen]').forEach(el => {
            this._screenObserver.observe(el, { attributes: true, attributeFilter: ['style', 'open', 'class'] });
        });
    },

    _layer() {
        // 持久层直接挂在 game-screen、但不放进 visuals：
        // 'show scene' 只重渲染 visuals，不会删除该层；同时人物和文本框
        // 处于同一个层叠上下文，避免 body 跨层叠上下文时偶发盖住文本框。
        const screen = document.querySelector('game-screen');
        if (!screen) return null;
        let layer = document.getElementById('manual-stage-layer');
        if (!layer) {
            layer = document.createElement('div');
            layer.id = 'manual-stage-layer';
            layer.style.position = 'absolute';
            layer.style.top = '0';
            layer.style.left = '0';
            layer.style.width = '100%';
            layer.style.height = '100%';
            layer.style.overflow = 'hidden';
            layer.style.pointerEvents = 'none';
            layer.style.zIndex = '5';   // 低于 text-box(z-index:10)
        }
        // 兼容旧存档/热更新留下的 body 直属人物层：发现后移回 game-screen。
        if (layer.parentElement !== screen) screen.appendChild(layer);
        return layer;
    },

    reset() {
        this._cast = {};
        document.querySelectorAll('.manual-character-container').forEach(el => el.remove());
        const visuals = document.querySelector('game-screen [data-content="visuals"]');
        if (visuals) {
            visuals.querySelectorAll('[data-character]').forEach(el => el.remove());
        }
        this._observeScreen();
    },

    _createSlot(entry) {
        // entry: { key, stem, slot, leftPct }
        const layer = this._layer();
        if (!layer) { console.error('[StageManager] 找不到立绘层'); return null; }

        const container = document.createElement('div');
        container.className = 'manual-character-container';
        // 不使用 Monogatari 原生的 data-character 属性。
        // `show scene` 会全局删除所有 [data-character]，如果手动立绘也使用该
        // 属性，就会在同一 beat 的场景切换命令执行时被立即移除。
        container.setAttribute('data-manual-character', entry.key);
        container.style.position = 'absolute';
        // 立绘框定：图总高 142.857vh，可见中间 70%(=100vh 填满屏幕)；
        // 上边出框 5%(=-7.143vh)，下边出框 25%(=35.714vh)，宽度按比例自适应。
        container.style.top = '-7.143vh';
        container.style.left = entry.leftPct + '%';
        container.style.height = '142.857vh';
        container.style.pointerEvents = 'none';
        container.style.zIndex = '5';   // 低于 text-box(z-index:10)，立绘不会盖住文本框
        container.style.transition = 'left 0.4s ease, opacity 0.5s ease';

        const img = document.createElement('img');
        img.className = 'character-image';
        img.style.height = '100%';
        img.style.width = 'auto';
        // entry.leftPct 表示人物中心轴所在位置。容器本身是零宽锚点，
        // 因此必须平移图片自身；若平移容器，零宽度会导致 -50% 等于 0。
        img.style.transform = 'translateX(-50%)';
        img.style.transformOrigin = 'center center';
        img.style.opacity = '0';
        img.style.transition = 'opacity 0.9s ease';   // 淡入稍长
        // 图片加载完成后再淡入，避免坏图/未加载时的空白闪烁
        img.onload = () => { img.style.opacity = '1'; };
        img.onerror = () => { console.warn('[StageManager] 立绘加载失败:', img.src); };
        img.src = `assets/characters/${entry.stem}.png`;
        // 兜底：若 onload 因缓存未触发，下一帧也尝试淡入
        requestAnimationFrame(() => { if (img.complete && img.naturalWidth > 0) img.style.opacity = '1'; });
        container.appendChild(img);

        layer.appendChild(container);

        const rec = { container, img, stem: entry.stem, slot: entry.slot, leftPct: entry.leftPct };
        this._cast[entry.key] = rec;
        return rec;
    },

    _removeKey(key) {
        const rec = this._cast[key];
        if (!rec) return;
        delete this._cast[key];
        if (rec.container) rec.container.remove();
    },

    // list: [{ stem, slot, leftPct, key }]
    setCast(list) {
        this._observeScreen();
        list = Array.isArray(list) ? list : [];
        const nextByKey = {};
        list.forEach(e => { if (e && e.key) nextByKey[e.key] = e; });

        // 1. 退场：当前在场但 next 没有的 key
        Object.keys(this._cast).forEach(key => {
            if (!(key in nextByKey)) this._removeKey(key);
        });

        // 2. 保持 / 移位 / 换图 / 新增
        list.forEach(e => {
            if (!e || !e.key) return;
            const rec = this._cast[e.key];
            // 不存在，或缓存的 DOM 已脱离文档（被 show scene 清掉等）→ 重建
            if (!rec || !rec.container || !document.body.contains(rec.container)) {
                if (rec && rec.container) rec.container.remove();
                this._createSlot(e);
                return;
            }
            // 同角色保持：slot 变化平滑移位
            if (rec.leftPct !== e.leftPct) {
                rec.leftPct = e.leftPct;
                rec.slot = e.slot;
                if (rec.container) rec.container.style.left = e.leftPct + '%';
            }
            // png 变化：换图并重新走 onload 淡入（避免坏图/换图后空白不刷新）
            if (rec.stem !== e.stem) {
                rec.stem = e.stem;
                const img = rec.img;
                if (img) {
                    img.style.transition = 'opacity 0.9s ease';   // 淡入稍长
                    img.style.opacity = '0';
                    img.onload = () => { img.style.opacity = '1'; };
                    img.src = `assets/characters/${e.stem}.png`;
                    requestAnimationFrame(() => { if (img.complete && img.naturalWidth > 0) img.style.opacity = '1'; });
                }
            }
        });
    }
};

// 兼容旧调用：保留空壳，避免历史脚本/存档回放报错
function updateStage(speakerName) { return true; }


// ==================== 场景转场控制器 (SceneFX) ====================
// 换场景时用 0.3s 黑幕遮挡：cover() 淡入黑幕（await 完成后再换场景+立绘），reveal() 淡出黑幕。
const SceneFX = {
    _overlay: null,
    _ensure() {
        if (this._overlay && document.body.contains(this._overlay)) return this._overlay;
        let el = document.getElementById('scene-fade-overlay');
        if (!el) {
            el = document.createElement('div');
            el.id = 'scene-fade-overlay';
            el.style.position = 'fixed';
            el.style.top = '0';
            el.style.left = '0';
            el.style.width = '100%';
            el.style.height = '100%';
            el.style.background = '#000';
            el.style.opacity = '0';
            el.style.pointerEvents = 'none';
            el.style.zIndex = '50';   // 高于背景(0)/立绘(10)，低于文本框与菜单
            el.style.transition = 'opacity 0.3s ease';
            document.body.appendChild(el);
        }
        this._overlay = el;
        return el;
    },
    cover() {
        const el = this._ensure();
        return new Promise(resolve => {
            requestAnimationFrame(() => { el.style.opacity = '1'; });
            setTimeout(resolve, 320);   // 略大于 0.3s，确保完全遮住
        });
    },
    reveal() {
        const el = this._ensure();
        requestAnimationFrame(() => { el.style.opacity = '0'; });
        return Promise.resolve();
    },
    // 兜底：立即清掉黑幕（用于 Reverse / reset）
    clear() {
        const el = this._ensure();
        el.style.transition = 'none';
        el.style.opacity = '0';
        requestAnimationFrame(() => { el.style.transition = 'opacity 0.3s ease'; });
        return true;
    }
};


function setFilter(color = null, opacity = 0.3) {
    const overlay = document.getElementById('cinematic-overlay');
    if (!overlay) return;
    if (!color) { overlay.style.display = 'none'; }
    else {
        overlay.style.display = 'block';
        overlay.style.background = color;
        overlay.style.opacity = opacity;
    }
    return Promise.resolve();
}
function startBackgroundMotion() {
    setTimeout(() => {
        const bg = document.querySelector('#background');
        if (bg) bg.classList.add('bg-pan-zoom');
    }, 100);
    return Promise.resolve();
}

// ==================== 序章钩子管理器 (HookManager) ====================
window.HookManager = {
    active: false,
    _typeTimer: null,
    _paraCount: 0,          // 当前屏已追加的段落数
    _PAGE_SIZE: 5,          // 每屏最多显示几段后清空

    _ensureDom: function() {
        if (!document.getElementById('hook-overlay')) {
            const overlay = document.createElement('div');
            overlay.id = 'hook-overlay';
            const content = document.createElement('div');
            content.id = 'hook-text-content';
            const hint = document.createElement('div');
            hint.id = 'hook-hint';
            hint.textContent = 'click to continue';
            overlay.appendChild(content);
            overlay.appendChild(hint);
            document.body.appendChild(overlay);
        }
        if (!document.getElementById('hook-flash')) {
            const flash = document.createElement('div');
            flash.id = 'hook-flash';
            document.body.appendChild(flash);
        }
    },

    enter: function() {
        if (this.active) return Promise.resolve();
        this.active = true;
        this._paraCount = 0;
        this._ensureDom();
        document.body.classList.remove('hook-done', 'hook-exiting');
        document.body.classList.add('hook-active');
        // overlay 点击转发到 text-box(text-box 被 CSS 隐藏但仍可响应 .click())
        const overlay = document.getElementById('hook-overlay');
        if (overlay && !overlay._clickBound) {
            overlay._clickBound = true;
            function _unlockAudio() {
                document.querySelectorAll('audio').forEach(a => {
                    if (a.paused && a.src) a.play().catch(() => {});
                });
            }
            overlay.addEventListener('click', function() {
                // 首x点击解锁浏览器 autoplay
                _unlockAudio();
                const tb = document.querySelector('[data-component="text-box"]');
                if (tb) tb.click();
            });
            // 空格/回车也解锁 autoplay(一x性)
            function _keyUnlock(e) {
                if (e.key === ' ' || e.key === 'Enter') {
                    _unlockAudio();
                    document.removeEventListener('keydown', _keyUnlock, true);
                }
            }
            document.addEventListener('keydown', _keyUnlock, true);
        }
        return new Promise(resolve => setTimeout(resolve, 650));
    },

    setText: function(text) {
        this._ensureDom();
        if (this._typeTimer) { clearTimeout(this._typeTimer); this._typeTimer = null; }
        const container = document.getElementById('hook-text-content');
        if (!container) return Promise.resolve();

        // 移除旧光标
        const oldCursor = document.getElementById('hook-cursor');
        if (oldCursor) oldCursor.remove();

        // 每 _PAGE_SIZE 段清屏一x(fade out 再清)
        if (this._paraCount > 0 && this._paraCount % this._PAGE_SIZE === 0) {
            container.style.opacity = '0';
            setTimeout(() => {
                container.innerHTML = '';
                container.style.opacity = '1';
                this._appendPara(container, text);
            }, 400);
        } else {
            this._appendPara(container, text);
        }
        this._paraCount++;
        return Promise.resolve();
    },

    _appendPara: function(container, text) {
        const p = document.createElement('p');
        container.appendChild(p);
        // 逐字打印到这 <p>
        const chars = [...text];
        let i = 0;
        const speed = Math.max(15, Math.min(35, 1400 / chars.length));
        const type = () => {
            if (i < chars.length) {
                p.textContent += chars[i++];
                this._typeTimer = setTimeout(type, speed);
            } else {
                // 打印完毕,追加光标到末尾
                const cursor = document.createElement('span');
                cursor.id = 'hook-cursor';
                p.appendChild(cursor);
            }
        };
        setTimeout(type, 80);
    },

    exit: function() {
        return new Promise(resolve => {
            if (this._typeTimer) { clearTimeout(this._typeTimer); this._typeTimer = null; }
            const flash = document.getElementById('hook-flash');
            if (flash) {
                flash.style.opacity = '1';
                setTimeout(() => { flash.style.opacity = '0'; }, 120);
            }
            document.body.classList.remove('hook-active');
            document.body.classList.add('hook-exiting');
            setTimeout(() => {
                document.body.classList.remove('hook-exiting');
                document.body.classList.add('hook-done');
                this.active = false;
                resolve(true);
            }, 1200);
        });
    }
};
""")
        return lines

    # ==================== 旧版多章模式方法（已废弃，不再使用） ====================
    # 注意：以下方法已被 create_per_node_projects() 替代
    # 保留代码仅供参考，实际运行中不会被调用

    # def create_project(self):
    #     """创建完整项目（支持多章输出）"""
    #     print("=" * 60)
    #     print("创建视觉小说项目 - 多章分镜版")
    #     print("=" * 60)
    #
    #     try:
    #         # 0. 加载角色信息
    #         self.load_character_info()
    #
    #         # 1. 扫描素材
    #         self.scan_assets()
    #
    #         # 2. 组织素材并分章
    #         scenes = self.organize_materials()
    #
    #         # 3. 确定章列表（排除'all'键）
    #         chapter_keys = [key for key in scenes.keys() if key.startswith('chapter')]
    #
    #         if not chapter_keys:
    #             print("警告：未找到任何章，将使用'all'键创建单章��目")
    #             chapter_keys = ['all']
    #
    #         print(f"将创建 {len(chapter_keys)} 个游戏文件")
    #
    #         # 保存原始输出目录
    #         original_output_dir = self.output_dir
    #
    #         # 4. 为每一章创建独立游戏项目
    #         for chap_key in chapter_keys:
    #             chapter_data = scenes[chap_key]
    #             chap_id = chapter_data['chapter'] if chap_key != 'all' else 'all'
    #
    #             print(f"\n--- 创建第 {chap_id} 章游戏项目 ---")
    #
    #             # 设置本章输出目录
    #             if chap_key == 'all':
    #                 chapter_output_dir = original_output_dir
    #             else:
    #                 chapter_output_dir = original_output_dir / f"chapter{chap_id}"
    #
    #             self.output_dir = chapter_output_dir
    #
    #             # 复制模板（如果目录不存在）
    #             if not self.output_dir.exists():
    #                 self.copy_template()
    #             else:
    #                 print(f"输出目录已存在: {self.output_dir}")
    #                 # 仍然需要确保必要的目录存在
    #
    #             # 复制素材（过滤出本章的音频和字幕）
    #             self.copy_assets_for_chapter(chapter_data)
    #
    #             # 生成脚本文件
    #             script_file = self.output_dir / "js" / "script.js"
    #
    #             script_content = "/* global monogatari */\n\n"
    #             script_content += self.generate_character_definitions()
    #             script_content += self.generate_asset_declarations()
    #             script_content += "\n".join(self.generate_utility_code())
    #             script_content += self.generate_script_for_chapter(chapter_data)
    #
    #             with open(script_file, 'w', encoding='utf-8') as f:
    #                 f.write(script_content)
    #
    #             # 更新main.js
    #             main_js_content = """'use strict';
    #         /* global Monogatari */
    #
    #         const { $_ready, $_ } = Monogatari;
    #
    #         $_ready (() => {
    #             // 拦截 localStorage 来限制存档数量
    #             const originalSetItem = localStorage.setItem.bind(localStorage);
    #             localStorage.setItem = function(key, value) {
    #                 // 检查是否是保存游戏存档(包含 _Save_ 或 _AutoSave_)
    #                 if (key.includes('_Save_') || key.includes('_AutoSave_')) {
    #                     // 获取所有存档
    #                     const saveKeys = Object.keys(localStorage).filter(k => k.includes('_Save_') || k.includes('_AutoSave_'));
    #
    #                     // 如果已有3存档且当前key不在已有存档中,则阻止保存
    #                     if (saveKeys.length >= 3 && !saveKeys.includes(key)) {
    #
    #
    #                         // 关键:强制刷新 save-screen 显示
    #                         setTimeout(() => {
    #                             const saveScreen = document.querySelector('save-screen');
    #                             if (saveScreen) {
    #                                 // 方法1:触发重新渲染
    #                                 const parent = saveScreen.parentNode;
    #                                 const clone = saveScreen.cloneNode(true);
    #                                 parent.replaceChild(clone, saveScreen);
    #
    #                                 // 方法2:如果有刷新方法,调用它
    #                                 if (clone.refresh) {
    #                                     clone.refresh();
    #                                 }
    #                             }
    #                         }, 100);
    #
    #                         throw new Error('Save limit reached: Maximum 3 slots allowed.');
    #                     }
    #                 }
    #                 return originalSetItem(key, value);
    #             };
    #
    #             // 页面加载完成后初始化游戏
    #             monogatari.init ('#monogatari').then (() => {
    #                 console.log('视觉小说已启动 - 音频字幕同步版');
    #
    #                 // 初始化保存/加载管理器
    #                 if (window.SaveLoadManager) {
    #                     window.SaveLoadManager.init();
    #                 }
    #
    #                 // 初始化进度管理器
    #                 if (window.ProgressManager) {
    #                     window.ProgressManager.init();
    #                 }
    #
    #                 // 设置音频自动播放
    #                 monogatari.storage({
    #                     settings: {
    #                         autoAdvance: false,  // 关闭自动前进,确保click命令有效
    #                         voiceVolume: 100,
    #                         musicVolume: 15,     // <--- 在这里修改数值 (0-100),建议 30-50 之间
    #                         skipVoice: true  // 允许跳过音频
    #                     }
    #                 });
    #
    #                 // 监听来自 parent 的初始进度消息
    #                 window.addEventListener('message', function(event) {
    #                     if (event.data && event.data.type === "INITIAL_PROGRESS") {
    #                         const progressData = event.data.data;
    #                         console.log("📥 收到初始进度:", progressData);
    #
    #                         // 修改开始按钮文本
    #                         const startButton = document.querySelector('[data-action="start"]');
    #                         if (startButton) {
    #                             // 如果没有进度数据或 current_index 为空,默认显示"开始"
    #                             if (!progressData || !progressData.current_index || progressData.current_index === 1) {
    #                                 startButton.textContent = "开始";
    #                             } else {
    #                                 startButton.textContent = "继续";
    #                                 // 保存进度信息,用于点击后跳转
    #                                 window._resumeIndex = progressData.current_index;
    #                             }
    #                         }
    #                     }
    #                 });
    #
    #                 // 请求初始进度
    #                 if (window.parent && window.parent !== window) {
    #                     window.parent.postMessage({
    #                         type: "REQUEST_INITIAL_PROGRESS",
    #                         data: { timestamp: Date.now() }
    #                     }, '*');
    #                 }
    #
    #                 // 检查是否从跳转加载(通过URL参数或sessionStorage)
    #                 const urlParams = new URLSearchParams(window.location.search);
    #                 const fromJump = urlParams.get('autostart') === '1' || sessionStorage.getItem('autostart') === '1';
    #
    #                 if (fromJump) {
    #                     // 清除标记
    #                     sessionStorage.removeItem('autostart');
    #
    #                     // 延迟自动点击开始按钮
    #                     setTimeout(() => {
    #                         const startButton = document.querySelector('[data-action="start"]');
    #                         if (startButton) {
    #                             startButton.click();
    #                             console.log('Auto-started game from jump');
    #                         }
    #                     }, 100);
    #                 }
    #             });
    #         });
    #         """
    #             main_js_file = self.output_dir / "js" / "main.js"
    #
    #             # 【重要修改】直接覆盖写入，不要判断文件是否存在，确保音量修改立即生效
    #             with open(main_js_file, 'w', encoding='utf-8') as f:
    #                 f.write(main_js_content)
    #
    #             # 创建运行脚本（如果不存在）
    #             self.create_run_scripts()
    #
    #             print(f"第 {chap_id} 章创建完成: {self.output_dir}")
    #
    #         # 恢复原始输出目录
    #         self.output_dir = original_output_dir
    #
    #         print("\n" + "=" * 60)
    #         print("多章项目创建完成！")
    #         print(f"总输出目录: {original_output_dir}")
    #         print("")
    #         print("运行游戏:")
    #         print(f"  每章独立运行:")
    #         for chap_key in chapter_keys:
    #             if chap_key == 'all':
    #                 print(f"  cd {original_output_dir}")
    #             else:
    #                 chap_id = scenes[chap_key]['chapter']
    #                 print(f"  cd {original_output_dir}/chapter{chap_id}")
    #             print("    ./run.sh  (Linux/Mac)")
    #             print("    run.bat   (Windows)")
    #             print("    浏览器访问: http://localhost:3000")
    #             print("")
    #         print("特性:")
    #         print("  ✓ 基于音频、字幕、图片素材")
    #         print("  ✓ 音频字幕同步播放")
    #         print("  ✓ 角色图片自动显示")
    #         print("  ✓ 支持多种字幕格式")
    #         print("  ✓ 旁白不显示角色")
    #         print("  ✓ 点击跳过音频")
    #         print("  ✓ 多章分镜，每章独立游戏文件")
    #         print("=" * 60)
    #
    #     except Exception as e:
    #         print(f"创建项目时出错: {e}")
    #         import traceback
    #         traceback.print_exc()

    def create_per_node_projects(self):
        """为每个节点创建独立的项目（基于story_tree.json逻辑）"""
        print("=" * 60)
        print("创建视觉小说项目 - 基于story_tree.json的章节重命名")
        print("=" * 60)

        try:
            # 0. 加载story_tree.json
            # 使用脚本文件所在位置计算路径
            script_dir = Path(__file__).resolve().parent
            project_root = script_dir.parent.parent  # 从 services/game_builder 向上两级到项目根目录
            story_tree_path = self.source_dir / "story_tree.json"
            if not story_tree_path.exists():
                print(f"错误：找不到story_tree.json文件: {story_tree_path}")
                return

            print(f"加载story_tree.json: {story_tree_path}")
            with open(story_tree_path, 'r', encoding='utf-8') as f:
                story_tree = json.load(f)

            root_id = story_tree.get('root_id', 'chapter_1')
            nodes = story_tree.get('nodes', {})
            hook_id = story_tree.get('hook_id', None)  # 序章钩子截止 beat 索引，仅根节点生效

            # 从 story_tree.json 读取 art_style（由 story_planner 写入）
            art_style = story_tree.get('art_style', self.art_style) or self.art_style
            print(f"  art_style: {art_style}")

            # UI 与画面 art_style 解耦：Ancient 使用本轮古风界面，其余暂走 Other。
            ui_mode = story_tree.get('UI_mode', story_tree.get('ui_mode', self.ui_mode))
            ui_mode = "Ancient" if str(ui_mode).strip().lower() == "ancient" else "Other"
            self.ui_mode = ui_mode
            print(f"  UI_mode: {ui_mode}")

            # 从 story_tree.json 读取 book_language（用于 speaker_id → 显示名）
            book_language = story_tree.get('book_language', self.book_language) or self.book_language
            print(f"  book_language: {book_language}")

            if not nodes:
                print("错误：story_tree.json中没有节点数据")
                return

            print(f"找到 {len(nodes)} 个节点，根节点ID: {root_id}")

            # 全局扫描主 JSON：是否存在任何 system_text（决定是否注入 system 按钮 UI）
            has_system_text_anywhere = False
            try:
                _, master_data = self._find_master_json()
                if master_data:
                    for chap_beats in master_data.values():
                        if not isinstance(chap_beats, list):
                            continue
                        for b in chap_beats:
                            st = b.get('system_text') if isinstance(b, dict) else None
                            if st is not None and str(st).strip() != "":
                                has_system_text_anywhere = True
                                break
                        if has_system_text_anywhere:
                            break
            except Exception as e:
                print(f"⚠️ 扫描 system_text 失败，按存在处理: {e}")
                has_system_text_anywhere = True
            print(f"  全局是否含 system_text: {has_system_text_anywhere}")
            self._has_system_text = has_system_text_anywhere

            # 1. 预计算映射表：node_id -> 文件夹名（chapter_{depth}_{完整8位uuid}）
            folder_mapping = {}
            for node_id, node_data in nodes.items():
                depth = node_data.get('depth', 1)
                # 提取8位uuid：如果是node_xxxxxxxx格式则取后8位，否则直接用node_id
                if node_id.startswith('node_'):
                    uuid_suffix = node_id[5:]  # 去掉"node_"前缀
                else:
                    uuid_suffix = node_id  # 如chapter_1这样的直接使用
                folder_name = f"chapter_{depth}_{uuid_suffix}"
                folder_mapping[node_id] = folder_name
                print(f"  映射: {node_id} -> {folder_name}")

            # 2. 加载角色信息（共享）
            self.load_character_info()

            # 3. 遍历所有节点，为每个节点创建项目
            for node_id, node_data in nodes.items():
                folder_name = folder_mapping[node_id]
                depth = node_data.get('depth', 1)  # 获取章节编号
                print(f"\n处理节点: {node_id} -> 输出文件夹: {folder_name}")

                # 确定源目录：outputs-temp/{node_id}
                source_dir = self.source_dir / node_id
                if not source_dir.exists():
                    print(f"  警告：源目录不存在，跳过: {source_dir}")
                    continue

                # 为每个节点创建独立的转换器实例
                node_converter = FinalConverter(
                    source_dir=str(source_dir),
                    template_dir=self.template_dir,
                    output_dir=Path(self.output_dir) / folder_name,
                    images_base_dir=self.images_base_dir,
                    art_style=art_style,
                    delete_local_assets=self.delete_local_assets,
                    task_id=self.task_id,
                    enable_oss_upload=self.enable_oss_upload,
                    book_language=book_language,
                    ui_mode=ui_mode
                )

                # 复制当前实例的角色信息
                node_converter.pinyin_to_chinese = self.pinyin_to_chinese.copy()
                node_converter.chinese_to_id = self.chinese_to_id.copy()
                node_converter.chinese_to_pinyin = self.chinese_to_pinyin.copy()

                # 【关键】复制特效配置（从父实例继承，因为atmophere.json在根目录）
                node_converter.atmosphere_config = self.atmosphere_config

                # 下发：主 JSON 是否含 system_text（决定是否注入 system 按钮 UI）
                node_converter._has_system_text = self._has_system_text

                # 扫描该节点的素材
                node_converter.scan_assets()

                # 组织素材
                scenes = node_converter.organize_materials()

                # 创建项目
                if scenes:
                    # 复制模板
                    node_converter.copy_template()

                    # 获取章节数据
                    chapter_data = scenes.get('all', {})

                    # 先复制素材（根据音频文件名推断需要的场景），获取实际复制的资源列表
                    copied_assets = node_converter.copy_assets_for_chapter(chapter_data, node_id)

                    # 生成脚本文件（传递node_data和folder_mapping，使用实际复制的资源）
                    script_file = node_converter.output_dir / "js" / "script.js"

                    script_content = "/* global monogatari */\n\n"
                    script_content += node_converter.generate_character_definitions()
                    script_content += node_converter.generate_asset_declarations(copied_assets)  # 传入实际复制的资源
                    script_content += "\n".join(node_converter.generate_utility_code(chapter_data, node_id, depth))
                    # 调用修改后的generate_script_for_chapter，传入node_data和folder_mapping
                    # hook_id 只对根节点（第一章）生效
                    node_hook_id = hook_id if node_id == root_id else None
                    script_content += node_converter.generate_script_for_chapter(
                        chapter_data, node_data,
                        folder_mapping,
                        game_project=self.output_dir.name,
                        hook_id=node_hook_id,
                        player_variables=story_tree.get('player_variables', {}),
                        branch_thresholds=story_tree.get('branch_thresholds', {}),
                        is_root_node=(node_id == root_id)
                    )

                    with open(script_file, 'w', encoding='utf-8') as f:
                        f.write(script_content)

                    # 更新main.js（使用字符串格式化直接写入chapter_id和node_id）
                    # 注意：所有工具代码（SaveLoadManager、GameProgressReporter、StageManager等）已经在script.js中声明
                    # main.js只包含页面初始化逻辑，不重复声明这些管理器

                    main_js_content = """'use strict';
                /* global Monogatari */

                const {{ $_ready, $_ }} = Monogatari;

                $_ready (() => {{
                    // [Back按钮修复]初始化会话起始索引
                    window._sessionStartIndex = 1;

                    //  1. 请求数据
                    if (window.parent && window.parent !== window) {{
                        window.parent.postMessage({{
                            type: "REQUEST_INITIAL_PROGRESS",
                            data: {{ chapter_id: "{chapter_id}", node_id: "{node_id}", timestamp: Date.now() }}
                        }}, '*');
                        setTimeout(() => {{
                            window.parent.postMessage({{
                                type: "REQUEST_SAVE_DATA",
                                data: {{ chapter_id: "{chapter_id}", node_id: "{node_id}", timestamp: Date.now() }}
                            }}, '*');
                        }}, 50);
                    }}

                    //  2. LocalStorage 拦截
                    const originalSetItem = localStorage.setItem.bind(localStorage);
                    localStorage.setItem = function(key, value) {{
                        if (key.includes('_Save_') || key.includes('_AutoSave_')) {{
                            const saveKeys = Object.keys(localStorage).filter(k => k.includes('_Save_') || k.includes('_AutoSave_'));
                            if (saveKeys.length >= 3 && !saveKeys.includes(key)) {{
                                alert('Save slots are full (max 3).\\nPlease delete an existing save before saving again.');
                                throw new Error('Save limit reached.');
                            }}
                        }}
                        const result = originalSetItem(key, value);
                        if (key.includes('Save') || key.includes('Slot')) {{
                            setTimeout(() => {{ if (window.SaveLoadManager) window.SaveLoadManager.sendSavesToParent(); }}, 100);
                        }}
                        return result;
                    }};

                    const originalRemoveItem = localStorage.removeItem.bind(localStorage);
                    localStorage.removeItem = function(key) {{
                        const result = originalRemoveItem(key);
                        if (key.includes('Save') || key.includes('Slot')) {{
                            setTimeout(() => {{ if (window.SaveLoadManager) window.SaveLoadManager.sendSavesToParent(); }}, 100);
                        }}
                        return result;
                    }};

                    //  3. 引擎初始化
                    try {{ localStorage.removeItem('VisualNovel_Settings'); }} catch(e) {{}}

                    // 多语言：按 window._lang 设引擎 UI 语言 + 预处理脚本字幕占位
                    try {{
                        var _engLangMap = {{'ja-JP':'日本語','en-US':'English'}};
                        var _engLang = _engLangMap[window._lang] || 'English';
                        monogatari.setting('MultiLanguage', false);
                        monogatari.preference('Language', _engLang);
                        // 语音目录指向当前语言子目录 voices/语言（init 前设一次，读档/Start 都生效）
                        try {{
                            var _ap0 = monogatari.setting('AssetsPath') || {{}};
                            _ap0.voices = 'voices/' + (window._lang || window._defaultLang || '');
                            monogatari.setting('AssetsPath', _ap0);
                        }} catch(e) {{ console.warn('[i18n] set voices path err', e); }}
                        // 补引擎内置翻译漏翻的词条（Log / Video Volume），仅中日英
                        try {{
                            monogatari.translation('简体中文', {{ Log:'对话记录', Video:'视频音量' }});
                            monogatari.translation('日本語', {{ Log:'ログ', Video:'動画音量' }});
                        }} catch(e) {{ console.warn('[i18n] patch translation err', e); }}
                        if (window.I18nScript) window.I18nScript.apply();
                        // 角色显示名按当前语言覆写
                        try {{
                            var _names = (window._namesByLang && (window._namesByLang[window._lang] || window._namesByLang[window._defaultLang])) || {{}};
                            var _chars = monogatari.characters();
                            Object.keys(_names).forEach(function(sid){{
                                if (_chars[sid]) _chars[sid].name = _names[sid];
                            }});
                        }} catch(e) {{ console.warn('[i18n] name override err', e); }}
                        console.log('🌐 [i18n] lang=', window._lang, 'engLang=', _engLang);
                    }} catch(e) {{ console.warn('[i18n] setup err', e); }}

                    // 注册 Service Worker（二次访问加速；version 由 build 时间戳决定，新版自动顶掉旧缓存）
                    if ('serviceWorker' in navigator) {{
                        window.addEventListener('load', () => {{
                            navigator.serviceWorker.register('service-worker.js')
                                .then(reg => console.log('✅ [SW] registered, scope:', reg.scope))
                                .catch(err => console.warn('⚠️ [SW] register failed:', err));
                        }});
                    }}

                    monogatari.init('#monogatari').then(() => {{
                        console.log('✅ Visual Novel Engine Started');
                        // init 会从持久化设置恢复旧语言，必须在 init 完成后按 URL 再覆盖一次。
                        if (window.refreshGameUiLanguage) window.refreshGameUiLanguage();
                        if (window.SmartAutoPlay) window.SmartAutoPlay.install();
                        if (window.VoicePlaybackRate) window.VoicePlaybackRate.init();

                        // [关键]彻底禁用引擎的end方法,防止游戏自动结束
                        console.log('🚫 [AntiEnd] 开始Hook引擎的end方法');

                        // 标记:用户是否主动点击quit
                        window._userQuitFlag = false;

                        // 方法1:Hook monogatari.run,拦截'end'命令(但允许用户主动quit)
                        const originalRun = monogatari.run.bind(monogatari);
                        monogatari.run = function(statement, ...args) {{
                            if (typeof statement === 'string' && statement.trim().toLowerCase() === 'end') {{
                                if (window._userQuitFlag) {{
                                    console.log('✅ [AntiEnd] 用户主动quit，允许执行end命令');
                                    window._userQuitFlag = false; // 重置标记
                                    return originalRun(statement, ...args);
                                }} else {{
                                    console.warn('🚫 [AntiEnd] 拦截了脚本自动end命令，游戏不会结束');
                                    return Promise.resolve();
                                }}
                            }}
                            return originalRun(statement, ...args);
                        }};

                        // 方法2:Hook monogatari.global('playing'),防止设置为false(但允许用户主动quit)
                        const originalGlobal = monogatari.global.bind(monogatari);
                        monogatari.global = function(key, value) {{
                            if (key === 'playing' && value === false) {{
                                if (window._userQuitFlag) {{
                                    console.log('✅ [AntiEnd] 用户主动quit，允许设置playing=false');
                                    window._userQuitFlag = false; // 重置标记
                                    return originalGlobal(key, value);
                                }} else {{
                                    console.warn('🚫 [AntiEnd] 拦截了playing=false，游戏不会退出');
                                    return originalGlobal(key, true); // 强制保持true
                                }}
                            }}
                            return originalGlobal(key, value);
                        }};

                        console.log('✅ [AntiEnd] 引擎end方法已被Hook（允许用户主动quit）');

                        if (window.SaveLoadManager) window.SaveLoadManager.init();
                        monogatari.storage({{ settings: {{ autoAdvance: false, voiceVolume: 100, musicVolume: 15, skipVoice: false }} }});

                        // ============================================================
                        //  4. [极简版]按钮逻辑
                        // ============================================================

                        let buttonsAdded = false;
                        function addCustomButtons() {{
                            if (buttonsAdded) return;
                            const mainMenu = document.querySelector('main-menu');
                            if (!mainMenu) return;
                            const startButton = mainMenu.querySelector('[data-action="start"]');
                            if (!startButton) return;

                            // --- Restart 按钮:使用Start标签(包含初始化) ---
                            if (!document.querySelector('.restart-button')) {{
                                const restartBtn = startButton.cloneNode(true);
                                restartBtn.classList.add('restart-button');
                                restartBtn.removeAttribute('data-action');
                                if (window.__setUiButtonLabel) window.__setUiButtonLabel(restartBtn, window.__ui('restart'));
                                startButton.parentNode.insertBefore(restartBtn, startButton.nextSibling);

                                restartBtn.addEventListener('click', (e) => {{
                                    e.preventDefault(); e.stopPropagation();
                                    console.log('🔄 [Main] Restart -> 执行Start标签');

                                    // [Back按钮修复]设置会话起始索引为1
                                    window._sessionStartIndex = 1;

                                    // [Back按钮修复]立即禁用Back按钮
                                    setTimeout(() => {{
                                        const backBtn = document.querySelector('[data-action="back"]');
                                        if (backBtn) {{
                                            backBtn.setAttribute('disabled', 'true');
                                            backBtn.style.opacity = '0.3';
                                            backBtn.style.pointerEvents = 'none';
                                            backBtn.style.cursor = 'not-allowed';
                                            console.log('🚫 [Restart] 预先禁用Back按钮');
                                        }}
                                    }}, 50);

                                    monogatari.global('playing', true);
                                    monogatari.onStart().then(() => {{
                                        monogatari.element().find('[data-screen]').each((screen) => {{
                                            screen.setState({{ open: false }});
                                        }});
                                        monogatari.element().find('[data-screen="game"]').get(0).setState({{ open: true }});
                                        monogatari.run('jump Start');
                                    }});
                                }});
                            }}

                            // --- Continue 按钮:直接跳转到Material_X ---
                            if (!document.querySelector('.continue-button')) {{
                                const continueBtn = startButton.cloneNode(true);
                                continueBtn.classList.add('continue-button');
                                continueBtn.removeAttribute('data-action');
                                if (window.__setUiButtonLabel) window.__setUiButtonLabel(continueBtn, window.__ui('cont'));
                                const restartBtn = document.querySelector('.restart-button');
                                startButton.parentNode.insertBefore(continueBtn, restartBtn ? restartBtn.nextSibling : startButton.nextSibling);

                                continueBtn.addEventListener('click', (e) => {{
                                    e.preventDefault(); e.stopPropagation();

                                    // 获取目标索引
                                    const targetIndex = (window._progressData && window._progressData.current_index) ? window._progressData.current_index : 1;
                                    const branchState = window.BranchProgress ? window.BranchProgress.resume(window._progressData || {{}}) : null;
                                    if (branchState && window.BranchEffectLedger) window.BranchEffectLedger.mark(branchState.pivot + '_' + branchState.branch);
                                    const targetLabel = branchState ? branchState.label : `Material_${{targetIndex}}`;
                                    console.log(`📥 [Main] Continue -> 跳转到${{targetLabel}}`);

                                    // [Back按钮修复]设置会话起始索引为targetIndex
                                    window._sessionStartIndex = targetIndex;

                                    // [Back按钮修复]立即禁用Back按钮,防止在Material逻辑执行前被点击
                                    setTimeout(() => {{
                                        const backBtn = document.querySelector('[data-action="back"]');
                                        if (backBtn) {{
                                            backBtn.setAttribute('disabled', 'true');
                                            backBtn.style.opacity = '0.3';
                                            backBtn.style.pointerEvents = 'none';
                                            backBtn.style.cursor = 'not-allowed';
                                            console.log('🚫 [Continue] 预先禁用Back按钮');
                                        }}
                                    }}, 50);

                                    // 手动初始化系统
                                    if (window.GameProgressReporter) {{
                                        window.GameProgressReporter.init({total_steps}, '{chap_id}', '{node_id}');
                                        window.GameProgressReporter.startAutoSave();
                                        window.GameProgressReporter.update(targetIndex);
                                    }}
                                    if (window.ErrorReporter) {{
                                        window.ErrorReporter.init('{chap_id}', '{node_id}');
                                    }}
                                    if (window.StageManager) {{
                                        window.StageManager.reset();
                                    }}

                                    // 重置引擎状态
                                    try {{
                                        if (monogatari.global) {{
                                            monogatari.global('auto_play', false);
                                            monogatari.global('skipping', false);
                                        }}
                                        document.body.removeAttribute('data-action');
                                    }} catch (e) {{}}

                                    // 启动游戏
                                    monogatari.global('playing', true);
                                    monogatari.onStart().then(() => {{
                                        monogatari.element().find('[data-screen]').each((screen) => {{
                                            screen.setState({{ open: false }});
                                        }});
                                        monogatari.element().find('[data-screen="game"]').get(0).setState({{ open: true }});

                                        // 分支中途继续时直接进入精确 label，避免重放选项并重复结算 effects。
                                        monogatari.run(`jump ${{targetLabel}}`);
                                    }});
                                }});
                                continueBtn.style.display = 'none';
                            }}

                            // --- Quit 按钮：首页 ESC 与该按钮统一向主站申请关闭播放器 ---
                            if (!document.querySelector('.main-quit-button')) {{
                                const quitBtn = startButton.cloneNode(true);
                                quitBtn.classList.add('main-quit-button');
                                quitBtn.removeAttribute('data-action');
                                if (window.__setUiButtonLabel) window.__setUiButtonLabel(quitBtn, window.__ui('quit'));
                                startButton.parentNode.appendChild(quitBtn);
                                quitBtn.addEventListener('click', (e) => {{
                                    e.preventDefault();
                                    e.stopImmediatePropagation();
                                    if (window.GameHostBridge) window.GameHostBridge.requestExit('main_menu_quit');
                                }});
                            }}

                            if (window.refreshGameUiLanguage) window.refreshGameUiLanguage();
                            if (window.GameplayUiState) window.GameplayUiState.sync();
                            updateButtonVisibility();
                            buttonsAdded = true;
                        }}

                        function updateButtonVisibility() {{
                            const startBtn = document.querySelector('[data-action="start"]:not(.restart-button):not(.continue-button)');
                            const continueBtn = document.querySelector('.continue-button');

                            console.log('🔄 [UpdateButton] 开始更新按钮显示');
                            console.log('🔄 [UpdateButton] startBtn:', startBtn);
                            console.log('🔄 [UpdateButton] continueBtn:', continueBtn);
                            console.log('🔄 [UpdateButton] _progressData:', window._progressData);

                            if (!startBtn || !continueBtn) {{
                                console.warn('⚠️ [UpdateButton] 找不到按钮，可能还未创建');
                                return;
                            }}

                            if (window._progressData && window._progressData.current_index > 1) {{
                                console.log('✅ [UpdateButton] 有进度数据，显示Continue按钮');
                                startBtn.style.display = 'none';
                                continueBtn.style.display = '';
                            }} else {{
                                console.log('✅ [UpdateButton] 无进度数据，显示Start按钮');
                                startBtn.style.display = '';
                                continueBtn.style.display = 'none';
                            }}
                        }}

                        // [关键]将updateButtonVisibility挂载到全局,确保quit后可访问
                        window.updateButtonVisibility = updateButtonVisibility;

                        setTimeout(addCustomButtons, 500);

                        // 修复:quit 后从主菜单进入 Settings/Help,点 back 黑屏问题
                        window._screenOpenedFromMain = false;
                        document.addEventListener('click', function(e) {{
                            const t = e.target;
                            const opener = t.closest('[data-action="open-screen"]');
                            if (opener) {{
                                window._screenOpenedFromMain = !!opener.closest('main-menu');
                            }}
                        }}, true);
                        document.addEventListener('click', function(e) {{
                            const t = e.target;
                            const isBack = t.getAttribute('data-action') === 'back' || t.closest('[data-action="back"]');
                            if (!isBack) return;
                            if (!window._screenOpenedFromMain) return;
                            e.stopImmediatePropagation();
                            window._screenOpenedFromMain = false;
                            monogatari.element().find('[data-screen]').each((screen) => {{
                                screen.setState({{ open: false }});
                            }});
                            monogatari.element().find('[data-screen="main"]').get(0).setState({{ open: true }});
                        }}, true);

                        window.addEventListener('message', function(event) {{
                            if (event.data && event.data.type === "INITIAL_PROGRESS") {{
                                console.log('📥 [Message] 收到INITIAL_PROGRESS:', event.data.data);
                                const _pd = event.data.data;
                                window._progressData = _pd;
                                if (typeof window.updateButtonVisibility === 'function') {{
                                    window.updateButtonVisibility();
                                }}
                                if (_pd && _pd.player_variables &&
                                        Object.keys(_pd.player_variables).length > 0) {{
                                    window._playerVariables = Object.assign({{}}, _pd.player_variables);
                                    if (window._syncPlayerVars) window._syncPlayerVars();
                                    if (window.updateStatsUI) window.updateStatsUI();
                                }}
                                if (_pd && _pd.player_variable_choices) {{
                                    window._playerVariableChoices = JSON.parse(JSON.stringify(_pd.player_variable_choices));
                                    if (window._syncPlayerVars) window._syncPlayerVars();
                                }}
                                if (_pd && _pd.player_legacy_flags) {{
                                    window._playerLegacyFlags = Object.assign({{}}, _pd.player_legacy_flags);
                                    if (window._syncPlayerVars) window._syncPlayerVars();
                                }}
                                if (_pd && _pd.system_text && window.SystemPanel) {{
                                    window.SystemPanel.setText(_pd.system_text);
                                }}
                            }}
                        }});

                        // 监听 Quit 确认
                        document.addEventListener('click', function(e) {{
                            const t = e.target;
                            const isQuit = t.getAttribute('data-action') === 'quit' || t.closest('[data-action="quit"]');
                            const isInModal = t.closest('.modal, [role="dialog"]');
                            if (isQuit && isInModal) {{
                                console.log('🚪 [Quit] 用户点击Quit确认按钮');

                                // [关键]设置用户主动quit标记,允许end命令执行
                                window._userQuitFlag = true;
                                console.log('✅ [Quit] 已设置_userQuitFlag=true，允许quit执行');

                                // 禁用进度上报,避免干扰
                                if (window.GameProgressReporter) window.GameProgressReporter._disableReport = true;

                                // [关键]完全模拟游戏加载流程
                                setTimeout(() => {{
                                    console.log('🔄 [Quit] 开始刷新进度和存档数据...');

                                    if (window.parent && window.parent !== window) {{
                                        //  1. 请求最新进度(和游戏加载时一样)
                                        console.log('📤 [Quit] 请求最新进度数据');
                                        window.parent.postMessage({{
                                            type: "REQUEST_INITIAL_PROGRESS",
                                            data: {{
                                                chapter_id: "{chapter_id}",
                                                node_id: "{node_id}",
                                                timestamp: Date.now(),
                                                source: 'quit'  // 标记来源
                                            }}
                                        }}, '*');

                                        //  2. 延迟50ms后请求存档数据(和游戏加载时一样)
                                        setTimeout(() => {{
                                            console.log('📤 [Quit] 请求存档数据');
                                            window.parent.postMessage({{
                                                type: "REQUEST_SAVE_DATA",
                                                data: {{
                                                    chapter_id: "{chapter_id}",
                                                    node_id: "{node_id}",
                                                    timestamp: Date.now(),
                                                    source: 'quit'
                                                }}
                                            }}, '*');
                                        }}, 50);

                                        //  3. 等待数据返回后更新按钮显示
                                        console.log('⏳ [Quit] 等待数据返回，将自动更新按钮显示');
                                    }}

                                    // 重新启用进度上报
                                    if (window.GameProgressReporter) window.GameProgressReporter._disableReport = false;
                                }}, 500);
                            }}
                        }}, true);

                    }});
                }});
                """.format(
                        chapter_id=depth,
                        node_id=node_id,
                        chap_id=depth,
                        total_steps=chapter_data.get('end_index', 1)
                    )
                    main_js_file = node_converter.output_dir / "js" / "main.js"

                    with open(main_js_file, 'w', encoding='utf-8') as f:
                        f.write(main_js_content)

                    # 创建运行脚本
                    node_converter.create_run_scripts()

                    # OSS上传：将assets上传到OSS，更新options.js，删除本地assets
                    if self.enable_oss_upload:
                        oss_base = node_converter._get_oss_base_url()
                        if oss_base:
                            node_converter._patch_options_oss(oss_base, self.output_dir.name, folder_name)
                            node_converter._upload_assets_to_oss(self.output_dir.name, folder_name)
                    else:
                        print(f"  ⏭️  跳过 OSS 上传（enable_oss_upload=False），本地保留 assets")

                    # 给 service-worker.js 注入唯一 name + 时间戳 version
                    # 必须在 OSS 阶段之后（OSS 阶段不动 sw 文件，先后无影响，但放这里语义最清晰）
                    node_converter._patch_service_worker(self.output_dir.name, folder_name)

                    print(f"节点 {node_id} 项目创建完成: {node_converter.output_dir}")
                else:
                    print(f"节点 {node_id} 无素材，跳过")

            # 4. 复制story_tree.json到输出目录
            # 确保输出目录存在
            self.output_dir.mkdir(parents=True, exist_ok=True)
            dest_story_tree = self.output_dir / "story_tree.json"
            shutil.copy2(story_tree_path, dest_story_tree)
            print(f"\n已复制story_tree.json到: {dest_story_tree}")

            print("\n" + "=" * 60)
            print("所有节点项目创建完成！")
            print(f"输出目录: {self.output_dir}")
            print("每个节点的游戏文件位于重命名的章节文件夹中")
            print("=" * 60)

        except Exception as e:
            print(f"创建项目时出错: {e}")
            import traceback
            traceback.print_exc()

    def _get_oss_base_url(self) -> str:
        """远端素材地址已停用，构建产物统一保留本地相对路径。"""
        return ""

    def _patch_service_worker(self, game_project: str, folder_name: str):
        """给 service-worker.js 注入唯一 cache name + 时间戳 version

        每个章节项目独立 cache 桶（避免相互清空），每次 build 用时间戳 version
        强制刷新（与模板里 'if (key !== cache_name) return caches.delete(key)' 协同生效）
        """
        sw_path = self.output_dir / "service-worker.js"
        if not sw_path.exists():
            return
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()

        unique_name = f"{game_project}_{folder_name}".replace("/", "_")
        version_str = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

        content = re.sub(
            r"const\s+name\s*=\s*'[^']*';",
            f"const name = '{unique_name}';",
            content,
            count=1,
        )
        content = re.sub(
            r"const\s+version\s*=\s*'[^']*';",
            f"const version = '{version_str}';",
            content,
            count=1,
        )
        with open(sw_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"  已更新 service-worker.js: name={unique_name}, version={version_str}")

    def _patch_options_oss(self, oss_base_url: str, game_project: str, folder_name: str):
        """将 options.js 和 script.js 里的 assets/ 相对路径替换为 OSS/CDN 绝对路径"""
        oss_root = f"{oss_base_url}/{game_project}/{folder_name}/assets"

        # 替换 options.js 里的 AssetsPath.root
        options_path = self.output_dir / "js" / "options.js"
        if options_path.exists():
            with open(options_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = content.replace("'root': 'assets'", f"'root': '{oss_root}'")
            with open(options_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  已更新 options.js AssetsPath.root -> {oss_root}")

        # 替换 main.css 里硬编码的背景图片路径
        css_path = self.output_dir / "style" / "main.css"
        if css_path.exists():
            with open(css_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = content.replace(
                'url("../assets/images/beijing.png")',
                f'url("{oss_root}/images/beijing.png")'
            )
            with open(css_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  已更新 main.css 背景图片路径 -> {oss_root}/images/beijing.png")
        script_path = self.output_dir / "js" / "script.js"
        if script_path.exists():
            with open(script_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = content.replace("'assets/voices/'", f"'{oss_root}/voices/'")
            content = content.replace("'assets/scenes/'", f"'{oss_root}/scenes/'")
            content = content.replace("`assets/characters/", f"`{oss_root}/characters/")
            content = content.replace("assets/fonts/", f"{oss_root}/fonts/")
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  已更新 script.js 硬编码 assets/ 路径 -> {oss_root}")

    def _upload_assets_to_oss(self, game_project: str, folder_name: str) -> bool:
        """兼容旧调用名，但只统计并保留本地 assets，不连接远端。"""
        assets_dir = self.output_dir / "assets"
        if not assets_dir.exists():
            print(f"  警告: assets目录不存在: {assets_dir}")
            return False
        skip_names = {'.gitignore', '.gitkeep', '.DS_Store', 'Thumbs.db'}
        local_count = sum(
            1 for path in assets_dir.rglob("*")
            if path.is_file() and path.name not in skip_names
        )
        print(f"  本地模式保留 {local_count} 个 assets 文件，跳过远端上传")
        return True

    def _update_char_json_oss_routes(self, folder_name: str, char_stems: list):
        """将已上传的角色图片路径回写到 character_prompts 表对应记录的 oss_route 字段"""
        try:
            import sys as _sys
            _project_root = str(Path(__file__).parent.parent.parent)
            if _project_root not in _sys.path:
                _sys.path.insert(0, _project_root)
            from common.config import DATABASE_URL, PLATFORM_DATABASE_URL
            from sqlalchemy import create_engine, text as sa_text
        except Exception as e:
            print(f"  警告: 无法导入数据库配置，跳过 oss_route 更新: {e}")
            return

        # task_id 由外部传入
        task_id = self.task_id
        if not task_id:
            print("  警告: task_id 未设置，跳过 oss_route 更新")
            return

        try:
            engine = create_engine(PLATFORM_DATABASE_URL)
            with engine.begin() as conn:
                res = conn.execute(
                    sa_text("SELECT data FROM character_prompts WHERE task_id = :tid"),
                    {"tid": task_id}
                )
                row = res.fetchone()
                if not row:
                    print(f"  警告: character_prompts 中未找到 task_id={task_id}，跳过 oss_route 更新")
                    return

                characters = row[0] if isinstance(row[0], list) else json.loads(row[0])
                speaker_index = {c['speaker_id']: i for i, c in enumerate(characters) if 'speaker_id' in c}

                updated = 0
                for stem in char_stems:
                    # 逐步前缀匹配 speaker_id（支持含下划线的 id，如 LuoSanPao）
                    parts = stem.split('_')
                    matched = None
                    for end in range(len(parts), 0, -1):
                        candidate = '_'.join(parts[:end])
                        if candidate in speaker_index:
                            matched = candidate
                            break
                    if matched is None:
                        continue

                    idx = speaker_index[matched]
                    route = f"{folder_name}/{stem}"
                    oss_routes = characters[idx].setdefault('oss_route', [])
                    if route not in oss_routes:
                        oss_routes.append(route)
                        updated += 1

                if updated:
                    conn.execute(
                        sa_text(
                            "UPDATE character_prompts SET data = CAST(:data AS jsonb), update_time = NOW() WHERE task_id = :tid"),
                        {"data": json.dumps(characters, ensure_ascii=False), "tid": task_id}
                    )
                    print(f"  已更新 character_prompts oss_route: {updated} 条新路径 (folder: {folder_name})")
        except Exception as e:
            print(f"  警告: oss_route 更新失败: {e}")

    def create_run_scripts(self):
        """创建运行脚本"""
        # Linux/Mac
        run_sh = """#!/bin/bash
echo "启动视觉小说..."
echo ""

if ! command -v node &> /dev/null; then
    echo "请先安装Node.js: https://nodejs.org/"
    exit 1
fi

if [ ! -d "node_modules" ]; then
    echo "安装依赖..."
    npm install
fi

echo "启动服务器..."
echo "访问: http://localhost:3000"
echo "按 Ctrl+C 停止"
echo ""

npm run serve
"""

        run_sh_file = self.output_dir / "run.sh"
        with open(run_sh_file, 'w', encoding='utf-8') as f:
            f.write(run_sh)
        os.chmod(run_sh_file, 0o755)

        # Windows
        run_bat = """@echo off
echo 启动视觉小说...
echo.

where node >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo 请先安装Node.js: https://nodejs.org/
    pause
    exit /b 1
)

if not exist "node_modules" (
    echo 安装依赖...
    call npm install
)

echo 启动服务器...
echo 访问: http://localhost:3000
echo 按 Ctrl+C 停止
echo.

call npm run serve
"""

        run_bat_file = self.output_dir / "run.bat"
        with open(run_bat_file, 'w', encoding='utf-8') as f:
            f.write(run_bat)

    def generate_nginx_config(self):
        """更新统一的Nginx配置文件，支持多章节动态路由"""
        print("更新统一的Nginx配置文件...")

        # 提取game_project名称（从output_dir的最后一级目录）
        game_project = self.output_dir.name
        print(f"游戏项目ID: {game_project}")

        # 扫描输出目录下的所有chapter_*文件夹
        chapter_folders = []
        if self.output_dir.exists():
            for item in self.output_dir.iterdir():
                if item.is_dir() and item.name.startswith('chapter_'):
                    chapter_folders.append(item.name)

        if not chapter_folders:
            print("警告：未找到任何章节文件夹，跳过Nginx配置生成")
            return

        print(f"找到 {len(chapter_folders)} 个章节文件夹: {chapter_folders}")

        # 构建该游戏的location块
        location_blocks = []

        # 添加游戏首页路由
        location_blocks.append(f"""
        # 游戏首页: {game_project}
        location = /{game_project}/ {{
            index index.html;
            try_files $uri /{game_project}/index.html;
        }}""")

        # 添加各章节路由
        for folder in sorted(chapter_folders):
            location_blocks.append(f"""
        # 章节: {folder}
        location /{game_project}/{folder}/ {{
            index index.html;
            try_files $uri $uri/ /{game_project}/{folder}/index.html;

            # 静态资源缓存
            location ~* \\.(js|css|png|jpg|jpeg|gif|ico|mp3|ogg|wav)$ {{
                expires 1y;
                add_header Cache-Control "public, immutable";
            }}
        }}""")

        location_config = "\n".join(location_blocks)

        # 生成该游戏的完整配置块（带标记）
        game_config_block = f"""
        # GAME_START: {game_project}
        # 游戏: {game_project}
        # 章节数量: {len(chapter_folders)}
        # 更新时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{location_config}
        # GAME_END: {game_project}
"""

        # 更新统一的nginx配置文件
        self._update_unified_nginx_config(game_project, game_config_block)

        # 生成游戏的章节列表首页
        self.generate_index_page(chapter_folders, game_project)

    def _update_unified_nginx_config(self, game_id, game_config_block):
        """更新统一的nginx配置文件，删除旧配置并添加新配置"""
        # 统一配置文件路径（在 storage 目录下）
        unified_nginx_path = self.output_dir.parent / "nginx.conf"

        print(f"统一配置文件路径: {unified_nginx_path}")

        # 如果配置文件不存在，创建初始模板
        if not unified_nginx_path.exists():
            print("统一配置文件不存在，创建初始模板...")
            self._create_initial_nginx_config(unified_nginx_path)

        # 读取现有配置
        with open(unified_nginx_path, 'r', encoding='utf-8') as f:
            config_content = f.read()

        # 删除该游戏的旧配置块（如果存在）
        start_marker = f"# GAME_START: {game_id}"
        end_marker = f"# GAME_END: {game_id}"

        # 使用正则表达式删除旧配置块
        # 修复：允许 GAME_END 后面没有换行符的情况
        pattern = re.compile(
            rf'\s*{re.escape(start_marker)}.*?{re.escape(end_marker)}',
            re.DOTALL
        )
        config_content = pattern.sub('', config_content)

        # 找到插入位置（在游戏路由配置区域的注释之后）
        insert_marker = "# 游戏配置将自动插入到此处"
        if insert_marker in config_content:
            # 在标记后插入新配置
            config_content = config_content.replace(
                insert_marker,
                f"{insert_marker}\n{game_config_block}"
            )
        else:
            # 如果找不到标记，在 server 块的最后插入（在最后一个 } 之前）
            # 找到最后一个 server 块
            last_brace_pos = config_content.rfind('}')
            if last_brace_pos != -1:
                # 在倒数第二个 } 之前插入
                second_last_brace = config_content.rfind('}', 0, last_brace_pos)
                if second_last_brace != -1:
                    config_content = (
                            config_content[:second_last_brace] +
                            game_config_block + '\n' +
                            config_content[second_last_brace:]
                    )

        # 写回配置文件
        with open(unified_nginx_path, 'w', encoding='utf-8') as f:
            f.write(config_content)

        print(f"✅ 已更新统一Nginx配置: {unified_nginx_path}")
        print(f"   游戏ID: {game_id}")

    def _create_initial_nginx_config(self, config_path):
        """创建初始的nginx配置文件模板"""
        initial_config = """# Nginx 统一配置 - 视觉小说游戏工厂
# 此配置文件由 game_builder 服务自动管理
# 请勿手动编辑 GAME_START 和 GAME_END 标记之间的内容
# 最后更新时间: """ + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + """

events {
    worker_connections 1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;

    sendfile        on;
    tcp_nopush      on;
    tcp_nodelay     on;
    keepalive_timeout  65;
    types_hash_max_size 2048;

    # 启用gzip压缩
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml text/javascript application/json application/javascript application/xml+rss application/atom+xml image/svg+xml;

    # 主服务器配置
    server {
        listen 80;
        server_name localhost;
        root /usr/share/nginx/html;

        # 全局首页
        location = / {
            index index.html;
            try_files $uri /index.html;
            autoindex off;
        }

        # 全局静态资源缓存
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|mp3|ogg|wav)$ {
            expires 1y;
            add_header Cache-Control "public, immutable";
        }

        # 禁止访问隐藏文件
        location ~ /\. {
            deny all;
        }

        # 错误页面
        error_page 404 /404.html;
        error_page 500 502 503 504 /50x.html;

        # ==========================================
        # 游戏路由配置区域（自动生成，请勿手动编辑）
        # ==========================================

        # 游戏配置将自动插入到此处

    }
}
"""
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(initial_config)
        print(f"✅ 已创建初始Nginx配置模板: {config_path}")

    def generate_index_page(self, chapter_folders, game_project):
        """生成章节列表首页"""
        index_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Visual Novel - Chapter Selection</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: white;
        }}
        .container {{
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }}
        h1 {{
            text-align: center;
            margin-bottom: 40px;
            font-size: 2.5em;
            text-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
        }}
        .chapter-list {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            margin-top: 30px;
        }}
        .chapter-card {{
            background: rgba(255, 255, 255, 0.2);
            border-radius: 15px;
            padding: 20px;
            text-align: center;
            transition: all 0.3s ease;
            text-decoration: none;
            color: white;
            display: block;
            border: 1px solid rgba(255, 255, 255, 0.3);
        }}
        .chapter-card:hover {{
            transform: translateY(-5px);
            background: rgba(255, 255, 255, 0.3);
            box-shadow: 0 10px 20px rgba(0, 0, 0, 0.2);
        }}
        .chapter-card h3 {{
            margin: 0 0 10px 0;
            font-size: 1.3em;
        }}
        .chapter-card p {{
            margin: 0;
            opacity: 0.8;
            font-size: 0.9em;
        }}
        .footer {{
            text-align: center;
            margin-top: 40px;
            opacity: 0.7;
            font-size: 0.9em;
        }}
        @media (max-width: 600px) {{
            .chapter-list {{
                grid-template-columns: 1fr;
            }}
            .container {{
                padding: 20px;
            }}
            h1 {{
                font-size: 2em;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🎮 Visual Novel - Chapter Selection</h1>
        <p style="text-align: center; margin-bottom: 30px;">Please select a chapter to begin:</p>

        <div class="chapter-list">
"""

        for folder in sorted(chapter_folders):
            # 尝试从章节文件夹中读取index.html获取章节标题
            chapter_title = folder.replace('_', ' ').title()
            # 从文件夹名 chapter_{depth}_{uuid} 解析章节深度,depth>2 标记为试玩锁定
            _depth = 1
            _parts = folder.split('_')
            if len(_parts) >= 2 and _parts[0] == 'chapter' and _parts[1].isdigit():
                _depth = int(_parts[1])
            _locked_attr = ' data-locked="1"' if _depth > 2 else ''
            index_html += f"""            <a href="/{game_project}/{folder}/" class="chapter-card"{_locked_attr}>
                <h3>{chapter_title}</h3>
                <p>Click to start</p>
            </a>
"""

        index_html += """        </div>

        <div class="footer">
            <p>Powered by Monogatari.js | Deployed: {deploy_time}</p>
        </div>
    </div>

    <script>
        // 自动从URL参数获取部署时间
        document.addEventListener('DOMContentLoaded', function() {{
            const deployTime = new Date().toLocaleString('en-US');
            const footer = document.querySelector('.footer p');
            if (footer) {{
                footer.innerHTML = footer.innerHTML.replace('{deploy_time}', deployTime);
            }}
            // 试玩限制:非iframe(独立打开)时锁定第3章及以后
            const standalone = !(window.parent && window.parent !== window);
            if (standalone) {{
                document.querySelectorAll('.chapter-card[data-locked="1"]').forEach(function(el) {{
                    el.style.opacity = '0.45';
                    el.style.pointerEvents = 'none';
                    el.style.cursor = 'not-allowed';
                    const p = el.querySelector('p');
                    if (p) p.textContent = '试玩仅开放前两章';
                }});
            }}
        }});
    </script>
</body>
</html>"""

        index_path = self.output_dir / "index.html"
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(index_html)
        print(f"首页已生成: {index_path}")

    def generate_nginx_config_only(self):
        """生成Nginx配置文件（适配新的Docker生产者-消费者架构）"""
        print("\n" + "=" * 60)
        print("生成 Nginx 配置文件 (用于 Docker 挂载)...")
        print("=" * 60)

        # 调用核心生成逻辑
        self.generate_nginx_config()

        print("\n✅ Nginx 配置已生成！")
        print("现在的架构是：")
        print("  1. 此脚本生成游戏文件 + nginx.conf -> shared_data 目录")
        print("  2. Docker (Nginx 容器) 挂载 shared_data 目录读取内容")
        print("=" * 60)
