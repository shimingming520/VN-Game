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
    from .converter import normalize_story_tree, extract_story_ui_text, extract_story_choice_text
except ImportError:
    try:
        from services.game_builder.converter import normalize_story_tree, extract_story_ui_text, extract_story_choice_text
    except ImportError:
        def normalize_story_tree(story_tree):
            return (story_tree if isinstance(story_tree, dict) else {}), {}
        def extract_story_ui_text(_layers):
            return {}
        def extract_story_choice_text(_layers, _node_id):
            return {}


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
    def get_effects_for_beat(atmosphere_config, chapter_id, beat_index):
        """获取指定beat的特效列表（按order排序）"""
        chapter_key = f"chapter_{chapter_id}"
        if chapter_key in atmosphere_config:
            beat_key = str(beat_index)
            if beat_key in atmosphere_config[chapter_key]:
                effects = atmosphere_config[chapter_key][beat_key]
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
        const sprites = document.querySelectorAll('[data-character]');
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
                z-index: 7500; pointer-events: none;
            `;
            document.body.appendChild(canvas);

            const ctx = canvas.getContext('2d');
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;

            const drops = [];
            for (let i = 0; i < 200; i++) {
                drops.push({
                    x: Math.random() * canvas.width,
                    y: Math.random() * canvas.height,
                    l: Math.random() * 15 + 10,
                    s: Math.random() * 6 + 4
                });
            }

            function animate() {
                if (!document.getElementById('atmo-weather-rain')) return;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.strokeStyle = 'rgba(174, 194, 224, 0.4)';
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
                z-index: 7500; pointer-events: none;
            `;
            document.body.appendChild(canvas);

            const ctx = canvas.getContext('2d');
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;

            const particles = [];
            for (let i = 0; i < 60; i++) {
                particles.push({
                    x: Math.random() * canvas.width,
                    y: Math.random() * canvas.height,
                    l: Math.random() * 40 + 20,
                    s: Math.random() * 10 + 6
                });
            }

            function animate() {
                if (!document.getElementById('atmo-weather-wind')) return;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.strokeStyle = 'rgba(255, 255, 255, 0.25)';
                ctx.lineWidth = 1.5;
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
            this._createParticleEffect('atmo-weather-leaves', 'rgba(200,140,60,0.7)', 70, true, 2);
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-leaves');
            if (el) el.remove();
        }
    },

    weather_ash: {
        start: function() {
            this._createParticleEffect('atmo-weather-ash', 'rgba(80,80,80,0.5)', 90, false, 1.5);
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-ash');
            if (el) el.remove();
        }
    },

    weather_dust: {
        start: function() {
            this._createParticleEffect('atmo-weather-dust', 'rgba(180,180,160,0.3)', 50, false, 1);
        },
        stop: function() {
            const el = document.getElementById('atmo-weather-dust');
            if (el) el.remove();
        }
    },

    _createParticleEffect: function(id, color, count, sway, maxRadius) {
        if (document.getElementById(id)) return;
        const canvas = document.createElement('canvas');
        canvas.id = id;
        canvas.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            z-index: 7500; pointer-events: none;
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
                r: Math.random() * maxRadius + 0.5,
                d: Math.random() * 0.6 + 0.2,
                sway: sway ? (Math.random() * 2 - 1) : 0
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
                p.x += p.sway * 0.3;
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
                game.style.transition = 'filter 2s ease';
                game.style.filter = 'sepia(0.75) contrast(0.92)';
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
                 domain=None, art_style="modern_realism"):
        """
        初始化转换器

        Args:
            source_dir: 素材源目录
            template_dir: 物语引擎模板目录
            output_dir: 输出目录
            images_base_dir: 图片基础目录，默认为source_dir.parent
            domain: 域名配置，默认为 localhost（可以是单个域名或多个域名，用空格分隔）
            art_style: 画面风格（ink_chinese/cyberpunk/japanese/dark_european/modern_realism）
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
        self.story_tree_by_lang = {}
        self.story_tree_default_language = 'en-US'
        self.story_ui_by_lang = {}
        self.story_choice_text_by_lang = {}
        self._source_node_id = None

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
            re.compile(r'(.+?)_(\d+)$')   # 旧格式：scene_id_index（下划线分隔）
        ]
        # 软分支格式：scene_id-index-A1 / scene_id-index-B3 / scene_id-index-C
        self.branch_pattern = re.compile(r'^(.+?)-(\d+)-([AB])(\d+)$')   # A/B 分支
        self.branch_c_pattern = re.compile(r'^(.+?)-(\d+)-C$')            # C 结算文件
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

    def scan_assets(self):
        """扫描所有素材文件"""
        print("扫描素材文件...")
        print(f"源目录: {self.source_dir}")
        print(f"图片基础目录: {self.images_base_dir}")

        # 清空现有列表
        self.audio_files = []
        self.subtitle_files = []
        self.scene_images = []
        self.character_images = []
        self.music_files = []
        self.sfx_files = []
        self.valid_image_stems = set()
        # 注意：不清除 character_map，因为它可能已由 load_character_info 加载

        # 确定要扫描的节点目录列表
        node_dirs_to_scan = []
        if self.source_dir.name.startswith('node'):
            # source_dir 本身就是一个节点目录
            node_dirs_to_scan = [self.source_dir]
        else:
            # source_dir 是根目录，扫描所有 node* 子目录
            for node_dir in self.source_dir.glob("node*"):
                if node_dir.is_dir():
                    node_dirs_to_scan.append(node_dir)

        if not node_dirs_to_scan:
            print("警告：未找到任何节点目录，将尝试直接扫描源目录下的 audios 文件夹")
            # 退回到直接扫描 source_dir/audios
            node_dirs_to_scan = [self.source_dir]

        # 扫描音频文件
        for node_dir in node_dirs_to_scan:
            audio_dir = node_dir / "audios" / "audio"
            if audio_dir.exists():
                for audio_file in audio_dir.glob("*.mp3"):
                    self.audio_files.append({
                        'path': audio_file,
                        'name': audio_file.name,
                        'stem': audio_file.stem
                    })
                    print(f"发现音频: {audio_file.name}")
            else:
                # 如果 audios/audio 不存在，尝试直接扫描 audio 目录
                audio_dir_alt = node_dir / "audio"
                if audio_dir_alt.exists():
                    for audio_file in audio_dir_alt.glob("*.mp3"):
                        self.audio_files.append({
                            'path': audio_file,
                            'name': audio_file.name,
                            'stem': audio_file.stem
                        })
                        print(f"发现音频: {audio_file.name}")

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
                    print(f"发现字幕: {subtitle_file.name}")
            else:
                # 如果 audios/subtitle 不存在，尝试直接扫描 subtitle 目录
                subtitle_dir_alt = node_dir / "subtitle"
                if subtitle_dir_alt.exists():
                    for subtitle_file in subtitle_dir_alt.glob("*.txt"):
                        self.subtitle_files.append({
                            'path': subtitle_file,
                            'name': subtitle_file.name,
                            'stem': subtitle_file.stem
                        })
                        print(f"发现字幕: {subtitle_file.name}")

        # 扫描场景图片（从共享的 images_base_dir）
        scenes_dir = self.images_base_dir / "images" / "scenes"
        if scenes_dir.exists():
            for scene_file in scenes_dir.glob("*.png"):
                self.scene_images.append({
                    'path': scene_file,
                    'name': scene_file.name,
                    'stem': scene_file.stem
                })
                # print(f"发现场景图片: {scene_file.name}")  # 注释掉减少刷屏
        else:
            print(f"警告：场景图片目录不存在: {scenes_dir}")

        # 扫描角色图片（从共享的 images_base_dir）
        characters_dir = self.images_base_dir / "images" / "characters"
        if characters_dir.exists():
            for char_file in characters_dir.glob("*.png"):
                stem = char_file.stem

                # 把文件名(不含后缀)加入白名单，比如 "tanghao_1_pingjing"
                self.valid_image_stems.add(stem)

                # 提取拼音部分 (支持 - 和 _ 分隔符)
                import re
                parts = re.split(r'[-_]', stem)
                if parts:
                    pinyin_base = parts[0]
                else:
                    pinyin_base = stem

                # 动态添加拼音到中文映射（如果不存在）
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
                # print(f"发现角色图片: {char_file.name}") # 你可以注释掉这行减少刷屏

                # 添加到角色映射 (保持原逻辑)
                if chinese_name not in self.character_map:
                    if chinese_name not in self.chinese_to_id:
                        # 直接使用完整的拼音作为角色的唯一ID，而不是使用前两个字母
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

        # 扫描背景音乐文件（新目录：audios/bgm，新格式：nodeid_start-end.mp3）
        for node_dir in node_dirs_to_scan:
            # 优先扫描新目录 audios/bgm，其次 audios/music，最后 music
            for music_dir in [node_dir / "audios" / "bgm", node_dir / "audios" / "music", node_dir / "music"]:
                if music_dir.exists():
                    for music_file in music_dir.glob("*.mp3"):
                        # 新格式：nodeid_start-end
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
                            print(f"发现BGM: {music_file.name} (索引范围: {start_idx} - {end_idx})")
                        else:
                            # 旧格式兼容：music_start_end
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
                                print(f"发现BGM(旧格式): {music_file.name} (索引范围: {start_idx} - {end_idx})")
                            else:
                                print(f"警告: BGM文件名格式不正确: {music_file.name}")
                    break  # 找到第一个存在的目录就停止

        # 扫描音效文件（新目录：audios/sfx，格式：nodeid_index.mp3）
        for node_dir in node_dirs_to_scan:
            sfx_dir = node_dir / "audios" / "sfx"
            if sfx_dir.exists():
                for sfx_file in sfx_dir.glob("*.mp3"):
                    match = self.sfx_pattern.match(sfx_file.stem)
                    if match:
                        sfx_idx = int(match.group(2))
                        self.sfx_files.append({
                            'path': sfx_file,
                            'name': sfx_file.name,
                            'stem': sfx_file.stem,
                            'index': sfx_idx
                        })
                        print(f"发现SFX: {sfx_file.name} (索引: {sfx_idx})")
                    else:
                        print(f"警告: SFX文件名格式不正确: {sfx_file.name}")

        print(f"资源扫描完成: {len(self.audio_files)}个音频, {len(self.subtitle_files)}个字幕, "
              f"{len(self.scene_images)}个场景, {len(self.character_images)}个角色图片, {len(self.music_files)}个BGM, {len(self.sfx_files)}个SFX")

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

    def split_pinyin_str(self, text):
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
                    scene_id = match.group(1)
                    index = int(match.group(2))
                    return {
                        'scene_id': scene_id,
                        'index': index,
                        'chapter': 1,
                        'scene': 1
                    }
                elif len(match.groups()) == 3:
                    return {
                        'scene_id': f"scene_{match.group(1)}_{match.group(2)}",
                        'index': int(match.group(3)),
                        'chapter': int(match.group(1)),
                        'scene': int(match.group(2))
                    }

        # 默认值
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

    def organize_materials(self):
        """组织素材，按索引排序；软分支 index 聚合为 choice_node"""
        print("组织素材（按索引排序，不分章）...")

        # ── 第一步：分桶 ──────────────────────────────────────────────
        normal_audio    = {}
        normal_subtitle = {}
        branch_audio    = {}   # index -> branch -> seq -> item
        branch_subtitle = {}   # index -> branch -> seq -> item
        branch_c_content = {}  # index -> raw content string

        for audio in self.audio_files:
            info = self.extract_scene_info(audio['name'])
            item = {
                'type': 'audio', 'file': audio['name'], 'id': audio['stem'],
                'index': info['index'], 'scene_id': info['scene_id'],
                'chapter': info['chapter'], 'scene': info['scene'],
                'path': audio['path'], 'is_audio': True
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
            info = self.extract_scene_info(subtitle['name'])
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
                        import re as _re
                        _m = _re.match(r'^[^:：]+[：:](.+)$', raw_option)
                        option_text = _m.group(1).strip() if _m else raw_option
                        actual_content = parts[1].strip()
                    dialogues = self.parse_subtitle_content(actual_content)
                    item = {
                        'type': 'subtitle', 'file': subtitle['name'],
                        'index': idx, 'scene_id': info['scene_id'],
                        'chapter': info['chapter'], 'scene': info['scene'],
                        'dialogues': dialogues, 'is_subtitle': True,
                        'option_text': option_text
                    }
                    branch_subtitle.setdefault(idx, {}).setdefault(br, {})[info['branch_seq']] = item
            else:
                dialogues = self.parse_subtitle_content(content)
                normal_subtitle[info['index']] = {
                    'type': 'subtitle', 'file': subtitle['name'],
                    'index': info['index'], 'scene_id': info['scene_id'],
                    'chapter': info['chapter'], 'scene': info['scene'],
                    'dialogues': dialogues, 'is_subtitle': True
                }

        # ── 第二步：组装线性列表 ──────────────────────────────────────
        all_indices = set(normal_audio.keys()) | set(normal_subtitle.keys()) | \
                      set(branch_audio.keys()) | set(branch_subtitle.keys())

        all_materials = []
        for idx in sorted(all_indices):
            if idx in branch_audio or idx in branch_subtitle:
                # 聚合为 choice_node
                def _build_branch_materials(br_letter):
                    audio_seqs    = branch_audio.get(idx, {}).get(br_letter, {})
                    subtitle_seqs = branch_subtitle.get(idx, {}).get(br_letter, {})
                    seqs = sorted(set(list(audio_seqs.keys()) + list(subtitle_seqs.keys())))
                    result = []
                    for s in seqs:
                        a = audio_seqs.get(s)
                        sub = subtitle_seqs.get(s)
                        result.append({'audio': a, 'subtitle': sub, 'seq': s})
                    return result

                branch_a_mats = _build_branch_materials('A')
                branch_b_mats = _build_branch_materials('B')

                def _get_option_text(br_letter):
                    sub1 = branch_subtitle.get(idx, {}).get(br_letter, {}).get(1)
                    return sub1.get('option_text') if sub1 else None

                effects = self._parse_branch_effects(branch_c_content.get(idx, ''))
                legacy_flags = {
                    'a': effects.pop('legacy_flags_a', {}),
                    'b': effects.pop('legacy_flags_b', {}),
                }

                scene_id = 'default'
                if branch_a_mats and branch_a_mats[0].get('audio'):
                    scene_id = branch_a_mats[0]['audio']['scene_id']
                elif branch_a_mats and branch_a_mats[0].get('subtitle'):
                    scene_id = branch_a_mats[0]['subtitle']['scene_id']

                all_materials.append({
                    'type': 'choice_node',
                    'index': idx,
                    'scene_id': scene_id,
                    'option_a_text': _get_option_text('A') or '选择A',
                    'option_b_text': _get_option_text('B') or '选择B',
                    'branch_a': branch_a_mats,
                    'branch_b': branch_b_mats,
                    'effects': effects,
                    'legacy_flags': legacy_flags,
                    'is_choice': True
                })
            else:
                audio = normal_audio.get(idx)
                subtitle = normal_subtitle.get(idx)
                if audio:
                    all_materials.append(audio)
                if subtitle:
                    all_materials.append(subtitle)

        if not all_materials:
            return {}

        # 按 index 再排一次
        all_materials.sort(key=lambda x: x['index'])

        scenes = {
            'all': {
                'chapter': 1, 'scene': 1,
                'materials': all_materials,
                'start_index': all_materials[0]['index'],
                'end_index': all_materials[-1]['index']
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
        music_dir = assets_dir / "music"  # 1. 确保定义 music 目录

        voices_dir.mkdir(exist_ok=True)
        scenes_dir.mkdir(exist_ok=True)
        characters_dir.mkdir(exist_ok=True)
        music_dir.mkdir(exist_ok=True)  # 2. 创建目录

        # 复制音频
        for audio in self.audio_files:
            shutil.copy2(audio['path'], voices_dir / audio['name'])

        # 复制场景图片
        for scene in self.scene_images:
            shutil.copy2(scene['path'], scenes_dir / scene['name'])

        # 复制角色图片
        for char in self.character_images:
            shutil.copy2(char['path'], characters_dir / char['name'])

        # 3. 【新增】复制背景音乐
        for music in self.music_files:
            shutil.copy2(music['path'], music_dir / music['name'])

        print(
            f"复制完成: {len(self.audio_files)}音频, {len(self.scene_images)}场景, {len(self.character_images)}角色, {len(self.music_files)}背景音乐")

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

        voices_dir.mkdir(exist_ok=True)
        scenes_dir.mkdir(exist_ok=True)
        characters_dir.mkdir(exist_ok=True)
        music_dir.mkdir(exist_ok=True)
        sfx_dir.mkdir(exist_ok=True)

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

        # 复制本章音频
        audio_count = 0
        for audio in self.audio_files:
            if audio['name'] in chapter_audio_files:
                shutil.copy2(audio['path'], voices_dir / audio['name'])
                audio_count += 1

        # 复制本章字幕
        subtitle_count = 0
        for subtitle in self.subtitle_files:
            if subtitle['name'] in chapter_subtitle_files:
                shutil.copy2(subtitle['path'], voices_dir / subtitle['name'])
                subtitle_count += 1

        # 根据音频和字幕文件名推断需要的场景图片
        needed_scenes = set()
        import re

        # 从音频文件推断场景
        for audio_file in chapter_audio_files:
            # 新格式：scene_id-index.mp3 -> scene_id.png
            base_name = re.sub(r'-\d+\.mp3$', '', audio_file)
            if base_name == audio_file:  # 没匹配到新格式，尝试旧格式
                base_name = re.sub(r'_\d+\.mp3$', '', audio_file)
            scene_name = base_name + '.png'
            needed_scenes.add(scene_name)

        # 从字幕文件推断场景（处理纯字幕没有音频的情况）
        for subtitle_file in chapter_subtitle_files:
            # 软分支格式：scene_id-index-A1.txt / scene_id-index-B2.txt / scene_id-index-C.txt
            base_name = re.sub(r'-\d+-[AB]\d+\.txt$', '', subtitle_file)
            if base_name == subtitle_file:
                base_name = re.sub(r'-\d+-C\.txt$', '', subtitle_file)
            if base_name == subtitle_file:
                base_name = re.sub(r'-\d+\.txt$', '', subtitle_file)
            if base_name == subtitle_file:
                base_name = re.sub(r'_\d+\.txt$', '', subtitle_file)
            scene_name = base_name + '.png'
            needed_scenes.add(scene_name)

        # 输出去重后的统计信息
        print(
            f"从 {len(chapter_audio_files)} 个音频 + {len(chapter_subtitle_files)} 个字幕文件推断出 {len(needed_scenes)} 个唯一场景（已自动去重）")

        # 复制场景图片（只复制本章需要的）
        scene_count = 0
        for scene in self.scene_images:
            if scene['name'] in needed_scenes:
                shutil.copy2(scene['path'], scenes_dir / scene['name'])
                scene_count += 1

        # 根据字幕文件推断需要的角色图片
        needed_characters = set()

        # 从字幕文件中提取说话人
        for subtitle_file in chapter_subtitle_files:
            # 读取字幕文件，提取speaker
            subtitle_path = None
            for subtitle in self.subtitle_files:
                if subtitle['name'] == subtitle_file:
                    subtitle_path = subtitle['path']
                    break

            if subtitle_path and subtitle_path.exists():
                try:
                    with open(subtitle_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        # 提取说话人，支持两种格式：
                        # 1. speaker: 角色名
                        # 2. 角色名：内容 或 角色名:内容
                        import re

                        # 格式1: speaker: 角色名
                        speakers = re.findall(r'speaker:\s*(.+)', content)
                        for speaker in speakers:
                            speaker = speaker.strip()
                            if speaker and speaker not in ['旁白', 'PangBai', 'Narrator']:
                                char_image = f"{speaker}.png"
                                needed_characters.add(char_image)

                        # 格式2: 角色名：内容 或 角色名:内容（行首）
                        speakers = re.findall(r'^([^：:\n]+)[：:]', content, re.MULTILINE)
                        for speaker in speakers:
                            speaker = speaker.strip()
                            if speaker and speaker not in ['旁白', 'PangBai', 'Narrator']:
                                char_image = f"{speaker}.png"
                                needed_characters.add(char_image)

                except Exception as e:
                    print(f"⚠️ 读取字幕文件失败 {subtitle_file}: {e}")

        # 输出去重后的统计信息
        print(f"从 {len(chapter_subtitle_files)} 个字幕文件推断出 {len(needed_characters)} 个唯一角色名（已自动去重）")

        # 复制角色图片（直接匹配完整文件名）
        character_count = 0
        copied_characters = []
        for char in self.character_images:
            if char['name'] in needed_characters:
                shutil.copy2(char['path'], characters_dir / char['name'])
                character_count += 1
                copied_characters.append(char)

        # 【新增】复制本章需要的背景音乐
        # 复制本章需要的背景音乐
        music_count = 0
        for music in self.music_files:
            if max(music['start'], chap_start) <= min(music['end'], chap_end):
                shutil.copy2(music['path'], music_dir / music['name'])
                music_count += 1

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
            f"复制完成: {audio_count}音频, {subtitle_count}字幕, {scene_count}场景(从{len(needed_scenes)}个需要), {character_count}角色(从{len(needed_characters)}个需要), {music_count}BGM, {sfx_count}SFX, {bg_count}自定义背景")

        # 返回实际复制的资源列表（用于生成资源声明）
        copied_scenes = [scene for scene in self.scene_images if scene['name'] in needed_scenes]
        copied_audios = [audio for audio in self.audio_files if audio['name'] in chapter_audio_files]
        copied_music = [music for music in self.music_files if
                        max(music['start'], chap_start) <= min(music['end'], chap_end)]

        return {
            'scenes': copied_scenes,
            'audios': copied_audios,
            'music': copied_music,
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
            music_to_declare = self.music_files
            sfx_to_declare = self.sfx_files
        else:
            scenes_to_declare = copied_assets.get('scenes', [])
            music_to_declare = copied_assets.get('music', [])
            sfx_to_declare = copied_assets.get('sfx', [])

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
    #     lines.append("// 游戏脚本 - 按索引顺序播放（忽略一二级标题）")
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
    def generate_script_for_chapter(self, chapter_data, node_data=None, folder_mapping=None,
                                     hook_id=None, player_variables=None, branch_thresholds=None,
                                     launcher_key=None):
        """生成本章游戏脚本（大道至简版：每一步都显式指定背景，逻辑与人物一致）"""
        chap_id = str(node_data.get('depth', 1)) if node_data else '1'
        print(f"生成第 {chap_id} 章游戏脚本...")

        lines = []

        _choice_default_map = (self.story_choice_text_by_lang or {}).get(
            self.story_tree_default_language, {}
        ) or (self.story_choice_text_by_lang or {}).get('en-US', {}) or {}
        def _localized_choice_text(child_id, fallback):
            key = f"{self._source_node_id or (node_data or {}).get('id', '')}::{child_id}"
            return f"␟choice:{key}␟" if key in _choice_default_map else str(fallback or '')

        # 1. 数据准备
        materials = chapter_data.get('materials', [])
        audio_by_index = {}
        subtitle_by_index = {}
        choice_by_index = {}
        for material in materials:
            idx = material.get('index', 0)
            if material.get('is_choice'):
                choice_by_index[idx] = material
            elif material.get('is_audio'):
                audio_by_index[idx] = material
            elif material.get('is_subtitle'):
                subtitle_by_index[idx] = material

        # SFX 按 index 建立映射
        sfx_by_index = {}
        for sfx in self.sfx_files:
            sfx_by_index.setdefault(sfx['index'], []).append(sfx)

        # ==================== 预处理场景状态 ====================
        audio_subtitle_pairs = []
        current_active_scene = "default"
        valid_scene_stems = {s['stem'] for s in self.scene_images}

        all_indices = sorted(set(
            list(audio_by_index.keys()) +
            list(subtitle_by_index.keys()) +
            list(choice_by_index.keys())
        ))

        for idx in all_indices:
            if idx in choice_by_index:
                cn = choice_by_index[idx]
                raw_scene_id = cn.get('scene_id', 'default')
                if raw_scene_id and raw_scene_id != 'default' and raw_scene_id in valid_scene_stems:
                    current_active_scene = raw_scene_id
                audio_subtitle_pairs.append({
                    'audio': None, 'subtitle': None,
                    'index': idx, 'scene_to_show': current_active_scene,
                    'chapter': 1, 'scene': 1,
                    'choice_node': cn
                })
                continue

            audio = audio_by_index.get(idx)
            subtitle = subtitle_by_index.get(idx)
            raw_scene_id = audio.get('scene_id') if audio else subtitle.get('scene_id') if subtitle else 'default'
            if raw_scene_id and raw_scene_id != 'default' and raw_scene_id in valid_scene_stems:
                current_active_scene = raw_scene_id

            audio_subtitle_pairs.append({
                'audio': audio, 'subtitle': subtitle,
                'index': idx, 'scene_to_show': current_active_scene,
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
            entry = f"{{i:{pair['index']}"
            if audio_name:
                escaped_audio = audio_name.replace("'", "\\'")
                entry += f",a:'{escaped_audio}'"
            escaped_scene = scene_file.replace("'", "\\'")
            entry += f",s:'{escaped_scene}'}}"
            manifest_entries.append(entry)

        lines.append(f"// 游戏脚本 - 第 {chap_id} 章")
        lines.append("")
        lines.append("// ==================== 渐进式资源预加载器 ====================")
        lines.append(f"window._assetManifest = [{','.join(manifest_entries)}];")
        lines.append("")
        lines.append("window.ProgressiveLoader = {")
        lines.append("    WINDOW: 10,")
        lines.append("    loaded: new Set(),")
        lines.append("    load: function(fromIdx, count) {")
        lines.append("        var end = fromIdx + count;")
        lines.append("        var self = this;")
        lines.append("        window._assetManifest.forEach(function(item) {")
        lines.append("            if (item.i >= fromIdx && item.i < end && !self.loaded.has(item.i)) {")
        lines.append("                self.loaded.add(item.i);")
        lines.append("                if (item.a) { var au = new Audio(); au.preload = 'auto'; au.src = 'assets/voices/' + item.a; }")
        lines.append("                if (item.s) { var im = new Image(); im.src = 'assets/scenes/' + item.s; }")
        lines.append("            }")
        lines.append("        });")
        lines.append("    },")
        lines.append("    step: function(idx) { this.load(idx + 1, this.WINDOW); }")
        lines.append("};")
        lines.append("")

        # 音频注册表（Start 时注册，不参与 loading 预加载）
        lines.append("// 音频注册表(Start 时注册,不参与 loading 预加载)")
        lines.append("window._allVoices = {")
        for pair in audio_subtitle_pairs:
            if pair.get('choice_node'):
                cn = pair['choice_node']
                for br_mats in [cn.get('branch_a', []), cn.get('branch_b', [])]:
                    for bm in br_mats:
                        a = bm.get('audio')
                        if a:
                            escaped_stem = a['id'].replace("'", "\\'")
                            escaped_file = a['file'].replace("'", "\\'")
                            lines.append(f"    '{escaped_stem}': '{escaped_file}',")
            elif pair['audio']:
                audio_stem = pair['audio']['id']
                audio_file = pair['audio']['file']
                escaped_stem = audio_stem.replace("'", "\\'")
                escaped_file = audio_file.replace("'", "\\'")
                lines.append(f"    '{escaped_stem}': '{escaped_file}',")
        lines.append("};")
        lines.append("")
        lines.append("window._changePlayerVariableChoices = function(effects, sign) {")
        lines.append("    window._playerVariableChoices = window._playerVariableChoices || {};")
        lines.append("    Object.entries(effects || {}).forEach(([key, value]) => {")
        lines.append("        const delta = Number(value || 0);")
        lines.append("        if (delta === 0) return;")
        lines.append("        const stat = window._playerVariableChoices[key] || (window._playerVariableChoices[key] = {positive: 0, effective: 0});")
        lines.append("        if (sign > 0) { stat.effective += 1; if (delta > 0) stat.positive += 1; }")
        lines.append("        else { stat.effective = Math.max(0, stat.effective - 1); if (delta > 0) stat.positive = Math.max(0, stat.positive - 1); }")
        lines.append("    });")
        lines.append("};")
        lines.append("")
        lines.append("window._changePlayerLegacyFlags = function(flags, sign) {")
        lines.append("    window._playerLegacyFlags = window._playerLegacyFlags || {};")
        lines.append("    Object.entries(flags || {}).forEach(([key, value]) => {")
        lines.append("        const delta = sign * Math.max(0, Number(value || 0));")
        lines.append("        window._playerLegacyFlags[key] = Math.max(0, Number(window._playerLegacyFlags[key] || 0) + delta);")
        lines.append("    });")
        lines.append("};")
        lines.append("")

        # 分支效果注册表（用于退出时回溯数值）
        lines.append("// 分支效果注册表(用于退出时回溯数值)")
        lines.append("window._branchEffects = {")
        for pair in audio_subtitle_pairs:
            if pair.get('choice_node'):
                cn = pair['choice_node']
                idx = pair['index']
                effects_a = cn.get('effects', {}).get('a', {})
                effects_b = cn.get('effects', {}).get('b', {})
                if effects_a:
                    effects_a_js = json.dumps(effects_a, ensure_ascii=False)
                    lines.append(f"    '{idx}_A': {effects_a_js},")
                if effects_b:
                    effects_b_js = json.dumps(effects_b, ensure_ascii=False)
                    lines.append(f"    '{idx}_B': {effects_b_js},")
        lines.append("};")
        lines.append("")
        lines.append("window._branchLegacyFlags = {")
        for pair in audio_subtitle_pairs:
            if pair.get('choice_node'):
                cn = pair['choice_node']
                idx = pair['index']
                flags_a = cn.get('legacy_flags', {}).get('a', {})
                flags_b = cn.get('legacy_flags', {}).get('b', {})
                if flags_a:
                    lines.append(f"    '{idx}_A': {json.dumps(flags_a, ensure_ascii=False)},")
                if flags_b:
                    lines.append(f"    '{idx}_B': {json.dumps(flags_b, ensure_ascii=False)},")
        lines.append("};")
        lines.append("")

        lines.append("monogatari.script({")

        # ==================== Start 标签 ====================
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
        lines.append(f"                // ── 数值初始化（Continue 时内存已有变量，跳过初始化） ──")
        lines.append("                const _initVars = " + init_vars_js + ";")
        lines.append(f"                if (!window._playerVariables || !window._playerVariableChoices || !window._playerLegacyFlags) {{")
        lines.append(f"                    (function() {{")
        lines.append(f"                        let vars = null;")
        lines.append(f"                        let choiceStats = null;")
        lines.append(f"                        let legacyFlags = null;")
        lines.append(f"                        try {{")
        lines.append(f"                            const urlParams = new URLSearchParams(window.location.search);")
        lines.append(f"                            const urlVars = urlParams.get('vars');")
        lines.append(f"                            if (urlVars) vars = JSON.parse(decodeURIComponent(urlVars));")
        lines.append(f"                            const urlChoiceStats = urlParams.get('choice_stats');")
        lines.append(f"                            if (urlChoiceStats) choiceStats = JSON.parse(decodeURIComponent(urlChoiceStats));")
        lines.append(f"                            const urlLegacyFlags = urlParams.get('legacy_flags');")
        lines.append(f"                            if (urlLegacyFlags) legacyFlags = JSON.parse(decodeURIComponent(urlLegacyFlags));")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                        if (!vars) {{")
        lines.append(f"                            try {{")
        lines.append(f"                                const snap = localStorage.getItem('_chapterEntryVars_{node_id}');")
        lines.append(f"                                if (snap) vars = JSON.parse(snap);")
        lines.append(f"                            }} catch(e) {{}}")
        lines.append(f"                        }}")
        lines.append(f"                        if (!legacyFlags) {{")
        lines.append(f"                            try {{")
        lines.append(f"                                const snap = localStorage.getItem('_chapterEntryLegacyFlags_{node_id}');")
        lines.append(f"                                if (snap) legacyFlags = JSON.parse(snap);")
        lines.append(f"                            }} catch(e) {{}}")
        lines.append(f"                        }}")
        lines.append(f"                        if (!choiceStats) {{")
        lines.append(f"                            try {{")
        lines.append(f"                                const snap = localStorage.getItem('_chapterEntryChoiceStats_{node_id}');")
        lines.append(f"                                if (snap) choiceStats = JSON.parse(snap);")
        lines.append(f"                            }} catch(e) {{}}")
        lines.append(f"                        }}")
        lines.append(f"                        if (!vars) {{")
        lines.append(f"                            try {{")
        lines.append("                                const lsState = JSON.parse(localStorage.getItem('" + launcher_key + "') || '{}');")
        lines.append(f"                                if (lsState.globalVars) vars = Object.assign({{}}, lsState.globalVars);")
        lines.append(f"                                if (lsState.globalChoiceStats) choiceStats = JSON.parse(JSON.stringify(lsState.globalChoiceStats));")
        lines.append(f"                                if (lsState.globalLegacyFlags) legacyFlags = Object.assign({{}}, lsState.globalLegacyFlags);")
        lines.append(f"                            }} catch(e) {{}}")
        lines.append(f"                        }}")
        lines.append(f"                        if (!vars) vars = Object.assign({{}}, _initVars);")
        lines.append(f"                        window._playerVariables = vars;")
        lines.append(f"                        window._playerVariableChoices = choiceStats || {{}};")
        lines.append(f"                        window._playerLegacyFlags = legacyFlags || {{}};")
        lines.append(f"                        try {{ localStorage.setItem('_chapterEntryChoiceStats_{node_id}', JSON.stringify(window._playerVariableChoices)); }} catch(e) {{}}")
        lines.append(f"                        try {{ localStorage.setItem('_chapterEntryLegacyFlags_{node_id}', JSON.stringify(window._playerLegacyFlags)); }} catch(e) {{}}")
        lines.append(f"                        window._chapterStartStats = Object.assign({{}}, vars);")
        lines.append(f"                        if (window.updateStatsUI) window.updateStatsUI();")
        lines.append(f"                        if (window._syncPlayerVars) window._syncPlayerVars();")
        lines.append(f"                    }})();")
        lines.append(f"                }}")
        lines.append("                if (window.GameProgressReporter) {")
        lines.append("                    window.GameProgressReporter.startAutoSave();")
        lines.append(f"                    window.GameProgressReporter.update({min_steps});")
        lines.append("                }")
        lines.append("                ")
        lines.append("                // 【Back按钮修复】Start标签禁用Back按钮")
        lines.append("                setTimeout(() => {")
        lines.append("                    const backBtn = document.querySelector('[data-action=\"back\"]');")
        lines.append("                    if (backBtn) {")
        lines.append("                        backBtn.setAttribute('disabled', 'true');")
        lines.append("                        backBtn.style.opacity = '0.3';")
        lines.append("                        backBtn.style.pointerEvents = 'none';")
        lines.append("                        backBtn.style.cursor = 'not-allowed';")
        lines.append("                    }")
        lines.append("                }, 150);")
        lines.append("                ")
        lines.append("                // 注册全部音频到 Monogatari")
        lines.append("                if (window._allVoices) { monogatari.assets('voices', window._allVoices); }")
        lines.append(f"                if (window.ProgressiveLoader) window.ProgressiveLoader.load({min_steps}, 10);")
        lines.append("                return true;")
        lines.append("            }, ")
        lines.append("            'Reverse': () => { ")
        lines.append("                setTimeout(() => {")
        lines.append("                    const backBtn = document.querySelector('[data-action=\"back\"]');")
        lines.append("                    if (backBtn) {")
        lines.append("                        backBtn.setAttribute('disabled', 'true');")
        lines.append("                        backBtn.style.opacity = '0.3';")
        lines.append("                        backBtn.style.pointerEvents = 'none';")
        lines.append("                        backBtn.style.cursor = 'not-allowed';")
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
        lines.append("                ")
        lines.append("                // 注册全部音频到 Monogatari")
        lines.append("                if (window._allVoices) { monogatari.assets('voices', window._allVoices); }")
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
        lines.append("                return true;")
        lines.append("            }, ")
        lines.append("            'Reverse': () => { return true; } ")
        lines.append("        }},")
        lines.append("        {'Function':{ ")
        lines.append("            'Apply': () => { ")
        lines.append("                const idx = window._continueTargetIndex || 1;")
        lines.append("                return `jump Material_${idx}`;")
        lines.append("            }, ")
        lines.append("            'Reverse': () => { return true; } ")
        lines.append("        }},")
        lines.append("    ],")
        lines.append("")

        # ==================== 素材循环 ====================
        sorted_music = sorted(self.music_files, key=lambda x: x['start'])
        current_bgm = None

        # 记录上一步实际显示的图，用来判断要不要加特效
        previous_shown_image = None

        for pair_idx, pair in enumerate(audio_subtitle_pairs):
            idx = pair['index']
            audio = pair['audio']
            subtitle = pair['subtitle']
            scene_to_show = pair['scene_to_show']
            prev_idx = audio_subtitle_pairs[pair_idx - 1]['index'] if pair_idx > 0 else idx
            next_idx_val = audio_subtitle_pairs[pair_idx + 1]['index'] if pair_idx < len(audio_subtitle_pairs) - 1 else None

            # ── 软分支节点 ──────────────────────────────────────────────
            if pair.get('choice_node'):
                cn = pair['choice_node']
                opt_a = cn.get('option_a_text', '选择A').replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
                opt_b = cn.get('option_b_text', '选择B').replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
                effects_a = cn.get('effects', {}).get('a', {})
                effects_b = cn.get('effects', {}).get('b', {})
                legacy_flags_a = cn.get('legacy_flags', {}).get('a', {})
                legacy_flags_b = cn.get('legacy_flags', {}).get('b', {})

                has_scene_image = any(img['stem'] == scene_to_show for img in self.scene_images)
                transition_suffix = " with fadeIn" if scene_to_show != previous_shown_image else ""
                if scene_to_show != previous_shown_image:
                    previous_shown_image = scene_to_show

                lines.append(f"    'Material_{idx}': [")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ const chosen = localStorage.getItem('_branchChosen_{node_id}_{idx}'); if(chosen === 'A') return 'jump BranchA_{idx}'; if(chosen === 'B') return 'jump BranchB_{idx}'; return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                if has_scene_image:
                    lines.append(f"        'show scene {scene_to_show}{transition_suffix}',")
                else:
                    lines.append(f"        'show scene #000000{transition_suffix}',")

                lines.append(f"        {{'Choice': {{")
                lines.append(f"            'BranchA_{idx}': {{")
                lines.append(f"                'Text': '{opt_a}',")
                lines.append(f"                'Do': 'jump ChoiceExec_{idx}_A',")
                lines.append(f"            }},")
                lines.append(f"            'BranchB_{idx}': {{")
                lines.append(f"                'Text': '{opt_b}',")
                lines.append(f"                'Do': 'jump ChoiceExec_{idx}_B',")
                lines.append(f"            }}")
                lines.append(f"        }}}},")
                lines.append(f"    ],")
                lines.append(f"")

                for br_letter, effects, legacy_flags in [
                    ('A', effects_a, legacy_flags_a),
                    ('B', effects_b, legacy_flags_b),
                ]:
                    eff_js = json.dumps(effects or {}, ensure_ascii=False)
                    legacy_flags_js = json.dumps(legacy_flags or {}, ensure_ascii=False)
                    lines.append(f"    'ChoiceExec_{idx}_{br_letter}': [")
                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ localStorage.setItem('_branchChosen_{node_id}_{idx}', '{br_letter}'); const eff={eff_js}; const legacyFlags={legacy_flags_js}; Object.entries(eff).forEach(([k,v])=>{{ window._playerVariables=window._playerVariables||{{}}; window._playerVariables[k]=Math.max(0,Math.min(100,(window._playerVariables[k]||0)+v)); }}); if(window._changePlayerVariableChoices) window._changePlayerVariableChoices(eff, 1); if(window._changePlayerLegacyFlags) window._changePlayerLegacyFlags(legacyFlags, 1); if(window._syncPlayerVars) window._syncPlayerVars(); if(window.updateStatsUI) window.updateStatsUI(); return true; }}, 'Reverse': () => {{ const eff={eff_js}; const legacyFlags={legacy_flags_js}; Object.entries(eff).forEach(([k,v])=>{{ window._playerVariables=window._playerVariables||{{}}; window._playerVariables[k]=Math.max(0,Math.min(100,(window._playerVariables[k]||0)-v)); }}); if(window._changePlayerVariableChoices) window._changePlayerVariableChoices(eff, -1); if(window._changePlayerLegacyFlags) window._changePlayerLegacyFlags(legacyFlags, -1); if(window._syncPlayerVars) window._syncPlayerVars(); if(window.updateStatsUI) window.updateStatsUI(); return true; }} }} }},")
                    lines.append(f"        'jump Branch{br_letter}_{idx}',")
                    lines.append(f"    ],")
                    lines.append(f"")

                # BranchA
                lines.append(f"    'BranchA_{idx}': [")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ window._inBranch_{idx} = 'A'; console.log('🔀 进入 BranchA_{idx}'); return true; }}, 'Reverse': () => {{ delete window._inBranch_{idx}; return true; }} }} }},")
                branch_a_items = cn.get('branch_a', [])
                if not branch_a_items:
                    # 如果分支为空，添加一个占位符避免卡住
                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ console.log('⚠️ BranchA_{idx} 为空'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                for bm in branch_a_items:
                    ba = bm.get('audio')
                    bs = bm.get('subtitle')
                    if ba:
                        lines.append(f"        'play voice {ba['id']}',")
                    if bs and bs.get('dialogues'):
                        for dlg in bs['dialogues']:
                            spk = dlg['speaker']
                            esc_spk = spk.replace("'", "\\'")
                            cid = self.chinese_to_id.get(spk, 'nb')
                            if spk == '旁白': cid = 'nb'
                            lines.append(f"        {{'Function':{{ 'Apply': () => {{ updateStage('{esc_spk}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                            txt = dlg['text'].replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').strip()
                            if txt:
                                if cid == 'nb':
                                    lines.append(f"        '{txt}',")
                                else:
                                    lines.append(f"        '{cid} {txt}',")
                    # 如果这个材料既没有音频也没有字幕，添加调试信息
                    if not ba and not (bs and bs.get('dialogues')):
                        seq_val = bm.get('seq', 'unknown')
                        lines.append(f"        {{'Function':{{ 'Apply': () => {{ console.log('⚠️ BranchA_{idx} 材料 seq={seq_val} 为空'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                lines.append(f"        'jump BranchMerge_{idx}',")
                lines.append(f"    ],")
                lines.append(f"")

                # BranchB
                lines.append(f"    'BranchB_{idx}': [")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ window._inBranch_{idx} = 'B'; console.log('🔀 进入 BranchB_{idx}'); return true; }}, 'Reverse': () => {{ delete window._inBranch_{idx}; return true; }} }} }},")
                branch_b_items = cn.get('branch_b', [])
                if not branch_b_items:
                    # 如果分支为空，添加一个占位符避免卡住
                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ console.log('⚠️ BranchB_{idx} 为空'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                for bm in branch_b_items:
                    ba = bm.get('audio')
                    bs = bm.get('subtitle')
                    if ba:
                        lines.append(f"        'play voice {ba['id']}',")
                    if bs and bs.get('dialogues'):
                        for dlg in bs['dialogues']:
                            spk = dlg['speaker']
                            esc_spk = spk.replace("'", "\\'")
                            cid = self.chinese_to_id.get(spk, 'nb')
                            if spk == '旁白': cid = 'nb'
                            lines.append(f"        {{'Function':{{ 'Apply': () => {{ updateStage('{esc_spk}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                            txt = dlg['text'].replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').strip()
                            if txt:
                                if cid == 'nb':
                                    lines.append(f"        '{txt}',")
                                else:
                                    lines.append(f"        '{cid} {txt}',")
                    # 如果这个材料既没有音频也没有字幕，添加调试信息
                    if not ba and not (bs and bs.get('dialogues')):
                        seq_val = bm.get('seq', 'unknown')
                        lines.append(f"        {{'Function':{{ 'Apply': () => {{ console.log('⚠️ BranchB_{idx} 材料 seq={seq_val} 为空'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                lines.append(f"        'jump BranchMerge_{idx}',")
                lines.append(f"    ],")
                lines.append(f"")

                # BranchMerge
                merge_target = f"Material_{next_idx_val}" if next_idx_val else "EndChapter"
                lines.append(f"    'BranchMerge_{idx}': [")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ delete window._inBranch_{idx}; if(window.StageManager) window.StageManager.reset(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                lines.append(f"        'jump {merge_target}',")
                lines.append(f"    ],")
                lines.append(f"")
                continue

            # ── 普通节点 ────────────────────────────────────────────────
            lines.append(f"    'Material_{idx}': [")

            # 1. 状态重置 + Back按钮控制 + 特效清理
            lines.append(f"        {{'Function':{{ ")
            lines.append(f"            'Apply': () => {{")
            lines.append(f"                // 清理上一个beat的持续特效")
            lines.append(f"                if (window.AtmosphereEffects) window.AtmosphereEffects.clearAll();")
            lines.append(f"                if (monogatari.global) {{")
            lines.append(f"                    monogatari.global('auto_play', false);")
            lines.append(f"                    monogatari.global('skipping', false);")
            lines.append(f"                }}")
            lines.append(f"                document.body.removeAttribute('data-action');")
            lines.append(f"                setTimeout(() => {{")
            lines.append(f"                    const currentIdx = {idx};")
            lines.append(f"                    const startIdx = window._sessionStartIndex || 1;")
            lines.append(f"                    const backBtn = document.querySelector('[data-action=\"back\"]');")
            lines.append(f"                    if (backBtn) {{")
            lines.append(f"                        if (currentIdx <= startIdx) {{")
            lines.append(f"                            backBtn.setAttribute('disabled', 'true');")
            lines.append(f"                            backBtn.style.opacity = '0.3';")
            lines.append(f"                            backBtn.style.pointerEvents = 'none';")
            lines.append(f"                            backBtn.style.cursor = 'not-allowed';")
            lines.append(f"                        }} else {{")
            lines.append(f"                            backBtn.removeAttribute('disabled');")
            lines.append(f"                            backBtn.style.opacity = '1';")
            lines.append(f"                            backBtn.style.pointerEvents = 'auto';")
            lines.append(f"                            backBtn.style.cursor = 'pointer';")
            lines.append(f"                        }}")
            lines.append(f"                    }}")
            lines.append(f"                }}, 150);")
            lines.append(f"                if(window.GameProgressReporter) window.GameProgressReporter.update({idx});")
            lines.append(f"                if(window.ProgressiveLoader) window.ProgressiveLoader.step({idx});")
            lines.append(f"                return true;")
            lines.append(f"            }}, ")
            lines.append(f"            'Reverse': () => {{")
            lines.append(f"                setTimeout(() => {{")
            lines.append(f"                    const prevIdx = {prev_idx};")
            lines.append(f"                    const startIdx = window._sessionStartIndex || 1;")
            lines.append(f"                    const backBtn = document.querySelector('[data-action=\"back\"]');")
            lines.append(f"                    if (backBtn) {{")
            lines.append(f"                        if (prevIdx <= startIdx) {{")
            lines.append(f"                            backBtn.setAttribute('disabled', 'true');")
            lines.append(f"                            backBtn.style.opacity = '0.3';")
            lines.append(f"                            backBtn.style.pointerEvents = 'none';")
            lines.append(f"                            backBtn.style.cursor = 'not-allowed';")
            lines.append(f"                        }} else {{")
            lines.append(f"                            backBtn.removeAttribute('disabled');")
            lines.append(f"                            backBtn.style.opacity = '1';")
            lines.append(f"                            backBtn.style.pointerEvents = 'auto';")
            lines.append(f"                            backBtn.style.cursor = 'pointer';")
            lines.append(f"                        }}")
            lines.append(f"                    }}")
            lines.append(f"                }}, 150);")
            lines.append(f"                return true;")
            lines.append(f"            }} ")
            lines.append(f"        }}}},")

            # 2. 音乐控制
            if current_bgm is not None and idx > current_bgm['end']:
                bgm_stem = current_bgm['stem']
                lines.append(f"        'stop music {bgm_stem} with fade 3',")
                current_bgm = None
            if current_bgm is None:
                for music in sorted_music:
                    if music['start'] <= idx <= music['end']:
                        music_stem = music['stem']
                        lines.append(f"        'play music {music_stem} with fade 3 loop',")
                        current_bgm = music
                        break

            # 3. SFX 控制
            if idx in sfx_by_index:
                lines.append(f"        'stop sound',")
                for sfx in sfx_by_index[idx]:
                    sfx_stem = sfx['stem']
                    lines.append(f"        'play sound {sfx_stem}',")

            # 4. 场景控制
            has_scene_image = any(img['stem'] == scene_to_show for img in self.scene_images)
            transition_suffix = ""
            if scene_to_show != previous_shown_image:
                transition_suffix = " with fadeIn"
                previous_shown_image = scene_to_show
            if has_scene_image:
                lines.append(f"        'show scene {scene_to_show}{transition_suffix}',")
            else:
                lines.append(f"        'show scene #000000{transition_suffix}',")

            # 5. 播放音频
            if audio:
                lines.append(f"        'play voice {audio['id']}',")

            # 5.5. 特效插入（在音频播放后，对话显示前）
            effects = AtmosphereEffects.get_effects_for_beat(
                self.atmosphere_config, chap_id, idx
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

            # 6. 序章钩子处理
            is_hook_beat = hook_id is not None and idx <= hook_id
            if is_hook_beat:
                lines.append(f"        {{'Function':{{ 'Apply': async () => {{")
                lines.append(f"            if (!window.HookManager || !window.HookManager.active) await window.HookManager.enter();")
                if subtitle and subtitle.get('dialogues'):
                    for dlg in subtitle['dialogues']:
                        txt = dlg['text'].replace("\\", "\\\\").replace("`", "\\`").replace("${{", "${{'{{'}}").strip()
                        if txt:
                            lines.append(f"            await window.HookManager.setText(`{txt}`);")
                if hook_id is not None and idx == hook_id:
                    lines.append(f"            await window.HookManager.exit();")
                lines.append(f"            return true;")
                lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{")
                lines.append(f"            if (monogatari.global) {{")
                lines.append(f"                monogatari.global('auto_play', false);")
                lines.append(f"                monogatari.global('skipping', false);")
                lines.append(f"            }}")
                lines.append(f"            return new Promise(() => {{}});")
                lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
            else:
                # 7. 对话处理（普通模式）
                has_blocking_action = False
                if subtitle and subtitle.get('dialogues'):
                    for dialogue in subtitle['dialogues']:
                        speaker = dialogue['speaker']
                        raw_key = dialogue.get('raw_key')
                        text = dialogue['text']
                        escaped_speaker = speaker.replace("'", "\\'")

                        specific_image = None
                        if raw_key and hasattr(self, 'valid_image_stems') and raw_key in self.valid_image_stems:
                            specific_image = raw_key + ".png"

                        if specific_image:
                            escaped_specific_image = specific_image.replace("'", "\\'")
                            lines.append(
                                f"        {{'Function':{{ 'Apply': () => {{ StageManager.forceImage('{escaped_speaker}', '{escaped_specific_image}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                        else:
                            lines.append(
                                f"        {{'Function':{{ 'Apply': () => {{ updateStage('{escaped_speaker}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

                        char_id_for_text = self.chinese_to_id.get(speaker, 'nb')
                        if speaker == '旁白': char_id_for_text = 'nb'

                        text_display = text
                        for tag in ["(QTE)", "（QTE）", "(TIMING)", "（TIMING）", "(MEMORY)", "（MEMORY）"]:
                            text_display = text_display.replace(tag, "")

                        import re as _re
                        interaction_match = _re.search(r'(?:[\(\[（\s]*)INTERACTION\s*[:：]\s*(.*)$', text_display, _re.DOTALL | _re.IGNORECASE)
                        if interaction_match:
                            text_display = text_display.replace(interaction_match.group(0), "")

                        text_clean = text_display.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').strip()

                        if text_clean:
                            if char_id_for_text == 'nb':
                                lines.append(f"        '{text_clean}',")
                            else:
                                lines.append(f"        '{char_id_for_text} {text_clean}',")
                            has_blocking_action = True
                            lines.append(f"        {{'Function':{{ 'Apply': () => {{")
                            lines.append(f"            if (monogatari.global) {{")
                            lines.append(f"                monogatari.global('auto_play', false);")
                            lines.append(f"                monogatari.global('skipping', false);")
                            lines.append(f"            }}")
                            lines.append(f"            return true;")
                            lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")

                if not has_blocking_action:
                    lines.append(f"        'nb ...',")

            # 8. 进度上报
            lines.append(
                f"        {{'Function':{{ 'Apply': () => {{ if(window.GameProgressReporter) window.GameProgressReporter.sendReport('step_progress'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

            # 9. 跳转
            if next_idx_val is not None:
                lines.append(f"        'jump Material_{next_idx_val}',")
            else:
                lines.append(f"        'jump EndChapter',")

            lines.append(f"    ],")

        # 结尾逻辑
        lines.append(f"    'EndChapter': [")
        lines.append(f"        '␟ui:chapterComplete␟',")
        if node_data and node_data.get('children') and folder_mapping:
            children = node_data['children']
            all_have_empty_choice = all(not child.get('choice_text', '').strip() for child in children)

            if all_have_empty_choice and branch_thresholds:
                # ── 自动路由：所有孩子的 choice_text 为空，根据变量最大值路由 ──
                thresholds_json = json.dumps(branch_thresholds, ensure_ascii=False)
                auto_folder_map = {child['child_id']: folder_mapping.get(child['child_id'], '') for child in children}
                auto_folder_map_json = json.dumps(auto_folder_map, ensure_ascii=False)
                escaped_launcher_key = (launcher_key or '').replace("'", "\\'")
                fallback_child_id = next(
                    (
                        child['child_id']
                        for child in children
                        if not (branch_thresholds or {}).get(child['child_id'], {}).get('variable')
                    ),
                    children[0]['child_id'],
                )
                lines.append(f"        {{'Function':{{ 'Apply': () => {{")
                lines.append(f"            const vars = window._playerVariables || {{}};")
                lines.append(f"            const choiceStats = window._playerVariableChoices || {{}};")
                lines.append(f"            const legacyFlags = window._playerLegacyFlags || {{}};")
                lines.append(f"            let maxVar = null, maxVal = -Infinity;")
                lines.append(f"            for (const [k, v] of Object.entries(vars)) {{")
                lines.append(f"                if (typeof v === 'number' && v > maxVal) {{ maxVal = v; maxVar = k; }}")
                lines.append(f"            }}")
                lines.append(f"            console.log('[AutoRoute] maxVar=' + maxVar + ' maxVal=' + maxVal);")
                lines.append(f"            const thresholds = {thresholds_json};")
                lines.append(f"            let targetId = null;")
                lines.append(f"            for (const [childId, cfg] of Object.entries(thresholds)) {{")
                lines.append(f"                const routeEnabled = cfg.route_enabled !== false;")
                lines.append(f"                const stat = choiceStats[cfg.variable] || {{positive: 0, effective: 0}};")
                lines.append(f"                const positive = Number(stat.positive || 0), effective = Number(stat.effective || 0);")
                lines.append(f"                const ratioThreshold = Number(cfg.ratio_threshold == null ? 0.65 : cfg.ratio_threshold);")
                lines.append(f"                const marginRatio = Number(cfg.margin_ratio == null ? 0.10 : cfg.margin_ratio);")
                lines.append(f"                const minimumEffectiveChoices = Number(cfg.minimum_effective_choices == null ? 3 : cfg.minimum_effective_choices);")
                lines.append(f"                const tendency = effective > 0 ? positive / effective : 0;")
                lines.append(f"                const competitorTendency = Math.max(...Object.entries(choiceStats).filter(([key]) => key !== cfg.variable).map(([, other]) => {{ const count=Number((other||{{}}).effective||0); return count>0 ? Number((other||{{}}).positive||0)/count : 0; }}), 0);")
                lines.append(f"                const requiredLegacyFlags = cfg.required_legacy_flags || [];")
                lines.append(f"                const legacyFlagsSatisfied = requiredLegacyFlags.every(flag => Number(legacyFlags[flag] || 0) > 0);")
                lines.append(f"                const routeValue = Number(vars[cfg.variable] || 0);")
                lines.append(f"                if (routeEnabled && routeValue >= cfg.threshold && effective >= minimumEffectiveChoices && tendency >= ratioThreshold && tendency - competitorTendency >= marginRatio && legacyFlagsSatisfied) {{")
                lines.append(f"                    console.log('[AutoRoute] matched child=' + childId);")
                lines.append(f"                    targetId = childId;")
                lines.append(f"                    break;")
                lines.append(f"                }}")
                lines.append(f"            }}")
                lines.append(f"            if (!targetId) targetId = '{fallback_child_id}';")
                lines.append(f"            const folderMap = {auto_folder_map_json};")
                lines.append(f"            const targetFolder = folderMap[targetId] || '';")
                lines.append(f"            const varsStr = encodeURIComponent(JSON.stringify(vars));")
                lines.append(f"            const choiceStatsStr = encodeURIComponent(JSON.stringify(choiceStats));")
                lines.append(f"            const legacyFlagsStr = encodeURIComponent(JSON.stringify(legacyFlags));")
                lines.append(f"            try {{")
                lines.append(f"                const state = JSON.parse(localStorage.getItem('{escaped_launcher_key}') || '{{}}');")
                lines.append(f"                state.chapters = state.chapters || {{}};")
                lines.append(f"                state.currentChapter = targetFolder;")
                lines.append(f"                state.currentNodeId = targetId;")
                lines.append(f"                state.globalVars = Object.assign({{}}, vars);")
                lines.append(f"                state.globalChoiceStats = JSON.parse(JSON.stringify(choiceStats));")
                lines.append(f"                state.globalLegacyFlags = Object.assign({{}}, legacyFlags);")
                lines.append(f"                localStorage.setItem('{escaped_launcher_key}', JSON.stringify(state));")
                lines.append(f"            }} catch (e) {{}}")
                lines.append(f"            if (targetFolder) {{")
                lines.append(f"                window.showToast('Loading...');")
                lines.append(f"                setTimeout(() => {{")
                lines.append(f"                    window.location.href = '../' + targetFolder + '/index.html?vars=' + varsStr + '&choice_stats=' + choiceStatsStr + '&legacy_flags=' + legacyFlagsStr + '&lang=' + encodeURIComponent(window._lang || window._defaultLang || 'en-US');")
                lines.append(f"                }}, 300);")
                lines.append(f"            }}")
                lines.append(f"            return true;")
                lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
                lines.append(f"    ],")
                lines.append(f"")
            else:
                # ── 硬分支：有孩子的 choice_text 不为空，显示选择菜单 ──
                lines.append(f"        '␟ui:makeChoice␟',")
                lines.append(f"        {{")
                lines.append(f"            'Choice': {{")
                for child in children:
                    choice_text = child['choice_text']
                    choice_text = _localized_choice_text(child.get('child_id'), choice_text)
                    escaped_choice_text = choice_text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"")
                    child_node_id = child['child_id']
                    target_folder = folder_mapping.get(child_node_id)
                    if target_folder:
                        lines.append(f"                '{child_node_id}': {{")
                        lines.append(f"                    'Text': '{escaped_choice_text}',")
                        lines.append(f"                    'Do': 'jump WaitForJump_{child_node_id}',")
                        lines.append(f"                }},")
                lines.append(f"            }}")
                lines.append(f"        }},")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{")
                lines.append(f"            if (monogatari.global) {{")
                lines.append(f"                monogatari.global('auto_play', false);")
                lines.append(f"                monogatari.global('skipping', false);")
                lines.append(f"            }}")
                lines.append(f"            return new Promise(() => {{}});")
                lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
                lines.append(f"    ],")
                lines.append(f"")

                # 为每个硬分支孩子创建 WaitForJump 标签
                for child in children:
                    choice_text = child['choice_text']
                    choice_text = _localized_choice_text(child.get('child_id'), choice_text)
                    escaped_choice_text = choice_text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"")
                    child_node_id = child['child_id']
                    target_folder = folder_mapping.get(child_node_id)
                    escaped_target_folder = target_folder.replace("'", "\\'") if target_folder else ''
                    if target_folder:
                        lines.append(f"    'WaitForJump_{child_node_id}': [")
                        lines.append(f"        {{'Function':{{ 'Apply': () => {{")
                        lines.append(f"            console.log('[Choice] ========== 用户点击选择 ==========');")
                        lines.append(f"            console.log('[Choice] 选择文本: {escaped_choice_text}');")
                        lines.append(f"            console.log('[Choice] 目标节点: {child_node_id}');")
                        escaped_launcher_key = (launcher_key or '').replace("'", "\\'")
                        lines.append(f"            console.log('[Choice] 目标文件夹: {escaped_target_folder}');")
                        lines.append(f"            const _vars = Object.assign({{}}, window._playerVariables || {{}});")
                        lines.append(f"            const _varsStr = encodeURIComponent(JSON.stringify(_vars));")
                        lines.append(f"            const _choiceStats = JSON.parse(JSON.stringify(window._playerVariableChoices || {{}}));")
                        lines.append(f"            const _choiceStatsStr = encodeURIComponent(JSON.stringify(_choiceStats));")
                        lines.append(f"            const _legacyFlags = Object.assign({{}}, window._playerLegacyFlags || {{}});")
                        lines.append(f"            const _legacyFlagsStr = encodeURIComponent(JSON.stringify(_legacyFlags));")
                        lines.append(f"            try {{")
                        lines.append(f"                const _state = JSON.parse(localStorage.getItem('{escaped_launcher_key}') || '{{}}');")
                        lines.append(f"                _state.chapters = _state.chapters || {{}};")
                        lines.append(f"                _state.currentChapter = '{escaped_target_folder}';")
                        lines.append(f"                _state.currentNodeId = '{child_node_id}';")
                        lines.append(f"                _state.globalVars = _vars;")
                        lines.append(f"                _state.globalChoiceStats = _choiceStats;")
                        lines.append(f"                _state.globalLegacyFlags = _legacyFlags;")
                        lines.append(f"                localStorage.setItem('{escaped_launcher_key}', JSON.stringify(_state));")
                        lines.append(f"            }} catch (e) {{}}")
                        lines.append(f"            window.showToast('Loading...');")
                        lines.append(f"            setTimeout(() => {{")
                        lines.append(f"                window.location.href = '../{escaped_target_folder}/index.html?vars=' + _varsStr + '&choice_stats=' + _choiceStatsStr + '&legacy_flags=' + _legacyFlagsStr + '&lang=' + encodeURIComponent(window._lang || window._defaultLang || 'en-US');")
                        lines.append(f"            }}, 300);")
                        lines.append(f"            return true;")
                        lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
                        lines.append(f"        'jump EndChapter',")
                        lines.append(f"    ],")
                        lines.append(f"")
        else:
            # 如果没有分支选择，执行 end 返回主菜单
            lines.append(f"        'end',")
            lines.append(f"    ],")

        lines.append("});")

        return "\n".join(lines)

    def _get_ui_theme_css(self) -> str:
        """根据 art_style 返回对应的游戏 UI 主题 CSS"""
        themes = {
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
    line-height: 1.9 !important;
}
[data-ui="who"] {
    color: #2a1a08 !important;
    background: rgba(245, 238, 220, 0.9) !important;
    border: 1px solid rgba(80, 50, 20, 0.4) !important;
    font-family: 'Noto Serif SC', 'STKaiti', serif !important;
    letter-spacing: 0.2em !important;
}
[data-component="quick-menu"] {
    background: rgba(245, 238, 220, 0.85) !important;
    border-top: 1px solid rgba(80, 50, 20, 0.2) !important;
}
[data-component="quick-menu"] span { color: #5a3a10 !important; }
[data-component="quick-menu"] span:hover { color: #2a1a08 !important; }
[data-component="choice-container"] button {
    background: rgba(245, 238, 220, 0.9) !important;
    border: 1px solid rgba(80, 50, 20, 0.4) !important;
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
    border-radius: 4px !important;
}
[data-screen="main"] button:hover {
    background: linear-gradient(135deg, rgba(120,80,30,0.95) 0%, rgba(80,50,20,0.9) 100%) !important;
    border-color: rgba(220,180,100,0.8) !important;
}
""",
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
    text-transform: uppercase !important;
}
[data-component="choice-container"] button:hover {
    background: rgba(0, 229, 255, 0.12) !important;
    border-color: #ff2d78 !important;
    color: #ff2d78 !important;
}
[data-screen="main"] button {
    background: rgba(5, 8, 20, 0.88) !important;
    border: 1px solid #00e5ff !important;
    border-top: 2px solid #ff2d78 !important;
    color: #00e5ff !important;
    font-family: 'Share Tech Mono', monospace !important;
    letter-spacing: 0.18em !important;
    text-transform: uppercase !important;
    border-radius: 2px !important;
}
[data-screen="main"] button:hover {
    background: rgba(0, 229, 255, 0.1) !important;
    border-color: #ff2d78 !important;
    color: #ff2d78 !important;
}
""",
            "japanese": """
/* === 日系 UI === */
[data-component="text-box"] {
    background: rgba(255, 252, 255, 0.86) !important;
    border: 1px solid rgba(220, 180, 200, 0.6) !important;
    border-top: 2px solid rgba(220, 150, 180, 0.7) !important;
    backdrop-filter: blur(10px);
}
[data-component="text-box"] p {
    color: #3a2535 !important;
    font-family: 'Noto Sans JP', 'Hiragino Kaku Gothic Pro', sans-serif !important;
    font-size: 1.18rem !important;
    letter-spacing: 0.05em !important;
    line-height: 1.9 !important;
}
[data-ui="who"] {
    color: #fff !important;
    background: linear-gradient(135deg, rgba(240,180,200,0.9), rgba(220,150,180,0.85)) !important;
    border: 1px solid rgba(255,255,255,0.7) !important;
    font-family: 'Noto Sans JP', sans-serif !important;
    letter-spacing: 0.18em !important;
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
}
[data-component="choice-container"] button:hover {
    background: linear-gradient(135deg, rgba(240,180,200,0.9), rgba(220,150,180,0.85)) !important;
    color: #fff !important;
}
[data-screen="main"] button {
    background: linear-gradient(135deg, rgba(240,180,200,0.88) 0%, rgba(220,150,180,0.82) 100%) !important;
    border: 1px solid rgba(255,255,255,0.6) !important;
    color: #fff !important;
    font-family: 'Noto Sans JP', sans-serif !important;
    letter-spacing: 0.2em !important;
    border-radius: 30px !important;
}
[data-screen="main"] button:hover {
    background: linear-gradient(135deg, rgba(220,150,180,0.95) 0%, rgba(200,120,160,0.9) 100%) !important;
}
""",
            "dark_european": """
/* === 欧式暗黑 UI === */
[data-component="text-box"] {
    background: rgba(18, 12, 8, 0.92) !important;
    border: 1px solid rgba(160, 120, 50, 0.5) !important;
    border-top: 2px solid rgba(200, 160, 70, 0.7) !important;
    backdrop-filter: blur(4px);
}
[data-component="text-box"] p {
    color: #d4c4a0 !important;
    font-family: 'IM Fell English', 'Palatino Linotype', serif !important;
    font-size: 1.18rem !important;
    letter-spacing: 0.04em !important;
    line-height: 1.85 !important;
}
[data-ui="who"] {
    color: #c8a84a !important;
    background: rgba(30, 20, 8, 0.95) !important;
    border: 1px solid rgba(200, 160, 70, 0.6) !important;
    font-family: 'IM Fell English', 'Palatino Linotype', serif !important;
    letter-spacing: 0.15em !important;
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
}
[data-component="choice-container"] button:hover {
    background: rgba(200, 160, 70, 0.15) !important;
    border-color: rgba(200, 160, 70, 0.9) !important;
    color: #e8c870 !important;
}
[data-screen="main"] button {
    background: rgba(18, 12, 8, 0.95) !important;
    border: 1px solid rgba(200, 160, 70, 0.55) !important;
    color: #c8a84a !important;
    font-family: 'IM Fell English', 'Palatino Linotype', serif !important;
    letter-spacing: 0.2em !important;
    border-radius: 2px !important;
}
[data-screen="main"] button:hover {
    background: rgba(200, 160, 70, 0.12) !important;
    border-color: rgba(200, 160, 70, 0.9) !important;
    color: #e8c870 !important;
}
""",
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
    backdrop-filter: blur(8px) !important;
}
[data-component="choice-container"] button:hover {
    background: rgba(255, 255, 255, 0.16) !important;
    border-color: rgba(255, 255, 255, 0.45) !important;
    color: #fff !important;
}
[data-screen="main"] button {
    background: rgba(255, 255, 255, 0.08) !important;
    border: 1px solid rgba(255, 255, 255, 0.2) !important;
    color: #f0f0f0 !important;
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif !important;
    letter-spacing: 0.1em !important;
    backdrop-filter: blur(12px) !important;
    box-shadow: none !important;
    border-radius: 6px !important;
}
[data-screen="main"] button:hover {
    background: rgba(255, 255, 255, 0.16) !important;
    border-color: rgba(255, 255, 255, 0.5) !important;
    color: #fff !important;
}
""",
        }
        return themes.get(self.art_style, themes["modern_realism"])

    def _get_hook_css(self) -> str:
        """根据 art_style 返回对应的序章钩子 CSS"""
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
        variants = {
            "ink_chinese": common + """
#hook-overlay { background: #f5eedc; }
#hook-text-content { max-width: 68%; }
#hook-text-content p {
    color: #1a0e04;
    font-size: clamp(1.2rem, 2.5vw, 1.8rem);
    font-family: 'Noto Serif SC', 'STKaiti', 'KaiTi', serif;
    letter-spacing: 0.22em; line-height: 2.2; text-align: center;
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
            "cyberpunk": common + """
#hook-overlay { background: #020408; }
#hook-text-content { max-width: 72%; }
#hook-text-content p {
    color: #00e5ff;
    font-size: clamp(1.1rem, 2.4vw, 1.75rem);
    font-family: 'Share Tech Mono', 'Courier New', monospace;
    letter-spacing: 0.1em; line-height: 1.8; text-align: center;
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
            "japanese": common + """
#hook-overlay { background: linear-gradient(160deg, #1a0a12 0%, #0d0810 100%); }
#hook-text-content { max-width: 70%; }
#hook-text-content p {
    color: #f8e8f0;
    font-size: clamp(1.15rem, 2.5vw, 1.8rem);
    font-family: 'Noto Sans JP', 'Hiragino Kaku Gothic Pro', sans-serif;
    letter-spacing: 0.12em; line-height: 2.0; text-align: center;
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
            "dark_european": common + """
#hook-overlay { background: #0c0804; }
#hook-text-content { max-width: 68%; }
#hook-text-content p {
    color: #c8a84a;
    font-size: clamp(1.15rem, 2.5vw, 1.85rem);
    font-family: 'IM Fell English', 'Palatino Linotype', serif;
    letter-spacing: 0.1em; line-height: 1.95; text-align: center;
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
            "modern_realism": common + """
#hook-overlay { background: #000; }
#hook-text-content { max-width: 72%; }
#hook-text-content p {
    color: #e8e8e8;
    font-size: clamp(1.3rem, 2.8vw, 2rem);
    font-family: 'Georgia', 'Noto Serif SC', serif;
    letter-spacing: 0.12em; line-height: 1.9; text-align: center;
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
        """生成视觉特效所需的CSS（调整版：人物下沉）"""
        base_css = """
/* ==================== 舞台调度 CSS ==================== */

/* 1. 舞台容器 */
game-screen {
    overflow: hidden;
    position: relative;
}

/* 2. 角色基础样式 */
.character-image {
    position: absolute !important;

    /* 【核心修改点】让人物下沉 */
    /* 0 是刚好对齐底边。如果你觉得太高，就设为负数。*/
    /* -10% 表示向下移动屏幕高度的 10%，你可以根据需要改成 -5% 或 -15% */
    bottom: -30% !important;

    display: block !important;
    visibility: visible !important;
    transition: all 0.6s cubic-bezier(0.25, 1, 0.5, 1);
    will-change: transform, opacity, filter;
    transform-origin: bottom center;

    /* 限制最大高度，防止人物太大顶出屏幕 */
    max-height: 75vh;

    z-index: 10;
    pointer-events: none;
    animation: breathing 4s ease-in-out infinite;
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

/* 3. 位置定义 */
.slot-left {
    left: 5% !important;
    transform: translateX(0);
}

.slot-right {
    right: 5% !important;
    transform: translateX(0) scaleX(-1); /* 镜像翻转 */
}
.slot-right-no-flip {
    right: 5% !important;
    transform: translateX(0);
}

/* 4. 状态定义 */
.state-active {
    opacity: 1;
    filter: brightness(100%);
    z-index: 20;
    transform: scale(1.05); /* 激活时稍微放大 */
}

.slot-right.state-active {
    transform: scale(1.05) scaleX(-1);
}

.state-inactive {
    opacity: 0.6;
    filter: brightness(60%) grayscale(30%);
    z-index: 10;
    /* 不说话时，稍微再缩小一点，并向下沉一点 */
    transform: scale(0.9) translateY(2%);
}

.slot-right.state-inactive {
    transform: scale(0.9) scaleX(-1) translateY(2%);
}

.state-dimmed {
    opacity: 0.5;
    filter: brightness(40%);
    transform: scale(0.85) translateY(5%);
    z-index: 5;
}
.slot-right.state-dimmed {
    transform: scale(0.85) scaleX(-1) translateY(5%);
}

/* 6. 进场/离场动画 */
.state-hidden {
    opacity: 0;
    transform: translateY(50px) scale(0.9); /* 进场时从更低的地方浮上来 */
    pointer-events: none;
}

/* 7. 呼吸动画 */
@keyframes breathing {
    0% { transform: scaleY(1); }
    50% { transform: scaleY(1.02); }
    100% { transform: scaleY(1); }
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
        return base_css + self._get_hook_css() + self._get_ui_theme_css()

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

        css_content = self.generate_visual_styles()

        choice_text_map = {
            str(lang): {str(key): str(value) for key, value in (values or {}).items() if value is not None}
            for lang, values in (self.story_choice_text_by_lang or {}).items()
            if isinstance(values, dict)
        }
        choice_text_js = json.dumps(choice_text_map, ensure_ascii=False)

        lines = []

        # 注入特效库
        lines.append(AtmosphereEffects.generate_effect_library_js())

        lines.append(f"window._defaultLang = {json.dumps(getattr(self, 'story_tree_default_language', 'en-US'), ensure_ascii=False)};")
        lines.append("window._lang = (function(){ try { var q = new URLSearchParams(location.search).get('lang'); if (q) { var l=String(q).toLowerCase(); if (l==='ja'||l.indexOf('ja-')===0) return 'ja-JP'; if (l==='en'||l.indexOf('en-')===0) return 'en-US'; } } catch(e) {} return window._defaultLang || 'en-US'; })();")
        lines.append(f"window._choiceTextByLang = {choice_text_js};")
        lines.append("window.__ui = function(key){ var ja=String(window._lang||'').toLowerCase().indexOf('ja')===0; var m=ja?{makeChoice:'選択してください',chapterComplete:'章完了',skipWarningTitle:'スキップの確認',skipWarningBody:'重要なストーリーを見逃す可能性があります。続行しますか？',confirm:'確認',cancel:'キャンセル',loading:'読み込み中...'}:{makeChoice:'Please make your choice',chapterComplete:'Chapter Complete',skipWarningTitle:'Confirm skip',skipWarningBody:'Skipping may cause you to miss important story content. Continue?',confirm:'Confirm',cancel:'Cancel',loading:'Loading...'}; return m[key] || key; };")
        lines.append("window.I18nScript = { apply: function(){ try { var script=window.monogatari&&monogatari._script; if(!script)return; var cm=window._choiceTextByLang[window._lang]||{}, fb=window._choiceTextByLang[window._defaultLang]||{}; Object.keys(script).forEach(function(label){ (script[label]||[]).forEach(function(row,i){ if(typeof row==='string') script[label][i]=row.replace(/\\u241fui:(.+?)\\u241f/g,function(_,key){ return window.__ui(key); }); else if(row&&row.Choice) Object.keys(row.Choice).forEach(function(k){ var o=row.Choice[k]; if(o&&typeof o.Text==='string') o.Text=o.Text.replace(/\\u241fchoice:(.+?)\\u241f/g,function(_,key){ return cm[key]!=null?cm[key]:(fb[key]||''); }); }); }); }); } catch(e){ console.warn('[i18n] apply error',e); } } };")

        lines.append(f"""
// ==================== 禁用左右方向键 ====================
document.addEventListener('keydown', function(e) {{
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {{
        e.preventDefault();
        e.stopPropagation();
        console.log('🚫 方向键已被禁用');
    }}
}}, true);

// ==================== 样式注入 ====================
(function() {{
    function _injectUI() {{
        const customStyles = `{css_content}`;
        const styleSheet = document.createElement("style");
        styleSheet.textContent = customStyles;
        document.head.appendChild(styleSheet);

        const overlayDiv = document.createElement("div");
        overlayDiv.id = "cinematic-overlay";
        if (document.body) document.body.appendChild(overlayDiv);

        // ── 数值面板 DOM（立即创建，Electron 环境不会触发 window load） ──
        const statsToggle = document.createElement("div");
        statsToggle.id = "stats-toggle";
        statsToggle.title = "查看数值";
        statsToggle.textContent = "◈";
        if (document.body) document.body.appendChild(statsToggle);
        else document.addEventListener('DOMContentLoaded', () => {{ if (document.body) document.body.appendChild(statsToggle); }});

        const statsPanel = document.createElement("div");
        statsPanel.id = "stats-panel";
        if (document.body) document.body.appendChild(statsPanel);
        else document.addEventListener('DOMContentLoaded', () => {{ if (document.body) document.body.appendChild(statsPanel); }});

        statsToggle.addEventListener("click", function() {{
            statsPanel.classList.toggle("open");
            if (window.updateStatsUI) window.updateStatsUI();
        }});
    }}

    if (document.body) {{
        _injectUI();
    }} else {{
        document.addEventListener('DOMContentLoaded', _injectUI);
    }}
}})();

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

window.updateStatsUI = function() {{
    const panel = document.getElementById('stats-panel');
    if (!panel) return;
    const vars = window._playerVariables || {{}};
    panel.innerHTML = Object.entries(vars).map(([k, v]) => {{
        return `<div class="stat-row"><span class="stat-label">${{k}}</span><span class="stat-val">${{v}}</span></div>`;
    }}).join('');
}};

// ==================== 路由导航器 ====================
window.StoryNavigator = {{
    go: function(targetFolderName) {{
        window.showToast('Loading...');
        setTimeout(() => {{
            const varsStr = encodeURIComponent(JSON.stringify(window._playerVariables || {{}}));
            const choiceStatsStr = encodeURIComponent(JSON.stringify(window._playerVariableChoices || {{}}));
            const legacyFlagsStr = encodeURIComponent(JSON.stringify(window._playerLegacyFlags || {{}}));
            const lang = encodeURIComponent(window._lang || window._defaultLang || 'en-US');
            window.location.href = '../' + targetFolderName + '/index.html?vars=' + varsStr + '&choice_stats=' + choiceStatsStr + '&legacy_flags=' + legacyFlagsStr + '&lang=' + lang;
        }}, 300);
    }}
}};

// ==================== 保存/加载管理器 (修复版：直接读取LocalStorage) ====================
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
            console.log('📦 [SaveLoadManager] 存档数据:', saveData);
        }} catch (e) {{
            console.error("❌ 读取存档信息失败:", e);
        }}
    }},

    init: function() {{
        console.log("SaveLoadManager initialized (Local Mode)");
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
        // 【修复】限制index范围，防止父端发来错误数据
        const safeIndex = Math.max(0, Math.min(this.total, index));
        this.current = safeIndex;
        const pct = (safeIndex / this.total) * 100;
        const fillEl = document.getElementById('game-progress-fill');
        if (fillEl) {{
            fillEl.style.width = pct + '%';
        }}
    }},

    sendReport: function(triggerSource) {{
        if (this.total === 0) {{ return; }}
        if (this._disableReport) {{ return; }}
        const safeCurrent = Math.max(0, Math.min(this.total, this.current));
        const safePercent = Math.min(100, parseFloat(((safeCurrent / this.total) * 100).toFixed(2)));
        if (triggerSource !== 'material_click') {{
            console.log('Progress:', triggerSource, safeCurrent, '/', this.total, safePercent + '%');
        }}
    }},

    startAutoSave: function() {{
        if (this.autoSaveTimer) {{
            clearInterval(this.autoSaveTimer);
        }}
        this.autoSaveTimer = setInterval(() => {{
            this.sendReport('auto_save');
        }}, 30000);
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
        console.error('❌ Error:', errorInfo);
    }}
}};

// ==================== 自定义提示系统 (新增) ====================
window.showToast = function(text) {{
    // 如果已存在，先移除
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

    // 2秒后消失
    setTimeout(() => {{
        if (toast) {{
            toast.style.opacity = '0';
            setTimeout(() => {{ if(toast.parentNode) toast.remove(); }}, 500);
        }}
    }}, 2000);
}};
// ==================== 小游戏引擎 ====================

window.MiniGame = {{
    activeResolve: null, // 【核心修改】用于存储当前正在进行的 Promise 句柄

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
            this.activeResolve = null; // 【核心修改】正常结束，清空句柄
        }}, 300);
    }},

    // ================= 【核心修改】强制清理并解锁引擎 =================
    cleanup: function() {{
        // 1. 移除 UI
        const overlay = document.getElementById('qte-overlay');
        if (overlay) overlay.remove();

        const toast = document.getElementById('custom-toast');
        if (toast) toast.remove();

        // 2. 【关键】强制解决 Promise，解锁引擎卡死状态
        if (this.activeResolve) {{
            console.log("强制结束小游戏 Promise，解锁引擎");
            this.activeResolve(true); // 视为通过，让剧情继续，解开死锁
            this.activeResolve = null;
        }}
    }},
    // ==============================================================

    // 1. 连点游戏
    startClicker: function(target=8, time=3, label="Burst!") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // 【记录】保存句柄

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

    // 2. 精准判定
    startTiming: function(speed=2, difficulty=20, label="Perfect Strike") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // 【记录】保存句柄

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

    // 3. 记忆阵法
    startMemory: function(count=4, label="Memory Challenge") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // 【记录】保存句柄

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
                    if (!this.activeResolve) return; // 如果中途退出了，停止动画
                    await new Promise(r=>setTimeout(r, 500));
                    if (!this.activeResolve) return; // 再次检查
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
// ==================== 核心：舞台管理器 (StageManager) ====================
const StageManager = {
    leftChar: null,
    rightChar: null,
    nextReplace: 'left',

    reset() {
        this.leftChar = null;
        this.rightChar = null;
        this.nextReplace = 'left';

        document.querySelectorAll('.manual-character-container').forEach(el => el.remove());

        const visuals = document.querySelector('game-screen [data-content="visuals"]');
        if (visuals) {
            const nativeChars = visuals.querySelectorAll('[data-character]');
            nativeChars.forEach(el => el.remove());
        }
    },

    async forceImage(speakerName, imageName) {
        let charId = null;
        if (speakerName && speakerName !== '旁白') {
            charId = characterIdMap[speakerName] || speakerName;
        }
        if (!charId) return;

        characterFileMap[charId] = imageName;

        const container = document.querySelector(`.manual-character-container[data-character="${charId}"]`);
        if (container) {
            const img = container.querySelector('img');
            if (img) {
                img.src = `assets/characters/${imageName}`;
            }
        }

        await this.update(charId);
    },

    async update(speakerId) {
        if (!speakerId || speakerId === 'nb' || speakerId === '旁白') {
            this.setVisualState(this.leftChar, 'dimmed');
            this.setVisualState(this.rightChar, 'dimmed');
            return;
        }

        if (speakerId === this.leftChar) {
            if (!this.getCharImage(speakerId)) {
                await this.addCharacter(speakerId, 'left');
            }

            this.setVisualState(this.leftChar, 'active');
            this.setVisualState(this.rightChar, 'inactive');
            return;
        }

        if (speakerId === this.rightChar) {
            if (!this.getCharImage(speakerId)) {
                await this.addCharacter(speakerId, 'right');
            }

            this.setVisualState(this.rightChar, 'active');
            this.setVisualState(this.leftChar, 'inactive');
            return;
        }

        let targetSlot = '';
        if (!this.leftChar) {
            targetSlot = 'left';
        } else if (!this.rightChar) {
            targetSlot = 'right';
        } else {
            targetSlot = this.nextReplace;
            const charToRemove = (targetSlot === 'left') ? this.leftChar : this.rightChar;
            await this.removeCharacter(charToRemove);
            this.nextReplace = (targetSlot === 'left') ? 'right' : 'left';
        }

        await this.addCharacter(speakerId, targetSlot);

        if (targetSlot === 'left') {
            this.leftChar = speakerId;
            this.setVisualState(this.leftChar, 'active');
            this.setVisualState(this.rightChar, 'inactive');
        } else {
            this.rightChar = speakerId;
            this.setVisualState(this.rightChar, 'active');
            this.setVisualState(this.leftChar, 'inactive');
        }
    },

    setVisualState(charId, state) {
        if (!charId) return;
        const img = this.getCharImage(charId);
        if (!img) return;

        img.classList.remove('state-active', 'state-inactive', 'state-dimmed');
        if (state === 'active') img.classList.add('state-active');
        if (state === 'inactive') img.classList.add('state-inactive');
        if (state === 'dimmed') img.classList.add('state-dimmed');
    },

    addCharacter(charId, slot) {
        return new Promise(resolve => {
            const filename = characterFileMap[charId];
            if (!filename) {
                console.error(`[StageManager] 错误：找不到ID为 ${charId} 的文件名`);
                resolve();
                return;
            }

            let container = document.querySelector(`.manual-character-container[data-character="${charId}"]`);

            if (!container) {
                container = document.createElement('div');
                container.className = 'manual-character-container';
                container.setAttribute('data-character', charId);

                container.style.position = 'absolute';
                container.style.top = '0';
                container.style.left = '0';
                container.style.width = '100%';
                container.style.height = '100%';
                container.style.pointerEvents = 'none';
                container.style.zIndex = '10';

                const img = document.createElement('img');
                img.src = `assets/characters/${filename}`;
                img.className = 'character-image';

                if (slot === 'left') img.classList.add('slot-left');
                else img.classList.add('slot-right');

                img.classList.add('state-hidden');
                container.appendChild(img);

                const visualsLayer = document.querySelector('game-screen [data-content="visuals"]');
                const gameScreen = document.querySelector('game-screen');

                if (visualsLayer) {
                    visualsLayer.appendChild(container);
                    setTimeout(() => { img.classList.remove('state-hidden'); }, 50);
                } else if (gameScreen) {
                    console.warn('[StageManager] 未找到 visuals 层，插入到 game-screen');
                    gameScreen.insertBefore(container, gameScreen.firstChild);
                    setTimeout(() => { img.classList.remove('state-hidden'); }, 50);
                } else {
                    console.error('[StageManager] 致命错误：找不到游戏容器');
                }
            }
            resolve();
        });
    },

    removeCharacter(charId) {
        return new Promise(resolve => {
            if (!charId) { resolve(); return; }
            const container = document.querySelector(`.manual-character-container[data-character="${charId}"]`);
            if (container) {
                const img = container.querySelector('img');
                if (img) img.classList.add('state-hidden');
                setTimeout(() => { container.remove(); resolve(); }, 600);
            } else {
                resolve();
            }
        });
    },

    getCharImage(charId) {
        const container = document.querySelector(`.manual-character-container[data-character="${charId}"]`);
        return container ? container.querySelector('img') : null;
    }
};

function updateStage(speakerName) {
    let charId = null;
    if (speakerName && speakerName !== '旁白' && speakerName !== 'nb') {
        charId = characterIdMap[speakerName] || speakerName;
    }
    return StageManager.update(charId);
}

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
    _paraCount: 0,
    _PAGE_SIZE: 5,

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
        const overlay = document.getElementById('hook-overlay');
        if (overlay && !overlay._clickBound) {
            overlay._clickBound = true;
            function _unlockAudio() {
                document.querySelectorAll('audio').forEach(a => {
                    if (a.paused && a.src) a.play().catch(() => {});
                });
            }
            overlay.addEventListener('click', function() {
                _unlockAudio();
                const tb = document.querySelector('[data-component="text-box"]');
                if (tb) tb.click();
            });
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
        const oldCursor = document.getElementById('hook-cursor');
        if (oldCursor) oldCursor.remove();
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
        const chars = [...text];
        let i = 0;
        const speed = Math.max(15, Math.min(35, 1400 / chars.length));
        const type = () => {
            if (i < chars.length) {
                p.textContent += chars[i++];
                this._typeTimer = setTimeout(type, speed);
            } else {
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
    #                 // 检查是否是保存游戏存档（包含 _Save_ 或 _AutoSave_）
    #                 if (key.includes('_Save_') || key.includes('_AutoSave_')) {
    #                     // 获取所有存档
    #                     const saveKeys = Object.keys(localStorage).filter(k => k.includes('_Save_') || k.includes('_AutoSave_'));
    #
    #                     // 如果已有3个存档且当前key不在已有存档中，则阻止保存
    #                     if (saveKeys.length >= 3 && !saveKeys.includes(key)) {
    #
    #
    #                         // 关键：强制刷新 save-screen 显示
    #                         setTimeout(() => {
    #                             const saveScreen = document.querySelector('save-screen');
    #                             if (saveScreen) {
    #                                 // 方法1：触发重新渲染
    #                                 const parent = saveScreen.parentNode;
    #                                 const clone = saveScreen.cloneNode(true);
    #                                 parent.replaceChild(clone, saveScreen);
    #
    #                                 // 方法2：如果有刷新方法，调用它
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
    #                         autoAdvance: false,  // 关闭自动前进，确保click命令有效
    #                         voiceVolume: 100,
    #                         musicVolume: 15,     // <--- 在这里修改数值 (0-100)，建议 30-50 之间
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
    #                             // 如果没有进度数据或 current_index 为空，默认显示"开始"
    #                             if (!progressData || !progressData.current_index || progressData.current_index === 1) {
    #                                 startButton.textContent = "开始";
    #                             } else {
    #                                 startButton.textContent = "继续";
    #                                 // 保存进度信息，用于点击后跳转
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
    #                 // 检查是否从跳转加载（通过URL参数或sessionStorage）
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
                raw_story_tree = json.load(f)

            story_tree, story_tree_by_lang = normalize_story_tree(raw_story_tree)
            self.story_tree_by_lang = story_tree_by_lang
            self.story_tree_default_language = 'en-US' if 'en-US' in story_tree_by_lang else next(iter(story_tree_by_lang), 'en-US')
            self.story_ui_by_lang = extract_story_ui_text(story_tree_by_lang)

            root_id = story_tree.get('root_id', 'chapter_1')
            nodes = story_tree.get('nodes', {})
            hook_id = story_tree.get('hook_id', None)
            player_variables = story_tree.get('player_variables', {})
            branch_thresholds = story_tree.get('branch_thresholds', {})

            # 从 story_tree.json 读取 art_style（由 story_planner 写入）
            art_style = story_tree.get('art_style', self.art_style) or self.art_style
            print(f"  art_style: {art_style}")

            if not nodes:
                print("错误：story_tree.json中没有节点数据")
                return

            print(f"找到 {len(nodes)} 个节点，根节点ID: {root_id}")

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

            launcher_state_key = f"steam_launcher_state_{self.output_dir.name}"

            for node_id, node_data in nodes.items():
                folder_name = folder_mapping[node_id]
                depth = node_data.get('depth', 1)  # 获取章节编号
                print(f"\n处理节点: {node_id} -> 输出文件夹: {folder_name}")

                # 同时兼容旧 node_id 和当前 chapter_{depth}_{suffix} 素材目录。
                source_dir = self.source_dir / node_id
                if not source_dir.exists():
                    source_dir = self.source_dir / folder_name
                if not source_dir.exists():
                    print(f"  警告：源目录不存在，跳过: {source_dir}")
                    continue

                # 为每个节点创建独立的转换器实例
                node_converter = FinalConverter(
                    source_dir=str(source_dir),
                    template_dir=self.template_dir,
                    output_dir=Path(self.output_dir) / folder_name,
                    images_base_dir=self.images_base_dir,
                    art_style=art_style
                )

                # 复制当前实例的角色信息
                node_converter.pinyin_to_chinese = self.pinyin_to_chinese.copy()
                node_converter.chinese_to_id = self.chinese_to_id.copy()
                node_converter.chinese_to_pinyin = self.chinese_to_pinyin.copy()
                node_converter._source_node_id = node_id
                node_converter.story_tree_by_lang = story_tree_by_lang
                node_converter.story_tree_default_language = self.story_tree_default_language
                node_converter.story_ui_by_lang = self.story_ui_by_lang
                node_converter.story_choice_text_by_lang = {
                    lang: {f"{node_id}::{child_id}": text for child_id, text in child_map.items()}
                    for lang, child_map in extract_story_choice_text(story_tree_by_lang, node_id).items()
                }

                # 【关键】复制特效配置（从父实例继承，因为atmophere.json在根目录）
                node_converter.atmosphere_config = self.atmosphere_config

                # 扫描该节点的素材
                node_converter.scan_assets()

                # 组织素材
                scenes = node_converter.organize_materials()

                # 创建项目（即使没有素材也要创建基础项目，支持 What-If 等空节点）
                # 复制模板
                node_converter.copy_template()

                # 隔离各章节的 localStorage 存档命名空间
                options_file = node_converter.output_dir / "js" / "options.js"
                if options_file.exists():
                    opts = options_file.read_text(encoding='utf-8')
                    opts = re.sub(
                        r"('Name'\s*:\s*)'([^']*)'",
                        lambda m: f"{m.group(1)}'{m.group(2)}_{node_id}'",
                        opts
                    )
                    options_file.write_text(opts, encoding='utf-8')

                if scenes:
                    # 获取章节数据
                    chapter_data = scenes.get('all', {})
                    # 先复制素材（根据音频文件名推断需要的场景），获取实际复制的资源列表
                    copied_assets = node_converter.copy_assets_for_chapter(chapter_data, node_id)
                else:
                    # 空节点（What-If 等）：生成最小化的游戏
                    chapter_data = {'materials': [], 'start_index': 1, 'end_index': 1}
                    copied_assets = {'scenes': [], 'audios': [], 'music': [], 'sfx': [], 'characters': []}

                # 生成脚本文件（传递node_data和folder_mapping，使用实际复制的资源）
                script_file = node_converter.output_dir / "js" / "script.js"

                script_content = "/* global monogatari */\n\n"
                script_content += node_converter.generate_character_definitions()
                script_content += node_converter.generate_asset_declarations(copied_assets)
                script_content += "\n".join(node_converter.generate_utility_code(chapter_data, node_id, depth))
                # hook_id 只对根节点生效
                node_hook_id = hook_id if node_id == root_id else None
                script_content += node_converter.generate_script_for_chapter(
                    chapter_data, node_data, folder_mapping,
                    hook_id=node_hook_id,
                    player_variables=player_variables,
                    branch_thresholds=branch_thresholds,
                    launcher_key=launcher_state_key
                )

                with open(script_file, 'w', encoding='utf-8') as f:
                    f.write(script_content)

                # 更新main.js

                main_js_content = """'use strict';
                /* global Monogatari */

                const { $_ready, $_ } = Monogatari;

                $_ready (() => {{
                    // 【Back按钮修复】初始化会话起始索引
                    window._sessionStartIndex = 1;
                    const LAUNCHER_STATE_KEY = '{launcher_state_key}';
                    const CURRENT_FOLDER = '{folder_name}';
                    const ROOT_FOLDER = '{root_folder}';
                    const CURRENT_NODE_ID = '{node_id}';

                    function readLauncherState() {{
                        try {{
                            return JSON.parse(localStorage.getItem(LAUNCHER_STATE_KEY) || '{{}}');
                        }} catch (e) {{
                            return {{}};
                        }}
                    }}

                    function writeLauncherState(patch) {{
                        const state = readLauncherState();
                        const next = Object.assign({{}}, state, patch || {{}});
                        localStorage.setItem(LAUNCHER_STATE_KEY, JSON.stringify(next));
                        return next;
                    }}

                    function syncLauncherProgress(materialIndex) {{
                        const state = readLauncherState();
                        state.chapters = state.chapters || {{}};
                        state.chapters[CURRENT_FOLDER] = Object.assign({{}}, state.chapters[CURRENT_FOLDER] || {{}}, {{
                            nodeId: CURRENT_NODE_ID,
                            lastMaterial: materialIndex,
                            completed: false,
                            updatedAt: Date.now()
                        }});
                        state.currentChapter = CURRENT_FOLDER;
                        state.currentNodeId = CURRENT_NODE_ID;
                        state.globalVars = Object.assign({{}}, window._playerVariables || {{}});
                        state.globalChoiceStats = JSON.parse(JSON.stringify(window._playerVariableChoices || {{}}));
                        state.globalLegacyFlags = Object.assign({{}}, window._playerLegacyFlags || {{}});
                        localStorage.setItem(LAUNCHER_STATE_KEY, JSON.stringify(state));
                        // 同步更新章节当前进度快照，Continue 时能恢复到当前变量状态
                        try {{ localStorage.setItem('_chapterCurrentVars_' + CURRENT_NODE_ID, JSON.stringify(window._playerVariables || {{}})); }} catch(e) {{}}
                        try {{ localStorage.setItem('_chapterCurrentChoiceStats_' + CURRENT_NODE_ID, JSON.stringify(window._playerVariableChoices || {{}})); }} catch(e) {{}}
                    }}

                    function markLauncherComplete(nextFolder, nextNodeId) {{
                        const state = readLauncherState();
                        state.chapters = state.chapters || {{}};
                        state.chapters[CURRENT_FOLDER] = Object.assign({{}}, state.chapters[CURRENT_FOLDER] || {{}}, {{
                            nodeId: CURRENT_NODE_ID,
                            completed: true,
                            updatedAt: Date.now()
                        }});
                        if (nextFolder) state.currentChapter = nextFolder;
                        if (nextNodeId) state.currentNodeId = nextNodeId;
                        state.globalVars = Object.assign({{}}, window._playerVariables || {{}});
                        state.globalChoiceStats = JSON.parse(JSON.stringify(window._playerVariableChoices || {{}}));
                        state.globalLegacyFlags = Object.assign({{}}, window._playerLegacyFlags || {{}});
                        localStorage.setItem(LAUNCHER_STATE_KEY, JSON.stringify(state));
                    }}

                    // LocalStorage 存档数量限制拦截（每章节独立计算3个上限）
                    const originalSetItem = localStorage.setItem.bind(localStorage);
                    localStorage.setItem = function(key, value) {{
                        if (key.includes('_Save_') || key.includes('_AutoSave_')) {{
                            const chapterPrefix = '视觉小说_{node_id}';
                            const saveKeys = Object.keys(localStorage).filter(k => k.startsWith(chapterPrefix) && (k.includes('_Save_') || k.includes('_AutoSave_')));
                            if (saveKeys.length >= 3 && !saveKeys.includes(key)) {{
                                alert('Save slots are full (max 3).\\nPlease delete an existing save before saving again.');
                                throw new Error('Save limit reached.');
                            }}
                        }}
                        return originalSetItem(key, value);
                    }};

                    // 引擎初始化
                    try {{ localStorage.removeItem('VisualNovel_Settings'); }} catch(e) {{}}

                    // 检查 URL 参数，continue=N 时自动跳转
                    const _urlParams = new URLSearchParams(window.location.search);
                    const _continueAt = _urlParams.get('continue');
                    const _isNewGame = _urlParams.get('newgame') === '1';
                    const _launcherState = readLauncherState();

                    // newgame=1：清理章节存档，强制从 Material_1 开始
                    if (_isNewGame) {{
                        localStorage.removeItem('视觉小说_{node_id}_last_material');
                        localStorage.removeItem('_chapterCurrentVars_{node_id}');
                        localStorage.removeItem('_chapterCurrentChoiceStats_{node_id}');
                        localStorage.removeItem('_chapterEntryVars_{node_id}');
                        localStorage.removeItem('_chapterEntryChoiceStats_{node_id}');
                        localStorage.removeItem('_chapterEntryLegacyFlags_{node_id}');
                        // 去掉 newgame 参数，防止刷新时重复清理
                        const _cleanUrl = new URL(window.location.href);
                        _cleanUrl.searchParams.delete('newgame');
                        if (_cleanUrl.toString() !== window.location.href) {{
                            window.history.replaceState(null, '', _cleanUrl.toString());
                        }}
                    }}

                    if (!_urlParams.get('vars') && !_isNewGame && _launcherState.globalVars && _launcherState.currentChapter === CURRENT_FOLDER) {{
                        try {{
                            const _resumeUrl = new URL(window.location.href);
                            _resumeUrl.searchParams.set('vars', JSON.stringify(_launcherState.globalVars));
                            if (_launcherState.globalChoiceStats) _resumeUrl.searchParams.set('choice_stats', JSON.stringify(_launcherState.globalChoiceStats));
                            if (_launcherState.globalLegacyFlags) _resumeUrl.searchParams.set('legacy_flags', JSON.stringify(_launcherState.globalLegacyFlags));
                            window.location.replace(_resumeUrl.toString());
                            return;
                        }} catch (e) {{}}
                    }}

                    monogatari.init('#monogatari').then(() => {{
                        console.log('✅ Visual Novel Engine Started');
                        if (window.I18nScript) window.I18nScript.apply();
                        try {{
                            monogatari.preference('Language', String(window._lang || '').toLowerCase().indexOf('ja') === 0 ? '日本語' : 'English');
                        }} catch (e) {{}}

                        // [修复1：音频无声问题] 在跳过 Start 标签直接 jump 前，将音频字典正式注册给引擎
                        if (window._allVoices) {{
                            monogatari.assets('voices', window._allVoices);
                        }}

                        // continue 参数存在时，模拟点击原生 Start 按钮，走完整初始化，拦截跳转到目标 Material
                        if (_continueAt) {{
                            const _mat = parseInt(_continueAt);
                            window._continueTarget = _mat;
                            window._sessionStartIndex = _mat;
                            setTimeout(() => {{
                                const mainMenu = document.querySelector('main-menu');
                                if (mainMenu) {{
                                    const nativeStart = mainMenu.querySelector('.start-continue-button') || mainMenu.querySelector('[data-action="start"]');
                                    if (nativeStart) nativeStart.click();
                                }}
                            }}, 800);
                        }}

                        // 彻底禁用引擎的end方法，防止游戏自动结束；同时同步进度条
                        window._userQuitFlag = false;

                        const originalRun = monogatari.run.bind(monogatari);
                        monogatari.run = function(statement, ...args) {{
                            if (typeof statement === 'string') {{
                                // Continue 拦截：Start 标签强制跳 Material_1 时，替换成目标 Material
                                if (statement.trim() === 'jump Material_1' && window._continueTarget) {{
                                    const target = window._continueTarget;
                                    window._continueTarget = null;
                                    return originalRun('jump Material_' + target, ...args);
                                }}
                                // 同步进度条到当前 Material（包括 Load 存档后的跳转）
                                const matMatch = statement.match(/^jump\s+Material_(\d+)$/);
                                if (matMatch && window.GameProgressReporter) {{
                                    const matIdx = parseInt(matMatch[1]);
                                    window.GameProgressReporter.update(matIdx);
                                    localStorage.setItem('视觉小说_{node_id}_last_material', matIdx);
                                    syncLauncherProgress(matIdx);
                                }}
                                if (statement.trim().toLowerCase() === 'end') {{
                                    if (window._userQuitFlag) {{
                                        window._userQuitFlag = false;
                                        return originalRun(statement, ...args);
                                    }} else {{
                                        return Promise.resolve();
                                    }}
                                }}
                            }}
                            return originalRun(statement, ...args);
                        }};

                        const originalGlobal = monogatari.global.bind(monogatari);
                        monogatari.global = function(key, value) {{
                            if (key === 'playing' && value === false) {{
                                if (window._userQuitFlag) {{
                                    window._userQuitFlag = false;
                                    return originalGlobal(key, value);
                                }} else {{
                                    return originalGlobal(key, true);
                                }}
                            }}
                            return originalGlobal(key, value);
                        }};

                        if (window.SaveLoadManager) window.SaveLoadManager.init();
                        monogatari.storage({{ settings: {{ autoAdvance: false, voiceVolume: 100, musicVolume: 15, skipVoice: false }} }});

                        // 初始化进度条（无论从 Start 还是 Load 进入都需要）
                        if (window.GameProgressReporter) {{
                            window.GameProgressReporter.init({total_steps}, '{chap_id}', '{node_id}');
                        }}

                        // ============================================================
                        // 按钮逻辑
                        // ============================================================

                        let buttonsAdded = false;
                        function addCustomButtons() {{
                            if (buttonsAdded) return;
                            const mainMenu = document.querySelector('main-menu');
                            if (!mainMenu) return;
                            const startButton = mainMenu.querySelector('[data-action="start"]');
                            if (!startButton) return;

                            // Start / Continue 按钮
                            startButton.classList.add('start-continue-button');

                            // 无论初始有没有存档，都彻底接管按钮，移除原生动作
                            startButton.removeAttribute('data-action');

                            startButton.addEventListener('click', (e) => {{
                                e.preventDefault(); e.stopPropagation();
                                // 读取当前进度位置
                                const currentMatStr = localStorage.getItem('视觉小说_{node_id}_last_material');
                                const currentMaterial = (currentMatStr && parseInt(currentMatStr) > 0) ? parseInt(currentMatStr) : 1;
                                window._continueTarget = currentMaterial;
                                window._sessionStartIndex = currentMaterial;
                                // Continue：保留当前变量（内存中已有的最新值），直接跳到存档位置
                                // 更新 launcher state
                                const state = readLauncherState();
                                state.currentChapter = CURRENT_FOLDER;
                                state.currentNodeId = CURRENT_NODE_ID;
                                state.globalVars = Object.assign({{}}, window._playerVariables || {{}});
                                state.globalChoiceStats = JSON.parse(JSON.stringify(window._playerVariableChoices || {{}}));
                                state.globalLegacyFlags = Object.assign({{}}, window._playerLegacyFlags || {{}});
                                localStorage.setItem(LAUNCHER_STATE_KEY, JSON.stringify(state));
                                monogatari.global('playing', true);
                                monogatari.onStart().then(() => {{
                                    monogatari.element().find('[data-screen]').each((screen) => {{
                                        screen.setState({{ open: false }});
                                    }});
                                    monogatari.element().find('[data-screen="game"]').get(0).setState({{ open: true }});
                                    // 直接跳存档位置（Start Function 会检测内存已有变量，跳过初始化）
                                    monogatari.run('jump Material_' + currentMaterial);
                                    // 跳转后刷新数值面板（确保 UI 与内存中的最新变量同步）
                                    setTimeout(() => {{
                                        if (window.updateStatsUI) window.updateStatsUI();
                                    }}, 100);
                                }});
                            }});

                            // 初始文本（MutationObserver 负责后续更新）
                            // newgame=1 时强制显示 Start，不读取 localStorage 存档
                            if (!_isNewGame) {{
                                const lastMaterial = localStorage.getItem('视觉小说_{node_id}_last_material');
                                if (lastMaterial && parseInt(lastMaterial) > 0) {{
                                    startButton.textContent = 'Continue';
                                }}
                            }}

                            // Restart 按钮
                            if (!document.querySelector('.restart-button')) {{
                                const restartBtn = startButton.cloneNode(true);
                                restartBtn.textContent = 'Restart';
                                restartBtn.classList.add('restart-button');
                                restartBtn.removeAttribute('data-action');
                                startButton.parentNode.insertBefore(restartBtn, startButton.nextSibling);

                                restartBtn.addEventListener('click', (e) => {{
                                    e.preventDefault(); e.stopPropagation();

                                    // 必须清空 Continue 目标，否则第一次点击会被底层拦截器当作 Continue 处理
                                    window._continueTarget = null;

                                    localStorage.removeItem('视觉小说_{node_id}_last_material');
                                    localStorage.removeItem('_chapterCurrentVars_{node_id}'); // 清除当前进度快照
                                    localStorage.removeItem('_chapterCurrentChoiceStats_{node_id}');

                                    // 读取章节入口快照（真正的初始值）
                                    let snap = null;
                                    try {{ snap = JSON.parse(localStorage.getItem('_chapterEntryVars_{node_id}')); }} catch(e) {{}}
                                    let choiceSnap = null;
                                    try {{ choiceSnap = JSON.parse(localStorage.getItem('_chapterEntryChoiceStats_{node_id}')); }} catch(e) {{}}
                                    if (!snap) {{
                                        try {{
                                            const ls = readLauncherState();
                                            if (ls.globalVars) snap = ls.globalVars;
                                        }} catch(e) {{}}
                                    }}
                                    if (!snap) snap = {{}};
                                    if (!choiceSnap) choiceSnap = {{}};

                                    // [修复2] 必须也要回写到 launcher_state，防止状态脱节
                                    const state = readLauncherState();
                                    state.globalVars = Object.assign({{}}, snap);
                                    state.globalChoiceStats = JSON.parse(JSON.stringify(choiceSnap));
                                    state.globalLegacyFlags = Object.assign({{}}, window._playerLegacyFlags || {{}});
                                    localStorage.setItem(LAUNCHER_STATE_KEY, JSON.stringify(state));

                                    window._playerVariables = Object.assign({{}}, snap);
                                    window._playerVariableChoices = JSON.parse(JSON.stringify(choiceSnap));
                                    window._chapterStartStats = Object.assign({{}}, snap);
                                    if (window._syncPlayerVars) window._syncPlayerVars();
                                    if (window.updateStatsUI) window.updateStatsUI();
                                    syncLauncherProgress(1);
                                    window._sessionStartIndex = 1;
                                    // 重置进度条到 step 1
                                    if (window.GameProgressReporter) {{
                                        window.GameProgressReporter.update(1);
                                    }}
                                    setTimeout(() => {{
                                        const backBtn = document.querySelector('[data-action="back"]');
                                        if (backBtn) {{
                                            backBtn.setAttribute('disabled', 'true');
                                            backBtn.style.opacity = '0.3';
                                            backBtn.style.pointerEvents = 'none';
                                            backBtn.style.cursor = 'not-allowed';
                                        }}
                                    }}, 50);
                                    monogatari.global('playing', true);
                                    monogatari.onStart().then(() => {{
                                        monogatari.element().find('[data-screen]').each((screen) => {{
                                            screen.setState({{ open: false }});
                                        }});
                                        monogatari.element().find('[data-screen="game"]').get(0).setState({{ open: true }});
                                        // 跳 Material_1 绕开 Start Function 重初始化，直接重播叙事
                                        monogatari.run('jump Material_1');
                                    }});
                                }});
                            }}

                            buttonsAdded = true;
                        }}

                        // 初始化数值变量（页面加载时立即执行，设完变量后立即刷新面板）
                        function _initPlayerVarsFromStorage() {{
                            if (!window._playerLegacyFlags) {{
                                let legacyFlags = null;
                                try {{
                                    const urlLegacyFlags = new URLSearchParams(window.location.search).get('legacy_flags');
                                    if (urlLegacyFlags) legacyFlags = JSON.parse(decodeURIComponent(urlLegacyFlags));
                                }} catch(e) {{}}
                                if (!legacyFlags) {{
                                    try {{
                                        const snap = localStorage.getItem('_chapterEntryLegacyFlags_{node_id}');
                                        if (snap) legacyFlags = JSON.parse(snap);
                                    }} catch(e) {{}}
                                }}
                                if (!legacyFlags) {{
                                    try {{
                                        const lsState = JSON.parse(localStorage.getItem('{launcher_state_key}') || '{{}}');
                                        if (lsState.globalLegacyFlags) legacyFlags = Object.assign({{}}, lsState.globalLegacyFlags);
                                    }} catch(e) {{}}
                                }}
                                window._playerLegacyFlags = legacyFlags || {{}};
                                if (!localStorage.getItem('_chapterEntryLegacyFlags_{node_id}')) {{
                                    localStorage.setItem('_chapterEntryLegacyFlags_{node_id}', JSON.stringify(window._playerLegacyFlags));
                                }}
                            }}
                            if (!window._playerVariableChoices) {{
                                let choiceStats = null;
                                try {{
                                    const urlChoiceStats = new URLSearchParams(window.location.search).get('choice_stats');
                                    if (urlChoiceStats) choiceStats = JSON.parse(decodeURIComponent(urlChoiceStats));
                                }} catch(e) {{}}
                                if (!choiceStats) {{
                                    try {{
                                        const snap = localStorage.getItem('_chapterCurrentChoiceStats_{node_id}');
                                        if (snap) choiceStats = JSON.parse(snap);
                                    }} catch(e) {{}}
                                }}
                                if (!choiceStats) {{
                                    try {{
                                        const snap = localStorage.getItem('_chapterEntryChoiceStats_{node_id}');
                                        if (snap) choiceStats = JSON.parse(snap);
                                    }} catch(e) {{}}
                                }}
                                if (!choiceStats) {{
                                    try {{
                                        const lsState = JSON.parse(localStorage.getItem('{launcher_state_key}') || '{{}}');
                                        if (lsState.globalChoiceStats) choiceStats = JSON.parse(JSON.stringify(lsState.globalChoiceStats));
                                    }} catch(e) {{}}
                                }}
                                window._playerVariableChoices = choiceStats || {{}};
                                if (!localStorage.getItem('_chapterEntryChoiceStats_{node_id}')) {{
                                    localStorage.setItem('_chapterEntryChoiceStats_{node_id}', JSON.stringify(window._playerVariableChoices));
                                }}
                            }}
                            if (window._playerVariables) return;
                            const _initVars = {init_vars_placeholder};
                            let vars = null;
                            try {{
                                const urlParams = new URLSearchParams(window.location.search);
                                const urlVars = urlParams.get('vars');
                                if (urlVars) {{
                                    vars = JSON.parse(decodeURIComponent(urlVars));
                                    // 仅当"入口快照"不存在时才写入。
                                    // 防止玩家中途退回启动器后再次 Continue 进来时，URL 携带的最新数值污染了初始快照
                                    if (!localStorage.getItem('_chapterEntryVars_{node_id}')) {{
                                        localStorage.setItem('_chapterEntryVars_{node_id}', JSON.stringify(vars));
                                    }}
                                }}
                            }} catch(e) {{}}

                            if (!vars) {{
                                try {{
                                    // [修复2] 不是从 URL 进入(如刷新页面)，优先读取 "当前进度快照" 以便 Continue
                                    const snap = localStorage.getItem('_chapterCurrentVars_{node_id}');
                                    if (snap) vars = JSON.parse(snap);
                                }} catch(e) {{}}
                            }}
                            if (!vars) {{
                                try {{
                                    // 没有当前进度，才降级读取 "入口快照"
                                    const snap = localStorage.getItem('_chapterEntryVars_{node_id}');
                                    if (snap) vars = JSON.parse(snap);
                                }} catch(e) {{}}
                            }}
                            if (!vars) {{
                                try {{
                                    const lsState = JSON.parse(localStorage.getItem('{launcher_state_key}') || '{{}}');
                                    if (lsState.globalVars) vars = Object.assign({{}}, lsState.globalVars);
                                }} catch(e) {{}}
                            }}
                            if (!vars) vars = Object.assign({{}}, _initVars);
                            window._playerVariables = vars;
                            window._chapterStartStats = Object.assign({{}}, vars);

                            // [修复2] 确保一定存在一个"入口快照"供 Restart 使用
                            if (!localStorage.getItem('_chapterEntryVars_{node_id}')) {{
                                localStorage.setItem('_chapterEntryVars_{node_id}', JSON.stringify(vars));
                            }}

                            // 设完变量后立即刷新面板
                            if (window.updateStatsUI) window.updateStatsUI();
                            if (window._syncPlayerVars) window._syncPlayerVars();
                        }}
                        _initPlayerVarsFromStorage();

                        setTimeout(addCustomButtons, 500);

                        // 修复：quit 后从主菜单进入 Settings/Help，点 back 黑屏问题
                        // 记录 Settings/Help 是从主菜单还是游戏内打开的
                        window._screenOpenedFromMain = false;
                        document.addEventListener('click', function(e) {{
                            const t = e.target;
                            const opener = t.closest('[data-action="open-screen"]');
                            if (opener) {{
                                // 判断点击来源：主菜单 or 游戏内快捷菜单
                                window._screenOpenedFromMain = !!opener.closest('main-menu');
                            }}
                        }}, true);
                        document.addEventListener('click', function(e) {{
                            const t = e.target;
                            const isBack = t.getAttribute('data-action') === 'back' || t.closest('[data-action="back"]');
                            if (!isBack) return;
                            if (!window._screenOpenedFromMain) return; // 游戏内打开的，不干预
                            // 从主菜单打开的 Settings/Help，back 强制回 main-screen
                            e.stopImmediatePropagation();
                            window._screenOpenedFromMain = false;
                            monogatari.element().find('[data-screen]').each((screen) => {{
                                screen.setState({{ open: false }});
                            }});
                            monogatari.element().find('[data-screen="main"]').get(0).setState({{ open: true }});
                        }}, true);

                        // 监听 Quit 确认
                        document.addEventListener('click', function(e) {{
                            const t = e.target;
                            const isQuit = t.getAttribute('data-action') === 'quit' || t.closest('[data-action="quit"]');
                            const isInModal = t.closest('.modal, [role="dialog"]');
                            if (isQuit && isInModal) {{
                                window._userQuitFlag = true;

                                // 检查是否在软分支内部，如果是则回溯数值效果
                                if (window.GameProgressReporter) {{
                                    const cur = window.GameProgressReporter.current;
                                    const inBranchKey = '_inBranch_' + cur;
                                    const inBranch = window[inBranchKey]; // 'A' 或 'B' 或 undefined

                                    if (inBranch) {{
                                        // 在软分支内部，需要回溯数值
                                        console.log('🔄 检测到在软分支内部退出，回溯数值效果');

                                        // 获取该分支的效果并回溯
                                        const branchEffects = window._branchEffects || {{}};
                                        const effectKey = cur + '_' + inBranch;
                                        const effects = branchEffects[effectKey];
                                        const legacyFlags = (window._branchLegacyFlags || {{}})[effectKey] || {{}};

                                        if (effects && window._playerVariables) {{
                                            Object.entries(effects).forEach(([k, v]) => {{
                                                window._playerVariables[k] = (window._playerVariables[k] || 0) - v;
                                            }});
                                            if (window._changePlayerVariableChoices) window._changePlayerVariableChoices(effects, -1);
                                            if (window._syncPlayerVars) window._syncPlayerVars();
                                            if (window.updateStatsUI) window.updateStatsUI();
                                        }}
                                        if (window._changePlayerLegacyFlags) window._changePlayerLegacyFlags(legacyFlags, -1);

                                        // 清除分支选择记录
                                        localStorage.removeItem('_branchChosen_{node_id}_' + cur);
                                    }}

                                    window.GameProgressReporter._disableReport = true;
                                    if (cur > 0) {{
                                        localStorage.setItem('视觉小说_{node_id}_last_material', cur);
                                        syncLauncherProgress(cur);
                                    }}
                                }}
                            }}
                        }}, true);

                        // quit 回主菜单后更新 Start → Continue
                        const mainScreenEl = monogatari.element().find('[data-screen="main"]').get(0);
                        if (mainScreenEl) {{
                            const observer = new MutationObserver(() => {{
                                const lastMaterial = localStorage.getItem('视觉小说_{node_id}_last_material');
                                const mainMenu = document.querySelector('main-menu');
                                if (!mainMenu) return;
                                const startButton = mainMenu.querySelector('.start-continue-button');
                                if (!startButton) return;
                                if (lastMaterial && parseInt(lastMaterial) > 0) {{
                                    startButton.textContent = 'Continue';
                                }} else {{
                                    startButton.textContent = 'Start';
                                }}
                            }});
                            observer.observe(mainScreenEl, {{ attributes: true, attributeFilter: ['class', 'style', 'data-open'] }});
                        }}

                        // 注入返回菜单按钮
                        setTimeout(() => {{
                            if (document.getElementById('__back-to-menu')) return;
                            const wrapper = document.createElement('div');
                            wrapper.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2147483647';
                            const btn = document.createElement('button');
                            btn.id = '__back-to-menu';
                            btn.textContent = '← Menu';
                            btn.style.cssText = 'position:absolute;top:12px;right:88px;pointer-events:auto;padding:4px 10px;width:auto;height:auto;min-width:0;min-height:0;line-height:1.2;background:rgba(0,0,0,0.65);color:#fff;border:1px solid rgba(255,255,255,0.35);border-radius:999px;font-size:12px;font-family:sans-serif;text-transform:none;white-space:nowrap;cursor:pointer';
                            btn.onclick = function(e) {{
                                e.stopPropagation();

                                // 检查是否在软分支内部，如果是则回溯数值效果
                                if (window.GameProgressReporter) {{
                                    const cur = window.GameProgressReporter.current;
                                    const inBranchKey = '_inBranch_' + cur;
                                    const inBranch = window[inBranchKey]; // 'A' 或 'B' 或 undefined

                                    if (inBranch) {{
                                        // 在软分支内部，需要回溯数值
                                        console.log('🔄 检测到在软分支内部返回菜单，回溯数值效果');

                                        // 获取该分支的效果并回溯
                                        const branchEffects = window._branchEffects || {{}};
                                        const effectKey = cur + '_' + inBranch;
                                        const effects = branchEffects[effectKey];
                                        const legacyFlags = (window._branchLegacyFlags || {{}})[effectKey] || {{}};

                                        if (effects && window._playerVariables) {{
                                            Object.entries(effects).forEach(([k, v]) => {{
                                                window._playerVariables[k] = (window._playerVariables[k] || 0) - v;
                                            }});
                                            if (window._changePlayerVariableChoices) window._changePlayerVariableChoices(effects, -1);
                                            if (window._syncPlayerVars) window._syncPlayerVars();
                                            if (window.updateStatsUI) window.updateStatsUI();
                                        }}
                                        if (window._changePlayerLegacyFlags) window._changePlayerLegacyFlags(legacyFlags, -1);

                                        // 清除分支选择记录
                                        localStorage.removeItem('_branchChosen_{node_id}_' + cur);
                                    }}
                                }}

                                const state = readLauncherState();
                                state.currentChapter = CURRENT_FOLDER;
                                state.currentNodeId = CURRENT_NODE_ID;
                                state.globalVars = Object.assign({{}}, window._playerVariables || {{}});
                                state.globalChoiceStats = JSON.parse(JSON.stringify(window._playerVariableChoices || {{}}));
                                state.globalLegacyFlags = Object.assign({{}}, window._playerLegacyFlags || {{}});
                                localStorage.setItem(LAUNCHER_STATE_KEY, JSON.stringify(state));
                                window.location.href = '../' + ROOT_FOLDER + '/index.html';
                            }};
                            wrapper.appendChild(btn);
                            document.body.appendChild(wrapper);
                        }}, 800);

                    }});
                }});
                """
                init_vars_js = json.dumps(player_variables or {}, ensure_ascii=False)
                main_js_content = (main_js_content
                    .replace('{launcher_state_key}', launcher_state_key)
                    .replace('{folder_name}', folder_name)
                    .replace('{root_folder}', '')
                    .replace('{chapter_id}', str(depth))
                    .replace('{node_id}', node_id)
                    .replace('{chap_id}', str(depth))
                    .replace('{total_steps}', str(chapter_data.get('end_index', 1)))
                    .replace('{init_vars_placeholder}', init_vars_js)
                    .replace('{{', '{')
                    .replace('}}', '}')
                )
                main_js_file = node_converter.output_dir / "js" / "main.js"

                with open(main_js_file, 'w', encoding='utf-8') as f:
                    f.write(main_js_content)

                # 创建运行脚本
                node_converter.create_run_scripts()

                print(f"节点 {node_id} 项目创建完成: {node_converter.output_dir}")

            # 4. 复制story_tree.json到输出目录
            # 确保输出目录存在
            self.output_dir.mkdir(parents=True, exist_ok=True)
            dest_story_tree = self.output_dir / "story_tree.json"
            shutil.copy2(story_tree_path, dest_story_tree)
            print(f"\n已复制story_tree.json到: {dest_story_tree}")

            # 5. 生成主菜单 index.html
            chapter_folders = [d.name for d in self.output_dir.iterdir()
                                if d.is_dir() and d.name.startswith("chapter_")]
            self.generate_index_page(chapter_folders, "")

            print("\n" + "=" * 60)
            print("所有节点项目创建完成！")
            print(f"输出目录: {self.output_dir}")
            print("每个节点的游戏文件位于重命名的章节文件夹中")
            print("=" * 60)

        except Exception as e:
            print(f"创建项目时出错: {e}")
            import traceback
            traceback.print_exc()

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
        """生成单机版启动器首页（单入口，不允许手动跳章）"""
        import re as _re
        import json as _json

        title_map = {}
        title_maps_by_lang = {}
        book_titles_by_lang = {}
        book_summaries_by_lang = {}
        book_title = None
        book_summary = None
        root_id = None
        root_folder = None
        tree_nodes = {}
        story_tree_path = self.output_dir / "story_tree.json"
        if story_tree_path.exists():
            try:
                tree, layers = normalize_story_tree(_json.loads(story_tree_path.read_text(encoding='utf-8')))
                if layers:
                    book_titles_by_lang = {str(lang): str(layer.get('book_title') or '').strip() for lang, layer in layers.items() if isinstance(layer, dict) and layer.get('book_title')}
                    book_summaries_by_lang = {str(lang): str(layer.get('book_summary') or '').strip() for lang, layer in layers.items() if isinstance(layer, dict) and layer.get('book_summary')}
                    for lang, layer in layers.items():
                        title_maps_by_lang[str(lang)] = {}
                        for node_id, node_data in (layer.get('nodes', {}) or {}).items():
                            if isinstance(node_data, dict):
                                value = node_data.get('title') or node_data.get('name') or node_data.get('chapter_title', '')
                                if value: title_maps_by_lang[str(lang)][str(node_id)] = str(value)
                book_title = tree.get('book_title')
                book_summary = tree.get('book_summary')
                root_id = tree.get('root_id')
                nodes = tree.get('nodes', tree) if isinstance(tree, dict) else tree
                if isinstance(nodes, dict):
                    tree_nodes = nodes
                    for node_id, node_data in nodes.items():
                        t = node_data.get('title') or node_data.get('name') or node_data.get('chapter_title', '')
                        if t:
                            title_map[node_id] = t
            except Exception:
                pass

        book_title = book_title or (book_titles_by_lang.get('en-US') if book_titles_by_lang else None) or 'Visual Novel'
        book_summary = book_summary or ''
        launcher_state_key = f"steam_launcher_state_{self.output_dir.name}"

        def sort_key(f):
            m = _re.match(r'chapter_(\d+)_', f)
            return (int(m.group(1)) if m else 999, f)

        def get_title(folder):
            m = _re.match(r'chapter_\d+_(.+)', folder)
            if m:
                node_suffix = m.group(1)
                for node_id, t in title_map.items():
                    if node_id.endswith(node_suffix) or node_suffix in node_id:
                        clean = _re.sub(r'\s+(node_[0-9a-f]+|chapter_\d+)\s*$', '', t).strip()
                        return clean if clean else t
            return folder.replace('_', ' ').title()

        def get_node_id(folder):
            m = _re.match(r'chapter_\d+_(.+)', folder)
            if m:
                node_suffix = m.group(1)
                for node_id in tree_nodes.keys():
                    if node_id.endswith(node_suffix) or node_suffix in node_id:
                        return node_id
            return None

        sorted_folders = sorted(chapter_folders, key=sort_key)
        if not root_folder and sorted_folders:
            root_folder = sorted_folders[0]

        # 按 depth 分组章节
        depth_groups = {}
        for folder in sorted_folders:
            m = _re.match(r'chapter_(\d+)_', folder)
            depth_num = int(m.group(1)) if m else 0
            if depth_num not in depth_groups:
                depth_groups[depth_num] = []
            depth_groups[depth_num].append(folder)

        # 构建树形结构的 HTML（按层级分组）
        cards_html = ''
        for depth in sorted(depth_groups.keys()):
            folders_in_depth = depth_groups[depth]
            cards_html += f'            <div class="depth-row" data-depth="{depth}">\n'
            for folder in folders_in_depth:
                title = get_title(folder)
                card_node_id = get_node_id(folder) or ''
                cards_html += f'''                <div class="chapter-card" data-folder="{folder}" data-depth="{depth}">
                    <span class="depth-tag">CHAPTER {depth}</span>
                    <h3 data-node-id="{card_node_id}">{title}</h3>
                    <p class="chapter-status">Locked</p>
                </div>\n'''
            cards_html += '            </div>\n'

        index_i18n = {
            'defaultLang': 'en-US' if 'en-US' in book_titles_by_lang else (next(iter(book_titles_by_lang), 'en-US')),
            'bookTitles': book_titles_by_lang,
            'bookSummaries': book_summaries_by_lang,
            'chapterTitles': title_maps_by_lang,
        }
        index_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{book_title} - Launcher</title>
    <style>
        :root {{
            --primary-color: #8a2be2;
            --secondary-color: #4facfe;
            --bg-dark: #0f172a;
            --glass-bg: rgba(15, 23, 42, 0.6);
            --glass-border: rgba(255, 255, 255, 0.1);
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --text-accent: #cbd5e1;
            --success: #34d399;
            --active: #fbbf24;
        }}
        body {{
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            margin: 0;
            padding: 40px 20px;
            background-color: var(--bg-dark);
            background-image:
                radial-gradient(circle at 15% 50%, rgba(138, 43, 226, 0.15), transparent 25%),
                radial-gradient(circle at 85% 30%, rgba(79, 172, 254, 0.15), transparent 25%);
            background-attachment: fixed;
            min-height: 100vh;
            color: var(--text-main);
            box-sizing: border-box;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            background: var(--glass-bg);
            backdrop-filter: blur(20px) saturate(150%);
            -webkit-backdrop-filter: blur(20px) saturate(150%);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 40px 50px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
        }}
        .game-header {{ text-align: center; margin-bottom: 30px; }}
        h1 {{
            margin: 0 0 15px 0;
            font-size: 3em;
            font-weight: 800;
            letter-spacing: 2px;
            text-transform: uppercase;
            background: linear-gradient(to right, var(--secondary-color), var(--primary-color));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .book-summary {{
            background: rgba(0, 0, 0, 0.25);
            border-left: 4px solid var(--primary-color);
            padding: 20px 25px;
            border-radius: 0 12px 12px 0;
            font-size: 0.95em;
            line-height: 1.6;
            color: var(--text-accent);
            text-align: justify;
            margin: 0 auto;
            max-width: 800px;
        }}
        .launcher-panel {{
            margin: 30px 0;
            padding: 24px;
            border: 1px solid var(--glass-border);
            border-radius: 18px;
            background: rgba(255,255,255,0.04);
            display: flex;
            flex-direction: column;
            gap: 18px;
            align-items: center;
        }}
        .launcher-status {{
            color: var(--text-accent);
            font-size: 0.95rem;
            text-align: center;
        }}
        .launcher-actions {{
            display: flex;
            gap: 14px;
            flex-wrap: wrap;
            justify-content: center;
        }}
        .launcher-btn {{
            border: 1px solid rgba(255,255,255,0.2);
            background: rgba(255,255,255,0.08);
            color: var(--text-main);
            padding: 12px 24px;
            border-radius: 999px;
            font-size: 0.95rem;
            cursor: pointer;
        }}
        .launcher-btn.primary {{
            background: linear-gradient(135deg, rgba(79,172,254,0.95), rgba(138,43,226,0.95));
            border: none;
        }}
        .section-title {{
            text-align: center;
            color: var(--text-muted);
            margin: 40px 0 25px 0;
            font-size: 1.1em;
            letter-spacing: 2px;
            text-transform: uppercase;
        }}
        .chapter-list {{
            display: flex;
            flex-direction: column;
            gap: 30px;
            padding: 20px 0;
        }}
        .depth-row {{
            display: flex;
            align-items: stretch;
            gap: 20px;
            overflow-x: auto;
            padding-bottom: 10px;
        }}
        .depth-row::-webkit-scrollbar {{
            height: 8px;
        }}
        .depth-row::-webkit-scrollbar-track {{
            background: rgba(255,255,255,0.05);
            border-radius: 4px;
        }}
        .depth-row::-webkit-scrollbar-thumb {{
            background: rgba(255,255,255,0.2);
            border-radius: 4px;
        }}
        .depth-row::-webkit-scrollbar-thumb:hover {{
            background: rgba(255,255,255,0.3);
        }}
        .chapter-card {{
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--glass-border);
            border-radius: 12px;
            padding: 20px;
            color: var(--text-main);
            display: flex;
            flex-direction: column;
            min-height: 140px;
            min-width: 280px;
            flex-shrink: 0;
            transition: all 0.3s ease;
        }}
        .chapter-card:hover {{
            background: rgba(255, 255, 255, 0.06);
            transform: translateY(-4px);
            box-shadow: 0 8px 16px rgba(0, 0, 0, 0.3);
        }}
        .chapter-card.current {{
            border-color: rgba(251,191,36,0.7);
            box-shadow: 0 0 0 1px rgba(251,191,36,0.35) inset;
            background: rgba(251,191,36,0.05);
        }}
        .chapter-card.completed {{
            border-color: rgba(52,211,153,0.65);
            background: rgba(52,211,153,0.03);
        }}
        .depth-tag {{
            font-size: 0.75em;
            font-family: monospace;
            color: var(--secondary-color);
            margin-bottom: 8px;
            letter-spacing: 1px;
            text-transform: uppercase;
        }}
        .chapter-card h3 {{
            margin: 0 0 15px 0;
            font-size: 1.1em;
            font-weight: 600;
            line-height: 1.4;
            color: #fff;
            flex-grow: 1;
        }}
        .chapter-status {{
            margin: 0;
            color: var(--text-muted);
            font-size: 0.9em;
        }}
        .chapter-card.completed .chapter-status {{ color: var(--success); }}
        .chapter-card.current .chapter-status {{ color: var(--active); }}
        @media (max-width: 768px) {{
            .container {{ padding: 28px 18px; }}
            h1 {{ font-size: 2.2em; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="game-header">
            <h1>{book_title}</h1>
            <div class="book-summary">{book_summary}</div>
        </div>

        <div class="launcher-panel">
            <div class="launcher-status" id="launcher-status">Preparing story state...</div>
            <div class="launcher-actions">
                <button class="launcher-btn primary" id="continue-btn">Continue</button>
                <button class="launcher-btn" id="new-game-btn">New Game</button>
            </div>
        </div>

        <div class="section-title">Chapter Progress</div>
        <div class="chapter-list">
{cards_html}        </div>
    </div>

    <script>
        const LAUNCHER_STATE_KEY = {json.dumps(launcher_state_key)};
        const ROOT_FOLDER = {json.dumps(root_folder or '')};
        const CHAPTERS = {json.dumps(sorted_folders, ensure_ascii=False)};
        const INDEX_I18N = {json.dumps(index_i18n, ensure_ascii=False)};
        function resolveLang(raw) {{
            const value = String(raw || '').trim().replace(/_/g, '-').toLowerCase();
            if (value === 'ja' || value.indexOf('ja-') === 0) return 'ja-JP';
            if (value === 'en' || value.indexOf('en-') === 0) return 'en-US';
            return Object.keys(INDEX_I18N.bookTitles || {{}}).find((x) => x.toLowerCase() === value) || INDEX_I18N.defaultLang || 'en-US';
        }}
        const PAGE_LANG = resolveLang(new URLSearchParams(window.location.search).get('lang'));
        const PAGE_UI = PAGE_LANG.toLowerCase().indexOf('ja') === 0
            ? {{continue:'続ける', newGame:'新規ゲーム', chapterProgress:'章の進行状況', current:'現在の章', completed:'完了', locked:'ロック中', preparing:'ストーリーを準備中...'}}
            : {{continue:'Continue', newGame:'New Game', chapterProgress:'Chapter Progress', current:'Current Chapter', completed:'Completed', locked:'Locked', preparing:'Preparing story state...'}};
        document.documentElement.lang = PAGE_LANG;
        const PAGE_BOOK_TITLE = (INDEX_I18N.bookTitles && (INDEX_I18N.bookTitles[PAGE_LANG] || INDEX_I18N.bookTitles[INDEX_I18N.defaultLang])) || 'Visual Novel';
        const PAGE_BOOK_SUMMARY = (INDEX_I18N.bookSummaries && (INDEX_I18N.bookSummaries[PAGE_LANG] || INDEX_I18N.bookSummaries[INDEX_I18N.defaultLang])) || '';
        document.title = PAGE_BOOK_TITLE + ' - Launcher';
        document.querySelector('.game-header h1').textContent = PAGE_BOOK_TITLE;
        document.querySelector('.book-summary').textContent = PAGE_BOOK_SUMMARY;
        document.querySelector('.section-title').textContent = PAGE_UI.chapterProgress;
        document.getElementById('continue-btn').textContent = PAGE_UI.continue;
        document.getElementById('new-game-btn').textContent = PAGE_UI.newGame;
        document.getElementById('launcher-status').textContent = PAGE_UI.preparing;
        document.querySelectorAll('.chapter-card').forEach((card) => {{
            const id = card.querySelector('h3') && card.querySelector('h3').dataset.nodeId;
            const title = INDEX_I18N.chapterTitles && INDEX_I18N.chapterTitles[PAGE_LANG] && INDEX_I18N.chapterTitles[PAGE_LANG][id];
            if (title) card.querySelector('h3').textContent = title;
        }});

        function readState() {{
            try {{
                return JSON.parse(localStorage.getItem(LAUNCHER_STATE_KEY) || '{{}}');
            }} catch (e) {{
                return {{}};
            }}
        }}

        function writeState(state) {{
            localStorage.setItem(LAUNCHER_STATE_KEY, JSON.stringify(state));
        }}

        function chapterLabel(folder, info, state) {{
            // 当前正在玩的章节
            if (state.currentChapter === folder) return 'Current Chapter';
            // 已经走过的章节（有访问记录）
            if (info && (info.completed || info.lastMaterial)) return 'Completed';
            // 未解锁或未走过的章节
            return 'Locked';
        }}

        function refreshCards(state) {{
            document.querySelectorAll('.chapter-card').forEach(card => {{
                const folder = card.dataset.folder;
                const info = (state.chapters || {{}})[folder] || null;
                card.classList.toggle('current', state.currentChapter === folder);
                card.classList.toggle('completed', !!(info && info.completed));
                const statusEl = card.querySelector('.chapter-status');
                if (statusEl) statusEl.textContent = chapterLabel(folder, info, state);
            }});
        }}

        function currentTarget(state) {{
            if (state.currentChapter) return state.currentChapter;
            return ROOT_FOLDER || CHAPTERS[0] || '';
        }}

        function updateLauncherStatus(state) {{
            const status = document.getElementById('launcher-status');
            const target = currentTarget(state);
            if (!status) return;
            if (!target) {{
                status.textContent = 'No playable chapter found.';
                return;
            }}
            const chapterInfo = (state.chapters || {{}})[target];
            if (chapterInfo && chapterInfo.completed) {{
                status.textContent = 'Ready to continue from the next unlocked chapter.';
            }} else if (chapterInfo && chapterInfo.lastMaterial) {{
                status.textContent = `Resume from ${{target.replaceAll('_', ' ')}} · beat ${{chapterInfo.lastMaterial}}`;
            }} else {{
                status.textContent = `Start from ${{target.replaceAll('_', ' ')}}`;
            }}
        }}

        document.getElementById('continue-btn').addEventListener('click', () => {{
            const state = readState();
            const target = currentTarget(state);
            if (!target) return;
            const chapterInfo = (state.chapters || {{}})[target] || {{}};
            const url = new URL(target + '/index.html', window.location.href);
            if (state.globalVars) {{
                url.searchParams.set('vars', JSON.stringify(state.globalVars));
            }}
            if (state.globalChoiceStats) {{
                url.searchParams.set('choice_stats', JSON.stringify(state.globalChoiceStats));
            }}
            if (state.globalLegacyFlags) {{
                url.searchParams.set('legacy_flags', JSON.stringify(state.globalLegacyFlags));
            }}
            if (chapterInfo.lastMaterial) {{
                url.searchParams.set('continue', chapterInfo.lastMaterial);
            }}
            url.searchParams.set('lang', PAGE_LANG);
            window.location.href = url.toString();
        }});

        document.getElementById('new-game-btn').addEventListener('click', () => {{
            // 清理所有章节的进度和入口快照 localStorage
            try {{
                const keysToRemove = [];
                for (let i = 0; i < localStorage.length; i++) {{
                    const k = localStorage.key(i);
                    if (k && (k.startsWith('视觉小说_') || k.startsWith('_chapterEntryVars_') || k.startsWith('_chapterCurrentVars_') || k.startsWith('_chapterEntryChoiceStats_') || k.startsWith('_chapterCurrentChoiceStats_') || k.startsWith('_chapterEntryLegacyFlags_'))) {{
                        keysToRemove.push(k);
                    }}
                }}
                keysToRemove.forEach(k => localStorage.removeItem(k));
            }} catch(e) {{}}
            const target = ROOT_FOLDER || CHAPTERS[0] || '';
            if (!target) return;
            const state = {{ currentChapter: target, chapters: {{}} }};
            writeState(state);
            const url = new URL(target + '/index.html', window.location.href);
            url.searchParams.set('newgame', '1');
            url.searchParams.set('lang', PAGE_LANG);
            window.location.href = url.toString();
        }});

        const state = readState();
        if (!state.currentChapter) {{
            state.currentChapter = ROOT_FOLDER || CHAPTERS[0] || '';
            state.chapters = state.chapters || {{}};
            writeState(state);
        }}
        refreshCards(state);
        updateLauncherStatus(state);
    </script>
</body>
</html>'''

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
