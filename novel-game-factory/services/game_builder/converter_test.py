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


class FinalConverter:
    def __init__(self, source_dir="novel-game-factory/storage/outputs",
                 template_dir="novel-game-factory/services/game_builder/game_template/shijuexiaoshuo",
                 output_dir="novel-game-factory/storage/built_games/shijuexiaoshuo-temp", images_base_dir=None,
                 domain=None, art_style="modern_realism", delete_local_assets=True, task_id=None):
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

        # OSS上传后是否删除本地assets目录
        self.delete_local_assets = delete_local_assets

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
            # 优先扫描新目录 audios/bgm
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
        effects = {'a': {}, 'b': {}}
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
        return effects

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
                        # 去掉说话人前缀（如 "旁白：" / "PangBai:" 等）
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
                        'option_text': option_text  # 仅 seq==1 时有值
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

                # 选项文本从 seq==1 的字幕里取
                def _get_option_text(br_letter):
                    sub1 = branch_subtitle.get(idx, {}).get(br_letter, {}).get(1)
                    return sub1.get('option_text') if sub1 else None

                effects = self._parse_branch_effects(branch_c_content.get(idx, ''))

                # 取 scene_id（从 A1 音频或字幕）
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
        # 支持新格式（scene_id-index）和旧格式（scene_id_index）
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
        # 规则：从字幕文件中提取说话人，对应的角色图片
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

        # 复制角色图片（直接匹配字幕里实际出现的完整文件名，加上characterFileMap默认图片）
        for info in self.character_map.values():
            if info.get('image'):
                needed_characters.add(info['image'])
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
            audios_to_declare = self.audio_files
            music_to_declare = self.music_files
            sfx_to_declare = self.sfx_files
        else:
            scenes_to_declare = copied_assets.get('scenes', [])
            audios_to_declare = copied_assets.get('audios', [])
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

        # SFX 按 index 建立映射（一个 index 可能有多个 SFX，用列表）
        sfx_by_index = {}
        for sfx in self.sfx_files:
            sfx_idx = sfx['index']
            sfx_by_index.setdefault(sfx_idx, []).append(sfx)

        # ==================== 核心修正：预处理场景状态 ====================
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
                    'audio': None,
                    'subtitle': None,
                    'index': idx,
                    'scene_to_show': current_active_scene,
                    'chapter': 1, 'scene': 1,
                    'choice_node': cn   # 标记为软分支节点
                })
                continue

            audio = audio_by_index.get(idx)
            subtitle = subtitle_by_index.get(idx)

            # 获取这一步原始数据里定义的场景ID
            raw_scene_id = audio.get('scene_id') if audio else subtitle.get('scene_id') if subtitle else 'default'

            # 如果原始数据里有有效场景ID，更新追踪器
            if raw_scene_id and raw_scene_id != 'default' and raw_scene_id in valid_scene_stems:
                current_active_scene = raw_scene_id

            audio_subtitle_pairs.append({
                'audio': audio,
                'subtitle': subtitle,
                'index': idx,
                'scene_to_show': current_active_scene,  # 【关键】这一步必须显示的图
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
        lines.append(
            "                if (item.a) { var au = new Audio(); au.preload = 'auto'; au.src = 'assets/voices/' + item.a; }")
        lines.append("                if (item.s) { var im = new Image(); im.src = 'assets/scenes/' + item.s; }")
        lines.append("            }")
        lines.append("        });")
        lines.append("    },")
        lines.append("    step: function(idx) { this.load(idx + 1, this.WINDOW); }")
        lines.append("};")
        lines.append("")

        # 所有音频的 stem->name 映射，在 Start 时注册到 Monogatari，不触发预加载
        lines.append("// 音频注册表(Start 时注册,不参与 loading 预加载)")
        lines.append("window._allVoices = {")
        for pair in audio_subtitle_pairs:
            if pair.get('choice_node'):
                # 收集分支音频
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
        lines.append(f"                // ── 数值初始化 ──")
        lines.append(f"                const _initVars = {init_vars_js};")
        lines.append(f"                // 优先级:URL参数 > localStorage章节入口快照 > tree.json初始值")
        lines.append(f"                (function() {{")
        lines.append(f"                    let vars = null;")
        lines.append(f"                    //  1. 尝试从 URL 参数读取(章节跳转时携带)")
        lines.append(f"                    try {{")
        lines.append(f"                        const urlParams = new URLSearchParams(window.location.search);")
        lines.append(f"                        const varsParam = urlParams.get('vars');")
        lines.append(f"                        if (varsParam) vars = JSON.parse(decodeURIComponent(varsParam));")
        lines.append(f"                    }} catch(e) {{}}")
        lines.append(f"                    //  2. 尝试从 localStorage 章节入口快照读取(刷新/重启后恢复)")
        lines.append(f"                    if (!vars) {{")
        lines.append(f"                        try {{")
        lines.append(f"                            const snap = localStorage.getItem('_chapterEntryVars');")
        lines.append(f"                            if (snap) vars = JSON.parse(snap);")
        lines.append(f"                        }} catch(e) {{}}")
        lines.append(f"                    }}")
        lines.append(f"                    //  3. 兜底:tree.json 初始值")
        lines.append(f"                    if (!vars) vars = Object.assign({{}}, _initVars);")
        lines.append(f"                    window._playerVariables = vars;")
        lines.append(f"                    // 保存章节入口快照(防 restart 污染)")
        lines.append(f"                    try {{ localStorage.setItem('_chapterEntryVars', JSON.stringify(vars)); }} catch(e) {{}}")
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
        # 简单直接跳转，无需任何额外逻辑，因为目标Material里包含了背景指令
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

            # ==================== 软分支节点 ====================
            if pair.get('choice_node'):
                cn = pair['choice_node']
                # 计算收束后的下一个 index
                next_idx = audio_subtitle_pairs[pair_idx + 1]['index'] if pair_idx + 1 < len(audio_subtitle_pairs) else None
                next_label = f"Material_{next_idx}" if next_idx else "EndChapter"

                effects = cn.get('effects', {'a': {}, 'b': {}})
                effects_a_js = json.dumps(effects.get('a', {}), ensure_ascii=False)
                effects_b_js = json.dumps(effects.get('b', {}), ensure_ascii=False)

                opt_a = cn['option_a_text'].replace("'", "\\'")
                opt_b = cn['option_b_text'].replace("'", "\\'")

                # Material_N label：展示选项
                lines.append(f"    'Material_{idx}': [")
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
                lines.append(f"                    'Text': '{opt_a}',")
                lines.append(f"                    'Do': 'jump ChoiceExec_{idx}_A',")
                lines.append(f"                }},")
                lines.append(f"                'BranchB_{idx}': {{")
                lines.append(f"                    'Text': '{opt_b}',")
                lines.append(f"                    'Do': 'jump ChoiceExec_{idx}_B',")
                lines.append(f"                }},")
                lines.append(f"            }}")
                lines.append(f"        }},")
                lines.append(f"    ],")
                lines.append(f"")

                # ChoiceExec label：修改数值后跳分支
                for br_letter, effects_js in [('A', effects_a_js), ('B', effects_b_js)]:
                    lines.append(f"    'ChoiceExec_{idx}_{br_letter}': [")
                    lines.append(f"        {{'Function':{{")
                    lines.append(f"            'Apply': () => {{")
                    lines.append(f"                const fx = {effects_js};")
                    lines.append(f"                window._playerVariables = window._playerVariables || {{}};")
                    lines.append(f"                for (const [k, v] of Object.entries(fx)) {{")
                    lines.append(f"                    window._playerVariables[k] = (window._playerVariables[k] || 0) + v;")
                    lines.append(f"                }}")
                    lines.append(f"                if (window.updateStatsUI) window.updateStatsUI();")
                    lines.append(f"                if (window._syncPlayerVars) window._syncPlayerVars();")
                    lines.append(f"                return true;")
                    lines.append(f"            }},")
                    lines.append(f"            'Reverse': () => {{")
                    lines.append(f"                const fx = {effects_js};")
                    lines.append(f"                window._playerVariables = window._playerVariables || {{}};")
                    lines.append(f"                for (const [k, v] of Object.entries(fx)) {{")
                    lines.append(f"                    window._playerVariables[k] = (window._playerVariables[k] || 0) - v;")
                    lines.append(f"                }}")
                    lines.append(f"                if (window.updateStatsUI) window.updateStatsUI();")
                    lines.append(f"                if (window._syncPlayerVars) window._syncPlayerVars();")
                    lines.append(f"                return true;")
                    lines.append(f"            }}")
                    lines.append(f"        }}}},")
                    lines.append(f"        'jump Branch{br_letter}_{idx}_1',")
                    lines.append(f"    ],")
                    lines.append(f"")

                # 分支 labels
                for br_letter, br_mats in [('A', cn.get('branch_a', [])), ('B', cn.get('branch_b', []))]:
                    for bm_idx, bm in enumerate(br_mats):
                        seq = bm['seq']
                        b_audio = bm.get('audio')
                        b_sub = bm.get('subtitle')
                        label = f"Branch{br_letter}_{idx}_{seq}"
                        lines.append(f"    '{label}': [")

                        # 背景（复用当前 scene_to_show）
                        if has_scene_image:
                            lines.append(f"        'show scene {scene_to_show}',")
                        else:
                            lines.append(f"        'show scene #000000',")

                        # 音频
                        if b_audio:
                            b_audio_id = b_audio['id']
                            lines.append(f"        'play voice {b_audio_id}',")

                        # 对话
                        has_blocking = False
                        if b_sub and b_sub.get('dialogues'):
                            for dialogue in b_sub['dialogues']:
                                speaker = dialogue['speaker']
                                raw_key = dialogue.get('raw_key', '')
                                text = dialogue['text']
                                escaped_speaker = speaker.replace("'", "\\'")
                                text_clean = text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').strip()

                                specific_image = None
                                if raw_key and hasattr(self, 'valid_image_stems') and raw_key in self.valid_image_stems:
                                    specific_image = raw_key + ".png"

                                if specific_image:
                                    esc_img = specific_image.replace("'", "\\'")
                                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ StageManager.forceImage('{escaped_speaker}', '{esc_img}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                                else:
                                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ updateStage('{escaped_speaker}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

                                char_id = self.chinese_to_id.get(speaker, 'nb')
                                if speaker == '旁白':
                                    char_id = 'nb'
                                elif char_id != 'nb':
                                    has_char_img = specific_image is not None or any(
                                        s.startswith(char_id) for s in self.valid_image_stems)
                                    if not has_char_img:
                                        char_id = 'nb'

                                if text_clean:
                                    if char_id == 'nb':
                                        lines.append(f"        '{text_clean}',")
                                    else:
                                        lines.append(f"        '{char_id} {text_clean}',")
                                    has_blocking = True
                                    lines.append(f"        {{'Function':{{ 'Apply': () => {{ if(monogatari.global){{ monogatari.global('auto_play',false); monogatari.global('skipping',false); }} return true; }}, 'Reverse': () => {{ return true; }} }} }},")

                        if not has_blocking:
                            lines.append(f"        'nb ...',")

                        # 跳转：下一个分支 beat 或收束
                        if bm_idx + 1 < len(br_mats):
                            next_seq = br_mats[bm_idx + 1]['seq']
                            lines.append(f"        'jump Branch{br_letter}_{idx}_{next_seq}',")
                        else:
                            # 分支结束，收束回主线
                            # 收束点强制重置舞台，防止穿帮
                            lines.append(f"        {{'Function':{{ 'Apply': () => {{ StageManager.reset(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                            lines.append(f"        'jump {next_label}',")

                        lines.append(f"    ],")
                        lines.append(f"")

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

            lines.append(f"    'Material_{idx}': [")

            # 1. 状态重置 (UI更新)
            lines.append(f"        {{'Function':{{ ")
            lines.append(f"            'Apply': () => {{")
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
            lines.append(f"                return true;")
            lines.append(f"            }}, ")
            lines.append(f"            'Reverse': () => {{")
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
            lines.append(f"                return true;")
            lines.append(f"            }}}}")
            lines.append(f"        }},")

            # 序章钩子：第一个 hook beat 时，最先进入钩子模式（在音乐/场景之前，确保text-box立即隐藏）
            if is_first_hook_beat:
                lines.append(
                    f"        {{'Function':{{ 'Apply': () => {{ return HookManager.enter(); }}, 'Reverse': () => {{ return true; }} }} }},")

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

            # 2b. 停止之前的所有SFX（SFX只绑定一个索引，进入下一步立即停）
            # 弃用指定ID停止（防止快进、跨步跳跃时漏掉指令），直接使用无参数的 'stop sound' 全局强杀所有残留音效
            lines.append(f"        'stop sound',")

            # 3. 场景控制 (核心逻辑：每一步都显式指定背景)
            has_scene_image = any(img['stem'] == scene_to_show for img in self.scene_images)

            # 判断特效：只有当这一步的图和上一步不一样时，才 FadeIn
            # 如果是一样的图，Monogatari 默认会忽略指令（无感刷新），这正是我们要的
            transition_suffix = ""
            if scene_to_show != previous_shown_image:
                transition_suffix = " with fadeIn"
                previous_shown_image = scene_to_show  # 更新记录

            if has_scene_image:
                lines.append(f"        'show scene {scene_to_show}{transition_suffix}',")
            else:
                # 没图就用黑底保底，防止白底白字
                lines.append(f"        'show scene #000000{transition_suffix}',")

            # 4. 播放音频
            if audio:
                lines.append(f"        'play voice {audio['id']}',")

            # 4b. 播放SFX（与voice并行，不阻塞）
            for sfx in sfx_by_index.get(idx, []):
                escaped_sfx_stem = sfx['stem'].replace("'", "\\'")
                lines.append(f"        'play sound {escaped_sfx_stem}',")

            # 5. 对话处理
            has_blocking_action = False
            if subtitle and subtitle.get('dialogues'):
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

                    if is_hook_beat:
                        # ---- 序章钩子模式：不显示立绘/对话框，文本走 HookManager ----
                        if text_clean:
                            lines.append(
                                f"        {{'Function':{{ 'Apply': () => {{ return HookManager.setText('{text_clean}'); }}, 'Reverse': () => {{ return true; }} }} }},")
                            # 用 Monogatari 原生对话阻塞（text-box 被 CSS 隐藏，overlay 点击会转发到 text-box）
                            lines.append(f"        'nb {text_clean}',")
                            has_blocking_action = True
                    else:
                        # ---- 普通模式：立绘更新 + 对话框 ----
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
                        if speaker == '旁白':
                            char_id_for_text = 'nb'
                        elif char_id_for_text != 'nb':
                            has_char_image = specific_image is not None or any(
                                s.startswith(char_id_for_text) for s in self.valid_image_stems
                            )
                            if not has_char_image:
                                char_id_for_text = 'nb'

                        if text_clean:
                            if char_id_for_text == 'nb':
                                lines.append(f"        '{text_clean}',")
                            else:
                                lines.append(f"        '{char_id_for_text} {text_clean}',")
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
            if pair_idx < len(audio_subtitle_pairs) - 1:
                next_idx = audio_subtitle_pairs[pair_idx + 1]['index']
                lines.append(f"        'jump Material_{next_idx}',")
            else:
                lines.append(f"        'jump EndChapter',")

            lines.append(f"    ],")
            lines.append("")

        # 结尾逻辑
        lines.append(f"    'EndChapter': [")
        lines.append(f"        'stop sound',")  # 确保进入选择界面时所有音效停止
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
                lines.append(f"                    const varsStr = encodeURIComponent(JSON.stringify(vars));")
                lines.append(f"                    function _goto(path) {{")
                lines.append(f"                        const sep = path.includes('?') ? '&' : '?';")
                lines.append(f"                        window.location.href = path + sep + 'vars=' + varsStr;")
                lines.append(f"                    }}")
                lines.append(f"                    // 找出数值最高的变量")
                lines.append(f"                    let maxVar = null, maxVal = -Infinity;")
                lines.append(f"                    for (const [k, v] of Object.entries(vars)) {{")
                lines.append(f"                        if (v > maxVal) {{ maxVal = v; maxVar = k; }}")
                lines.append(f"                    }}")
                lines.append(f"                    console.log('🎯 [EndChapter] 最高变量:', maxVar, maxVal);")
                # 按 branch_variable 匹配，检查最高变量是否达到阈值
                for child in conditional_endings:
                    bv  = child['branch_variable']
                    thr = child['branch_threshold']
                    tf  = (folder_mapping.get(child['child_id'], '') or '').replace("'", "\\'")
                    base_url = f"/{game_project + '/' if game_project else ''}{tf}/"
                    lines.append(f"                    if (maxVar === '{bv}' && maxVal >= {thr}) {{")
                    lines.append(f"                        console.log('✅ [EndChapter] 路由到结局: {tf}');")
                    lines.append(f"                        _goto('{base_url}'); return true;")
                    lines.append(f"                    }}")
                if fallback_ending:
                    tf  = (folder_mapping.get(fallback_ending['child_id'], '') or '').replace("'", "\\'")
                    base_url = f"/{game_project + '/' if game_project else ''}{tf}/"
                    lines.append(f"                    // 兜底:原著结局")
                    lines.append(f"                    console.log('📖 [EndChapter] 兜底原著结局: {tf}');")
                    lines.append(f"                    _goto('{base_url}'); return true;")
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
                    lines.append(f"                const targetUrl = _baseUrl + '?vars=' + _varsStr;")
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
            # 如果没有分支选择，才执行end返回主菜单
            lines.append(f"        'end',")
            lines.append(f"    ],")

        lines.append("});")

        return "\n".join(lines)

    def _get_ui_theme_css(self) -> str:
        """根据 art_style 返回对应的游戏 UI 主题 CSS（文本框/名字框/快捷菜单）"""
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
        return themes.get(self.art_style, themes["modern_realism"])

    def _get_hook_css(self) -> str:
        """根据 art_style 返回对应的序章钩子 CSS"""

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

        lines = []
        lines.append(f"""
// ==================== 样式注入 ====================
window.addEventListener('load', function() {{
    const customStyles = `{css_content}`;
    const styleSheet = document.createElement("style");
    styleSheet.textContent = customStyles;
    document.head.appendChild(styleSheet);

    const overlayDiv = document.createElement("div");
    overlayDiv.id = "cinematic-overlay";
    if (document.body) document.body.appendChild(overlayDiv);

    // ── 数值面板 DOM ──
    const statsToggle = document.createElement("div");
    statsToggle.id = "stats-toggle";
    statsToggle.title = "查看数值";
    statsToggle.textContent = "◈";
    document.body.appendChild(statsToggle);

    const statsPanel = document.createElement("div");
    statsPanel.id = "stats-panel";
    document.body.appendChild(statsPanel);

    statsToggle.addEventListener("click", function() {{
        statsPanel.classList.toggle("open");
        if (window.updateStatsUI) window.updateStatsUI();
    }});
}});

// ==================== 数值同步与UI ====================
window._syncPlayerVars = function() {{
    try {{
        if (monogatari && monogatari.storage) {{
            const s = monogatari.storage();
            s.playerVars = Object.assign({{}}, window._playerVariables || {{}});
            monogatari.storage(s);
        }}
    }} catch(e) {{}}
    try {{
        localStorage.setItem('_playerVarsCurrent', JSON.stringify(window._playerVariables || {{}}));
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
    pendingJumpUrl: null,  // 保存待跳转的 URL

    go: function(targetFolderName) {{
        const targetUrl = "/" + targetFolderName + "/";

        // 保存目标 URL
        this.pendingJumpUrl = targetUrl;

        // 发送跳转申请
        const requestMsg = {{
            "type": "CHAPTER_REQUEST",
            "data": {{
                "target_node_id": targetFolderName,
                "target_url": targetUrl,
                "timestamp": Date.now()
            }}
        }};
        console.log("📤 [Iframe] 申请跳转:", requestMsg);

        if (window.parent && window.parent !== window) {{
            // Iframe 模式:发送消息并等待回信
            window.parent.postMessage(requestMsg, '*');
        }} else {{
            // 独立调试模式:直接跳转
            console.log("🔧 [Debug] 独立模式直接跳转");
            window.location.href = targetUrl;
        }}
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
                "trigger_source": triggerSource
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
                    task_id=self.task_id
                )

                # 复制当前实例的角色信息
                node_converter.pinyin_to_chinese = self.pinyin_to_chinese.copy()
                node_converter.chinese_to_id = self.chinese_to_id.copy()
                node_converter.chinese_to_pinyin = self.chinese_to_pinyin.copy()

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

                    monogatari.init('#monogatari').then(() => {{
                        console.log('✅ Visual Novel Engine Started');

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
                                restartBtn.textContent = 'Restart';
                                restartBtn.classList.add('restart-button');
                                restartBtn.removeAttribute('data-action');
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
                                continueBtn.textContent = 'Continue';
                                continueBtn.classList.add('continue-button');
                                continueBtn.removeAttribute('data-action');
                                const restartBtn = document.querySelector('.restart-button');
                                startButton.parentNode.insertBefore(continueBtn, restartBtn ? restartBtn.nextSibling : startButton.nextSibling);

                                continueBtn.addEventListener('click', (e) => {{
                                    e.preventDefault(); e.stopPropagation();

                                    // 获取目标索引
                                    const targetIndex = (window._progressData && window._progressData.current_index) ? window._progressData.current_index : 1;
                                    console.log(`📥 [Main] Continue -> 跳转到Material_${{targetIndex}}`);

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

                                        // 直接跳转到Material_X,就像从Material_1开始一样
                                        monogatari.run(`jump Material_${{targetIndex}}`);
                                    }});
                                }});
                                continueBtn.style.display = 'none';
                            }}

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
                                window._progressData = event.data.data;
                                console.log('📥 [Message] 已更新_progressData，准备更新按钮');
                                if (typeof window.updateButtonVisibility === 'function') {{
                                    window.updateButtonVisibility();
                                }} else {{
                                    console.error('❌ [Message] updateButtonVisibility函数不存在');
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
                    oss_base = node_converter._get_oss_base_url()
                    if oss_base:
                        node_converter._patch_options_oss(oss_base, self.output_dir.name, folder_name)
                        node_converter._upload_assets_to_oss(self.output_dir.name, folder_name)

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
        """获取OSS或CDN的基础URL（优先CDN，无CDN则用OSS直连）"""
        cdn_base = os.getenv("CDN_BASE_URL", "").rstrip("/")
        if cdn_base:
            return cdn_base
        bucket = os.getenv("OSS_BUCKET", "")
        endpoint = os.getenv("OSS_ENDPOINT", "")
        if bucket and endpoint:
            return f"https://{bucket}.{endpoint}"
        return ""

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
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  已更新 script.js 硬编码 assets/ 路径 -> {oss_root}")

    def _upload_assets_to_oss(self, game_project: str, folder_name: str) -> bool:
        """将 assets 目录上传到 OSS，成功后删除本地 assets 目录"""
        try:
            import oss2
        except ImportError:
            print("  警告: oss2 未安装，跳过OSS上传。请运行: pip install oss2")
            return False

        access_key = os.getenv("OSS_ACCESS_KEY", "")
        secret_key = os.getenv("OSS_SECRET_KEY", "")
        endpoint = os.getenv("OSS_ENDPOINT", "")
        bucket_name = os.getenv("OSS_BUCKET", "")

        if not all([access_key, secret_key, endpoint, bucket_name]):
            print("  警告: OSS配置不完整，跳过上传")
            return False

        assets_dir = self.output_dir / "assets"
        if not assets_dir.exists():
            print(f"  警告: assets目录不存在: {assets_dir}")
            return False

        auth = oss2.Auth(access_key, secret_key)
        bucket = oss2.Bucket(auth, f"https://{endpoint}", bucket_name)

        oss_prefix = f"{game_project}/{folder_name}/assets"
        # 根据扩展名设置 Content-Type，并强制 inline 防止浏览器触发下载
        mime_map = {
            '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.webp': 'image/webp',
            '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.wav': 'audio/wav',
            '.js': 'application/javascript', '.css': 'text/css',
            '.html': 'text/html', '.json': 'application/json',
        }
        skip_names = {'.gitignore', '.gitkeep', '.DS_Store', 'Thumbs.db'}
        uploaded = 0
        uploaded_char_stems = []
        for file_path in assets_dir.rglob("*"):
            if file_path.is_file() and file_path.name not in skip_names:
                relative = file_path.relative_to(assets_dir)
                oss_key = f"{oss_prefix}/{relative.as_posix()}"
                content_type = mime_map.get(file_path.suffix.lower(), 'application/octet-stream')
                headers = {
                    'Content-Type': content_type,
                    'Content-Disposition': 'inline',
                }
                for attempt in range(3):
                    try:
                        bucket.put_object_from_file(oss_key, str(file_path), headers=headers)
                        break
                    except Exception as e:
                        if attempt < 2:
                            import time
                            time.sleep(2 ** attempt)
                        else:
                            raise
                uploaded += 1
                # 收集角色图片 stem，用于回写 char.json 的 oss_route
                if relative.parts[0] == "characters" and file_path.suffix.lower() == ".png":
                    uploaded_char_stems.append(file_path.stem)

        print(f"  已上传 {uploaded} 个文件到 OSS: oss://{bucket_name}/{oss_prefix}")

        if uploaded_char_stems:
            self._update_char_json_oss_routes(folder_name, uploaded_char_stems)

        if self.delete_local_assets:
            shutil.rmtree(assets_dir)
            print(f"  已删除本地 assets 目录: {assets_dir}")
        else:
            print(f"  保留本地 assets 目录: {assets_dir}")
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
            # 可以改进：从章节文件夹的index.html中提取标题
            index_html += f"""            <a href="/{game_project}/{folder}/" class="chapter-card">
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
