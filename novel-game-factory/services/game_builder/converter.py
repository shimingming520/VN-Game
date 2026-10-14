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

# 在线版运行期只接受这两个规范化 locale。不要在这里加入短码、语言名或
# 其他地区变体；URL 中的非法值统一回退到英语。
ONLINE_GAME_LANGUAGES = ("en-US", "ja-JP")


def _chapter_directory_name_for_node(node_id, depth):
    """Return the builder's canonical per-node material directory name."""
    node_id = str(node_id)
    suffix = node_id[5:] if node_id.startswith("node_") else node_id
    return f"chapter_{depth}_{suffix}"


def _safe_subtitle_stem(beat, chapter_key):
    subtitle_id = str(beat.get("subtitle_id") or "").strip()
    if subtitle_id:
        return subtitle_id

    beat_id = str(beat.get("beat_id") or beat.get("id") or "").strip()
    scene_id = str(beat.get("scene_id") or "").strip()
    if scene_id and beat_id:
        return f"{scene_id}-{beat_id}"
    if beat_id:
        return f"{chapter_key}-{beat_id}"
    return ""


def _beat_text_for_subtitle(beat):
    for key in ("content", "text", "dialogue", "system_text"):
        value = beat.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _speaker_id_for_subtitle(beat):
    return str(
        beat.get("speaker_id")
        or beat.get("speaker_name")
        or beat.get("full_name")
        or "PangBai"
    ).strip() or "PangBai"


def materialize_local_visual_sources(source_dir, plot_data, story_tree):
    """Create minimal per-node subtitle roots for the local no-audio pipeline.

    The online builder historically treated node/chapter material directories
    as proof that a chapter can be built. Local visual mode skips audio
    generation, so no such directories exist even though ultimate JSON and
    images are present. This function writes only UTF-8 subtitle text files
    derived from plot data; images remain under ``source_dir/images``.
    """
    source_dir = Path(source_dir)
    story_tree, _layers = normalize_story_tree(story_tree or {})
    nodes = story_tree.get("nodes", {}) if isinstance(story_tree, dict) else {}
    if not isinstance(nodes, dict) or not isinstance(plot_data, dict):
        return []

    created = []
    for node_id, node_data in nodes.items():
        beats = plot_data.get(node_id)
        if not isinstance(beats, list) or not beats:
            continue

        depth = 1
        if isinstance(node_data, dict):
            try:
                depth = int(node_data.get("depth", 1) or 1)
            except (TypeError, ValueError):
                depth = 1

        chapter_dir = source_dir / _chapter_directory_name_for_node(node_id, depth)
        subtitle_dir = chapter_dir / "audios" / f"{DEFAULT_AUDIO_LANGUAGE}-subtitle"
        subtitle_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            stem = _safe_subtitle_stem(beat, str(node_id))
            text = _beat_text_for_subtitle(beat)
            if not stem or not text:
                continue
            line = f"{_speaker_id_for_subtitle(beat)}：{text}"
            (subtitle_dir / f"{stem}.txt").write_text(line, encoding="utf-8")
            written += 1

        if written:
            created.append(chapter_dir)
        elif not any(subtitle_dir.glob("*.txt")):
            try:
                subtitle_dir.rmdir()
                (chapter_dir / "audios").rmdir()
                chapter_dir.rmdir()
            except OSError:
                pass

    return created


def load_local_visual_plot_data(source_dir, task_id=None):
    """Load plot text for local visual builds, preferring local files."""
    source_dir = Path(source_dir)
    local_candidates = [
        source_dir / "plots.json",
        source_dir / "plot.json",
    ]
    for candidate in local_candidates:
        if not candidate.is_file():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            print(f"⚠️ 本地剧情文件读取失败: {candidate} ({exc})")

    task_id = str(task_id or source_dir.name)
    if not task_id:
        return {}

    try:
        from sqlalchemy import create_engine, text as sa_text
        from common.config import PLATFORM_DATABASE_URL

        engine = create_engine(PLATFORM_DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(
                sa_text("SELECT data FROM plots WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).fetchone()
        if not row:
            return {}
        data = row[0]
        if isinstance(data, str):
            data = json.loads(data)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"⚠️ 无法加载本地视觉剧情文本: {exc}")
        return {}


def choose_default_audio_language(languages, book_language=None):
    """在线版未传 ``?lang`` 时始终使用英语。"""
    return DEFAULT_AUDIO_LANGUAGE


def normalize_story_tree(story_tree):
    """Return ``(default_layer, layers_by_language)`` for flat or localized trees.

    Older exports put ``nodes`` directly at the top level.  Newer exports wrap
    that same payload in language keys (for example ``en-US`` and ``ja-JP``).
    Keeping this normalization at the builder boundary prevents every caller
    from accidentally reading the wrapper as if it were a node map.
    """
    if not isinstance(story_tree, dict):
        return {}, {}

    def _language_payload(value):
        """Extract a complete story-tree payload from one language layer.

        Current exports put ``nodes`` directly below ``en-US``/``ja-JP``.
        A few intermediate exports wrapped that payload once more under
        ``story_tree`` (or ``data``/``tree``).  Accepting that harmless wrapper
        keeps the language boundary strict without reintroducing old locale
        aliases.
        """
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("nodes"), dict):
            return value
        for wrapper_key in ("story_tree", "storyTree", "tree", "data"):
            nested = value.get(wrapper_key)
            if isinstance(nested, dict) and isinstance(nested.get("nodes"), dict):
                return nested
        return None

    # 仅把在线版支持的语言层当作语言 wrapper。其他顶层字典（例如旧的
    # zh-CN/English 层）必须忽略，避免它们被意外暴露给运行期语言解析。
    layers = {}
    for key, value in story_tree.items():
        lang = str(key)
        if lang not in ONLINE_GAME_LANGUAGES:
            continue
        payload = _language_payload(value)
        if payload is not None:
            layers[lang] = payload
    if not layers:
        return story_tree, {}

    def _pick(key):
        wanted = str(key or "").lower()
        for lang, layer in layers.items():
            if lang.lower() == wanted:
                return lang, layer
        return None, None

    default_lang, default_layer = _pick("en-US")
    if default_layer is None:
        default_lang, default_layer = next(iter(layers.items()))

    # Metadata occasionally remains at the wrapper level (e.g. art_style
    # written by a rebuild task).  Fill only missing values; localized values
    # always win over wrapper metadata.
    merged = dict(story_tree)
    merged.update(default_layer)
    for key, value in story_tree.items():
        if key not in layers and key not in merged:
            merged[key] = value
    merged["nodes"] = default_layer.get("nodes", {})
    return merged, layers


def extract_story_ui_text(layers):
    """Read optional localized UI dictionaries from story-tree language layers."""
    result = {}
    for lang, layer in (layers or {}).items():
        if not isinstance(layer, dict):
            continue
        for key in ("ui_text", "UI_text", "ui", "UI", "labels"):
            value = layer.get(key)
            if isinstance(value, dict):
                result[str(lang)] = {
                    str(k): str(v) for k, v in value.items()
                    if isinstance(v, (str, int, float))
                }
                break
    return result


def extract_story_choice_text(layers, node_id):
    """Build ``lang -> child_id -> choice_text`` for one story-tree node.

    ``story_tree.json`` may wrap each complete tree in a language key.  The
    runtime script is generated once, so choices must be carried as a small
    language map instead of being baked from the default layer only.
    """
    result = {}
    for lang, layer in (layers or {}).items():
        if not isinstance(layer, dict):
            continue
        nodes = layer.get("nodes")
        node = nodes.get(str(node_id), {}) if isinstance(nodes, dict) else {}
        children = node.get("children", []) if isinstance(node, dict) else []
        if not isinstance(children, list):
            continue
        child_map = {}
        for child in children:
            if not isinstance(child, dict) or not child.get("child_id"):
                continue
            value = child.get("choice_text")
            if value is None:
                continue
            child_map[str(child["child_id"])] = str(value)
        if child_map:
            result[str(lang)] = child_map
    return result


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
                 enable_oss_upload=False, book_language="zh-CN", ui_mode="Other",
                 skip_asset_copy=False):
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
        self.languages = [DEFAULT_AUDIO_LANGUAGE]
        self.default_language = DEFAULT_AUDIO_LANGUAGE
        self.i18n_text = {}        # lang -> {subtitle_id: 纯正文}
        self.voices_by_lang = {}   # lang -> {subtitle_id: 'xxx.wav'}
        self.names_by_lang = {}    # lang -> {speaker_id: 显示名}
        # story_tree.json 的语言层（扁平旧格式也会被规范化为单层）。
        self.story_tree_by_lang = {}
        self.story_tree_default_language = DEFAULT_AUDIO_LANGUAGE
        self.story_ui_by_lang = {}
        self.story_choice_text_by_lang = {}
        # 主调度 JSON 中的完整 beat 元信息。分支 C1 即使文件缺失，
        # 仍可用这份元信息决定选项界面场景和分支状态。
        self.master_beat_meta = {}  # beat_id_raw -> meta
        self.branch_control_content = {}  # numeric index -> C/C1 effects raw text
        # 主调度 JSON 会被每个节点转换器复用，避免按节点反复扫描和解析根目录。
        self._master_json_cache = None
        self._source_node_id = None
        # 同一节点可能同时存在 node_* 与 chapter_* 两套素材目录。
        # source_dir 是按优先级选出的主目录；asset_source_dirs 保留所有匹配目录，
        # 供字幕、语音、BGM、Ambience、SFX 扫描与复制时合并使用。
        self.asset_source_dirs = [self.source_dir]

        # 资产统一保留在共享本地存储，不上传 OSS，也不删除本地文件。
        self.delete_local_assets = False
        self.enable_oss_upload = False

        # 只重建脚本时置 True：素材清单照常计算（generate_asset_declarations 需要），
        # 但不实际复制文件。素材早已在 OSS 上，重复搬运只是白花时间。
        self.skip_asset_copy = skip_asset_copy

        # 主 JSON 中是否存在任何 system_text（决定是否在前端注入 system 按钮）
        # 由 create_per_node_projects 全局扫描后赋值并下发给子 converter
        self._has_system_text = True
        # story_tree 没有 player_variables 时，不创建数值按钮和面板。
        # 默认 True 保持直接调用旧接口时的兼容性，正式构建会按 story_tree 覆盖。
        self._has_player_variables = True
        # Schema copied from story_tree.player_variables for runtime input
        # validation.  Keep this as a small map; the generated client only
        # accepts values that belong to the current game.
        self._player_variables_schema = {}

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
        if self._master_json_cache is not None:
            return self._master_json_cache

        ignore_names = {"story_tree.json", "atmophere.json", "options.json"}
        candidates_dirs = [self.source_dir, self.source_dir.parent]
        for d in candidates_dirs:
            if not d.exists():
                continue
            # 通常主表就是 {task_id}.json，先走 O(1) 的已知路径；再兼容旧文件名。
            preferred = d / f"{d.name}.json"
            json_candidates = []
            if preferred.is_file():
                json_candidates.append(preferred)
            json_candidates.extend(jf for jf in d.glob("*.json") if jf != preferred)
            for jf in json_candidates:
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
                            self._master_json_cache = (jf, data)
                            return self._master_json_cache
                except Exception:
                    continue
        self._master_json_cache = (None, None)
        return self._master_json_cache

    @staticmethod
    def _chapter_directory_name(node_id, depth):
        """把 story_tree 节点转换为 chapter_{depth}_{suffix} 目录名。"""
        node_id = str(node_id)
        suffix = node_id[5:] if node_id.startswith("node_") else node_id
        return f"chapter_{depth}_{suffix}"

    @staticmethod
    def _index_source_directories(source_root):
        """只遍历一次素材根目录，供全部节点进行 O(1) 目录查找。"""
        source_root = Path(source_root)
        if not source_root.is_dir():
            return {}
        return {entry.name: entry for entry in source_root.iterdir() if entry.is_dir()}

    @classmethod
    def _resolve_node_source_directory(cls, source_index, node_id, depth):
        """兼容 node_xxx 与 chapter_{depth}_{xxx} 两种素材目录命名。

        返回值保持为单个主目录，兼容旧调用方；需要合并两套目录时请使用
        ``_resolve_node_source_directories``。
        """
        directories = cls._resolve_node_source_directories(source_index, node_id, depth)
        return directories[0] if directories else None

    @staticmethod
    def _valid_asset_file_count(directory):
        """统计目录中可用于游戏构建的有效素材文件数量。"""
        if directory is None or not directory.is_dir():
            return 0
        valid_suffixes = {'.txt', '.wav', '.mp3', '.ogg', '.png', '.jpg', '.jpeg', '.webp'}
        count = 0
        try:
            for path in directory.rglob('*'):
                if (
                    path.is_file()
                    and path.suffix.lower() in valid_suffixes
                    and not path.name.startswith('.')
                    and path.stat().st_size > 0
                ):
                    count += 1
        except OSError:
            return count
        return count

    @staticmethod
    def _is_usable_asset_file(path):
        """Return true only for a non-empty regular asset file."""
        if path is None:
            return False
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @classmethod
    def _directory_material_priority(cls, directory):
        """按字幕 > 音频 > 有效文件数选择 node/chapter 主目录。"""
        if directory is None or not directory.is_dir():
            return (0, 0, 0)
        audio_root = directory / 'audios'
        subtitle_dirs = [
            audio_root / 'en-US-subtitle', audio_root / 'ja-JP-subtitle',
            audio_root / 'subtitle', directory / 'subtitle',
        ]
        audio_dirs = [
            audio_root / 'en-US-audio', audio_root / 'ja-JP-audio',
            audio_root / 'audio', directory / 'audio',
        ]
        # Match ``_valid_asset_file_count``: zero-byte placeholders and
        # hidden metadata files are not usable game material.  Counting them
        # here would let an empty node/chapter directory win the priority
        # tuple over a sibling containing real subtitles or audio.
        subtitle_count = sum(
            len([
                p for p in d.glob('*.txt')
                if p.is_file() and not p.name.startswith('.') and p.stat().st_size > 0
            ])
            for d in subtitle_dirs if d.is_dir()
        )
        audio_count = sum(
            len([
                p for p in d.iterdir()
                if p.is_file()
                and not p.name.startswith('.')
                and p.suffix.lower() in {'.wav', '.mp3', '.ogg'}
                and p.stat().st_size > 0
            ])
            for d in audio_dirs if d.is_dir()
        )
        # 第一关键字只区分“是否有字幕”，第二关键字区分“是否有音频”；
        # 同等级再比较有效文件总数，避免空目录抢占真实素材目录。
        return (1 if subtitle_count else 0, 1 if audio_count else 0, cls._valid_asset_file_count(directory))

    @classmethod
    def _resolve_node_source_directories(cls, source_index, node_id, depth):
        """返回匹配节点的全部素材目录，主目录按优先级排在第一位。

        例如 node_xxx 含字幕/语音而 chapter_7_xxx 只含 BGM 时，返回
        ``[node_xxx, chapter_7_xxx]``，后续扫描会从两者合并取材。
        """
        names = (str(node_id), cls._chapter_directory_name(node_id, depth))
        candidates = []
        seen = set()
        for name in names:
            directory = source_index.get(name)
            if directory is not None and directory not in seen:
                candidates.append(directory)
                seen.add(directory)
        candidates.sort(
            key=lambda directory: (
                cls._directory_material_priority(directory),
                # 同分时保留 node_* 优先的稳定顺序。
                1 if directory.name == str(node_id) else 0,
            ),
            reverse=True,
        )
        # Empty/metadata-only directories must not win the lookup.  Keep all
        # matching directories that contain at least one supported material so
        # a node_* directory with dialogue and a chapter_* directory with BGM
        # can still be merged.  When neither prefix has a usable file, return
        # an empty list; the builder will report a failed build instead of
        # publishing an empty chapter.
        return [
            directory for directory in candidates
            if cls._valid_asset_file_count(directory) > 0
        ]

    def _asset_roots(self):
        """返回去重后的素材根目录，主目录始终排在最前。"""
        roots = getattr(self, 'asset_source_dirs', None) or [self.source_dir]
        result = []
        seen = set()
        for root in roots:
            root = Path(root)
            key = str(root.resolve())
            if key not in seen:
                result.append(root)
                seen.add(key)
        return result

    def _find_asset_path(self, relative_paths):
        """按主目录优先，从合并素材目录中寻找第一个存在的文件。"""
        if isinstance(relative_paths, (str, Path)):
            relative_paths = [relative_paths]
        for root in self._asset_roots():
            for relative in relative_paths:
                path = root / relative
                if self._is_usable_asset_file(path):
                    return path
        return None

    def _resolve_chapter_key(self, master_data, node_dir_name, node_id=None):
        """根据节点目录名定位 master_data 中的章节 key

        - chapter_1 目录 → "chapter_1"
        - node_xxxx 目录 → "node_xxxx"（直接同名）
        - chapter_{depth}_{xxxx} 目录 → "node_xxxx"
        - 否则尝试 startswith 匹配
        """
        if node_id and node_id in master_data:
            return node_id
        if node_dir_name in master_data:
            return node_dir_name

        chapter_match = re.fullmatch(r"chapter_\d+_(.+)", str(node_dir_name))
        if chapter_match:
            suffix = chapter_match.group(1)
            for candidate in (suffix, f"node_{suffix}"):
                if candidate in master_data:
                    return candidate

        for k in master_data.keys():
            if k == node_dir_name or k.endswith(node_dir_name) or node_dir_name.endswith(k):
                return k
        return None

    @staticmethod
    def _is_generic_continue_choice(choice_text):
        """识别章节末尾的通用继续按钮，保留剧情含义明确的其他选项。"""
        normalized = re.sub(r"[\s.!！?？。…]+", "", str(choice_text or "")).lower()
        return normalized in {
            "继续", "继续剧情", "继续阅读", "下一章", "下一章节", "进入下一章",
            "continue", "continuestory", "next", "nextchapter",
            "続ける", "続行", "次へ", "次の章へ",
        }

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
            # 在线版运行期严格只支持 en-US / ja-JP。不要把旧的
            # zh-CN 或其他语言层继续打包到前端，避免任何兜底路径
            # 意外把中文角色名泄漏到在线 UI。
            self.names_by_lang = {}
            for k, v in change_data.items():
                if k in {"conversion", "new-name"}:
                    continue
                if str(k) not in ONLINE_GAME_LANGUAGES:
                    continue
                if isinstance(v, dict) and all(isinstance(x, str) for x in v.values()):
                    self.names_by_lang[str(k)] = {str(a): str(b) for a, b in v.items()}

            # 生成的静态角色定义必须使用英语作为在线默认层；运行期
            # 再由 URL 的精确 locale 覆写为 ja-JP。这样即使前端初始化
            # 顺序异常，也不会先显示 book_language=zh-CN 的角色名。
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
            print(f"✅ 加载说话人映射 ({preferred_language or 'legacy-flat'}): {len(self.name_map)} 个；全语言 {list(self.names_by_lang.keys())}")
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
        chapter_key = self._resolve_chapter_key(
            master_data,
            node_dir_name,
            node_id=self._source_node_id,
        )

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
        # bgm/sfx 通用。node_* 与 chapter_* 两套目录可能各自只包含部分素材，
        # 因此这里必须在所有匹配根目录上合并查找，而不能只看 self.source_dir。
        asset_roots = self._asset_roots()
        audios_root = self.source_dir / "audios"

        def _dirs_for(relative):
            return [root / relative for root in asset_roots if (root / relative).is_dir()]

        def _first_file(directories, filename):
            # A matching zero-byte placeholder is not usable material.  When
            # node_* and chapter_* roots are merged, always prefer the first
            # non-empty file so a placeholder in the priority root cannot
            # mask the real subtitle/audio in the companion root.
            for directory in directories:
                path = directory / filename
                if self._is_usable_asset_file(path):
                    return path
            return None

        def _expected_file(directories, filename):
            return (directories[0] / filename) if directories else (audios_root / filename)

        self.languages = []
        for lang in ONLINE_GAME_LANGUAGES:
            if (_dirs_for(f"audios/{lang}-subtitle")
                    or _dirs_for(f"audios/{lang}-audio")):
                self.languages.append(lang)
        # 旧结构兼容：没有 {lang}-subtitle 时退回单语言 audios/subtitle
        legacy_mode = not self.languages
        if legacy_mode:
            self.languages = [DEFAULT_AUDIO_LANGUAGE]

        def _audio_dirs_for(lang):
            return _dirs_for("audios/audio" if legacy_mode else f"audios/{lang}-audio")

        def _subtitle_dirs_for(lang):
            return _dirs_for("audios/subtitle" if legacy_mode else f"audios/{lang}-subtitle")

        # 运行期默认英语；book_language 仍只负责源文本与角色显示名语义。
        # 扫描阶段不能假定英语素材一定存在：例如仅有 ja-JP 的任务仍应
        # 生成可玩的 beat。音频和字幕分别选择第一个实际存在的语言目录，
        # 这样“英语只有音频、日语只有字幕”等部分素材组合也不会丢掉整段。
        self.default_language = choose_default_audio_language(
            self.languages, self.book_language
        )
        ordered_languages = [
            self.default_language,
            *[x for x in self.languages if x != self.default_language],
        ]
        scan_audio_language = next(
            (lang for lang in ordered_languages if _audio_dirs_for(lang)),
            None,
        )
        scan_subtitle_language = next(
            (lang for lang in ordered_languages if _subtitle_dirs_for(lang)),
            None,
        )

        # 默认语言目录（驱动 beat 结构、index、meta）。若默认语言没有
        # 对应目录，使用实际存在的语言作为构建扫描源；这不改变运行期
        # URL 语言解析，window._defaultLang 仍然固定为 en-US。
        # Search all available locale roots in deterministic preference order,
        # not just the first language that has a directory.  A provider export
        # can legitimately split beats across locales (for example English
        # voice for one beat and Japanese voice for the next); restricting the
        # primary scan to one locale would silently remove those beats.
        audio_dirs = [
            directory
            for lang in ordered_languages
            for directory in _audio_dirs_for(lang)
        ]
        subtitle_dirs = [
            directory
            for lang in ordered_languages
            for directory in _subtitle_dirs_for(lang)
        ]
        self.scan_audio_language = scan_audio_language or self.default_language
        self.scan_subtitle_language = scan_subtitle_language or self.default_language
        bgm_dirs = _dirs_for("audios/bgm")
        sfx_dirs = _dirs_for("audios/sfx")
        # Ambience（环境底音）目录
        ambience_dirs = _dirs_for("audios/ambience")

        # 各语言字幕文本 / 音频存在性
        # self.i18n_text[lang][subtitle_id] = 纯正文（去 speaker 前缀）
        # self.voices_by_lang[lang][subtitle_id] = '{subtitle_id}.wav'（存在才记）
        self.i18n_text = {lang: {} for lang in self.languages}
        self.voices_by_lang = {lang: {} for lang in self.languages}
        print(
            f"  发现语言: {self.languages}（运行期默认 {self.default_language}；"
            f"扫描音频 {self.scan_audio_language}，扫描字幕 {self.scan_subtitle_language}）"
        )

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
                sub_path = _first_file(subtitle_dirs, f"{subtitle_id}.txt")
                if self._is_usable_asset_file(sub_path):
                    self.subtitle_files.append({
                        'path': sub_path,
                        'name': sub_path.name,
                        'stem': subtitle_id,
                        'meta': meta,
                    })
                else:
                    print(f"  ⚠️ 字幕缺失: {_expected_file(subtitle_dirs, f'{subtitle_id}.txt')}")

                # 多语言：收集每个语言的纯正文进 i18n_text
                # 软分支字幕含 `选项文本||正文`：拆开，正文存主 key，
                # 选项存 {sid}__opt。前缀可能位于 A8/B7 等非首个 seq。
                for lang in self.languages:
                    lpath = _first_file(_subtitle_dirs_for(lang), f"{subtitle_id}.txt")
                    if self._is_usable_asset_file(lpath):
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
                        candidate = _first_file(_subtitle_dirs_for(lang), f"{subtitle_id}.txt")
                        if self._is_usable_asset_file(candidate):
                            control_raw = self.read_subtitle_file(candidate)
                            if control_raw:
                                break
                    if control_raw:
                        self.branch_control_content[num_idx] = control_raw

            # ── 音频（PangBai 旁白没有音频）──
            if subtitle_id and speaker_id and speaker_id != 'PangBai':
                audio_path = _first_file(audio_dirs, f"{subtitle_id}.wav")
                if self._is_usable_asset_file(audio_path):
                    self.audio_files.append({
                        'path': audio_path,
                        'name': audio_path.name,
                        'stem': subtitle_id,
                        'meta': meta,
                    })
                else:
                    print(f"  ⚠️ 音频缺失: {_expected_file(audio_dirs, f'{subtitle_id}.wav')} (speaker={speaker_id})")

                # 多语言：记录每个语言该 beat 音频是否存在
                for lang in self.languages:
                    lpath = _first_file(_audio_dirs_for(lang), f"{subtitle_id}.wav")
                    if self._is_usable_asset_file(lpath):
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
            bgm_path = _first_file(bgm_dirs, f"{bgm_id}.wav")
            if not self._is_usable_asset_file(bgm_path):
                # 兜底：按尾部 _start-end 模糊匹配（兼容旧 uuid 命名）
                fallback = next(
                    (match for directory in bgm_dirs
                     for match in [self._fuzzy_match_bgm_file(directory, bgm_id, start_idx, end_idx)]
                     if match is not None),
                    None,
                )
                if fallback is not None:
                    bgm_path = fallback
                    print(f"  ℹ️ BGM 名不匹配，按范围匹配到: {bgm_path.name} (期望 {bgm_id})")
            if self._is_usable_asset_file(bgm_path):
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
                print(f"  ⚠️ BGM 缺失: {_expected_file(bgm_dirs, f'{bgm_id}.wav')}")

        # 写入 Ambience（环境底音）列表（规则同 BGM）
        for ambience_id, (start_idx, end_idx) in ambience_ranges.items():
            amb_path = _first_file(ambience_dirs, f"{ambience_id}.wav")
            if not self._is_usable_asset_file(amb_path):
                # 兜底：按尾部 _start-end 模糊匹配（兼容旧 uuid 命名）
                fallback = next(
                    (match for directory in ambience_dirs
                     for match in [self._fuzzy_match_bgm_file(directory, ambience_id, start_idx, end_idx)]
                     if match is not None),
                    None,
                )
                if fallback is not None:
                    amb_path = fallback
                    print(f"  ℹ️ Ambience 名不匹配，按范围匹配到: {amb_path.name} (期望 {ambience_id})")
            if self._is_usable_asset_file(amb_path):
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
                print(f"  ⚠️ Ambience 缺失: {_expected_file(ambience_dirs, f'{ambience_id}.wav')}")

        # 写入 SFX 列表
        for sfx_id, idx in sfx_index_map.items():
            sfx_path = _first_file(sfx_dirs, f"{sfx_id}.wav")
            if not self._is_usable_asset_file(sfx_path):
                fallback = next(
                    (match for directory in sfx_dirs
                     for match in [self._fuzzy_match_sfx_file(directory, sfx_id, idx)]
                     if match is not None),
                    None,
                )
                if fallback is not None:
                    sfx_path = fallback
                    print(f"  ℹ️ SFX 名不匹配，按尾部 index 匹配到: {sfx_path.name} (期望 {sfx_id})")
            if self._is_usable_asset_file(sfx_path):
                self.sfx_files.append({
                    'path': sfx_path,
                    'name': sfx_path.name,
                    'stem': sfx_path.stem,
                    'index': idx,
                    'declared_id': sfx_id,
                })
                print(f"  发现SFX: {sfx_path.name} (索引: {idx})")
            else:
                print(f"  ⚠️ SFX 缺失: {_expected_file(sfx_dirs, f'{sfx_id}.wav')}")

    @staticmethod
    def _fuzzy_match_bgm_file(bgm_dir, bgm_id, start_idx, end_idx):
        """按尾部 _start-end 后缀在 bgm 目录里找匹配的文件（兼容旧命名）"""
        if not bgm_dir.exists():
            return None
        suffix = f"_{start_idx}-{end_idx}"
        for f in bgm_dir.glob("*.wav"):
            if f.stem.endswith(suffix) and FinalConverter._is_usable_asset_file(f):
                return f
        return None

    @staticmethod
    def _fuzzy_match_sfx_file(sfx_dir, sfx_id, idx):
        """按尾部 _index 后缀在 sfx 目录里找匹配的文件（兼容旧命名）"""
        if not sfx_dir.exists():
            return None
        suffix = f"_{idx}"
        for f in sfx_dir.glob("*.wav"):
            if f.stem.endswith(suffix) and FinalConverter._is_usable_asset_file(f):
                return f
        return None

    def _scan_assets_legacy(self):
        """旧的、按文件名扫描素材的方式（保留作回落）"""
        # 确定要扫描的节点目录列表；主目录与补充目录都要纳入，避免
        # node_* / chapter_* 混合命名时只取到一半素材。
        source_name = self.source_dir.name
        if source_name.startswith('node') or re.match(r'^chapter_\d+(?:_|$)', source_name):
            node_dirs_to_scan = self._asset_roots()
        else:
            # 单次目录遍历，同时兼容 node_xxx 与 chapter_{depth}_{xxx}；
            # outputs 根目录本身不能作为素材节点。
            node_dirs_to_scan = []
            for node_dir in self.source_dir.iterdir() if self.source_dir.is_dir() else ():
                if node_dir.is_dir() and (
                    node_dir.name.startswith('node')
                    or re.match(r'^chapter_\d+(?:_|$)', node_dir.name)
                ):
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
                            else:
                                continue
                            self.music_files.append({
                                'path': music_file,
                                'name': music_file.name,
                                'stem': music_file.stem,
                                'start': start_idx,
                                'end': end_idx
                            })
                    # Do not stop after the first existing music directory.
                    # With mixed node_*/chapter_* layouts, BGM can be split
                    # across both roots (e.g. dialogue assets in node_* and
                    # chapter-wide music in chapter_*).  Scanning every root
                    # is required to preserve the complete timeline.

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
                    # 解析 || 分隔：|| 前是选项文本，|| 后是实际对话。
                    # 上游会把 pending_option_prefix 附到“该分支的第一条
                    # 可配音对白”上；这条对白不一定是 seq=1（前面可能有
                    # system/variable/narration beat），因此这里不能把解析
                    # 限死在 seq=1。
                    option_text = None
                    actual_content = content
                    if '||' in content:
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

                # 上游的选项前缀会附到每条分支的第一条 dialogue/narration。
                # 当分支开头存在 system/variable beat 时，前缀可能落在 A8、B7
                # 等后续 seq；只读取 seq=1 会直接回退为 Option A/B。
                # 优先读取 organize 阶段已拆出的 option_text；如果某个旧
                # 字幕对象没有拆出文本，则用同一 subtitle_id 的 i18n
                # ``__opt`` 字段恢复，确保英语/日语都能在运行期选择。
                def _get_option_material(br_letter):
                    branch_subs = branch_subtitle.get(idx, {}).get(br_letter, {}) or {}
                    for seq in sorted(branch_subs):
                        sub = branch_subs.get(seq) or {}
                        text = str(sub.get('option_text') or '').strip()
                        if text:
                            return sub, text

                    # 兼容历史中 option_text 尚未拆出的对象；任何支持的
                    # 语言层存在 __opt 即视为有效选项来源。
                    for seq in sorted(branch_subs):
                        sub = branch_subs.get(seq) or {}
                        sid = str(sub.get('subtitle_id') or '').strip()
                        if not sid:
                            continue
                        opt_key = f"{sid}__opt"
                        localized = None
                        for lang in [self.default_language] + [
                            lang for lang in self.languages if lang != self.default_language
                        ]:
                            value = (self.i18n_text.get(lang, {}) or {}).get(opt_key)
                            if value is not None and str(value).strip():
                                localized = str(value).strip()
                                break
                        if localized:
                            return sub, localized
                    return None, None

                def _get_option_text(br_letter):
                    _sub, text = _get_option_material(br_letter)
                    return text

                def _get_option_key(br_letter):
                    sub, _text = _get_option_material(br_letter)
                    return sub.get('subtitle_id') if sub else None

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
                    'option_a_text': _get_option_text('A') or '␟ui:optionA␟',
                    'option_b_text': _get_option_text('B') or '␟ui:optionB␟',
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

    def _copy_asset(self, src, dst) -> bool:
        """复制单个素材；``skip_asset_copy`` 时只记账不落盘。

        返回是否“视为已就绪”。只重建脚本的场景里素材已经在 OSS 上，返回 True
        才能让调用方把它计入清单，生成的资源声明与正常构建完全一致。
        """
        if self.skip_asset_copy:
            return True
        shutil.copy2(src, dst)
        return True

    def copy_assets_for_chapter(self, chapter_data, node_id=None):
        """复制本章素材（根据音频文件名推断需要的场景和角色）"""
        if self.skip_asset_copy:
            print("仅重建脚本：跳过素材复制，只统计清单...")
        else:
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
        asset_roots = self._asset_roots()
        audios_root = self.source_dir / "audios"
        audio_count = 0
        chapter_subtitle_ids = {Path(n).stem for n in chapter_audio_files}

        def _first_usable_file(directories, filename):
            """Return the first non-empty file from merged source roots.

            A node/chapter export can contain zero-byte placeholder files in
            one root and the real asset in the other.  ``Path.is_file()``
            alone treats the placeholder as valid and silently copies it to
            the published package, which later makes voice playback appear
            to work while the browser receives an empty response.
            """
            for directory in directories:
                candidate = directory / filename
                if self._is_usable_asset_file(candidate):
                    return candidate
            return None

        for lang in self.languages:
            # 优先使用该语言目录；若当前任务是旧单语言结构，才回落到 audio/。
            src_dirs = [root / "audios" / f"{lang}-audio" for root in asset_roots
                        if (root / "audios" / f"{lang}-audio").is_dir()]
            if not src_dirs:
                src_dirs = [root / "audios" / "audio" for root in asset_roots
                            if (root / "audios" / "audio").is_dir()]
            if not src_dirs:
                continue
            lang_voices_dir = voices_dir / lang
            lang_voices_dir.mkdir(parents=True, exist_ok=True)
            for sid in chapter_subtitle_ids:
                src = _first_usable_file(src_dirs, f"{sid}.wav")
                if src is not None:
                    self._copy_asset(src, lang_voices_dir / f"{sid}.wav")
                    audio_count += 1

        # 字幕 .txt 仍随 assets 上传一份到 OSS（备份用，运行期实际读内联 window._i18n）
        # 按语言分目录 assets/subtitles/{lang}/{sid}.txt，与音频 voices/{lang} 对齐
        # 上游源目录 audios/{lang}-subtitle/（旧结构 audios/subtitle 时只有默认语言）
        subtitles_dir = assets_dir / "subtitles"
        subtitle_count = 0
        chapter_sub_ids = {Path(n).stem for n in chapter_subtitle_files}
        for lang in self.languages:
            src_dirs = [root / "audios" / f"{lang}-subtitle" for root in asset_roots
                        if (root / "audios" / f"{lang}-subtitle").is_dir()]
            if not src_dirs:
                src_dirs = [root / "audios" / "subtitle" for root in asset_roots
                            if (root / "audios" / "subtitle").is_dir()]
            if not src_dirs:
                continue
            lang_sub_dir = subtitles_dir / lang
            lang_sub_dir.mkdir(parents=True, exist_ok=True)
            for sid in chapter_sub_ids:
                src = _first_usable_file(src_dirs, f"{sid}.txt")
                if src is not None:
                    self._copy_asset(src, lang_sub_dir / f"{sid}.txt")
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
                self._copy_asset(scene['path'], scenes_dir / scene['name'])
                scene_count += 1

        # 需要的角色立绘：来自 character_position 解析出的 png 名
        needed_characters = set(chapter_data.get('needed_char_pngs', set()))
        character_count = 0
        copied_characters = []
        for char in self.character_images:
            if char['name'] in needed_characters:
                self._copy_asset(char['path'], characters_dir / char['name'])
                character_count += 1
                copied_characters.append(char)

        # 复制本章需要的背景音乐
        # 逻辑：只要音乐范围 [music.start, music.end] 与本章范围 [chap_start, chap_end] 有重叠，就复制
        music_count = 0
        for music in self.music_files:
            if max(music['start'], chap_start) <= min(music['end'], chap_end):
                self._copy_asset(music['path'], music_dir / music['name'])
                music_count += 1

        # 复制本章需要的环境底音（规则同 BGM：范围有重叠就复制）
        ambience_count = 0
        copied_ambience = []
        for amb in self.ambience_files:
            if max(amb['start'], chap_start) <= min(amb['end'], chap_end):
                self._copy_asset(amb['path'], ambience_dir / amb['name'])
                ambience_count += 1
                copied_ambience.append(amb)

        # 复制本章需要的SFX（索引在本章范围内的）
        sfx_count = 0
        copied_sfx = []
        for sfx in self.sfx_files:
            if chap_start <= sfx['index'] <= chap_end:
                self._copy_asset(sfx['path'], sfx_dir / sfx['name'])
                sfx_count += 1
                copied_sfx.append(sfx)

        # 复制自定义章节背景图片（替换beijing.png）
        bg_count = 0
        if node_id:
            bg_source = self.images_base_dir / "images" / "backgrounds" / f"{node_id}.png"
            if bg_source.exists():
                images_dir = assets_dir / "images"
                images_dir.mkdir(exist_ok=True)
                self._copy_asset(bg_source, images_dir / "beijing.png")
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

        # story_tree 的 children.choice_text 位于语言层；脚本只保存占位符，
        # 由 I18nScript 在 init 前按 URL ?lang= 解析，避免把英语/日语混入同一份 Choice。
        _choice_default_map = (self.story_choice_text_by_lang or {}).get(
            self.default_language, {}
        ) or (self.story_choice_text_by_lang or {}).get('en-US', {}) or {}
        def _localized_choice_text(child_id, fallback):
            key = f"{self._source_node_id or (node_data or {}).get('id', '')}::{child_id}"
            if key in _choice_default_map:
                return f"␟choice:{key}␟"
            return str(fallback or '')

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
        # Skip needs to distinguish an ordinary beat from a choice pivot.  A
        # pivot may have a normal Material_N label followed by Choice_N.
        _choice_labels_by_index = {}
        for _choice_idx, _choice_material in choice_by_index.items():
            _has_pivot = bool(audio_by_index.get(_choice_idx) or subtitle_by_index.get(_choice_idx))
            _choice_labels_by_index[str(_choice_idx)] = f"Choice_{_choice_idx}" if _has_pivot else f"Material_{_choice_idx}"
        lines.append(f"window._choiceLabelsByIndex = {json.dumps(_choice_labels_by_index, ensure_ascii=False)};")
        lines.append(f"window._maxMaterialIndex = {json.dumps(max(all_indices) if all_indices else 0)};")
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

        # 运行期只暴露规范 locale，防止旧数据中的中文/短码语言层泄漏到 UI。
        runtime_languages = [lang for lang in ONLINE_GAME_LANGUAGES if lang in self.languages]
        if not runtime_languages:
            runtime_languages = [DEFAULT_AUDIO_LANGUAGE]
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
        # locale 解析与资源是否存在解耦：只要 URL 是精确的受支持值，
        # 就保留该值；缺少对应资源时由字幕/音频/UI 各自回退英语。
        # 不能因为 _allLangs 只有 ja-JP 就把合法的 en-US 改成日语。
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
        lines.append("window.__chapterTitleForLang = function(lang){")
        lines.append("    var wanted = window._resolveGameLang ? window._resolveGameLang(lang) : String(lang || '');")
        lines.append("    var map = window._chapterTitlesByLang || {}; var fallback = map[window._defaultLang] || map['en-US'] || window._chapterTitle || ''; ")
        lines.append("    return map[wanted] || fallback;")
        lines.append("};")
        lines.append("window._chapterTitle = window.__chapterTitleForLang(window._lang);")
        lines.append(f"window._voicesByLang = {voices_js};")
        lines.append(f"window._i18n = {i18n_js};")
        lines.append(f"window._namesByLang = {names_js};")
        lines.append("// 当前语言的音频清单（Start 时注册到 Monogatari）")
        lines.append("window._allVoices = window._voicesByLang[window._lang] || window._voicesByLang[window._defaultLang] || {};")
        # Voice assets are intentionally registered lazily (the loading screen
        # must not download every line).  Continue used to jump straight to a
        # Material label without running Start, so `_allVoices` was never
        # registered and every resumed beat silently lost its voice.  Keep a
        # small idempotent helper available to all entry points (Start,
        # Continue, seek/skip and chapter navigation) instead of relying on a
        # one-shot mutable `_allVoices` value.
        lines.append("window.__registerCurrentVoices = function(){")
        lines.append("    try {")
        lines.append("        if (typeof monogatari === 'undefined' || !monogatari) return false;")
        lines.append("        var lang = String(window._lang || window._defaultLang || 'en-US');")
        lines.append("        var maps = window._voicesByLang || {};")
        # JS 的空对象也是真值；不能让一个空的 ja-JP/en-US map 阻止回退，
        # 更不能在回退到另一语言的 map 后仍把 AssetsPath 指向原语言目录。
        lines.append("        var hasVoices = function(value){ return !!value && typeof value === 'object' && Object.keys(value).length > 0; };")
        lines.append("        var selectedLang = lang;")
        lines.append("        var voices = maps[selectedLang];")
        lines.append("        if (!hasVoices(voices)) { selectedLang = String(window._defaultLang || 'en-US'); voices = maps[selectedLang]; }")
        lines.append("        if (!hasVoices(voices)) { selectedLang = 'en-US'; voices = maps[selectedLang]; }")
        lines.append("        if (!voices || typeof voices !== 'object') voices = {};")
        lines.append("        var path = monogatari.setting('AssetsPath') || {};")
        lines.append("        path.voices = 'voices/' + selectedLang;")
        lines.append("        monogatari.setting('AssetsPath', path);")
        lines.append("        monogatari.assets('voices', voices);")
        lines.append("        window._allVoices = voices;")
        lines.append("        window._voicesRegisteredLang = selectedLang;")
        lines.append("        return true;")
        lines.append("    } catch (e) { console.warn('[Voices] registration failed:', e); return false; }")
        lines.append("};")
        lines.append("// 运行期按当前语言取字幕文本（NVL/CG 等 Function 内用）")
        lines.append("window.__i18nText = function(key){")
        lines.append("    var d = window._i18n[window._lang] || {}; var fb = window._i18n[window._defaultLang] || {};")
        lines.append("    return (d[key] != null) ? d[key] : (fb[key] || '');")
        lines.append("};")
        ui_defaults = {
            'ja-JP': {
                'start': '開始', 'restart': '最初から', 'cont': '続ける', 'makeChoice': '選択してください',
                'optionA': '選択肢 A', 'optionB': '選択肢 B', 'quit': 'クイット', 'system': 'システム',
                'stats': 'ステータス', 'settings': '設定', 'save': 'セーブ', 'load': '読み込み', 'history': '履歴',
                'auto': 'オート', 'skip': 'スキップ', 'back': '戻る', 'close': '閉じる', 'confirm': '確認',
                'cancel': 'キャンセル', 'delete': '削除', 'fullscreen': '全画面', 'clickContinue': 'クリックして続ける',
                'quickMenu': 'クイックメニュー', 'show': '表示', 'hide': '非表示', 'restoreDefaults': '初期設定に戻す',
                'savePage': 'セーブ', 'saveName': 'セーブ名', 'currentProgress': '現在の進行状況', 'createSave': '新規セーブ',
                'manualSave': '手動セーブ', 'trialLocked': '体験版では第2章までプレイできます。', 'loading': '読み込み中...',
                'accessDenied': 'この章にはアクセスできません。', 'saveSynced': 'セーブデータを同期しました。',
                'autoPlayInterval': 'オートプレイ間隔', 'skipWarningTitle': 'スキップの確認',
                'skipWarningBody': 'この操作はクリック50回分を一度に進めます。重要なストーリーを見逃す可能性があります。続行しますか？',
                'skipToEnd': '終幕または分岐点に到達しました。',
                'rapidClick': '連打チャレンジ', 'burst': '連打！', 'perfectStrike': 'タイミングチャレンジ', 'lock': '止める',
                'success': '成功', 'failed': '失敗', 'memoryChallenge': '記憶チャレンジ', 'watchSequence': '順番を覚えてください...',
                'clickInOrder': '順番どおりにクリック', 'chapterComplete': '章完了', 'systemTitle': 'システム'
            },
            'en-US': {
                'start': 'Start', 'restart': 'Restart', 'cont': 'Continue', 'makeChoice': 'Please make your choice',
                'optionA': 'Option A', 'optionB': 'Option B', 'quit': 'Quit', 'system': 'System', 'stats': 'Stats',
                'settings': 'Settings', 'save': 'Save', 'load': 'Load', 'history': 'History', 'auto': 'Auto', 'skip': 'Skip',
                'back': 'Back', 'close': 'Close', 'confirm': 'Confirm', 'cancel': 'Cancel', 'delete': 'Delete',
                'fullscreen': 'Fullscreen', 'clickContinue': 'click to continue', 'quickMenu': 'Quick Menu', 'show': 'Show',
                'hide': 'Hide', 'restoreDefaults': 'Restore Defaults', 'savePage': 'Save', 'saveName': 'Save name',
                'currentProgress': 'Current story progress', 'createSave': 'Create save', 'manualSave': 'Manual save',
                'trialLocked': 'The demo includes Chapters 1 and 2 only.', 'loading': 'Loading...',
                'accessDenied': 'You do not have access to this chapter.', 'saveSynced': 'Save data synced.',
                'autoPlayInterval': 'Auto-play interval', 'skipWarningTitle': 'Confirm skip',
                'skipWarningBody': 'This skips ahead by the equivalent of 50 clicks at once. You may miss important story content. Continue?',
                'skipToEnd': 'Reached a branch point or the ending.',
                'rapidClick': 'Rapid Click Challenge', 'burst': 'Burst!', 'perfectStrike': 'Timing Challenge', 'lock': 'Lock',
                'success': 'Success', 'failed': 'Failed', 'memoryChallenge': 'Memory Challenge', 'watchSequence': 'Watch the sequence...',
                'clickInOrder': 'Click in order', 'chapterComplete': 'Chapter Complete', 'systemTitle': 'System'
            }
        }
        for _lang, _custom in (self.story_ui_by_lang or {}).items():
            if isinstance(_custom, dict):
                ui_defaults.setdefault(str(_lang), {}).update(_custom)
        # Canonical online Japanese labels.  story_tree may contain an older
        # custom UI dictionary (or the engine may later re-localize its own
        # labels), so these two user-facing labels must remain stable in every
        # generated chapter and on both the title and in-game menus.
        ui_defaults.setdefault('ja-JP', {})['quit'] = 'クイット'
        ui_defaults['ja-JP']['load'] = '読み込み'
        ui_text_js = json.dumps(ui_defaults, ensure_ascii=False)
        lines.append("// Localized custom UI text; story_tree values are merged, with the canonical online Japanese Quit/Load labels enforced above.")
        lines.append(f"window._uiText = {ui_text_js};")
        lines.append("window.__ui = function(key){")
        lines.append("    var m = window._uiText[window._lang] || window._uiText[window._defaultLang] || window._uiText['en-US'] || {};")
        lines.append("    return m[key] != null ? m[key] : key;")
        lines.append("};")
        lines.append("window.__setUiButtonLabel = function(button, text){")
        lines.append("    if (!button) return;")
        lines.append("    var labels = Array.from(button.querySelectorAll('[data-string], [data-content=\"label\"], .button__label, .label, .custom-ui-label'));")
        lines.append("    var label = labels.length ? labels[0] : null;")
        lines.append("    if (label) {")
        lines.append("        label.removeAttribute('data-string');")
        lines.append("        label.classList.add('custom-ui-label');")
        lines.append("        labels.slice(1).forEach(function(extra){ if (extra !== label) extra.remove(); });")
        lines.append("        var textNodes = []; var cleanupWalker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT); var cleanupNode;")
        lines.append("        while ((cleanupNode = cleanupWalker.nextNode())) textNodes.push(cleanupNode);")
        lines.append("        textNodes.forEach(function(node){ if (!label.contains(node) && String(node.nodeValue || '').trim()) node.nodeValue = ''; });")
        lines.append("        if (label.textContent !== text) label.textContent = text;")
        lines.append("    } else {")
        lines.append("        var walker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT);")
        lines.append("        var node = null, candidate = null;")
        lines.append("        while ((node = walker.nextNode())) { if (String(node.nodeValue || '').trim()) candidate = node; }")
        lines.append("        if (candidate) {")
        lines.append("            var host = candidate.parentElement;")
        lines.append("            if (host && host !== button) { host.classList.add('custom-ui-label'); if (candidate.nodeValue !== text) candidate.nodeValue = text; }")
        lines.append("            else { label = document.createElement('span'); label.className = 'custom-ui-label'; label.textContent = text; candidate.replaceWith(label); }")
        lines.append("        } else { label = document.createElement('span'); label.className = 'custom-ui-label'; label.textContent = text; button.appendChild(label); }")
        lines.append("    }")
        lines.append("    button.removeAttribute('data-string');")
        lines.append("    if (button.getAttribute('aria-label') !== text) button.setAttribute('aria-label', text);")
        lines.append("};")
        lines.append("window.__refreshEngineButtonLabels = function(){")
        lines.append("    var keys = { start:'start', save:'save', load:'load', settings:'settings', log:'history', history:'history', 'auto-play':'auto', autoplay:'auto', skip:'skip', back:'back', quit:'quit', end:'quit', fullscreen:'fullscreen', close:'close', confirm:'confirm', cancel:'cancel', delete:'delete' };")
        lines.append("    document.querySelectorAll('button').forEach(function(button){")
        lines.append("        var action = String(button.getAttribute('data-action') || '').toLowerCase();")
        lines.append("        if (action === 'open-screen') action = String(button.getAttribute('data-open') || button.getAttribute('data-screen') || '').toLowerCase();")
        lines.append("        if (action === 'save' && button.closest('save-screen, [data-component=\"save-screen\"]')) return;")
        lines.append("        var key = keys[action]; if (!key) return;")
        lines.append("        var text = window.__ui(key);")
        lines.append("        var hasText = !!button.querySelector('[data-string], [data-content=\"label\"], .button__label, .label, .custom-ui-label') || Array.from(button.childNodes).some(function(node){ return node.nodeType === 3 && String(node.nodeValue || '').trim(); });")
        lines.append("        if (hasText) window.__setUiButtonLabel(button, text);")
        lines.append("        else { if (button.getAttribute('aria-label') !== text) button.setAttribute('aria-label', text); if (button.title !== text) button.title = text; }")
        lines.append("    });")
        lines.append("};")
        lines.append("window.refreshGameUiLanguage = function(){")
        lines.append("    document.documentElement.lang = window._lang || window._defaultLang || 'en-US';")
        lines.append("    if (window.__chapterTitleForLang) window._chapterTitle = window.__chapterTitleForLang(window._lang);")
        lines.append("    var engineLanguage = {'ja-JP':'日本語','en-US':'English'}[window._lang] || 'English';")
        lines.append("    try {")
        lines.append("        if (window._lang === 'ja-JP' && typeof monogatari.translation === 'function') monogatari.translation('日本語', { Quit:'クイット', QuitButton:'クイット', Load:'読み込み', LoadButton:'読み込み' });")
        lines.append("        monogatari.preference('Language', engineLanguage);")
        lines.append("        try { var _path = monogatari.setting('AssetsPath') || {}; _path.voices = 'voices/' + (window._lang || window._defaultLang || ''); monogatari.setting('AssetsPath', _path); } catch (e) {}");
        lines.append("        if (typeof monogatari.localize === 'function') monogatari.localize();")
        lines.append("    } catch (e) { console.warn('[i18n] refresh engine UI err', e); }")
        lines.append("    // 语言切换后同步更新 Monogatari 的 voice 资源表；否则下一句可能仍沿用旧语言/空映射。")
        lines.append("    if (window.__registerCurrentVoices) window.__registerCurrentVoices();")
        lines.append("    window.__setUiButtonLabel(document.querySelector('.restart-button'), window.__ui('restart'));")
        lines.append("    window.__setUiButtonLabel(document.querySelector('.continue-button'), window.__ui('cont'));")
        lines.append("    window.__setUiButtonLabel(document.querySelector('.main-quit-button'), window.__ui('quit'));")
        lines.append("    window.__refreshEngineButtonLabels();")
        lines.append("    var system = document.getElementById('system-toggle');")
        lines.append("    if (system) { system.textContent = window.__ui('system'); system.title = window.__ui('system'); }")
        lines.append("    var stats = document.getElementById('stats-toggle');")
        lines.append("    if (stats) { stats.title = window.__ui('stats'); stats.setAttribute('aria-label', window.__ui('stats')); }")
        lines.append("    var systemClose = document.querySelector('#system-panel-overlay .sys-close');")
        lines.append("    if (systemClose) { systemClose.title = window.__ui('close'); systemClose.setAttribute('aria-label', window.__ui('close')); }")
        lines.append("    var systemTitle = document.querySelector('#system-panel-overlay .sys-title'); if (systemTitle) systemTitle.textContent = window.__ui('systemTitle');")
        lines.append("    if (window.GameplayUiState && window.GameplayUiState.syncSaveScreen) window.GameplayUiState.syncSaveScreen();")
        lines.append("    var languageSelector = document.querySelector('[data-action=\"set-language\"]'); if (languageSelector) languageSelector.value = engineLanguage;")
        lines.append("    if (window.AncientUI && window.AncientUI.refreshSettingsText) window.AncientUI.refreshSettingsText();")
        lines.append("    if (window.OtherUI && window.OtherUI.refreshSettingsText) window.OtherUI.refreshSettingsText();")
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
        # ``monogatari`` is declared as a top-level lexical binding by
        # options.js, so it is intentionally not a ``window`` property.
        # Checking window.monogatari made the i18n preprocessor and the
        # autoplay installer silently no-op in the browser.
        lines.append("            var script = (typeof monogatari !== 'undefined' && monogatari && monogatari._script) ? monogatari._script : null;")
        lines.append("            if (!script) return;")
        lines.append("            var resolve = function(s){")
        lines.append("                if (typeof s !== 'string') return s;")
        lines.append("                return s.replace(/\\u241f(.+?)\\u241f/g, function(_, k){")
        lines.append("                    if (k.indexOf('ui:') === 0 && window.__ui) return window.__ui(k.slice(3));")
        lines.append("                    if (k.indexOf('choice:') === 0) {")
        lines.append("                        var choiceKey = k.slice(7); var cm = (window._choiceTextByLang && window._choiceTextByLang[window._lang]) || {}; var cfb = (window._choiceTextByLang && window._choiceTextByLang[window._defaultLang]) || {}; return cm[choiceKey] != null ? cm[choiceKey] : (cfb[choiceKey] != null ? cfb[choiceKey] : '');")
        lines.append("                    }")
        lines.append("                    return (dict[k]!=null)?dict[k]:(fb[k]||'');")
        lines.append("                });")
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
        lines.append("    _retryTimer: null,")
        lines.append("    _generation: 0,")
        lines.append("    _advancing: false,")
        lines.append("    _paused: false,")
        lines.append("    _retryCount: 0,")
        lines.append("    _waitingAudio: null,")
        lines.append("    _audioWatchdog: null,")
        lines.append("    _audioHandlers: [],")
        lines.append("    _typingTimer: null,")
        lines.append("    _waitingTyping: false,")
        lines.append("    _contentReady: false,")
        lines.append("    _contentToken: '',")
        lines.append("    _pendingContentToken: '',")
        lines.append("    _pendingContentReason: '',")
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
        lines.append("        // Auto-play speed is an interval in seconds (0–3s), not a voice playback rate.")
        lines.append("        return window.AutoPlayInterval ? window.AutoPlayInterval.delay() : 1000;")
        lines.append("    },")
        lines.append("    isEnabled: function(){ return this._enabled; },")
        lines.append("    currentVoiceAudio: function(){")
        lines.append("        var voiceId = window.AutoVoiceAdvance ? window.AutoVoiceAdvance._voiceId : ''; if (!voiceId) return null;")
        lines.append("        var audio = null; try { audio = monogatari.mediaPlayer('voice', voiceId); } catch(e) {}")
        lines.append("        if (!audio || audio.ended || audio.error) return null;")
        lines.append("        // A ready-but-paused player is a failed/aborted voice startup, not an active voice.")
        lines.append("        if (audio.paused && Number(audio.currentTime || 0) <= 0 && Number(audio.readyState || 0) >= 2) return null;")
        lines.append("        return audio;")
        lines.append("    },")
        lines.append("    clearTimer: function(){")
        lines.append("        if (this._timer != null) clearTimeout(this._timer);")
        lines.append("        this._timer = null;")
        lines.append("        try { monogatari.global('_auto_play_timer', this._enabled ? true : null); } catch(e) {}")
        lines.append("    },")
        lines.append("    clearRetry: function(){")
        lines.append("        if (this._retryTimer != null) clearTimeout(this._retryTimer);")
        lines.append("        this._retryTimer = null;")
        lines.append("    },")
        lines.append("    clearAudioWatchdog: function(){")
        lines.append("        if (this._audioWatchdog != null) clearTimeout(this._audioWatchdog);")
        lines.append("        this._audioWatchdog = null;")
        lines.append("    },")
        lines.append("    clearTypingWatch: function(){")
        lines.append("        if (this._typingTimer != null) clearTimeout(this._typingTimer);")
        lines.append("        this._typingTimer = null; this._waitingTyping = false;")
        lines.append("    },")
        lines.append("    clearAudioWatch: function(){")
        lines.append("        var item = this._waitingAudio;")
        lines.append("        (this._audioHandlers || []).forEach(function(h){ try { h.audio.removeEventListener(h.type, h.fn); } catch(e) {} });")
        lines.append("        this._audioHandlers = []; this._waitingAudio = null;")
        lines.append("        this.clearAudioWatchdog();")
        lines.append("    },")
        lines.append("    updateUI: function(){")
        lines.append("        var button = document.querySelector('quick-menu [data-action=\"auto-play\"]'); if (!button) return;")
        lines.append("        var label = button.querySelector('[data-string]');")
        lines.append("        if (label) { try { label.textContent = monogatari.string(this._enabled ? 'Stop' : 'AutoPlay'); } catch(e) {} }")
        lines.append("        var icon = button.querySelector('svg, [data-icon], .fa-play-circle, .fa-stop-circle');")
        lines.append("        if (icon) { var span = document.createElement('span'); span.className = this._enabled ? 'fas fa-stop-circle' : 'fas fa-play-circle'; icon.replaceWith(span); }")
        lines.append("    },")
        lines.append("    finishAdvance: function(generation){")
        lines.append("        if (generation !== this._generation) return;")
        lines.append("        this._advancing = false;")
        lines.append("        if (this._enabled) this.schedule();")
        lines.append("    },")
        lines.append("    contentReady: function(token, reason){")
        lines.append("        token = String(token || '');")
        lines.append("        if (!token || !window.AutoVoiceAdvance || window.AutoVoiceAdvance._token !== token) return;")
        lines.append("        if (!this._enabled || this._paused) { this._pendingContentToken = token; this._pendingContentReason = reason || 'content-complete'; return; }")
        lines.append("        if (this._contentReady && this._contentToken === token) return;")
        lines.append("        this._pendingContentToken = ''; this._pendingContentReason = '';")
        lines.append("        this.clearTypingWatch(); this.clearAudioWatch(); this._contentReady = true; this._contentToken = token;")
        lines.append("        console.info('[SmartAutoPlay] content ready:', token, reason || '');")
        lines.append("        this.schedule();")
        lines.append("    },")
        lines.append("    typingComplete: function(token){")
        lines.append("        token = String(token || '');")
        lines.append("        if (!token || !window.AutoVoiceAdvance || window.AutoVoiceAdvance._token !== token) return;")
        lines.append("        this.contentReady(token, 'typing-complete');")
        lines.append("    },")
        lines.append("    waitForTyping: function(token){")
        lines.append("        token = String(token || ''); if (!token || !this._enabled || this._paused) return;")
        lines.append("        this.clearAudioWatch(); this.clearTypingWatch(); this._contentReady = false; this._contentToken = token; this._waitingTyping = true;")
        lines.append("        var self = this; var check = function(){")
        lines.append("            self._typingTimer = null;")
        lines.append("            if (!self._enabled || self._paused || !window.AutoVoiceAdvance || window.AutoVoiceAdvance._token !== token) return;")
        lines.append("            var state = window.AutoVoiceAdvance; var started = !!state._dialogStarted;")
        lines.append("            var done = false; var custom = false;")
        lines.append("            try {")
        lines.append("                if (window.NvlOverlay && window.NvlOverlay.isOpen && window.NvlOverlay.isOpen()) { started = true; custom = true; done = !window.NvlOverlay.isTyping(); }")
        lines.append("                if (window.HookManager && window.HookManager.active && window.HookManager._textReady) { started = true; custom = true; done = !window.HookManager.isTyping || !window.HookManager.isTyping(); }")
        lines.append("                if (!custom) {")
        lines.append("                    done = !!monogatari.global('finished_typing');")
        lines.append("                    // didFinishTyping can fire while Auto is paused or before it is enabled.")
        lines.append("                    // When that happens, use the rendered dialog as a deterministic fallback:")
        lines.append("                    // once the full expected text is visible, the typewriter is complete.")
        lines.append("                    var expectedText = ''; var renderedText = ''; try { expectedText = state.normalizeText(state.readingText()); renderedText = state.normalizeText(state.visibleText()); } catch(e) {}")
        lines.append("                    if (expectedText && renderedText && (renderedText === expectedText || renderedText.indexOf(expectedText) >= 0)) { started = true; done = true; }")
        lines.append("                }")
        lines.append("            } catch(e) {}")
        lines.append("            if (!state._hasText) { var step = -1; try { step = Number(monogatari.state('step')); } catch(e) {} if (started || (state._initialStep >= 0 && step > state._initialStep)) { self.contentReady(token, 'non-dialogue-complete'); return; } self._typingTimer = setTimeout(check, 50); return; }")
        lines.append("            if (started && done) { self.contentReady(token, custom ? 'custom-typing-complete' : 'typing-complete'); return; }")
        lines.append("            self._typingTimer = setTimeout(check, started ? 35 : 50);")
        lines.append("        }; self._typingTimer = setTimeout(check, 35);")
        lines.append("    },")
        lines.append("    waitForAudio: function(audio, token){")
        lines.append("        token = String(token || (window.AutoVoiceAdvance && window.AutoVoiceAdvance._token) || '');")
        lines.append("        if (!audio || !token || !this._enabled || this._paused) return;")
        lines.append("        this.clearTypingWatch(); this.clearAudioWatch(); this._contentReady = false; this._contentToken = token; this._waitingAudio = audio;")
        lines.append("        var self = this; var finish = function(kind){")
        lines.append("            if (!self._waitingAudio || self._waitingAudio !== audio) return;")
        lines.append("            if (!window.AutoVoiceAdvance || window.AutoVoiceAdvance._token !== token) return;")
        lines.append("            if (kind !== 'ended') { self.clearAudioWatch(); if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.fallbackToTyping(token); return; }")
        # Audio can finish before the custom NVL/intro typewriter.  Treat the
        # voice as consumed, then wait for rendered text too; otherwise the
        # next beat can hide an unfinished NVL line and leave stale listeners.
        lines.append("            self.clearAudioWatch(); if (window.AutoVoiceAdvance) { window.AutoVoiceAdvance._voiceId = ''; window.AutoVoiceAdvance._audioConsumed = true; if (window.AutoVoiceAdvance._hasText) { self.waitForTyping(token); return; } }")
        lines.append("            self.contentReady(token, 'audio-ended');")
        lines.append("        };")
        lines.append("        ['ended','error','abort','emptied'].forEach(function(type){ var fn = function(){ finish(type); }; audio.addEventListener(type, fn); self._audioHandlers.push({audio:audio,type:type,fn:fn}); });")
        lines.append("        self._audioWatchdog = setTimeout(function(){ if (self._waitingAudio === audio && !audio.ended && audio.paused && Number(audio.currentTime || 0) < 0.05 && Number(audio.readyState || 0) >= 2) finish('stalled'); }, 3500);")
        lines.append("        if (audio.ended) finish('ended'); else if (audio.error) finish('error');")
        lines.append("    },")
        lines.append("    beginContentWait: function(){")
        lines.append("        if (!this._enabled || this._paused || !window.AutoVoiceAdvance) return;")
        lines.append("        var state = window.AutoVoiceAdvance; if (!state._token) return;")
        lines.append("        if (this._contentReady && this._contentToken === state._token) { this.schedule(); return; }")
        lines.append("        if (state._voiceId) { state.probeVoice(state._token); return; }")
        lines.append("        this.waitForTyping(state._token);")
        lines.append("    },")
        lines.append("    retry: function(delay, generation){")
        lines.append("        if (!this._enabled || this._paused || generation !== this._generation) return;")
        lines.append("        this.clearRetry(); var self = this; this._retryCount = Math.min(12, (this._retryCount || 0) + 1);")
        lines.append("        var wait = Math.max(40, Number(delay) || 100); if (this._retryCount > 3) wait = Math.min(600, wait + (this._retryCount - 3) * 80);")
        lines.append("        this._retryTimer = setTimeout(function(){ self._retryTimer = null; if (generation === self._generation) self.proceed(); }, wait);")
        lines.append("    },")
        lines.append("    isInteractionPoint: function(reason){")
        lines.append("        var label = ''; try { label = String(monogatari.state('label') || ''); } catch(e) {}")
        lines.append("        if (label === 'EndChapter' || label === 'EndBridge' || /^Choice_/.test(label) || /^Branch[A-Z]_/.test(label)) return true;")
        lines.append("        if (document.querySelector('choice-container, [data-component=\"choice-container\"] [data-choice]')) return true;")
        lines.append("        if (document.querySelector('[data-screen=\"main\"].open, [data-screen=\"main\"][open]')) return true;")
        lines.append("        return /choice|branch|ending|end chapter|game over|interaction required/i.test(String(reason || ''));")
        lines.append("    },")
        lines.append("    proceed: function(){")
        lines.append("        if (!this._enabled || this._paused || this._advancing) return;")
        lines.append("        var generation = this._generation; var self = this;")
        lines.append("        if (typeof monogatari === 'undefined' || !monogatari) return;")
        lines.append("        if (!this._contentReady) { this.beginContentWait(); return; }")
        lines.append("        if (monogatari.global('block') || monogatari.global('_engine_block')) { this.retry(100, generation); return; }")
        lines.append("        this.clearTimer(); this.clearRetry(); this._advancing = true;")
        # Use the exact same proceed flags as the engine's game-screen click
        # handler.  This keeps autoplay in the normal user-navigation path:
        # an active typewriter is completed first, then the next click advances
        # the story.  It also avoids the voice-only shouldProceed gate that
        # could leave autoplay permanently waiting on a stale audio element.
        lines.append("        Promise.resolve(monogatari.proceed({userInitiated:true, skip:false, autoPlay:false}))")
        lines.append("            .then(function(){ self._retryCount = 0; self.finishAdvance(generation); })")
        lines.append("            .catch(function(error){")
        lines.append("                if (generation !== self._generation) return;")
        lines.append("                self._advancing = false;")
        lines.append("                if (!self._enabled || self._paused) return;")
        lines.append("                var reason = String(error || '');")
        lines.append("                var interaction = self.isInteractionPoint(reason);")
        lines.append("                if (interaction) { console.info('[SmartAutoPlay] waiting for interaction:', reason); self.set(false); return; }")
        lines.append("                // TypeWriter completion, engine races and action transitions are recoverable.")
        lines.append("                // Keep autoplay alive and retry instead of turning it off after one transient reject.")
        lines.append("                if (/TypeWriter/i.test(reason)) { self._contentReady = true; self._contentToken = (window.AutoVoiceAdvance && window.AutoVoiceAdvance._token) || ''; self.schedule(); return; }")
        lines.append("                self.retry(90, generation);")
        lines.append("            });")
        lines.append("    },")
        lines.append("    schedule: function(){")
        lines.append("        if (!this._enabled || this._paused) return;")
        lines.append("        if (!this._contentReady) { this.beginContentWait(); return; }")
        lines.append("        if (this._timer != null) clearTimeout(this._timer);")
        lines.append("        this.clearRetry(); var self = this; var generation = this._generation;")
        lines.append("        this._timer = setTimeout(function(){ self._timer = null; if (generation === self._generation) self.proceed(); }, this.delay());")
        lines.append("        try { monogatari.global('_auto_play_timer', this._timer); } catch(e) {}")
        lines.append("    },")
        lines.append("    set: function(enabled){")
        # Disabling Auto/Back/Skip must always remove ended/error listeners.
        # The old enabled-only guard left stale voice callbacks behind when
        # the user stopped autoplay, which could advance a later beat.
        lines.append("        this._generation += 1; this._enabled = !!enabled; this._advancing = false; this._paused = false; this._contentReady = false; this._contentToken = ''; this._retryCount = 0; this.clearTimer(); this.clearRetry(); this.clearTypingWatch(); this.clearAudioWatch();")
        lines.append("        if (this._enabled) { if (this._pendingContentToken && window.AutoVoiceAdvance && this._pendingContentToken === window.AutoVoiceAdvance._token) { var pendingToken = this._pendingContentToken; var pendingReason = this._pendingContentReason; this._pendingContentToken = ''; this._pendingContentReason = ''; this.contentReady(pendingToken, pendingReason); } else this.schedule(); }")
        lines.append("        this.updateUI();")
        lines.append("    },")
        lines.append("    pauseForScreen: function(){")
        lines.append("        if (!this._enabled) return;")
        lines.append("        this._paused = true; this.clearTimer();")
        lines.append("    },")
        lines.append("    resumeFromScreen: function(){")
        lines.append("        if (!this._enabled || !this._paused) return;")
        lines.append("        this._paused = false; if (this._pendingContentToken && window.AutoVoiceAdvance && this._pendingContentToken === window.AutoVoiceAdvance._token) { var pendingToken = this._pendingContentToken; var pendingReason = this._pendingContentReason; this._pendingContentToken = ''; this._pendingContentReason = ''; this.contentReady(pendingToken, pendingReason); } else this.schedule();")
        lines.append("    },")
        lines.append("    advanceFromVoice: function(){")
        lines.append("        if (!this._enabled) return;")
        lines.append("        this.proceed();")
        lines.append("    },")
        # A user click advances the story through the engine's own delegated
        # game-screen listener.  Any autoplay timer already scheduled for the
        # beat we just left would fire on top of that click and advance a
        # second time (voice/typing callbacks in flight can add a third), so
        # one click appeared to skip several subtitles.  Bump the generation to
        # invalidate every in-flight callback, drop the pending timers, then
        # re-arm from the new beat.  Autoplay keeps running, just re-based.
        lines.append("    restartAfterUserAdvance: function(){")
        lines.append("        if (!this._enabled || this._paused) return;")
        lines.append("        this._generation += 1;")
        lines.append("        this._advancing = false; this._retryCount = 0;")
        lines.append("        this._contentReady = false; this._contentToken = '';")
        lines.append("        this._pendingContentToken = ''; this._pendingContentReason = '';")
        lines.append("        this.clearTimer(); this.clearRetry(); this.clearTypingWatch(); this.clearAudioWatch();")
        # Always re-arm here; never rely on the next beat's dialogStarted() to do
        # it.  dialogStarted() de-dupes on step (`if (step === this._dialogStep)
        # return`), so when the beat we land on already ran its Dialog action
        # nothing would fire again and autoplay stalled permanently.
        # beginContentWait() waits for voice/typing when a beat token is live and
        # falls back to a plain schedule() otherwise, so it is safe in both cases.
        lines.append("        var token = window.AutoVoiceAdvance && window.AutoVoiceAdvance._token;")
        lines.append("        if (token) this.beginContentWait(); else this.schedule();")
        lines.append("    },")
        lines.append("    cancelWaitingAudio: function(audio){")
        lines.append("        if (audio && this._waitingAudio !== audio) return;")
        lines.append("        this.clearAudioWatch(); this.clearTypingWatch(); this._contentReady = false;")
        lines.append("        if (this._enabled && !this._paused && window.AutoVoiceAdvance && window.AutoVoiceAdvance._token) this.beginContentWait();")
        lines.append("    },")
        lines.append("    voiceFailed: function(token, voiceId){")
        lines.append("        if (!this._enabled || (window.AutoVoiceAdvance && window.AutoVoiceAdvance._token !== token)) return;")
        lines.append("        try { monogatari.removeMediaPlayer('voice', voiceId); } catch(e) {}")
        lines.append("        if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.fallbackToTyping(token); else this.schedule();")
        lines.append("    },")
        lines.append("    bindLifecycle: function(){")
        lines.append("        if (this._lifecycleBound) return;")
        lines.append("        this._lifecycleBound = true; var self = this;")
        lines.append("        var root = document.querySelector('visual-novel');")
        lines.append("        if (root && !root._smartAutoPlayEventsBound) {")
        lines.append("            root._smartAutoPlayEventsBound = true;")
        lines.append("            root.addEventListener('didRunAction', function(event){ var action = event && event.detail && event.detail.action; var id = action && action.constructor && action.constructor.id || action && action.id || ''; if (id === 'Dialog' && window.AutoVoiceAdvance) window.AutoVoiceAdvance.dialogStarted(); });")
        lines.append("        }")
        lines.append("        document.addEventListener('click', function(event){")
        lines.append("            var target = event.target && event.target.closest ? event.target : null; if (!target) return;")
        lines.append("            var autoButton = target.closest('[data-action=\"auto-play\"]');")
        lines.append("            if (autoButton) { event.preventDefault(); event.stopImmediatePropagation(); event.stopPropagation(); self.set(!self.isEnabled()); return; }")
        lines.append("            var opener = target.closest('[data-action=\"open-screen\"], [data-action=\"dialog-log\"], [data-action=\"end\"]');")
        lines.append("            if (opener) self.pauseForScreen();")
        lines.append("            var back = target.closest('[data-action=\"back\"]');")
        lines.append("            if (back && back.closest('[data-screen]:not([data-screen=\"game\"])')) setTimeout(function(){ self.resumeFromScreen(); }, 0);")
        lines.append("            if (target.closest('[data-action=\"dismiss-alert\"]')) setTimeout(function(){ self.resumeFromScreen(); }, 0);")
        lines.append("            if (target.closest('[data-action=\"quit\"]')) self.set(false);")
        # Detect the plain "click the screen to continue" gesture and re-base the
        # autoplay clock so the pending timer cannot stack on top of it.
        # Excluded: anything with a data-action, the UI containers listed in
        # GameplayInteractionGuard.selectors, choices, and #nvl-overlay (NVL runs
        # its own two-stage gate through NvlOverlay.requestAdvance).
        lines.append("            if (!self._enabled || self._paused) return;")
        lines.append("            if (target.closest('[data-action]')) return;")
        lines.append("            if (target.closest('#nvl-overlay')) return;")
        lines.append("            if (target.closest('[data-choice], choice-container, [data-component=\"choice-container\"]')) return;")
        lines.append("            var uiSelectors = (window.GameplayInteractionGuard && window.GameplayInteractionGuard.selectors) ? window.GameplayInteractionGuard.selectors.join(',') : '';")
        lines.append("            if (uiSelectors && target.closest(uiSelectors)) return;")
        # Only the game screen advances on a bare click.
        lines.append("            var onGameScreen = target.closest('[data-screen=\"game\"], game-screen');")
        lines.append("            if (!onGameScreen) return;")
        lines.append("            if (target.closest('[data-screen]:not([data-screen=\"game\"])')) return;")
        # Defer past this click's own proceed() so we re-arm from the new beat.
        lines.append("            setTimeout(function(){ self.restartAfterUserAdvance(); }, 0);")
        lines.append("        }, true);")
        lines.append("        document.addEventListener('keydown', function(event){")
        lines.append("            var tag = event.target && event.target.tagName ? event.target.tagName.toLowerCase() : '';")
        lines.append("            if (tag === 'input' || tag === 'textarea' || tag === 'select' || (event.target && event.target.isContentEditable)) return;")
        lines.append("            var key = String(event.key || '').toLowerCase();")
        lines.append("            if (key === 'escape' || (event.shiftKey && (key === 's' || key === 'l' || key === 'q'))) self.pauseForScreen();")
        # Space/Enter reach the engine's own ["right","space"] shortcut and advance
        # the story without ever touching the click path above, so the pending
        # autoplay timer used to stack on top of the keypress and skip a beat.
        # Re-base the clock here too.  NVL is excluded: its overlay handler
        # swallows the key and calls restartAfterUserAdvance() itself.
        lines.append("            if (event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;")
        lines.append("            if (key !== ' ' && key !== 'spacebar' && key !== 'enter') return;")
        lines.append("            if (!self._enabled || self._paused) return;")
        lines.append("            if (window.NvlOverlay && window.NvlOverlay.isOpen && window.NvlOverlay.isOpen()) return;")
        lines.append("            var node = event.target;")
        # Only choices genuinely consume the key. Do NOT bail on [data-action]:
        # after clicking Back the focus stays on that button, so event.target is
        # the Back button for the NEXT keypress and the clock would never re-base.
        lines.append("            if (node && node.closest && node.closest('[data-choice], choice-container, [data-component=\"choice-container\"]')) return;")
        lines.append("            if (node && node.closest && node.closest('[data-screen]:not([data-screen=\"game\"])')) return;")
        lines.append("            setTimeout(function(){ self.restartAfterUserAdvance(); }, 0);")
        lines.append("        }, true);")
        lines.append("    },")
        lines.append("    install: function(){")
        lines.append("        if (this._installed || typeof monogatari === 'undefined' || !monogatari) return;")
        lines.append("        var self = this;")
        lines.append("        try { if (!window.monogatari) window.monogatari = monogatari; } catch(e) {}")
        lines.append("        monogatari.autoPlay = function(enabled){ self.set(!!enabled); };")
        lines.append("        try { monogatari.global('_auto_play_timer', null); } catch(e) {}")
        lines.append("        this.bindLifecycle();")
        lines.append("        this._installed = true;")
        lines.append("    }")
        lines.append("};")
        lines.append("")
        lines.append("// ==================== 自动播放间隔（0–3 秒；不改变语音速度） ====================")
        lines.append("window.AutoPlayInterval = {")
        lines.append("    _seconds: 1,")
        lines.append("    _storageKey: 'VisualNovel_AutoPlayIntervalSeconds',")
        lines.append("    _observer: null,")
        lines.append("    _delegatedBound: false,")
        lines.append("    load: function(){")
        lines.append("        // Number(null) === 0.  Treat missing storage/preferences as missing,")
        lines.append("        // otherwise a first-time player is silently initialized to 0s.")
        lines.append("        var readNumber = function(getter){ try { var raw = getter(); if (raw == null || raw === '') return NaN; return Number(raw); } catch(e) { return NaN; } };")
        lines.append("        var value = readNumber(function(){ return localStorage.getItem(window.AutoPlayInterval._storageKey); });")
        lines.append("        if (!isFinite(value) || value < 0 || value > 3) value = readNumber(function(){ return monogatari.preference('AutoPlayInterval'); });")
        lines.append("        if (!isFinite(value) || value < 0 || value > 3) value = readNumber(function(){ return monogatari.preference('AutoPlaySpeed'); });")
        lines.append("        if (!isFinite(value) || value < 0 || value > 3) value = 1;")
        lines.append("        return Math.max(0, Math.min(3, Math.round(value * 10) / 10));")
        lines.append("    },")
        lines.append("    delay: function(){")
        lines.append("        // Read the in-memory value on every schedule so a slider change takes effect immediately.")
        lines.append("        var value = Number(this._seconds); if (!isFinite(value)) value = this.load();")
        lines.append("        value = Math.max(0, Math.min(3, Math.round(value * 10) / 10)); this._seconds = value;")
        lines.append("        return value * 1000;")
        lines.append("    },")
        lines.append("    set: function(value){")
        lines.append("        value = Number(value); if (!isFinite(value)) value = 1;")
        lines.append("        this._seconds = Math.max(0, Math.min(3, Math.round(value * 10) / 10));")
        lines.append("        try { localStorage.setItem(this._storageKey, String(this._seconds)); } catch(e) {}")
        lines.append("        try { monogatari.preference('AutoPlayInterval', this._seconds); } catch(e) {}")
        lines.append("        // Keep the engine preference in sync for compatibility, but never use it for voice playback.")
        lines.append("        try { monogatari.preference('AutoPlaySpeed', this._seconds); } catch(e) {}")
        lines.append("        if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) window.SmartAutoPlay.schedule();")
        lines.append("        this.updateUI();")
        lines.append("    },")
        lines.append("    mountUI: function(){")
        lines.append("        var root = document.querySelector('settings-screen [data-settings=\"auto-play-speed\"]');")
        lines.append("        if (!root) return false;")
        lines.append("        var oldInput = root.querySelector('[data-action=\"set-auto-play-speed\"]'); if (!oldInput) return;")
        lines.append("        if (oldInput.dataset && oldInput.dataset.autoPlayIntervalBound === '1') { this.updateUI(); return; }")
        lines.append("        var input = oldInput.cloneNode(true); oldInput.replaceWith(input);")
        lines.append("        if (input.dataset) input.dataset.autoPlayIntervalBound = '1';")
        lines.append("        input.min = '0'; input.max = '3'; input.step = '0.1'; input.value = String(this._seconds);")
        lines.append("        var self = this; input.addEventListener('input', function(){ self.set(this.value); }); input.addEventListener('change', function(){ self.set(this.value); });")
        lines.append("        this.updateUI();")
        lines.append("        return true;")
        lines.append("    },")
        lines.append("    updateUI: function(){")
        lines.append("        var input = document.querySelector('settings-screen [data-action=\"set-auto-play-speed\"]');")
        lines.append("        if (input) { input.min = '0'; input.max = '3'; input.step = '0.1'; var value = String(this._seconds); if (input.value !== value) input.value = value; }")
        lines.append("        var root = document.querySelector('settings-screen [data-settings=\"auto-play-speed\"]');")
        lines.append("        var heading = root && root.querySelector('h3');")
        lines.append("        if (heading) { heading.removeAttribute('data-string'); var title = window.__ui ? window.__ui('autoPlayInterval') : 'Auto-play interval'; if (heading.textContent !== title) heading.textContent = title; }")
        lines.append("    },")
        lines.append("    bindDelegated: function(){")
        lines.append("        if (this._delegatedBound) return; this._delegatedBound = true; var self = this;")
        lines.append("        document.addEventListener('input', function(event){ var input = event.target && event.target.closest ? event.target.closest('[data-action=\"set-auto-play-speed\"]') : null; if (input) self.set(input.value); }, true);")
        lines.append("        document.addEventListener('change', function(event){ var input = event.target && event.target.closest ? event.target.closest('[data-action=\"set-auto-play-speed\"]') : null; if (input) self.set(input.value); }, true);")
        lines.append("    },")
        lines.append("    init: function(){")
        lines.append("        this._seconds = this.load(); this.bindDelegated(); this.mountUI(); this.set(this._seconds);")
        lines.append("        if (!this._observer && typeof MutationObserver !== 'undefined') { var self = this; this._observer = new MutationObserver(function(){ if (self.mountUI()) self.updateUI(); }); var root = document.body || document.documentElement; if (root) this._observer.observe(root, {childList:true, subtree:true}); }")
        lines.append("    }")
        lines.append("};")
        lines.append("// Compatibility shim: all generated voice players remain at normal speed.")
        lines.append("window.VoicePlaybackRate = { _rate: 1, apply: function(audio){ if (!audio) return; try { audio.defaultPlaybackRate = 1; audio.playbackRate = 1; audio.preservesPitch = true; audio.webkitPreservesPitch = true; } catch(e) {} }, applyActive: function(){}, init: function(){} };")
        lines.append("")
        lines.append("// ==================== 自动播放：语音结束后立即前进 ====================")
        lines.append("// 有语音时等待 ended；无语音时等待当前字幕/自定义打字机完成，再使用设置中的额外间隔。")
        lines.append("window.AutoVoiceAdvance = {")
        lines.append("    _token: '',")
        lines.append("    _voiceId: '',")
        lines.append("    _textKey: '',")
        lines.append("    _fallbackText: '',")
        lines.append("    _advancing: false,")
        lines.append("    _hasText: false,")
        lines.append("    _dialogStarted: false,")
        lines.append("    _dialogStep: -1,")
        lines.append("    _audioConsumed: false,")
        lines.append("    _initialTextObject: null,")
        lines.append("    _initialVisibleText: '',")
        lines.append("    _initialStep: -1,")
        lines.append("    _probeTimer: null,")
        lines.append("    _probeAttempts: 0,")
        lines.append("    _readyToken: '',")
        lines.append("    _eventsBound: false,")
        lines.append("    clearProbe: function(){ if (this._probeTimer != null) clearTimeout(this._probeTimer); this._probeTimer = null; this._probeAttempts = 0; },")
        lines.append("    reset: function(){ this.clearProbe(); if (window.SmartAutoPlay) { window.SmartAutoPlay.clearAudioWatch(); window.SmartAutoPlay.clearTypingWatch(); } this._token = ''; this._voiceId = ''; this._textKey = ''; this._fallbackText = ''; this._hasText = false; this._dialogStarted = false; this._dialogStep = -1; this._audioConsumed = false; this._initialTextObject = null; this._initialVisibleText = ''; this._initialStep = -1; this._readyToken = ''; this._advancing = false; },")
        lines.append("    visibleText: function(){")
        lines.append("        var el = document.querySelector('#nvl-overlay.open .nvl-text, text-box [data-content=\"dialog\"], [data-component=\"text-box\"] [data-content=\"dialog\"], [data-ui=\"say\"]');")
        lines.append("        return el ? String(el.textContent || '').replace(/\\s+/g, ' ').trim() : '';" )
        lines.append("    },")
        lines.append("    normalizeText: function(text){ return String(text || '').replace(/<[^>]*>/g, ' ').replace(/\\s+/g, ' ').trim(); },")
        lines.append("    bindEvents: function(){")
        lines.append("        if (this._eventsBound) return; var root = document.querySelector('visual-novel'); if (!root) return; this._eventsBound = true; var self = this;")
        lines.append("        root.addEventListener('didRunAction', function(event){ var action = event && event.detail && event.detail.action; var id = action && action.constructor && action.constructor.id || action && action.id || ''; if (id === 'Dialog' && self._token) self.dialogStarted(); });")
        lines.append("        root.addEventListener('didFinishTyping', function(){ var label = ''; try { label = String(monogatari.state('label') || ''); } catch(e) {} if (self._token && (!label || label === self._token) && !self._voiceId && self.isEnabled() && !(window.NvlOverlay && window.NvlOverlay.isOpen && window.NvlOverlay.isOpen()) && !(window.HookManager && window.HookManager.active && window.HookManager._typing)) { self._dialogStarted = true; if (window.SmartAutoPlay) window.SmartAutoPlay.typingComplete(self._token); } });")
        lines.append("    },")
        lines.append("    enter: function(token, voiceId, textKey, fallbackText){")
        lines.append("        this.reset(); this.bindEvents(); this._token = String(token || '');")
        lines.append("        this._voiceId = String(voiceId || '');")
        lines.append("        this._textKey = String(textKey || '');")
        lines.append("        this._fallbackText = String(fallbackText || '');")
        lines.append("        this._hasText = !!this.normalizeText(this.readingText());")
        lines.append("        this._dialogStarted = false;")
        lines.append("        this._dialogStep = -1; this._audioConsumed = false;")
        lines.append("        try { this._initialTextObject = monogatari.global('textObject'); this._initialStep = Number(monogatari.state('step')); } catch(e) {}")
        lines.append("        this._initialVisibleText = this.visibleText();")
        lines.append("        if (window.SmartAutoPlay && window.SmartAutoPlay.isEnabled()) { this.ensureWatching(); var smart = window.SmartAutoPlay; var expected = this._token; setTimeout(function(){ if (window.AutoVoiceAdvance._token === expected && smart.isEnabled()) smart.beginContentWait(); }, 0); }")
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
        lines.append("    isReady: function(token){ return !!token && String(token) === String(this._readyToken || ''); },")
        lines.append("    ensureWatching: function(){")
        lines.append("        if (!this._token || !this.isEnabled() || (window.SmartAutoPlay && window.SmartAutoPlay._paused)) return;")
        lines.append("        this.bindEvents(); if (window.SmartAutoPlay && window.SmartAutoPlay._contentReady && window.SmartAutoPlay._contentToken === this._token) return;")
        lines.append("        if (this._voiceId) this.probeVoice(this._token); else this.waitForTyping(this._token);")
        lines.append("    },")
        lines.append("    complete: function(token, reason){")
        lines.append("        token = String(token || ''); if (!token || token !== this._token || !this.isEnabled()) return;")
        lines.append("        this.clearProbe(); this._readyToken = token; this._dialogStarted = true; if (this._voiceId) this._audioConsumed = true;")
        lines.append("        if (window.SmartAutoPlay) { window.SmartAutoPlay.clearTypingWatch(); window.SmartAutoPlay.clearAudioWatch(); window.SmartAutoPlay.contentReady(token, reason || 'content-complete'); }")
        lines.append("    },")
        lines.append("    fallbackToTyping: function(token){")
        lines.append("        token = String(token || ''); if (!token || token !== this._token) return; this._voiceId = ''; this._readyToken = ''; this._hasText = !!this.normalizeText(this.readingText());")
        lines.append("        if (window.SmartAutoPlay && this.isEnabled()) this.waitForTyping(token);")
        lines.append("    },")
        lines.append("    cancel: function(token){")
        lines.append("        token = String(token || '');")
        lines.append("        if (!token || this._token === token) {")
        lines.append("            this.reset();")
        lines.append("        }")
        lines.append("    },")
        lines.append("    isEnabled: function(){")
        lines.append("        if (window.SmartAutoPlay) return window.SmartAutoPlay.isEnabled();")
        lines.append("        try { return !!monogatari.global('_auto_play_timer'); } catch(e) { return false; }")
        lines.append("    },")
        lines.append("    watch: function(token, voiceId){")
        lines.append("        token = String(token || ''); voiceId = String(voiceId || '');")
        lines.append("        if (!voiceId || this._token !== token) return;")
        lines.append("        this._voiceId = voiceId; this.probeVoice(token);")
        lines.append("    },")
        lines.append("    dialogStarted: function(){")
        lines.append("        if (!this._token || !this.isEnabled()) return;")
        lines.append("        if (window.NvlOverlay && window.NvlOverlay.isOpen && window.NvlOverlay.isOpen()) return;")
        lines.append("        if (window.HookManager && window.HookManager.active && window.HookManager._typing) return;")
        lines.append("        var step = -1; try { step = Number(monogatari.state('step')); } catch(e) {}")
        lines.append("        if (step === this._dialogStep) return;")
        lines.append("        this._dialogStep = step; this._dialogStarted = true; this._readyToken = '';")
        lines.append("        if (!this._voiceId && window.SmartAutoPlay) window.SmartAutoPlay.waitForTyping(this._token);")
        lines.append("    },")
        lines.append("    probeVoice: function(token){")
        lines.append("        token = String(token || ''); if (!token || token !== this._token || !this.isEnabled()) return;")
        lines.append("        this.clearProbe(); var self = this; var attempts = 0; var attach = function(){")
        lines.append("            self._probeTimer = null; if (self._token !== token || !self.isEnabled()) return;")
        lines.append("            var audio = null; try { audio = monogatari.mediaPlayer('voice', self._voiceId); } catch(e) {}")
        lines.append("            if (!audio) { if (++attempts < 400) { self._probeTimer = setTimeout(attach, 25); return; } self.fallbackToTyping(token); return; }")
        lines.append("            if (audio.error) { self.fallbackToTyping(token); return; }")
        lines.append("            if (audio.ended) { self.complete(token, 'audio-already-ended'); return; }")
        lines.append("            if (window.SmartAutoPlay) window.SmartAutoPlay.waitForAudio(audio, token);")
        lines.append("        }; attach();")
        lines.append("    },")
        lines.append("    waitForTyping: function(token){ if (window.SmartAutoPlay) window.SmartAutoPlay.waitForTyping(String(token || this._token)); },")
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
        lines.append("    _change: function(fx, legacyFlags, sign){ window._playerVariables=window._playerVariables||{}; window._playerVariableChoices=window._playerVariableChoices||{}; window._playerLegacyFlags=window._playerLegacyFlags||{}; var schema=window._playerVariableSchema||{}; var enforceSchema=Object.keys(schema).length>0; Object.entries(fx||{}).forEach(function(e){ var key=e[0]; if(enforceSchema&&!Object.prototype.hasOwnProperty.call(schema,key)) return; var amount=Number(e[1]); if(!Number.isFinite(amount)||amount===0) return; var delta=sign*amount; window._playerVariables[key]=Math.max(0, Math.min(100, Number(window._playerVariables[key]||0)+delta)); var stat=window._playerVariableChoices[key]||(window._playerVariableChoices[key]={positive:0,effective:0}); if(sign>0){stat.effective+=1;if(amount>0)stat.positive+=1;} else {stat.effective=Math.max(0,stat.effective-1);if(amount>0)stat.positive=Math.max(0,stat.positive-1);} }); Object.entries(legacyFlags||{}).forEach(function(e){ var key=e[0], delta=sign*Math.max(0,Number(e[1]||0)); if(!Number.isFinite(delta)) return; window._playerLegacyFlags[key]=Math.max(0,Number(window._playerLegacyFlags[key]||0)+delta); }); if(window.updateStatsUI)window.updateStatsUI(); if(window._syncPlayerVars)window._syncPlayerVars(); },")
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
        # Do not mutate branch values/history until the destination jump has
        # actually succeeded.  A rejected jump used to leave the score and
        # branch snapshot changed while the player remained on the old beat.
        lines.append("        var applyAfterJump = function(){");
        lines.append("            if (state.mode === 'branch') {");
        lines.append("                if (state.isFirst) {");
        lines.append("                    if (window.BranchEffectLedger) window.BranchEffectLedger.reverse(state.effectKey, state.effects || {}, state.legacyFlags || {});");
        lines.append("                    if (window.BranchProgress) window.BranchProgress.clear();");
        lines.append("                    window.BranchNavigator._state = null;");
        lines.append("                } else if (window.BranchProgress) {");
        lines.append("                    window.BranchProgress.set(state.pivot, state.branch, state.previousSeq, target);");
        lines.append("                }");
        lines.append("            } else if (state.mode === 'choice') {");
        lines.append("                if (window.BranchProgress) window.BranchProgress.clear();");
        lines.append("                document.querySelectorAll('[data-component=\"choice-container\"], choice-container').forEach(function(node){ try { node.remove(); } catch(e) {} });");
        lines.append("                window.BranchNavigator.pruneBranchHistory(String(state.label || state.choiceLabel || ''));");
        lines.append("                window.BranchNavigator._state = null;");
        lines.append("            }");
        lines.append("        };");
        lines.append("        if (window.BackVoicePlayer) window.BackVoicePlayer.seek(target, state.previousVoice || '');")
        lines.append("        try { if(monogatari.action) monogatari.action('Dialog').reset(); } catch(e) {}")
        lines.append("        try { if(monogatari.global) { monogatari.global('block', false); monogatari.global('_engine_block', false); monogatari.global('_executing_sub_action', false); } } catch(e) {}")
        lines.append("        var self = this;")
        lines.append("        setTimeout(function(){")
        lines.append("            try { var result = (window.GameNavigation ? window.GameNavigation.jump(target) : self.go(target)); if(result && result.then) result.then(function(success){ if(success === false) throw new Error('Branch back target was not reached'); applyAfterJump(); self._busy=false; var m=String(target).match(/(?:Material|Choice|Branch[A-B])_(\\d+)/); var backIndex=m?Number(m[1]):NaN; if(Number.isFinite(backIndex)&&window.GameProgressReporter){ window.GameProgressReporter.update(backIndex); window.GameProgressReporter.sendReport('back'); } }, function(e){ self._busy=false; console.error('[BranchNavigator] 分支回退失败:',e); }); }")
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
        _branch_storage_key_js = "window._scopedStorageKey('BranchProgress')"
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
        lines.append("// ==================== 串行游戏内导航 ====================")
        lines.append("// Seek / Skip / 分支回退共用同一条导航通道，避免 jump 与旧 click/proceed 并发覆盖引擎状态。")
        lines.append("window.GameNavigation = {")
        lines.append("    _busy: false,")
        lines.append("    _token: 0,")
        lines.append("    resetLocks: function(){")
        lines.append("        try { monogatari.global('_engine_block', false); monogatari.global('_executing_sub_action', false); monogatari.global('block', false); monogatari.global('auto_play', false); monogatari.global('skipping', false); } catch(e) {}")
        lines.append("    },")
        lines.append("    stopTransient: function(){")
        lines.append("        if (window.SmartAutoPlay) window.SmartAutoPlay.clearTimer();")
        lines.append("        if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.reset();")
        lines.append("        if (window.BackVoicePlayer) window.BackVoicePlayer.stop();")
        lines.append("        try { (monogatari.mediaPlayers('voice') || []).forEach(function(audio){ audio.pause(); audio.currentTime = 0; }); } catch(e) {}")
        lines.append("        try { if (window.NvlOverlay) window.NvlOverlay.hide(); if (window.CgOverlay) window.CgOverlay.setMode(false, ''); } catch(e) {}")
        lines.append("        try { document.querySelectorAll('choice-container, [data-component=\"choice-container\"]').forEach(function(node){ node.remove(); }); } catch(e) {}")
        lines.append("    },")
        lines.append("    jump: function(label){")
        lines.append("        label = String(label || ''); if (label === 'EndChapter') { try { if (monogatari.script('EndBridge')) label = 'EndBridge'; } catch(e) {} } if (!label || this._busy) return Promise.resolve(false);")
        lines.append("        var actions; try { actions = monogatari.script(label); } catch(e) { actions = null; }")
        lines.append("        if (!Array.isArray(actions) || !actions.length) return Promise.reject(new Error('Game label does not exist: ' + label));")
        lines.append("        // Seek/Skip/branch jumps can also bypass Start/ContinueStart; keep voice assets available for every entry path.")
        lines.append("        if (window.__registerCurrentVoices) window.__registerCurrentVoices();")
        lines.append("        this._busy = true; var token = ++this._token; this.stopTransient(); this.resetLocks();")
        lines.append("        try { if (monogatari.showScreen) monogatari.showScreen('game'); if (monogatari.action) monogatari.action('Dialog').reset(); } catch(e) {}")
        lines.append("        // Use the engine's real Jump action.  Running actions[0] directly leaves the")
        lines.append("        // engine's internal label/step and jump history out of sync; the next NVL")
        lines.append("        // click can then replay the previous label (the 206 -> 207 loop).")
        lines.append("        var self = this; var run; try { run = monogatari.run('jump ' + label, false); } catch(e) { this._busy = false; this.resetLocks(); return Promise.reject(e); }")
        lines.append("        // Monogatari's Jump.apply() starts the target action asynchronously and resolves")
        lines.append("        // its own run() immediately.  Do not release the navigation lock while the")
        lines.append("        // first target action is still blocked; doing so caused the NVL 206 -> 207 loop.")
        lines.append("        // Keep the engine's block flags intact once the target is ready; only the timeout")
        lines.append("        // fallback is allowed to clear them.")
        lines.append("        var expectedLabel = label === 'EndBridge' ? 'EndChapter' : label;")
        lines.append("        var waitForTarget = function(){ return new Promise(function(resolve){ var attempts=0; var stable=0; var last=''; var startedAt=Date.now(); var check=function(){ if (token !== self._token) { resolve(false); return; } var actual=''; var blocked=false; try { actual=String(monogatari.state('label') || ''); blocked=!!(monogatari.global('block') || monogatari.global('_engine_block') || monogatari.global('_executing_sub_action')); } catch(e) {} var ready = (actual === expectedLabel && Date.now() - startedAt >= 80); if (ready) { stable = last === actual ? stable + 1 : 1; } else { stable = 0; } last = actual; if (stable >= 3) { resolve(true); return; } if (attempts++ >= 180) { console.warn('[GameNavigation] target state wait timed out:', { expected: expectedLabel, actual: actual, blocked: blocked }); resolve(false); return; } if (window.requestAnimationFrame) requestAnimationFrame(check); else setTimeout(check, 16); }; check(); }); };")
        lines.append("        return Promise.resolve(run).then(function(result){ return waitForTarget().then(function(targetReady){ if (token === self._token) { if (!targetReady) self.resetLocks(); self._busy = false; } return result !== false && targetReady; }); }, function(err){ self._busy = false; self.resetLocks(); throw err; });")
        lines.append("    }")
        lines.append("};")
        lines.append("")
        navigation_guard = r'''
// ==================== Monogatari Navigation Race Guard ====================
// The bundled Monogatari runtime schedules next()/Jump work with setTimeout.
// A click, NVL overlay, autoplay, seek or Back can therefore leave an old
// callback pointing at an invalid label/step.  Keep one serialized next queue,
// invalidate stale callbacks on jumps/rollback, and never call run(undefined).
(function () {
    if (typeof monogatari === 'undefined' || !monogatari || window.__navigationRaceGuardInstalled) return;
    window.__navigationRaceGuardInstalled = true;
    var guard = window.NavigationRaceGuard = {
        epoch: 0,
        nextQueue: [],
        nextRunning: false,
        proceedPromise: null,
        previousPromise: null,
        rollbackPromise: null,
        invalidate: function (reason) {
            this.epoch += 1;
            var stale = this.nextQueue.splice(0);
            stale.forEach(function (item) {
                try { item.resolve({ advance: false, stale: true, reason: reason || 'invalidated' }); } catch (e) {}
            });
            // 引擎的 proceed 在调用 next() 之前就置了 _engine_block，丢弃排队请求
            // 时必须解锁，否则 shouldProceed 会永远 reject，点击彻底失效。
            this.unlock();
            return this.epoch;
        },
        isRaceError: function (error) {
            return /The action did not match any of the ones registered/i.test(String(error || ''));
        },
        isMediaError: function (error) {
            var name = (error && error.name) || '';
            return /^(NotSupportedError|NotAllowedError|AbortError)$/.test(name)
                || /no supported source|play\(\) request was interrupted/i.test(String(error || ''));
        },
        unlock: function () {
            try {
                monogatari.global('_engine_block', false);
                monogatari.global('_executing_sub_action', false);
            } catch (e) {}
        },
        enqueueNext: function () {
            var self = this;
            var labelName = '';
            var currentStep = -1;
            var targetStep = -1;
            var label = null;
            try {
                labelName = String(monogatari.state('label') || '');
                currentStep = Number(monogatari.state('step'));
                label = monogatari.label(labelName);
                // `drainNext` writes the active target step before running it.
                // Reserve additional queued requests after that step so two
                // overlapping callbacks cannot run the same statement twice.
                targetStep = currentStep + 1;
            } catch (e) {}
            if (!Array.isArray(label) || !Number.isFinite(targetStep) || targetStep < 0 || targetStep >= label.length || label[targetStep] == null) {
                console.warn('[NavigationGuard] ignored next with no registered target', { label: labelName, step: targetStep });
                this.unlock();
                return Promise.resolve({ advance: false, stale: true });
            }
            var duplicate = this.nextQueue.find(function (item) {
                return item.label === labelName && item.fromStep === currentStep;
            });
            if (duplicate && duplicate.promise) return duplicate.promise;
            var entry = {
                epoch: self.epoch,
                label: labelName,
                fromStep: currentStep,
                targetStep: targetStep,
                statement: label[targetStep],
                resolve: null,
                reject: null,
                promise: null
            };
            entry.promise = new Promise(function (resolve, reject) {
                entry.resolve = resolve;
                entry.reject = reject;
            });
            self.nextQueue.push(entry);
            self.drainNext();
            return entry.promise;
        },
        drainNext: function () {
            var self = this;
            if (this.nextRunning) return;
            var item = this.nextQueue.shift();
            if (!item) return;
            if (item.epoch !== this.epoch) {
                try { item.resolve({ advance: false, stale: true }); } catch (e) {}
                this.unlock();
                this.drainNext();
                return;
            }
            var currentLabel = '';
            try { currentLabel = String(monogatari.state('label') || ''); } catch (e) {}
            if (currentLabel !== item.label) {
                try { item.resolve({ advance: false, stale: true }); } catch (e) {}
                this.unlock();
                this.drainNext();
                return;
            }
            this.nextRunning = true;
            try { monogatari.state({ step: item.targetStep }); } catch (e) {}
            var timer = setTimeout(function () {
                if (item.epoch !== self.epoch) {
                    self.nextRunning = false;
                    self.unlock();
                    try { item.resolve({ advance: false, stale: true }); } catch (e) {}
                    self.drainNext();
                    return;
                }
                var result;
                try {
                    // Call the unwrapped run through the public wrapper so an
                    // invalid action is converted into a resolved stale result.
                    result = monogatari.run(item.statement);
                } catch (e) {
                    result = Promise.reject(e);
                }
                Promise.resolve(result).then(function (value) {
                    self.nextRunning = false;
                    self.unlock();
                    try { item.resolve(value); } catch (e) {}
                    self.drainNext();
                }, function (error) {
                    self.nextRunning = false;
                    self.unlock();
                    if (self.isRaceError(error)) {
                        console.warn('[NavigationGuard] swallowed stale next rejection');
                        try { item.resolve({ advance: false, stale: true }); } catch (e) {}
                    } else if (self.isMediaError(error)) {
                        // A missing voice/BGM file must not stop the story: the
                        // statement is treated as done so the following Function
                        // (which arms AutoVoiceAdvance) and the dialog still run.
                        console.warn('[NavigationGuard] media failed, continuing muted:', String(error));
                        try { item.resolve({ advance: true, mediaFailed: true }); } catch (e) {}
                    } else {
                        try { item.reject(error); } catch (e) {}
                    }
                    self.drainNext();
                });
            }, 0);
            // Keep a reference for diagnostics; invalidate() deliberately does
            // not cancel the active callback because its action may be the jump
            // which performs the invalidation itself.
            this.activeTimer = timer;
        },
        install: function () {
            if (this.installed) return;
            this.installed = true;
            var self = this;
            var originalRun = monogatari.run.bind(monogatari);
            var originalProceed = monogatari.proceed.bind(monogatari);
            var originalPrevious = monogatari.previous.bind(monogatari);
            var originalRollback = monogatari.rollback.bind(monogatari);
            monogatari.__navigationRaceGuard = this;

            monogatari.run = function (statement, shouldAdvance) {
                if (statement === undefined || statement === null) {
                    console.warn('[NavigationGuard] ignored stale empty action');
                    self.unlock();
                    return Promise.resolve({ advance: false, stale: true });
                }
                // 上游只产出一侧分支素材时，选项的 'jump BranchX_N_1' 可能指向一个
                // 从未生成的 label；引擎 Jump 找不到目标就静默停住，整局卡死。
                // 这里改跳到存在的兄弟分支，兄弟也没有就落到下一条主线 —— 宁可少
                // 一段分支剧情，也不能让玩家卡在选项上。
                if (typeof statement === 'string' && /^jump\s+\S+$/i.test(statement.trim())) {
                    var wanted = statement.trim().split(/\s+/)[1];
                    var exists = function (name) {
                        try {
                            var actions = monogatari.script(name);
                            return Array.isArray(actions) && actions.length > 0;
                        } catch (e) { return false; }
                    };
                    if (!exists(wanted)) {
                        var alt = null;
                        var m = /^Branch([A-Z])_(\d+)_(\d+)$/.exec(wanted);
                        if (m) {
                            var sibling = m[1] === 'A' ? 'B' : 'A';
                            var candidates = [
                                'Branch' + sibling + '_' + m[2] + '_' + m[3],
                                'Branch' + sibling + '_' + m[2] + '_1',
                                (window._choiceLabelsByIndex || {})[String(Number(m[2]) + 1)],
                                'Material_' + (Number(m[2]) + 1)
                            ];
                            for (var i = 0; i < candidates.length; i++) {
                                if (candidates[i] && exists(candidates[i])) { alt = candidates[i]; break; }
                            }
                        }
                        if (alt) {
                            console.warn('[NavigationGuard] jump 目标 ' + wanted + ' 不存在，兜底跳向 ' + alt);
                            statement = 'jump ' + alt;
                        } else {
                            console.error('[NavigationGuard] jump 目标 ' + wanted + ' 不存在且无可用兜底，忽略该跳转');
                            self.unlock();
                            return Promise.resolve({ advance: false, stale: true });
                        }
                    }
                }
                var result;
                try { result = originalRun(statement, shouldAdvance); } catch (e) { return Promise.reject(e); }
                return Promise.resolve(result).catch(function (error) {
                    if (self.isRaceError(error)) {
                        console.warn('[NavigationGuard] swallowed stale action mismatch');
                        self.unlock();
                        return { advance: false, stale: true };
                    }
                    return Promise.reject(error);
                });
            };
            // Replace the runtime next implementation rather than calling the
            // original one: the original increments shared state before its
            // delayed callback, which is exactly the race being guarded.
            monogatari.next = function () { return self.enqueueNext(); };

            var wrapSingleton = function (name, original, slot, invalidate) {
                monogatari[name] = function (options) {
                    if (self[slot]) return self[slot];
                    if (invalidate) self.invalidate(name);
                    var result;
                    try { result = original(options); } catch (e) { return Promise.reject(e); }
                    self[slot] = Promise.resolve(result).catch(function (error) {
                        if (self.isRaceError(error)) {
                            console.warn('[NavigationGuard] swallowed stale ' + name + ' rejection');
                            self.unlock();
                            return { advance: false, stale: true };
                        }
                        return Promise.reject(error);
                    }).then(function (value) {
                        self[slot] = null;
                        return value;
                    }, function (error) {
                        self[slot] = null;
                        throw error;
                    });
                    return self[slot];
                };
            };
            // Two simultaneous proceeds/backs must share one operation; a
            // second click cannot enqueue another next against the same state.
            wrapSingleton('proceed', originalProceed, 'proceedPromise', false);
            wrapSingleton('previous', originalPrevious, 'previousPromise', true);
            wrapSingleton('rollback', originalRollback, 'rollbackPromise', true);

            // Await the target action in Jump.apply(). The stock action starts
            // it and immediately resolves, allowing the old label's callbacks
            // to overlap the new label.
            try {
                var JumpAction = monogatari.action && monogatari.action('Jump');
                if (JumpAction && JumpAction.prototype && !JumpAction.prototype.__navigationAwaitPatch) {
                    JumpAction.prototype.apply = function () {
                        var engine = this.engine;
                        var destination = this.label;
                        if (engine.__navigationRaceGuard) engine.__navigationRaceGuard.invalidate('jump');
                        engine.history('jump').push({
                            source: { label: engine.state('label'), step: engine.state('step') },
                            destination: { label: destination, step: 0 }
                        });
                        engine.state({ step: 0, label: destination });
                        var textBox = engine.element().find('[data-component="text-box"]').get(0);
                        if (textBox && textBox.props && textBox.props.mode !== 'nvl') engine.action('Dialog').reset();
                        var target = engine.label()[engine.state('step')];
                        if (target === undefined || target === null) {
                            self.unlock();
                            return Promise.resolve({ advance: false, stale: true });
                        }
                        engine.history('label').push(destination);
                        return Promise.resolve(engine.run(target));
                    };
                    JumpAction.prototype.__navigationAwaitPatch = true;
                }
            } catch (e) { console.warn('[NavigationGuard] Jump patch unavailable:', e); }

            // Replace both the old afternoon-image implementation (which ran
            // actions[0] directly) and the newer variant with one safe entry.
            var nav = window.GameNavigation;
            if (nav) {
                nav.jump = function (label) {
                    label = String(label || '');
                    try { if (label === 'EndChapter' && monogatari.script('EndBridge')) label = 'EndBridge'; } catch (e) {}
                    if (!label || this._busy) return Promise.resolve(false);
                    var actions;
                    try { actions = monogatari.script(label); } catch (e) { actions = null; }
                    if (!Array.isArray(actions) || !actions.length) return Promise.reject(new Error('Game label does not exist: ' + label));
                    this._busy = true;
                    var navToken = (Number(this._token) || 0) + 1;
                    this._token = navToken;
                    try { if (this.stopTransient) this.stopTransient(); } catch (e) {}
                    try { if (this.resetLocks) this.resetLocks(); } catch (e) {}
                    try { if (window.__registerCurrentVoices) window.__registerCurrentVoices(); } catch (e) {}
                    try { if (monogatari.showScreen) monogatari.showScreen('game'); } catch (e) {}
                    var expected = label === 'EndBridge' ? 'EndChapter' : label;
                    var run;
                    try { run = monogatari.run('jump ' + label, false); } catch (e) {
                        this._busy = false;
                        try { if (this.resetLocks) this.resetLocks(); } catch (_) {}
                        return Promise.reject(e);
                    }
                    var selfNav = this;
                    var wait = new Promise(function (resolve) {
                        var tries = 0;
                        var check = function () {
                            if (selfNav._token !== navToken) return resolve(false);
                            var actual = '';
                            try { actual = String(monogatari.state('label') || ''); } catch (e) {}
                            if (actual === expected) return resolve(true);
                            if (++tries >= 120) return resolve(false);
                            if (window.requestAnimationFrame) requestAnimationFrame(check); else setTimeout(check, 16);
                        };
                        check();
                    });
                    return Promise.resolve(run).then(function (result) {
                        return wait.then(function (ready) {
                            if (selfNav._token === navToken) {
                                selfNav._busy = false;
                                if (!ready) try { if (selfNav.resetLocks) selfNav.resetLocks(); } catch (e) {}
                            }
                            return result !== false && ready;
                        });
                    }, function (error) {
                        if (selfNav._token === navToken) {
                            selfNav._busy = false;
                            try { if (selfNav.resetLocks) selfNav.resetLocks(); } catch (e) {}
                        }
                        throw error;
                    });
                };
            }
        }
    };
    guard.install();
})();
'''
        lines.extend(navigation_guard.rstrip('\n').splitlines())
        material_back_controller = r'''
// ==================== Deterministic in-game Back ====================
// The stock Monogatari rollback walks reversible actions through several
// independent setTimeout callbacks.  On a label boundary that queue can keep
// running after state has moved, producing an empty/unknown action (for
// example Material_164 -> Material_163).  Route the gameplay Back button
// through the same serialized jump channel used by Seek/Skip instead.
window.GameBackController = {
    _installed: false,
    _busy: false,
    _numberFromLabel: function (label) {
        var match = String(label || '').match(/^(?:Material|Choice|Branch[A-B])_(\d+)/);
        return match ? Number(match[1]) : NaN;
    },
    _current: function () {
        var label = '';
        try { label = String(monogatari.state('label') || ''); } catch (e) {}
        var index = Number(window._currentMaterialIndex);
        if (!Number.isFinite(index)) index = this._numberFromLabel(label);
        return { label: label, index: index };
    },
    _historyTarget: function (current) {
        var player = window.BackVoicePlayer;
        var stack = player && Array.isArray(player._stack) ? player._stack : [];
        if (stack.length < 2) return null;
        var position = -1;
        for (var i = stack.length - 1; i >= 0; i--) {
            var token = String(stack[i] && stack[i].token || '');
            if (token === current.label || (Number.isFinite(current.index) && this._numberFromLabel(token) === current.index)) {
                position = i;
                break;
            }
        }
        if (position <= 0) return null;
        var previous = stack[position - 1] || {};
        var label = String(previous.token || '');
        if (!label) return null;
        try { if (!Array.isArray(monogatari.script(label))) return null; } catch (e) { return null; }
        return { label: label, index: this._numberFromLabel(label), voiceId: String(previous.voiceId || '') };
    },
    _indexTarget: function (current) {
        var indices = (window._materialIndices || []).map(Number).filter(Number.isFinite).sort(function (a, b) { return a - b; });
        if (!indices.length || !Number.isFinite(current.index)) return null;
        var previous = NaN;
        for (var i = 0; i < indices.length; i++) {
            if (indices[i] < current.index) previous = indices[i];
            else break;
        }
        if (!Number.isFinite(previous)) return null;
        var label = (window._choiceLabelsByIndex && window._choiceLabelsByIndex[String(previous)]) || ('Material_' + previous);
        try { if (!Array.isArray(monogatari.script(label))) return null; } catch (e) { return null; }
        return { label: label, index: previous, voiceId: '' };
    },
    _syncButton: function (index) {
        var button = document.querySelector('[data-screen="game"] [data-action="back"], game-screen [data-action="back"]');
        if (!button) return;
        var start = Number(window._sessionStartIndex || 1);
        var disabled = !Number.isFinite(index) || index <= start;
        if (disabled) {
            button.setAttribute('disabled', 'true');
            button.style.opacity = '0.3';
            button.style.pointerEvents = 'auto';
            button.style.cursor = 'not-allowed';
        } else {
            button.removeAttribute('disabled');
            button.style.opacity = '1';
            button.style.pointerEvents = 'auto';
            button.style.cursor = 'pointer';
        }
    },
    back: function () {
        if (this._busy || (window.GameNavigation && window.GameNavigation._busy)) return true;
        var current = this._current();
        var start = Number(window._sessionStartIndex || 1);
        if (!Number.isFinite(current.index) || current.index <= start) {
            this._syncButton(current.index);
            return true;
        }
        var target = this._historyTarget(current) || this._indexTarget(current);
        if (!target || !target.label) return true;
        this._busy = true;
        // Clear pending autoplay work, but keep the user's Auto setting; the
        // destination beat will re-arm it after its voice/typing completes.
        try { if (window.SmartAutoPlay) window.SmartAutoPlay.clearTimer(); } catch (e) {}
        try { if (window.NavigationRaceGuard) window.NavigationRaceGuard.invalidate('game-back'); } catch (e) {}
        try { if (window.BackVoicePlayer) window.BackVoicePlayer.seek(target.label, target.voiceId || ''); } catch (e) {}
        try { if (monogatari.action) monogatari.action('Dialog').reset(); } catch (e) {}
        try {
            monogatari.global('block', false);
            monogatari.global('_engine_block', false);
            monogatari.global('_executing_sub_action', false);
        } catch (e) {}
        var self = this;
        var operation;
        try {
            operation = window.GameNavigation
                ? window.GameNavigation.jump(target.label)
                : monogatari.run('jump ' + target.label, false);
        } catch (error) {
            operation = Promise.reject(error);
        }
        Promise.resolve(operation).then(function (success) {
            if (success === false) throw new Error('Back target was not reached');
            self._busy = false;
            self._syncButton(target.index);
            if (target.index && window.GameProgressReporter) {
                window.GameProgressReporter.update(target.index);
                window.GameProgressReporter.sendReport('back');
            }
            console.info('[GameBack] jumped', current.label, '->', target.label);
        }, function (error) {
            self._busy = false;
            try { if (window.GameNavigation) window.GameNavigation.resetLocks(); } catch (e) {}
            console.error('[GameBack] jump failed', error);
        });
        return true;
    },
    handleRollback: function () {
        // Keyboard shortcut (Left) reaches monogatari.rollback() directly and
        // therefore never passes through the DOM capture listener below.
        // Keep it on the same deterministic path as a mouse/touch Back click.
        if (window.BranchNavigator && window.BranchNavigator._state) {
            try { return !!window.BranchNavigator.back(); } catch (e) { console.error('[GameBack] branch rollback failed', e); return true; }
        }
        return this.back();
    },
    install: function () {
        if (this._installed) return;
        this._installed = true;
        var self = this;
        document.addEventListener('click', function (event) {
            var node = event.target && event.target.closest ? event.target : null;
            var button = node && node.closest('[data-action="back"]');
            if (!button) return;
            // Settings/Save/Load/Help Back closes its own screen and is not a
            // gameplay rollback. BranchNavigator was registered first and
            // keeps ownership of active Choice/Branch labels.
            if (button.closest('[data-screen]:not([data-screen="game"])')) return;
            if (!button.closest('[data-screen="game"], game-screen, quick-menu')) return;
            if (window.BranchNavigator && window.BranchNavigator._state) return;
            event.preventDefault();
            event.stopPropagation();
            event.stopImmediatePropagation();
            self.back();
        }, true);
        // Route the built-in Left-key rollback as well.  Keep non-game screens
        // on Monogatari's own handler so Settings/Save/Load remain closable.
        try {
            if (typeof monogatari !== 'undefined' && monogatari && !monogatari.__gameBackRollbackRouted) {
                var originalRollback = monogatari.rollback.bind(monogatari);
                monogatari.rollback = function (options) {
                    var label = '';
                    try { label = String(monogatari.state('label') || ''); } catch (e) {}
                    var isGameplayLabel = /^(?:Material|Choice|Branch[A-B])_\d+/.test(label);
                    if (isGameplayLabel && window.GameBackController) {
                        window.GameBackController.handleRollback();
                        return Promise.resolve({ deterministic: true });
                    }
                    return originalRollback(options);
                };
                monogatari.__gameBackRollbackRouted = true;
            }
        } catch (e) { console.warn('[GameBack] keyboard rollback hook unavailable', e); }
    }
};
window.GameBackController.install();
'''
        lines.extend(material_back_controller.rstrip('\n').splitlines())
        lines.append("// ==================== 单次 50 beat Skip ====================")
        lines.append("window.SkipController = {")
        lines.append("    STEP: 50,")
        lines.append("    _installed: false,")
        lines.append("    _warning: null,")
        lines.append("    _resumeAuto: false,")
        lines.append("    _currentIndex: function(){")
        lines.append("        var value = Number(window._currentMaterialIndex);")
        lines.append("        if (isFinite(value)) return value;")
        lines.append("        var label = ''; try { label = String(monogatari.state('label') || ''); } catch(e) {}")
        lines.append("        var match = label.match(/(?:Material|Choice|Branch[A-B])_(\\d+)/);")
        lines.append("        return match ? Number(match[1]) : Number(window._materialIndices && window._materialIndices[0] || 0);")
        lines.append("    },")
        lines.append("    _stopTransientState: function(){")
        lines.append("        if (window.SmartAutoPlay) window.SmartAutoPlay.clearTimer();")
        lines.append("        if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.reset();")
        lines.append("        if (window.BackVoicePlayer) window.BackVoicePlayer.stop();")
        lines.append("        try { (monogatari.mediaPlayers('voice') || []).forEach(function(audio){ audio.pause(); audio.currentTime = 0; }); } catch(e) {}")
        lines.append("        try { if (window.NvlOverlay) window.NvlOverlay.hide(); } catch(e) {}")
        lines.append("        try { if (window.CgOverlay) window.CgOverlay.setMode(false, ''); } catch(e) {}")
        lines.append("        try { document.querySelectorAll('choice-container, [data-component=\"choice-container\"]').forEach(function(node){ node.remove(); }); } catch(e) {}")
        lines.append("    },")
        lines.append("    _target: function(){")
        lines.append("        var indices = (window._materialIndices || []).map(Number).filter(function(x){ return isFinite(x); }).sort(function(a,b){ return a-b; });")
        lines.append("        if (!indices.length) return {label:'EndBridge', index:0, boundary:true};")
        lines.append("        var current = this._currentIndex(); var desired = current + this.STEP;")
        lines.append("        var choices = window._choiceLabelsByIndex || {};")
        lines.append("        // A branch point inside the skip window wins over the full 50 beat jump.")
        lines.append("        for (var i=0; i<indices.length; i++) { var idx=indices[i]; if (idx>current && idx<=desired && choices[String(idx)]) return {label:choices[String(idx)], index:idx, boundary:true}; }")
        lines.append("        for (var j=0; j<indices.length; j++) { if (indices[j] >= desired) return {label: choices[String(indices[j])] || ('Material_' + indices[j]), index:indices[j], boundary:!!choices[String(indices[j])]}; }")
        lines.append("        return {label:'EndBridge', index:indices[indices.length-1], boundary:true};")
        lines.append("    },")
        lines.append("    _hideWarning: function(){")
        lines.append("        if (this._warning && this._warning.parentNode) this._warning.parentNode.removeChild(this._warning);")
        lines.append("        this._warning = null;")
        lines.append("    },")
        lines.append("    _showWarning: function(){")
        lines.append("        var self = this; this._hideWarning();")
        lines.append("        // Use the real alert-modal component renderer.  Writing innerHTML before the custom element connects is overwritten by Monogatari and produced the old TIPS/undefined dialog.")
        lines.append("        var modal = document.createElement('alert-modal'); modal.id='skip-warning-modal'; modal.className='modal modal--active skip-warning-modal'; modal.setAttribute('role','dialog'); modal.setAttribute('aria-modal','true'); document.body.appendChild(modal); this._warning=modal;")
        lines.append("        var titleText=window.__ui?window.__ui('skipWarningTitle'):'Confirm skip'; var bodyText=window.__ui?window.__ui('skipWarningBody'):'This skips ahead by the equivalent of 50 clicks at once. You may miss important story content. Continue?';")
        lines.append("        // 'Confirm' is a built-in translation key; replace its rendered text with the language-layer title/body after the component finishes rendering.")
        lines.append("        try { modal.setProps({message:'Confirm', context:bodyText, actions:[]}); } catch(e) {}")
        lines.append("        return new Promise(function(resolve){ setTimeout(function(){")
        lines.append("            var title = modal.querySelector('[data-content=\"message\"]'); if (title) { title.removeAttribute('data-string'); title.textContent=titleText; }")
        lines.append("            var body = modal.querySelector('[data-content=\"context\"]'); if (body) body.textContent=bodyText;")
        lines.append("            var actions = modal.querySelector('[data-content=\"actions\"]'); if (!actions) { var content=modal.querySelector('.modal__content'); actions=document.createElement('div'); actions.setAttribute('data-content','actions'); if(content) content.appendChild(actions); }")
        lines.append("            if (actions) actions.innerHTML='<button type=\"button\" class=\"skip-warning-confirm\"></button><button type=\"button\" class=\"skip-warning-cancel\"></button>'; var cancel = modal.querySelector('.skip-warning-cancel'); var confirm = modal.querySelector('.skip-warning-confirm');")
        lines.append("            if (cancel) cancel.textContent=window.__ui?window.__ui('cancel'):'Cancel'; if (confirm) confirm.textContent=window.__ui?window.__ui('confirm'):'Confirm';")

        lines.append("            var done=function(value){ self._hideWarning(); resolve(value); };")
        lines.append("            if (cancel) cancel.addEventListener('click',function(e){e.preventDefault();e.stopImmediatePropagation();e.stopPropagation();done(false);});")
        lines.append("            if (confirm) confirm.addEventListener('click',function(e){e.preventDefault();e.stopImmediatePropagation();e.stopPropagation();done(true);});")
        lines.append("            modal.addEventListener('click',function(e){if(e.target===modal) done(false);});")
        lines.append("            setTimeout(function(){ if (confirm && confirm.focus) confirm.focus(); }, 0);")
        lines.append("        }, 0); });")
        lines.append("    },")
        lines.append("    _jump: function(){")
        lines.append("        var target=this._target(); this._stopTransientState();")
        lines.append("        try { monogatari.global('auto_play', false); monogatari.global('skipping', false); } catch(e) {}")
        lines.append("        var self=this;")
        lines.append("        var run; try { run = window.GameNavigation ? window.GameNavigation.jump(target.label) : monogatari.run('jump '+target.label); } catch(e) { console.error('[Skip] jump failed',e); return; }")
        lines.append("        Promise.resolve(run).then(function(success){")
        lines.append("            if (success === false) throw new Error('Skip target was not reached');")
        lines.append("            if (target.index && window.GameProgressReporter) { window.GameProgressReporter.update(target.index); window.GameProgressReporter.sendReport('skip'); }")
        lines.append("            if ((target.label==='EndChapter' || target.label==='EndBridge') && window.showToast) window.showToast(window.__ui?window.__ui('skipToEnd'):'Reached a branch point or the ending.');")
        lines.append("            if (self._resumeAuto && target.label!=='EndChapter' && target.label!=='EndBridge' && !(window._choiceLabelsByIndex||{})[String(target.index)]) { if(window.SmartAutoPlay) window.SmartAutoPlay.set(true); }")
        lines.append("            self._resumeAuto=false;")
        # 跳转失败时也必须 resume：pauseForScreen 是在 _showWarning 前调的，
        # 只清 _resumeAuto 会让 _paused 永久为 true —— Auto 按钮仍显示开启但永不推进。
        lines.append("        }).catch(function(e){ self._resumeAuto=false; if(window.SmartAutoPlay) window.SmartAutoPlay.resumeFromScreen(); console.error('[Skip] proceed failed',e); });")
        lines.append("    },")
        lines.append("    request: function(){")
        lines.append("        if (this._warning) return;")
        lines.append("        var label=''; try { label=String(monogatari.state('label')||''); } catch(e) {}")
        lines.append("        if (!label || label==='EndChapter' || label==='EndBridge' || /^Choice_/.test(label) || /^Branch[A-B]_/.test(label)) { if(window.showToast) window.showToast(window.__ui?window.__ui('skipToEnd'):'Reached a branch point or the ending.'); return; }")
        lines.append("        this._resumeAuto = !!(window.SmartAutoPlay && window.SmartAutoPlay.isEnabled());")
        lines.append("        if (window.SmartAutoPlay) window.SmartAutoPlay.pauseForScreen();")
        lines.append("        var self=this; this._showWarning().then(function(ok){ if(ok) self._jump(); else { self._resumeAuto=false; if(window.SmartAutoPlay) window.SmartAutoPlay.resumeFromScreen(); } });")
        lines.append("    },")
        lines.append("    install: function(){")
        lines.append("        if (this._installed) return; this._installed=true; var self=this;")
        lines.append("        document.addEventListener('click',function(event){ var target=event.target&&event.target.closest?event.target.closest('[data-action=\"skip\"]'):null; if(!target) return; event.preventDefault(); event.stopImmediatePropagation(); event.stopPropagation(); self.request(); },true);")
        lines.append("    }")
        lines.append("};")
        lines.append("window.SkipController.install();")
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
        lines.append(f"                            const snap = localStorage.getItem(window._scopedStorageKey('ChapterEntryVars_{chap_id}_{node_id}'));")
        lines.append(f"                            if (snap) vars = JSON.parse(snap);")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                    }}")
        lines.append(f"                    if (!choiceStats) {{")
        lines.append(f"                        try {{")
        lines.append(f"                            const snap = localStorage.getItem(window._scopedStorageKey('ChapterEntryChoiceStats_{chap_id}_{node_id}'));")
        lines.append(f"                            if (snap) choiceStats = JSON.parse(snap);")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                    }}")
        lines.append(f"                    if (!legacyFlags) {{")
        lines.append(f"                        try {{")
        lines.append(f"                            const snap = localStorage.getItem(window._scopedStorageKey('ChapterEntryLegacyFlags_{chap_id}_{node_id}'));")
        lines.append(f"                            if (snap) legacyFlags = JSON.parse(snap);")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                    }}")
        lines.append(f"                    //  3. 兜底:tree.json 初始值")
        lines.append(f"                    if (!vars) vars = Object.assign({{}}, _initVars);")
        lines.append(f"                    window._playerVariables = window._sanitizePlayerVariables(vars);")
        lines.append(f"                    window._playerVariableChoices = window._sanitizeChoiceStats(choiceStats || {{}});")
        lines.append(f"                    window._playerLegacyFlags = window._sanitizeLegacyFlags(legacyFlags || {{}});")
        lines.append(f"                    // 保存章节入口快照(防 restart 污染)")
        lines.append(f"                    try {{ localStorage.setItem(window._scopedStorageKey('ChapterEntryVars_{chap_id}_{node_id}'), JSON.stringify(window._playerVariables)); }} catch(e) {{}}")
        lines.append(f"                    try {{ localStorage.setItem(window._scopedStorageKey('ChapterEntryChoiceStats_{chap_id}_{node_id}'), JSON.stringify(window._playerVariableChoices)); }} catch(e) {{}}")
        lines.append(f"                    try {{ localStorage.setItem(window._scopedStorageKey('ChapterEntryLegacyFlags_{chap_id}_{node_id}'), JSON.stringify(window._playerLegacyFlags)); }} catch(e) {{}}")
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
        lines.append("                (() => {")
        lines.append("                    const backBtn = document.querySelector('[data-action=\"back\"]');")
        lines.append("                    if (backBtn) {")
        lines.append("                        backBtn.setAttribute('disabled', 'true');")
        lines.append("                        backBtn.style.opacity = '0.3';")
        lines.append("                        backBtn.style.pointerEvents = 'auto';")
        lines.append("                        backBtn.style.cursor = 'not-allowed';")
        lines.append("                        console.log('🚫 [Start-Apply] 禁用Back按钮');")
        lines.append("                    }")
        lines.append("                })();")
        lines.append("                ")
        lines.append("                // 注册当前语言音频到 Monogatari（幂等；Start/Continue/读档均可调用）")
        lines.append("                if (window.__registerCurrentVoices) window.__registerCurrentVoices();")
        lines.append(f"                // 预加载前10步音频")
        lines.append(f"                if (window.ProgressiveLoader) window.ProgressiveLoader.load({min_steps}, 10);")
        lines.append("                ")
        lines.append("                return true;")
        lines.append("            }, ")
        lines.append("            'Reverse': () => { ")
        lines.append("                // [Back按钮修复]回退到Start时也禁用Back")
        lines.append("                (() => {")
        lines.append("                    const backBtn = document.querySelector('[data-action=\"back\"]');")
        lines.append("                    if (backBtn) {")
        lines.append("                        backBtn.setAttribute('disabled', 'true');")
        lines.append("                        backBtn.style.opacity = '0.3';")
        lines.append("                        backBtn.style.pointerEvents = 'auto';")
        lines.append("                        backBtn.style.cursor = 'not-allowed';")
        lines.append("                        console.log('🚫 [Start-Reverse] 禁用Back按钮');")
        lines.append("                    }")
        lines.append("                })();")
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
        lines.append("                // Continue 不经过 Start，必须先注册当前语言音频映射，否则恢复后只显示字幕无配音。")
        lines.append("                if (window.__registerCurrentVoices) window.__registerCurrentVoices();")
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
            # Route the final material through a tiny bridge label.  A direct
            # jump into EndChapter can race with the currently pending NVL /
            # Dialog action (Monogatari's Jump action starts the target action
            # asynchronously).  The bridge gives the engine one clean label
            # boundary before the choice/ending UI is mounted.
            next_label = (next_pair.get('label') or f"Material_{next_pair['index']}") if next_pair else "EndBridge"

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
                # 选项文本可能只在某一个语言素材目录中存在。只要任一
                # 支持语言有该 __opt key，就保留占位符，运行期按 URL
                # lang 选择对应文案；不能只检查英语层，否则日语-only
                # 素材会再次退回 Option A/B。
                def _has_localized_option(opt_key):
                    return bool(opt_key) and any(
                        f"{opt_key}__opt" in (self.i18n_text.get(lang, {}) or {})
                        for lang in self.languages
                    )

                _opt_a_has = _has_localized_option(opt_a_key)
                _opt_b_has = _has_localized_option(opt_b_key)
                opt_a_ph = f"{_MARK}{opt_a_key}__opt{_MARK}" if _opt_a_has else opt_a
                opt_b_ph = f"{_MARK}{opt_b_key}__opt{_MARK}" if _opt_b_has else opt_b
                _a_first_seq = (cn.get('branch_a') or [{'seq': 1}])[0]['seq']
                _b_first_seq = (cn.get('branch_b') or [{'seq': 1}])[0]['seq']
                # 上游偶尔只产出一侧分支素材（例如只有 _B 没有 _A）。此时下面的
                # 分支 label 循环不会生成空侧的 label，但选项仍会渲染，点下去就
                # jump 到不存在的 label —— 整个游戏卡死在这里。
                # 兜底：空侧的 jump 目标改指向另一侧的首个 beat；两侧都空时直接
                # 汇到该 pivot 之后的主线，宁可少一段分支剧情也不能卡住玩家。
                _a_mats = cn.get('branch_a') or []
                _b_mats = cn.get('branch_b') or []
                _a_jump = f"BranchA_{idx}_{_a_first_seq}"
                _b_jump = f"BranchB_{idx}_{_b_first_seq}"
                _branch_fallback_label = _choice_labels_by_index.get(str(idx + 1)) or f"Material_{idx + 1}"
                if not _a_mats and _b_mats:
                    _a_jump = f"BranchB_{idx}_{_b_first_seq}"
                    print(f"  ⚠️ beat {idx}: branch_a 素材缺失，选项A 兜底跳向 branch_b（避免卡死）")
                elif not _b_mats and _a_mats:
                    _b_jump = f"BranchA_{idx}_{_a_first_seq}"
                    print(f"  ⚠️ beat {idx}: branch_b 素材缺失，选项B 兜底跳向 branch_a（避免卡死）")
                elif not _a_mats and not _b_mats:
                    _a_jump = _b_jump = _branch_fallback_label
                    print(f"  ⚠️ beat {idx}: 两侧分支素材均缺失，选项直接跳向 {_branch_fallback_label}（避免卡死）")

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
                _choice_prev_speaker_js = json.dumps(
                    str((_pivot_audio or {}).get('meta', {}).get('speaker_id') or '')
                    if _pivot_audio else '',
                    ensure_ascii=False,
                )

                # 选项界面 label（有 pivot 时为 Choice_N，否则 Material_N）
                _choice_token_js = json.dumps(cur_label, ensure_ascii=False)
                lines.append(f"    '{cur_label}': [")
                lines.append(
                    f"        {{'Function':{{ 'Apply': () => {{ window._currentMaterialIndex = {idx}; if(window.BranchNavigator) window.BranchNavigator.enter({_choice_nav_state_js}); "
                    # 上一 beat 若是 NVL 独白，overlay 仍全屏铺在最上层（z-index 9700）
                    # 且会吞掉点击，选项渲染在它底下就点不到。Material 节点每个都
                    # 关，选项节点同样必须关。
                    f"if(window.NvlOverlay) window.NvlOverlay.hide(); "
                    f"if(window.BranchProgress) window.BranchProgress.clear(); "
                    f"if(window.BackVoicePlayer) window.BackVoicePlayer.enter({_choice_token_js}, ''); "
                    f"if(window.StageManager) window.StageManager.setSpeaker(''); "
                    f"if(window.AmbiencePlayer) window.AmbiencePlayer.sync({ambience_js}); return true; }}, "
                    f"'Reverse': () => {{ if(window.AmbiencePlayer) window.AmbiencePlayer.sync({ambience_js}); "
                    f"if(window.StageManager) window.StageManager.setSpeaker({_choice_prev_speaker_js}); "
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
                lines.append(f"                    'Do': 'jump {_a_jump}',")
                lines.append(f"                }},")
                lines.append(f"                'BranchB_{idx}': {{")
                lines.append(f"                    'Text': '{opt_b_ph}',")
                lines.append(f"                    'Do': 'jump {_b_jump}',")
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
                    _branch_prev_speaker_id = str(
                        beat_meta_by_index.get(idx, {}).get('speaker_id') or ''
                    )
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
                        # CG 不再使用全屏覆盖层，但仍显示字幕；仅隐藏说话人姓名。
                        _b_is_cg_beat = bool(_bm_meta.get('cg_mode'))
                        _branch_token_js = json.dumps(label, ensure_ascii=False)
                        _branch_voice_js = json.dumps(b_audio['id'] if b_audio else '', ensure_ascii=False)
                        _branch_text_key_js = json.dumps(
                            b_sub.get('subtitle_id', '') if b_sub else '',
                            ensure_ascii=False,
                        )
                        _branch_fallback_text_js = json.dumps(
                            ' '.join(str(d.get('text', '')).strip() for d in (b_sub.get('dialogues') or []) if str(d.get('text', '')).strip())
                            if b_sub else '',
                            ensure_ascii=False,
                        )
                        _b_scene = bm.get('scene_to_show') or _branch_prev_scene
                        _b_has_scene = _b_scene in valid_scene_stems
                        _b_is_nvl = bool(_bm_meta.get('nvl_mode'))
                        _b_cast = self._parse_character_position(_bm_meta.get('character_position')) if not (_b_is_nvl or _b_is_cg_beat) else []
                        _b_cast_js = self._cast_to_js(_b_cast)
                        _b_speaker_id = _bm_meta.get('speaker_id')
                        _branch_speaker_js = json.dumps(str(_b_speaker_id or ''), ensure_ascii=False)
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
                            f"window._currentMaterialIndex = {idx}; "
                            f"{_effect_apply_js}"
                            f"if(window.BranchProgress) window.BranchProgress.set({idx}, '{br_letter}', {seq}, {_branch_token_js}); "
                            f"if(window.BranchNavigator) window.BranchNavigator.enter({_branch_nav_state_js}); "
                            f"if(window.AutoVoiceAdvance) window.AutoVoiceAdvance.enter({_branch_token_js}, {_branch_voice_js}, {_branch_text_key_js}, {_branch_fallback_text_js}); "
                            f"if(window.BackVoicePlayer) window.BackVoicePlayer.enter({_branch_token_js}, {_branch_voice_js}); "
                            f"if(window.SystemPanel) window.SystemPanel.setText({_b_system_text_js}); "
                            f"if(window.CgOverlay) window.CgOverlay.setMode({_b_cg_js}, {_b_cg_file_js}); "
                            f"if(window.NvlOverlay && !{str(_b_is_nvl).lower()}) window.NvlOverlay.hide(); "
                            f"StageManager.setCast({_b_cast_js}, {_branch_speaker_js}); return true; }}, "
                            f"'Reverse': () => {{ if(window.AutoVoiceAdvance) window.AutoVoiceAdvance.cancel({_branch_token_js}); "
                            f"{_effect_reverse_js}"
                            f"if(window.BranchProgress) {{ if({str(bm_idx > 0).lower()}) window.BranchProgress.set({idx}, '{br_letter}', {_branch_prev_seq}, {_branch_prev_label_js}); else window.BranchProgress.clear(); }} "
                            f"if(window.SystemPanel) window.SystemPanel.setText({_prev_system_text_js}); "
                            f"if(window.CgOverlay) window.CgOverlay.setMode({_branch_prev_cg_js}, {_branch_prev_cg_file_js}); "
                            f"if(window.NvlOverlay && !{str(_branch_prev_nvl).lower()}) window.NvlOverlay.hide(); "
                            f"StageManager.setCast({_branch_prev_cast_js}, {json.dumps(_branch_prev_speaker_id, ensure_ascii=False)}); "
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

                        if b_sub and b_sub.get('dialogues'):
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
                                        # 与主线 NVL 同一个兜底：译文缺失时不要留一屏空白。
                                        # 字面量取未转义的 text，避免双重转义。
                                        _b_fallback_js = json.dumps(str(text).strip(), ensure_ascii=False)
                                        lines.append(
                                            f"        {{'Function':{{ 'Apply': () => {{ if(window.NvlOverlay) window.NvlOverlay.show(window.__i18nText({_b_key_js}) || {_b_fallback_js}); return true; }}, 'Reverse': () => {{ if(window.NvlOverlay) window.NvlOverlay.hide(); return true; }} }} }},")
                                        lines.append(f"        '{_b_placeholder}',")
                                    elif _b_is_cg_beat or self._is_narrator_speaker(_b_speaker_id):
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
                            lines.append(
                                f"        {{'Function':{{ 'Apply': () => {{ StageManager.reset(); return true; }}, "
                                f"'Reverse': () => {{ StageManager.setCast({_b_cast_js}, {_branch_speaker_js}); return true; }} }} }},"
                            )
                            lines.append(f"        'jump {next_label}',")
                            lines.append(f"    ],")
                            lines.append(f"")
                        _branch_prev_cast_js = _b_cast_js
                        _branch_prev_speaker_id = str(_b_speaker_id or '')
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
            previous_index = audio_subtitle_pairs[pair_idx - 1]['index'] if pair_idx > 0 else idx
            previous_speaker_id = str(
                beat_meta_by_index.get(previous_index, {}).get('speaker_id') or ''
            )
            is_nvl = bool(beat_meta.get('nvl_mode'))
            # CG 不再启用全屏覆盖层，使用正常场景与字幕流程；姓名栏保持隐藏。
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

            is_normal_mode = (not is_hook_beat) and (not is_nvl) and (not is_cg_beat)
            cast = self._parse_character_position(beat_meta.get('character_position')) if is_normal_mode else []
            cast_js = self._cast_to_js(cast)

            lines.append(f"    '{cur_label}': [")
            _beat_id_raw = beat_meta.get('beat_id_raw', '')
            if _beat_id_raw:
                lines.append(f"        // @beat {_beat_id_raw}")

            # 1. 状态重置 (UI更新) + 特效清理
            lines.append(f"        {{'Function':{{ ")
            lines.append(f"            'Apply': () => {{")
            lines.append(f"                window._currentMaterialIndex = {idx};")
            lines.append(f"                // 清理上一个beat的持续特效")
            lines.append(f"                if (window.AtmosphereEffects) window.AtmosphereEffects.clearAll();")
            lines.append(f"                if (window.BranchNavigator) window.BranchNavigator.clear();")
            lines.append(f"                if (window.BranchProgress) window.BranchProgress.clear();")
            current_voice_js = json.dumps(audio['id'] if audio else '', ensure_ascii=False)
            current_token_js = json.dumps(cur_label, ensure_ascii=False)
            current_text_key_js = json.dumps(
                subtitle.get('subtitle_id', '') if subtitle else '',
                ensure_ascii=False,
            )
            current_fallback_text_js = json.dumps(
                ' '.join(str(d.get('text', '')).strip() for d in (subtitle.get('dialogues') or []) if str(d.get('text', '')).strip())
                if subtitle else '',
                ensure_ascii=False,
            )
            lines.append(
                f"                if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.enter({current_token_js}, {current_voice_js}, {current_text_key_js}, {current_fallback_text_js});")
            lines.append(
                f"                if (window.BackVoicePlayer) window.BackVoicePlayer.enter({current_token_js}, {current_voice_js});")
            if not is_scene_change:
                # 同场景时先于音乐、音效、show scene 同步人物，杜绝退场残留帧。
                lines.append(
                    f"                StageManager.setCast({cast_js if is_normal_mode else '[]'}, "
                    f"{json.dumps(str(beat_meta.get('speaker_id') or '') if is_normal_mode else '', ensure_ascii=False)});"
                )
            lines.append(f"                if (monogatari.global) {{")
            lines.append(f"                    monogatari.global('auto_play', false);")
            lines.append(f"                    monogatari.global('skipping', false);")
            lines.append(f"                }}")
            lines.append(f"                document.body.removeAttribute('data-action');")
            lines.append(f"                ")
            lines.append(f"                // [Back按钮修复]控制Back按钮的启用/禁用状态")
            lines.append(f"                (() => {{")
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
            # 保留 disabled 按钮自身作为命中目标，不能用 pointer-events:none，
            # 否则点击按钮位置会穿透到 game-screen 推进剧情。
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
            lines.append(f"                }})();")
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
                lines.append(
                    f"                StageManager.setCast({prev_cast_js}, "
                    f"{json.dumps(previous_speaker_id, ensure_ascii=False)});"
                )
            lines.append(f"                // [Back按钮修复]回退时检查前一索引的Back按钮状态")
            lines.append(f"                (() => {{")
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
            # disabled 按钮仍需拦住底层 game-screen，避免点击穿透。
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
            lines.append(f"                }})();")
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
                lines.append(
                    f"        {{'Function':{{ 'Apply': () => {{ StageManager.setCast({cast_js}, "
                    f"{json.dumps(str(beat_meta.get('speaker_id') or ''), ensure_ascii=False)}); return true; }}, "
                    f"'Reverse': () => {{ StageManager.setCast({prev_cast_js}, "
                    f"{json.dumps(previous_speaker_id, ensure_ascii=False)}); return true; }} }} }},"
                )
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ SceneFX.reveal(); return true; }}, 'Reverse': () => {{ SceneFX.clear(); return true; }} }} }},")
            elif is_normal_mode:
                # 同场景立绘已在本 beat 的第一个 Function 中同步。
                lines.append(f"        'show scene {scene_target}',")
            else:
                # hook/nvl/cg：不放立绘；但需清空立绘，且 back 回退时恢复上一 beat 的立绘
                transition_suffix = " with fadeIn" if is_scene_change else ""
                lines.append(f"        'show scene {scene_target}{transition_suffix}',")
                if is_scene_change:
                    lines.append(
                        f"        {{'Function':{{ 'Apply': () => {{ StageManager.setCast([], ''); return true; }}, "
                        f"'Reverse': () => {{ StageManager.setCast({prev_cast_js}, "
                        f"{json.dumps(previous_speaker_id, ensure_ascii=False)}); return true; }} }} }},"
                    )

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
            if subtitle and subtitle.get('dialogues'):
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
                            # __i18nText 在当前语言和默认语言都缺该 key 时返回空串，
                            # NVL 就成了一屏空白。普通模式用 _has_i18n 回退到内嵌原文，
                            # 这里对齐同样的兜底。字面量必须从未转义的原文生成：
                            # text_clean 是为引擎单引号字符串准备的，再进 JS 参数会双重转义。
                            fallback_js = json.dumps(text_display.strip(), ensure_ascii=False)
                            lines.append(
                                f"        {{'Function':{{ 'Apply': () => {{ if(window.NvlOverlay) window.NvlOverlay.show(window.__i18nText({key_js}) || {fallback_js}); return true; }}, 'Reverse': () => {{ if(window.NvlOverlay) window.NvlOverlay.hide(); return true; }} }} }},")
                            lines.append(f"        '{placeholder}',")
                            has_blocking_action = True
                    else:
                        # ---- 普通模式 / CG 模式：说话人由 beat 的 speaker_id 驱动 ----
                        # CG 只是背景换成全屏大图并隐藏立绘，对话框行为与普通模式一致
                        beat_speaker_id = beat_meta.get('speaker_id')
                        if text_clean:
                            if is_cg_beat or self._is_narrator_speaker(beat_speaker_id):
                                # CG / 旁白：裸占位行，who 区域为空（不显示说话人名）
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
        # Keep EndChapter as the public ending/choice label, but enter it via
        # a cleanup bridge.  NVL owns a second (hidden) Dialog state; clearing
        # that state and yielding one frame prevents the final NVL action from
        # re-entering Material_N when the ending is reached by seek/skip/click.
        lines.append(f"    'EndBridge': [")
        lines.append("        {'Function':{")
        lines.append("            'Apply': () => {")
        lines.append("                console.info('[EndBridge] clearing transient NVL/Dialog state');")
        lines.append("                try { if (window.SmartAutoPlay) { window.SmartAutoPlay.clearTimer(); window.SmartAutoPlay.clearRetry(); window.SmartAutoPlay.clearAudioWatch(); window.SmartAutoPlay.clearTypingWatch(); } } catch(e) {}")
        lines.append("                try { if (window.AutoVoiceAdvance) window.AutoVoiceAdvance.reset(); if (window.BackVoicePlayer) window.BackVoicePlayer.stop(); } catch(e) {}")
        lines.append("                try { if (window.NvlOverlay) { window.NvlOverlay.finishTyping(); if (window.NvlOverlay.syncEngineTyping) window.NvlOverlay.syncEngineTyping(); window.NvlOverlay.hide(); } } catch(e) {}")
        lines.append("                try { if (window.CgOverlay) window.CgOverlay.setMode(false, ''); document.querySelectorAll('choice-container, [data-component=\"choice-container\"]').forEach(function(n){ n.remove(); }); } catch(e) {}")
        lines.append("                try { if (monogatari.action) monogatari.action('Dialog').reset(); } catch(e) {}")
        lines.append("                try { monogatari.global('block', false); monogatari.global('_engine_block', false); monogatari.global('_executing_sub_action', false); monogatari.global('auto_play', false); monogatari.global('skipping', false); } catch(e) {}")
        lines.append("                window._nvlAdvancing = false; window._cgAdvancing = false;")
        lines.append("                return new Promise(function(resolve){ var release=function(){ try { monogatari.global('block', false); monogatari.global('_engine_block', false); monogatari.global('_executing_sub_action', false); } catch(e) {} resolve(true); }; if (window.requestAnimationFrame) requestAnimationFrame(function(){ requestAnimationFrame(release); }); else setTimeout(release, 32); });")
        lines.append("            },")
        lines.append("            'Reverse': () => { return true; }")
        lines.append("        }},")
        lines.append(f"        'jump EndChapter',")
        lines.append(f"    ],")
        lines.append(f"")
        lines.append(f"    'EndChapter': [")
        lines.append(f"        'stop sound',")  # 确保进入选择界面时所有音效停止
        lines.append(f"        {{'Function':{{ 'Apply': () => {{ if(window.AmbiencePlayer) window.AmbiencePlayer.stop(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")  # 停环境底音
        if node_data and node_data.get('children') and folder_mapping:
            children = node_data['children']
            # 故事树会把普通的线性章节也表示成一个 child（通常带有
            # “继续”或下一章标题）。唯一可达子节点不是用户选择，不能
            # 渲染 Choice/“Please make your choice”；直接沿用章节导航器
            # 跳到下一章。只有两个以上可达子节点才是真正的选择界面。
            linear_child = None
            if len(children) == 1:
                _candidate = children[0] if isinstance(children[0], dict) else {}
                _candidate_id = str(_candidate.get('child_id') or '')
                if _candidate_id and folder_mapping.get(_candidate_id):
                    # branch_variable 仅用于条件结局；这类边仍交给下面的
                    # 自动结局路由，避免绕过阈值/遗产标记检查。
                    if not _candidate.get('branch_variable'):
                        linear_child = _candidate

            # 检测是否为结局自动路由（所有 choice_text 均为空）
            all_empty = all(not str(c.get('choice_text') or '').strip() for c in children)
            if linear_child is not None:
                # 只有一个可达且没有 branch_variable 的 child 是普通线性章节，
                # 不是玩家选择。旧逻辑仍渲染 Choice + 永久 Promise，导致
                # 页面停在 “Please make your choice”。这里直接发起章节路由，
                # 同时保留变量/分支统计/语言参数，兼容 iframe 与独立打开。
                child_node_id = str(linear_child.get('child_id') or '')
                escaped_child_node_id = child_node_id.replace("'", "\\'")
                target_folder = str(folder_mapping.get(child_node_id) or '')
                escaped_target_folder = target_folder.replace("'", "\\'")
                base_url = f"/{game_project + '/' if game_project else ''}{escaped_target_folder}/"
                child_node_id_js = json.dumps(child_node_id, ensure_ascii=False)
                lines.append(f"        {{'Function':{{")
                lines.append(f"            'Apply': () => {{")
                lines.append(f"                const _varsStr = encodeURIComponent(JSON.stringify(window._playerVariables || {{}}));")
                lines.append(f"                const _choiceStatsStr = encodeURIComponent(JSON.stringify(window._playerVariableChoices || {{}}));")
                lines.append(f"                const _legacyFlagsStr = encodeURIComponent(JSON.stringify(window._playerLegacyFlags || {{}}));")
                lines.append(f"                const _langStr = encodeURIComponent(window._lang || window._defaultLang || 'en-US');")
                lines.append(f"                const _targetUrl = '{base_url}?vars=' + _varsStr + '&choice_stats=' + _choiceStatsStr + '&legacy_flags=' + _legacyFlagsStr + '&lang=' + _langStr;")
                lines.append(f"                console.log('➡️ [EndChapter] 线性章节自动跳转: {escaped_child_node_id}');")
                lines.append(f"                if (window.StoryNavigator && window.StoryNavigator.request) {{")
                lines.append(f"                    window.StoryNavigator.request(_targetUrl, {child_node_id_js});")
                lines.append(f"                }} else if (!window._canEnterStandalone || window._canEnterStandalone(_targetUrl)) {{")
                lines.append(f"                    window.location.href = _targetUrl;")
                lines.append(f"                }}")
                lines.append(f"                return true;")
                lines.append(f"            }},")
                lines.append(f"            'Reverse': () => {{ return true; }}")
                lines.append(f"        }}}},")
                lines.append(f"    ],")
                lines.append(f"")
            elif all_empty:
                # 结局前最后一章：按最大变量值自动路由，不展示选择菜单
                lines.append(f"        '␟ui:chapterComplete␟',")
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
                lines.append(f"                        const targetUrl = path + sep + 'vars=' + varsStr + '&choice_stats=' + choiceStatsStr + '&legacy_flags=' + legacyFlagsStr + '&lang=' + encodeURIComponent(window._lang || window._defaultLang || 'en-US');")
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
                # 多子节点才是实际分支。单个非空 child 只是线性章节推进：
                # 只有真正的多路分支才渲染 Choice。
                lines.append(f"        '␟ui:chapterComplete␟',")
                lines.append(f"        '␟ui:makeChoice␟',")
                lines.append(f"        {{")
                lines.append(f"            'Choice': {{")
                for child in children:
                    choice_text = child['choice_text']
                    display_choice_text = "␟ui:cont␟" if self._is_generic_continue_choice(choice_text) else choice_text
                    if not self._is_generic_continue_choice(choice_text):
                        display_choice_text = _localized_choice_text(child.get('child_id'), display_choice_text)
                    escaped_choice_text = display_choice_text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"")
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
                    display_choice_text = "␟ui:cont␟" if self._is_generic_continue_choice(choice_text) else choice_text
                    if not self._is_generic_continue_choice(choice_text):
                        display_choice_text = _localized_choice_text(child.get('child_id'), display_choice_text)
                    escaped_choice_text = display_choice_text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"")
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
                    lines.append(f"                        request_id: String(Date.now()) + '_' + Math.random().toString(36).slice(2),")
                    lines.append(f"                        chapter_id: '{chap_id}',")
                    lines.append(f"                        node_id: '{node_id}',")
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
                    lines.append(f"                const messageHandler = (event) => {{")
                    lines.append(f"                    console.log('📨 [Choice] 收到消息:', event.data);")
                    lines.append(f"                    console.log('📨 [Choice] 消息来源:', event.origin);")
                    lines.append(f"                    console.log('📨 [Choice] 消息类型:', event.data?.type);")
                    lines.append(f"                    if (event.data && event.data.type === 'CHAPTER_PERMIT' && window._messageMatchesContext(event, event.data.data || {{}}) && (!event.data.data || !event.data.data.request_id || event.data.data.request_id === requestMsg.data.request_id)) {{")
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
                    lines.append(f"                            window.showToast(window.__ui ? window.__ui('loading') : 'Loading...');")
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
                    lines.append(f"                            window.showToast(window.__ui ? window.__ui('accessDenied') : 'You do not have access to this chapter.');")
                    lines.append(f"                        }}")
                    lines.append(f"                    }}")
                    lines.append(f"                }};")
                    lines.append(f"                window.addEventListener('message', messageHandler);")
                    lines.append(f"                console.log('✅ [Choice] 消息监听器已设置');")
                    lines.append(f"                if (window.parent && window.parent !== window) {{")
                    lines.append(f"                    try {{")
                    lines.append(f"                        console.log('📤 [Choice] 开始发送postMessage...');")
                    lines.append(f"                        window._postToParent(requestMsg);")
                    lines.append(f"                        console.log('✅ [Choice] postMessage调用成功');")
                    lines.append(f"                    }} catch (e) {{")
                    lines.append(f"                        console.error('❌ [Choice] 发送消息失败:', e);")
                    lines.append(f"                    }}")
                    lines.append(f"                }} else {{")
                    lines.append(f"                    window.removeEventListener('message', messageHandler);")
                    lines.append(f"                    if (!window._canEnterStandalone(targetUrl)) return false;")
                    lines.append(f"                    console.log('🔧 [Debug] 独立模式（非iframe），直接跳转');")
                    lines.append(f"                    window.location.href = targetUrl;")
                    lines.append(f"                    return true;")
                    lines.append(f"                }}")
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
            # 真正的最终节点：按 UI 模式展示终幕与当前数值，再结束游戏。
            lines.append(f"        '␟ui:chapterComplete␟',")
            completed_task_id_js = json.dumps(str(self.task_id or game_project or ''), ensure_ascii=False)
            completed_game_project_js = json.dumps(str(game_project or ''), ensure_ascii=False)
            completed_chapter_id_js = json.dumps(chap_id, ensure_ascii=False)
            completed_node_id_js = json.dumps(str(node_data.get('id', '') if node_data else node_id), ensure_ascii=False)
            lines.append(f"        {{'Function':{{ 'Apply': () => {{")
            lines.append(f"            if (window.AncientEndScreen) return window.AncientEndScreen.show();")
            lines.append(f"            if (window.OtherEndScreen) return window.OtherEndScreen.show();")
            lines.append(f"            return true;")
            lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
            lines.append(f"        {{'Function':{{ 'Apply': () => {{")
            lines.append(f"            if (window.__gameCompletedSent) return true;")
            lines.append(f"            window.__gameCompletedSent = true;")
            lines.append(f"            const completedData = {{")
            lines.append(f"                protocol_version: 1,")
            lines.append(f"                task_id: {completed_task_id_js},")
            lines.append(f"                game_project: {completed_game_project_js},")
            lines.append(f"                chapter_id: {completed_chapter_id_js},")
            lines.append(f"                node_id: {completed_node_id_js},")
            lines.append(f"                ending_title: window._chapterTitle || '',")
            lines.append(f"                timestamp: Date.now(),")
            lines.append(f"                player_variables: window._playerVariables || {{}},")
            lines.append(f"                player_variable_choices: window._playerVariableChoices || {{}},")
            lines.append(f"                player_legacy_flags: window._playerLegacyFlags || {{}}")
            lines.append(f"            }};")
            lines.append(f"            document.dispatchEvent(new CustomEvent('game-completed', {{ detail: completedData }}));")
            lines.append(f"            if (window.parent && window.parent !== window) {{")
            lines.append(f"                window._postToParent({{ type: 'GAME_COMPLETED', data: completedData }});")
            lines.append(f"                console.log('📤 [GameHostBridge] GAME_COMPLETED', completedData);")
            lines.append(f"            }} else {{")
            lines.append(f"                console.info('[GameHostBridge] standalone mode, game completion emitted locally', completedData);")
            lines.append(f"            }}")
            lines.append(f"            return true;")
            lines.append(f"        }}, 'Reverse': () => {{ return true; }} }} }},")
            lines.append(f"        'end',")
            lines.append(f"    ],")

        lines.append("});")

        return "\n".join(lines)

    def _get_ui_theme_css(self) -> str:
        """按 story_tree.UI_mode 返回游戏 UI；art_style 不再参与界面选型。"""

        if self.ui_mode != "Ancient":
            return r"""
/* ==================== 通用 Other UI ==================== */

body.ui-mode-other {
    --other-bg: #090a0c;
    --other-text: rgba(247, 247, 247, 0.94);
    --other-muted: rgba(232, 232, 232, 0.55);
    --other-highlight: #ffffff;
    --other-motion: 260ms;

    color: var(--other-text);
    font-family:
        "Segoe UI Variable",
        "Segoe UI",
        "PingFang SC",
        "Microsoft YaHei",
        sans-serif;
}

/* ==================== 首页背景 ==================== */

body.ui-mode-other main-screen,
body.ui-mode-other [data-screen="main"] {
    position: relative;
    isolation: isolate;
    overflow: hidden;
    background-color: var(--other-bg) !important;
}

/* 中性环境光，不带固定题材色彩。 */
body.ui-mode-other main-screen::before,
body.ui-mode-other [data-screen="main"]::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: 0;
    pointer-events: none;

    background:
        radial-gradient(
            ellipse at 16% 46%,
            rgba(255, 255, 255, 0.055) 0%,
            rgba(255, 255, 255, 0.018) 30%,
            transparent 58%
        ),
        radial-gradient(
            ellipse at 82% 18%,
            rgba(255, 255, 255, 0.022) 0%,
            transparent 44%
        );
}

/* 左侧暗场，保证菜单在明暗不同的背景上都清晰。 */
body.ui-mode-other main-screen::after,
body.ui-mode-other [data-screen="main"]::after {
    content: "";
    position: absolute;
    inset: 0;
    z-index: 0;
    pointer-events: none;

    background:
        linear-gradient(
            90deg,
            rgba(3, 4, 6, 0.94) 0%,
            rgba(3, 4, 6, 0.76) 17%,
            rgba(3, 4, 6, 0.39) 36%,
            rgba(3, 4, 6, 0.09) 60%,
            transparent 80%
        ),
        linear-gradient(
            180deg,
            rgba(0, 0, 0, 0.12) 0%,
            transparent 24%,
            transparent 72%,
            rgba(0, 0, 0, 0.20) 100%
        );
}

/* ==================== 首页菜单 ==================== */

body.ui-mode-other main-menu {
    position: absolute !important;
    z-index: 2 !important;

    top: 53% !important;
    right: auto !important;
    bottom: auto !important;
    left: clamp(3rem, 8.5vw, 10rem) !important;

    display: flex !important;
    flex-direction: column !important;
    flex-wrap: nowrap !important;
    align-items: stretch !important;
    justify-content: center !important;
    gap: 0.3rem !important;

    width: clamp(16rem, 19vw, 21rem) !important;
    max-width: none !important;
    height: auto !important;

    margin: 0 !important;
    padding: 0 !important;

    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;

    transform: translateY(-50%) !important;
}

/* 禁止引擎默认主题生成额外菜单装饰。 */
body.ui-mode-other main-menu::before,
body.ui-mode-other main-menu::after {
    content: none !important;
    display: none !important;
}

/* ==================== 首页按钮 ==================== */

body.ui-mode-other main-menu button,
body.ui-mode-other [data-screen="main"] main-menu button {
    position: relative !important;
    isolation: isolate;

    width: 100% !important;
    min-width: 0 !important;
    height: auto !important;
    min-height: 3.2rem !important;

    margin: 0 !important;
    padding: 0.64rem 0.85rem !important;
    box-sizing: border-box !important;
    overflow: visible !important;

    border: none !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: var(--other-text) !important;

    font-family:
        "Segoe UI Variable",
        "Segoe UI",
        "PingFang SC",
        "Microsoft YaHei",
        sans-serif !important;

    font-size: clamp(1.04rem, 1.18vw, 1.34rem) !important;
    font-weight: 400 !important;
    line-height: 1.3 !important;
    letter-spacing: 0.075em !important;

    text-align: left !important;
    text-transform: none !important;
    text-shadow: 0 1px 14px rgba(0, 0, 0, 0.86) !important;

    cursor: pointer !important;
    transform: none !important;
    animation: otherUniversalMenuEnter 520ms both !important;
}

/* 菜单项依次淡入。 */
body.ui-mode-other main-menu button:nth-child(2) {
    animation-delay: 45ms !important;
}

body.ui-mode-other main-menu button:nth-child(3) {
    animation-delay: 90ms !important;
}

body.ui-mode-other main-menu button:nth-child(4) {
    animation-delay: 135ms !important;
}

body.ui-mode-other main-menu button:nth-child(5) {
    animation-delay: 180ms !important;
}

body.ui-mode-other main-menu button:nth-child(6) {
    animation-delay: 225ms !important;
}

/* 停用引擎或旧主题挂在整个按钮上的装饰。 */
body.ui-mode-other main-menu button::before,
body.ui-mode-other main-menu button::after {
    content: none !important;
    display: none !important;
}

/* 最后一个 span 是按钮的可见文字。 */
body.ui-mode-other main-menu button > span:last-child {
    position: relative;
    z-index: 2;
    isolation: isolate;
    display: inline-block;
    color: inherit;

    transition:
        color var(--other-motion) ease,
        text-shadow var(--other-motion) ease,
        transform var(--other-motion) cubic-bezier(0.22, 1, 0.36, 1);
}

/* 柔和光幕：以文字中心为基准，不使用科技网格或刻度。 */
body.ui-mode-other main-menu button > span:last-child::before {
    content: "";
    position: absolute;
    z-index: -2;
    top: 50%;
    left: 50%;

    width: clamp(15rem, calc(100% + 9.5rem), 22rem);
    height: 4.2rem;

    opacity: 0;
    pointer-events: none;

    background:
        radial-gradient(
            ellipse at center,
            rgba(255, 255, 255, 0.13) 0%,
            rgba(255, 255, 255, 0.055) 30%,
            rgba(255, 255, 255, 0.018) 52%,
            transparent 74%
        ),
        linear-gradient(
            90deg,
            transparent 0%,
            rgba(255, 255, 255, 0.05) 12%,
            rgba(255, 255, 255, 0.28) 34%,
            rgba(255, 255, 255, 0.76) 50%,
            rgba(255, 255, 255, 0.28) 66%,
            rgba(255, 255, 255, 0.05) 88%,
            transparent 100%
        ) center 50% / 100% 1px no-repeat,
        linear-gradient(
            90deg,
            transparent 5%,
            rgba(255, 255, 255, 0.05) 25%,
            rgba(255, 255, 255, 0.16) 50%,
            rgba(255, 255, 255, 0.05) 75%,
            transparent 95%
        ) center 67% / 70% 1px no-repeat;

    filter:
        drop-shadow(0 0 5px rgba(255, 255, 255, 0.20))
        drop-shadow(0 0 17px rgba(255, 255, 255, 0.08));

    clip-path: inset(0 50% 0 50%);
    -webkit-clip-path: inset(0 50% 0 50%);

    transform: translate(-50%, -50%) scaleX(0.32);
    transform-origin: center;

    transition:
        opacity 180ms ease,
        clip-path 480ms cubic-bezier(0.22, 1, 0.36, 1),
        transform 480ms cubic-bezier(0.22, 1, 0.36, 1);
}

/* 一道更亮的中心细线，让交互清晰但不过分强调题材。 */
body.ui-mode-other main-menu button > span:last-child::after {
    content: "";
    position: absolute;
    z-index: -1;
    top: 50%;
    left: 50%;

    width: clamp(11rem, calc(100% + 6.5rem), 17rem);
    height: 1px;

    opacity: 0;
    pointer-events: none;

    background: linear-gradient(
        90deg,
        transparent 0%,
        rgba(255, 255, 255, 0.10) 15%,
        rgba(255, 255, 255, 0.52) 42%,
        rgba(255, 255, 255, 0.92) 50%,
        rgba(255, 255, 255, 0.52) 58%,
        rgba(255, 255, 255, 0.10) 85%,
        transparent 100%
    );

    box-shadow:
        0 0 5px rgba(255, 255, 255, 0.34),
        0 0 13px rgba(255, 255, 255, 0.12);

    transform: translate(-50%, -50%) scaleX(0.25);
    transform-origin: center;

    transition:
        opacity 190ms ease 35ms,
        transform 520ms cubic-bezier(0.22, 1, 0.36, 1);
}

/* ==================== 悬浮状态 ==================== */

body.ui-mode-other main-menu button:hover,
body.ui-mode-other main-menu button:focus-visible {
    padding-left: 0.85rem !important;
    border: none !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: var(--other-text) !important;
    transform: none !important;
}

body.ui-mode-other main-menu button:hover > span:last-child::before,
body.ui-mode-other main-menu button:focus-visible > span:last-child::before {
    opacity: 0.88;
    clip-path: inset(0);
    -webkit-clip-path: inset(0);
    transform: translate(-50%, -50%) scaleX(1);
    animation: otherUniversalAura 3s ease-in-out infinite;
}

body.ui-mode-other main-menu button:hover > span:last-child::after,
body.ui-mode-other main-menu button:focus-visible > span:last-child::after {
    opacity: 0.78;
    transform: translate(-50%, -50%) scaleX(1);
}

body.ui-mode-other main-menu button:hover > span:last-child,
body.ui-mode-other main-menu button:focus-visible > span:last-child {
    color: var(--other-highlight) !important;

    text-shadow:
        0 0 2px rgba(255, 255, 255, 0.84),
        0 0 9px rgba(255, 255, 255, 0.25),
        0 2px 16px rgba(0, 0, 0, 0.94);

    transform: translateY(-1px) scale(1.01);
}

/* 重新开始与主操作略微分隔。 */
body.ui-mode-other main-menu .restart-button {
    margin-top: 0.28rem !important;
}

/* 退出按钮默认降低视觉权重，悬浮时仍使用统一效果。 */
body.ui-mode-other main-menu .main-quit-button {
    margin-top: 0.62rem !important;
    color: var(--other-muted) !important;
}

body.ui-mode-other main-menu .main-quit-button:hover > span:last-child,
body.ui-mode-other main-menu .main-quit-button:focus-visible > span:last-child {
    color: var(--other-highlight) !important;
}

/* ==================== 禁用状态 ==================== */

body.ui-mode-other main-menu button:disabled,
body.ui-mode-other main-menu button[disabled] {
    opacity: 0.28 !important;
    padding-left: 0.85rem !important;
    pointer-events: none !important;
    cursor: not-allowed !important;
    color: var(--other-muted) !important;
    background: transparent !important;
    box-shadow: none !important;
    transform: none !important;
}

body.ui-mode-other main-menu button:disabled > span:last-child::before,
body.ui-mode-other main-menu button:disabled > span:last-child::after,
body.ui-mode-other main-menu button[disabled] > span:last-child::before,
body.ui-mode-other main-menu button[disabled] > span:last-child::after {
    display: none !important;
}

/* ==================== 动画 ==================== */

@keyframes otherUniversalMenuEnter {
    from {
        opacity: 0;
        filter: blur(3px);
    }

    to {
        opacity: 1;
        filter: blur(0);
    }
}

@keyframes otherUniversalAura {
    0%,
    100% {
        opacity: 0.7;
        filter:
            drop-shadow(0 0 5px rgba(255, 255, 255, 0.16))
            drop-shadow(0 0 14px rgba(255, 255, 255, 0.06));
    }

    50% {
        opacity: 0.96;
        filter:
            drop-shadow(0 0 7px rgba(255, 255, 255, 0.25))
            drop-shadow(0 0 20px rgba(255, 255, 255, 0.10));
    }
}

/* ==================== 响应式 ==================== */

@media (max-width: 800px) {
    body.ui-mode-other main-menu {
        left: 10vw !important;
        width: min(72vw, 19rem) !important;
    }

    body.ui-mode-other main-menu button,
    body.ui-mode-other [data-screen="main"] main-menu button {
        min-height: 2.9rem !important;
        padding: 0.54rem 0.7rem 0.54rem 0.85rem !important;
        font-size: clamp(0.92rem, 3.3vw, 1.08rem) !important;
    }

    body.ui-mode-other main-menu button:hover,
    body.ui-mode-other main-menu button:focus-visible {
        padding-left: 0.85rem !important;
    }

    body.ui-mode-other main-menu button > span:last-child::before {
        width: clamp(13rem, calc(100% + 7rem), 18rem);
        height: 3.8rem;
    }

    body.ui-mode-other main-menu button > span:last-child::after {
        width: clamp(10rem, calc(100% + 5rem), 15rem);
    }
}

/* 手机横屏和高度较小的窗口。 */
@media (orientation: landscape) and (max-height: 600px) {
    body.ui-mode-other main-menu {
        top: 54% !important;
        left: 8vw !important;
        width: clamp(14rem, 25vw, 18rem) !important;
        gap: 0.04rem !important;
    }

    body.ui-mode-other main-menu button,
    body.ui-mode-other [data-screen="main"] main-menu button {
        min-height: 2.35rem !important;
        padding: 0.36rem 0.65rem 0.36rem 0.85rem !important;
        font-size: clamp(0.8rem, 2.8vh, 0.98rem) !important;
    }

    body.ui-mode-other main-menu button:hover,
    body.ui-mode-other main-menu button:focus-visible {
        padding-left: 0.85rem !important;
    }

    body.ui-mode-other main-menu .restart-button {
        margin-top: 0.12rem !important;
    }

    body.ui-mode-other main-menu .main-quit-button {
        margin-top: 0.25rem !important;
    }
}

/* 尊重系统的“减少动态效果”设置。 */
@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other main-menu button,
    body.ui-mode-other [data-screen="main"] main-menu button,
    body.ui-mode-other main-menu button > span:last-child,
    body.ui-mode-other main-menu button > span:last-child::before,
    body.ui-mode-other main-menu button > span:last-child::after {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}

/* ==================== Save / Load 电影资料库 ==================== */

/* 两个页面使用完整暗场，不再把所有内容包进一个中央大面板。 */
body.ui-mode-other save-screen,
body.ui-mode-other load-screen,
body.ui-mode-other [data-component="save-screen"],
body.ui-mode-other [data-component="load-screen"] {
    position: fixed !important;
    inset: 0 !important;
    z-index: 9400 !important;

    box-sizing: border-box !important;
    width: 100vw !important;
    height: 100vh !important;
    min-height: 100vh !important;
    padding: clamp(68px, 9vh, 102px) clamp(34px, 7.5vw, 144px) clamp(38px, 7vh, 78px) !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;

    border: 0 !important;
    background:
        radial-gradient(
            ellipse at 76% 18%,
            rgba(255, 255, 255, 0.055) 0%,
            rgba(166, 168, 174, 0.028) 28%,
            transparent 56%
        ),
        linear-gradient(
            112deg,
            rgba(3, 3, 4, 0.99) 0%,
            rgba(8, 8, 10, 0.97) 48%,
            rgba(15, 15, 18, 0.96) 76%,
            rgba(4, 4, 5, 0.99) 100%
        ) !important;
    box-shadow: none !important;

    color: rgba(236, 236, 239, 0.86) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    isolation: isolate !important;
}

/* 极淡的银幕曝光与上下构图线，只组织空间，不形成外框。 */
body.ui-mode-other save-screen::before,
body.ui-mode-other load-screen::before,
body.ui-mode-other [data-component="save-screen"]::before,
body.ui-mode-other [data-component="load-screen"]::before {
    content: "" !important;
    position: fixed !important;
    inset: 0 !important;
    z-index: -2 !important;
    pointer-events: none !important;

    background:
        linear-gradient(
            90deg,
            transparent 0%,
            rgba(255, 255, 255, 0.025) 24%,
            rgba(255, 255, 255, 0.06) 50%,
            rgba(255, 255, 255, 0.022) 76%,
            transparent 100%
        ) center 7vh / 84vw 1px no-repeat,
        linear-gradient(
            90deg,
            transparent 0%,
            rgba(255, 255, 255, 0.018) 28%,
            rgba(255, 255, 255, 0.045) 50%,
            rgba(255, 255, 255, 0.018) 72%,
            transparent 100%
        ) center calc(100vh - 7vh) / 84vw 1px no-repeat,
        radial-gradient(
            ellipse at 50% 35%,
            rgba(255, 255, 255, 0.035),
            transparent 53%
        ) !important;
}

body.ui-mode-other save-screen::after,
body.ui-mode-other load-screen::after,
body.ui-mode-other [data-component="save-screen"]::after,
body.ui-mode-other [data-component="load-screen"]::after {
    content: "" !important;
    position: fixed !important;
    inset: 0 !important;
    z-index: -1 !important;
    pointer-events: none !important;
    background: radial-gradient(
        ellipse at center,
        transparent 42%,
        rgba(0, 0, 0, 0.24) 76%,
        rgba(0, 0, 0, 0.52) 100%
    ) !important;
}

body.ui-mode-other save-screen.active,
body.ui-mode-other load-screen.active,
body.ui-mode-other [data-component="save-screen"].active,
body.ui-mode-other [data-component="load-screen"].active {
    animation: otherArchiveScreenReveal 430ms ease both !important;
}

/* 返回只保留熟悉的箭头符号，扩大实际点击区域但不显示按钮外形。 */
body.ui-mode-other save-screen > button[data-action="back"],
body.ui-mode-other load-screen > button[data-action="back"],
body.ui-mode-other [data-component="save-screen"] > button[data-action="back"],
body.ui-mode-other [data-component="load-screen"] > button[data-action="back"] {
    position: fixed !important;
    top: clamp(22px, 4vh, 42px) !important;
    left: clamp(24px, 4.5vw, 78px) !important;
    z-index: 3 !important;

    display: grid !important;
    place-items: center !important;
    width: 58px !important;
    height: 50px !important;
    margin: 0 !important;
    padding: 0 !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(232, 232, 235, 0.54) !important;
    font-size: 26px !important;
    line-height: 1 !important;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.88) !important;

    cursor: pointer !important;
    opacity: 0.86 !important;
    transform: none !important;
    transition:
        color 220ms ease,
        opacity 220ms ease,
        transform 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other save-screen > button[data-action="back"]::after,
body.ui-mode-other load-screen > button[data-action="back"]::after,
body.ui-mode-other [data-component="save-screen"] > button[data-action="back"]::after,
body.ui-mode-other [data-component="load-screen"] > button[data-action="back"]::after {
    content: "" !important;
    position: absolute !important;
    top: 50% !important;
    left: 42px !important;
    width: 0 !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.34), transparent) !important;
    opacity: 0 !important;
    transform: translateY(-50%) !important;
    transition:
        width 300ms cubic-bezier(0.16, 1, 0.3, 1),
        opacity 220ms ease !important;
}

body.ui-mode-other save-screen > button[data-action="back"]:hover,
body.ui-mode-other save-screen > button[data-action="back"]:focus-visible,
body.ui-mode-other load-screen > button[data-action="back"]:hover,
body.ui-mode-other load-screen > button[data-action="back"]:focus-visible,
body.ui-mode-other [data-component="save-screen"] > button[data-action="back"]:hover,
body.ui-mode-other [data-component="save-screen"] > button[data-action="back"]:focus-visible,
body.ui-mode-other [data-component="load-screen"] > button[data-action="back"]:hover,
body.ui-mode-other [data-component="load-screen"] > button[data-action="back"]:focus-visible {
    color: rgba(255, 255, 255, 0.98) !important;
    opacity: 1 !important;
    transform: translateX(-3px) !important;
}

body.ui-mode-other save-screen > button[data-action="back"]:hover::after,
body.ui-mode-other save-screen > button[data-action="back"]:focus-visible::after,
body.ui-mode-other load-screen > button[data-action="back"]:hover::after,
body.ui-mode-other load-screen > button[data-action="back"]:focus-visible::after,
body.ui-mode-other [data-component="save-screen"] > button[data-action="back"]:hover::after,
body.ui-mode-other [data-component="save-screen"] > button[data-action="back"]:focus-visible::after,
body.ui-mode-other [data-component="load-screen"] > button[data-action="back"]:hover::after,
body.ui-mode-other [data-component="load-screen"] > button[data-action="back"]:focus-visible::after {
    width: 38px !important;
    opacity: 1 !important;
}

/* Load 标题是小型电影章节标识，不使用巨型展示字。 */
body.ui-mode-other load-screen > h2,
body.ui-mode-other [data-component="load-screen"] > h2 {
    width: min(100%, 1280px) !important;
    margin: 0 auto clamp(24px, 4vh, 44px) !important;
    padding: 0 0 17px !important;

    border: 0 !important;
    background: transparent !important;

    color: rgba(248, 248, 249, 0.9) !important;
    font-size: clamp(24px, 1.8vw, 34px) !important;
    font-weight: 330 !important;
    line-height: 1.2 !important;
    letter-spacing: 0.18em !important;
    text-align: left !important;
    text-shadow: 0 4px 18px rgba(0, 0, 0, 0.76) !important;
}

body.ui-mode-other load-screen > h2::after,
body.ui-mode-other [data-component="load-screen"] > h2::after {
    content: "" !important;
    display: block !important;
    width: min(48vw, 560px) !important;
    height: 1px !important;
    margin-top: 16px !important;
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.27), transparent) !important;
}

/* Save 顶部输入区是一条编辑轴，不做输入框卡片。 */
body.ui-mode-other save-screen > .horizontal,
body.ui-mode-other [data-component="save-screen"] > .horizontal {
    position: relative !important;
    z-index: 1 !important;
    display: grid !important;
    grid-template-columns: minmax(180px, 1fr) auto !important;
    align-items: center !important;
    gap: clamp(18px, 2.5vw, 38px) !important;

    width: min(100%, 1280px) !important;
    margin: 0 auto clamp(34px, 5.5vh, 62px) !important;
    padding: 0 0 15px !important;

    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-other save-screen > .horizontal::before,
body.ui-mode-other [data-component="save-screen"] > .horizontal::before {
    content: attr(data-other-page-title) !important;
    position: absolute !important;
    top: 50% !important;
    left: 4px !important;
    z-index: 2 !important;
    display: block !important;
    pointer-events: none !important;

    color: rgba(248, 248, 249, 0.94) !important;
    font-size: clamp(25px, 1.8vw, 34px) !important;
    font-weight: 360 !important;
    line-height: 1.2 !important;
    letter-spacing: 0.12em !important;
    text-shadow: 0 4px 18px rgba(0, 0, 0, 0.76) !important;
    transform: translateY(-50%) !important;
}

body.ui-mode-other save-screen > .horizontal::after,
body.ui-mode-other [data-component="save-screen"] > .horizontal::after {
    content: "" !important;
    position: absolute !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 0 !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(
        90deg,
        rgba(255, 255, 255, 0.24),
        rgba(255, 255, 255, 0.07) 52%,
        transparent 92%
    ) !important;
}

body.ui-mode-other save-screen input[data-content="slot-name"],
body.ui-mode-other [data-component="save-screen"] input[data-content="slot-name"] {
    width: 100% !important;
    min-width: 0 !important;
    height: 46px !important;
    margin: 0 !important;
    padding: 0 4px !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: transparent !important;
    caret-color: transparent !important;
    font-family: inherit !important;
    font-size: clamp(19px, 1.45vw, 24px) !important;
    font-weight: 360 !important;
    line-height: 46px !important;
    letter-spacing: 0.035em !important;
    text-align: left !important;
    text-shadow: none !important;
    pointer-events: none !important;
    user-select: none !important;
}

body.ui-mode-other save-screen input[data-content="slot-name"]::placeholder,
body.ui-mode-other [data-component="save-screen"] input[data-content="slot-name"]::placeholder {
    color: rgba(224, 224, 228, 0.35) !important;
    opacity: 1 !important;
}

body.ui-mode-other save-screen button[data-action="save"],
body.ui-mode-other [data-component="save-screen"] button[data-action="save"] {
    position: relative !important;
    width: auto !important;
    min-width: 138px !important;
    height: 46px !important;
    margin: 0 !important;
    padding: 0 20px 0 48px !important;

    border: 1px solid rgba(255, 255, 255, 0.32) !important;
    border-radius: 2px !important;
    outline: none !important;
    background: linear-gradient(135deg, rgba(255, 255, 255, 0.14), rgba(255, 255, 255, 0.055)) !important;
    box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.06),
        0 8px 22px rgba(0, 0, 0, 0.18) !important;

    color: rgba(248, 248, 249, 0.94) !important;
    font-family: inherit !important;
    font-size: 14px !important;
    font-weight: 520 !important;
    line-height: 44px !important;
    letter-spacing: 0.08em !important;
    text-align: center !important;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.9) !important;

    cursor: pointer !important;
    transform: none !important;
    transition:
        background 240ms ease,
        border-color 240ms ease,
        box-shadow 280ms ease,
        color 220ms ease,
        transform 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other save-screen button[data-action="save"]::before,
body.ui-mode-other [data-component="save-screen"] button[data-action="save"]::before {
    content: "" !important;
    position: absolute !important;
    top: 50% !important;
    left: 18px !important;
    width: 16px !important;
    height: 16px !important;
    background:
        linear-gradient(currentColor, currentColor) center / 11px 1px no-repeat,
        linear-gradient(currentColor, currentColor) center / 1px 11px no-repeat !important;
    opacity: 0.86 !important;
    transform: translateY(-50%) !important;
    transition:
        opacity 220ms ease,
        transform 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other save-screen button[data-action="save"]:hover,
body.ui-mode-other save-screen button[data-action="save"]:focus-visible,
body.ui-mode-other [data-component="save-screen"] button[data-action="save"]:hover,
body.ui-mode-other [data-component="save-screen"] button[data-action="save"]:focus-visible {
    border-color: rgba(255, 255, 255, 0.82) !important;
    background: rgba(246, 246, 247, 0.94) !important;
    box-shadow:
        0 12px 30px rgba(0, 0, 0, 0.28),
        0 0 24px rgba(255, 255, 255, 0.08) !important;
    color: rgba(16, 16, 18, 0.96) !important;
    text-shadow: none !important;
    transform: translateY(-2px) !important;
}

body.ui-mode-other save-screen button[data-action="save"]:hover::before,
body.ui-mode-other save-screen button[data-action="save"]:focus-visible::before,
body.ui-mode-other [data-component="save-screen"] button[data-action="save"]:hover::before,
body.ui-mode-other [data-component="save-screen"] button[data-action="save"]:focus-visible::before {
    opacity: 1 !important;
    transform: translateY(-50%) rotate(90deg) !important;
}

body.ui-mode-other save-screen button[data-action="save"]:active,
body.ui-mode-other [data-component="save-screen"] button[data-action="save"]:active {
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.2) !important;
    transform: translateY(0) scale(0.985) !important;
    transition-duration: 70ms !important;
}

/* Load 的普通与自动存档分区保持开放式布局，只用标题和渐隐线分节。 */
body.ui-mode-other load-screen > [data-ui="saveSlots"],
body.ui-mode-other load-screen > [data-ui="autoSaveSlots"],
body.ui-mode-other [data-component="load-screen"] > [data-ui="saveSlots"],
body.ui-mode-other [data-component="load-screen"] > [data-ui="autoSaveSlots"] {
    width: min(100%, 1280px) !important;
    margin: 0 auto clamp(38px, 6vh, 68px) !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-other load-screen h3,
body.ui-mode-other [data-component="load-screen"] h3 {
    position: relative !important;
    margin: 0 0 clamp(17px, 2.5vh, 28px) !important;
    padding: 0 0 12px !important;

    border: 0 !important;
    background: transparent !important;

    color: rgba(225, 225, 229, 0.48) !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    line-height: 1.3 !important;
    letter-spacing: 0.16em !important;
    text-align: left !important;
    text-transform: uppercase !important;
}

body.ui-mode-other load-screen h3::after,
body.ui-mode-other [data-component="load-screen"] h3::after {
    content: "" !important;
    position: absolute !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 0 !important;
    height: 1px !important;
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.15), transparent 72%) !important;
}

/* 槽位列表按胶片目录排列；页面自身没有包围列表的大框。 */
body.ui-mode-other save-screen > [data-ui="slots"],
body.ui-mode-other load-screen [data-ui="slots"],
body.ui-mode-other [data-component="save-screen"] > [data-ui="slots"],
body.ui-mode-other [data-component="load-screen"] [data-ui="slots"] {
    display: block !important;
    width: min(100%, 1480px) !important;
    margin: 0 auto !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-other slot-container,
body.ui-mode-other [data-component="slot-container"] {
    display: grid !important;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 430px)) !important;
    align-items: start !important;
    justify-content: start !important;
    gap: clamp(38px, 5vw, 72px) clamp(28px, 3.3vw, 54px) !important;

    width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

/* 空状态是画面中的一行安静文字，不是占位卡片。 */
body.ui-mode-other slot-container > p,
body.ui-mode-other [data-component="slot-container"] > p {
    grid-column: 1 / -1 !important;
    min-height: clamp(150px, 30vh, 300px) !important;
    margin: 0 !important;
    padding: clamp(58px, 12vh, 120px) 16px 20px !important;

    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(226, 226, 230, 0.32) !important;
    font-size: clamp(15px, 1.1vw, 18px) !important;
    font-weight: 360 !important;
    line-height: 1.7 !important;
    letter-spacing: 0.08em !important;
    text-align: center !important;
    text-shadow: 0 3px 12px rgba(0, 0, 0, 0.75) !important;
}

/* 单个存档是允许被独立查看的重复项目，但不做厚重卡片。 */
body.ui-mode-other save-slot,
body.ui-mode-other [data-component="save-slot"] {
    position: relative !important;
    display: grid !important;
    grid-template-rows: auto auto auto !important;
    align-content: start !important;
    width: 100% !important;
    max-width: 430px !important;
    min-width: 0 !important;
    min-height: 0 !important;
    height: auto !important;
    margin: 0 !important;
    padding: 0 0 26px !important;
    overflow: visible !important;

    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(236, 236, 239, 0.76) !important;
    cursor: pointer !important;
    isolation: isolate !important;
    justify-self: center !important;
    transform: translateZ(0) !important;
    transition:
        color 220ms ease,
        transform 380ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

/* 光晕由图片本身向四周扩散，不使用会形成中央白圈的径向光层。 */
body.ui-mode-other save-slot::before,
body.ui-mode-other [data-component="save-slot"]::before {
    content: none !important;
    display: none !important;
}

body.ui-mode-other save-slot::after,
body.ui-mode-other [data-component="save-slot"]::after {
    content: "" !important;
    position: absolute !important;
    right: auto !important;
    bottom: 0 !important;
    left: 50% !important;
    width: 100% !important;
    height: 1px !important;
    pointer-events: none !important;
    background: rgba(255, 255, 255, 0.72) !important;
    opacity: 0 !important;
    transform: translateX(-50%) scaleX(0.18) !important;
    transform-origin: center !important;
    transition:
        opacity 200ms ease,
        transform 360ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other save-slot [data-content="background"],
body.ui-mode-other [data-component="save-slot"] [data-content="background"] {
    position: relative !important;
    grid-row: 1 !important;
    width: calc(100% + clamp(18px, 1.5vw, 32px)) !important;
    max-width: none !important;
    justify-self: center !important;
    height: auto !important;
    aspect-ratio: 16 / 9 !important;
    overflow: hidden !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: 1px solid rgba(255, 255, 255, 0.075) !important;
    outline-offset: -1px !important;
    background-color: rgba(20, 20, 23, 0.76) !important;
    background-position: center !important;
    background-repeat: no-repeat !important;
    background-size: cover !important;
    box-shadow: 0 16px 34px rgba(0, 0, 0, 0.22) !important;

    filter: grayscale(0.62) brightness(0.8) saturate(0.52) contrast(1.07) !important;
    transform: translateZ(0) !important;
    transition:
        filter 420ms ease,
        outline-color 260ms ease,
        box-shadow 420ms ease,
        transform 480ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other save-slot [data-content="background"]::before,
body.ui-mode-other [data-component="save-slot"] [data-content="background"]::before {
    content: "" !important;
    position: absolute !important;
    inset: 0 !important;
    z-index: 1 !important;
    pointer-events: none !important;
    background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.065), transparent 24%, transparent 72%, rgba(0, 0, 0, 0.24)),
        linear-gradient(90deg, rgba(255, 255, 255, 0.035), transparent 24%, transparent 78%, rgba(0, 0, 0, 0.16)) !important;
    opacity: 0.72 !important;
    transition: opacity 360ms ease !important;
}

body.ui-mode-other save-slot [data-content="background"]::after,
body.ui-mode-other [data-component="save-slot"] [data-content="background"]::after {
    content: "" !important;
    position: absolute !important;
    z-index: 2 !important;
    top: -24% !important;
    bottom: -24% !important;
    left: -48% !important;
    width: 34% !important;
    pointer-events: none !important;
    background: linear-gradient(
        90deg,
        transparent,
        rgba(255, 255, 255, 0.025) 24%,
        rgba(255, 255, 255, 0.14) 52%,
        rgba(255, 255, 255, 0.03) 76%,
        transparent
    ) !important;
    filter: blur(2px) !important;
    opacity: 0 !important;
    transform: skewX(-14deg) translateX(-180%) !important;
}

/* 信息移出画面，形成缩略图、标题、时间三层清晰节奏。 */
body.ui-mode-other save-slot .badge,
body.ui-mode-other [data-component="save-slot"] .badge {
    position: relative !important;
    z-index: 1 !important;
    grid-row: 2 !important;
    display: -webkit-box !important;
    box-sizing: border-box !important;
    width: 100% !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: clamp(18px, 1.45vw, 23px) 20px 0 !important;
    overflow: hidden !important;

    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(241, 241, 244, 0.88) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    font-size: clamp(17px, 1.05vw, 20px) !important;
    font-style: normal !important;
    font-weight: 420 !important;
    font-feature-settings: "kern" 1 !important;
    line-height: 1.42 !important;
    letter-spacing: 0.045em !important;
    text-align: center !important;
    text-shadow: 0 3px 14px rgba(0, 0, 0, 0.78) !important;
    white-space: normal !important;
    -webkit-box-orient: vertical !important;
    -webkit-line-clamp: 2 !important;
    transition:
        color 260ms ease,
        text-shadow 360ms ease,
        transform 380ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other save-slot figcaption,
body.ui-mode-other [data-component="save-slot"] figcaption {
    grid-row: 3 !important;
    margin: 0 !important;
    padding: 7px 20px 0 !important;

    border: 0 !important;
    background: transparent !important;

    color: rgba(218, 218, 223, 0.46) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    font-size: clamp(11px, 0.72vw, 12px) !important;
    font-style: normal !important;
    font-weight: 410 !important;
    font-feature-settings: "kern" 1, "tnum" 1 !important;
    font-variant-numeric: tabular-nums !important;
    line-height: 1.5 !important;
    letter-spacing: 0.075em !important;
    text-align: center !important;
    text-transform: none !important;
    transition:
        color 260ms ease,
        letter-spacing 360ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

/* 删除是熟悉的 X 图标，无圆形或方形底；悬浮槽位时再提高可见度。 */
body.ui-mode-other save-slot [data-delete],
body.ui-mode-other [data-component="save-slot"] [data-delete] {
    position: absolute !important;
    z-index: 4 !important;
    top: 5px !important;
    right: -10px !important;

    display: grid !important;
    place-items: center !important;
    width: 35px !important;
    height: 35px !important;
    margin: 0 !important;
    padding: 0 !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(246, 246, 248, 0.34) !important;
    font-size: 15px !important;
    line-height: 1 !important;
    text-shadow: 0 2px 7px rgba(0, 0, 0, 0.98) !important;

    cursor: pointer !important;
    opacity: 0.5 !important;
    transform: none !important;
    transition:
        color 220ms ease,
        opacity 220ms ease,
        transform 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other save-slot:hover,
body.ui-mode-other save-slot:focus-within,
body.ui-mode-other [data-component="save-slot"]:hover,
body.ui-mode-other [data-component="save-slot"]:focus-within {
    color: rgba(255, 255, 255, 0.96) !important;
    transform: translateY(-5px) !important;
}

body.ui-mode-other save-slot:hover::before,
body.ui-mode-other save-slot:focus-within::before,
body.ui-mode-other [data-component="save-slot"]:hover::before,
body.ui-mode-other [data-component="save-slot"]:focus-within::before {
    content: none !important;
    display: none !important;
}

body.ui-mode-other save-slot:hover::after,
body.ui-mode-other save-slot:focus-within::after,
body.ui-mode-other [data-component="save-slot"]:hover::after,
body.ui-mode-other [data-component="save-slot"]:focus-within::after {
    opacity: 0.82 !important;
    transform: translateX(-50%) scaleX(1) !important;
}

body.ui-mode-other save-slot:active::after,
body.ui-mode-other [data-component="save-slot"]:active::after {
    opacity: 1 !important;
    transform: translateX(-50%) scaleX(1) !important;
    transition-duration: 60ms !important;
}

body.ui-mode-other save-slot:hover [data-content="background"],
body.ui-mode-other save-slot:focus-within [data-content="background"],
body.ui-mode-other [data-component="save-slot"]:hover [data-content="background"],
body.ui-mode-other [data-component="save-slot"]:focus-within [data-content="background"] {
    outline-color: rgba(255, 255, 255, 0.14) !important;
    box-shadow:
        0 18px 40px rgba(0, 0, 0, 0.32),
        0 0 10px rgba(255, 255, 255, 0.105),
        0 0 24px rgba(246, 247, 249, 0.068),
        0 0 48px rgba(224, 226, 232, 0.036) !important;
    filter: grayscale(0.12) brightness(0.95) saturate(0.88) contrast(1.045) !important;
    transform: scale(1.01) !important;
}

body.ui-mode-other save-slot:hover [data-content="background"]::before,
body.ui-mode-other save-slot:focus-within [data-content="background"]::before,
body.ui-mode-other [data-component="save-slot"]:hover [data-content="background"]::before,
body.ui-mode-other [data-component="save-slot"]:focus-within [data-content="background"]::before {
    opacity: 0.4 !important;
}

body.ui-mode-other save-slot:hover [data-content="background"]::after,
body.ui-mode-other save-slot:focus-within [data-content="background"]::after,
body.ui-mode-other [data-component="save-slot"]:hover [data-content="background"]::after,
body.ui-mode-other [data-component="save-slot"]:focus-within [data-content="background"]::after {
    animation: otherSaveSlotSweep 860ms cubic-bezier(0.2, 0.72, 0.2, 1) both !important;
}

body.ui-mode-other save-slot:hover .badge,
body.ui-mode-other save-slot:focus-within .badge,
body.ui-mode-other [data-component="save-slot"]:hover .badge,
body.ui-mode-other [data-component="save-slot"]:focus-within .badge {
    color: rgba(255, 255, 255, 0.98) !important;
    text-shadow:
        0 2px 9px rgba(255, 255, 255, 0.12),
        0 5px 18px rgba(0, 0, 0, 0.82) !important;
    transform: none !important;
}

body.ui-mode-other save-slot:hover figcaption,
body.ui-mode-other save-slot:focus-within figcaption,
body.ui-mode-other [data-component="save-slot"]:hover figcaption,
body.ui-mode-other [data-component="save-slot"]:focus-within figcaption {
    color: rgba(239, 239, 242, 0.72) !important;
    letter-spacing: 0.09em !important;
}

body.ui-mode-other save-slot:hover [data-delete],
body.ui-mode-other save-slot:focus-within [data-delete],
body.ui-mode-other [data-component="save-slot"]:hover [data-delete],
body.ui-mode-other [data-component="save-slot"]:focus-within [data-delete] {
    opacity: 0.78 !important;
}

body.ui-mode-other save-slot:active,
body.ui-mode-other [data-component="save-slot"]:active {
    transform: translateY(-3px) scale(0.988) !important;
    transition-duration: 80ms !important;
}

body.ui-mode-other save-slot:active [data-content="background"],
body.ui-mode-other [data-component="save-slot"]:active [data-content="background"] {
    filter: grayscale(0.9) brightness(1.16) saturate(0.42) contrast(1.09) !important;
    transition-duration: 80ms !important;
}

body.ui-mode-other save-slot [data-delete]:hover,
body.ui-mode-other save-slot [data-delete]:focus-visible,
body.ui-mode-other [data-component="save-slot"] [data-delete]:hover,
body.ui-mode-other [data-component="save-slot"] [data-delete]:focus-visible {
    color: rgba(255, 255, 255, 0.98) !important;
    opacity: 1 !important;
    transform: scale(1.08) !important;
}

/* ==================== Dialog Log 电影字幕记录 ==================== */

body.ui-mode-other dialog-log {
    position: fixed !important;
    inset: 0 !important;
    z-index: 9650 !important;

    align-items: center !important;
    justify-content: center !important;
    width: 100vw !important;
    height: 100vh !important;
    margin: 0 !important;
    padding: 0 !important;

    border: 0 !important;
    background:
        radial-gradient(
            ellipse at 62% 36%,
            rgba(255, 255, 255, 0.045),
            transparent 45%
        ),
        linear-gradient(
            96deg,
            rgba(2, 2, 3, 0.97) 0%,
            rgba(7, 7, 9, 0.94) 48%,
            rgba(3, 3, 4, 0.98) 100%
        ) !important;
    box-shadow: none !important;
    backdrop-filter: blur(13px) brightness(0.66) saturate(0.62) !important;
    -webkit-backdrop-filter: blur(13px) brightness(0.66) saturate(0.62) !important;

    color: rgba(232, 232, 235, 0.84) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
}

body.ui-mode-other dialog-log.modal--active {
    animation: otherDialogLogReveal 400ms ease both !important;
}

/* 内容区完全透明，让记录直接存在于暗场上。 */
body.ui-mode-other dialog-log .modal__content {
    position: relative !important;
    top: auto !important;
    right: auto !important;
    bottom: auto !important;
    left: auto !important;
    display: grid !important;
    grid-template-rows: minmax(0, 1fr) auto !important;
    gap: 12px !important;

    width: min(94vw, 1600px) !important;
    height: min(88vh, 960px) !important;
    max-width: none !important;
    max-height: none !important;
    margin: 0 !important;
    padding: clamp(24px, 4vh, 42px) 0 0 !important;
    overflow: visible !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    transform: none !important;
}

/* 顶部只有一条两端渐隐的电影构图线。 */
body.ui-mode-other dialog-log .modal__content::before {
    content: "" !important;
    position: absolute !important;
    top: 0 !important;
    right: 0 !important;
    left: 0 !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(
        90deg,
        transparent,
        rgba(255, 255, 255, 0.12) 14%,
        rgba(255, 255, 255, 0.25) 50%,
        rgba(255, 255, 255, 0.12) 86%,
        transparent
    ) !important;
}

body.ui-mode-other dialog-log [data-content="log"] {
    width: 100% !important;
    height: 100% !important;
    max-height: none !important;
    margin: 0 !important;
    padding: clamp(10px, 2vh, 22px) clamp(6px, 1.2vw, 20px) clamp(30px, 5vh, 58px) !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;

    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    text-align: left !important;

    -webkit-mask-image: linear-gradient(
        180deg,
        transparent 0%,
        #000 4%,
        #000 92%,
        transparent 100%
    ) !important;
    mask-image: linear-gradient(
        180deg,
        transparent 0%,
        #000 4%,
        #000 92%,
        transparent 100%
    ) !important;
}

body.ui-mode-other dialog-log [data-content="log"] [data-spoke] {
    position: relative !important;
    display: grid !important;
    grid-template-columns: clamp(92px, 10vw, 156px) minmax(0, 1fr) !important;
    align-items: baseline !important;
    column-gap: clamp(18px, 2vw, 36px) !important;

    box-sizing: border-box !important;
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100% !important;
    margin: 0 !important;
    padding: clamp(18px, 2.6vh, 30px) 0 !important;

    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-other dialog-log [data-content="log"] [data-spoke]::after {
    content: "" !important;
    position: absolute !important;
    right: 0 !important;
    bottom: 0 !important;
    left: calc(clamp(92px, 10vw, 156px) + clamp(18px, 2vw, 36px)) !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.11), transparent 86%) !important;
}

body.ui-mode-other dialog-log [data-content="log"] [data-spoke] > span {
    grid-column: 1 !important;
    margin: 0 !important;
    padding: 3px 0 0 !important;

    color: rgba(232, 232, 235, 0.62) !important;
    font-family: inherit !important;
    font-size: clamp(14px, 1.05vw, 17px) !important;
    font-weight: 520 !important;
    line-height: 1.5 !important;
    letter-spacing: 0.08em !important;
    text-align: right !important;
    text-shadow: 0 2px 7px rgba(0, 0, 0, 0.88) !important;
}

body.ui-mode-other dialog-log [data-content="log"] [data-spoke] > p {
    grid-column: 2 !important;
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;

    color: rgba(239, 239, 241, 0.82) !important;
    font-family: inherit !important;
    font-size: clamp(16px, 1.2vw, 20px) !important;
    font-weight: 350 !important;
    line-height: 1.8 !important;
    letter-spacing: 0.025em !important;
    text-align: left !important;
    text-shadow:
        0 2px 7px rgba(0, 0, 0, 0.94),
        0 7px 22px rgba(0, 0, 0, 0.46) !important;
    white-space: normal !important;
    word-break: normal !important;
    overflow-wrap: anywhere !important;
}

/* 旁白沿正文阅读轴排版，以稍弱色阶区分，不改变整体高度节奏。 */
body.ui-mode-other dialog-log [data-content="log"] [data-spoke="narrator"],
body.ui-mode-other dialog-log [data-content="log"] [data-spoke="centered"] {
    grid-template-columns: clamp(92px, 10vw, 156px) minmax(0, 1fr) !important;
}

body.ui-mode-other dialog-log [data-content="log"] [data-spoke="narrator"] > p,
body.ui-mode-other dialog-log [data-content="log"] [data-spoke="centered"] > p {
    grid-column: 2 !important;
    color: rgba(218, 218, 222, 0.62) !important;
    font-style: italic !important;
}

body.ui-mode-other dialog-log [data-content="log"] [data-spoke="centered"] > p {
    text-align: center !important;
}

body.ui-mode-other dialog-log [data-content="placeholder"] {
    display: grid !important;
    place-items: center !important;
    min-height: 56vh !important;
    padding: 20px !important;
    color: rgba(224, 224, 228, 0.32) !important;
    font-size: 14px !important;
    font-weight: 360 !important;
    letter-spacing: 0.075em !important;
    text-align: center !important;
}

/* 关闭是底部纯文字动作，不使用按钮框。 */
body.ui-mode-other dialog-log .modal__content > button[data-action="dialog-log"] {
    position: relative !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: flex-end !important;
    gap: 11px !important;
    justify-self: end !important;
    width: auto !important;
    min-width: 0 !important;
    min-height: 48px !important;
    height: 48px !important;
    margin: 0 !important;
    padding: 0 4px !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(228, 228, 232, 0.52) !important;
    font-family: inherit !important;
    font-size: 17px !important;
    font-weight: 500 !important;
    line-height: 1 !important;
    letter-spacing: 0.16em !important;
    text-align: right !important;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.9) !important;

    cursor: pointer !important;
    transform: none !important;
    transition:
        color 220ms ease,
        letter-spacing 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other dialog-log .modal__content > button[data-action="dialog-log"]::before {
    content: "" !important;
    position: static !important;
    display: block !important;
    flex: 0 0 auto !important;
    width: 22px !important;
    height: 1px !important;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.4)) !important;
    transform: none !important;
    transition: width 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other dialog-log .modal__content > button[data-action="dialog-log"]:hover,
body.ui-mode-other dialog-log .modal__content > button[data-action="dialog-log"]:focus-visible {
    color: rgba(255, 255, 255, 0.98) !important;
    letter-spacing: 0.18em !important;
}

body.ui-mode-other dialog-log .modal__content > button[data-action="dialog-log"]:hover::before,
body.ui-mode-other dialog-log .modal__content > button[data-action="dialog-log"]:focus-visible::before {
    width: 27px !important;
}

body.ui-mode-other save-screen::-webkit-scrollbar,
body.ui-mode-other load-screen::-webkit-scrollbar,
body.ui-mode-other dialog-log [data-content="log"]::-webkit-scrollbar {
    width: 3px !important;
}

body.ui-mode-other save-screen::-webkit-scrollbar-track,
body.ui-mode-other load-screen::-webkit-scrollbar-track,
body.ui-mode-other dialog-log [data-content="log"]::-webkit-scrollbar-track {
    background: transparent !important;
}

body.ui-mode-other save-screen::-webkit-scrollbar-thumb,
body.ui-mode-other load-screen::-webkit-scrollbar-thumb,
body.ui-mode-other dialog-log [data-content="log"]::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.15) !important;
}

@keyframes otherArchiveScreenReveal {
    from {
        opacity: 0;
        filter: brightness(1.12);
    }

    to {
        opacity: 1;
        filter: brightness(1);
    }
}

@keyframes otherSaveSlotSweep {
    0% {
        opacity: 0;
        transform: skewX(-14deg) translateX(-180%);
    }

    18% {
        opacity: 0.78;
    }

    100% {
        opacity: 0;
        transform: skewX(-14deg) translateX(720%);
    }
}

@keyframes otherDialogLogReveal {
    from {
        opacity: 0;
        backdrop-filter: blur(4px) brightness(0.82) saturate(0.7);
    }

    to {
        opacity: 1;
        backdrop-filter: blur(13px) brightness(0.66) saturate(0.62);
    }
}

/* ==================== Save / Load / Log 响应式 ==================== */

@media (min-width: 1800px) {
    body.ui-mode-other save-screen,
    body.ui-mode-other load-screen,
    body.ui-mode-other [data-component="save-screen"],
    body.ui-mode-other [data-component="load-screen"] {
        padding-top: clamp(96px, 10vh, 150px) !important;
        padding-right: clamp(150px, 11vw, 260px) !important;
        padding-left: clamp(150px, 11vw, 260px) !important;
    }

    body.ui-mode-other save-screen > .horizontal,
    body.ui-mode-other [data-component="save-screen"] > .horizontal {
        width: min(100%, 1480px) !important;
        margin-bottom: clamp(64px, 7vh, 96px) !important;
    }

    body.ui-mode-other load-screen > h2,
    body.ui-mode-other [data-component="load-screen"] > h2,
    body.ui-mode-other load-screen > [data-ui="saveSlots"],
    body.ui-mode-other load-screen > [data-ui="autoSaveSlots"],
    body.ui-mode-other [data-component="load-screen"] > [data-ui="saveSlots"],
    body.ui-mode-other [data-component="load-screen"] > [data-ui="autoSaveSlots"] {
        width: min(100%, 1480px) !important;
    }
}

@media (max-width: 900px) {
    body.ui-mode-other save-screen,
    body.ui-mode-other load-screen,
    body.ui-mode-other [data-component="save-screen"],
    body.ui-mode-other [data-component="load-screen"] {
        padding: 70px 30px 44px !important;
    }

    body.ui-mode-other slot-container,
    body.ui-mode-other [data-component="slot-container"] {
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
        gap: 42px 28px !important;
    }

    body.ui-mode-other dialog-log .modal__content {
        width: 91vw !important;
        height: 86vh !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke] {
        grid-template-columns: 120px minmax(0, 1fr) !important;
        column-gap: 26px !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke]::after {
        left: 146px !important;
    }
}

@media (max-width: 560px) and (orientation: portrait) {
    body.ui-mode-other save-screen,
    body.ui-mode-other load-screen,
    body.ui-mode-other [data-component="save-screen"],
    body.ui-mode-other [data-component="load-screen"] {
        padding: 64px 20px 34px !important;
    }

    body.ui-mode-other save-screen > .horizontal,
    body.ui-mode-other [data-component="save-screen"] > .horizontal {
        grid-template-columns: minmax(0, 1fr) auto !important;
        gap: 12px !important;
        margin-bottom: 34px !important;
    }

    body.ui-mode-other save-screen input[data-content="slot-name"],
    body.ui-mode-other [data-component="save-screen"] input[data-content="slot-name"] {
        font-size: 17px !important;
    }

    body.ui-mode-other save-screen button[data-action="save"],
    body.ui-mode-other [data-component="save-screen"] button[data-action="save"] {
        min-width: 112px !important;
        padding-right: 14px !important;
        padding-left: 38px !important;
        font-size: 12px !important;
    }

    body.ui-mode-other save-screen button[data-action="save"]::before,
    body.ui-mode-other [data-component="save-screen"] button[data-action="save"]::before {
        left: 14px !important;
    }

    body.ui-mode-other slot-container,
    body.ui-mode-other [data-component="slot-container"] {
        grid-template-columns: 1fr !important;
        gap: 40px !important;
    }

    body.ui-mode-other dialog-log .modal__content {
        width: 92vw !important;
        height: 88vh !important;
        padding-top: 22px !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] {
        padding-right: 8px !important;
        padding-left: 8px !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke],
    body.ui-mode-other dialog-log [data-content="log"] [data-spoke="narrator"],
    body.ui-mode-other dialog-log [data-content="log"] [data-spoke="centered"] {
        display: block !important;
        padding: 20px 0 22px !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke]::after {
        left: 0 !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke] > span {
        display: block !important;
        margin-bottom: 8px !important;
        padding: 0 !important;
        font-size: 13px !important;
        text-align: left !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke] > p,
    body.ui-mode-other dialog-log [data-content="log"] [data-spoke="narrator"] > p,
    body.ui-mode-other dialog-log [data-content="log"] [data-spoke="centered"] > p {
        display: block !important;
        font-size: 16px !important;
        line-height: 1.7 !important;
    }
}

@media (orientation: landscape) and (max-height: 600px) {
    body.ui-mode-other save-screen,
    body.ui-mode-other load-screen,
    body.ui-mode-other [data-component="save-screen"],
    body.ui-mode-other [data-component="load-screen"] {
        padding: 44px 7vw 30px !important;
    }

    body.ui-mode-other save-screen > button[data-action="back"],
    body.ui-mode-other load-screen > button[data-action="back"],
    body.ui-mode-other [data-component="save-screen"] > button[data-action="back"],
    body.ui-mode-other [data-component="load-screen"] > button[data-action="back"] {
        top: 7px !important;
        left: 18px !important;
    }

    body.ui-mode-other load-screen > h2,
    body.ui-mode-other [data-component="load-screen"] > h2 {
        margin-bottom: 18px !important;
        padding-bottom: 10px !important;
        font-size: clamp(17px, 4vh, 22px) !important;
    }

    body.ui-mode-other load-screen > h2::after,
    body.ui-mode-other [data-component="load-screen"] > h2::after {
        margin-top: 10px !important;
    }

    body.ui-mode-other save-screen > .horizontal,
    body.ui-mode-other [data-component="save-screen"] > .horizontal {
        height: 42px !important;
        margin-bottom: 22px !important;
        padding-bottom: 8px !important;
    }

    body.ui-mode-other save-screen input[data-content="slot-name"],
    body.ui-mode-other [data-component="save-screen"] input[data-content="slot-name"] {
        height: 38px !important;
        font-size: 14px !important;
        line-height: 38px !important;
    }

    body.ui-mode-other load-screen h3,
    body.ui-mode-other [data-component="load-screen"] h3 {
        margin-bottom: 12px !important;
        padding-bottom: 7px !important;
    }

    body.ui-mode-other slot-container,
    body.ui-mode-other [data-component="slot-container"] {
        grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
        gap: 22px !important;
    }

    body.ui-mode-other save-slot .badge,
    body.ui-mode-other [data-component="save-slot"] .badge {
        min-height: 2.6em !important;
        padding-top: 10px !important;
        font-size: 15px !important;
    }

    body.ui-mode-other save-slot figcaption,
    body.ui-mode-other [data-component="save-slot"] figcaption {
        padding-top: 3px !important;
        font-size: 11px !important;
    }

    body.ui-mode-other slot-container > p,
    body.ui-mode-other [data-component="slot-container"] > p {
        min-height: 40vh !important;
        padding-top: 15vh !important;
    }

    body.ui-mode-other dialog-log .modal__content {
        width: 88vw !important;
        height: 86vh !important;
        gap: 8px !important;
        padding-top: 18px !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] {
        padding-top: 5px !important;
        padding-bottom: 30px !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke] {
        padding: 12px 0 !important;
    }

    body.ui-mode-other dialog-log [data-content="log"] [data-spoke] > p {
        font-size: clamp(14px, 3.2vh, 17px) !important;
        line-height: 1.6 !important;
    }

    body.ui-mode-other dialog-log .modal__content > button[data-action="dialog-log"] {
        height: 34px !important;
        line-height: 34px !important;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other save-screen.active,
    body.ui-mode-other load-screen.active,
    body.ui-mode-other dialog-log.modal--active,
    body.ui-mode-other save-slot,
    body.ui-mode-other save-slot::before,
    body.ui-mode-other save-slot::after,
    body.ui-mode-other save-slot [data-content="background"],
    body.ui-mode-other save-slot [data-content="background"]::before,
    body.ui-mode-other save-slot [data-content="background"]::after,
    body.ui-mode-other save-slot .badge,
    body.ui-mode-other save-slot figcaption,
    body.ui-mode-other save-slot [data-delete],
    body.ui-mode-other [data-component="save-slot"],
    body.ui-mode-other [data-component="save-slot"]::before,
    body.ui-mode-other [data-component="save-slot"]::after,
    body.ui-mode-other [data-component="save-slot"] [data-content="background"],
    body.ui-mode-other [data-component="save-slot"] [data-content="background"]::before,
    body.ui-mode-other [data-component="save-slot"] [data-content="background"]::after,
    body.ui-mode-other [data-component="save-slot"] .badge,
    body.ui-mode-other [data-component="save-slot"] figcaption,
    body.ui-mode-other [data-component="save-slot"] [data-delete] {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}

/* ==================== Settings 电影感环境设定 ==================== */

/*
 * 非对称电影构图：左侧是设置动线，右侧保留当前章节画面。
 * --other-settings-cover 由设置页公共初始化逻辑写入当前场景图片。
 */
body.ui-mode-other settings-screen,
body.ui-mode-other [data-component="settings-screen"] {
    position: fixed !important;
    isolation: isolate !important;
    inset: 0 !important;

    width: 100vw !important;
    height: 100vh !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;

    color: var(--other-text) !important;
    background:
        radial-gradient(
            ellipse at 79% 42%,
            rgba(205, 211, 220, 0.105) 0%,
            rgba(125, 132, 143, 0.042) 24%,
            transparent 54%
        ),
        radial-gradient(
            ellipse at 18% 112%,
            rgba(255, 255, 255, 0.052) 0%,
            transparent 48%
        ),
        repeating-linear-gradient(
            118deg,
            rgba(255, 255, 255, 0.007) 0 1px,
            transparent 1px 8px
        ),
        #050609 !important;

    animation: otherSettingsEnter 360ms ease both !important;
}

/* 右侧章节封面；没有封面变量时显示低调的中性环境光。 */
body.ui-mode-other settings-screen::before,
body.ui-mode-other [data-component="settings-screen"]::before {
    content: "";
    position: fixed;
    z-index: -2;
    top: 0;
    right: 0;
    bottom: 0;

    width: 66%;
    pointer-events: none;

    background-image:
        linear-gradient(
            112deg,
            transparent 10%,
            rgba(255, 255, 255, 0.018) 36%,
            rgba(230, 234, 240, 0.075) 50%,
            rgba(255, 255, 255, 0.015) 64%,
            transparent 76%
        ),
        radial-gradient(
            ellipse at 72% 42%,
            rgba(238, 241, 245, 0.14) 0%,
            rgba(164, 171, 181, 0.052) 28%,
            transparent 62%
        ),
        linear-gradient(
            180deg,
            rgba(0, 0, 0, 0.02),
            rgba(0, 0, 0, 0.22)
        ),
        var(--other-settings-cover, none);
    background-position: center center;
    background-size: cover;
    background-repeat: no-repeat;

    background-blend-mode: screen, soft-light, normal, normal;
    filter: brightness(0.68) contrast(1.07) saturate(0.72);
    opacity: 0.94;
    transform: scale(1.024);
}

/* 暗场从左向右自然消隐，不生成可见的面板边界。 */
body.ui-mode-other settings-screen::after,
body.ui-mode-other [data-component="settings-screen"]::after {
    content: "";
    position: fixed;
    z-index: -1;
    inset: 0;
    pointer-events: none;

    background:
        radial-gradient(
            ellipse at center,
            transparent 32%,
            rgba(0, 0, 0, 0.14) 68%,
            rgba(0, 0, 0, 0.48) 100%
        ),
        linear-gradient(
            90deg,
            #050609 0%,
            rgba(5, 6, 9, 0.995) 30%,
            rgba(5, 6, 9, 0.91) 45%,
            rgba(5, 6, 9, 0.53) 60%,
            rgba(5, 6, 9, 0.15) 82%,
            rgba(5, 6, 9, 0.30) 100%
        ),
        linear-gradient(
            180deg,
            rgba(0, 0, 0, 0.48) 0%,
            transparent 24%,
            transparent 70%,
            rgba(0, 0, 0, 0.70) 100%
        ),
        repeating-linear-gradient(
            0deg,
            rgba(255, 255, 255, 0.008) 0 1px,
            transparent 1px 4px
        );
}

body.ui-mode-other settings-screen > h2,
body.ui-mode-other [data-component="settings-screen"] > h2 {
    position: fixed !important;
    z-index: 3 !important;
    top: clamp(28px, 5.2vh, 58px) !important;
    left: clamp(92px, 8.2vw, 158px) !important;

    width: min(48vw, 900px) !important;
    margin: 0 !important;
    padding: 0 0 15px !important;

    border: none !important;
    background: transparent !important;
    color: transparent !important;

    font-family: inherit !important;
    font-size: clamp(27px, 2.55vw, 45px) !important;
    font-weight: 260 !important;
    line-height: 1.12 !important;
    letter-spacing: 0.15em !important;
    text-align: left !important;

    background: linear-gradient(
        180deg,
        #ffffff 0%,
        #e6e7e9 38%,
        #a9adb4 100%
    ) !important;
    -webkit-background-clip: text !important;
    background-clip: text !important;

    filter:
        drop-shadow(0 2px 2px rgba(255, 255, 255, 0.04))
        drop-shadow(0 8px 24px rgba(0, 0, 0, 0.78));

    animation: otherSettingsTitleEnter 520ms 80ms both !important;
}

body.ui-mode-other settings-screen > h2::after,
body.ui-mode-other [data-component="settings-screen"] > h2::after {
    content: "";
    position: absolute;
    left: 0;
    bottom: 0;

    width: min(27rem, 76%);
    height: 1px;
    background: linear-gradient(
        90deg,
        rgba(255, 255, 255, 0.82) 0%,
        rgba(220, 224, 230, 0.34) 18%,
        rgba(255, 255, 255, 0.055) 72%,
        transparent 100%
    );
    box-shadow: 0 0 15px rgba(220, 225, 232, 0.08);
}

/* 返回键保持为电影字幕式图标，不使用圆形或方形底。 */
body.ui-mode-other settings-screen > [data-action="back"],
body.ui-mode-other [data-component="settings-screen"] > [data-action="back"] {
    position: fixed !important;
    z-index: 5 !important;
    top: clamp(24px, 4.7vh, 52px) !important;
    left: clamp(22px, 2.6vw, 50px) !important;

    display: grid !important;
    place-items: center !important;
    width: clamp(46px, 4.2vw, 64px) !important;
    height: clamp(38px, 4.8vw, 54px) !important;
    min-width: 0 !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;

    border: none !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(245, 244, 242, 0.64) !important;
    font-size: clamp(21px, 2.1vw, 31px) !important;
    filter: drop-shadow(0 3px 10px rgba(0, 0, 0, 0.7));
    transition:
        color 200ms ease,
        transform 260ms cubic-bezier(0.22, 1, 0.36, 1),
        filter 200ms ease !important;
}

body.ui-mode-other settings-screen > [data-action="back"]::after,
body.ui-mode-other [data-component="settings-screen"] > [data-action="back"]::after {
    content: "";
    position: absolute;
    top: 50%;
    left: 72%;

    width: clamp(22px, 2.8vw, 46px);
    height: 1px;
    pointer-events: none;

    background: linear-gradient(
        90deg,
        rgba(255, 255, 255, 0.34),
        transparent
    );
    transform: scaleX(0.62);
    transform-origin: left center;
    transition: opacity 200ms ease, transform 260ms ease;
}

body.ui-mode-other settings-screen > [data-action="back"]:hover,
body.ui-mode-other settings-screen > [data-action="back"]:focus-visible,
body.ui-mode-other [data-component="settings-screen"] > [data-action="back"]:hover,
body.ui-mode-other [data-component="settings-screen"] > [data-action="back"]:focus-visible {
    color: rgba(255, 255, 255, 0.96) !important;
    transform: translateX(-4px) !important;
    filter:
        drop-shadow(0 0 7px rgba(255, 255, 255, 0.16))
        drop-shadow(0 3px 10px rgba(0, 0, 0, 0.72));
}

body.ui-mode-other settings-screen > [data-action="back"]:hover::after,
body.ui-mode-other settings-screen > [data-action="back"]:focus-visible::after,
body.ui-mode-other [data-component="settings-screen"] > [data-action="back"]:hover::after,
body.ui-mode-other [data-component="settings-screen"] > [data-action="back"]:focus-visible::after {
    opacity: 0.88;
    transform: scaleX(1);
}

/* Monogatari 原有双列被拍平成一条稳定的设置阅读动线。 */
body.ui-mode-other settings-screen > .row,
body.ui-mode-other [data-component="settings-screen"] > .row {
    position: absolute !important;
    z-index: 2 !important;
    top: clamp(102px, 17.5vh, 176px) !important;
    bottom: clamp(66px, 8.5vh, 96px) !important;
    left: clamp(92px, 8.2vw, 158px) !important;

    display: flex !important;
    flex-direction: column !important;
    justify-content: flex-start !important;
    align-items: stretch !important;
    gap: 0 !important;

    width: min(48vw, 900px) !important;
    height: auto !important;
    margin: 0 !important;
    padding: 0 clamp(14px, 1.4vw, 26px) 0 0 !important;
    box-sizing: border-box !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;

    text-align: left !important;
    scrollbar-width: thin;
    scrollbar-color: rgba(255, 255, 255, 0.20) transparent;
}

/* 右侧的镜头光圈、投影光束和克制的银幕构图线。 */
body.ui-mode-other settings-screen > .row::before,
body.ui-mode-other [data-component="settings-screen"] > .row::before {
    content: "";
    position: fixed;
    z-index: -1;
    inset: 0;
    pointer-events: none;

    background:
        radial-gradient(
            circle at 80% 47%,
            transparent 0 9.5%,
            rgba(236, 239, 243, 0.045) 9.8%,
            transparent 10.2% 18%,
            rgba(214, 219, 226, 0.022) 18.25%,
            transparent 18.65%
        ),
        radial-gradient(
            ellipse at 81% 45%,
            rgba(242, 244, 247, 0.086) 0%,
            rgba(161, 169, 180, 0.032) 24%,
            transparent 54%
        ),
        linear-gradient(
            112deg,
            transparent 46%,
            rgba(255, 255, 255, 0.018) 56%,
            rgba(235, 239, 244, 0.058) 61%,
            transparent 69%
        );

    opacity: 0.94;
    mix-blend-mode: screen;
}

body.ui-mode-other settings-screen > .row::after,
body.ui-mode-other [data-component="settings-screen"] > .row::after {
    content: none !important;
    display: none !important;
}

body.ui-mode-other settings-screen > .row > .row__column,
body.ui-mode-other [data-component="settings-screen"] > .row > .row__column {
    display: contents !important;
}

body.ui-mode-other settings-screen > .row::-webkit-scrollbar,
body.ui-mode-other [data-component="settings-screen"] > .row::-webkit-scrollbar {
    width: 3px;
}

body.ui-mode-other settings-screen > .row::-webkit-scrollbar-track,
body.ui-mode-other [data-component="settings-screen"] > .row::-webkit-scrollbar-track {
    background: transparent;
}

body.ui-mode-other settings-screen > .row::-webkit-scrollbar-thumb,
body.ui-mode-other [data-component="settings-screen"] > .row::-webkit-scrollbar-thumb {
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.18);
}

/* 每项只以细分隔线组织，不形成独立卡片。 */
body.ui-mode-other settings-screen [data-settings],
body.ui-mode-other [data-component="settings-screen"] [data-settings] {
    position: relative !important;
    min-width: 0 !important;

    display: grid !important;
    grid-template-columns: minmax(148px, 34%) minmax(0, 1fr) !important;
    align-items: center !important;
    column-gap: clamp(22px, 2.5vw, 44px) !important;

    width: 100% !important;
    min-height: clamp(60px, 8.1vh, 82px) !important;
    margin: 0 !important;
    padding: clamp(9px, 1.15vh, 14px) 0 !important;
    box-sizing: border-box !important;

    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(238, 237, 235, 0.78) !important;
    text-align: left !important;

    animation: otherSettingsItemEnter 500ms both !important;
    transition:
        border-color 240ms ease,
        background 300ms ease,
        filter 240ms ease !important;
}

body.ui-mode-other settings-screen [data-settings]::before,
body.ui-mode-other [data-component="settings-screen"] [data-settings]::before {
    content: none !important;
    display: none !important;
}

/* 局部渐隐分隔线：只负责节奏，不包围整项。 */
body.ui-mode-other settings-screen [data-settings]::after,
body.ui-mode-other [data-component="settings-screen"] [data-settings]::after {
    content: none !important;
    display: none !important;
}

body.ui-mode-other settings-screen [data-settings]:hover,
body.ui-mode-other settings-screen [data-settings]:focus-within,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:hover,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:focus-within {
    background: transparent !important;
    filter: none !important;
}

body.ui-mode-other settings-screen [data-settings]:hover::before,
body.ui-mode-other settings-screen [data-settings]:focus-within::before,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:hover::before,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:focus-within::before {
    content: none !important;
    display: none !important;
}

body.ui-mode-other settings-screen [data-settings]:hover::after,
body.ui-mode-other settings-screen [data-settings]:focus-within::after,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:hover::after,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:focus-within::after {
    content: none !important;
    display: none !important;
}

body.ui-mode-other settings-screen [data-settings="audio"] {
    animation-delay: 80ms !important;
}

body.ui-mode-other settings-screen [data-settings="text-speed"] {
    animation-delay: 135ms !important;
}

body.ui-mode-other settings-screen [data-settings="auto-play-speed"] {
    animation-delay: 190ms !important;
}

body.ui-mode-other settings-screen [data-settings$="quick-menu"] {
    animation-delay: 245ms !important;
}

body.ui-mode-other settings-screen [data-settings] > h3,
body.ui-mode-other [data-component="settings-screen"] [data-settings] > h3 {
    grid-column: 1 !important;
    align-self: center !important;

    min-width: 0 !important;
    width: auto !important;
    margin: 0 !important;
    padding: 0 !important;

    border: none !important;
    background: transparent !important;
    color: rgba(222, 224, 228, 0.78) !important;

    font-family: inherit !important;
    font-size: clamp(15px, 1.12vw, 19px) !important;
    font-weight: 400 !important;
    line-height: 1.42 !important;
    letter-spacing: 0.035em !important;
    text-align: left !important;
    text-transform: none !important;
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
    white-space: normal !important;
    overflow: visible !important;
    text-shadow:
        0 1px 1px rgba(255, 255, 255, 0.025),
        0 3px 11px rgba(0, 0, 0, 0.76) !important;

    transition: color 220ms ease, text-shadow 220ms ease, transform 260ms ease !important;
}

body.ui-mode-other settings-screen [data-settings]:hover > h3,
body.ui-mode-other settings-screen [data-settings]:focus-within > h3,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:hover > h3,
body.ui-mode-other [data-component="settings-screen"] [data-settings]:focus-within > h3 {
    color: rgba(250, 250, 250, 0.94) !important;
    text-shadow:
        0 0 9px rgba(255, 255, 255, 0.10),
        0 3px 12px rgba(0, 0, 0, 0.78) !important;
    transform: translateX(4px);
}

body.ui-mode-other settings-screen [data-settings] > input[type="range"],
body.ui-mode-other settings-screen [data-settings] > .horizontal,
body.ui-mode-other settings-screen [data-settings] > [data-content="wrapper"],
body.ui-mode-other [data-component="settings-screen"] [data-settings] > input[type="range"],
body.ui-mode-other [data-component="settings-screen"] [data-settings] > .horizontal,
body.ui-mode-other [data-component="settings-screen"] [data-settings] > [data-content="wrapper"] {
    grid-column: 2 !important;
    width: 100% !important;
    margin: 0 !important;
}

/* 音量项目共用与其他设置相同的两列节奏。 */
body.ui-mode-other settings-screen [data-settings="audio"],
body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] {
    grid-template-rows: repeat(4, minmax(48px, 6.4vh)) !important;
    padding-top: clamp(11px, 1.6vh, 18px) !important;
    padding-bottom: clamp(11px, 1.6vh, 18px) !important;
}

body.ui-mode-other settings-screen [data-settings="audio"] > h3,
body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] > h3 {
    display: none !important;
}

body.ui-mode-other settings-screen [data-settings="audio"] > span,
body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] > span {
    grid-column: 1 !important;
    align-self: center !important;

    margin: 0 !important;
    padding: 0 !important;

    color: rgba(210, 213, 218, 0.72) !important;
    font-family: inherit !important;
    font-size: clamp(14px, 1.04vw, 18px) !important;
    font-weight: 400 !important;
    line-height: 1.4 !important;
    letter-spacing: 0.025em !important;
    text-align: left !important;
    min-width: 0 !important;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
    overflow: visible !important;
    text-shadow:
        0 1px 1px rgba(255, 255, 255, 0.02),
        0 3px 10px rgba(0, 0, 0, 0.72) !important;
    transition: color 220ms ease, text-shadow 220ms ease, transform 260ms ease !important;
}

body.ui-mode-other settings-screen [data-settings="audio"] > span:hover,
body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] > span:hover {
    color: rgba(248, 248, 248, 0.92) !important;
    text-shadow:
        0 0 8px rgba(255, 255, 255, 0.08),
        0 3px 11px rgba(0, 0, 0, 0.76) !important;
    transform: translateX(4px);
}

/* 音量组不再使用整组悬浮状态，只点亮当前滑杆对应的标签。 */
body.ui-mode-other settings-screen [data-settings="audio"] > span:has(+ input[type="range"]:hover),
body.ui-mode-other settings-screen [data-settings="audio"] > span:has(+ input[type="range"]:focus-visible),
body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] > span:has(+ input[type="range"]:hover),
body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] > span:has(+ input[type="range"]:focus-visible) {
    color: rgba(248, 248, 248, 0.92) !important;
    text-shadow:
        0 0 8px rgba(255, 255, 255, 0.08),
        0 3px 11px rgba(0, 0, 0, 0.76) !important;
    transform: translateX(4px);
}

body.ui-mode-other settings-screen [data-settings="audio"] > input[type="range"],
body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] > input[type="range"] {
    grid-column: 2 !important;
    align-self: center !important;
}

/* 银灰滑杆：细轨道、圆形光点，没有系统蓝色。 */
body.ui-mode-other settings-screen input[type="range"],
body.ui-mode-other [data-component="settings-screen"] input[type="range"] {
    width: 100% !important;
    height: 28px !important;
    margin: 0 !important;
    padding: 0 !important;

    border: none !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    accent-color: rgba(238, 238, 238, 0.92) !important;

    cursor: pointer !important;
    -webkit-appearance: none !important;
    appearance: none !important;
    filter: drop-shadow(0 4px 9px rgba(0, 0, 0, 0.26));
}

body.ui-mode-other settings-screen input[type="range"]:hover,
body.ui-mode-other settings-screen input[type="range"]:focus-visible,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]:hover,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]:focus-visible {
    filter:
        drop-shadow(0 0 7px rgba(235, 239, 245, 0.14))
        drop-shadow(0 4px 10px rgba(0, 0, 0, 0.32));
}

body.ui-mode-other settings-screen input[type="range"]::-webkit-slider-runnable-track,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]::-webkit-slider-runnable-track {
    height: 3px;
    border: none;
    border-radius: 0;
    background: linear-gradient(
        90deg,
        rgba(255, 255, 255, 0.08) 0%,
        rgba(154, 160, 169, 0.34) 16%,
        rgba(231, 234, 238, 0.78) 49%,
        rgba(128, 134, 144, 0.30) 84%,
        rgba(255, 255, 255, 0.06) 100%
    );
    box-shadow:
        0 1px 0 rgba(0, 0, 0, 0.78),
        0 -1px 0 rgba(255, 255, 255, 0.025),
        0 0 12px rgba(223, 228, 235, 0.055);
}

body.ui-mode-other settings-screen input[type="range"]::-webkit-slider-thumb,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]::-webkit-slider-thumb {
    width: 17px;
    height: 17px;
    margin-top: -7px;

    border: 1px solid rgba(255, 255, 255, 0.88);
    border-radius: 50%;
    background: radial-gradient(
        circle at 36% 30%,
        #ffffff 0%,
        #e3e5e8 34%,
        #a6abb2 74%,
        #71767e 100%
    );
    box-shadow:
        0 0 0 1px rgba(0, 0, 0, 0.50),
        0 0 0 5px rgba(235, 239, 244, 0.045),
        0 0 17px rgba(227, 232, 239, 0.22),
        0 5px 11px rgba(0, 0, 0, 0.78),
        inset 0 1px 2px rgba(255, 255, 255, 0.72);

    -webkit-appearance: none;
    transition:
        transform 180ms ease,
        background 180ms ease,
        box-shadow 180ms ease;
}

body.ui-mode-other settings-screen input[type="range"]:hover::-webkit-slider-thumb,
body.ui-mode-other settings-screen input[type="range"]:focus-visible::-webkit-slider-thumb,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]:hover::-webkit-slider-thumb,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]:focus-visible::-webkit-slider-thumb {
    background: #ffffff;
    box-shadow:
        0 0 0 1px rgba(0, 0, 0, 0.52),
        0 0 0 7px rgba(240, 243, 247, 0.065),
        0 0 24px rgba(235, 239, 245, 0.36),
        0 5px 12px rgba(0, 0, 0, 0.80),
        inset 0 1px 2px rgba(255, 255, 255, 0.82);
    transform: scale(1.14);
}

body.ui-mode-other settings-screen input[type="range"]::-moz-range-track,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]::-moz-range-track {
    height: 2px;
    border: none;
    border-radius: 0;
    background: rgba(255, 255, 255, 0.30);
}

body.ui-mode-other settings-screen input[type="range"]::-moz-range-thumb,
body.ui-mode-other [data-component="settings-screen"] input[type="range"]::-moz-range-thumb {
    width: 14px;
    height: 14px;
    border: 1px solid rgba(255, 255, 255, 0.78);
    border-radius: 50%;
    background: rgba(238, 238, 238, 0.98);
    box-shadow: 0 0 12px rgba(255, 255, 255, 0.18);
}

/* 下拉类控件沿用轻量暗场；只在项目存在时生效。 */
body.ui-mode-other settings-screen select,
body.ui-mode-other settings-screen input[type="text"],
body.ui-mode-other [data-component="settings-screen"] select,
body.ui-mode-other [data-component="settings-screen"] input[type="text"] {
    width: 100% !important;
    min-height: 42px !important;
    margin: 0 !important;
    padding: 7px 34px 7px 12px !important;

    border: none !important;
    border-bottom: 1px solid rgba(255, 255, 255, 0.20) !important;
    border-radius: 0 !important;
    outline: none !important;
    background: rgba(255, 255, 255, 0.025) !important;
    box-shadow: none !important;
    color: rgba(245, 244, 242, 0.86) !important;

    font-family: inherit !important;
    font-size: clamp(14px, 1.03vw, 18px) !important;
    font-weight: 400 !important;
    text-align: left !important;
}

/* Hide / Show：纯文字双选，不使用胶囊和矩形按钮。 */
body.ui-mode-other settings-screen .other-settings-choice,
body.ui-mode-other settings-screen .ancient-settings-choice,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice {
    grid-column: 2 !important;

    display: grid !important;
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    align-items: center !important;
    gap: 0 !important;

    width: 100% !important;
    min-height: 40px !important;
    margin: 0 !important;
    padding: 0 !important;

    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-other settings-screen .other-settings-choice button,
body.ui-mode-other settings-screen .ancient-settings-choice button,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice button,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice button {
    position: relative !important;

    /* Monogatari's desktop stylesheet gives every button a fixed width
       (about 10rem).  The two-column settings grid is narrower than that,
       so the inherited width made the second label get clipped by the row's
       overflow rule (Hide rendered as only H). */
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
    min-height: 40px !important;
    margin: 0 !important;
    padding: clamp(9px, 1.15vh, 14px) 16px !important;

    border: none !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(236, 235, 233, 0.46) !important;

    font-family: inherit !important;
    font-size: clamp(15px, 1.12vw, 19px) !important;
    font-weight: 400 !important;
    line-height: 1.42 !important;
    letter-spacing: 0.045em !important;
    text-align: center !important;
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.72) !important;

    transition:
        color 180ms ease,
        text-shadow 180ms ease !important;
}

body.ui-mode-other settings-screen .other-settings-choice button + button::before,
body.ui-mode-other settings-screen .ancient-settings-choice button + button::before,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice button + button::before,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice button + button::before {
    content: "";
    position: absolute;
    top: 27%;
    bottom: 27%;
    left: 0;
    width: 1px;
    background: rgba(255, 255, 255, 0.11);
}

body.ui-mode-other settings-screen .other-settings-choice button::after,
body.ui-mode-other settings-screen .ancient-settings-choice button::after,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice button::after,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice button::after {
    content: "";
    position: absolute;
    right: 24%;
    bottom: 3px;
    left: 24%;
    height: 1px;
    opacity: 0;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.66), transparent);
    transform: scaleX(0.42);
    transition: opacity 180ms ease, transform 260ms ease;
}

body.ui-mode-other settings-screen .other-settings-choice button:hover,
body.ui-mode-other settings-screen .other-settings-choice button:focus-visible,
body.ui-mode-other settings-screen .other-settings-choice button.is-active,
body.ui-mode-other settings-screen .ancient-settings-choice button:hover,
body.ui-mode-other settings-screen .ancient-settings-choice button:focus-visible,
body.ui-mode-other settings-screen .ancient-settings-choice button.is-active,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice button:hover,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice button:focus-visible,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice button.is-active,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice button:hover,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice button:focus-visible,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice button.is-active {
    color: rgba(255, 255, 255, 0.94) !important;
    text-shadow:
        0 0 8px rgba(255, 255, 255, 0.12),
        0 2px 9px rgba(0, 0, 0, 0.78) !important;
}

body.ui-mode-other settings-screen .other-settings-choice button.is-active::after,
body.ui-mode-other settings-screen .ancient-settings-choice button.is-active::after,
body.ui-mode-other [data-component="settings-screen"] .other-settings-choice button.is-active::after,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-choice button.is-active::after {
    opacity: 0.88;
    transform: scaleX(1);
}

/* 恢复默认是低调的文字动作，不抢夺设置项的视觉焦点。 */
body.ui-mode-other settings-screen .other-settings-reset,
body.ui-mode-other settings-screen .ancient-settings-reset,
body.ui-mode-other [data-component="settings-screen"] .other-settings-reset,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset {
    position: fixed !important;
    z-index: 4 !important;
    bottom: clamp(24px, 4.1vh, 46px) !important;
    right: clamp(92px, 8.2vw, 158px) !important;
    left: auto !important;

    display: inline-flex !important;
    align-items: center !important;
    justify-content: flex-end !important;
    gap: 11px !important;
    width: auto !important;
    min-width: 0 !important;
    min-height: 34px !important;
    margin: 0 !important;
    padding: clamp(9px, 1.15vh, 14px) 0 !important;

    border: none !important;
    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    -webkit-appearance: none !important;
    appearance: none !important;
    color: rgba(235, 234, 232, 0.46) !important;

    font-family: inherit !important;
    font-size: clamp(15px, 1.12vw, 19px) !important;
    font-weight: 400 !important;
    line-height: 1.42 !important;
    letter-spacing: 0.055em !important;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.72) !important;

    transition:
        color 180ms ease,
        text-shadow 180ms ease,
        transform 240ms ease !important;
}

body.ui-mode-other settings-screen .other-settings-reset::before,
body.ui-mode-other settings-screen .ancient-settings-reset::before,
body.ui-mode-other [data-component="settings-screen"] .other-settings-reset::before,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset::before {
    content: "" !important;
    display: block !important;
    width: 24px !important;
    height: 1px !important;
    flex: 0 0 auto !important;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.38)) !important;
    transition: width 240ms ease, opacity 180ms ease !important;
}

body.ui-mode-other settings-screen .other-settings-reset::after,
body.ui-mode-other settings-screen .ancient-settings-reset::after,
body.ui-mode-other [data-component="settings-screen"] .other-settings-reset::after,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset::after {
    content: none !important;
    display: none !important;
}

body.ui-mode-other settings-screen .other-settings-reset:hover,
body.ui-mode-other settings-screen .other-settings-reset:focus-visible,
body.ui-mode-other settings-screen .ancient-settings-reset:hover,
body.ui-mode-other settings-screen .ancient-settings-reset:focus-visible,
body.ui-mode-other [data-component="settings-screen"] .other-settings-reset:hover,
body.ui-mode-other [data-component="settings-screen"] .other-settings-reset:focus-visible,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset:hover,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset:focus-visible {
    color: rgba(255, 255, 255, 0.88) !important;
    text-shadow:
        0 0 9px rgba(255, 255, 255, 0.10),
        0 2px 8px rgba(0, 0, 0, 0.76) !important;
    transform: translateX(-3px) !important;
}

body.ui-mode-other settings-screen .other-settings-reset:hover::before,
body.ui-mode-other settings-screen .other-settings-reset:focus-visible::before,
body.ui-mode-other settings-screen .ancient-settings-reset:hover::before,
body.ui-mode-other settings-screen .ancient-settings-reset:focus-visible::before,
body.ui-mode-other [data-component="settings-screen"] .other-settings-reset:hover::before,
body.ui-mode-other [data-component="settings-screen"] .other-settings-reset:focus-visible::before,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset:hover::before,
body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset:focus-visible::before {
    width: 31px !important;
}

/* 兼容当前最小公共化方案所沿用的状态类。 */
body.ui-mode-other.ancient-quick-menu-hidden [data-component="quick-menu"],
body.ui-mode-other.other-quick-menu-hidden [data-component="quick-menu"] {
    display: none !important;
}

body.ui-mode-other.ancient-quick-menu-hidden #stats-toggle,
body.ui-mode-other.other-quick-menu-hidden #stats-toggle {
    display: none !important;
}

@keyframes otherSettingsEnter {
    from {
        opacity: 0;
        filter: brightness(0.86);
    }

    to {
        opacity: 1;
        filter: brightness(1);
    }
}

@keyframes otherSettingsTitleEnter {
    from {
        opacity: 0;
        transform: translateY(10px);
        filter:
            blur(2px)
            drop-shadow(0 8px 24px rgba(0, 0, 0, 0.78));
    }

    to {
        opacity: 1;
        transform: translateY(0);
        filter:
            blur(0)
            drop-shadow(0 2px 2px rgba(255, 255, 255, 0.04))
            drop-shadow(0 8px 24px rgba(0, 0, 0, 0.78));
    }
}

@keyframes otherSettingsItemEnter {
    from {
        opacity: 0;
        transform: translateX(-10px);
        filter: blur(1px);
    }

    to {
        opacity: 1;
        transform: translateX(0);
        filter: blur(0);
    }
}

/* ==================== Settings 响应式 ==================== */

/*
 * 宽屏不再把全部设置压在一条窄列中：
 * 左栏承担四组音量，右栏承担文字、自动播放及快捷菜单。
 */
@media (min-width: 1500px) and (min-aspect-ratio: 16 / 10) {
    body.ui-mode-other settings-screen > h2,
    body.ui-mode-other [data-component="settings-screen"] > h2,
    body.ui-mode-other settings-screen > .row,
    body.ui-mode-other [data-component="settings-screen"] > .row {
        width: min(60vw, 1680px) !important;
    }

    body.ui-mode-other settings-screen > .row,
    body.ui-mode-other [data-component="settings-screen"] > .row {
        top: clamp(150px, 14vh, 220px) !important;
        bottom: clamp(92px, 10vh, 160px) !important;

        display: grid !important;
        grid-template-columns: minmax(0, 1.16fr) minmax(0, 0.84fr) !important;
        grid-template-rows: auto auto !important;
        align-content: center !important;
        align-items: stretch !important;
        justify-content: stretch !important;
        column-gap: clamp(72px, 5vw, 170px) !important;
        row-gap: clamp(26px, 3vh, 56px) !important;

        padding-right: 0 !important;
        overflow-y: auto !important;
    }

    body.ui-mode-other settings-screen > .row > .row__column,
    body.ui-mode-other [data-component="settings-screen"] > .row > .row__column {
        position: relative !important;

        display: flex !important;
        flex-direction: column !important;
        justify-content: center !important;
        align-items: stretch !important;
        gap: clamp(28px, 3.3vh, 68px) !important;

        width: 100% !important;
        min-width: 0 !important;
        max-width: none !important;
        height: auto !important;
        margin: 0 !important;
        padding: 0 !important;
    }

    body.ui-mode-other settings-screen > .row > .row__column:first-child,
    body.ui-mode-other [data-component="settings-screen"] > .row > .row__column:first-child {
        grid-column: 1 !important;
        grid-row: 1 / span 2 !important;
        align-self: center !important;
    }

    body.ui-mode-other settings-screen > .row > .row__column:nth-child(2),
    body.ui-mode-other [data-component="settings-screen"] > .row > .row__column:nth-child(2) {
        grid-column: 2 !important;
        grid-row: 1 !important;
        align-self: end !important;
    }

    /* 两个控制区域之间仅用一条渐隐银线分区，不生成第二个面板。 */
    body.ui-mode-other settings-screen > .row > .row__column:nth-child(2)::before,
    body.ui-mode-other [data-component="settings-screen"] > .row > .row__column:nth-child(2)::before {
        content: none !important;
        display: none !important;
    }

    body.ui-mode-other settings-screen [data-settings="audio"],
    body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] {
        min-height: clamp(500px, 59vh, 820px) !important;
        grid-template-rows: repeat(4, minmax(92px, 1fr)) !important;
        padding: clamp(22px, 2.6vh, 42px) 0 !important;

        border: none !important;
    }

    body.ui-mode-other settings-screen [data-settings="text-speed"],
    body.ui-mode-other settings-screen [data-settings="auto-play-speed"],
    body.ui-mode-other [data-component="settings-screen"] [data-settings="text-speed"],
    body.ui-mode-other [data-component="settings-screen"] [data-settings="auto-play-speed"] {
        min-height: clamp(142px, 17vh, 230px) !important;
        padding-top: clamp(18px, 2vh, 32px) !important;
        padding-bottom: clamp(18px, 2vh, 32px) !important;
    }

    body.ui-mode-other settings-screen > .row > [data-settings$="quick-menu"],
    body.ui-mode-other [data-component="settings-screen"] > .row > [data-settings$="quick-menu"] {
        grid-column: 2 !important;
        grid-row: 2 !important;
        align-self: start !important;

        min-height: clamp(108px, 12vh, 166px) !important;
        padding-top: clamp(16px, 1.8vh, 28px) !important;
        padding-bottom: clamp(16px, 1.8vh, 28px) !important;
    }

    body.ui-mode-other settings-screen [data-settings],
    body.ui-mode-other [data-component="settings-screen"] [data-settings] {
        grid-template-columns: minmax(170px, 31%) minmax(0, 1fr) !important;
        column-gap: clamp(34px, 2.5vw, 76px) !important;
    }

    body.ui-mode-other settings-screen [data-settings] > h3,
    body.ui-mode-other settings-screen [data-settings="audio"] > span,
    body.ui-mode-other [data-component="settings-screen"] [data-settings] > h3,
    body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] > span {
        font-size: clamp(16px, 1.05vw, 23px) !important;
    }

    body.ui-mode-other settings-screen .other-settings-reset,
    body.ui-mode-other settings-screen .ancient-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .other-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset {
        bottom: clamp(30px, 4.5vh, 70px) !important;
    }
}

@media (max-width: 900px) {
    body.ui-mode-other settings-screen::before,
    body.ui-mode-other [data-component="settings-screen"]::before {
        width: 100%;
        opacity: 0.72;
    }

    body.ui-mode-other settings-screen::after,
    body.ui-mode-other [data-component="settings-screen"]::after {
        background:
            linear-gradient(90deg, rgba(7, 8, 10, 0.98), rgba(7, 8, 10, 0.86) 68%, rgba(7, 8, 10, 0.64)),
            linear-gradient(180deg, rgba(0, 0, 0, 0.46), transparent 24%, transparent 70%, rgba(0, 0, 0, 0.68));
    }

    body.ui-mode-other settings-screen > h2,
    body.ui-mode-other [data-component="settings-screen"] > h2,
    body.ui-mode-other settings-screen > .row,
    body.ui-mode-other [data-component="settings-screen"] > .row {
        left: clamp(78px, 11vw, 112px) !important;
        width: min(76vw, 650px) !important;
    }

    body.ui-mode-other settings-screen .other-settings-reset,
    body.ui-mode-other settings-screen .ancient-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .other-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset {
        right: clamp(78px, 11vw, 112px) !important;
        left: auto !important;
    }
}

@media (orientation: landscape) and (max-height: 600px) {
    body.ui-mode-other settings-screen > h2,
    body.ui-mode-other [data-component="settings-screen"] > h2 {
        top: 20px !important;
        left: clamp(76px, 8vw, 124px) !important;
        width: min(50vw, 650px) !important;
        padding-bottom: 10px !important;
        font-size: clamp(23px, 4.5vh, 34px) !important;
    }

    body.ui-mode-other settings-screen > [data-action="back"],
    body.ui-mode-other [data-component="settings-screen"] > [data-action="back"] {
        top: 14px !important;
        left: 18px !important;
    }

    body.ui-mode-other settings-screen > .row,
    body.ui-mode-other [data-component="settings-screen"] > .row {
        top: 78px !important;
        bottom: 45px !important;
        left: clamp(76px, 8vw, 124px) !important;
        width: min(52vw, 680px) !important;
    }

    body.ui-mode-other settings-screen [data-settings],
    body.ui-mode-other [data-component="settings-screen"] [data-settings] {
        grid-template-columns: minmax(132px, 32%) minmax(0, 1fr) !important;
        min-height: 48px !important;
        padding: 6px 0 !important;
    }

    body.ui-mode-other settings-screen [data-settings="audio"],
    body.ui-mode-other [data-component="settings-screen"] [data-settings="audio"] {
        grid-template-rows: repeat(4, minmax(40px, 7vh)) !important;
        padding-top: 6px !important;
        padding-bottom: 6px !important;
    }

    body.ui-mode-other settings-screen .other-settings-reset,
    body.ui-mode-other settings-screen .ancient-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .other-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset {
        bottom: 8px !important;
        right: clamp(76px, 8vw, 124px) !important;
        left: auto !important;
        min-height: 28px !important;
        padding: 3px 0 5px !important;
    }
}

@media (max-width: 560px) and (orientation: portrait) {
    body.ui-mode-other settings-screen > h2,
    body.ui-mode-other [data-component="settings-screen"] > h2 {
        top: 28px !important;
        left: 72px !important;
        width: calc(100vw - 96px) !important;
        font-size: 25px !important;
    }

    body.ui-mode-other settings-screen > [data-action="back"],
    body.ui-mode-other [data-component="settings-screen"] > [data-action="back"] {
        top: 21px !important;
        left: 14px !important;
    }

    body.ui-mode-other settings-screen > .row,
    body.ui-mode-other [data-component="settings-screen"] > .row {
        top: 96px !important;
        bottom: 58px !important;
        left: 28px !important;
        width: calc(100vw - 48px) !important;
    }

    body.ui-mode-other settings-screen [data-settings],
    body.ui-mode-other [data-component="settings-screen"] [data-settings] {
        grid-template-columns: minmax(112px, 37%) minmax(0, 1fr) !important;
        column-gap: 16px !important;
    }

    body.ui-mode-other settings-screen .other-settings-reset,
    body.ui-mode-other settings-screen .ancient-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .other-settings-reset,
    body.ui-mode-other [data-component="settings-screen"] .ancient-settings-reset {
        right: 28px !important;
        left: auto !important;
        bottom: 17px !important;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other settings-screen,
    body.ui-mode-other [data-component="settings-screen"],
    body.ui-mode-other settings-screen *,
    body.ui-mode-other [data-component="settings-screen"] * {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}

/* ==================== Alert / Message 电影暗场弹窗 ==================== */

/* 全屏只负责压暗和分离景深，不生成可见的外框。 */
body.ui-mode-other alert-modal.modal,
body.ui-mode-other message-modal.modal,
body.ui-mode-other [data-component="alert-modal"].modal,
body.ui-mode-other [data-component="message-modal"].modal {
    position: fixed !important;
    z-index: 9800 !important;
    inset: 0 !important;

    width: 100vw !important;
    height: 100vh !important;
    margin: 0 !important;
    padding: clamp(28px, 6vh, 76px) clamp(20px, 5vw, 86px) !important;
    box-sizing: border-box !important;

    align-items: center !important;
    justify-content: center !important;

    background:
        radial-gradient(
            ellipse at center,
            rgba(0, 0, 0, 0.10) 0%,
            rgba(0, 0, 0, 0.32) 54%,
            rgba(0, 0, 0, 0.60) 100%
        ),
        linear-gradient(
            180deg,
            rgba(2, 3, 5, 0.30),
            rgba(2, 3, 5, 0.46)
        ) !important;
    backdrop-filter: blur(4px) brightness(0.68) saturate(0.78) !important;
    -webkit-backdrop-filter: blur(4px) brightness(0.68) saturate(0.78) !important;
    transform: none !important;
}

body.ui-mode-other alert-modal.modal.modal--active,
body.ui-mode-other message-modal.modal.modal--active,
body.ui-mode-other [data-component="alert-modal"].modal.modal--active,
body.ui-mode-other [data-component="message-modal"].modal.modal--active {
    display: flex !important;
    animation: otherPromptBackdropEnter 260ms ease both !important;
}

/* 中央是两端渐隐的电影光幕，不是卡片。 */
body.ui-mode-other alert-modal .modal__content,
body.ui-mode-other message-modal .modal__content,
body.ui-mode-other [data-component="alert-modal"] .modal__content,
body.ui-mode-other [data-component="message-modal"] .modal__content {
    position: fixed !important;
    isolation: isolate !important;
    z-index: 1 !important;
    top: 50vh !important;
    left: 50vw !important;

    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;

    width: min(88vw, 980px) !important;
    min-width: 0 !important;
    max-width: none !important;
    min-height: clamp(230px, 34vh, 360px) !important;
    height: auto !important;
    margin: 0 !important;
    padding: clamp(48px, 8vh, 82px) clamp(34px, 8vw, 112px) clamp(36px, 6vh, 62px) !important;
    box-sizing: border-box !important;
    overflow: visible !important;

    border: none !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(245, 245, 245, 0.92) !important;

    transform: translate(-50%, -50%) !important;
    animation: otherPromptContentEnter 440ms cubic-bezier(0.22, 1, 0.36, 1) both !important;
}

/* 文字后的柔和银幕曝光。 */
body.ui-mode-other alert-modal .modal__content::before,
body.ui-mode-other message-modal .modal__content::before,
body.ui-mode-other [data-component="alert-modal"] .modal__content::before,
body.ui-mode-other [data-component="message-modal"] .modal__content::before {
    content: "" !important;
    position: absolute !important;
    z-index: -1 !important;
    top: 50% !important;
    left: 50% !important;

    width: min(126vw, 1540px) !important;
    height: clamp(310px, 54vh, 560px) !important;
    pointer-events: none !important;

    opacity: 0.96 !important;
    background:
        radial-gradient(
            ellipse at center,
            rgba(22, 24, 29, 0.86) 0%,
            rgba(11, 13, 17, 0.72) 28%,
            rgba(7, 8, 11, 0.46) 48%,
            rgba(4, 5, 7, 0.18) 66%,
            transparent 82%
        ) !important;
    filter: blur(14px) !important;
    transform: translate(-50%, -50%) scaleX(1.04) !important;
}

/* 消息与操作之间的一条渐隐银线。 */
body.ui-mode-other alert-modal .modal__content::after,
body.ui-mode-other message-modal .modal__content::after,
body.ui-mode-other [data-component="alert-modal"] .modal__content::after,
body.ui-mode-other [data-component="message-modal"] .modal__content::after {
    content: "" !important;
    position: absolute !important;
    top: 58% !important;
    left: 50% !important;

    width: min(66%, 560px) !important;
    height: 1px !important;
    pointer-events: none !important;

    opacity: 0.54 !important;
    background: linear-gradient(
        90deg,
        transparent,
        rgba(227, 232, 238, 0.16) 25%,
        rgba(255, 255, 255, 0.32) 50%,
        rgba(227, 232, 238, 0.16) 75%,
        transparent
    ) !important;
    box-shadow: 0 0 12px rgba(229, 234, 240, 0.055) !important;
    transform: translateX(-50%) !important;
}

body.ui-mode-other alert-modal [data-content="message"],
body.ui-mode-other alert-modal [data-content="context"],
body.ui-mode-other message-modal [data-content="message"],
body.ui-mode-other message-modal [data-content="context"],
body.ui-mode-other [data-component="alert-modal"] [data-content="message"],
body.ui-mode-other [data-component="alert-modal"] [data-content="context"],
body.ui-mode-other [data-component="message-modal"] [data-content="message"],
body.ui-mode-other [data-component="message-modal"] [data-content="context"] {
    position: relative !important;
    z-index: 2 !important;

    max-width: min(76vw, 700px) !important;
    margin: 0 auto !important;
    padding: 0 !important;

    border: none !important;
    background: transparent !important;
    color: rgba(243, 243, 242, 0.92) !important;

    font-family: inherit !important;
    font-size: clamp(21px, 1.75vw, 30px) !important;
    font-weight: 350 !important;
    line-height: 1.62 !important;
    letter-spacing: 0.045em !important;
    text-align: center !important;
    text-wrap: balance;
    overflow-wrap: anywhere;

    text-shadow:
        0 1px 1px rgba(255, 255, 255, 0.04),
        0 4px 15px rgba(0, 0, 0, 0.88) !important;
}

body.ui-mode-other alert-modal [data-content="context"],
body.ui-mode-other message-modal [data-content="context"],
body.ui-mode-other [data-component="alert-modal"] [data-content="context"],
body.ui-mode-other [data-component="message-modal"] [data-content="context"] {
    margin-top: 10px !important;
    color: rgba(225, 226, 228, 0.62) !important;
    font-size: clamp(15px, 1.15vw, 19px) !important;
    font-weight: 350 !important;
    line-height: 1.65 !important;
}

/* 覆盖存档确认框的可编辑名称输入：保留输入与 Overwrite 功能，
   只移除浏览器默认白底/默认边框，避免出现突兀的白色矩形。 */
body.ui-mode-other alert-modal input[data-content="context"],
body.ui-mode-other message-modal input[data-content="context"],
body.ui-mode-other [data-component="alert-modal"] input[data-content="context"],
body.ui-mode-other [data-component="message-modal"] input[data-content="context"] {
    width: min(100%, 30rem) !important;
    margin: .25rem auto !important;
    padding: .65rem 1rem !important;
    box-sizing: border-box !important;
    border: 1px solid rgba(210, 214, 221, .28) !important;
    border-radius: 2px !important;
    outline: none !important;
    background: rgba(12, 14, 18, .82) !important;
    color: rgba(243, 244, 246, .92) !important;
    box-shadow: inset 0 0 18px rgba(0, 0, 0, .36) !important;
    text-align: center !important;
    font: inherit !important;
    appearance: none !important;
    -webkit-appearance: none !important;
}

body.ui-mode-other alert-modal input[data-content="context"]:focus,
body.ui-mode-other message-modal input[data-content="context"]:focus,
body.ui-mode-other [data-component="alert-modal"] input[data-content="context"]:focus,
body.ui-mode-other [data-component="message-modal"] input[data-content="context"]:focus {
    border-color: rgba(242, 244, 248, .62) !important;
    background: rgba(18, 20, 25, .92) !important;
}

body.ui-mode-other alert-modal [data-content="actions"],
body.ui-mode-other message-modal [data-content="actions"],
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"],
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] {
    position: relative !important;
    z-index: 2 !important;

    display: flex !important;
    flex-wrap: wrap !important;
    align-items: center !important;
    justify-content: center !important;
    gap: clamp(34px, 6vw, 84px) !important;

    width: min(100%, 620px) !important;
    margin: clamp(42px, 7vh, 66px) auto 0 !important;
    padding: 0 !important;

    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
}

/* 操作按钮是纯文字，不使用灰色矩形底。 */
body.ui-mode-other alert-modal [data-content="actions"] button,
body.ui-mode-other message-modal [data-content="actions"] button,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button {
    position: relative !important;
    isolation: isolate !important;

    min-width: clamp(120px, 13vw, 180px) !important;
    min-height: 48px !important;
    margin: 0 !important;
    padding: 10px clamp(20px, 3vw, 38px) 12px !important;

    border: none !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(224, 225, 227, 0.62) !important;

    font-family: inherit !important;
    font-size: clamp(17px, 1.32vw, 22px) !important;
    font-weight: 400 !important;
    line-height: 1.35 !important;
    letter-spacing: 0.08em !important;
    text-align: center !important;
    text-shadow: 0 3px 10px rgba(0, 0, 0, 0.82) !important;

    cursor: pointer !important;
    transform: translateY(0) !important;
    transition:
        color 200ms ease,
        letter-spacing 280ms cubic-bezier(0.22, 1, 0.36, 1),
        text-shadow 200ms ease,
        transform 260ms cubic-bezier(0.22, 1, 0.36, 1) !important;
}

body.ui-mode-other alert-modal [data-content="actions"] button::before,
body.ui-mode-other message-modal [data-content="actions"] button::before,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button::before,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button::before {
    content: "" !important;
    position: absolute !important;
    z-index: -1 !important;
    top: 50% !important;
    left: 50% !important;

    width: 170% !important;
    height: 58px !important;
    pointer-events: none !important;

    opacity: 0 !important;
    background: radial-gradient(
        ellipse at center,
        rgba(238, 241, 245, 0.085) 0%,
        rgba(151, 159, 170, 0.022) 46%,
        transparent 74%
    ) !important;
    transform: translate(-50%, -50%) scaleX(0.72) !important;
    transition: opacity 220ms ease, transform 360ms cubic-bezier(0.22, 1, 0.36, 1) !important;
}

body.ui-mode-other alert-modal [data-content="actions"] button::after,
body.ui-mode-other message-modal [data-content="actions"] button::after,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button::after,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button::after {
    content: "" !important;
    position: absolute !important;
    right: 18% !important;
    bottom: 5px !important;
    left: 18% !important;

    height: 1px !important;
    pointer-events: none !important;

    opacity: 0.32 !important;
    background: linear-gradient(
        90deg,
        transparent,
        rgba(238, 241, 245, 0.42),
        transparent
    ) !important;
    transform: scaleX(0.46) !important;
    transition: opacity 200ms ease, transform 340ms cubic-bezier(0.22, 1, 0.36, 1) !important;
}

body.ui-mode-other alert-modal [data-content="actions"] button:hover,
body.ui-mode-other alert-modal [data-content="actions"] button:focus-visible,
body.ui-mode-other message-modal [data-content="actions"] button:hover,
body.ui-mode-other message-modal [data-content="actions"] button:focus-visible,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button:hover,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button:focus-visible,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button:hover,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button:focus-visible {
    background: transparent !important;
    color: rgba(255, 255, 255, 0.96) !important;
    letter-spacing: 0.115em !important;
    text-shadow:
        0 0 9px rgba(255, 255, 255, 0.12),
        0 3px 11px rgba(0, 0, 0, 0.86) !important;
    transform: translateY(-2px) !important;
}

body.ui-mode-other alert-modal [data-content="actions"] button:hover::before,
body.ui-mode-other alert-modal [data-content="actions"] button:focus-visible::before,
body.ui-mode-other message-modal [data-content="actions"] button:hover::before,
body.ui-mode-other message-modal [data-content="actions"] button:focus-visible::before,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button:hover::before,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button:focus-visible::before,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button:hover::before,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button:focus-visible::before {
    opacity: 1 !important;
    transform: translate(-50%, -50%) scaleX(1) !important;
}

body.ui-mode-other alert-modal [data-content="actions"] button:hover::after,
body.ui-mode-other alert-modal [data-content="actions"] button:focus-visible::after,
body.ui-mode-other message-modal [data-content="actions"] button:hover::after,
body.ui-mode-other message-modal [data-content="actions"] button:focus-visible::after,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button:hover::after,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button:focus-visible::after,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button:hover::after,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button:focus-visible::after {
    opacity: 0.86 !important;
    transform: scaleX(1) !important;
}

/* 居中剧情短句采用电影转场字幕，不用中央黑色雾块。 */
body.ui-mode-other centered-dialog,
body.ui-mode-other [data-component="centered-dialog"] {
    position: absolute !important;
    isolation: isolate !important;
    width: max-content !important;
    min-width: clamp(280px, 34vw, 520px) !important;
    max-width: min(84vw, 860px) !important;
    padding: clamp(24px, 3.8vh, 38px) clamp(38px, 6vw, 88px) !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(246, 246, 247, 0.94) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter,
        "PingFang SC", "Microsoft YaHei", sans-serif !important;
    font-size: clamp(17px, 1.35vw, 23px) !important;
    font-style: normal !important;
    font-weight: 360 !important;
    line-height: 1.72 !important;
    letter-spacing: 0.09em !important;
    text-align: center !important;
    text-wrap: balance !important;
    text-shadow:
        0 3px 12px rgba(0, 0, 0, 0.96),
        0 7px 26px rgba(0, 0, 0, 0.72) !important;

    animation: otherCenteredDialogEnter 620ms cubic-bezier(0.16, 1, 0.3, 1) both !important;
}

body.ui-mode-other centered-dialog::before,
body.ui-mode-other centered-dialog::after,
body.ui-mode-other [data-component="centered-dialog"]::before,
body.ui-mode-other [data-component="centered-dialog"]::after {
    content: "" !important;
    position: absolute !important;
    z-index: -1 !important;
    right: 10% !important;
    left: 10% !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(
        90deg,
        transparent,
        rgba(255, 255, 255, 0.08) 16%,
        rgba(255, 255, 255, 0.34) 50%,
        rgba(255, 255, 255, 0.08) 84%,
        transparent
    ) !important;
    filter: drop-shadow(0 0 8px rgba(255, 255, 255, 0.08)) !important;
    transform: scaleX(0.38) !important;
    transform-origin: center !important;
    animation: otherCenteredLineReveal 760ms 120ms cubic-bezier(0.16, 1, 0.3, 1) both !important;
}

body.ui-mode-other centered-dialog::before,
body.ui-mode-other [data-component="centered-dialog"]::before {
    top: 0 !important;
    opacity: 0.74 !important;
}

body.ui-mode-other centered-dialog::after,
body.ui-mode-other [data-component="centered-dialog"]::after {
    bottom: 0 !important;
    opacity: 0.42 !important;
}

@keyframes otherPromptBackdropEnter {
    from { opacity: 0; }
    to { opacity: 1; }
}

@keyframes otherPromptContentEnter {
    from {
        opacity: 0;
        filter: blur(3px) brightness(0.82);
        transform: translate(-50%, calc(-50% + 12px)) scale(0.985);
    }

    to {
        opacity: 1;
        filter: blur(0) brightness(1);
        transform: translate(-50%, -50%) scale(1);
    }
}

@keyframes otherCenteredDialogEnter {
    from {
        opacity: 0;
        filter: blur(5px) brightness(0.72);
        transform: translate(-50%, calc(-50% + 10px)) scale(0.988);
    }

    to {
        opacity: 1;
        filter: blur(0) brightness(1);
        transform: translate(-50%, -50%) scale(1);
    }
}

@keyframes otherCenteredLineReveal {
    from {
        opacity: 0;
        transform: scaleX(0.18);
    }

    to {
        transform: scaleX(1);
    }
}

@media (max-width: 700px) {
    body.ui-mode-other centered-dialog,
    body.ui-mode-other [data-component="centered-dialog"] {
        width: min(86vw, 620px) !important;
        min-width: 0 !important;
        padding: 20px 30px !important;
        font-size: clamp(15px, 3.4vw, 18px) !important;
        letter-spacing: 0.065em !important;
    }
}

@media (max-width: 700px) {
    body.ui-mode-other alert-modal .modal__content,
    body.ui-mode-other message-modal .modal__content,
    body.ui-mode-other [data-component="alert-modal"] .modal__content,
    body.ui-mode-other [data-component="message-modal"] .modal__content {
        width: 94vw !important;
        min-height: 250px !important;
        padding: 44px 24px 34px !important;
    }

    body.ui-mode-other alert-modal .modal__content::after,
    body.ui-mode-other message-modal .modal__content::after,
    body.ui-mode-other [data-component="alert-modal"] .modal__content::after,
    body.ui-mode-other [data-component="message-modal"] .modal__content::after {
        top: 57% !important;
        width: 74% !important;
    }

    body.ui-mode-other alert-modal [data-content="actions"],
    body.ui-mode-other message-modal [data-content="actions"],
    body.ui-mode-other [data-component="alert-modal"] [data-content="actions"],
    body.ui-mode-other [data-component="message-modal"] [data-content="actions"] {
        gap: 18px !important;
        margin-top: 48px !important;
    }

    body.ui-mode-other alert-modal [data-content="actions"] button,
    body.ui-mode-other message-modal [data-content="actions"] button,
    body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button,
    body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button {
        min-width: 112px !important;
        padding-right: 18px !important;
        padding-left: 18px !important;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other alert-modal,
    body.ui-mode-other message-modal,
    body.ui-mode-other alert-modal *,
    body.ui-mode-other message-modal *,
    body.ui-mode-other centered-dialog,
    body.ui-mode-other centered-dialog::before,
    body.ui-mode-other centered-dialog::after,
    body.ui-mode-other [data-component="centered-dialog"],
    body.ui-mode-other [data-component="centered-dialog"]::before,
    body.ui-mode-other [data-component="centered-dialog"]::after {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}

/* ==================== ADV 黑白灰电影字幕层 ==================== */

/*
 * 固定高度的底部电影暗场。
 * 不使用悬浮卡片、圆角大框或整块强模糊。
 */
body.ui-mode-other [data-component="text-box"] {
    position: absolute !important;
    z-index: 10 !important;

    inset: auto 0 0 0 !important;

    display: grid !important;
    grid-template-columns: minmax(0, 1fr) !important;
    grid-template-rows: 32px minmax(0, 1fr) !important;
    align-content: start !important;
    row-gap: 10px !important;

    width: 100% !important;
    /* 保持原有文本框高度；只有内容真正溢出时才滚动。 */
    height: clamp(300px, 35vh, 390px) !important;
    min-height: 0 !important;
    max-height: none !important;

    margin: 0 !important;
    /* 底部 72px 是 Back/Log/Save/Load 等快捷按钮的专用安全区。 */
    padding: clamp(50px, 6vh, 68px) 0 calc(72px + env(safe-area-inset-bottom, 0px)) !important;
    box-sizing: border-box !important;
    overflow: hidden !important;

    border: none !important;
    border-radius: 0 !important;

    background:
        linear-gradient(
            180deg,
            transparent 0%,
            rgba(8, 9, 11, 0.06) 9%,
            rgba(8, 9, 11, 0.24) 25%,
            rgba(7, 8, 10, 0.52) 47%,
            rgba(5, 6, 8, 0.78) 72%,
            rgba(3, 4, 5, 0.94) 100%
        ) !important;

    box-shadow: none !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
    /* 不向下平移，避免长字幕滚动区侵入底部快捷按钮区域。 */
    transform: none !important;
}

/* 水平柔光只用于增强电影层次，不形成边框。 */
body.ui-mode-other [data-component="text-box"]::before {
    content: "" !important;
    position: absolute !important;
    z-index: 0 !important;
    inset: 0 !important;

    display: block !important;
    pointer-events: none !important;

    background:
        radial-gradient(
            ellipse at 50% 100%,
            rgba(255, 255, 255, 0.055) 0%,
            rgba(255, 255, 255, 0.018) 32%,
            transparent 67%
        ),
        linear-gradient(
            90deg,
            rgba(0, 0, 0, 0.18) 0%,
            transparent 16%,
            transparent 84%,
            rgba(0, 0, 0, 0.18) 100%
        ) !important;
}

/* 极细灰白过渡线，位置固定且不会包围正文。 */
body.ui-mode-other [data-component="text-box"]::after {
    content: "" !important;
    position: absolute !important;
    z-index: 1 !important;
    top: 34% !important;
    left: 50% !important;

    display: block !important;
    width: min(86vw, 1560px) !important;
    height: 1px !important;

    opacity: 0.34 !important;
    pointer-events: none !important;

    background: linear-gradient(
        90deg,
        transparent 0%,
        rgba(255, 255, 255, 0.04) 14%,
        rgba(255, 255, 255, 0.16) 50%,
        rgba(255, 255, 255, 0.04) 86%,
        transparent 100%
    ) !important;

    transform: translateX(-50%) !important;
}

/* 名称与正文共享同一条固定排版轴。 */
body.ui-mode-other [data-component="text-box"] [data-content="name"],
body.ui-mode-other [data-component="text-box"] [data-content="text"] {
    position: relative !important;
    z-index: 2 !important;

    grid-column: 1 !important;

    width: min(84vw, 1520px) !important;
    max-width: none !important;
    height: auto !important;
    max-height: none !important;

    margin: 0 auto !important;
    padding: 0 !important;

    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
}

body.ui-mode-other [data-component="text-box"] [data-content="name"] {
    grid-row: 1 !important;
    align-self: start !important;
    overflow: hidden !important;
}

body.ui-mode-other [data-component="text-box"] [data-content="text"] {
    grid-row: 2 !important;
    /* 关键：正文必须填满网格第二行，超出部分在正文自身滚动。
       如果使用 start + auto，高文本会把 text-box 撑出按钮安全区。 */
    align-self: stretch !important;

    display: block !important;
    min-height: 0 !important;
    height: 100% !important;
    max-height: 100% !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;
}

body.ui-mode-other [data-component="text-box"] [data-content="side-image"] {
    display: none !important;
}

/* 人物名称固定在第一行顶部，不受正文行数影响。 */
body.ui-mode-other [data-component="text-box"] [data-ui="who"] {
    position: relative !important;

    display: inline-flex !important;
    align-items: center !important;
    gap: 11px !important;

    width: auto !important;
    min-width: 0 !important;
    min-height: 32px !important;

    margin: 0 !important;
    padding: 0 !important;

    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(255, 255, 255, 0.9) !important;

    font-size: clamp(17px, 1.06vw, 21px) !important;
    font-weight: 600 !important;
    line-height: 32px !important;
    letter-spacing: 0.085em !important;

    text-shadow:
        0 1px 8px rgba(0, 0, 0, 0.88),
        0 0 14px rgba(255, 255, 255, 0.06) !important;
}

body.ui-mode-other [data-component="text-box"] [data-ui="who"]::before {
    content: "" !important;
    position: static !important;

    display: block !important;
    width: 26px !important;
    height: 1px !important;
    flex: 0 0 26px !important;

    background: linear-gradient(
        90deg,
        rgba(255, 255, 255, 0.8),
        rgba(255, 255, 255, 0.16)
    ) !important;
    box-shadow: none !important;
    transform: none !important;
}

body.ui-mode-other [data-component="text-box"] [data-ui="who"].speaker-empty,
body.ui-mode-other [data-component="text-box"] [data-ui="who"][hidden] {
    display: none !important;
}

/* 旁白不显示名字，但保留固定名称行，正文位置不会跳动。 */
body.ui-mode-other [data-component="text-box"] [data-content="name"].speaker-empty {
    display: block !important;
    visibility: hidden !important;
}

body.ui-mode-other [data-component="text-box"] [data-content="dialog"],
body.ui-mode-other [data-component="text-box"] p {
    display: block !important;
    width: 100% !important;
    height: auto !important;
    max-height: none !important;

    margin: 0 !important;
    padding: 0 !important;
    overflow: visible !important;

    color: rgba(250, 250, 250, 0.95) !important;

    font-size: clamp(20px, 1.38vw, 29px) !important;
    font-weight: 400 !important;
    line-height: 1.55 !important;
    letter-spacing: 0.012em !important;

    text-align: left !important;
    text-wrap: pretty;
    overflow-wrap: anywhere;
    word-break: normal;

    text-shadow:
        0 1px 3px rgba(0, 0, 0, 0.96),
        0 4px 16px rgba(0, 0, 0, 0.58) !important;
}

body.ui-mode-other [data-component="text-box"] [data-content="text"]::-webkit-scrollbar {
    width: 3px;
}

body.ui-mode-other [data-component="text-box"] [data-content="text"]::-webkit-scrollbar-track {
    background: transparent;
}

body.ui-mode-other [data-component="text-box"] [data-content="text"]::-webkit-scrollbar-thumb {
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.16);
}

/* ==================== 电影字幕快捷菜单 ==================== */

/* 快捷菜单直接融入底部暗场，不再使用独立框架。 */
body.ui-mode-other [data-component="quick-menu"] {
    position: absolute !important;
    z-index: 100 !important;

    right: clamp(18px, 2.8vw, 52px) !important;
    bottom: 12px !important;
    left: auto !important;

    display: flex !important;
    align-items: center !important;
    justify-content: flex-end !important;
    flex-wrap: nowrap !important;
    gap: clamp(2px, 0.35vw, 7px) !important;

    width: auto !important;
    max-width: calc(100vw - 36px) !important;
    height: 36px !important;

    margin: 0 !important;
    padding: 0 !important;
    overflow-x: auto !important;
    overflow-y: hidden !important;

    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
}

/* 主页面不显示 Monogatari 的 Hide/Show 快捷按钮；显示/隐藏快捷菜单
   统一从 Settings 中操作。该规则必须位于 Other 分支（此分支会提前
   return），不能只放在 Ancient 样式尾部。 */
body.ui-mode-other [data-component="quick-menu"] [data-action="distraction-free"],
body.ui-mode-other quick-menu [data-action="distraction-free"],
body.ui-mode-other [data-component="quick-menu"] .is-distraction-free-hidden,
body.ui-mode-other quick-menu .is-distraction-free-hidden,
body.ui-mode-other [data-component="quick-menu"] button:has([data-string="Hide"]),
body.ui-mode-other [data-component="quick-menu"] button:has([data-string="Show"]),
body.ui-mode-other quick-menu button:has([data-string="Hide"]),
body.ui-mode-other quick-menu button:has([data-string="Show"]),
body.ui-mode-other [data-component="quick-menu"] button:has(.fa-eye),
body.ui-mode-other [data-component="quick-menu"] button:has(.fa-eye-slash),
body.ui-mode-other quick-menu button:has(.fa-eye),
body.ui-mode-other quick-menu button:has(.fa-eye-slash) {
    display: none !important;
}

body.ui-mode-other [data-component="quick-menu"] button {
    position: relative !important;

    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 5px !important;

    width: auto !important;
    min-width: 0 !important;
    height: 32px !important;
    min-height: 0 !important;

    margin: 0 !important;
    padding: 0 5px !important;

    border: none !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(238, 238, 238, 0.54) !important;

    font-size: clamp(12px, 0.74vw, 14px) !important;
    font-weight: 400 !important;
    line-height: 1 !important;
    letter-spacing: 0.015em !important;

    transform: none !important;

    transition:
        color 180ms ease,
        text-shadow 180ms ease,
        transform 180ms ease !important;
}

body.ui-mode-other [data-component="quick-menu"] button::before {
    content: none !important;
    display: none !important;
}

body.ui-mode-other [data-component="quick-menu"] button::after {
    content: "" !important;
    position: absolute !important;
    right: 5px !important;
    bottom: 2px !important;
    left: 5px !important;

    display: block !important;
    height: 1px !important;

    opacity: 0 !important;
    pointer-events: none !important;

    background: linear-gradient(
        90deg,
        transparent,
        rgba(255, 255, 255, 0.55),
        transparent
    ) !important;

    transform: scaleX(0.3) !important;
    transform-origin: center !important;

    transition:
        opacity 180ms ease,
        transform 220ms cubic-bezier(0.22, 1, 0.36, 1) !important;
}

body.ui-mode-other [data-component="quick-menu"] button:hover,
body.ui-mode-other [data-component="quick-menu"] button:focus-visible {
    color: rgba(255, 255, 255, 0.96) !important;
    background: transparent !important;
    text-shadow:
        0 0 7px rgba(255, 255, 255, 0.16),
        0 2px 9px rgba(0, 0, 0, 0.82) !important;
    transform: translateY(-1px) !important;
}

body.ui-mode-other [data-component="quick-menu"] button:hover::after,
body.ui-mode-other [data-component="quick-menu"] button:focus-visible::after {
    opacity: 0.7 !important;
    transform: scaleX(1) !important;
}

body.ui-mode-other [data-component="quick-menu"] button svg {
    width: 11px !important;
    height: 11px !important;
    opacity: 0.7 !important;
}

body.ui-mode-other [data-component="quick-menu"] button span {
    color: inherit !important;
    font: inherit !important;
}

body.ui-mode-other [data-component="quick-menu"] button:disabled,
body.ui-mode-other [data-component="quick-menu"] button[disabled] {
    opacity: 0.22 !important;
    pointer-events: none !important;
    cursor: not-allowed !important;
    transform: none !important;
}

/* ==================== ADV 响应式 ==================== */

@media (max-width: 800px) {
    body.ui-mode-other [data-component="text-box"] {
        grid-template-rows: 28px minmax(0, 1fr) !important;
        row-gap: 7px !important;

        height: 290px !important;
        padding: 44px 0 calc(72px + env(safe-area-inset-bottom, 0px)) !important;
    }

    body.ui-mode-other [data-component="text-box"]::after {
        top: 31% !important;
        width: 90vw !important;
    }

    body.ui-mode-other [data-component="text-box"] [data-content="name"],
    body.ui-mode-other [data-component="text-box"] [data-content="text"] {
        width: 88vw !important;
    }

    body.ui-mode-other [data-component="text-box"] [data-ui="who"] {
        min-height: 28px !important;
        gap: 8px !important;
        font-size: clamp(15px, 2.5vw, 18px) !important;
        line-height: 28px !important;
    }

    body.ui-mode-other [data-component="text-box"] [data-ui="who"]::before {
        width: 20px !important;
        flex-basis: 20px !important;
    }

    body.ui-mode-other [data-component="text-box"] [data-content="dialog"],
    body.ui-mode-other [data-component="text-box"] p {
        font-size: clamp(17px, 3vw, 21px) !important;
        line-height: 1.5 !important;
    }

    body.ui-mode-other [data-component="quick-menu"] {
        right: 8px !important;
        bottom: 7px !important;

        width: calc(100vw - 16px) !important;
        max-width: none !important;
        height: 34px !important;

        justify-content: center !important;
    }

    body.ui-mode-other [data-component="quick-menu"] button {
        height: 30px !important;
        padding: 0 4px !important;
        font-size: clamp(10px, 1.8vw, 12px) !important;
    }
}

@media (orientation: landscape) and (max-height: 600px) {
    body.ui-mode-other [data-component="text-box"] {
        grid-template-rows: 27px minmax(0, 1fr) !important;
        row-gap: 6px !important;

        height: clamp(225px, 42vh, 270px) !important;
        padding: 36px 0 calc(72px + env(safe-area-inset-bottom, 0px)) !important;
    }

    body.ui-mode-other [data-component="text-box"]::after {
        top: 29% !important;
    }

    body.ui-mode-other [data-component="text-box"] [data-content="name"],
    body.ui-mode-other [data-component="text-box"] [data-content="text"] {
        width: min(86vw, 1180px) !important;
    }

    body.ui-mode-other [data-component="text-box"] [data-ui="who"] {
        min-height: 27px !important;
        font-size: clamp(14px, 2.3vh, 17px) !important;
        line-height: 27px !important;
    }

    body.ui-mode-other [data-component="text-box"] [data-content="dialog"],
    body.ui-mode-other [data-component="text-box"] p {
        font-size: clamp(16px, 2.8vh, 20px) !important;
        line-height: 1.46 !important;
    }

    body.ui-mode-other [data-component="quick-menu"] {
        bottom: 5px !important;
        height: 32px !important;
    }

    body.ui-mode-other [data-component="quick-menu"] button {
        height: 28px !important;
        font-size: clamp(10px, 2vh, 12px) !important;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other [data-component="quick-menu"] button,
    body.ui-mode-other [data-component="quick-menu"] button::after {
        transition-duration: 1ms !important;
    }
}

/* ==================== 轻熟电影感剧情选项 ==================== */

/* 选择时隐藏常驻游戏 UI，让画面与选项成为唯一视觉焦点。 */
body.ui-mode-other:has(choice-container) [data-component="text-box"],
body.ui-mode-other:has([data-component="choice-container"]) [data-component="text-box"],
body.ui-mode-other:has(choice-container) [data-component="quick-menu"],
body.ui-mode-other:has([data-component="choice-container"]) [data-component="quick-menu"],
body.ui-mode-other:has(choice-container) #stats-toggle,
body.ui-mode-other:has([data-component="choice-container"]) #stats-toggle,
body.ui-mode-other:has(choice-container) #system-toggle,
body.ui-mode-other:has([data-component="choice-container"]) #system-toggle {
    opacity: 0 !important;
    visibility: hidden !important;
    pointer-events: none !important;
}

/* 全屏仅做柔和暗角，不生成弹窗框架。 */
body.ui-mode-other choice-container,
body.ui-mode-other [data-component="choice-container"] {
    position: fixed !important;
    z-index: 30 !important;
    inset: 0 !important;

    display: flex !important;
    align-items: center !important;
    justify-content: center !important;

    width: 100vw !important;
    max-width: none !important;
    height: 100vh !important;
    max-height: none !important;

    margin: 0 !important;
    padding: clamp(40px, 7vh, 82px) clamp(24px, 6vw, 96px) !important;
    box-sizing: border-box !important;
    overflow: hidden !important;

    border: none !important;
    border-radius: 0 !important;

    background:
        radial-gradient(
            ellipse at center,
            rgba(0, 0, 0, 0.015) 0%,
            rgba(0, 0, 0, 0.08) 45%,
            rgba(0, 0, 0, 0.34) 100%
        ),
        linear-gradient(
            180deg,
            rgba(0, 0, 0, 0.045),
            rgba(0, 0, 0, 0.14)
        ) !important;

    box-shadow: none !important;
    backdrop-filter: brightness(0.76) saturate(0.84) !important;
    -webkit-backdrop-filter: brightness(0.76) saturate(0.84) !important;
    transform: none !important;

    animation: otherMatureChoiceLayer 320ms ease both !important;
}

body.ui-mode-other choice-container [data-content="wrapper"],
body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"] {
    position: absolute !important;
    z-index: 2 !important;
    top: 65% !important;
    left: 50% !important;
    transform: translate(-50%, -50%) !important;

    display: grid !important;
    grid-template-columns: minmax(0, 1fr) !important;
    grid-auto-rows: auto !important;
    align-content: center !important;
    gap: clamp(18px, 2.5vh, 30px) !important;

    width: min(82vw, 1200px) !important;
    max-width: none !important;
    max-height: min(64vh, 720px) !important;

    margin: 0 !important;
    padding: 18px 0 !important;
    box-sizing: border-box !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;

    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-other choice-container [data-content="wrapper"]:has(> button:only-child),
body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"]:has(> button:only-child) {
    width: min(76vw, 900px) !important;
}

body.ui-mode-other choice-container [data-content="wrapper"]:has(> button:nth-child(4)),
body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"]:has(> button:nth-child(4)) {
    gap: 10px !important;
    padding-top: 10px !important;
    padding-bottom: 10px !important;
}

/*
 * 银幕暗幕选项：无编号、无传统卡片外框，以两端渐隐的电影字幕条表达可交互性。
 */
body.ui-mode-other choice-container button,
body.ui-mode-other [data-component="choice-container"] button,
body.ui-mode-other choice-container [data-choice],
body.ui-mode-other [data-component="choice-container"] [data-choice] {
    position: relative !important;
    isolation: isolate;

    display: flex !important;
    align-items: center !important;
    justify-content: center !important;

    width: 100% !important;
    min-width: 0 !important;
    min-height: 64px !important;
    height: auto !important;

    margin: 0 !important;
    padding: 16px clamp(28px, 5vw, 76px) !important;
    box-sizing: border-box !important;
    overflow: visible !important;

    border: none !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(244, 243, 240, 0.9) !important;

    font-size: clamp(21px, 1.48vw, 30px) !important;
    font-weight: 400 !important;
    line-height: 1.45 !important;
    letter-spacing: 0.025em !important;

    text-align: center !important;
    text-transform: none !important;
    text-wrap: balance;
    overflow-wrap: anywhere;
    word-break: normal;

    cursor: pointer !important;
    text-shadow:
        0 2px 4px rgba(0, 0, 0, 0.96),
        0 6px 20px rgba(0, 0, 0, 0.62) !important;

    transform: translateY(0) scale(1) !important;

    transition: none !important;

    animation: otherMatureChoiceItem 440ms both !important;
}

/* 默认可见的横向暗幕：两端溶入画面，不形成普通矩形卡片。 */
body.ui-mode-other choice-container button::before,
body.ui-mode-other [data-component="choice-container"] button::before,
body.ui-mode-other choice-container [data-choice]::before,
body.ui-mode-other [data-component="choice-container"] [data-choice]::before {
    content: "" !important;
    position: absolute !important;
    z-index: -2 !important;
    top: 50% !important;
    left: 50% !important;

    display: block !important;
    width: min(100%, 1120px) !important;
    height: calc(100% + 8px) !important;

    opacity: 0.96;
    pointer-events: none !important;

    background:
        linear-gradient(
            180deg,
            rgba(18, 18, 20, 0.72) 0%,
            rgba(5, 5, 6, 0.94) 26%,
            rgba(1, 1, 2, 0.97) 72%,
            rgba(15, 15, 17, 0.7) 100%
        ),
        linear-gradient(
            90deg,
            transparent 0%,
            rgba(0, 0, 0, 0.7) 9%,
            rgba(5, 5, 6, 0.92) 24%,
            rgba(1, 1, 2, 0.98) 50%,
            rgba(5, 5, 6, 0.92) 76%,
            rgba(0, 0, 0, 0.7) 91%,
            transparent 100%
        );

    background-size: 100% 100%, 100% 100%;
    background-position: 50% 50%, 50% 50%;
    background-repeat: no-repeat;

    -webkit-mask-image: linear-gradient(90deg, transparent, #000 8%, #000 92%, transparent);
    mask-image: linear-gradient(90deg, transparent, #000 8%, #000 92%, transparent);
    backdrop-filter: blur(8px) brightness(0.68) saturate(0.7) !important;
    -webkit-backdrop-filter: blur(8px) brightness(0.68) saturate(0.7) !important;
    filter: drop-shadow(0 9px 24px rgba(0, 0, 0, 0.34));
    transform: translate(-50%, -50%) scaleX(0.985);
    transform-origin: center !important;

    transition: none !important;
}

/* 小星芒与短流星：默认完全隐藏，鼠标进入时连续掠过一次。 */
body.ui-mode-other choice-container button::after,
body.ui-mode-other [data-component="choice-container"] button::after,
body.ui-mode-other choice-container [data-choice]::after,
body.ui-mode-other [data-component="choice-container"] [data-choice]::after {
    content: "" !important;
    position: absolute !important;
    z-index: -1 !important;
    top: 50% !important;
    left: 50% !important;

    display: block !important;
    width: min(94%, 1040px) !important;
    height: calc(100% - 2px) !important;

    opacity: 0;
    pointer-events: none !important;

    background:
        linear-gradient(90deg, transparent, rgba(255, 253, 248, 0.96), transparent)
            20% 38% / 0 1px no-repeat,
        linear-gradient(0deg, transparent, rgba(255, 253, 248, 0.96), transparent)
            20% 38% / 1px 0 no-repeat,
        linear-gradient(90deg, transparent, rgba(255, 255, 255, 1), transparent)
            50% 50% / 0 1px no-repeat,
        linear-gradient(0deg, transparent, rgba(255, 255, 255, 1), transparent)
            50% 50% / 1px 0 no-repeat,
        linear-gradient(90deg, transparent, rgba(238, 238, 238, 0.94), transparent)
            80% 62% / 0 1px no-repeat,
        linear-gradient(0deg, transparent, rgba(238, 238, 238, 0.94), transparent)
            80% 62% / 1px 0 no-repeat,
        linear-gradient(
            155deg,
            transparent 0%,
            rgba(255, 255, 255, 0.02) 32%,
            rgba(255, 255, 255, 0.82) 49%,
            rgba(255, 255, 255, 0.08) 54%,
            transparent 62%
        ) -10% 78% / 0 0 no-repeat,
        linear-gradient(
            155deg,
            transparent 0%,
            rgba(255, 255, 255, 0.02) 32%,
            rgba(255, 255, 255, 0.76) 49%,
            rgba(255, 255, 255, 0.07) 54%,
            transparent 62%
        ) -14% 30% / 0 0 no-repeat,
        linear-gradient(
            155deg,
            transparent 0%,
            rgba(255, 255, 255, 0.02) 32%,
            rgba(255, 255, 255, 0.7) 49%,
            rgba(255, 255, 255, 0.06) 54%,
            transparent 62%
        ) -18% 64% / 0 0 no-repeat;

    filter: drop-shadow(0 0 7px rgba(255, 255, 255, 0.14));
    transform: translate(-50%, -50%);
    transform-origin: center !important;

    transition: none !important;
}

/* 鼠标进入时播放一次；停留不循环，移出后再进入才重新播放。 */
body.ui-mode-other choice-container button:hover::after,
body.ui-mode-other choice-container button:focus-visible::after,
body.ui-mode-other choice-container button:active::after,
body.ui-mode-other [data-component="choice-container"] button:hover::after,
body.ui-mode-other [data-component="choice-container"] button:focus-visible::after,
body.ui-mode-other [data-component="choice-container"] button:active::after,
body.ui-mode-other choice-container [data-choice]:hover::after,
body.ui-mode-other choice-container [data-choice]:focus-visible::after,
body.ui-mode-other choice-container [data-choice]:active::after,
body.ui-mode-other [data-component="choice-container"] [data-choice]:hover::after,
body.ui-mode-other [data-component="choice-container"] [data-choice]:focus-visible::after,
body.ui-mode-other [data-component="choice-container"] [data-choice]:active::after {
    animation: otherChoiceHoverMeteors 1080ms linear both !important;
}

body.ui-mode-other choice-container button:active,
body.ui-mode-other [data-component="choice-container"] button:active,
body.ui-mode-other choice-container [data-choice]:active,
body.ui-mode-other [data-component="choice-container"] [data-choice]:active {
    transform: translateY(0) scale(0.998) !important;
}

body.ui-mode-other choice-container button:nth-child(2),
body.ui-mode-other [data-component="choice-container"] button:nth-child(2) {
    animation-delay: 65ms !important;
}

body.ui-mode-other choice-container button:nth-child(3),
body.ui-mode-other [data-component="choice-container"] button:nth-child(3) {
    animation-delay: 130ms !important;
}

body.ui-mode-other choice-container button:nth-child(4),
body.ui-mode-other [data-component="choice-container"] button:nth-child(4) {
    animation-delay: 195ms !important;
}

body.ui-mode-other choice-container button:nth-child(n+5),
body.ui-mode-other [data-component="choice-container"] button:nth-child(n+5) {
    animation-delay: 230ms !important;
}

body.ui-mode-other choice-container button:disabled,
body.ui-mode-other choice-container button[disabled],
body.ui-mode-other [data-component="choice-container"] button:disabled,
body.ui-mode-other [data-component="choice-container"] button[disabled] {
    opacity: 0.28 !important;
    pointer-events: none !important;
    cursor: not-allowed !important;
    transform: none !important;
}

body.ui-mode-other choice-container [data-content="wrapper"]::-webkit-scrollbar,
body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"]::-webkit-scrollbar {
    width: 3px;
}

body.ui-mode-other choice-container [data-content="wrapper"]::-webkit-scrollbar-track,
body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"]::-webkit-scrollbar-track {
    background: transparent;
}

body.ui-mode-other choice-container [data-content="wrapper"]::-webkit-scrollbar-thumb,
body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"]::-webkit-scrollbar-thumb {
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.14);
}

@keyframes otherMatureChoiceLayer {
    from {
        opacity: 0;
    }

    to {
        opacity: 1;
    }
}

@keyframes otherMatureChoiceItem {
    from {
        opacity: 0;
        transform: translateY(9px);
        filter: blur(2px);
    }

    to {
        opacity: 1;
        transform: translateY(0);
        filter: blur(0);
    }
}

@keyframes otherChoiceHoverMeteors {
    0% {
        opacity: 0;
        background-size:
            0 1px, 1px 0,
            0 1px, 1px 0,
            0 1px, 1px 0,
            62px 18px, 0 0, 0 0;
        background-position:
            20% 38%, 20% 38%,
            50% 50%, 50% 50%,
            80% 62%, 80% 62%,
            -10% 78%, -14% 30%, -18% 64%;
        filter: drop-shadow(0 0 4px rgba(255, 255, 255, 0.08));
    }

    8% {
        opacity: 0.9;
        background-size:
            0 1px, 1px 0,
            0 1px, 1px 0,
            0 1px, 1px 0,
            68px 20px, 54px 16px, 0 0;
        background-position:
            20% 38%, 20% 38%,
            50% 50%, 50% 50%,
            80% 62%, 80% 62%,
            4% 78%, -8% 30%, -18% 64%;
    }

    24% {
        opacity: 1;
        background-size:
            14px 1px, 1px 14px,
            0 1px, 1px 0,
            0 1px, 1px 0,
            70px 20px, 64px 18px, 52px 15px;
        background-position:
            20% 38%, 20% 38%,
            50% 50%, 50% 50%,
            80% 62%, 80% 62%,
            34% 78%, 12% 30%, -3% 64%;
        filter: drop-shadow(0 0 8px rgba(255, 255, 255, 0.28));
    }

    42% {
        background-size:
            0 1px, 1px 0,
            14px 1px, 1px 14px,
            0 1px, 1px 0,
            68px 20px, 66px 19px, 60px 17px;
        background-position:
            20% 38%, 20% 38%,
            50% 50%, 50% 50%,
            80% 62%, 80% 62%,
            62% 78%, 39% 30%, 18% 64%;
        filter: drop-shadow(0 0 9px rgba(255, 255, 255, 0.3));
    }

    60% {
        opacity: 0.96;
        background-size:
            0 1px, 1px 0,
            0 1px, 1px 0,
            13px 1px, 1px 13px,
            60px 18px, 66px 19px, 64px 18px;
        background-position:
            20% 38%, 20% 38%,
            50% 50%, 50% 50%,
            80% 62%, 80% 62%,
            91% 78%, 66% 30%, 46% 64%;
        filter: drop-shadow(0 0 8px rgba(255, 255, 255, 0.26));
    }

    78% {
        opacity: 0.86;
        background-size:
            0 1px, 1px 0,
            0 1px, 1px 0,
            0 1px, 1px 0,
            0 0, 60px 18px, 64px 18px;
        background-position:
            20% 38%, 20% 38%,
            50% 50%, 50% 50%,
            80% 62%, 80% 62%,
            110% 78%, 93% 30%, 73% 64%;
        filter: drop-shadow(0 0 6px rgba(255, 255, 255, 0.2));
    }

    100% {
        opacity: 0;
        background-size:
            0 1px, 1px 0,
            0 1px, 1px 0,
            0 1px, 1px 0,
            0 0, 0 0, 0 0;
        background-position:
            20% 38%, 20% 38%,
            50% 50%, 50% 50%,
            80% 62%, 80% 62%,
            110% 78%, 110% 30%, 104% 64%;
        filter: drop-shadow(0 0 3px rgba(255, 255, 255, 0.06));
    }
}

/* ==================== 轻熟选项响应式 ==================== */

@media (max-width: 800px) {
    body.ui-mode-other choice-container,
    body.ui-mode-other [data-component="choice-container"] {
        padding: 30px 16px !important;
    }

    body.ui-mode-other choice-container [data-content="wrapper"],
    body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"],
    body.ui-mode-other choice-container [data-content="wrapper"]:has(> button:only-child),
    body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"]:has(> button:only-child) {
        width: 90vw !important;
        max-height: 64vh !important;
        gap: 12px !important;
        padding: 10px 0 !important;
    }

    body.ui-mode-other choice-container button,
    body.ui-mode-other [data-component="choice-container"] button,
    body.ui-mode-other choice-container [data-choice],
    body.ui-mode-other [data-component="choice-container"] [data-choice] {
        min-height: 56px !important;
        padding: 13px 22px !important;
        font-size: clamp(18px, 3.2vw, 23px) !important;
        line-height: 1.4 !important;
    }

    body.ui-mode-other choice-container button::before,
    body.ui-mode-other [data-component="choice-container"] button::before,
    body.ui-mode-other choice-container [data-choice]::before,
    body.ui-mode-other [data-component="choice-container"] [data-choice]::before {
        width: 100% !important;
        height: calc(100% + 6px) !important;
    }

    body.ui-mode-other choice-container button::after,
    body.ui-mode-other [data-component="choice-container"] button::after,
    body.ui-mode-other choice-container [data-choice]::after,
    body.ui-mode-other [data-component="choice-container"] [data-choice]::after {
        width: 94% !important;
    }
}

@media (orientation: landscape) and (max-height: 600px) {
    body.ui-mode-other choice-container,
    body.ui-mode-other [data-component="choice-container"] {
        padding: 22px 24px !important;
    }

    body.ui-mode-other choice-container [data-content="wrapper"],
    body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"] {
        width: min(84vw, 1040px) !important;
        max-height: 64vh !important;
        gap: 9px !important;
        padding: 8px 0 !important;
    }

    body.ui-mode-other choice-container button,
    body.ui-mode-other [data-component="choice-container"] button,
    body.ui-mode-other choice-container [data-choice],
    body.ui-mode-other [data-component="choice-container"] [data-choice] {
        min-height: 49px !important;
        padding-top: 10px !important;
        padding-bottom: 10px !important;
        font-size: clamp(16px, 2.9vh, 21px) !important;
        line-height: 1.36 !important;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other choice-container,
    body.ui-mode-other [data-component="choice-container"],
    body.ui-mode-other choice-container button,
    body.ui-mode-other [data-component="choice-container"] button,
    body.ui-mode-other choice-container [data-choice],
    body.ui-mode-other [data-component="choice-container"] [data-choice],
    body.ui-mode-other choice-container button::before,
    body.ui-mode-other choice-container button::after,
    body.ui-mode-other [data-component="choice-container"] button::before,
    body.ui-mode-other [data-component="choice-container"] button::after {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}
/* 回退至截图中的半透明电影暗幕风格。
 * 请放在现有选项 CSS 最后，覆盖之前的白色渐变与星芒效果。
 */

body.ui-mode-other choice-container [data-content="wrapper"],
body.ui-mode-other [data-component="choice-container"] [data-content="wrapper"] {
    gap: clamp(12px, 1.8vh, 20px) !important;
}

/* 选项文字 */
body.ui-mode-other choice-container button,
body.ui-mode-other [data-component="choice-container"] button,
body.ui-mode-other choice-container [data-choice],
body.ui-mode-other [data-component="choice-container"] [data-choice] {
    min-height: 72px !important;
    padding: 16px clamp(28px, 5vw, 72px) !important;

    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(241, 241, 243, 0.92) !important;

    font-size: clamp(20px, 1.5vw, 28px) !important;
    font-weight: 400 !important;
    line-height: 1.45 !important;
    letter-spacing: 0.018em !important;

    text-shadow:
        0 2px 5px rgba(0, 0, 0, 0.96),
        0 5px 18px rgba(0, 0, 0, 0.62) !important;

    transition:
        color 220ms ease,
        text-shadow 220ms ease !important;
}

/* 截图中的半透明黑灰电影暗幕 */
body.ui-mode-other choice-container button::before,
body.ui-mode-other [data-component="choice-container"] button::before,
body.ui-mode-other choice-container [data-choice]::before,
body.ui-mode-other [data-component="choice-container"] [data-choice]::before {
    content: "" !important;
    display: block !important;

    position: absolute !important;
    z-index: -2 !important;
    top: 50% !important;
    left: 50% !important;

    width: min(100%, 1120px) !important;
    height: calc(100% + 8px) !important;

    opacity: 0.88 !important;
    pointer-events: none !important;

    background:
        /* 中央柔和灰色电影光 */
        radial-gradient(
            ellipse at center,
            rgba(255, 255, 255, 0.095) 0%,
            rgba(166, 166, 170, 0.11) 28%,
            rgba(54, 54, 58, 0.29) 56%,
            transparent 82%
        ),

        /* 半透明黑色暗幕 */
        linear-gradient(
            180deg,
            rgba(41, 41, 45, 0.46) 0%,
            rgba(15, 15, 18, 0.67) 20%,
            rgba(3, 3, 5, 0.82) 50%,
            rgba(14, 14, 17, 0.69) 80%,
            rgba(40, 40, 44, 0.43) 100%
        ),

        /* 左右渐隐 */
        linear-gradient(
            90deg,
            transparent 0%,
            rgba(35, 35, 39, 0.12) 5%,
            rgba(19, 19, 22, 0.55) 15%,
            rgba(7, 7, 9, 0.76) 28%,
            rgba(2, 2, 4, 0.83) 50%,
            rgba(7, 7, 9, 0.76) 72%,
            rgba(19, 19, 22, 0.55) 85%,
            rgba(35, 35, 39, 0.12) 95%,
            transparent 100%
        ) !important;

    background-size: 100% 100% !important;
    background-position: center !important;
    background-repeat: no-repeat !important;

    -webkit-mask-image: linear-gradient(
        90deg,
        transparent 0%,
        rgba(0, 0, 0, 0.62) 7%,
        #000 15%,
        #000 85%,
        rgba(0, 0, 0, 0.62) 93%,
        transparent 100%
    ) !important;

    mask-image: linear-gradient(
        90deg,
        transparent 0%,
        rgba(0, 0, 0, 0.62) 7%,
        #000 15%,
        #000 85%,
        rgba(0, 0, 0, 0.62) 93%,
        transparent 100%
    ) !important;

    backdrop-filter:
        blur(8px)
        brightness(0.82)
        saturate(0.76) !important;

    -webkit-backdrop-filter:
        blur(8px)
        brightness(0.82)
        saturate(0.76) !important;

    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;

    filter:
        drop-shadow(0 8px 22px rgba(0, 0, 0, 0.2)) !important;

    transform:
        translate(-50%, -50%)
        scaleX(0.985) !important;

    animation: none !important;

    transition:
        opacity 220ms ease,
        transform 300ms cubic-bezier(0.16, 1, 0.3, 1),
        filter 260ms ease !important;
}

/* 完全取消星芒、白色曝光和其他悬停装饰 */
body.ui-mode-other choice-container button::after,
body.ui-mode-other [data-component="choice-container"] button::after,
body.ui-mode-other choice-container [data-choice]::after,
body.ui-mode-other [data-component="choice-container"] [data-choice]::after {
    content: none !important;
    display: none !important;
    opacity: 0 !important;
    background: none !important;
    animation: none !important;
}

/* 悬停仅做克制的亮度变化 */
body.ui-mode-other choice-container button:hover::before,
body.ui-mode-other choice-container button:focus-visible::before,
body.ui-mode-other [data-component="choice-container"] button:hover::before,
body.ui-mode-other [data-component="choice-container"] button:focus-visible::before,
body.ui-mode-other choice-container [data-choice]:hover::before,
body.ui-mode-other choice-container [data-choice]:focus-visible::before,
body.ui-mode-other [data-component="choice-container"] [data-choice]:hover::before,
body.ui-mode-other [data-component="choice-container"] [data-choice]:focus-visible::before {
    opacity: 0.97 !important;

    filter:
        brightness(1.08)
        drop-shadow(0 9px 25px rgba(0, 0, 0, 0.26)) !important;

    transform:
        translate(-50%, -50%)
        scaleX(1.006) !important;

    animation: none !important;
}

body.ui-mode-other choice-container button:hover,
body.ui-mode-other choice-container button:focus-visible,
body.ui-mode-other [data-component="choice-container"] button:hover,
body.ui-mode-other [data-component="choice-container"] button:focus-visible,
body.ui-mode-other choice-container [data-choice]:hover,
body.ui-mode-other choice-container [data-choice]:focus-visible,
body.ui-mode-other [data-component="choice-container"] [data-choice]:hover,
body.ui-mode-other [data-component="choice-container"] [data-choice]:focus-visible {
    color: rgba(255, 255, 255, 0.99) !important;

    text-shadow:
        0 0 3px rgba(255, 255, 255, 0.26),
        0 3px 8px rgba(0, 0, 0, 0.92),
        0 7px 22px rgba(0, 0, 0, 0.68) !important;
}

/* ==================== Toast 电影过场提示 ==================== */

body.ui-mode-other #custom-toast {
    position: fixed !important;
    z-index: 10000 !important;
    top: clamp(54px, 10vh, 108px) !important;
    right: auto !important;
    left: 50% !important;

    box-sizing: border-box !important;
    width: max-content !important;
    min-width: clamp(220px, 23vw, 360px) !important;
    max-width: min(82vw, 720px) !important;
    margin: 0 !important;
    padding: clamp(13px, 1.4vh, 17px) clamp(34px, 4.2vw, 68px) !important;

    border: 0 !important;
    border-radius: 0 !important;
    outline: 0 !important;
    background: linear-gradient(
        90deg,
        transparent 0,
        rgba(8, 9, 11, 0.5) 12%,
        rgba(8, 9, 11, 0.82) 32%,
        rgba(8, 9, 11, 0.82) 68%,
        rgba(8, 9, 11, 0.5) 88%,
        transparent 100%
    ) !important;
    box-shadow:
        0 12px 34px rgba(0, 0, 0, 0.3),
        0 0 28px rgba(255, 255, 255, 0.018) !important;

    color: rgba(246, 246, 247, 0.92) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter,
        "PingFang SC", "Microsoft YaHei", sans-serif !important;
    font-size: clamp(14px, 1vw, 17px) !important;
    font-style: normal !important;
    font-weight: 390 !important;
    line-height: 1.55 !important;
    letter-spacing: 0.075em !important;
    text-align: center !important;
    text-shadow: 0 3px 12px rgba(0, 0, 0, 0.84) !important;

    transform: translateX(-50%) !important;
    pointer-events: none !important;
    transition: opacity 420ms ease !important;
}

body.ui-mode-other #custom-toast::before,
body.ui-mode-other #custom-toast::after {
    content: "" !important;
    position: absolute !important;
    right: 8% !important;
    left: 8% !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(
        90deg,
        transparent,
        rgba(255, 255, 255, 0.12) 26%,
        rgba(255, 255, 255, 0.38) 50%,
        rgba(255, 255, 255, 0.12) 74%,
        transparent
    ) !important;
    filter: blur(0.2px) !important;
}

body.ui-mode-other #custom-toast::before {
    top: 0 !important;
    opacity: 0.72 !important;
}

body.ui-mode-other #custom-toast::after {
    bottom: 0 !important;
    opacity: 0.38 !important;
}

@media (max-width: 640px) {
    body.ui-mode-other #custom-toast {
        top: clamp(34px, 8vh, 62px) !important;
        width: min(86vw, 520px) !important;
        min-width: 0 !important;
        padding: 12px 28px !important;
        font-size: 13px !important;
        letter-spacing: 0.055em !important;
    }
}

/* ==================== Stats / System 电影信息层 ==================== */

/* 数值入口只保留一个细小符号，不使用圆形、方形或胶囊底。 */
body.ui-mode-other #stats-toggle {
    position: fixed !important;
    top: 18px !important;
    right: 22px !important;
    z-index: 9200 !important;

    display: grid !important;
    place-items: center !important;
    width: 38px !important;
    height: 38px !important;
    padding: 0 !important;

    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(236, 236, 238, 0.7) !important;
    font-family: "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    /* 38px 的触控区域保持不变；符号本身放大到可识别但不喧宾夺主。 */
    font-size: 21px !important;
    font-weight: 400 !important;
    line-height: 1 !important;
    text-align: center !important;
    text-shadow:
        0 2px 7px rgba(0, 0, 0, 0.96),
        0 0 14px rgba(255, 255, 255, 0.08) !important;

    cursor: pointer !important;
    user-select: none !important;
    opacity: 0.78 !important;
    filter: none !important;
    transform: translateZ(0) !important;
    transition:
        color 220ms ease,
        opacity 220ms ease,
        text-shadow 260ms ease,
        transform 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

/* 符号背后只有无边界的柔和曝光，不形成按钮轮廓。 */
body.ui-mode-other #stats-toggle::before {
    content: "" !important;
    position: absolute !important;
    inset: -12px !important;
    z-index: -1 !important;
    pointer-events: none !important;

    background: radial-gradient(
        circle at center,
        rgba(255, 255, 255, 0.14) 0%,
        rgba(255, 255, 255, 0.045) 30%,
        transparent 69%
    ) !important;

    opacity: 0 !important;
    transform: scale(0.72) !important;
    transition:
        opacity 240ms ease,
        transform 360ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other #stats-toggle::after {
    content: "" !important;
    position: absolute !important;
    top: 50% !important;
    right: 33px !important;
    width: 0 !important;
    height: 1px !important;
    pointer-events: none !important;

    background: linear-gradient(
        90deg,
        transparent,
        rgba(255, 255, 255, 0.32)
    ) !important;

    opacity: 0 !important;
    transform: translateY(-50%) !important;
    transition:
        width 340ms cubic-bezier(0.16, 1, 0.3, 1),
        opacity 220ms ease !important;
}

body.ui-mode-other #stats-toggle:hover,
body.ui-mode-other #stats-toggle:focus-visible,
body.ui-mode-other:has(#stats-panel.open) #stats-toggle {
    color: rgba(255, 255, 255, 0.98) !important;
    opacity: 1 !important;
    text-shadow:
        0 2px 8px rgba(0, 0, 0, 0.96),
        0 0 11px rgba(255, 255, 255, 0.32) !important;
    transform: scale(1.055) !important;
}

body.ui-mode-other #stats-toggle:hover::before,
body.ui-mode-other #stats-toggle:focus-visible::before,
body.ui-mode-other:has(#stats-panel.open) #stats-toggle::before {
    opacity: 1 !important;
    transform: scale(1) !important;
}

body.ui-mode-other #stats-toggle:hover::after,
body.ui-mode-other #stats-toggle:focus-visible::after,
body.ui-mode-other:has(#stats-panel.open) #stats-toggle::after {
    width: 34px !important;
    opacity: 0.72 !important;
}

/* 面板背景向画面自然消隐，没有硬边界、圆角卡片或黑色矩形。 */
body.ui-mode-other #stats-panel {
    position: fixed !important;
    top: 57px !important;
    right: 20px !important;
    z-index: 9190 !important;

    width: clamp(260px, 24vw, 348px) !important;
    min-width: 0 !important;
    max-height: min(58vh, 480px) !important;
    padding: 16px 18px 20px 24px !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;

    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;

    color: rgba(242, 242, 244, 0.9) !important;
    font-family: "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    isolation: isolate !important;
}

body.ui-mode-other #stats-panel.open {
    display: grid !important;
    gap: 0 !important;
    animation: otherStatsReveal 420ms cubic-bezier(0.16, 1, 0.3, 1) both !important;
}

body.ui-mode-other #stats-panel::before {
    content: "" !important;
    position: absolute !important;
    z-index: -2 !important;
    top: -16px !important;
    right: -20px !important;
    bottom: -12px !important;
    left: -78px !important;
    pointer-events: none !important;

    background:
        radial-gradient(
            ellipse at 100% 0%,
            rgba(255, 255, 255, 0.065) 0%,
            rgba(121, 123, 129, 0.05) 24%,
            transparent 58%
        ),
        linear-gradient(
            90deg,
            transparent 0%,
            rgba(5, 5, 7, 0.12) 16%,
            rgba(4, 4, 6, 0.58) 47%,
            rgba(2, 2, 4, 0.84) 100%
        ) !important;

    -webkit-mask-image:
        linear-gradient(90deg, transparent 0%, #000 32%, #000 100%),
        linear-gradient(180deg, #000 0%, #000 78%, transparent 100%) !important;
    -webkit-mask-composite: source-in !important;
    mask-image:
        linear-gradient(90deg, transparent 0%, #000 32%, #000 100%),
        linear-gradient(180deg, #000 0%, #000 78%, transparent 100%) !important;
    mask-composite: intersect !important;

    backdrop-filter: blur(8px) brightness(0.75) saturate(0.72) !important;
    -webkit-backdrop-filter: blur(8px) brightness(0.75) saturate(0.72) !important;
    filter: drop-shadow(-10px 16px 24px rgba(0, 0, 0, 0.16)) !important;
}

/* 右侧构图细线只负责建立视觉轴，不包围内容。 */
body.ui-mode-other #stats-panel::after {
    content: "" !important;
    position: absolute !important;
    top: 12px !important;
    right: 0 !important;
    bottom: 25px !important;
    width: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(
        180deg,
        transparent,
        rgba(255, 255, 255, 0.3) 20%,
        rgba(255, 255, 255, 0.12) 72%,
        transparent
    ) !important;
    opacity: 0.7 !important;
}

body.ui-mode-other #stats-panel .stat-row {
    position: relative !important;
    display: grid !important;
    grid-template-columns: 24px minmax(0, 1fr) auto !important;
    align-items: center !important;
    column-gap: 12px !important;
    min-height: 48px !important;
    margin: 0 !important;
    padding: 10px 4px 10px 0 !important;

    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(242, 242, 244, 0.9) !important;
    font-family: inherit !important;
    font-size: 14px !important;
    letter-spacing: 0.04em !important;
}

body.ui-mode-other #stats-panel .stat-row::after {
    content: "" !important;
    position: absolute !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 36px !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(
        90deg,
        rgba(255, 255, 255, 0.16),
        rgba(255, 255, 255, 0.055) 62%,
        transparent
    ) !important;
}

body.ui-mode-other #stats-panel .stat-row:last-child::after {
    opacity: 0 !important;
}

body.ui-mode-other #stats-panel .stat-icon {
    grid-column: 1 !important;
    align-self: center !important;
    color: rgba(255, 255, 255, 0.46) !important;
    font-family: Georgia, "Times New Roman", serif !important;
    font-size: 24px !important;
    font-weight: 400 !important;
    line-height: 1 !important;
    text-align: center !important;
    text-shadow: 0 0 9px rgba(255, 255, 255, 0.08) !important;
}

body.ui-mode-other #stats-panel .stat-label {
    grid-column: 2 !important;
    grid-row: 1 !important;
    align-self: center !important;
    min-width: 0 !important;
    margin: 0 !important;
    overflow: hidden !important;

    color: rgba(232, 232, 236, 0.72) !important;
    font-size: 14px !important;
    font-weight: 500 !important;
    line-height: 1.3 !important;
    letter-spacing: 0.085em !important;
    text-overflow: ellipsis !important;
    text-transform: uppercase !important;
    white-space: nowrap !important;
}

body.ui-mode-other #stats-panel .stat-val {
    grid-column: 3 !important;
    grid-row: 1 !important;
    align-self: center !important;
    min-width: 2.25ch !important;

    color: rgba(255, 255, 255, 0.96) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, sans-serif !important;
    font-size: 20px !important;
    font-weight: 500 !important;
    font-variant-numeric: tabular-nums !important;
    line-height: 1 !important;
    letter-spacing: 0.025em !important;
    text-align: right !important;
    text-shadow:
        0 2px 6px rgba(0, 0, 0, 0.96),
        0 0 11px rgba(255, 255, 255, 0.1) !important;
}

body.ui-mode-other #stats-panel::-webkit-scrollbar {
    width: 2px !important;
}

body.ui-mode-other #stats-panel::-webkit-scrollbar-track {
    background: transparent !important;
}

body.ui-mode-other #stats-panel::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.16) !important;
}

/* System 入口是低调的文字动作，与数值符号共用右上视觉轴。 */
body.ui-mode-other #system-toggle {
    position: fixed !important;
    top: 20px !important;
    right: 76px !important;
    z-index: 9210 !important;

    width: auto !important;
    height: 32px !important;
    padding: 0 8px 0 20px !important;

    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;

    color: rgba(226, 226, 230, 0.5) !important;
    font-family: "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    font-size: 10px !important;
    font-weight: 500 !important;
    line-height: 32px !important;
    letter-spacing: 0.18em !important;
    text-align: right !important;
    text-transform: uppercase !important;
    text-shadow: 0 2px 7px rgba(0, 0, 0, 0.92) !important;

    cursor: pointer !important;
    opacity: 0.82 !important;
    transition:
        color 220ms ease,
        opacity 220ms ease,
        letter-spacing 320ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other #system-toggle::before {
    content: "" !important;
    position: absolute !important;
    top: 50% !important;
    left: 0 !important;
    width: 12px !important;
    height: 1px !important;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.34)) !important;
    transform: translateY(-50%) !important;
    transition: width 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other #system-toggle:hover,
body.ui-mode-other #system-toggle:focus-visible {
    color: rgba(255, 255, 255, 0.92) !important;
    opacity: 1 !important;
    letter-spacing: 0.22em !important;
}

body.ui-mode-other #system-toggle:hover::before,
body.ui-mode-other #system-toggle:focus-visible::before {
    width: 17px !important;
}

/* System 使用整幅电影暗场承载文本，不在中央放置独立大面板。 */
body.ui-mode-other #system-panel-overlay {
    position: fixed !important;
    inset: 0 !important;
    z-index: 9700 !important;

    padding: clamp(64px, 8vh, 94px) clamp(34px, 9vw, 150px) clamp(48px, 8vh, 88px) !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;

    border: 0 !important;
    background:
        radial-gradient(
            ellipse at 66% 42%,
            rgba(255, 255, 255, 0.055) 0%,
            transparent 38%
        ),
        linear-gradient(
            90deg,
            rgba(2, 2, 3, 0.96) 0%,
            rgba(5, 5, 7, 0.91) 42%,
            rgba(5, 5, 7, 0.78) 70%,
            rgba(2, 2, 3, 0.88) 100%
        ) !important;
    box-shadow: none !important;
    backdrop-filter: blur(14px) brightness(0.66) saturate(0.58) !important;
    -webkit-backdrop-filter: blur(14px) brightness(0.66) saturate(0.58) !important;

    color: rgba(234, 234, 237, 0.86) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    isolation: isolate !important;
}

body.ui-mode-other #system-panel-overlay.open {
    display: block !important;
    animation: otherSystemFade 420ms ease both !important;
}

/* 上下极淡的银幕构图线，不形成完整边框。 */
body.ui-mode-other #system-panel-overlay::before,
body.ui-mode-other #system-panel-overlay::after {
    content: "" !important;
    position: fixed !important;
    right: 8vw !important;
    left: 8vw !important;
    z-index: -1 !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(
        90deg,
        transparent,
        rgba(255, 255, 255, 0.08) 20%,
        rgba(255, 255, 255, 0.16) 50%,
        rgba(255, 255, 255, 0.08) 80%,
        transparent
    ) !important;
}

body.ui-mode-other #system-panel-overlay::before {
    top: 7vh !important;
}

body.ui-mode-other #system-panel-overlay::after {
    bottom: 7vh !important;
}

body.ui-mode-other #system-panel-overlay .sys-header {
    position: relative !important;
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    width: min(100%, 1040px) !important;
    margin: 0 auto clamp(32px, 5vh, 58px) !important;
    padding: 0 0 18px !important;

    border: 0 !important;
    color: rgba(248, 248, 249, 0.88) !important;
}

body.ui-mode-other #system-panel-overlay .sys-header::after {
    content: "" !important;
    position: absolute !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 0 !important;
    height: 1px !important;
    background: linear-gradient(
        90deg,
        rgba(255, 255, 255, 0.28),
        rgba(255, 255, 255, 0.07) 48%,
        transparent 88%
    ) !important;
}

body.ui-mode-other #system-panel-overlay .sys-title {
    font-size: clamp(17px, 1.35vw, 23px) !important;
    font-weight: 360 !important;
    line-height: 1.2 !important;
    letter-spacing: 0.34em !important;
    text-transform: uppercase !important;
    text-shadow: 0 3px 12px rgba(0, 0, 0, 0.74) !important;
}

body.ui-mode-other #system-panel-overlay .sys-close {
    position: relative !important;
    width: 40px !important;
    height: 40px !important;
    padding: 0 !important;

    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    outline: none !important;

    color: rgba(232, 232, 235, 0.55) !important;
    font-family: "Inter Local", Inter, sans-serif !important;
    font-size: 25px !important;
    font-weight: 200 !important;
    line-height: 40px !important;
    text-align: center !important;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.85) !important;

    cursor: pointer !important;
    opacity: 0.8 !important;
    transition:
        color 220ms ease,
        opacity 220ms ease,
        transform 300ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other #system-panel-overlay .sys-close::before {
    content: "" !important;
    position: absolute !important;
    inset: -10px !important;
    z-index: -1 !important;
    background: radial-gradient(circle, rgba(255, 255, 255, 0.1), transparent 68%) !important;
    opacity: 0 !important;
    transform: scale(0.74) !important;
    transition:
        opacity 220ms ease,
        transform 320ms cubic-bezier(0.16, 1, 0.3, 1) !important;
}

body.ui-mode-other #system-panel-overlay .sys-close:hover,
body.ui-mode-other #system-panel-overlay .sys-close:focus-visible {
    background: transparent !important;
    color: rgba(255, 255, 255, 0.98) !important;
    opacity: 1 !important;
    transform: rotate(4deg) scale(1.04) !important;
}

body.ui-mode-other #system-panel-overlay .sys-close:hover::before,
body.ui-mode-other #system-panel-overlay .sys-close:focus-visible::before {
    opacity: 1 !important;
    transform: scale(1) !important;
}

body.ui-mode-other #system-panel-overlay .sys-body {
    position: relative !important;
    width: min(82%, 820px) !important;
    margin: 0 auto !important;
    padding: 0 0 12vh !important;

    color: rgba(231, 231, 234, 0.82) !important;
    font-size: clamp(16px, 1.18vw, 20px) !important;
    font-weight: 360 !important;
    line-height: 1.95 !important;
    letter-spacing: 0.052em !important;
    text-align: left !important;
    text-shadow:
        0 2px 7px rgba(0, 0, 0, 0.95),
        0 7px 24px rgba(0, 0, 0, 0.5) !important;
    white-space: pre-wrap !important;
}

body.ui-mode-other #system-panel-overlay .sys-body > div:not(.sys-empty) {
    animation: otherSystemTextReveal 560ms 80ms cubic-bezier(0.16, 1, 0.3, 1) both !important;
}

body.ui-mode-other #system-panel-overlay .sys-empty {
    margin: 20vh 0 0 !important;
    color: rgba(230, 230, 233, 0.35) !important;
    font-size: 12px !important;
    font-weight: 400 !important;
    letter-spacing: 0.22em !important;
    text-align: center !important;
    text-transform: uppercase !important;
}

body.ui-mode-other #system-panel-overlay::-webkit-scrollbar {
    width: 3px !important;
}

body.ui-mode-other #system-panel-overlay::-webkit-scrollbar-track {
    background: transparent !important;
}

body.ui-mode-other #system-panel-overlay::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.16) !important;
}

@keyframes otherStatsReveal {
    from {
        opacity: 0;
        transform: translate3d(12px, -4px, 0);
        filter: blur(3px);
    }

    to {
        opacity: 1;
        transform: translate3d(0, 0, 0);
        filter: blur(0);
    }
}

@keyframes otherSystemFade {
    from {
        opacity: 0;
        filter: brightness(1.12);
    }

    to {
        opacity: 1;
        filter: brightness(1);
    }
}

@keyframes otherSystemTextReveal {
    from {
        opacity: 0;
        transform: translateY(9px);
        filter: blur(2px);
    }

    to {
        opacity: 1;
        transform: translateY(0);
        filter: blur(0);
    }
}

/* ==================== Other 电影终幕 ==================== */

body.ui-mode-other #other-end-screen {
    position: fixed !important;
    inset: 0 !important;
    z-index: 12000 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    overflow: hidden !important;

    background:
        radial-gradient(circle at 50% 43%, rgba(255, 255, 255, 0.055), transparent 28%),
        linear-gradient(180deg, #0a0b0d 0%, #050608 54%, #020304 100%) !important;
    color: rgba(247, 247, 247, 0.96) !important;
    cursor: pointer !important;

    opacity: 0 !important;
    visibility: hidden !important;
    pointer-events: none !important;
    transition:
        opacity 900ms cubic-bezier(0.22, 1, 0.36, 1),
        visibility 0s linear 900ms !important;
}

body.ui-mode-other #other-end-screen.open {
    opacity: 1 !important;
    visibility: visible !important;
    pointer-events: auto !important;
    transition-delay: 0s !important;
}

body.ui-mode-other #other-end-screen::before {
    content: "" !important;
    position: absolute !important;
    inset: 0 !important;
    pointer-events: none !important;
    background:
        linear-gradient(90deg, rgba(0, 0, 0, 0.76), transparent 24%, transparent 76%, rgba(0, 0, 0, 0.76)),
        linear-gradient(180deg, rgba(0, 0, 0, 0.72), transparent 22%, transparent 76%, rgba(0, 0, 0, 0.86)),
        repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.012) 0, rgba(255, 255, 255, 0.012) 1px, transparent 1px, transparent 4px) !important;
}

body.ui-mode-other #other-end-screen::after {
    content: "" !important;
    position: absolute !important;
    top: 50% !important;
    left: 50% !important;
    width: min(76vw, 1040px) !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.08), transparent) !important;
    transform: translate(-50%, clamp(92px, 15vh, 150px)) scaleX(0.28) !important;
    opacity: 0 !important;
}

body.ui-mode-other #other-end-screen.open::after {
    animation: otherEndLine 1100ms 480ms cubic-bezier(0.16, 1, 0.3, 1) forwards !important;
}

body.ui-mode-other #other-end-screen .other-end-content {
    position: relative !important;
    z-index: 1 !important;
    width: min(86vw, 980px) !important;
    margin-top: -2vh !important;
    text-align: center !important;
}

body.ui-mode-other #other-end-screen.open .other-end-content {
    animation: otherEndContent 1050ms 160ms cubic-bezier(0.16, 1, 0.3, 1) both !important;
}

body.ui-mode-other #other-end-screen .other-end-kicker {
    margin-bottom: clamp(18px, 3.2vh, 34px) !important;
    color: rgba(238, 238, 238, 0.42) !important;
    font-size: clamp(10px, 0.72vw, 12px) !important;
    font-weight: 500 !important;
    letter-spacing: 0.46em !important;
    text-transform: uppercase !important;
}

body.ui-mode-other #other-end-screen .other-end-title {
    margin: 0 !important;
    color: rgba(250, 250, 250, 0.97) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    font-size: clamp(42px, 7vw, 88px) !important;
    font-weight: 250 !important;
    line-height: 1 !important;
    letter-spacing: 0.2em !important;
    text-indent: 0.2em !important;
    text-shadow: 0 8px 32px rgba(0, 0, 0, 0.88) !important;
}

body.ui-mode-other #other-end-screen .other-end-rule {
    width: clamp(64px, 8vw, 112px) !important;
    height: 1px !important;
    margin: clamp(25px, 4vh, 42px) auto clamp(18px, 2.8vh, 28px) !important;
    background: rgba(255, 255, 255, 0.52) !important;
    box-shadow: 0 0 18px rgba(255, 255, 255, 0.16) !important;
}

body.ui-mode-other #other-end-screen .other-end-epilogue {
    margin: 0 auto !important;
    color: rgba(235, 235, 235, 0.62) !important;
    font-size: clamp(14px, 1.15vw, 17px) !important;
    font-weight: 350 !important;
    line-height: 1.8 !important;
    letter-spacing: 0.12em !important;
}

body.ui-mode-other #other-end-screen .other-end-stats {
    display: flex !important;
    align-items: stretch !important;
    justify-content: center !important;
    flex-wrap: wrap !important;
    gap: clamp(18px, 4vw, 52px) !important;
    max-width: 850px !important;
    margin: clamp(32px, 5.5vh, 56px) auto 0 !important;
}

body.ui-mode-other #other-end-screen .other-end-stats.is-empty {
    display: none !important;
}

body.ui-mode-other #other-end-screen .other-end-stat {
    position: relative !important;
    display: grid !important;
    grid-template-rows: auto auto !important;
    gap: 8px !important;
    min-width: clamp(112px, 13vw, 168px) !important;
    padding: 0 clamp(10px, 1.5vw, 20px) !important;
}

body.ui-mode-other #other-end-screen .other-end-stat:not(:last-child)::after {
    content: "" !important;
    position: absolute !important;
    top: 12% !important;
    right: calc(clamp(18px, 4vw, 52px) / -2) !important;
    width: 1px !important;
    height: 76% !important;
    background: linear-gradient(180deg, transparent, rgba(255, 255, 255, 0.16), transparent) !important;
}

body.ui-mode-other #other-end-screen .other-end-stat span {
    color: rgba(226, 226, 226, 0.42) !important;
    font-size: clamp(10px, 0.78vw, 12px) !important;
    font-weight: 450 !important;
    letter-spacing: 0.16em !important;
}

body.ui-mode-other #other-end-screen .other-end-stat strong {
    color: rgba(248, 248, 248, 0.88) !important;
    font-size: clamp(18px, 1.8vw, 25px) !important;
    font-weight: 320 !important;
    letter-spacing: 0.06em !important;
}

body.ui-mode-other #other-end-screen .other-end-hint {
    position: fixed !important;
    right: 0 !important;
    bottom: clamp(24px, 4.8vh, 52px) !important;
    left: 0 !important;
    color: rgba(235, 235, 235, 0.33) !important;
    font-size: clamp(10px, 0.74vw, 12px) !important;
    font-weight: 400 !important;
    letter-spacing: 0.2em !important;
    text-align: center !important;
    animation: otherEndHint 2.4s ease-in-out infinite !important;
}

@keyframes otherEndContent {
    from { opacity: 0; transform: translateY(18px); filter: blur(5px); }
    to { opacity: 1; transform: translateY(0); filter: blur(0); }
}

@keyframes otherEndLine {
    from { opacity: 0; transform: translate(-50%, clamp(92px, 15vh, 150px)) scaleX(0.28); }
    to { opacity: 1; transform: translate(-50%, clamp(92px, 15vh, 150px)) scaleX(1); }
}

@keyframes otherEndHint {
    0%, 100% { opacity: 0.34; }
    50% { opacity: 0.72; }
}

/* ==================== Other 电影式加载画面 ==================== */

body.ui-mode-other loading-screen,
body.ui-mode-other [data-screen="loading"] {
    position: fixed !important;
    inset: 0 !important;
    z-index: 11800 !important;
    overflow: hidden !important;
    isolation: isolate !important;
    padding: 0 !important;
    border: 0 !important;
    background:
        radial-gradient(circle at 50% 45%, rgba(255, 255, 255, 0.05), transparent 27%),
        linear-gradient(145deg, #0b0c0f 0%, #060709 52%, #020304 100%) !important;
    color: rgba(245, 245, 245, 0.94) !important;
}

body.ui-mode-other loading-screen::before,
body.ui-mode-other [data-screen="loading"]::before {
    content: "" !important;
    position: absolute !important;
    inset: 0 !important;
    z-index: -1 !important;
    pointer-events: none !important;
    background:
        linear-gradient(90deg, rgba(0, 0, 0, 0.62), transparent 28%, transparent 72%, rgba(0, 0, 0, 0.62)),
        linear-gradient(180deg, rgba(0, 0, 0, 0.54), transparent 30%, transparent 72%, rgba(0, 0, 0, 0.74)),
        repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.011) 0, rgba(255, 255, 255, 0.011) 1px, transparent 1px, transparent 4px) !important;
}

body.ui-mode-other loading-screen::after,
body.ui-mode-other [data-screen="loading"]::after {
    content: "" !important;
    position: absolute !important;
    top: 0 !important;
    bottom: 0 !important;
    left: -45% !important;
    z-index: -1 !important;
    width: 38% !important;
    pointer-events: none !important;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.026), transparent) !important;
    transform: skewX(-12deg) !important;
    animation: otherLoadingSweep 3.8s 500ms ease-in-out infinite !important;
}

body.ui-mode-other loading-screen [data-content="wrapper"],
body.ui-mode-other [data-screen="loading"] [data-content="wrapper"] {
    position: absolute !important;
    top: 50% !important;
    left: 50% !important;
    display: grid !important;
    grid-template-columns: 1fr auto !important;
    align-items: end !important;
    width: min(76vw, 840px) !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    outline: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    transform: translate(-50%, -50%) !important;
    animation: otherLoadingContent 760ms cubic-bezier(0.16, 1, 0.3, 1) both !important;
}

body.ui-mode-other loading-screen [data-content="title"],
body.ui-mode-other [data-screen="loading"] [data-content="title"] {
    grid-column: 1 / -1 !important;
    grid-row: 2 !important;
    margin: 0 0 clamp(28px, 4.8vh, 48px) !important;
    color: rgba(247, 247, 247, 0.94) !important;
    font-family: "Source Sans 3 Local", "Inter Local", Inter, "PingFang SC", "Microsoft YaHei", sans-serif !important;
    font-size: clamp(24px, 3.4vw, 46px) !important;
    font-weight: 280 !important;
    line-height: 1.05 !important;
    letter-spacing: 0.24em !important;
    text-indent: 0.24em !important;
    text-align: center !important;
    text-transform: uppercase !important;
    text-shadow: 0 7px 28px rgba(0, 0, 0, 0.86) !important;
}

body.ui-mode-other loading-screen .other-loading-meta,
body.ui-mode-other [data-screen="loading"] .other-loading-meta {
    position: relative !important;
    grid-column: 1 / -1 !important;
    grid-row: 1 !important;
    justify-self: center !important;
    max-width: min(78vw, 680px) !important;
    margin: 0 0 clamp(15px, 2.5vh, 24px) !important;
    padding: 0 clamp(28px, 4vw, 58px) !important;
    color: rgba(235, 235, 235, 0.48) !important;
    font-size: clamp(10px, 0.76vw, 12px) !important;
    font-weight: 430 !important;
    line-height: 1.45 !important;
    letter-spacing: 0.14em !important;
    text-align: center !important;
    text-transform: none !important;
}

body.ui-mode-other loading-screen .other-loading-meta::before,
body.ui-mode-other loading-screen .other-loading-meta::after,
body.ui-mode-other [data-screen="loading"] .other-loading-meta::before,
body.ui-mode-other [data-screen="loading"] .other-loading-meta::after {
    content: "" !important;
    position: absolute !important;
    top: 50% !important;
    width: clamp(18px, 2.6vw, 36px) !important;
    height: 1px !important;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.26)) !important;
}

body.ui-mode-other loading-screen .other-loading-meta::before,
body.ui-mode-other [data-screen="loading"] .other-loading-meta::before {
    left: 0 !important;
}

body.ui-mode-other loading-screen .other-loading-meta::after,
body.ui-mode-other [data-screen="loading"] .other-loading-meta::after {
    right: 0 !important;
    transform: scaleX(-1) !important;
}

body.ui-mode-other loading-screen .other-loading-percent,
body.ui-mode-other [data-screen="loading"] .other-loading-percent {
    grid-column: 2 !important;
    grid-row: 3 !important;
    margin: 0 0 9px 24px !important;
    color: rgba(244, 244, 244, 0.58) !important;
    font-size: clamp(11px, 0.82vw, 13px) !important;
    font-variant-numeric: tabular-nums !important;
    font-weight: 400 !important;
    line-height: 1 !important;
    letter-spacing: 0.09em !important;
}

body.ui-mode-other loading-screen progress,
body.ui-mode-other loading-screen [data-content="progress"],
body.ui-mode-other [data-screen="loading"] progress,
body.ui-mode-other [data-screen="loading"] [data-content="progress"] {
    grid-column: 1 / -1 !important;
    grid-row: 4 !important;
    display: block !important;
    appearance: none !important;
    -webkit-appearance: none !important;
    width: 100% !important;
    height: 2px !important;
    margin: 0 !important;
    overflow: visible !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: rgba(255, 255, 255, 0.1) !important;
    box-shadow: none !important;
}

body.ui-mode-other loading-screen progress::-webkit-progress-bar,
body.ui-mode-other [data-screen="loading"] progress::-webkit-progress-bar {
    border: 0 !important;
    border-radius: 0 !important;
    background: rgba(255, 255, 255, 0.1) !important;
}

body.ui-mode-other loading-screen progress::-webkit-progress-value,
body.ui-mode-other [data-screen="loading"] progress::-webkit-progress-value {
    border-radius: 0 !important;
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.48), #ffffff) !important;
    box-shadow: 0 0 14px rgba(255, 255, 255, 0.24) !important;
    transition: width 240ms ease-out !important;
}

body.ui-mode-other loading-screen progress::-moz-progress-bar,
body.ui-mode-other [data-screen="loading"] progress::-moz-progress-bar {
    border-radius: 0 !important;
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.48), #ffffff) !important;
    box-shadow: 0 0 14px rgba(255, 255, 255, 0.24) !important;
}

body.ui-mode-other loading-screen [data-content="message"],
body.ui-mode-other [data-screen="loading"] [data-content="message"] {
    grid-column: 1 / -1 !important;
    grid-row: 5 !important;
    margin: clamp(14px, 2.4vh, 22px) 0 0 !important;
    color: rgba(230, 230, 230, 0.35) !important;
    font-size: clamp(10px, 0.76vw, 12px) !important;
    font-weight: 380 !important;
    line-height: 1.6 !important;
    letter-spacing: 0.1em !important;
    text-align: left !important;
}

@keyframes otherLoadingContent {
    from { opacity: 0; transform: translate(-50%, calc(-50% + 12px)); filter: blur(3px); }
    to { opacity: 1; transform: translate(-50%, -50%); filter: blur(0); }
}

@keyframes otherLoadingSweep {
    0%, 20% { left: -45%; opacity: 0; }
    42% { opacity: 1; }
    70%, 100% { left: 118%; opacity: 0; }
}

@media (max-width: 720px) {
    body.ui-mode-other #other-end-screen .other-end-content {
        width: 88vw !important;
    }

    body.ui-mode-other #other-end-screen .other-end-title {
        font-size: clamp(36px, 12vw, 58px) !important;
        letter-spacing: 0.13em !important;
        text-indent: 0.13em !important;
    }

    body.ui-mode-other #other-end-screen .other-end-stats {
        gap: 22px 14px !important;
        margin-top: 34px !important;
    }

    body.ui-mode-other #other-end-screen .other-end-stat {
        min-width: 108px !important;
    }

    body.ui-mode-other #other-end-screen .other-end-stat::after {
        display: none !important;
    }

    body.ui-mode-other loading-screen [data-content="wrapper"],
    body.ui-mode-other [data-screen="loading"] [data-content="wrapper"] {
        width: 82vw !important;
    }

    body.ui-mode-other loading-screen [data-content="title"],
    body.ui-mode-other [data-screen="loading"] [data-content="title"] {
        font-size: clamp(22px, 8vw, 34px) !important;
        letter-spacing: 0.17em !important;
        text-indent: 0.17em !important;
    }
}

@media (max-width: 800px) {
    body.ui-mode-other #stats-toggle {
        top: 12px !important;
        right: 13px !important;
    }

    body.ui-mode-other #stats-panel {
        top: 51px !important;
        right: 12px !important;
        width: min(78vw, 310px) !important;
        max-height: 62vh !important;
        padding-left: 28px !important;
    }

    body.ui-mode-other #system-toggle {
        top: 14px !important;
        right: 64px !important;
    }

    body.ui-mode-other #system-panel-overlay {
        padding: 58px 24px 46px !important;
    }

    body.ui-mode-other #system-panel-overlay .sys-header {
        margin-bottom: 30px !important;
    }

    body.ui-mode-other #system-panel-overlay .sys-body {
        width: 88% !important;
        font-size: clamp(15px, 3.3vw, 18px) !important;
        line-height: 1.82 !important;
    }
}

@media (orientation: landscape) and (max-height: 600px) {
    body.ui-mode-other #stats-toggle {
        top: 8px !important;
        right: 12px !important;
        transform: scale(0.92) !important;
    }

    body.ui-mode-other #stats-panel {
        top: 43px !important;
        right: 12px !important;
        max-height: 70vh !important;
    }

    body.ui-mode-other #stats-panel .stat-row {
        min-height: 36px !important;
        padding-top: 6px !important;
        padding-bottom: 7px !important;
    }

    body.ui-mode-other #stats-panel .stat-val {
        font-size: 19px !important;
    }

    body.ui-mode-other #system-toggle {
        top: 10px !important;
        right: 62px !important;
    }

    body.ui-mode-other #system-panel-overlay {
        padding: 40px 8vw 34px !important;
    }

    body.ui-mode-other #system-panel-overlay .sys-header {
        margin-bottom: 22px !important;
        padding-bottom: 12px !important;
    }

    body.ui-mode-other #system-panel-overlay .sys-body {
        width: min(86%, 900px) !important;
        padding-bottom: 50px !important;
        font-size: clamp(14px, 2.8vh, 17px) !important;
        line-height: 1.68 !important;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other #stats-toggle,
    body.ui-mode-other #stats-toggle::before,
    body.ui-mode-other #stats-toggle::after,
    body.ui-mode-other #stats-panel.open,
    body.ui-mode-other #system-toggle,
    body.ui-mode-other #system-toggle::before,
    body.ui-mode-other #system-panel-overlay.open,
    body.ui-mode-other #system-panel-overlay .sys-close,
    body.ui-mode-other #system-panel-overlay .sys-close::before,
    body.ui-mode-other #system-panel-overlay .sys-body > div {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}

/* Other 与 Ancient 共用同一组“游戏内阅读 UI”字号基准。
   只统一正文、说话人、选项和快捷菜单，不改首页标题/背景构图。 */
body.ui-mode-other [data-component="text-box"] [data-content="dialog"],
body.ui-mode-other [data-component="text-box"] p {
    font-size: clamp(1.1rem, 2.15vw, 1.6rem) !important;
    line-height: 1.72 !important;
}
body.ui-mode-other [data-component="text-box"] [data-ui="who"] {
    font-size: clamp(.95rem, 1.75vw, 1.28rem) !important;
}
body.ui-mode-other choice-container button,
body.ui-mode-other [data-component="choice-container"] button,
body.ui-mode-other choice-container [data-choice],
body.ui-mode-other [data-component="choice-container"] [data-choice] {
    font-size: clamp(1.02rem, 2vw, 1.55rem) !important;
    line-height: 1.45 !important;
}
body.ui-mode-other [data-component="quick-menu"] button {
    font-size: clamp(.9rem, 1.3vw, 1.08rem) !important;
}
body.ui-mode-other main-menu button,
body.ui-mode-other [data-screen="main"] main-menu button {
    font-size: clamp(1.25rem, 2.5vw, 2.25rem) !important;
}
body.ui-mode-other alert-modal [data-content="message"],
body.ui-mode-other message-modal [data-content="message"],
body.ui-mode-other [data-component="alert-modal"] [data-content="message"],
body.ui-mode-other [data-component="message-modal"] [data-content="message"] {
    font-size: clamp(1rem, 1.8vw, 1.3rem) !important;
    line-height: 1.65 !important;
}
body.ui-mode-other alert-modal [data-content="actions"] button,
body.ui-mode-other message-modal [data-content="actions"] button,
body.ui-mode-other [data-component="alert-modal"] [data-content="actions"] button,
body.ui-mode-other [data-component="message-modal"] [data-content="actions"] button {
    font-size: clamp(.92rem, 1.55vw, 1.14rem) !important;
}
body.ui-mode-other settings-screen [data-settings] > h3,
body.ui-mode-other [data-component="settings-screen"] [data-settings] > h3,
body.ui-mode-other settings-screen [data-settings] > span,
body.ui-mode-other [data-component="settings-screen"] [data-settings] > span {
    font-size: clamp(.92rem, 1.35vw, 1.12rem) !important;
    line-height: 1.35 !important;
}
"""


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
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) !important;
    grid-template-rows: auto minmax(0, 1fr) !important;
    row-gap: .45rem !important;
    /* 保持原有文本框高度；正文超出后才在正文区域滚动。 */
    height: clamp(240px, 30vh, 360px) !important;
    min-height: clamp(240px, 30vh, 360px) !important;
    max-height: clamp(240px, 30vh, 360px) !important;
    box-sizing: border-box !important;
    padding: 0 0 calc(72px + env(safe-area-inset-bottom, 0px)) !important;
    /* 不向下平移，避免长字幕滚动区侵入底部快捷按钮区域。 */
    transform: none !important;
    background: transparent !important;
    border: 0 !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
    /* 文本框本身不能越过底部快捷菜单区域。 */
    overflow: hidden !important;
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
    grid-row: 2 !important;
    width: 60vw !important;
    max-width: 60vw !important;
    height: 100% !important;
    min-height: 0 !important;
    max-height: 100% !important;
    margin: 0 auto !important;
    padding: 0 !important;
    align-self: stretch !important;
    position: relative !important;
    z-index: 2 !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;
}
body.ui-mode-ancient [data-component="text-box"] [data-content="name"] {
    grid-column: 1 / -1 !important;
    grid-row: 1 !important;
    width: 60vw !important;
    max-width: 60vw !important;
    margin: 0 auto !important;
    position: relative !important;
    z-index: 2 !important;
}
body.ui-mode-ancient [data-component="text-box"] p {
    width: 100% !important;
    height: auto !important;
    max-height: none !important;
    margin: 0 !important;
    /* 行数阈值由滚动容器控制，不额外占用会话正文的可视高度。 */
    padding: 0 !important;
    color: rgba(247,243,233,.92) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(1.1rem, 2.15vw, 1.6rem) !important;
    font-weight: 400 !important;
    line-height: 1.72 !important;
    letter-spacing: .025em !important;
    text-shadow: 0 2px 6px rgba(0,0,0,.8) !important;
}
body.ui-mode-ancient [data-component="text-box"] [data-content="text"]::-webkit-scrollbar {
    width: 3px;
}
body.ui-mode-ancient [data-component="text-box"] [data-content="text"]::-webkit-scrollbar-track {
    background: transparent;
}
body.ui-mode-ancient [data-component="text-box"] [data-content="text"]::-webkit-scrollbar-thumb {
    border-radius: 3px;
    background: rgba(183,155,105,.38);
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
    z-index: 100 !important;
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
/* 游戏快捷菜单不再展示 Hide；显示/隐藏快捷菜单统一放到 Settings 中。
   两套运行期皮肤都隐藏主页面快捷项；Settings 中的 [data-*-quick]
   控件不在 quick-menu 容器内，因此仍然保留。 */
body.ui-mode-ancient [data-component="quick-menu"] [data-action="distraction-free"],
body.ui-mode-ancient quick-menu [data-action="distraction-free"],
body.ui-mode-other [data-component="quick-menu"] [data-action="distraction-free"],
body.ui-mode-other quick-menu [data-action="distraction-free"],
body.ui-mode-ancient [data-component="quick-menu"] .is-distraction-free-hidden,
body.ui-mode-ancient quick-menu .is-distraction-free-hidden,
body.ui-mode-other [data-component="quick-menu"] .is-distraction-free-hidden,
body.ui-mode-other quick-menu .is-distraction-free-hidden,
body.ui-mode-ancient [data-component="quick-menu"] button:has([data-string="Hide"]),
body.ui-mode-ancient [data-component="quick-menu"] button:has([data-string="Show"]),
body.ui-mode-ancient quick-menu button:has([data-string="Hide"]),
body.ui-mode-ancient quick-menu button:has([data-string="Show"]),
body.ui-mode-other [data-component="quick-menu"] button:has([data-string="Hide"]),
body.ui-mode-other [data-component="quick-menu"] button:has([data-string="Show"]),
body.ui-mode-other quick-menu button:has([data-string="Hide"]),
body.ui-mode-other quick-menu button:has([data-string="Show"]),
body.ui-mode-ancient [data-component="quick-menu"] button:has(.fa-eye),
body.ui-mode-ancient [data-component="quick-menu"] button:has(.fa-eye-slash),
body.ui-mode-ancient quick-menu button:has(.fa-eye),
body.ui-mode-ancient quick-menu button:has(.fa-eye-slash),
body.ui-mode-other [data-component="quick-menu"] button:has(.fa-eye),
body.ui-mode-other [data-component="quick-menu"] button:has(.fa-eye-slash),
body.ui-mode-other quick-menu button:has(.fa-eye),
body.ui-mode-other quick-menu button:has(.fa-eye-slash) {
    display: none !important;
}

/* 剧情选择：选项在画面 65% 高度附近纵向排列。 */
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
    position: absolute !important;
    top: 65% !important;
    left: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(82vw, 68rem) !important;
    max-height: min(64vh, 720px) !important;
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) !important;
    gap: clamp(.8rem, 2vh, 1.35rem) !important;
    align-items: center !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;
    pointer-events: auto;
}
body.ui-mode-ancient choice-container button,
body.ui-mode-ancient [data-component="choice-container"] button {
    position: relative !important;
    grid-column: 1 !important;
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
    min-width: 0 !important;
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
    min-width: 0 !important;
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
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
    white-space: normal !important;
    overflow: visible !important;
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
    min-width: 0 !important;
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
    width: 100% !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
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
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
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
    min-width: 0 !important;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
    overflow: visible !important;
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

/* 覆盖存档确认框的可编辑名称输入：保留输入与 Overwrite 功能，
   只移除浏览器默认白底/默认边框，保持古风墨色面板。 */
body.ui-mode-ancient alert-modal input[data-content="context"],
body.ui-mode-ancient message-modal input[data-content="context"],
body.ui-mode-ancient [data-component="alert-modal"] input[data-content="context"],
body.ui-mode-ancient [data-component="message-modal"] input[data-content="context"] {
    width: min(100%, 30rem) !important;
    margin: .25rem auto !important;
    padding: .65rem 1rem !important;
    box-sizing: border-box !important;
    border: 1px solid rgba(183, 155, 105, .48) !important;
    border-radius: 0 !important;
    outline: none !important;
    background: rgba(24, 22, 19, .82) !important;
    color: rgba(244, 239, 228, .9) !important;
    box-shadow: inset 0 0 18px rgba(0, 0, 0, .32) !important;
    text-align: center !important;
    font: inherit !important;
    appearance: none !important;
    -webkit-appearance: none !important;
}

body.ui-mode-ancient alert-modal input[data-content="context"]:focus,
body.ui-mode-ancient message-modal input[data-content="context"]:focus,
body.ui-mode-ancient [data-component="alert-modal"] input[data-content="context"]:focus,
body.ui-mode-ancient [data-component="message-modal"] input[data-content="context"]:focus {
    border-color: rgba(220, 190, 135, .78) !important;
    background: rgba(30, 27, 23, .9) !important;
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

/* ==================== Ancient Save / Load 电影资料库 ==================== */
/*
 * 复用 Other 的开放式资料库构图，只把银灰色调换成墨黑、纸白与暗金。
 * 以下规则只覆盖既有 Monogatari 节点的视觉属性，不替换节点、不绑定存取档事件。
 */
body.ui-mode-ancient save-screen,
body.ui-mode-ancient load-screen,
body.ui-mode-ancient [data-component="save-screen"],
body.ui-mode-ancient [data-component="load-screen"] {
    position: fixed !important;
    inset: 0 !important;
    z-index: 9400 !important;
    box-sizing: border-box !important;
    width: 100vw !important;
    height: 100vh !important;
    min-height: 100vh !important;
    padding: clamp(68px, 9vh, 102px) clamp(34px, 7.5vw, 144px) clamp(38px, 7vh, 78px) !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;
    border: 0 !important;
    background:
        radial-gradient(ellipse at 76% 18%, rgba(214,184,126,.075), rgba(108,82,43,.025) 31%, transparent 58%),
        repeating-linear-gradient(116deg, rgba(255,248,230,.006) 0 1px, transparent 1px 8px),
        linear-gradient(112deg, #030303 0%, #090907 48%, #11100c 76%, #040403 100%) !important;
    box-shadow: none !important;
    color: rgba(239,232,217,.88) !important;
    font-family: var(--ancient-source-font) !important;
    isolation: isolate !important;
}

body.ui-mode-ancient save-screen::before,
body.ui-mode-ancient load-screen::before,
body.ui-mode-ancient [data-component="save-screen"]::before,
body.ui-mode-ancient [data-component="load-screen"]::before {
    content: '' !important;
    position: fixed !important;
    inset: 0 !important;
    z-index: -2 !important;
    pointer-events: none !important;
    background:
        linear-gradient(90deg, transparent, rgba(224,202,157,.035) 25%, rgba(244,230,200,.105) 50%, rgba(183,155,105,.03) 76%, transparent) center 7vh / 84vw 1px no-repeat,
        linear-gradient(90deg, transparent, rgba(183,155,105,.025) 28%, rgba(224,202,157,.075) 50%, rgba(183,155,105,.025) 72%, transparent) center calc(100vh - 7vh) / 84vw 1px no-repeat,
        radial-gradient(ellipse at 50% 35%, rgba(183,155,105,.045), transparent 54%) !important;
}

body.ui-mode-ancient save-screen::after,
body.ui-mode-ancient load-screen::after,
body.ui-mode-ancient [data-component="save-screen"]::after,
body.ui-mode-ancient [data-component="load-screen"]::after {
    content: '' !important;
    position: fixed !important;
    inset: 0 !important;
    z-index: -1 !important;
    pointer-events: none !important;
    background: radial-gradient(ellipse at center, transparent 40%, rgba(0,0,0,.28) 76%, rgba(0,0,0,.64) 100%) !important;
}

body.ui-mode-ancient save-screen.active,
body.ui-mode-ancient load-screen.active,
body.ui-mode-ancient [data-component="save-screen"].active,
body.ui-mode-ancient [data-component="load-screen"].active {
    animation: ancientArchiveScreenReveal 430ms ease both !important;
}

body.ui-mode-ancient load-screen > h2,
body.ui-mode-ancient [data-component="load-screen"] > h2 {
    width: min(100%, 1280px) !important;
    margin: 0 auto clamp(24px, 4vh, 44px) !important;
    padding: 0 0 17px !important;
    border: 0 !important;
    background: transparent !important;
    color: rgba(244,235,216,.92) !important;
    font-family: var(--ancient-ui-font) !important;
    font-size: clamp(24px, 1.8vw, 34px) !important;
    font-weight: 400 !important;
    line-height: 1.2 !important;
    letter-spacing: .15em !important;
    text-align: left !important;
    text-shadow: 0 4px 18px rgba(0,0,0,.82) !important;
}

body.ui-mode-ancient load-screen > h2::after,
body.ui-mode-ancient [data-component="load-screen"] > h2::after {
    content: '' !important;
    display: block !important;
    width: min(48vw, 560px) !important;
    height: 1px !important;
    margin-top: 16px !important;
    background: linear-gradient(90deg, rgba(214,184,126,.42), transparent) !important;
}

body.ui-mode-ancient save-screen > .horizontal,
body.ui-mode-ancient [data-component="save-screen"] > .horizontal {
    position: relative !important;
    z-index: 1 !important;
    display: grid !important;
    grid-template-columns: minmax(180px, 1fr) auto !important;
    align-items: center !important;
    gap: clamp(18px, 2.5vw, 38px) !important;
    width: min(100%, 1280px) !important;
    margin: 0 auto clamp(34px, 5.5vh, 62px) !important;
    padding: 0 0 15px !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-ancient save-screen > .horizontal::before,
body.ui-mode-ancient [data-component="save-screen"] > .horizontal::before {
    content: attr(data-other-page-title) !important;
    position: absolute !important;
    top: 50% !important;
    left: 4px !important;
    z-index: 2 !important;
    pointer-events: none !important;
    color: rgba(244,235,216,.94) !important;
    font-family: var(--ancient-ui-font) !important;
    font-size: clamp(25px, 1.8vw, 34px) !important;
    font-weight: 400 !important;
    line-height: 1.2 !important;
    letter-spacing: .12em !important;
    text-shadow: 0 4px 18px rgba(0,0,0,.82) !important;
    transform: translateY(-50%) !important;
}

body.ui-mode-ancient save-screen > .horizontal::after,
body.ui-mode-ancient [data-component="save-screen"] > .horizontal::after {
    content: '' !important;
    position: absolute !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 0 !important;
    height: 1px !important;
    pointer-events: none !important;
    background: linear-gradient(90deg, rgba(214,184,126,.40), rgba(183,155,105,.11) 52%, transparent 92%) !important;
}

body.ui-mode-ancient save-screen input[data-content="slot-name"],
body.ui-mode-ancient [data-component="save-screen"] input[data-content="slot-name"] {
    width: 100% !important;
    min-width: 0 !important;
    height: 46px !important;
    margin: 0 !important;
    padding: 0 4px !important;
    border: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: transparent !important;
    caret-color: transparent !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(19px, 1.45vw, 24px) !important;
    font-weight: 360 !important;
    line-height: 46px !important;
    letter-spacing: .035em !important;
    pointer-events: none !important;
    user-select: none !important;
}

body.ui-mode-ancient save-screen button[data-action="save"],
body.ui-mode-ancient [data-component="save-screen"] button[data-action="save"] {
    position: relative !important;
    width: auto !important;
    min-width: 138px !important;
    height: 46px !important;
    margin: 0 !important;
    padding: 0 20px 0 48px !important;
    border: 1px solid rgba(214,184,126,.48) !important;
    border-radius: 1px !important;
    outline: none !important;
    background: linear-gradient(135deg, rgba(183,155,105,.19), rgba(78,60,32,.11)) !important;
    box-shadow: inset 0 1px 0 rgba(255,239,204,.07), 0 8px 22px rgba(0,0,0,.24) !important;
    color: rgba(244,232,208,.94) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: 14px !important;
    font-weight: 500 !important;
    line-height: 44px !important;
    letter-spacing: .08em !important;
    text-align: center !important;
    text-shadow: 0 2px 8px rgba(0,0,0,.9) !important;
    cursor: pointer !important;
    transition: background .24s ease, border-color .24s ease, color .22s ease, transform .3s ease !important;
}

body.ui-mode-ancient save-screen button[data-action="save"]::before,
body.ui-mode-ancient [data-component="save-screen"] button[data-action="save"]::before {
    content: '' !important;
    position: absolute !important;
    top: 50% !important;
    left: 18px !important;
    width: 16px !important;
    height: 16px !important;
    background: linear-gradient(currentColor,currentColor) center / 11px 1px no-repeat, linear-gradient(currentColor,currentColor) center / 1px 11px no-repeat !important;
    opacity: .86 !important;
    transform: translateY(-50%) !important;
    transition: transform .3s ease !important;
}

body.ui-mode-ancient save-screen button[data-action="save"]:hover,
body.ui-mode-ancient save-screen button[data-action="save"]:focus-visible,
body.ui-mode-ancient [data-component="save-screen"] button[data-action="save"]:hover,
body.ui-mode-ancient [data-component="save-screen"] button[data-action="save"]:focus-visible {
    border-color: rgba(239,216,170,.86) !important;
    background: rgba(224,202,157,.91) !important;
    color: rgba(25,20,13,.96) !important;
    text-shadow: none !important;
    transform: translateY(-2px) !important;
}

body.ui-mode-ancient save-screen button[data-action="save"]:hover::before,
body.ui-mode-ancient save-screen button[data-action="save"]:focus-visible::before,
body.ui-mode-ancient [data-component="save-screen"] button[data-action="save"]:hover::before,
body.ui-mode-ancient [data-component="save-screen"] button[data-action="save"]:focus-visible::before {
    transform: translateY(-50%) rotate(90deg) !important;
}

body.ui-mode-ancient load-screen > [data-ui="saveSlots"],
body.ui-mode-ancient load-screen > [data-ui="autoSaveSlots"],
body.ui-mode-ancient [data-component="load-screen"] > [data-ui="saveSlots"],
body.ui-mode-ancient [data-component="load-screen"] > [data-ui="autoSaveSlots"] {
    width: min(100%, 1280px) !important;
    margin: 0 auto clamp(38px, 6vh, 68px) !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-ancient load-screen h3,
body.ui-mode-ancient [data-component="load-screen"] h3 {
    position: relative !important;
    margin: 0 0 clamp(17px, 2.5vh, 28px) !important;
    padding: 0 0 12px !important;
    border: 0 !important;
    background: transparent !important;
    color: rgba(224,211,183,.55) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    line-height: 1.3 !important;
    letter-spacing: .15em !important;
    text-align: left !important;
    text-transform: uppercase !important;
}

body.ui-mode-ancient load-screen h3::after,
body.ui-mode-ancient [data-component="load-screen"] h3::after {
    content: '' !important;
    position: absolute !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 0 !important;
    height: 1px !important;
    background: linear-gradient(90deg, rgba(183,155,105,.28), transparent 72%) !important;
}

body.ui-mode-ancient save-screen > [data-ui="slots"],
body.ui-mode-ancient load-screen [data-ui="slots"],
body.ui-mode-ancient [data-component="save-screen"] > [data-ui="slots"],
body.ui-mode-ancient [data-component="load-screen"] [data-ui="slots"] {
    display: block !important;
    width: min(100%, 1480px) !important;
    margin: 0 auto !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-ancient slot-container,
body.ui-mode-ancient [data-component="slot-container"] {
    display: grid !important;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 430px)) !important;
    align-items: start !important;
    justify-content: start !important;
    gap: clamp(38px, 5vw, 72px) clamp(28px, 3.3vw, 54px) !important;
    width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

body.ui-mode-ancient slot-container > p,
body.ui-mode-ancient [data-component="slot-container"] > p {
    grid-column: 1 / -1 !important;
    min-height: clamp(150px, 30vh, 300px) !important;
    margin: 0 !important;
    padding: clamp(58px, 12vh, 120px) 16px 20px !important;
    border: 0 !important;
    background: transparent !important;
    color: rgba(224,211,183,.38) !important;
    font-size: clamp(15px, 1.1vw, 18px) !important;
    letter-spacing: .08em !important;
    text-align: center !important;
}

body.ui-mode-ancient save-slot,
body.ui-mode-ancient [data-component="save-slot"] {
    position: relative !important;
    display: grid !important;
    grid-template-rows: auto auto auto !important;
    align-content: start !important;
    justify-self: center !important;
    width: 100% !important;
    max-width: 430px !important;
    min-width: 0 !important;
    min-height: 0 !important;
    height: auto !important;
    margin: 0 !important;
    padding: 0 0 26px !important;
    overflow: visible !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(239,232,217,.78) !important;
    cursor: pointer !important;
    isolation: isolate !important;
    transition: color .22s ease, transform .38s cubic-bezier(.16,1,.3,1) !important;
}

body.ui-mode-ancient save-slot::before,
body.ui-mode-ancient [data-component="save-slot"]::before {
    content: none !important;
    display: none !important;
}

body.ui-mode-ancient save-slot::after,
body.ui-mode-ancient [data-component="save-slot"]::after {
    content: '' !important;
    position: absolute !important;
    bottom: 0 !important;
    left: 50% !important;
    width: 100% !important;
    height: 1px !important;
    pointer-events: none !important;
    background: rgba(224,202,157,.76) !important;
    opacity: 0 !important;
    transform: translateX(-50%) scaleX(.18) !important;
    transform-origin: center !important;
    transition: opacity .2s ease, transform .36s cubic-bezier(.16,1,.3,1) !important;
}

body.ui-mode-ancient save-slot [data-content="background"],
body.ui-mode-ancient [data-component="save-slot"] [data-content="background"] {
    position: relative !important;
    grid-row: 1 !important;
    justify-self: center !important;
    width: calc(100% + clamp(18px, 1.5vw, 32px)) !important;
    max-width: none !important;
    height: auto !important;
    aspect-ratio: 16 / 9 !important;
    overflow: hidden !important;
    border: 0 !important;
    border-radius: 0 !important;
    outline: 1px solid rgba(214,184,126,.11) !important;
    outline-offset: -1px !important;
    background-color: rgba(20,18,13,.78) !important;
    background-position: center !important;
    background-repeat: no-repeat !important;
    background-size: cover !important;
    box-shadow: 0 16px 34px rgba(0,0,0,.26) !important;
    filter: sepia(.16) brightness(.72) saturate(.58) contrast(1.08) !important;
    transition: filter .42s ease, outline-color .26s ease, box-shadow .42s ease, transform .48s cubic-bezier(.16,1,.3,1) !important;
}

body.ui-mode-ancient save-slot [data-content="background"]::before,
body.ui-mode-ancient [data-component="save-slot"] [data-content="background"]::before {
    content: '' !important;
    position: absolute !important;
    inset: 0 !important;
    z-index: 1 !important;
    pointer-events: none !important;
    background: linear-gradient(180deg, rgba(239,216,170,.055), transparent 24%, transparent 72%, rgba(0,0,0,.28)), linear-gradient(90deg, rgba(183,155,105,.045), transparent 26%, transparent 78%, rgba(0,0,0,.18)) !important;
    opacity: .74 !important;
    transition: opacity .36s ease !important;
}

body.ui-mode-ancient save-slot [data-content="background"]::after,
body.ui-mode-ancient [data-component="save-slot"] [data-content="background"]::after {
    content: '' !important;
    position: absolute !important;
    z-index: 2 !important;
    top: -24% !important;
    bottom: -24% !important;
    left: -48% !important;
    width: 34% !important;
    pointer-events: none !important;
    background: linear-gradient(90deg, transparent, rgba(239,216,170,.025) 24%, rgba(239,216,170,.15) 52%, rgba(239,216,170,.03) 76%, transparent) !important;
    filter: blur(2px) !important;
    opacity: 0 !important;
    transform: skewX(-14deg) translateX(-180%) !important;
}

body.ui-mode-ancient save-slot .badge,
body.ui-mode-ancient [data-component="save-slot"] .badge {
    position: relative !important;
    z-index: 1 !important;
    grid-row: 2 !important;
    display: -webkit-box !important;
    box-sizing: border-box !important;
    width: 100% !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: clamp(18px, 1.45vw, 23px) 20px 0 !important;
    overflow: hidden !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(244,235,216,.90) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(17px, 1.05vw, 20px) !important;
    font-weight: 430 !important;
    line-height: 1.42 !important;
    letter-spacing: .045em !important;
    text-align: center !important;
    text-shadow: 0 3px 14px rgba(0,0,0,.82) !important;
    white-space: normal !important;
    -webkit-box-orient: vertical !important;
    -webkit-line-clamp: 2 !important;
}

body.ui-mode-ancient save-slot figcaption,
body.ui-mode-ancient [data-component="save-slot"] figcaption {
    grid-row: 3 !important;
    margin: 0 !important;
    padding: 7px 20px 0 !important;
    border: 0 !important;
    background: transparent !important;
    color: rgba(224,211,183,.50) !important;
    font-family: var(--ancient-source-font) !important;
    font-size: clamp(11px, .72vw, 12px) !important;
    font-variant-numeric: tabular-nums !important;
    line-height: 1.5 !important;
    letter-spacing: .075em !important;
    text-align: center !important;
    transition: color .26s ease, letter-spacing .36s ease !important;
}

body.ui-mode-ancient save-slot [data-delete],
body.ui-mode-ancient [data-component="save-slot"] [data-delete] {
    position: absolute !important;
    z-index: 4 !important;
    top: 5px !important;
    right: -10px !important;
    display: grid !important;
    place-items: center !important;
    width: 35px !important;
    height: 35px !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    outline: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: rgba(239,216,170,.42) !important;
    font-size: 15px !important;
    line-height: 1 !important;
    text-shadow: 0 2px 7px rgba(0,0,0,.98) !important;
    cursor: pointer !important;
    opacity: .52 !important;
    transition: color .22s ease, opacity .22s ease, transform .3s ease !important;
}

body.ui-mode-ancient save-slot:hover,
body.ui-mode-ancient save-slot:focus-within,
body.ui-mode-ancient [data-component="save-slot"]:hover,
body.ui-mode-ancient [data-component="save-slot"]:focus-within {
    color: rgba(255,246,226,.97) !important;
    transform: translateY(-5px) !important;
}

body.ui-mode-ancient save-slot:hover::after,
body.ui-mode-ancient save-slot:focus-within::after,
body.ui-mode-ancient [data-component="save-slot"]:hover::after,
body.ui-mode-ancient [data-component="save-slot"]:focus-within::after {
    opacity: .84 !important;
    transform: translateX(-50%) scaleX(1) !important;
}

body.ui-mode-ancient save-slot:hover [data-content="background"],
body.ui-mode-ancient save-slot:focus-within [data-content="background"],
body.ui-mode-ancient [data-component="save-slot"]:hover [data-content="background"],
body.ui-mode-ancient [data-component="save-slot"]:focus-within [data-content="background"] {
    outline-color: rgba(224,202,157,.25) !important;
    box-shadow: 0 18px 40px rgba(0,0,0,.38), 0 0 10px rgba(224,202,157,.13), 0 0 28px rgba(183,155,105,.08), 0 0 52px rgba(183,155,105,.04) !important;
    filter: sepia(.08) brightness(.92) saturate(.82) contrast(1.05) !important;
    transform: scale(1.01) !important;
}

body.ui-mode-ancient save-slot:hover [data-content="background"]::after,
body.ui-mode-ancient save-slot:focus-within [data-content="background"]::after,
body.ui-mode-ancient [data-component="save-slot"]:hover [data-content="background"]::after,
body.ui-mode-ancient [data-component="save-slot"]:focus-within [data-content="background"]::after {
    animation: ancientSaveSlotSweep 860ms cubic-bezier(.2,.72,.2,1) both !important;
}

body.ui-mode-ancient save-slot:hover .badge,
body.ui-mode-ancient save-slot:focus-within .badge,
body.ui-mode-ancient [data-component="save-slot"]:hover .badge,
body.ui-mode-ancient [data-component="save-slot"]:focus-within .badge {
    color: rgba(255,247,230,.99) !important;
    text-shadow: 0 2px 9px rgba(224,202,157,.13), 0 5px 18px rgba(0,0,0,.84) !important;
}

body.ui-mode-ancient save-slot:hover figcaption,
body.ui-mode-ancient save-slot:focus-within figcaption,
body.ui-mode-ancient [data-component="save-slot"]:hover figcaption,
body.ui-mode-ancient [data-component="save-slot"]:focus-within figcaption {
    color: rgba(238,225,199,.74) !important;
    letter-spacing: .09em !important;
}

body.ui-mode-ancient save-slot:hover [data-delete],
body.ui-mode-ancient save-slot:focus-within [data-delete],
body.ui-mode-ancient [data-component="save-slot"]:hover [data-delete],
body.ui-mode-ancient [data-component="save-slot"]:focus-within [data-delete] {
    opacity: .82 !important;
}

body.ui-mode-ancient save-slot [data-delete]:hover,
body.ui-mode-ancient save-slot [data-delete]:focus-visible,
body.ui-mode-ancient [data-component="save-slot"] [data-delete]:hover,
body.ui-mode-ancient [data-component="save-slot"] [data-delete]:focus-visible {
    color: rgba(255,239,204,.98) !important;
    opacity: 1 !important;
    transform: scale(1.08) !important;
}

body.ui-mode-ancient save-screen::-webkit-scrollbar,
body.ui-mode-ancient load-screen::-webkit-scrollbar {
    width: 5px;
}
body.ui-mode-ancient save-screen::-webkit-scrollbar-track,
body.ui-mode-ancient load-screen::-webkit-scrollbar-track {
    background: transparent;
}
body.ui-mode-ancient save-screen::-webkit-scrollbar-thumb,
body.ui-mode-ancient load-screen::-webkit-scrollbar-thumb {
    border-radius: 5px;
    background: rgba(183,155,105,.28);
}

@keyframes ancientArchiveScreenReveal {
    from { opacity: 0; filter: brightness(.76); }
    to { opacity: 1; filter: brightness(1); }
}
@keyframes ancientSaveSlotSweep {
    0% { opacity: 0; transform: skewX(-14deg) translateX(-180%); }
    18% { opacity: .78; }
    100% { opacity: 0; transform: skewX(-14deg) translateX(620%); }
}

@media (min-width: 1800px) {
    body.ui-mode-ancient save-screen,
    body.ui-mode-ancient load-screen,
    body.ui-mode-ancient [data-component="save-screen"],
    body.ui-mode-ancient [data-component="load-screen"] {
        padding-top: clamp(96px, 10vh, 150px) !important;
        padding-right: clamp(150px, 11vw, 260px) !important;
        padding-left: clamp(150px, 11vw, 260px) !important;
    }
    body.ui-mode-ancient save-screen > .horizontal,
    body.ui-mode-ancient [data-component="save-screen"] > .horizontal {
        width: min(100%, 1480px) !important;
        margin-bottom: clamp(64px, 7vh, 96px) !important;
    }
    body.ui-mode-ancient load-screen > h2,
    body.ui-mode-ancient [data-component="load-screen"] > h2,
    body.ui-mode-ancient load-screen > [data-ui="saveSlots"],
    body.ui-mode-ancient load-screen > [data-ui="autoSaveSlots"],
    body.ui-mode-ancient [data-component="load-screen"] > [data-ui="saveSlots"],
    body.ui-mode-ancient [data-component="load-screen"] > [data-ui="autoSaveSlots"] {
        width: min(100%, 1480px) !important;
    }
}

@media (max-width: 900px) {
    body.ui-mode-ancient slot-container,
    body.ui-mode-ancient [data-component="slot-container"] {
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
        gap: 42px 28px !important;
    }
}

@media (max-width: 800px) {
    body.ui-mode-ancient save-screen,
    body.ui-mode-ancient load-screen,
    body.ui-mode-ancient [data-component="save-screen"],
    body.ui-mode-ancient [data-component="load-screen"] {
        padding: 70px 30px 44px !important;
    }
    body.ui-mode-ancient save-screen > .horizontal,
    body.ui-mode-ancient [data-component="save-screen"] > .horizontal {
        grid-template-columns: 1fr auto !important;
        gap: 14px !important;
    }
}

@media (max-width: 560px) and (orientation: portrait) {
    body.ui-mode-ancient save-screen,
    body.ui-mode-ancient load-screen,
    body.ui-mode-ancient [data-component="save-screen"],
    body.ui-mode-ancient [data-component="load-screen"] {
        padding: 64px 20px 34px !important;
    }
    body.ui-mode-ancient save-screen > .horizontal,
    body.ui-mode-ancient [data-component="save-screen"] > .horizontal {
        grid-template-columns: minmax(0, 1fr) auto !important;
        gap: 12px !important;
        margin-bottom: 34px !important;
    }
    body.ui-mode-ancient slot-container,
    body.ui-mode-ancient [data-component="slot-container"] {
        grid-template-columns: 1fr !important;
        gap: 40px !important;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-ancient save-screen.active,
    body.ui-mode-ancient load-screen.active,
    body.ui-mode-ancient save-slot,
    body.ui-mode-ancient save-slot::after,
    body.ui-mode-ancient save-slot [data-content="background"],
    body.ui-mode-ancient save-slot [data-content="background"]::after,
    body.ui-mode-ancient [data-component="save-slot"],
    body.ui-mode-ancient [data-component="save-slot"]::after,
    body.ui-mode-ancient [data-component="save-slot"] [data-content="background"],
    body.ui-mode-ancient [data-component="save-slot"] [data-content="background"]::after {
        animation: none !important;
        transition-duration: 1ms !important;
    }
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
    body.ui-mode-ancient [data-component="text-box"] {
        height: clamp(250px, 34vh, 310px) !important;
        min-height: clamp(250px, 34vh, 310px) !important;
        max-height: clamp(250px, 34vh, 310px) !important;
    }
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
        max-height: 64vh !important;
        gap: .65rem !important;
    }
    body.ui-mode-ancient choice-container button,
    body.ui-mode-ancient [data-component="choice-container"] button {
        grid-column: 1 !important;
        width: 100% !important;
        min-height: 3.8rem !important;
    }
}

@media (orientation: landscape) and (max-height: 600px) {
    body.ui-mode-ancient [data-component="text-box"] {
        height: clamp(190px, 42vh, 230px) !important;
        min-height: clamp(190px, 42vh, 230px) !important;
        max-height: clamp(190px, 42vh, 230px) !important;
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
/* Other：电影冷开场。 */
body.ui-mode-other #hook-overlay {
    overflow: hidden;
    box-sizing: border-box;
    padding: clamp(52px, 9vh, 108px) clamp(40px, 8vw, 156px);
    background:
        radial-gradient(ellipse at 50% 46%, rgba(104,108,118,.13), transparent 42%),
        linear-gradient(180deg, #08090b 0%, #15171b 50%, #07080a 100%);
    color: rgba(244,244,246,.92);
}

body.ui-mode-other #hook-overlay::before,
body.ui-mode-other #hook-overlay::after {
    content: "";
    position: absolute;
    z-index: 0;
    right: 0;
    left: 0;
    height: 22vh;
    pointer-events: none;
}

body.ui-mode-other #hook-overlay::before {
    top: 0;
    background: linear-gradient(180deg, rgba(2,2,3,.78), transparent);
}

body.ui-mode-other #hook-overlay::after {
    bottom: 0;
    background: linear-gradient(0deg, rgba(2,2,3,.82), transparent);
}

body.ui-mode-other #hook-text-content {
    position: relative;
    z-index: 1;
    justify-content: flex-end;
    box-sizing: border-box;
    width: min(72vw, 1080px);
    max-width: none;
    height: min(48vh, 480px);
    min-height: 340px;
    padding: clamp(42px, 5vh, 62px) clamp(18px, 3vw, 44px);
    overflow: hidden;
    background: transparent;
}

body.ui-mode-other #hook-text-content::before,
body.ui-mode-other #hook-text-content::after {
    content: "";
    position: absolute;
    width: clamp(70px, 8vw, 124px);
    height: 1px;
    pointer-events: none;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.36), transparent);
    box-shadow: 0 0 12px rgba(255,255,255,.06);
}

body.ui-mode-other #hook-text-content::before {
    top: 20px;
    left: 4%;
}

body.ui-mode-other #hook-text-content::after {
    right: 4%;
    bottom: 20px;
    opacity: .62;
}

body.ui-mode-other #hook-text-content p {
    width: 100%;
    max-width: 980px;
    margin: 0 auto clamp(13px, 1.7vh, 20px);
    padding: 0;
    color: rgba(239,239,242,.88);
    font-family: "Source Sans 3 Local", "Inter Local", Inter,
        "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: clamp(16px, 1.28vw, 22px);
    font-style: normal;
    font-weight: 350;
    line-height: 1.82;
    letter-spacing: .035em;
    text-align: center;
    text-wrap: balance;
    text-shadow: 0 4px 18px rgba(0,0,0,.9);
    animation: otherHookLineIn 620ms cubic-bezier(.16,1,.3,1) both;
    transition:
        color 420ms ease,
        opacity 420ms ease,
        filter 420ms ease;
}

body.ui-mode-other #hook-text-content p:not(:last-child) {
    color: rgba(221,222,226,.34) !important;
    opacity: .55 !important;
    filter: blur(.15px);
}

body.ui-mode-other #hook-text-content p:last-child {
    color: rgba(250,250,251,.96) !important;
    opacity: 1 !important;
    text-shadow:
        0 4px 18px rgba(0,0,0,.92),
        0 0 24px rgba(255,255,255,.035);
}

body.ui-mode-other #hook-cursor {
    display: inline-block;
    width: 1px;
    height: .92em;
    margin-left: 6px;
    vertical-align: text-bottom;
    background: rgba(255,255,255,.72);
    box-shadow: 0 0 8px rgba(255,255,255,.16);
    animation: hookBlink 1.05s step-end infinite;
    opacity: 0;
}

body.ui-mode-other.hook-active #hook-cursor { opacity: 1; }

body.ui-mode-other #hook-hint {
    position: relative;
    z-index: 1;
    margin-top: clamp(20px, 3vh, 34px);
    color: rgba(238,238,241,.28);
    font-family: "Inter Local", Inter, "PingFang SC", sans-serif;
    font-size: clamp(10px, .68vw, 12px);
    font-weight: 420;
    line-height: 1;
    letter-spacing: .22em;
    text-shadow: 0 2px 10px rgba(0,0,0,.8);
}

body.ui-mode-other #hook-flash { background: #e9eaed; }

@keyframes otherHookLineIn {
    from {
        opacity: 0;
        filter: blur(5px) brightness(.72);
        transform: translateY(10px);
    }
    to {
        opacity: 1;
        filter: blur(0) brightness(1);
        transform: translateY(0);
    }
}

@keyframes hookBlink { 0%,100% { opacity:1; } 50% { opacity:0; } }

@media (max-width: 800px) {
    body.ui-mode-other #hook-overlay {
        padding: 34px 28px;
    }

    body.ui-mode-other #hook-text-content {
        width: min(86vw, 820px);
        height: 56vh;
        min-height: 280px;
        padding: 34px 14px;
    }

    body.ui-mode-other #hook-text-content p {
        font-size: clamp(14px, 2.2vw, 18px);
        line-height: 1.72;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other #hook-overlay,
    body.ui-mode-other #hook-text-content,
    body.ui-mode-other #hook-text-content p,
    body.ui-mode-other #hook-cursor,
    body.ui-mode-other #hook-hint,
    body.ui-mode-other #hook-flash {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}
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

/* 多人站位时突出当前说话人。
   非说话角色保持原始亮度/清晰度，不做虚化、降亮度或透明度处理；
   说话角色只做轻微提亮、柔和描边和极小放大，避免抢画面或改变布局。 */
.manual-character-container.is-speaking {
    z-index: 8 !important;
}
.manual-character-container.is-speaking .character-image {
    filter: brightness(1.04)
            drop-shadow(0 0 1px rgba(255, 245, 220, 0.78))
            drop-shadow(0 0 4px rgba(255, 220, 150, 0.30)) !important;
    transform: translateX(-50%) scale(1.012) !important;
    transform-origin: center bottom !important;
}
.manual-character-container.is-not-speaking .character-image {
    opacity: 1 !important;
    filter: none !important;
    transform: translateX(-50%) !important;
}
/* 保留图片换图/初次加载的淡入，不让上面的原始亮度规则覆盖 loading 状态。 */
.manual-character-container.is-not-speaking .character-image[data-stage-loading="true"] {
    opacity: 0 !important;
}
.manual-character-container .character-image {
    transition: opacity 0.25s ease, filter 0.25s ease, transform 0.25s ease;
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
/* Ancient / Other 都不展示可视进度条；内部进度记录、存档和父端上报保持不变。 */
#game-progress-container {
    display: none !important;
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
        """NVL 全屏独白模式：现代电影旁白版式，保留打字机与点击推进逻辑。"""
        return """
/* 隐藏 UI 后的唤回按钮：只在隐藏态出现，右下角常驻但很淡，hover 才显形。
   设置入口本身在快捷菜单里，隐藏后若无出路，用户只能清 localStorage。 */
#ui-restore-toggle {
    position: fixed;
    right: 18px;
    bottom: 18px;
    width: 40px;
    height: 40px;
    display: none;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: rgba(12, 12, 14, 0.45);
    border: 1px solid rgba(255, 255, 255, 0.22);
    backdrop-filter: blur(4px);
    cursor: pointer;
    z-index: 9500;
    opacity: 0.18;
    transition: opacity 0.22s ease, background 0.22s ease, transform 0.22s ease;
}
body.other-quick-menu-hidden #ui-restore-toggle,
body.ancient-quick-menu-hidden #ui-restore-toggle {
    display: flex;
}
#ui-restore-toggle:hover,
#ui-restore-toggle:focus-visible {
    opacity: 0.95;
    background: rgba(20, 20, 24, 0.72);
    transform: scale(1.06);
    outline: none;
}
#ui-restore-toggle svg {
    width: 21px;
    height: 21px;
    fill: rgba(255, 255, 255, 0.9);
    pointer-events: none;
}
/* NVL 全屏独白时不要在角落留亮点破坏画面 */
body.nvl-active #ui-restore-toggle {
    display: none !important;
}

/* ==================== NVL 全屏电影旁白 ==================== */
/* 公共基线保持原样，确保 Ancient NVL 不发生视觉回归。 */
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
    transition: opacity 0.3s ease, visibility 0s linear 0.3s;
}
#nvl-overlay.open {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
    transition: opacity 0.3s ease;
}
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

/* Other 使用现代电影旁白版式。 */
body.ui-mode-other #nvl-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background:
        radial-gradient(ellipse at 50% 48%, rgba(63, 65, 72, .16), transparent 54%),
        linear-gradient(180deg, #0c0d10 0%, #15171b 48%, #0b0c0f 100%);
    z-index: 9700;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: clamp(54px, 9vh, 110px) clamp(42px, 8vw, 160px);
    box-sizing: border-box;
    overflow: hidden;
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    cursor: pointer;
    will-change: opacity;
    /* 关键：用 visibility+opacity 切换，避免 display:none 造成的整页重排卡顿。
       关闭时 visibility 延后到淡出结束再切；开启时立即可见。 */
    transition: opacity 0.3s ease, visibility 0s linear 0.3s;
}
body.ui-mode-other #nvl-overlay.open {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
    transition: opacity 0.3s ease;
}

body.ui-mode-other #nvl-overlay::before,
body.ui-mode-other #nvl-overlay::after {
    content: "";
    position: absolute;
    z-index: 2;
    right: 0;
    left: 0;
    height: clamp(54px, 11vh, 120px);
    pointer-events: none;
}

body.ui-mode-other #nvl-overlay::before {
    top: 0;
    background: linear-gradient(180deg, #0c0d10 12%, rgba(12,13,16,.82) 48%, transparent);
}

body.ui-mode-other #nvl-overlay::after {
    bottom: 0;
    background: linear-gradient(0deg, #0b0c0f 12%, rgba(11,12,15,.84) 48%, transparent);
}

/* 正文保持适中的电影旁白行宽，长英文不再铺满整个屏幕。 */
body.ui-mode-other #nvl-overlay .nvl-text {
    position: relative;
    z-index: 1;
    box-sizing: border-box;
    width: min(76vw, 1180px);
    height: min(60vh, 620px);
    min-height: min(60vh, 620px);
    max-height: none;
    margin: 0 auto;
    padding: clamp(42px, 5vh, 64px) clamp(36px, 4.4vw, 76px) !important;
    overflow-x: hidden;
    overflow-y: auto;

    background: transparent !important;
    border-top: 0 !important;
    border-bottom: 0 !important;
    border-left: 0 !important;
    border-right: 0 !important;
    box-shadow: none !important;
    backdrop-filter: none !important;

    color: rgba(241, 241, 243, .88);
    font-family: "Source Sans 3 Local", "Inter Local", Inter,
        "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: clamp(16px, 1.24vw, 21px);
    font-style: normal;
    font-weight: 350;
    font-feature-settings: "kern" 1;
    line-height: 1.9;
    letter-spacing: 0.026em;
    text-align: left;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: normal;
    text-wrap: pretty;
    text-shadow:
        0 3px 16px rgba(0, 0, 0, .92),
        0 0 24px rgba(255, 255, 255, .035);
    scrollbar-width: thin;
    scrollbar-color: rgba(255,255,255,.2) transparent;
}

body.ui-mode-other #nvl-overlay .nvl-text::before {
    content: "";
    position: absolute;
    top: clamp(18px, 2.4vh, 28px);
    left: 8%;
    width: clamp(74px, 10vw, 150px);
    height: 1px;
    margin: 0;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.42), transparent);
    box-shadow: 0 0 12px rgba(255,255,255,.08);
}

body.ui-mode-other #nvl-overlay .nvl-text::after {
    content: "";
    position: absolute;
    right: 8%;
    bottom: clamp(18px, 2.4vh, 28px);
    width: clamp(74px, 10vw, 150px);
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.24), transparent);
    box-shadow: 0 0 12px rgba(255,255,255,.05);
}

body.ui-mode-other #nvl-overlay .nvl-text::-webkit-scrollbar {
    width: 3px;
}

body.ui-mode-other #nvl-overlay .nvl-text::-webkit-scrollbar-track {
    background: transparent;
}

body.ui-mode-other #nvl-overlay .nvl-text::-webkit-scrollbar-thumb {
    background: linear-gradient(180deg, transparent, rgba(255,255,255,.2), transparent);
}

body.ui-mode-other #nvl-overlay .nvl-cursor {
    display: inline-block;
    width: 1px;
    height: .92em;
    background: rgba(255,255,255,.76);
    margin-left: 5px;
    vertical-align: text-bottom;
    box-shadow: 0 0 8px rgba(255,255,255,.16);
    animation: nvlBlink 1s step-end infinite;
}
/* NVL 正文也采用 Ancient 的阅读字号基准，避免两套 UI 在独白时跳变。 */
body.ui-mode-other #nvl-overlay .nvl-text {
    font-size: clamp(1.2rem, 2.4vw, 1.85rem) !important;
    line-height: 2.0 !important;
}
@keyframes nvlBlink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

@media (max-width: 900px) {
    body.ui-mode-other #nvl-overlay {
        padding: 38px clamp(28px, 6vw, 64px);
    }

    body.ui-mode-other #nvl-overlay .nvl-text {
        width: min(86vw, 900px);
        height: 62vh;
        min-height: 340px;
        max-height: none;
        padding: 36px 34px !important;
        font-size: clamp(15px, 2.1vw, 18px);
        line-height: 1.78;
    }
}

@media (max-width: 680px) {
    body.ui-mode-other #nvl-overlay {
        padding: 28px 24px;
    }

    body.ui-mode-other #nvl-overlay .nvl-text {
        width: 88vw;
        height: 66vh;
        min-height: 300px;
        max-height: none;
        padding: 30px 24px !important;
        font-size: 14px;
        line-height: 1.72;
        letter-spacing: .018em;
    }
}

@media (prefers-reduced-motion: reduce) {
    body.ui-mode-other #nvl-overlay,
    body.ui-mode-other #nvl-overlay .nvl-cursor {
        animation: none !important;
        transition-duration: 1ms !important;
    }
}

/* NVL 激活时隐藏 ADV 对话框和名字，避免底层残留文字。
   快捷菜单不能一起隐藏：Back / Skip / Save / Load / Settings 仍必须能
   在 NVL 独白期间操作。菜单置于 NVL 画布之上，点击由统一交互守卫隔离，
   不会再误触底层对话推进。 */
body.nvl-active [data-component="text-box"],
body.nvl-active [data-ui="who"] {
    opacity: 0 !important;
    pointer-events: none !important;
    transition: opacity 0.2s ease;
}

/* NVL overlay is z-index:9700.  Raise only the quick menu above it; the
   underlying game/text layers remain below the overlay.  Keep both the
   custom-element and Monogatari's data-component form for engine versions
   that attach the marker at different points in the mount lifecycle. */
body.nvl-active quick-menu,
body.nvl-active [data-component="quick-menu"] {
    opacity: 1 !important;
    visibility: visible !important;
    pointer-events: auto !important;
    z-index: 9701 !important;
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

/* 对话框浮在 CG 大图之上；CG 字幕保留，但不显示说话人姓名。 */
body.cg-active [data-component="text-box"] {
    z-index: 10 !important;
    opacity: 1 !important;
    visibility: visible !important;
}
body.cg-active [data-component="text-box"] [data-content="name"],
body.cg-active [data-component="text-box"] [data-ui="who"] {
    display: none !important;
}
"""

    def generate_utility_code(self, chapter_data=None, node_id='', depth=1, include_character_maps=True,
                              chapter_title='', chapter_titles_by_lang=None):
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
        chapter_title = str(chapter_title or '').strip() or f"Chapter {chap_id}"
        chapter_title_map = {
            str(lang): str(title or '').strip()
            for lang, title in (chapter_titles_by_lang or {}).items()
            if str(title or '').strip()
        }
        if not chapter_title_map:
            chapter_title_map = {str(self.default_language or 'en-US'): chapter_title}
        chapter_title_map.setdefault(str(self.default_language or 'en-US'), chapter_title)
        chapter_title_js = json.dumps(chapter_title, ensure_ascii=False)
        chapter_titles_js = json.dumps(chapter_title_map, ensure_ascii=False)
        choice_text_map = {
            str(lang): {str(key): str(value) for key, value in (values or {}).items() if value is not None}
            for lang, values in (self.story_choice_text_by_lang or {}).items()
            if isinstance(values, dict)
        }
        choice_text_js = json.dumps(choice_text_map, ensure_ascii=False)

        character_id_mapping = {name: info['id'] for name, info in self.character_map.items() if name != "旁白"}
        character_file_mapping = {info['id']: info['image'] for name, info in self.character_map.items() if
                                  name != "旁白"}

        # Serialize to valid JS objects
        character_id_mapping_js = json.dumps(character_id_mapping, ensure_ascii=False)
        character_file_mapping_js = json.dumps(character_file_mapping, ensure_ascii=False)
        task_id_js = json.dumps(str(self.task_id or ''), ensure_ascii=False)
        chapter_id_js = json.dumps(chap_id, ensure_ascii=False)
        node_id_js = json.dumps(str(node_id or ''), ensure_ascii=False)
        # Use a stable, task-scoped namespace for our own snapshots.  Native
        # Monogatari save keys are left untouched for compatibility with the
        # host, while custom variable/branch snapshots cannot leak across
        # games opened on the same origin.
        _storage_seed = re.sub(
            r'[^A-Za-z0-9._-]+', '_',
            str(self.task_id or node_id or 'game')
        ).strip('_') or 'game'
        storage_namespace_js = json.dumps(
            f"VN_{_storage_seed}", ensure_ascii=False
        )
        player_schema_js = json.dumps(
            getattr(self, '_player_variables_schema', {}) or {},
            ensure_ascii=False,
        )

        css_content = self.generate_visual_styles()

        lines = []

        # options.js declares `const monogatari` in the global lexical scope;
        # expose the same engine on window for generated utility modules and
        # browser integrations that intentionally use window-scoped globals.
        lines.append("try { if (typeof monogatari !== 'undefined' && monogatari && !window.monogatari) window.monogatari = monogatari; } catch(e) {}")

        # 注入特效库
        lines.append(AtmosphereEffects.generate_effect_library_js())

        # 系统面板开关：全文 JSON 没有任何 system_text 时关闭，前端不渲染按钮和面板
        lines.append(f"window._enableSystemPanel = {str(bool(self._has_system_text)).lower()};")
        lines.append(f"window._enableStatsPanel = {str(bool(self._has_player_variables)).lower()};")
        lines.append(f"window._uiMode = {json.dumps(self.ui_mode, ensure_ascii=False)};")
        lines.append(f"window._chapterTitlesByLang = {chapter_titles_js};")
        lines.append(f"window._chapterTitle = {chapter_title_js};")
        lines.append(f"window._choiceTextByLang = {choice_text_js};")
        lines.append(f"window._gameTaskId = {task_id_js};")
        lines.append(f"window._gameChapterId = {chapter_id_js};")
        lines.append(f"window._gameNodeId = {node_id_js};")
        lines.append(f"window._storageNamespace = {storage_namespace_js};")
        lines.append(f"window._playerVariableSchema = {player_schema_js};")
        lines.append("window._scopedStorageKey = function(suffix){ return String(window._storageNamespace || 'VN_game') + '_' + String(suffix || ''); };")
        lines.append("window._messageMatchesContext = function(event, data){")
        lines.append("    if (window.parent && window.parent !== window && (!event || event.source !== window.parent)) return false;")
        lines.append("    data = data || {};")
        lines.append("    if (data.task_id != null && String(window._gameTaskId || '') && String(data.task_id) !== String(window._gameTaskId)) return false;")
        lines.append("    if (data.chapter_id != null && String(window._gameChapterId || '') && String(data.chapter_id) !== String(window._gameChapterId)) return false;")
        lines.append("    if (data.node_id != null && String(window._gameNodeId || '') && String(data.node_id) !== String(window._gameNodeId)) return false;")
        lines.append("    return true;")
        lines.append("};")
        lines.append("window._parentMessageOrigin = (function(){ try { var ref = document.referrer; if (ref) { var origin = new URL(ref, location.href).origin; if (origin && origin !== 'null') return origin; } } catch(e) {} return '*'; })();")
        # Keep the existing host protocol's wildcard targetOrigin.  The game
        # already validates incoming messages by source/context, while a
        # guessed referrer origin can silently drop messages behind proxies.
        lines.append("window._postToParent = function(message){ if (!(window.parent && window.parent !== window)) return false; try { window.parent.postMessage(message, '*'); return true; } catch(e) { return false; } };")
        lines.append("window._sanitizePlayerVariables = function(raw){")
        lines.append("    var schema = window._playerVariableSchema || {}; var out = {}; raw = raw && typeof raw === 'object' ? raw : {};")
        lines.append("    var keys = Object.keys(schema); if (!keys.length) keys = Object.keys(raw);")
        lines.append("    keys.forEach(function(key){ var value = Number(raw[key]); var initial = Number(schema[key]); if (!Number.isFinite(initial)) initial = 0; if (!Number.isFinite(value)) value = initial; if (Number.isFinite(value)) out[key] = Math.max(0, Math.min(100, value)); });")
        lines.append("    return out;")
        lines.append("};")
        lines.append("window._sanitizeChoiceStats = function(raw){ var out={}; raw=raw&&typeof raw==='object'?raw:{}; Object.keys(raw).forEach(function(key){ var v=raw[key]||{}; var positive=Math.max(0,Number(v.positive)||0), effective=Math.max(0,Number(v.effective)||0); if(Number.isFinite(positive)&&Number.isFinite(effective)) out[key]={positive:Math.min(100000,positive),effective:Math.min(100000,effective)}; }); return out; };")
        lines.append("window._sanitizeLegacyFlags = function(raw){ var out={}; raw=raw&&typeof raw==='object'?raw:{}; Object.keys(raw).forEach(function(key){ var value=Number(raw[key]); if(Number.isFinite(value)&&value>0) out[key]=Math.min(100000,value); }); return out; };")

        lines.append(f"""
// ==================== 主站退出桥接 ====================
// 仅游戏首页的 ESC / Quit 会发送 GAME_EXIT_REQUEST。
// 游戏过程中与 Settings 内的 ESC 仍完全交给 Monogatari 自身处理。
window.GameHostBridge = {{
    _lastExitAt: 0,
    _chapterReadySent: false,
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
    notifyChapterReady: function(source) {{
        if (this._chapterReadySent) return false;
        if (!(window.parent && window.parent !== window)) return false;
        var now = Date.now();
        var data = {{
            protocol_version: 1,
            status: 'ready',
            lifecycle: 'initialized',
            task_id: {task_id_js},
            chapter_id: {chapter_id_js},
            node_id: {node_id_js},
            chapter_title: String(window._chapterTitle || ''),
            language: String(window._lang || window._defaultLang || 'en-US'),
            ui_mode: String(window._uiMode || 'Other'),
            engine_ready: !!window.monogatari,
            ui_ready: !!document.querySelector('visual-novel'),
            stats_enabled: !!window._enableStatsPanel,
            system_enabled: !!window._enableSystemPanel,
            document_ready_state: document.readyState,
            source: String(source || 'monogatari_init_complete'),
            timestamp: now
        }};
        this._chapterReadySent = true;
        document.dispatchEvent(new CustomEvent('chapter-client-ready', {{ detail: data }}));
        window._postToParent({{ type: 'CHAPTER_CLIENT_READY', data: data }});
        console.log('📤 [GameHostBridge] CHAPTER_CLIENT_READY', data);
        return true;
    }},
    requestExit: function(source) {{
        var now = Date.now();
        if (now - this._lastExitAt < 350) return false;
        this._lastExitAt = now;
        // Flush the beat that is currently visible before the host tears down
        // this iframe.  The old flow only sent GAME_EXIT_REQUEST, so the host
        // kept the previous step and the next visit showed Start instead of
        // Continue.
        try {{
            if (window.GameProgressReporter && typeof window.GameProgressReporter.flushForExit === 'function') {{
                window.GameProgressReporter.flushForExit('user_exit');
            }}
        }} catch (e) {{ console.warn('[GameHostBridge] progress flush failed:', e); }}
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
            window._postToParent({{ type: 'GAME_EXIT_REQUEST', data: data }});
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

// ==================== ESC 交还浏览器（退出全屏）====================
// 引擎自带 keymaster 绑定：游戏中按 ESC 开设置、设置中按 ESC 回游戏。
// 但页面通常以原生全屏嵌入，浏览器强制提示“按 ESC 退出全屏”（该提示不在
// DOM 里、无法隐藏），两者语义冲突。这里在捕获阶段吃掉 ESC，让它只用于
// 退出全屏；设置面板仍由快捷菜单进入，隐藏 UI 时靠右下角唤出按钮或右键。
document.addEventListener('keydown', function(event) {{
    if (event.key !== 'Escape' || event.repeat) return;
    // 主菜单的 ESC 退出握手由上面那个更早注册的监听器处理，走到这里说明
    // 不在主菜单，直接阻止引擎的设置开关。
    event.stopImmediatePropagation();
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
    // ==================== 游戏 UI 事件隔离 ====================
    // Monogatari 的 game-screen 会把未停止冒泡的 click 当作推进剧情。
    // UI 自身仍先执行，事件到达 UI 容器后才停止冒泡，避免按钮穿透。
    window.GameplayInteractionGuard = window.GameplayInteractionGuard || {{
        _installed: false,
        selectors: [
            '[data-component="quick-menu"]', 'quick-menu',
            'dialog-log', '[data-component="dialog-log"]',
            'settings-screen', '[data-component="settings-screen"]',
            'save-screen', '[data-component="save-screen"]',
            'load-screen', '[data-component="load-screen"]',
            'help-screen', '[data-component="help-screen"]',
            'alert-modal', 'message-modal',
            'choice-container', '[data-component="choice-container"]'
        ],
        install: function() {{
            if (this._installed) return;
            this._installed = true;
            var self = this;
            // UI controls live inside game-screen. Monogatari's delegated
            // game-screen click handler would otherwise treat a menu click as
            // "next dialogue", while stopping propagation at the UI container
            // also prevents the actual open/save/load/settings handler from
            // running. Mark UI clicks and let the proceed guard below suppress
            // only the accidental gameplay advance.
            var selector = self.selectors.join(',');
            var markUiClick = function(event) {{
                var target = event.target && event.target.closest ? event.target.closest(selector) : null;
                if (!target) return;
                window.__uiClickInProgress = true;
                if (window.__uiClickClearTimer) clearTimeout(window.__uiClickClearTimer);
                window.__uiClickClearTimer = setTimeout(function() {{
                    window.__uiClickInProgress = false;
                    window.__uiClickClearTimer = null;
                }}, 0);
            }};
            document.addEventListener('click', markUiClick, true);
            // Handle Back on every non-game screen at capture phase. Some
            // Monogatari versions bind this listener through a delegated
            // component and can miss it after custom UI decoration; handling
            // it here keeps Settings/Save/Load from getting stuck open while
            // still leaving the in-game Back button to the normal rollback
            // and BranchNavigator paths.
            document.addEventListener('click', function(event) {{
                var target = event.target && event.target.closest ? event.target : null;
                var back = target && target.closest('[data-action="back"]');
                if (!back) return;
                var screen = back.closest('[data-screen]');
                if (!screen || screen.getAttribute('data-screen') === 'game') return;
                event.preventDefault();
                event.stopImmediatePropagation();
                event.stopPropagation();
                try {{
                    var isPlaying = (typeof monogatari !== 'undefined' && monogatari.global) ? !!monogatari.global('playing') : false;
                    if (typeof monogatari !== 'undefined' && monogatari.showScreen) monogatari.showScreen(isPlaying ? 'game' : 'main');
                    if (window.SmartAutoPlay) window.SmartAutoPlay.resumeFromScreen();
                    window._screenOpenedFromMain = false;
                }} catch (e) {{ console.warn('[UI Back] failed to close screen', e); }}
            }}, true);
            // Patch proceed once: the delegated game-screen listener still
            // receives the event, but cannot advance while a UI click is being
            // dispatched. Programmatic proceed/auto-play remains unaffected.
            if (typeof monogatari !== 'undefined' && monogatari && typeof monogatari.proceed === 'function' && !monogatari.__uiProceedGuarded) {{
                var originalProceed = monogatari.proceed.bind(monogatari);
                monogatari.proceed = function(options) {{
                    if (window.__uiClickInProgress) return Promise.resolve(false);
                    // Always hand back a promise. The engine's own keyboard
                    // shortcut does `proceed(...).then(...)`, so any path that
                    // returns undefined (or throws synchronously) raised
                    // "Cannot read properties of undefined (reading 'then')"
                    // on every space press and flooded the error reporter.
                    var result;
                    try {{ result = originalProceed(options); }} catch (e) {{ return Promise.reject(e); }}
                    return (result && typeof result.then === 'function') ? result : Promise.resolve(result);
                }};
                monogatari.__uiProceedGuarded = true;
            }}
        }}
    }};
    window.GameplayInteractionGuard.install();

    const customStyles = `{css_content}`;
    const styleSheet = document.createElement("style");
    styleSheet.textContent = customStyles;
    document.head.appendChild(styleSheet);

    if (document.body) {{
        document.body.classList.toggle('ui-mode-ancient', window._uiMode === 'Ancient');
        document.body.classList.toggle('ui-mode-other', window._uiMode !== 'Ancient');
    }}

    // ── ADV 正文自动跟随 ──
    // 正文溢出后始终滚到最新位置，保证打字机追加新字符时用户能看到末尾。
    // 只监听普通 text-box，不干扰 NVL overlay 的独立阅读/滚动行为。
    window.DialogAutoScroll = window.DialogAutoScroll || {{
        _textEl: null,
        _textObserver: null,
        _rootObserver: null,
        _resizeObserver: null,
        _raf: null,
        _queue: function() {{
            if (this._raf) return;
            var self = this;
            this._raf = requestAnimationFrame(function() {{
                self._raf = null;
                var el = self._textEl;
                if (!el || !el.isConnected) return;
                if (el.scrollHeight > el.clientHeight + 1) el.scrollTop = el.scrollHeight;
            }});
        }},
        _bind: function() {{
            var el = document.querySelector('game-screen [data-component="text-box"] [data-content="text"], [data-screen="game"] [data-component="text-box"] [data-content="text"]');
            if (el === this._textEl) {{ this._queue(); return; }}
            if (this._textObserver) this._textObserver.disconnect();
            if (this._resizeObserver) this._resizeObserver.disconnect();
            this._textEl = el || null;
            if (!el) return;
            var self = this;
            this._textObserver = new MutationObserver(function() {{ self._queue(); }});
            this._textObserver.observe(el, {{ childList: true, characterData: true, subtree: true }});
            if (typeof ResizeObserver !== 'undefined') {{
                this._resizeObserver = new ResizeObserver(function() {{ self._queue(); }});
                this._resizeObserver.observe(el);
            }}
            this._queue();
        }},
        init: function() {{
            var self = this;
            this._bind();
            this._rootObserver = new MutationObserver(function() {{ self._bind(); }});
            this._rootObserver.observe(document.body || document.documentElement, {{ childList: true, subtree: true }});
        }}
    }};
    window.DialogAutoScroll.init();

    // Other 加载页补充章节标识与实时百分比；不改动 Monogatari 的预加载流程。
    if (window._uiMode !== 'Ancient') {{
        const decorateLoadingScreen = function(attempt) {{
            const screen = document.querySelector('loading-screen, [data-screen="loading"]');
            const root = screen && (screen.shadowRoot || screen);
            const wrapper = root && root.querySelector('[data-content="wrapper"]');
            const progress = root && root.querySelector('[data-content="progress"], progress');

            if (!screen || !wrapper || !progress) {{
                if ((attempt || 0) < 40) setTimeout(() => decorateLoadingScreen((attempt || 0) + 1), 100);
                return;
            }}

            let meta = wrapper.querySelector('.other-loading-meta');
            if (!meta) {{
                meta = document.createElement('div');
                meta.className = 'other-loading-meta';
                wrapper.appendChild(meta);
            }}
            meta.textContent = String(window._chapterTitle || 'STORY').toUpperCase();

            let percent = wrapper.querySelector('.other-loading-percent');
            if (!percent) {{
                percent = document.createElement('div');
                percent.className = 'other-loading-percent';
                wrapper.appendChild(percent);
            }}

            const sync = function() {{
                const max = Math.max(1, Number(progress.max || progress.getAttribute('max') || 100));
                const value = Math.max(0, Number(progress.value || progress.getAttribute('value') || 0));
                percent.textContent = String(Math.min(100, Math.round((value / max) * 100))).padStart(2, '0') + '%';
            }};

            sync();
            const observer = new MutationObserver(sync);
            observer.observe(progress, {{ attributes: true, attributeFilter: ['value', 'max'] }});

            // 引擎某些版本只更新 progress DOM property，不触发 attribute mutation。
            let checks = 0;
            const timer = setInterval(() => {{
                sync();
                checks++;
                if (checks > 300 || Number(progress.value || 0) >= Number(progress.max || 1)) {{
                    clearInterval(timer);
                    observer.disconnect();
                }}
            }}, 100);
        }};
        decorateLoadingScreen(0);
    }}

    const overlayDiv = document.createElement("div");
    overlayDiv.id = "cinematic-overlay";
    if (document.body) document.body.appendChild(overlayDiv);

    // ── 数值面板 DOM ──
    const statsToggle = window._enableStatsPanel ? document.createElement("div") : null;
    const statsPanel = window._enableStatsPanel ? document.createElement("div") : null;
    if (statsToggle && statsPanel) {{
        statsToggle.id = "stats-toggle";
        statsToggle.title = "Stats";
        statsToggle.setAttribute("aria-label", "Stats");
        statsToggle.textContent = "◈";
        document.body.appendChild(statsToggle);

        statsPanel.id = "stats-panel";
        document.body.appendChild(statsPanel);
    }}

    window._statsDismissedByUser = false;
    if (statsToggle && statsPanel) statsToggle.addEventListener("click", function() {{
        const shouldOpen = !statsPanel.classList.contains("open");
        statsPanel.classList.toggle("open", shouldOpen);
        window._statsDismissedByUser = !shouldOpen;
        if (shouldOpen && window.updateStatsUI) window.updateStatsUI();
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
        hideMainSettings: function() {{
            // Hide Settings only on the title screen.  The in-game quick-menu
            // Settings button is intentionally left untouched.
            document.querySelectorAll('main-screen main-menu, [data-screen="main"] main-menu').forEach((menu) => {{
                Array.from(menu.querySelectorAll('button, [role="button"]')).forEach((button) => {{
                    const action = String(button.getAttribute('data-action') || '').toLowerCase();
                    const open = String(button.getAttribute('data-open') || button.getAttribute('data-screen') || '').toLowerCase();
                    const key = String(button.getAttribute('data-string') || button.getAttribute('string') || '').toLowerCase();
                    const label = String(button.getAttribute('aria-label') || button.textContent || '').trim().toLowerCase();
                    const isSettings = open === 'settings' || action === 'settings' || key === 'settings' ||
                        /(^|\\s)(settings|setting|设置|設定|環境設定)(\\s|$)/i.test(label);
                    if (!isSettings) return;
                    button.classList.add('is-main-settings-hidden');
                    button.hidden = true;
                    button.setAttribute('aria-hidden', 'true');
                    button.setAttribute('tabindex', '-1');
                    button.style.setProperty('display', 'none', 'important');
                }});
            }});
        }},
        hideMainQuickMenuDistractionFree: function() {{
            // Monogatari renders the same quick-menu item as “Hide” or
            // “Show” depending on distraction_free state. Keep this guard
            // scoped to quick-menu only; Settings Show/Hide stays available.
            document.querySelectorAll('quick-menu, [data-component="quick-menu"]').forEach((menu) => {{
                const hide = (candidate) => {{
                    if (!candidate) return;
                    // Monogatari puts data-action/data-string on different
                    // descendants across engine versions. Always hide the
                    // actual button, not only the icon or label span.
                    const button = (candidate.matches && candidate.matches('button, [role="button"]'))
                        ? candidate
                        : (candidate.closest ? candidate.closest('button, [role="button"]') : null) || candidate;
                    if (!button || !menu.contains(button)) return;
                    button.classList.add('is-distraction-free-hidden');
                    button.hidden = true;
                    button.setAttribute('aria-hidden', 'true');
                    button.setAttribute('tabindex', '-1');
                    button.style.setProperty('display', 'none', 'important');
                }};
                menu.querySelectorAll([
                    '[data-action="distraction-free"]',
                    '[data-string="Hide"]', '[data-string="Show"]',
                    '[aria-label="Hide"]', '[aria-label="Show"]',
                    '[title="Hide"]', '[title="Show"]',
                    '.fa-eye', '.fa-eye-slash'
                ].join(',')).forEach(hide);
                // Fallback for localized/re-rendered labels where the
                // identifying data attributes were removed by the engine.
                menu.querySelectorAll('button, [role="button"]').forEach((button) => {{
                    const action = String(button.getAttribute('data-action') || '').toLowerCase();
                    const text = String(button.textContent || '').replace(/[「」]/g, '').trim().toLowerCase();
                    if (action === 'distraction-free' || /^(hide|show|表示|非表示|ショー)$/.test(text)) hide(button);
                }});
                // Do not remove the stock item from Monogatari's configuration
                // here: dialog-log inserts its Log button immediately after
                // the Hide anchor during engine setup. Removing that anchor
                // too early would make Log disappear. CSS + this runtime
                // guard hide the item without changing menu registration.
            }});
        }},
        isGeneratedSaveName: function(value) {{
            const text = String(value || '').trim();
            if (!/[0-9][0-9][0-9][0-9]/.test(text)) return false;
            const hasTime = /(?:[0-9][0-9]?:[0-9][0-9]|上午|下午|am|pm)/i.test(text);
            const hasDate = /(?:[年月日]|january|february|march|april|may|june|july|august|september|october|november|december|[0-9][.-][0-9])/i.test(text) || text.indexOf('/') !== -1;
            return hasTime && hasDate;
        }},
        syncSaveScreen: function() {{
            const screen = document.querySelector('save-screen, [data-component="save-screen"]');
            if (screen) {{
                const field = screen.querySelector(':scope > .horizontal');
                const input = screen.querySelector('input[data-content="slot-name"]');
                const button = screen.querySelector('button[data-action="save"]');
                const fieldLabel = window.__ui('saveName');
                const buttonLabel = window.__ui('createSave');
                const chapterTitle = String(window._chapterTitle || window.__ui('currentProgress')).trim();

                if (field) {{
                    if (field.hasAttribute('data-other-label')) field.removeAttribute('data-other-label');
                    field.dataset.otherPageTitle = window.__ui('savePage');
                }}
                if (input) {{
                    input.value = chapterTitle;
                    input.placeholder = chapterTitle;
                    input.setAttribute('aria-label', chapterTitle);
                }}
                if (button && button.dataset.otherSaveLabel !== buttonLabel) {{
                    window.__setUiButtonLabel(button, buttonLabel);
                    button.dataset.otherSaveLabel = buttonLabel;
                }}
            }}

            const chapterTitle = String(window._chapterTitle || window.__ui('currentProgress')).trim();
            const genericNames = [
                '手动存档', '当前剧情进度',
                '手動セーブ', '現在の進行状況',
                'Manual save', 'Current story progress'
            ];
            document.querySelectorAll('save-screen save-slot .badge, load-screen save-slot .badge, [data-component="save-slot"] .badge').forEach((badge) => {{
                const currentName = String(badge.textContent || '').trim();
                const generated = badge.dataset.otherGeneratedSaveName === '1' || this.isGeneratedSaveName(currentName) || genericNames.indexOf(currentName) !== -1;
                if (!generated) return;
                badge.dataset.otherGeneratedSaveName = '1';
                if (badge.textContent !== chapterTitle) badge.textContent = chapterTitle;
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
        ensureSkipButton: function() {{
            const menu = document.querySelector('quick-menu, [data-component="quick-menu"]');
            if (!menu || menu.querySelector('.game-skip-button')) return;
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'game-skip-button';
            button.setAttribute('data-action', 'skip');
            button.innerHTML = '<span class="fas fa-forward" data-icon="skip"></span><span data-string="Skip"></span>';
            menu.appendChild(button);
            if (window.__refreshEngineButtonLabels) window.__refreshEngineButtonLabels();
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
            this.hideMainSettings();
            this.hideMainQuickMenuDistractionFree();
            this.ensureSkipButton();
            if (window.__refreshEngineButtonLabels) window.__refreshEngineButtonLabels();
            this.syncSaveScreen();
            this.syncSpeakerNames();
            this.centerPromptModals();
            const gameActive = !!document.querySelector('game-screen.active, [data-screen="game"].active');
            document.body.classList.toggle('gameplay-active', gameActive);
            const statsAvailable = window._enableStatsPanel &&
                !document.body.classList.contains('ancient-quick-menu-hidden') &&
                !document.body.classList.contains('other-quick-menu-hidden');
            if (statsPanel && gameActive && statsAvailable && !window._statsDismissedByUser) {{
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

    // ── Other 电影终幕：最终节点先完成视觉收束，再交还给引擎结束游戏 ──
    if (window._uiMode !== 'Ancient') {{
        const otherEndScreen = document.createElement('div');
        otherEndScreen.id = 'other-end-screen';
        otherEndScreen.setAttribute('aria-hidden', 'true');
        otherEndScreen.innerHTML = `
            <div class="other-end-content">
                <div class="other-end-kicker">Final Frame</div>
                <h1 class="other-end-title">THE END</h1>
                <div class="other-end-rule"></div>
                <p class="other-end-epilogue">Every ending leaves something behind.</p>
                <div class="other-end-stats"></div>
            </div>
            <div class="other-end-hint">CLICK OR PRESS ANY KEY TO CONTINUE</div>
        `;
        document.body.appendChild(otherEndScreen);

        window.OtherEndScreen = {{
            _resolve: null,
            _armedAt: 0,
            _escape: function(value) {{
                return String(value == null ? '' : value)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#039;');
            }},
            _finish: function() {{
                if (!this._resolve || Date.now() < this._armedAt) return;
                const resolve = this._resolve;
                this._resolve = null;
                otherEndScreen.classList.remove('open');
                otherEndScreen.setAttribute('aria-hidden', 'true');
                document.removeEventListener('keydown', this._keyHandler, true);
                window._userQuitFlag = true;
                resolve(true);
            }},
            _keyHandler: function(event) {{
                if (event.key === 'Tab' || event.key === 'Shift' || event.key === 'Control' || event.key === 'Alt' || event.key === 'Meta') return;
                event.preventDefault();
                window.OtherEndScreen._finish();
            }},
            show: function() {{
                if (window._uiMode === 'Ancient') return Promise.resolve(true);

                const lang = String(window._lang || document.documentElement.lang || '');
                const ja = lang === 'ja-JP';
                const kicker = otherEndScreen.querySelector('.other-end-kicker');
                const title = otherEndScreen.querySelector('.other-end-title');
                const epilogue = otherEndScreen.querySelector('.other-end-epilogue');
                const hint = otherEndScreen.querySelector('.other-end-hint');
                const stats = otherEndScreen.querySelector('.other-end-stats');

                if (ja) {{
                    if (kicker) kicker.textContent = 'FINAL FRAME';
                    if (title) title.textContent = '終 幕';
                    if (epilogue) epilogue.textContent = '物語は幕を閉じ、その余韻だけが残る。';
                    if (hint) hint.textContent = 'クリックまたは任意のキーで続行';
                }} else {{
                    if (kicker) kicker.textContent = 'FINAL FRAME';
                    if (title) title.textContent = 'THE END';
                    if (epilogue) epilogue.textContent = 'Every ending leaves something behind.';
                    if (hint) hint.textContent = 'CLICK OR PRESS ANY KEY TO CONTINUE';
                }}

                const vars = window._playerVariables || {{}};
                const entries = Object.entries(vars).slice(0, 5);
                if (stats) {{
                    stats.classList.toggle('is-empty', entries.length === 0);
                    stats.innerHTML = entries.map(([key, value]) => `
                        <div class="other-end-stat">
                            <span>${{window.OtherEndScreen._escape(key)}}</span>
                            <strong>${{window.OtherEndScreen._escape(value)}}</strong>
                        </div>
                    `).join('');
                }}

                otherEndScreen.classList.add('open');
                otherEndScreen.setAttribute('aria-hidden', 'false');
                this._armedAt = Date.now() + 500;
                document.addEventListener('keydown', this._keyHandler, true);
                return new Promise((resolve) => {{ this._resolve = resolve; }});
            }}
        }};

        otherEndScreen.addEventListener('click', () => window.OtherEndScreen._finish());
    }}

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
            <span class="sys-title">System</span>
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

    // ── 隐藏 UI 后的唤回入口 ──
    // 设置面板里的 Hide 会隐藏快捷菜单，而设置入口本身就在快捷菜单里；ESC 已
    // 交还浏览器用于退出全屏，所以隐藏后必须另留出路，否则用户只能清
    // localStorage（隐藏状态是持久化的，刷新也回不来）。
    // 两条出路：右下角淡按钮（看得见）+ 右键唤回（galgame 肌肉记忆）。
    const uiRestoreBtn = document.createElement("div");
    uiRestoreBtn.id = "ui-restore-toggle";
    uiRestoreBtn.setAttribute("role", "button");
    uiRestoreBtn.setAttribute("tabindex", "0");
    uiRestoreBtn.setAttribute("aria-label", (window.__ui ? window.__ui('show') : 'Show'));
    uiRestoreBtn.title = (window.__ui ? window.__ui('show') : 'Show');
    uiRestoreBtn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5c-7 0-10 7-10 7s3 7 10 7 10-7 10-7-3-7-10-7zm0 11a4 4 0 1 1 0-8 4 4 0 0 1 0 8z"/></svg>';
    document.body.appendChild(uiRestoreBtn);

    window.UiVisibility = {{
        isHidden: function() {{
            return document.body.classList.contains('other-quick-menu-hidden')
                || document.body.classList.contains('ancient-quick-menu-hidden');
        }},
        // 走各模式已有的 setQuickMenuVisible，以便 localStorage、统计面板、
        // 设置页里 Show/Hide 的选中态一起同步，不要直接摘 class。
        restore: function(source) {{
            if (!this.isHidden()) return false;
            var handled = false;
            try {{
                if (document.body.classList.contains('ui-mode-ancient')
                    && window.AncientUI && window.AncientUI.setQuickMenuVisible) {{
                    window.AncientUI.setQuickMenuVisible(true);
                    handled = true;
                }} else if (window.OtherUI && window.OtherUI.setQuickMenuVisible) {{
                    window.OtherUI.setQuickMenuVisible(true);
                    handled = true;
                }}
            }} catch (e) {{
                console.warn('[UiVisibility] restore via settings failed', e);
            }}
            if (!handled) {{
                // 兜底：至少让 UI 回来，不要把用户困死。
                document.body.classList.remove('other-quick-menu-hidden');
                document.body.classList.remove('ancient-quick-menu-hidden');
            }}
            console.log('👁️ [UiVisibility] UI 已恢复显示 (来源: ' + (source || 'unknown') + ')');
            return true;
        }}
    }};

    uiRestoreBtn.addEventListener("click", function(event) {{
        event.preventDefault();
        event.stopPropagation();
        window.UiVisibility.restore('restore-button');
    }});
    uiRestoreBtn.addEventListener("keydown", function(event) {{
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        event.stopPropagation();
        window.UiVisibility.restore('restore-button-key');
    }});

    // 右键唤回：只在隐藏状态拦截，否则浏览器原生右键菜单照常工作。
    document.addEventListener("contextmenu", function(event) {{
        if (!window.UiVisibility || !window.UiVisibility.isHidden()) return;
        event.preventDefault();
        window.UiVisibility.restore('contextmenu');
    }});

    // ── NVL 全屏独白 DOM（黑底白字打字机）──
    const nvlOverlay = document.createElement("div");
    nvlOverlay.id = "nvl-overlay";
    nvlOverlay.innerHTML = `<div class="nvl-text"></div>`;
    // 必须挂在 game-screen 内，和 CG 一样。挂到 body 时这个全屏 overlay 会盖住
    // [data-screen="game"]，引擎的 shouldProceed() 用 isVisible() 判定该元素，
    // 于是 proceed() 每次都被 "Game screen is not visible" 拒绝，NVL 点不动。
    const nvlHost = document.querySelector("game-screen") || document.body;
    nvlHost.appendChild(nvlOverlay);
    // 点 NVL 任意处 → NvlOverlay.requestAdvance()（打字中补全 / 已打完则推进）。
    //
    // 必须用 document 上的事件委托，不能绑在 nvlOverlay 这个元素引用上。
    // 引擎重建 game-screen 时会销毁或克隆其子节点，克隆不复制监听器，于是页面上
    // 留下一个长得一样、却没有任何监听器的 #nvl-overlay：点击落上去毫无反应，
    // finishTyping() 永远不被调用，表现为“点一下没反应、文字继续逐字打”。
    // 捕获阶段拦下，顺带阻止引擎挂在 game-screen 上的委托监听器
    // '[data-screen="game"] *:not([data-choice])' → proceed() 抢跑，
    // 否则同一次点击会既补全又推进，看起来像一点即跳。
    document.addEventListener("click", function(event) {{
        var node = event.target;
        if (!node || !node.closest) return;
        if (!node.closest('#nvl-overlay')) return;
        if (!window.NvlOverlay || !window.NvlOverlay.isOpen()) return;
        event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
        window.NvlOverlay.requestAdvance();
    }}, true);

    // NVL 打开时把空格/回车接到同一个入口。引擎自己绑了 ["right","space"] → proceed()，
    // 那条路径只会补全被 CSS 隐藏的那层 Dialog，中央大字继续逐字打 —— 按下去看起来没反应。
    // 捕获阶段拦下并阻止 mousetrap 收到按键，NVL 的空格才和点击行为一致。
    // 方向键已在别处全局禁用，这里只管空格与回车。
    document.addEventListener("keydown", function(event) {{
        if (event.key !== ' ' && event.key !== 'Enter') return;
        if (event.repeat) return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (!window.NvlOverlay || !window.NvlOverlay.isOpen()) return;
        var node = event.target;
        var tag = node && node.tagName ? node.tagName.toLowerCase() : '';
        // 文本录入控件必须让路，空格是内容而不是推进。
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
        if (node && node.isContentEditable) return;
        // 设置/存档/读档等面板打开时不抢键：那些界面自己要用回车确认。
        if (node && node.closest && node.closest('[data-screen]:not([data-screen="game"])')) return;
        // 不能因为焦点停在按钮上就放弃推进。点 Back 之后浏览器把焦点留在
        // 那个按钮上，event.target 就是 <button>；早先这里直接 return，
        // 于是回到 NVL 后按空格只会补全隐藏的那层 Dialog，中央大字继续逐字打。
        // 只有非 game 屏幕里的按钮才真正需要空格激活（上面那条已经放行）。
        if ((tag === 'button' || tag === 'a') && node.closest && !node.closest('[data-screen="game"], game-screen')) return;
        event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
        window.NvlOverlay.requestAdvance();
    }}, true);

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
    cgOverlay.addEventListener("click", function(event) {{
        event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
        if (window.GameNavigation && window.GameNavigation._busy) return;
        if (window._cgAdvancing) return;
        window._cgAdvancing = true;
        Promise.resolve(monogatari.proceed({{userInitiated:true, skip:false, autoPlay:false}}))
            .catch(function(e) {{ console.warn('[CG] proceed blocked', e); }})
            .finally(function() {{ window._cgAdvancing = false; }});
    }});

    // ── Other 设置页装饰器：复用 Ancient 的 Quick Menu 与 Restore Defaults 功能。──
    // 只新增设置页 DOM 和设置控件自身的 click/input 处理，不接管 Save / Load、章节跳转或 ESC。
    if (window._uiMode !== 'Ancient') {{
        window.OtherUI = {{
            _lastSceneImage: '',

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
                const settings = document.querySelector('settings-screen, [data-component="settings-screen"]');
                if (!settings) return;
                const image = this.captureScene() || this._lastSceneImage;
                if (image) settings.style.setProperty('--other-settings-cover', image);
            }},

            setQuickMenuVisible: function(visible) {{
                visible = visible !== false;
                document.body.classList.toggle('other-quick-menu-hidden', !visible);
                try {{ localStorage.setItem('VisualNovel_OtherQuickMenuVisible', visible ? '1' : '0'); }} catch (e) {{}}
                if (!visible) {{
                    const panel = document.getElementById('stats-panel');
                    if (panel) panel.classList.remove('open');
                    window._statsDismissedByUser = true;
                }} else {{
                    window._statsDismissedByUser = false;
                    if (window.GameplayUiState) window.GameplayUiState.sync();
                }}
                this.syncSettingsControls();
            }},

            syncSettingsControls: function() {{
                const quickVisible = !document.body.classList.contains('other-quick-menu-hidden');
                document.querySelectorAll('[data-other-quick]').forEach((button) => {{
                    button.classList.toggle('is-active', button.dataset.otherQuick === (quickVisible ? 'show' : 'hide'));
                }});
            }},

            refreshSettingsText: function() {{
                const quick = document.querySelector('[data-settings="other-quick-menu"] > h3');
                if (quick) quick.textContent = window.__ui('quickMenu');
                const labels = {{ show: window.__ui('show'), hide: window.__ui('hide') }};
                document.querySelectorAll('[data-other-label]').forEach((button) => {{
                    button.textContent = labels[button.dataset.otherLabel] || button.dataset.otherLabel;
                }});
                const reset = document.querySelector('.other-settings-reset');
                if (reset) reset.textContent = window.__ui('restoreDefaults');
                this.syncSettingsControls();
            }},

            restoreSettingsDefaults: function() {{
                const dispatch = function(input) {{
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }};
                document.querySelectorAll('settings-screen [data-action="set-volume"], [data-component="settings-screen"] [data-action="set-volume"]').forEach((input) => {{
                    input.value = '0.8';
                    dispatch(input);
                }});
                const textSpeed = document.querySelector('settings-screen [data-action="set-text-speed"], [data-component="settings-screen"] [data-action="set-text-speed"]');
                if (textSpeed) {{
                    textSpeed.value = String(Math.round((Number(textSpeed.min || 1) + Number(textSpeed.max || 50)) / 2));
                    dispatch(textSpeed);
                }}
                if (window.AutoPlayInterval) window.AutoPlayInterval.set(1);
                this.setQuickMenuVisible(true);
                this.syncSettingsControls();
            }},

            decorateSettings: function() {{
                const settings = document.querySelector('settings-screen, [data-component="settings-screen"]');
                const row = settings && settings.querySelector(':scope > .row');
                if (!settings || !row) return;

                if (!row.querySelector('[data-settings="other-quick-menu"]')) {{
                    const item = document.createElement('div');
                    item.setAttribute('data-settings', 'other-quick-menu');
                    const title = document.createElement('h3');
                    const controls = document.createElement('div');
                    controls.className = 'other-settings-choice';
                    ['show', 'hide'].forEach((option) => {{
                        const button = document.createElement('button');
                        button.type = 'button';
                        button.dataset.otherLabel = option;
                        button.dataset.otherQuick = option;
                        controls.appendChild(button);
                    }});
                    item.append(title, controls);
                    item.addEventListener('click', (event) => {{
                        const button = event.target.closest('[data-other-quick]');
                        if (!button) return;
                        this.setQuickMenuVisible(button.dataset.otherQuick === 'show');
                    }});
                    row.appendChild(item);
                }}

                if (!settings.querySelector('.other-settings-reset')) {{
                    const reset = document.createElement('button');
                    reset.type = 'button';
                    reset.className = 'other-settings-reset';
                    reset.addEventListener('click', () => this.restoreSettingsDefaults());
                    settings.appendChild(reset);
                }}

                this.refreshSettingsText();
            }},

            init: function() {{
                let quickVisible = true;
                try {{ quickVisible = localStorage.getItem('VisualNovel_OtherQuickMenuVisible') !== '0'; }} catch (e) {{}}
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

        window.OtherUI.init();
    }}

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
                    window._statsDismissedByUser = true;
                    if (panel) panel.classList.remove('open');
                }} else if (wasHidden) {{
                    window._statsDismissedByUser = false;
                    if (window.GameplayUiState) window.GameplayUiState.sync();
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
                if (window.AutoPlayInterval) window.AutoPlayInterval.set(1);
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
                const lang = String(window._lang || document.documentElement.lang || '');
                const ja = lang === 'ja-JP';
                if (title) title.textContent = ja ? '終 幕' : 'The End';
                if (hint) hint.textContent = ja ? 'クリックまたは任意のキーで続行' : 'Press any key to continue';
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
    _syncTimer: null,
    _syncPromise: null,
    _syncResolve: null,
    _syncGeneration: 0,
    _engineTextObjectAtShow: null,
    _fullText: "",
    _typing: false,

    show: function(text) {{
        this._cancelEngineSync();
        try {{ this._engineTextObjectAtShow = monogatari.global('textObject'); }} catch (e) {{ this._engineTextObjectAtShow = null; }}
        this._fullText = (text == null) ? "" : String(text);
        // _typing 必须在任何 early return 之前就置位。show() 返回后引擎才接管
        // 后面那条 Dialog 语句，这中间的点击若看到 _typing=false 就会被当成
        // “推进到下一句”，独白一个字都没显示就被跳过。
        // 反过来，没有正文时绝不能置位：否则空白屏上的第一次点击会被当成“补全”白吃掉。
        this._typing = this._fullText !== "";
        const overlay = document.getElementById('nvl-overlay');
        if (!overlay) {{ this._typing = false; return; }}
        const tEl = overlay.querySelector('.nvl-text');

        // 先切到激活态（ADV 对话框由 CSS 隐藏），overlay 立即淡入
        document.body.classList.add('nvl-active');
        overlay.classList.add('open');

        // 打字机：清空后逐字追加，末尾挂闪烁光标
        if (this._typeTimer) {{ clearTimeout(this._typeTimer); this._typeTimer = null; }}
        if (!tEl) {{ this._typing = false; return; }}
        tEl.textContent = '';
        tEl.scrollTop = 0;
        const chars = [...this._fullText];
        let i = 0;
        const speed = Math.max(18, Math.min(45, 1600 / Math.max(1, chars.length)));
        // 不自动滚动：视口始终停在顶部，超出一屏的长独白由玩家用滚动条自己看。
        // 跟随滚动会把开头顶出视口，正文看起来一直在移动。
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
        // 立刻打第一个字，不留“已 typing 但屏幕全空”的窗口——那段时间里的
        // 点击会让用户以为没反应，或误触发全文瞬显。
        type();
        // 不要在这里预热 syncEngineTyping()。引擎在 <game-screen> 上挂了委托监听器
        // '[data-screen="game"] *:not([data-choice])' → proceed()，而 #nvl-overlay 就在
        // game-screen 内，所以一次点击会同时到达本模块和引擎。两阶段闸门之所以成立，
        // 正是靠 Dialog.shouldProceed 在 finished_typing 尚为 false 时 reject 一次，
        // 把引擎那条委托点击吃掉。提前中和 finished_typing 会撤掉这道拦阻，
        // 结果是第一次点击就直接跳句。
    }},

    _appendCursor: function(tEl) {{
        if (!tEl || tEl.querySelector('.nvl-cursor')) return;
        const cur = document.createElement('span');
        cur.className = 'nvl-cursor';
        tEl.appendChild(cur);
    }},

    // 点击时若还在打字：立即补全全文，视口回到顶部。
    // 不要在这里滚到底：补全是“整段一次呈现”，玩家要从第一行开始读。
    // 滚到底会把开头顶出视口，看起来像文字仍在移动、没有一次出全。
    // 超出一屏的长独白靠滚动条自己看。
    finishTyping: function() {{
        if (!this._typing) return;
        if (this._typeTimer) {{ clearTimeout(this._typeTimer); this._typeTimer = null; }}
        this._typing = false;
        const overlay = document.getElementById('nvl-overlay');
        const tEl = overlay && overlay.querySelector('.nvl-text');
        if (tEl) {{ tEl.textContent = this._fullText; this._appendCursor(tEl); tEl.scrollTop = 0; }}
    }},

    // 推进入口。只有两种结果，每次点击都必须有可见反馈：
    //   还在打字 → 补全全文（不推进）
    //   已经打完 → 推进到下一句
    // 早先这里还夹了一个“展示确认”空阶段：文字自然打完后，第一次点击什么都不做，
    // 只把内部标志复位，屏幕上毫无变化，用户会以为点击失灵。已移除。
    requestAdvance: function() {{
        if (this.isTyping()) {{
            this.finishTyping();
            this.syncEngineTyping();
            return;
        }}
        if (window.GameNavigation && window.GameNavigation._busy) return;
        if (window._nvlAdvancing) return;
        window._nvlAdvancing = true;
        // NVL 走自己的推进通道，通用点击处理已把 #nvl-overlay 排除，所以这里必须
        // 自己重置 autoplay 时钟：否则上一句排好的定时器会叠在这次推进上，一次点击跳两句。
        if (window.SmartAutoPlay && window.SmartAutoPlay.restartAfterUserAdvance) {{
            window.SmartAutoPlay.restartAfterUserAdvance();
        }}
        // show() 已提前发起同步，这里通常直接命中快速路径；保留调用是为了覆盖
        // show() 之后引擎又新建隐藏 Dialog 的情况。
        var sync = this.syncEngineTyping();
        var self = this;
        var proceedOnce = function(retried) {{
            return Promise.resolve(monogatari.proceed({{userInitiated:true, skip:false, autoPlay:false}}))
                .catch(function(e) {{
                    var reason = String(e || '');
                    // Dialog.shouldProceed rejects once when it finishes a
                    // TypeWriter.  Synchronize again and retry only that one
                    // transient case; all other errors remain visible in the
                    // console and are not looped indefinitely.
                    if (!retried && /TypeWriter effect has not finished|TypeWriter/i.test(reason)) {{
                        return Promise.resolve(self.syncEngineTyping()).then(function() {{ return proceedOnce(true); }});
                    }}
                    throw e;
                }});
        }};
        Promise.resolve(sync)
            .then(function(ok) {{
                if (ok === false) return false;
                if (window.GameNavigation && window.GameNavigation._busy) return false;
                return proceedOnce(false);
            }})
            .catch(function(e) {{ console.warn('[NVL] proceed blocked', e); }})
            .finally(function() {{ window._nvlAdvancing = false; }});
    }},

    _cancelEngineSync: function() {{
        this._syncGeneration += 1;
        if (this._syncTimer) {{ clearTimeout(this._syncTimer); this._syncTimer = null; }}
        if (this._syncResolve) {{ try {{ this._syncResolve(false); }} catch (e) {{}} }}
        this._syncResolve = null;
        this._syncPromise = null;
    }},

    // 完成与自定义 NVL 同步运行的 Monogatari 隐藏 Dialog 打字机。
    // 如果不处理这里，NVL 画面已经显示完整时，底层 Dialog 仍会把下一次
    // 点击当成“完成打字”，导致用户需要额外点击一次且看起来像剧情卡住。
    syncEngineTyping: function() {{
        const self = this;
        if (typeof monogatari === 'undefined' || !monogatari || !monogatari.global) return Promise.resolve(false);
        if (this._syncPromise) return this._syncPromise;
        const generation = this._syncGeneration;
        const startedAt = Date.now();
        // Dialog is normally installed within the same event turn.  Keep a
        // short safety window for a busy browser, but never make a user click
        // feel like it is waiting seconds before the next beat.
        const maxWait = 900;
        this._syncPromise = new Promise(function(resolve) {{
            self._syncResolve = resolve;
            const finish = function(value) {{
                if (generation === self._syncGeneration) {{
                    if (self._syncTimer) {{ clearTimeout(self._syncTimer); self._syncTimer = null; }}
                    self._syncResolve = null;
                    self._syncPromise = null;
                }}
                resolve(!!value);
            }};
            const attempt = function() {{
                if (generation !== self._syncGeneration) {{ finish(false); return; }}
                try {{
                    const textObject = monogatari.global('textObject');
                    const finished = !!monogatari.global('finished_typing');
                    const freshObject = !!textObject && textObject !== self._engineTextObjectAtShow;
                    let typeAnimationEnabled = true;
                    try {{ typeAnimationEnabled = monogatari.setting('TypeAnimation') !== false; }} catch (e) {{}}
                    // A finished flag from the previous beat is not enough: the
                    // new hidden Dialog may not have been created yet.  Accept a
                    // completed state immediately only when the current Dialog
                    // object is fresh, or when type animation is disabled.
                    if (finished && (freshObject || (!typeAnimationEnabled && !textObject))) {{ finish(true); return; }}
                    // The custom NVL overlay may receive a click before Dialog
                    // has installed its hidden TypeWriter.  Poll briefly instead
                    // of marking finished_typing early and letting Dialog reset
                    // it to false a moment later.
                    if (!textObject || !freshObject) {{
                        if (Date.now() - startedAt >= maxWait) {{
                            monogatari.global('finished_typing', true);
                            finish(true);
                            return;
                        }}
                        self._syncTimer = setTimeout(attempt, 16);
                        return;
                    }}
                    const strings = textObject.strings || [];
                    const raw = strings.length ? String(strings[0] || '') : '';
                    const element = textObject.el;
                    if (typeof textObject.destroy === 'function') textObject.destroy();
                    if (element) {{
                        const html = raw.replace(/\\{{pause:(\\d+)\\}}/g, '').replace(/\\{{speed:(\\d+)\\}}/g, '');
                        if (typeof element.innerHTML !== 'undefined') element.innerHTML = html;
                    }}
                    monogatari.global('finished_typing', true);
                    if (typeof monogatari.trigger === 'function') monogatari.trigger('didFinishTyping');
                    console.info('[NVL] hidden Dialog typing synchronized');
                    finish(true);
                }} catch (e) {{
                    if (Date.now() - startedAt >= maxWait) {{
                        console.warn('[NVL] hidden Dialog sync failed', e);
                        finish(false);
                    }} else {{
                        self._syncTimer = setTimeout(attempt, 16);
                    }}
                }}
            }};
            // Defer the first probe one task so _syncPromise is assigned before
            // a fast-path completion can clear it.
            self._syncTimer = setTimeout(attempt, 0);
        }});
        return this._syncPromise;
    }},

    isTyping: function() {{ return !!this._typing; }},

    hide: function() {{
        const overlay = document.getElementById('nvl-overlay');
        this._cancelEngineSync();
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
            localStorage.setItem(window._scopedStorageKey('PlayerVarsCurrent'), JSON.stringify(window._playerVariables || {{}}));
            localStorage.setItem(window._scopedStorageKey('PlayerVariableChoicesCurrent'), JSON.stringify(window._playerVariableChoices || {{}}));
            localStorage.setItem(window._scopedStorageKey('PlayerLegacyFlagsCurrent'), JSON.stringify(window._playerLegacyFlags || {{}}));
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
        const message = window.__ui ? window.__ui('trialLocked') : 'The demo includes Chapters 1 and 2 only.';
        if (window.showToast) window.showToast(message);
        else alert(message);
        return false;
    }}
    return true;
}};

// ==================== 路由导航器 ====================
window.StoryNavigator = {{
    pendingJumpUrl: null,  // 保存待跳转的 URL
    pendingRequestId: null,

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
        // 每次申请都有独立 request_id，延迟到达的旧许可不能串到新章节。
        this.pendingJumpUrl = targetUrl;
        this.pendingRequestId = String(Date.now()) + '_' + Math.random().toString(36).slice(2);

        // 发送跳转申请
        const requestMsg = {{
            "type": "CHAPTER_REQUEST",
            "data": {{
                "target_node_id": targetNodeId || '',
                "target_url": targetUrl,
                "request_id": this.pendingRequestId,
                "timestamp": Date.now()
            }}
        }};
        console.log("📤 [Iframe] 申请跳转:", requestMsg);

        if (window.parent && window.parent !== window) {{
            // Iframe 模式:发送消息并等待回信
            window._postToParent(requestMsg);
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
            const permit = event.data.data;
            if (!window._messageMatchesContext(event, permit || {{}})) return;
            const expectedRequestId = String(window.StoryNavigator.pendingRequestId || '');
            const permitRequestId = permit && permit.request_id != null ? String(permit.request_id) : '';
            if (expectedRequestId && permitRequestId && expectedRequestId !== permitRequestId) return;
            console.log("📥 [Iframe] 收到跳转许可");

            // 检查是否允许跳转
            if (permit && permit.allowed === true) {{
                // 获取之前保存的目标 URL
                const targetUrl = window.StoryNavigator.pendingJumpUrl;

                if (targetUrl) {{
                    window.StoryNavigator.pendingJumpUrl = null;
                    window.StoryNavigator.pendingRequestId = null;
                    window.showToast(window.__ui ? window.__ui('loading') : 'Loading...');
                    const jumpUrl = targetUrl.includes('?')
                        ? targetUrl + '&_t=' + Date.now()
                        : targetUrl + '?_t=' + Date.now();

                    setTimeout(() => {{ window.location.href = jumpUrl; }}, 300);
                }}
            }} else {{
                console.log("❌ 跳转被拒绝");
                window.showToast(window.__ui ? window.__ui('accessDenied') : 'You do not have access to this chapter.');
            }}
        }} catch (e) {{
            console.error("❌ 跳转失败:", e);
        }}
    }}
}});

// ==================== 进度条跳转 ====================
window.addEventListener('message', function(event) {{
    if (!event.data || event.data.type !== 'SEEK_TO_BEAT') return;
    if (!window._messageMatchesContext(event, event.data.data || {{}})) return;
    const beatIndex = event.data.data && event.data.data.beat_index;
    if (beatIndex == null || !window._materialIndices) return;
    if (!Number.isFinite(Number(beatIndex))) return;
    if (window._seekInFlight) return;
    const indices = window._materialIndices.map(Number).filter(Number.isFinite).sort((a,b) => a-b);
    if (!indices.length) return;
    // Keep floor lookup for sparse in-range material indices.  Requests past
    // the final beat must go through EndBridge instead of clamping to the last
    // Material label forever (the observed 207/208/210 -> Material_206 loop).
    const requested = Number(beatIndex);
    let target = indices[0];
    for (let i = 0; i < indices.length; i++) {{
        if (indices[i] <= requested) target = indices[i];
        else break;
    }}
    let targetLabel;
    if (requested > indices[indices.length - 1]) {{
        target = indices[indices.length - 1];
        targetLabel = 'EndBridge';
    }} else {{
        targetLabel = (window._choiceLabelsByIndex && window._choiceLabelsByIndex[String(target)]) || `Material_${{target}}`;
    }}
    console.info('[SEEK_TO_BEAT] request=', Number(beatIndex), 'resolved=', target, 'label=', targetLabel);
    const doJump = () => window.GameNavigation ? window.GameNavigation.jump(targetLabel) : Promise.resolve(monogatari.run(`jump ${{targetLabel}}`));
    window._seekInFlight = (async function() {{
        try {{
            if (!monogatari.global('playing')) {{
                monogatari.global('playing', true);
                await Promise.resolve(monogatari.onStart());
                monogatari.element().find('[data-screen]').each((screen) => {{ screen.setState({{ open: false }}); }});
                const gameScreen = monogatari.element().find('[data-screen="game"]').get(0);
                if (gameScreen) gameScreen.setState({{ open: true }});
            }}
            await doJump();
        }} catch (e) {{
            console.error('[SEEK_TO_BEAT] jump failed', e);
            try {{ if (window.GameNavigation) window.GameNavigation.resetLocks(); }} catch (_) {{}}
        }} finally {{
            window._seekInFlight = null;
        }}
    }})();
}});

// ==================== 保存/加载管理器 (修复版:直接读取LocalStorage) ====================
window.SaveLoadManager = {{
    // 发送所有存档信息到 parent
    sendSavesToParent: function() {{
        try {{
            const saveData = [];
            let storagePrefix = '';
            try {{
                const cfg = monogatari && monogatari.Storage && monogatari.Storage.configuration
                    ? monogatari.Storage.configuration() : null;
                if (cfg && cfg.name && cfg.store && cfg.version) storagePrefix = String(cfg.name) + '::' + String(cfg.store) + '::' + String(cfg.version) + '_';
            }} catch (e) {{}}

            // 方法1: 直接从 LocalStorage 读取所有存档
            for (let i = 0; i < localStorage.length; i++) {{
                const key = localStorage.key(i);
                // 匹配 Monogatari 存档键名格式
                if (key && (key.includes('Save') || key.includes('save') ||
                           key.includes('Slot') || key.includes('slot'))) {{
                    if (storagePrefix && key.indexOf(storagePrefix) !== 0) continue;
                    try {{
                        const value = localStorage.getItem(key);
                        const saveInfo = JSON.parse(value);

                        console.log('📦 [SaveLoadManager] 找到存档:', key, saveInfo);

                        const slotKey = storagePrefix && key.indexOf(storagePrefix) === 0
                            ? key.slice(storagePrefix.length) : key;
                        saveData.push({{
                            slot: slotKey,
                            storage_key: key,
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
                window._postToParent(saveMessage);
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
                    if (!window._messageMatchesContext(event, event.data.data || {{}})) return;
                    console.log("📥 [SaveLoadManager] 收到云端存档数据:", event.data);
                    const saveData = event.data.data;

                    // 如果数据为空或没有存档,跳过加载
                    if (!saveData || !saveData.saves || saveData.saves.length === 0) {{
                        console.log("⚠️ 没有存档数据");
                        return;
                    }}

                    // 等待 Monogatari 初始化完成
                    const loadSaves = (attempt) => {{
                        attempt = Number(attempt || 0);
                        if (!monogatari || !monogatari.storage) {{
                            if (attempt >= 50) {{
                                console.error("❌ Monogatari 初始化超时，放弃本次云端存档加载");
                                if (window.showToast) window.showToast(window.__ui ? window.__ui('loadFailed') : 'Unable to load save data.');
                                return;
                            }}
                            setTimeout(() => loadSaves(attempt + 1), 100);
                            return;
                        }}

                        const storage = monogatari.storage();
                        console.log("📦 当前 storage 状态:", storage);

                        if (!storage.saves) storage.saves = {{}};

                        console.log("📦 开始加载存档，共 " + saveData.saves.length + " 个");

                        // [关键修复]同时写入 localStorage 和 storage 对象
                        for (const save of saveData.saves) {{
                            if (save.slot && save.gameState) {{
                                let localSaveKey = String(save.storage_key || save.slot);
                                try {{
                                    const cfg = monogatari && monogatari.Storage && monogatari.Storage.configuration
                                        ? monogatari.Storage.configuration() : null;
                                    const prefix = cfg && cfg.name && cfg.store && cfg.version
                                        ? String(cfg.name) + '::' + String(cfg.store) + '::' + String(cfg.version) + '_' : '';
                                    if (!save.storage_key && prefix && localSaveKey.indexOf(prefix) !== 0) localSaveKey = prefix + localSaveKey;
                                }} catch (e) {{}}
                                console.log("  - 加载存档槽位:", save.slot);
                                console.log("  - 存档数据:", save.gameState);

                                //  1. 写入 localStorage(Monogatari 从这里读取)
                                try {{
                                    localStorage.setItem(localSaveKey, JSON.stringify(save.gameState));
                                    console.log("  ✅ 存档已写入 localStorage:", localSaveKey);
                                }} catch (e) {{
                                    console.error("  ❌ 写入 localStorage 失败:", e);
                                }}

                                //  2. 同时更新 storage 对象(Load 界面从这里读取)
                                storage.saves[localSaveKey] = save.gameState;
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

                        window.showToast(window.__ui ? window.__ui('saveSynced') : 'Save data synced.');
                    }};

                    loadSaves(0);
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
    requestInitialProgress: function(source) {{
        if (window.parent && window.parent !== window) {{
            const requestMsg = {{
                type: "REQUEST_INITIAL_PROGRESS",
                data: {{
                    chapter_id: "{chap_id}",
                    node_id: "{node_id}",
                    timestamp: Date.now(),
                    source: String(source || 'initial')
                }}
            }};
            window._postToParent(requestMsg);
            console.log("📤 请求初始进度:", requestMsg);
        }}
    }},

    // 监听初始进度返回
    listenForInitialProgress: function() {{
        window.addEventListener('message', function(event) {{
            if (event.data && event.data.type === "INITIAL_PROGRESS") {{
                try {{
                    if (!window._messageMatchesContext(event, event.data.data || {{}})) return;
                    console.log("📥 收到初始进度:", event.data);
                    let progressData = event.data.data;
                    const incomingIndex = Number(progressData && progressData.current_index || 0);
                    const localIndex = Math.max(
                        Number(window._progressData && window._progressData.current_index || 0),
                        Number(window.GameProgressReporter && window.GameProgressReporter.current || 0),
                        Number(window._currentMaterialIndex || 0)
                    );
                    // A delayed response to the Quit-time request may contain
                    // an older server snapshot.  Never let it overwrite the
                    // beat we just flushed locally.
                    if (window.GameProgressReporter && window.GameProgressReporter._exitInProgress && incomingIndex < localIndex) {{
                        progressData = Object.assign({{}}, progressData || {{}}, window._progressData || {{}}, {{ current_index: localIndex }});
                    }} else if (progressData && typeof progressData === 'object') {{
                        window._progressData = progressData;
                    }}

                    // 如果有进度数据,更新游戏进度报告器
                    if (progressData && progressData.current_index != null && window.GameProgressReporter && window.GameProgressReporter.total > 0) {{
                        window.GameProgressReporter.update(progressData.current_index);
                        console.log("✅ 进度已恢复:", progressData);
                    }}
                    if (typeof window.updateButtonVisibility === 'function') window.updateButtonVisibility();
                    if (progressData && progressData.player_variables &&
                            Object.keys(progressData.player_variables).length > 0) {{
                        window._playerVariables = window._sanitizePlayerVariables(progressData.player_variables);
                        if (window._syncPlayerVars) window._syncPlayerVars();
                        if (window.updateStatsUI) window.updateStatsUI();
                    }}
                    if (progressData && progressData.player_variable_choices) {{
                        window._playerVariableChoices = window._sanitizeChoiceStats(progressData.player_variable_choices);
                        if (window._syncPlayerVars) window._syncPlayerVars();
                    }}
                    if (progressData && progressData.player_legacy_flags) {{
                        window._playerLegacyFlags = window._sanitizeLegacyFlags(progressData.player_legacy_flags);
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
    requestSaveData: function(source) {{
        if (window.parent && window.parent !== window) {{
            const requestMsg = {{
                type: "REQUEST_SAVE_DATA",
                data: {{
                    chapter_id: "{chap_id}",
                    node_id: "{node_id}",
                    timestamp: Date.now(),
                    source: String(source || 'initial')
                }}
            }};
            window._postToParent(requestMsg);
            console.log("📤 请求存档数据:", requestMsg);
        }}
    }},

    init: function() {{
        this.listenForInitialProgress();
        // 调用方在 Monogatari + SaveLoadManager 初始化完成后发起请求，
        // 避免响应先于监听器/按钮建立而丢失。
        this.requestInitialProgress('initial');
        // 延迟500ms后请求存档(确保进度请求先处理)
        setTimeout(() => {{
            this.requestSaveData('initial');
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
    _disableReport: false,
    _exitInProgress: false,
    _lastReportedIndex: 0,
    _lifecycleBound: false,
    _exitSent: false,

    init: function(total, chapId, nodeId) {{
        this.total = total;
        this.current = 0;
        this.chapId = chapId;
        this.nodeId = nodeId;
        this._disableReport = false; // 初始化进度上报开关
        this._exitInProgress = false;
        this._lastReportedIndex = 0;
        this._exitSent = false;

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
        const numericIndex = Number(index); const safeIndex = Math.max(0, Math.min(this.total, Number.isFinite(numericIndex) ? numericIndex : (Number(this.current) || 0)));
        let effectiveIndex = safeIndex;
        // Keep the menu's local snapshot in sync with the reporter.  Quit can
        // return to the main screen before the parent sends INITIAL_PROGRESS;
        // updateButtonVisibility must still see the latest beat.
        try {{
            const previous = (window._progressData && typeof window._progressData === 'object')
                ? window._progressData : {{}};
            const localIndex = Number(previous.current_index || 0);
            const nextIndex = this._exitInProgress
                ? Math.max(Number.isFinite(localIndex) ? localIndex : 0, safeIndex)
                : safeIndex;
            effectiveIndex = nextIndex;
            this.current = effectiveIndex;
            if (!Number.isFinite(localIndex) || nextIndex !== localIndex || !this._exitInProgress) {{
                window._progressData = Object.assign({{}}, previous, {{
                    chapter_id: this.chapId,
                    node_id: this.nodeId,
                    current_index: nextIndex,
                    total_count: this.total,
                    progress_percent: this.total ? Math.min(100, parseFloat(((nextIndex / this.total) * 100).toFixed(2))) : 0,
                    player_variables: window._playerVariables || previous.player_variables || {{}},
                    player_variable_choices: window._playerVariableChoices || previous.player_variable_choices || {{}},
                    player_legacy_flags: window._playerLegacyFlags || previous.player_legacy_flags || {{}},
                    branch_state: (window.BranchProgress && window.BranchProgress.get()) || previous.branch_state || null,
                    system_text: (window.SystemPanel && window.SystemPanel.getText()) || previous.system_text || ''
                }});
            }}
        }} catch (e) {{ console.warn('[GameProgressReporter] local snapshot sync failed:', e); }}
        if (!Number.isFinite(effectiveIndex)) effectiveIndex = safeIndex;
        this.current = effectiveIndex;
        const pct = (effectiveIndex / this.total) * 100;
        const fillEl = document.getElementById('game-progress-fill');
        if (fillEl) {{
            fillEl.style.width = pct + '%';
        }}
    }},

    sendReport: function(triggerSource, force) {{
        // [还原]移除了熔断机制,允许玩家正常快进
        if (this.total === 0) {{ return; }}
        if (this._disableReport && !force) {{ return false; }}
        if (this._exitInProgress && !force) {{ return false; }}

        // [修复]确保current不超过total,防止发送错误数据给父端
        const numericCurrent = Number(this.current); const safeCurrent = Math.max(0, Math.min(this.total, Number.isFinite(numericCurrent) ? numericCurrent : 0));
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

        if (!(window.parent && window.parent !== window)) return false;
        window._postToParent(msg);
        this._lastReportedIndex = Math.max(Number(this._lastReportedIndex || 0), safeCurrent);
        return true;
    }},

    // Synchronously publish the currently visible beat before Quit/pagehide.
    // This is intentionally independent of the 30s timer and of the engine's
    // later End/reset actions, which may otherwise report a lower index.
    flushForExit: function(source) {{
        if (this._exitSent) return false;
        this._exitSent = true;
        this._exitInProgress = true;
        this._disableReport = true;
        try {{
            const visible = Number(window._currentMaterialIndex);
            if (Number.isFinite(visible)) this.update(visible);
        }} catch (e) {{}}
        return this.sendReport(source || 'user_exit', true);
    }},

    startAutoSave: function() {{
        // 每30s自动上报进度
        if (this.autoSaveTimer) {{
            clearInterval(this.autoSaveTimer);
        }}
        this.autoSaveTimer = setInterval(() => {{
            this.sendReport('auto_save');
        }}, 30000);

        // 监听页面关闭/刷新；只绑定一次，避免 Restart/Continue 累积监听器。
        if (!this._lifecycleBound) {{
            this._lifecycleBound = true;
            const flush = () => {{
                if (this._exitSent) return;
                this.flushForExit('user_exit');
            }};
            window.addEventListener('beforeunload', flush);
            window.addEventListener('pagehide', flush);
            document.addEventListener('visibilitychange', () => {{
                // Hiding a tab/app is not an exit.  Keep reporting enabled so
                // returning to the game cannot leave the host on a stale beat.
                if (document.visibilityState === 'hidden' && !this._exitSent) this.sendReport('visibility_hidden');
            }});
        }}

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
        closeBtn.title = window.__ui ? window.__ui('close') : 'Close';
        closeBtn.setAttribute('aria-label', closeBtn.title);
        closeBtn.setAttribute('role', 'button');
        closeBtn.setAttribute('tabindex', '0');
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
    startClicker: function(target=8, time=3, label=null) {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // [记录]保存句柄
            label = label || (window.__ui ? window.__ui('burst') : 'Burst!');
            const title = window.__ui ? window.__ui('rapidClick') : 'Rapid Click Challenge';

            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">${{title}}</div>
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
    startTiming: function(speed=2, difficulty=20, label=null) {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // [记录]保存句柄
            label = label || (window.__ui ? window.__ui('perfectStrike') : 'Timing Challenge');
            const lockText = window.__ui ? window.__ui('lock') : 'Lock';

            const zoneLeft = 20 + Math.random() * 50;
            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">${{label}}</div>
                    <div class="timing-bar-container">
                        <div class="timing-zone" style="left:${{zoneLeft}}%; width:${{difficulty}}%"></div>
                        <div class="timing-cursor" id="t-cursor"></div>
                    </div>
                    <div class="qte-btn" id="t-btn" style="width:80px;height:80px;font-size:1rem">${{lockText}}</div>
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
                btn.innerText = success
                    ? (window.__ui ? window.__ui('success') : 'Success')
                    : (window.__ui ? window.__ui('failed') : 'Failed');
                setTimeout(() => this.closeOverlay(overlay, success, resolve), 800);
            }};
        }});
    }},

    //  3. 记忆阵法
    startMemory: function(count=4, label=null) {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // [记录]保存句柄
            label = label || (window.__ui ? window.__ui('memoryChallenge') : 'Memory Challenge');
            const watchSequenceText = window.__ui ? window.__ui('watchSequence') : 'Watch the sequence...';
            const clickInOrderText = window.__ui ? window.__ui('clickInOrder') : 'Click in order';

            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">${{label}}</div>
                    <div class="rune-grid" id="rune-grid">
                        ${{Array(9).fill(0).map((_,i)=>`<div class="rune-cell" data-i="${{i}}"></div>`).join('')}}
                    </div>
                    <div id="mem-msg" style="margin-top:20px">${{watchSequenceText}}</div>
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
                if (overlay.querySelector('#mem-msg')) overlay.querySelector('#mem-msg').innerText = clickInOrderText;
            }};
            setTimeout(playSeq, 500);

            cells.forEach(c => c.onclick = () => {{
                if(overlay.querySelector('#mem-msg').innerText !== clickInOrderText) return;
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
const StageManager = window.StageManager = {
    _screenObserver: null,
    // key -> { container, img, stem, slot, leftPct }
    _cast: {},
    _speakerKey: '',

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
        this._speakerKey = '';
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
        container.setAttribute('data-character-key', entry.key);
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
        img.setAttribute('data-stage-loading', 'true');
        img.style.transition = 'opacity 0.9s ease, filter 0.25s ease, transform 0.25s ease';   // 淡入稍长；高亮切换平滑
        // 图片加载完成后再淡入，避免坏图/未加载时的空白闪烁
        img.onload = () => { img.removeAttribute('data-stage-loading'); img.style.opacity = '1'; };
        img.onerror = () => { console.warn('[StageManager] 立绘加载失败:', img.src); };
        img.src = `assets/characters/${entry.stem}.png`;
        // 兜底：若 onload 因缓存未触发，下一帧也尝试淡入
        requestAnimationFrame(() => { if (img.complete && img.naturalWidth > 0) { img.removeAttribute('data-stage-loading'); img.style.opacity = '1'; } });
        container.appendChild(img);

        layer.appendChild(container);

        const rec = { container, img, stem: entry.stem, slot: entry.slot, leftPct: entry.leftPct, key: entry.key };
        this._cast[entry.key] = rec;
        this._syncSpeakingState();
        return rec;
    },

    _removeKey(key) {
        const rec = this._cast[key];
        if (!rec) return;
        delete this._cast[key];
        if (rec.container) rec.container.remove();
    },

    _syncSpeakingState() {
        const entries = Object.values(this._cast || {});
        const multi = entries.length >= 2 && !!this._speakerKey;
        const speaker = String(this._speakerKey || '').toLowerCase();
        // 只有 speaker_id 确实对应当前站位中的角色时才启用说话人提示。
        // 数据不匹配（例如旁白、旧存档或角色 ID 变更）时保持所有立绘
        // 原始亮度与清晰度，不误加高亮或降亮度。
        const speakerMatched = multi && entries.some(rec =>
            String(rec && (rec.key || rec.container?.getAttribute('data-character-key')) || '').toLowerCase() === speaker
        );
        entries.forEach(rec => {
            if (!rec || !rec.container) return;
            const key = String(rec.key || rec.container.getAttribute('data-character-key') || '').toLowerCase();
            const speaking = speakerMatched && key === speaker;
            rec.container.classList.toggle('is-speaking', speaking);
            rec.container.classList.toggle('is-not-speaking', speakerMatched && !speaking);
        });
    },

    // speakerId 为空表示旁白/无当前说话人。提示只在至少两名角色在场且
    // speakerId 能匹配当前 cast 时启用；不匹配时所有角色保持原始状态。
    setSpeaker(speakerId) {
        this._speakerKey = String(speakerId || '').trim();
        this._syncSpeakingState();
    },

    // list: [{ stem, slot, leftPct, key }]
    setCast(list, speakerId) {
        this._observeScreen();
        list = Array.isArray(list) ? list : [];
        if (arguments.length > 1) this._speakerKey = String(speakerId || '').trim();
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
                    img.style.transition = 'opacity 0.9s ease, filter 0.25s ease, transform 0.25s ease';   // 淡入稍长；高亮切换平滑
                    img.style.opacity = '0';
                    img.setAttribute('data-stage-loading', 'true');
                    img.onload = () => { img.removeAttribute('data-stage-loading'); img.style.opacity = '1'; };
                    img.src = `assets/characters/${e.stem}.png`;
                    requestAnimationFrame(() => { if (img.complete && img.naturalWidth > 0) { img.removeAttribute('data-stage-loading'); img.style.opacity = '1'; } });
                }
            }
        });
        this._syncSpeakingState();
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
    _textReady: false,
    _typing: false,

    _ensureDom: function() {
        if (!document.getElementById('hook-overlay')) {
            const overlay = document.createElement('div');
            overlay.id = 'hook-overlay';
            const content = document.createElement('div');
            content.id = 'hook-text-content';
            const hint = document.createElement('div');
            hint.id = 'hook-hint';
            hint.textContent = window.__ui ? window.__ui('clickContinue') : 'click to continue';
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
        this._textReady = false;
        this._typing = false;
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
        this._textReady = true;
        this._typing = false;
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
        this._textReady = true;
        this._typing = true;
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
                this._typing = false;
            }
        };
        setTimeout(type, 80);
    },
    isTyping: function() { return !!this._typing; },

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
                this._textReady = false;
                this._typing = false;
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

        # Keep an explicit result on the converter as well as returning it.
        # Callers must be able to distinguish a successful build from a run
        # that merely copied story_tree.json after every node was skipped.
        created_node_ids = []
        skipped_nodes = []

        try:
            # 0. 加载story_tree.json
            # 使用脚本文件所在位置计算路径
            script_dir = Path(__file__).resolve().parent
            project_root = script_dir.parent.parent  # 从 services/game_builder 向上两级到项目根目录
            story_tree_path = self.source_dir / "story_tree.json"
            if not story_tree_path.exists():
                raise FileNotFoundError(
                    f"找不到story_tree.json文件: {story_tree_path}"
                )

            print(f"加载story_tree.json: {story_tree_path}")
            with open(story_tree_path, 'r', encoding='utf-8') as f:
                raw_story_tree = json.load(f)
            story_tree, story_tree_by_lang = normalize_story_tree(raw_story_tree)
            self.story_tree_by_lang = story_tree_by_lang
            self.story_tree_default_language = (
                'en-US' if 'en-US' in story_tree_by_lang
                else DEFAULT_AUDIO_LANGUAGE
            )
            self.story_ui_by_lang = extract_story_ui_text(story_tree_by_lang)

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
            if story_tree_by_lang:
                print(f"  story_tree languages: {list(story_tree_by_lang)} (default {self.story_tree_default_language})")

            player_variables = story_tree.get('player_variables', {})
            has_player_variables = isinstance(player_variables, dict) and bool(player_variables)
            self._has_player_variables = has_player_variables
            print(f"  player_variables UI: {has_player_variables}")

            if not nodes:
                raise ValueError("story_tree.json中没有节点数据")

            print(f"找到 {len(nodes)} 个节点，根节点ID: {root_id}")

            # 全局扫描主 JSON：是否存在任何 system_text（决定是否注入 system 按钮 UI）
            has_system_text_anywhere = False
            master_json_path = None
            master_data = None
            try:
                master_json_path, master_data = self._find_master_json()
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
                folder_name = self._chapter_directory_name(node_id, depth)
                folder_mapping[node_id] = folder_name
                print(f"  映射: {node_id} -> {folder_name}")

            # 素材目录只扫描一次；后续节点按名称从索引中直接取。
            source_directory_index = self._index_source_directories(self.source_dir)

            # 2. 加载角色信息（共享）
            self.load_character_info()

            # 3. 遍历所有节点，为每个节点创建项目
            for node_id, node_data in nodes.items():
                folder_name = folder_mapping[node_id]
                depth = node_data.get('depth', 1)  # 获取章节编号
                print(f"\n处理节点: {node_id} -> 输出文件夹: {folder_name}")

                # 同时兼容 outputs/{node_id} 与 outputs/chapter_{depth}_{suffix}。
                source_dirs = self._resolve_node_source_directories(
                    source_directory_index,
                    node_id,
                    depth,
                )
                source_dir = source_dirs[0] if source_dirs else None
                if source_dir is None:
                    expected_names = (
                        node_id,
                        self._chapter_directory_name(node_id, depth),
                    )
                    print(f"  警告：源目录不存在，跳过: {' / '.join(expected_names)}")
                    skipped_nodes.append((str(node_id), "source directory missing"))
                    continue
                print(f"  素材目录: {source_dir.name}")
                if len(source_dirs) > 1:
                    print(f"  合并素材目录: {', '.join(directory.name for directory in source_dirs[1:])}")

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
                    ui_mode=ui_mode,
                    skip_asset_copy=self.skip_asset_copy,
                )

                # 复制当前实例的角色信息
                node_converter.pinyin_to_chinese = self.pinyin_to_chinese.copy()
                node_converter.chinese_to_id = self.chinese_to_id.copy()
                node_converter.chinese_to_pinyin = self.chinese_to_pinyin.copy()
                node_converter._source_node_id = node_id
                node_converter.asset_source_dirs = list(source_dirs)
                node_converter.story_tree_by_lang = story_tree_by_lang
                node_converter.story_tree_default_language = self.story_tree_default_language
                node_converter.story_ui_by_lang = self.story_ui_by_lang
                node_converter.story_choice_text_by_lang = {
                    lang: {
                        f"{node_id}::{child_id}": text
                        for child_id, text in child_map.items()
                    }
                    for lang, child_map in extract_story_choice_text(story_tree_by_lang, node_id).items()
                }
                node_converter._master_json_cache = (master_json_path, master_data)
                node_converter._has_player_variables = has_player_variables
                node_converter._player_variables_schema = {
                    str(key): value for key, value in (player_variables or {}).items()
                    if isinstance(key, str) and isinstance(value, (int, float))
                }

                # 【关键】复制特效配置（从父实例继承，因为atmophere.json在根目录）
                node_converter.atmosphere_config = self.atmosphere_config

                # 下发：主 JSON 是否含 system_text（决定是否注入 system 按钮 UI）
                node_converter._has_system_text = self._has_system_text

                # 扫描该节点的素材
                node_converter.scan_assets()
                # 语言层是 story_tree 的事实来源；即使某种语言暂时没有音频，
                # 也要保留它以便章节名、选项和 UI 能按 ?lang= 正确选择。
                for tree_lang in story_tree_by_lang:
                    if tree_lang not in ONLINE_GAME_LANGUAGES:
                        continue
                    if tree_lang not in node_converter.languages:
                        node_converter.languages.append(tree_lang)
                        node_converter.i18n_text.setdefault(tree_lang, {})
                        node_converter.voices_by_lang.setdefault(tree_lang, {})
                # 未传 ?lang 时在线版必须默认英语；不要因为只有日语语言层
                # 或旧 book_language 字段而把运行期默认值切到其他语言。
                node_converter.default_language = DEFAULT_AUDIO_LANGUAGE

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
                    def _clean_title(value):
                        cleaned = re.sub(
                            r'\s+(?:chapter_\d+|node_[0-9a-f]+)\s*$',
                            '',
                            str(value or '').strip(),
                            flags=re.IGNORECASE
                        ).strip()
                        return cleaned or f"Chapter {depth}"

                    chapter_titles_by_lang = {}
                    for lang, localized_tree in story_tree_by_lang.items():
                        localized_nodes = localized_tree.get('nodes', {}) if isinstance(localized_tree, dict) else {}
                        localized_node = localized_nodes.get(node_id, {}) if isinstance(localized_nodes, dict) else {}
                        localized_title = localized_node.get('title') if isinstance(localized_node, dict) else None
                        if localized_title:
                            chapter_titles_by_lang[str(lang)] = _clean_title(localized_title)
                    raw_chapter_title = str(
                        chapter_titles_by_lang.get(self.story_tree_default_language)
                        or node_data.get('title')
                        or f"Chapter {depth}"
                    ).strip()
                    chapter_title = re.sub(
                        r'\s+(?:chapter_\d+|node_[0-9a-f]+)\s*$',
                        '',
                        raw_chapter_title,
                        flags=re.IGNORECASE
                    ).strip() or f"Chapter {depth}"
                    script_content += "\n".join(node_converter.generate_utility_code(
                        chapter_data,
                        node_id,
                        depth,
                        chapter_title=chapter_title,
                        chapter_titles_by_lang=chapter_titles_by_lang,
                    ))
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

                    //  1. LocalStorage 拦截
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

                    //  2. 引擎初始化
                    try {{ localStorage.removeItem('VisualNovel_Settings'); }} catch(e) {{}}

                    // Monogatari 的原生存档命名空间由 Name/Version/Store
                    // 组成。模板默认 Name=Visual Novel，会让不同游戏共用同一
                    // 个 origin 的 Save_1；按 task/node 隔离，避免跨游戏串档。
                    try {{
                        const _storageId = String(window._gameTaskId || window._gameNodeId || 'game')
                            .replace(/[^A-Za-z0-9._-]+/g, '_');
                        if (_storageId) monogatari.setting('Name', 'Visual Novel ' + _storageId);
                    }} catch(e) {{ console.warn('[Storage] namespace setup failed:', e); }}

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
                        // 统一覆盖引擎内置词条，避免日语包在主菜单/快捷菜单
                        // 被 Monogatari 的默认翻译（終了する/ロード）重新写回。
                        try {{
                            monogatari.translation('日本語', {{
                                Quit:'クイット', QuitButton:'クイット',
                                Load:'読み込み', LoadButton:'読み込み',
                                Log:'ログ', Video:'動画音量'
                            }});
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
                        // Register the current locale's voice map before any
                        // main-menu Load action can restore a saved media
                        // state.  Continue/seek also call this helper, but a
                        // native load can bypass both entry labels entirely.
                        if (window.__registerCurrentVoices) window.__registerCurrentVoices();
                        if (window.SmartAutoPlay) window.SmartAutoPlay.install();
                        if (window.AutoPlayInterval) window.AutoPlayInterval.init();

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
                                    // 不能在这里清标记：originalRun 内部 End.apply 紧接着会调
                                    // global('playing', false)，标记被提前清掉会让下面的 global
                                    // hook 把它拦回 true，于是 playing 残留、下次 quit 不走握手。
                                    // 用深度计数在 end 执行期间维持许可，结束后再统一落幕。
                                    window._userQuitDepth = (window._userQuitDepth || 0) + 1;
                                    var _endResult;
                                    try {{
                                        _endResult = originalRun(statement, ...args);
                                    }} catch (e) {{
                                        window._userQuitDepth -= 1;
                                        if (window._userQuitDepth <= 0) window._userQuitFlag = false;
                                        throw e;
                                    }}
                                    return Promise.resolve(_endResult).finally(function () {{
                                        window._userQuitDepth -= 1;
                                        if (window._userQuitDepth <= 0) window._userQuitFlag = false;
                                    }});
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
                                    // 标记由 run hook 的 finally 统一清除。这里清掉会让
                                    // 同一次 quit 里后续的 end 步骤失去许可。
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
                                            // disabled 按钮仍保留命中区域，避免点击穿透到 game-screen。
                                            backBtn.style.pointerEvents = 'auto';
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
                                        if (window.GameNavigation) window.GameNavigation.jump('Start');
                                        else monogatari.run('jump Start');
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
                                            // disabled 按钮仍保留命中区域，避免点击穿透到 game-screen。
                                            backBtn.style.pointerEvents = 'auto';
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

                                        // Continue 直接跳过 Start；补做延迟语音注册，
                                        // 否则恢复到中途时 play voice 找不到映射。
                                        if (window.__registerCurrentVoices) window.__registerCurrentVoices();
                                        // 分支中途继续时直接进入精确 label，避免重放选项并重复结算 effects。
                                        const continueJump = window.GameNavigation
                                            ? window.GameNavigation.jump(targetLabel)
                                            : monogatari.run(`jump ${{targetLabel}}`);
                                        return Promise.resolve(continueJump).then((success) => {{
                                            if (success === false) throw new Error('Continue target was not reached');
                                        }});
                                    }}).catch((error) => {{
                                        console.error('[Continue] jump failed:', error);
                                        try {{ if (window.GameNavigation) window.GameNavigation.resetLocks(); }} catch (_) {{}}
                                        if (window.showToast) window.showToast(window.__ui ? window.__ui('loadFailed') : 'Unable to continue.');
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

                            const reporterIndex = window.GameProgressReporter
                                ? Number(window.GameProgressReporter.current || 0) : 0;
                            const savedIndex = window._progressData
                                ? Number(window._progressData.current_index || 0) : 0;
                            const visibleIndex = Math.max(reporterIndex, savedIndex);
                            if (visibleIndex > 1) {{
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
                            if (event.data && event.data.type === "INITIAL_PROGRESS" && window._messageMatchesContext(event, event.data.data || {{}})) {{
                                console.log('📥 [Message] 收到INITIAL_PROGRESS:', event.data.data);
                                const _incoming = event.data.data;
                                const _incomingIndex = Number(_incoming && _incoming.current_index || 0);
                                const _localIndex = Math.max(
                                    Number(window._progressData && window._progressData.current_index || 0),
                                    Number(window.GameProgressReporter && window.GameProgressReporter.current || 0),
                                    Number(window._currentMaterialIndex || 0)
                                );
                                const _pd = window.GameProgressReporter && window.GameProgressReporter._exitInProgress && _incomingIndex < _localIndex
                                    ? Object.assign({{}}, _incoming || {{}}, window._progressData || {{}}, {{ current_index: _localIndex }})
                                    : _incoming;
                                if (_pd && typeof _pd === 'object') window._progressData = _pd;
                                if (typeof window.updateButtonVisibility === 'function') {{
                                    window.updateButtonVisibility();
                                }}
                                if (_pd && _pd.player_variables &&
                                        Object.keys(_pd.player_variables).length > 0) {{
                                    window._playerVariables = window._sanitizePlayerVariables(_pd.player_variables);
                                    if (window._syncPlayerVars) window._syncPlayerVars();
                                    if (window.updateStatsUI) window.updateStatsUI();
                                }}
                                if (_pd && _pd.player_variable_choices) {{
                                    window._playerVariableChoices = window._sanitizeChoiceStats(_pd.player_variable_choices);
                                    if (window._syncPlayerVars) window._syncPlayerVars();
                                }}
                                if (_pd && _pd.player_legacy_flags) {{
                                    window._playerLegacyFlags = window._sanitizeLegacyFlags(_pd.player_legacy_flags);
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
                            const isInModal = t.closest('.modal, [role="dialog"], alert-modal, message-modal');
                            if (isQuit && isInModal) {{
                                console.log('🚪 [Quit] 用户点击Quit确认按钮');

                                // Flush first.  The old code disabled reporting
                                // and only requested a (possibly stale) snapshot
                                // 500ms later, so the current beat was lost.
                                if (window.GameProgressReporter && typeof window.GameProgressReporter.flushForExit === 'function') {{
                                    window.GameProgressReporter.flushForExit('user_exit');
                                }}

                                // [关键]设置用户主动quit标记,允许end命令执行
                                window._userQuitFlag = true;
                                console.log('✅ [Quit] 已设置_userQuitFlag=true，允许quit执行');

                                // 禁用进度上报,避免干扰
                                if (window.GameProgressReporter) window.GameProgressReporter._disableReport = true;

                                // Keep the host handshake for compatibility,
                                // but it is no longer responsible for saving the
                                // current beat.  The forced report above is the
                                // authoritative exit snapshot.
                                setTimeout(() => {{
                                    console.log('🔄 [Quit] 开始刷新进度和存档数据...');

                                    if (window.parent && window.parent !== window) {{
                                        //  1. 请求最新进度(和游戏加载时一样)
                                        console.log('📤 [Quit] 请求最新进度数据');
                                        window._postToParent({{
                                            type: "REQUEST_INITIAL_PROGRESS",
                                            data: {{
                                                chapter_id: "{chapter_id}",
                                                node_id: "{node_id}",
                                                timestamp: Date.now(),
                                                source: 'quit'  // 标记来源
                                            }}
                                        }});

                                        //  2. 延迟50ms后请求存档数据(和游戏加载时一样)
                                        setTimeout(() => {{
                                            console.log('📤 [Quit] 请求存档数据');
                                            window._postToParent({{
                                                type: "REQUEST_SAVE_DATA",
                                                data: {{
                                                    chapter_id: "{chapter_id}",
                                                    node_id: "{node_id}",
                                                    timestamp: Date.now(),
                                                    source: 'quit'
                                                }}
                                            }});
                                        }}, 50);

                                        //  3. 等待数据返回后更新按钮显示
                                        console.log('⏳ [Quit] 等待数据返回，将自动更新按钮显示');
                                    }}

                                    // Do not re-enable reporting after Quit: the
                                    // engine may reset to step 0 while closing,
                                    // which would overwrite the flushed value.
                                }}, 500);
                            }}
                        }}, true);

                        // 此时 Monogatari、SaveLoadManager、进度响应监听和按钮
                        // 都已建立，再发起初始请求，避免响应竞态丢失。
                        if (window.ProgressManager && !window._progressBootstrapStarted) {{
                            window._progressBootstrapStarted = true;
                            window.ProgressManager.init();
                        }}

                        // 引擎、章节脚本、UI 以及父端消息监听均已初始化，可安全接收主站数据。
                        if (window.GameHostBridge) {{
                            window.GameHostBridge.notifyChapterReady('monogatari_init_complete');
                        }}

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
                    # 只重建脚本时素材已在 OSS 上：仍要把页面里的相对路径改写成
                    # 绝对地址，否则游戏会去请求本地不存在的 assets/。
                    if self.enable_oss_upload or self.skip_asset_copy:
                        oss_base = node_converter._get_oss_base_url()
                        if oss_base:
                            node_converter._patch_options_oss(oss_base, self.output_dir.name, folder_name)
                            if self.enable_oss_upload:
                                node_converter._upload_assets_to_oss(self.output_dir.name, folder_name)
                        elif self.skip_asset_copy:
                            raise RuntimeError(
                                "只重建脚本需要 CDN_BASE_URL 或 OSS_BUCKET/OSS_ENDPOINT，"
                                "否则素材路径会指向本地不存在的 assets/"
                            )
                    else:
                        print(f"  ⏭️  跳过 OSS 上传（enable_oss_upload=False），本地保留 assets")

                    # 给 service-worker.js 注入唯一 name + 时间戳 version
                    # 必须在 OSS 阶段之后（OSS 阶段不动 sw 文件，先后无影响，但放这里语义最清晰）
                    node_converter._patch_service_worker(self.output_dir.name, folder_name)

                    print(f"节点 {node_id} 项目创建完成: {node_converter.output_dir}")
                    created_node_ids.append(str(node_id))
                else:
                    print(f"节点 {node_id} 无素材，跳过")
                    skipped_nodes.append((str(node_id), "no valid subtitle/audio material"))

            # A story tree with no usable source material must never be
            # published as a successful (but empty) game package.  Partial
            # trees remain supported for backwards compatibility: nodes with
            # no generated material are skipped, while at least one usable
            # chapter is required for a successful build.
            if not created_node_ids:
                details = "; ".join(
                    f"{node_id}: {reason}" for node_id, reason in skipped_nodes
                )
                raise RuntimeError(
                    "没有有效素材，无法生成任何章节游戏项目"
                    + (f"（{details}）" if details else "")
                )

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

            self._created_node_ids = tuple(created_node_ids)
            self._skipped_nodes = tuple(skipped_nodes)
            return {
                "created_node_ids": tuple(created_node_ids),
                "skipped_nodes": tuple(skipped_nodes),
                "output_dir": str(self.output_dir),
            }

        except Exception as e:
            print(f"创建项目时出错: {e}")
            import traceback
            traceback.print_exc()
            # Do not swallow conversion errors.  The Celery wrapper relies on
            # an exception to mark game_builder FAILED and prevent nginx from
            # advertising a partial/empty package.
            raise

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

        # 图标同样随 assets 上传后本地被删，manifest/index.html 里的相对路径
        # 会回源到已不存在的本地文件。
        for rel_path in ("manifest.json", "index.html"):
            icon_file = self.output_dir / rel_path
            if not icon_file.exists():
                continue
            with open(icon_file, 'r', encoding='utf-8') as f:
                content = f.read()
            patched = content.replace("assets/icons/", f"{oss_root}/icons/")
            if patched == content:
                continue
            with open(icon_file, 'w', encoding='utf-8') as f:
                f.write(patched)
            print(f"  已更新 {rel_path} 图标路径 -> {oss_root}/icons/")

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

        # 兼容旧版本地配置：统一配置直接挂载为 nginx.conf 时，需要
        # 显式加载 MIME 类型，并在 server 级设置静态文件根目录。
        if "include /etc/nginx/mime.types;" not in config_content:
            config_content = config_content.replace(
                "http {\n",
                "http {\n    include /etc/nginx/mime.types;\n    default_type application/octet-stream;\n",
                1,
            )
        if "server_name localhost;\n        root /usr/share/nginx/html;" not in config_content:
            config_content = config_content.replace(
                "        server_name localhost;\n",
                "        server_name localhost;\n        root /usr/share/nginx/html;\n",
                1,
            )

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
        import html as _html

        # 首页与章节脚本使用同一份语言树。兼容旧扁平结构，并让每个
        # chapter card 按 URL 的 ?lang= 动态显示对应语言的书名/章节名。
        index_story_tree = {}
        index_story_layers = {}
        story_tree_path = self.output_dir / "story_tree.json"
        if story_tree_path.exists():
            try:
                with open(story_tree_path, 'r', encoding='utf-8') as _f:
                    index_story_tree, index_story_layers = normalize_story_tree(json.load(_f))
            except Exception as _e:
                print(f"⚠️ 首页读取 story_tree.json 失败，使用通用标题: {_e}")

        if not index_story_layers:
            # 扁平旧结构没有语言 wrapper，也不能把 book_language（例如
            # zh-CN）暴露成运行期语言；未传 URL 参数一律按英语处理。
            index_story_layers = {'en-US': index_story_tree}
        _index_default_lang = 'en-US'

        def _clean_chapter_title(value, depth):
            text = str(value or '').strip()
            text = re.sub(r'\s+(?:chapter_\d+|node_[0-9a-f]+)\s*$', '', text, flags=re.IGNORECASE).strip()
            return text or f"Chapter {depth}"

        def _node_id_for_folder(folder):
            match = re.match(r'^chapter_\d+_(.+)$', str(folder))
            suffix = match.group(1) if match else str(folder)
            for node_id in (index_story_tree.get('nodes', {}) or {}):
                if str(node_id) == suffix or str(node_id).endswith(suffix):
                    return str(node_id)
            return suffix

        index_book_titles = {
            str(lang): str(layer.get('book_title') or '').strip()
            for lang, layer in index_story_layers.items()
            if isinstance(layer, dict) and str(layer.get('book_title') or '').strip()
        }
        index_chapter_titles = {}
        for folder in chapter_folders:
            node_id = _node_id_for_folder(folder)
            depth_match = re.match(r'^chapter_(\d+)', str(folder))
            depth = int(depth_match.group(1)) if depth_match else 1
            per_lang = {}
            for lang, layer in index_story_layers.items():
                nodes = layer.get('nodes', {}) if isinstance(layer, dict) else {}
                node = nodes.get(node_id, {}) if isinstance(nodes, dict) else {}
                if isinstance(node, dict) and node.get('title'):
                    per_lang[str(lang)] = _clean_chapter_title(node.get('title'), depth)
            index_chapter_titles[str(folder)] = per_lang

        index_ui = {
            'en-US': {
                'chapterSelection': 'Chapter Selection',
                'selectChapter': 'Please select a chapter to begin:',
                'clickToStart': 'Click to start',
                'locked': 'The demo includes Chapters 1 and 2 only.',
                'footer': 'Powered by Monogatari.js | Deployed: {deploy_time}',
            },
            'ja-JP': {
                'chapterSelection': 'チャプター選択',
                'selectChapter': '開始するチャプターを選択してください：',
                'clickToStart': 'クリックして開始',
                'locked': '体験版では第2章までプレイできます。',
                'footer': 'Powered by Monogatari.js | Deployed: {deploy_time}',
            },
        }
        for lang, custom in extract_story_ui_text(index_story_layers).items():
            index_ui.setdefault(lang, {}).update(custom)
        index_i18n_json = json.dumps({
            'defaultLang': _index_default_lang,
            'bookTitles': index_book_titles,
            'chapterTitles': index_chapter_titles,
            'ui': index_ui,
        }, ensure_ascii=False).replace('</', '<\\/')

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
        <h1 id="index-title">🎮 Visual Novel - Chapter Selection</h1>
        <p id="index-subtitle" style="text-align: center; margin-bottom: 30px;">Please select a chapter to begin:</p>

        <div class="chapter-list">
"""

        # This is a plain triple-quoted template, so CSS braces must be
        # normalized before dynamic chapter-card content is appended below.
        index_html = index_html.replace('{{', '{').replace('}}', '}')

        for folder in sorted(chapter_folders):
            # 尝试从章节文件夹中读取index.html获取章节标题
            _node_id = _node_id_for_folder(folder)
            chapter_title = index_chapter_titles.get(str(folder), {}).get(
                _index_default_lang,
                folder.replace('_', ' ').title()
            )
            # 从文件夹名 chapter_{depth}_{uuid} 解析章节深度,depth>2 标记为试玩锁定
            _depth = 1
            _parts = folder.split('_')
            if len(_parts) >= 2 and _parts[0] == 'chapter' and _parts[1].isdigit():
                _depth = int(_parts[1])
            _locked_attr = ' data-locked="1"' if _depth > 2 else ''
            index_html += f"""            <a href="/{game_project}/{folder}/?lang={_html.escape(_index_default_lang, quote=True)}" class="chapter-card" data-folder="{_html.escape(folder, quote=True)}" data-node-id="{_html.escape(_node_id, quote=True)}"{_locked_attr}>
                <h3>{_html.escape(str(chapter_title))}</h3>
                <p class="chapter-cta">Click to start</p>
            </a>
"""

        index_footer_html = """        </div>

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
            const lang = String(new URLSearchParams(window.location.search).get('lang') || '');
            const lockedText = lang === 'ja-JP'
                ? '体験版では第2章までプレイできます。'
                : 'The demo includes Chapters 1 and 2 only.';
            if (standalone) {{
                document.querySelectorAll('.chapter-card[data-locked="1"]').forEach(function(el) {{
                    el.style.opacity = '0.45';
                    el.style.pointerEvents = 'none';
                    el.style.cursor = 'not-allowed';
                    const p = el.querySelector('p');
                    if (p) p.textContent = lockedText;
                }});
            }}
        }});
    </script>
</body>
</html>"""

        # Normalize only the static footer block as well.  Doing this on the
        # block itself avoids rewriting literal braces that may legitimately
        # occur in a localized chapter title or other dynamic content.
        index_html += index_footer_html.replace('{{', '{').replace('}}', '}')

        index_runtime_script = """
    <script>
        const INDEX_I18N = __INDEX_I18N__;
        (function () {
            function resolveLang(raw) {
                // URL locale 必须精确匹配；带空格或大小写变化也回退英语。
                const value = String(raw || '');
                if (value !== 'en-US' && value !== 'ja-JP') return 'en-US';
                return value;
            }
            document.addEventListener('DOMContentLoaded', function () {
                const params = new URLSearchParams(window.location.search);
                const lang = resolveLang(params.get('lang'));
                const ui = (INDEX_I18N.ui && (INDEX_I18N.ui[lang] || INDEX_I18N.ui[INDEX_I18N.defaultLang])) || {};
                const bookTitle = (INDEX_I18N.bookTitles && (INDEX_I18N.bookTitles[lang] || INDEX_I18N.bookTitles[INDEX_I18N.defaultLang])) || 'Visual Novel';
                document.documentElement.lang = lang;
                document.title = bookTitle + ' - ' + (ui.chapterSelection || 'Chapter Selection');
                const heading = document.getElementById('index-title');
                const subtitle = document.getElementById('index-subtitle');
                if (heading) heading.textContent = '🎮 ' + bookTitle + ' - ' + (ui.chapterSelection || 'Chapter Selection');
                if (subtitle) subtitle.textContent = ui.selectChapter || 'Please select a chapter to begin:';
                document.querySelectorAll('.chapter-card').forEach(function (card) {
                    const folder = card.getAttribute('data-folder') || '';
                    const titles = (INDEX_I18N.chapterTitles && INDEX_I18N.chapterTitles[folder]) || {};
                    const h3 = card.querySelector('h3');
                    const cta = card.querySelector('.chapter-cta');
                    if (h3) h3.textContent = titles[lang] || titles[INDEX_I18N.defaultLang] || h3.textContent;
                    if (cta) cta.textContent = ui.clickToStart || 'Click to start';
                    const href = new URL(card.getAttribute('href'), window.location.href);
                    href.searchParams.set('lang', lang);
                    card.setAttribute('href', href.pathname + href.search);
                    if (card.hasAttribute('data-locked') && !(window.parent && window.parent !== window)) {
                        card.style.opacity = '0.45';
                        card.style.pointerEvents = 'none';
                        card.style.cursor = 'not-allowed';
                        if (cta) cta.textContent = ui.locked || 'The demo includes Chapters 1 and 2 only.';
                    }
                });
                const footer = document.querySelector('.footer p');
                if (footer) footer.textContent = (ui.footer || 'Powered by Monogatari.js | Deployed: {deploy_time}').replace('{deploy_time}', new Date().toLocaleString(lang));
            });
        }());
    </script>
""".replace('__INDEX_I18N__', index_i18n_json)
        index_html = index_html.replace('</body>', index_runtime_script + '\n</body>')

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
