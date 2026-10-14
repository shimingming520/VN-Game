#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视觉小说素材转换脚本 - 最终版
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
    def __init__(self, source_dir="novel-game-factory/storage/outputs", template_dir="novel-game-factory/services/game_builder/game_template/shijuexiaoshuo", output_dir="novel-game-factory/storage/built_games/shijuexiaoshuo-temp", images_base_dir=None):
        """
        初始化转换器

        Args:
            source_dir: 素材源目录
            template_dir: 物语引擎模板目录
            output_dir: 输出目录
            images_base_dir: 图片基础目录，默认为source_dir.parent
        """
        self.source_dir = Path(source_dir)
        self.template_dir = Path(template_dir)
        self.output_dir = Path(output_dir)
        if images_base_dir:
            self.images_base_dir = Path(images_base_dir)
        else:
            # 默认：如果 source_dir 是 outputs-temp 目录，则 images_base_dir 为 source_dir 本身
            # 否则（例如 source_dir 是 node_xxx 目录），则 images_base_dir 为 source_dir 的父目录（即 outputs-temp 根目录）
            if self.source_dir.name == "outputs":
                self.images_base_dir = self.source_dir
            else:
                self.images_base_dir = self.source_dir.parent

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
        # 新正则：适配场景ID_序号格式（例如 DaTiePu_1.mp3）
        self.scene_patterns = [
            re.compile(r'(.+)[-_](\d+)')  # 匹配 scene_id_index 格式，支持 - 和 _ 分隔符
        ]
        self.music_pattern = re.compile(r'music_(\d+)_(\d+)')
        self.subtitle_patterns = [
            # 修改点：将 [^：]+ 改为 [^:：]+ ，意思是“只要遇到任意一种冒号就停止匹配”
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
                print(f"发现场景图片: {scene_file.name}")
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

        # 扫描背景音乐文件
        for node_dir in node_dirs_to_scan:
            music_dir = node_dir / "audios" / "music"
            if music_dir.exists():
                for music_file in music_dir.glob("*.mp3"):
                    match = self.music_pattern.match(music_file.stem)
                    if match:
                        start_idx = int(match.group(1))
                        end_idx = int(match.group(2))
                        self.music_files.append({
                            'path': music_file,
                            'name': music_file.name,
                            'stem': music_file.stem,
                            'start': start_idx,
                            'end': end_idx
                        })
                        print(f"发现背景音乐: {music_file.name} (索引范围: {start_idx} - {end_idx})")
                    else:
                        print(f"警告: 背景音乐文件名格式不正确: {music_file.name}")
            else:
                # 如果 audios/music 不存在，尝试直接扫描 music 目录
                music_dir_alt = node_dir / "music"
                if music_dir_alt.exists():
                    for music_file in music_dir_alt.glob("*.mp3"):
                        match = self.music_pattern.match(music_file.stem)
                        if match:
                            start_idx = int(match.group(1))
                            end_idx = int(match.group(2))
                            self.music_files.append({
                                'path': music_file,
                                'name': music_file.name,
                                'stem': music_file.stem,
                                'start': start_idx,
                                'end': end_idx
                            })
                            print(f"发现背景音乐: {music_file.name} (索引范围: {start_idx} - {end_idx})")
                        else:
                            print(f"警告: 背景音乐文件名格式不正确: {music_file.name}")

        print(f"扫描完成: {len(self.audio_files)}个音频, {len(self.subtitle_files)}个字幕, "
              f"{len(self.scene_images)}个场景, {len(self.character_images)}个角色图片, {len(self.music_files)}个背景音乐")

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
                return result[0].path[0] # 返回概率最高的词
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
        """从文件名提取场景信息（新格式：scene_id_index）"""
        stem = Path(filename).stem
        for pattern in self.scene_patterns:
            match = pattern.match(stem)
            if match:
                if len(match.groups()) == 2:
                    # 新格式：scene_id_index
                    scene_id = match.group(1)  # 如 datiepu
                    index = int(match.group(2))  # 如 1
                    return {
                        'scene_id': scene_id,
                        'index': index,
                        'chapter': 1,  # 向后兼容
                        'scene': 1     # 向后兼容
                    }
                elif len(match.groups()) == 3:
                    # 旧格式兼容（如果有）
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

    def organize_materials(self):
        """组织素材，按索引排序（新格式：取消分章，只按index排序）"""
        print("组织素材（按索引排序，不分章）...")

        # 收集所有素材到单一列表中
        all_materials = []

        # 处理音频文件
        for audio in self.audio_files:
            info = self.extract_scene_info(audio['name'])
            all_materials.append({
                'type': 'audio',
                'file': audio['name'],
                'id': audio['stem'],
                'index': info['index'],      # 索引
                'scene_id': info['scene_id'], # 场景ID（如 datiepu）
                'chapter': info['chapter'],  # 向后兼容
                'scene': info['scene'],      # 向后兼容
                'path': audio['path'],
                'is_audio': True
            })

        # 处理字幕文件
        for subtitle in self.subtitle_files:
            info = self.extract_scene_info(subtitle['name'])
            content = self.read_subtitle_file(subtitle['path'])
            if content:
                dialogues = self.parse_subtitle_content(content)
                all_materials.append({
                    'type': 'subtitle',
                    'file': subtitle['name'],
                    'index': info['index'],      # 索引
                    'scene_id': info['scene_id'], # 场景ID
                    'chapter': info['chapter'],  # 向后兼容
                    'scene': info['scene'],      # 向后兼容
                    'dialogues': dialogues,
                    'is_subtitle': True
                })

        # 按索引排序
        all_materials.sort(key=lambda x: x['index'])

        # 如果没有素材，返回空字典
        if not all_materials:
            return {}

        # 不再分章，只返回一个'all'场景
        scenes = {
            'all': {
                'chapter': 1,
                'scene': 1,
                'materials': all_materials,
                'start_index': all_materials[0]['index'],
                'end_index': all_materials[-1]['index']
            }
        }

        print(f"组织完成：共 {len(all_materials)} 个素材，索引范围 {all_materials[0]['index']} - {all_materials[-1]['index']}")
        audio_count = sum(1 for m in all_materials if m.get('is_audio'))
        subtitle_count = sum(1 for m in all_materials if m.get('is_subtitle'))
        print(f"  音频: {audio_count}, 字幕: {subtitle_count}")

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
        music_dir.mkdir(exist_ok=True)    # 2. 创建目录

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

        print(f"复制完成: {len(self.audio_files)}音频, {len(self.scene_images)}场景, {len(self.character_images)}角色, {len(self.music_files)}背景音乐")
    def copy_assets_for_chapter(self, chapter_data, node_id=None):
        """复制本章素材（过滤音频、字幕和背景音乐，角色和场景图片全部复制）"""
        print("复制本章素材文件...")

        assets_dir = self.output_dir / "assets"
        voices_dir = assets_dir / "voices"
        scenes_dir = assets_dir / "scenes"
        characters_dir = assets_dir / "characters"
        music_dir = assets_dir / "music"

        voices_dir.mkdir(exist_ok=True)
        scenes_dir.mkdir(exist_ok=True)
        characters_dir.mkdir(exist_ok=True)
        music_dir.mkdir(exist_ok=True)

        # 收集本章用到的音频和字幕文件名
        chapter_materials = chapter_data.get('materials', [])
        chapter_audio_files = set()
        chapter_subtitle_files = set()

        for material in chapter_materials:
            if material.get('is_audio'):
                chapter_audio_files.add(material['file'])
            elif material.get('is_subtitle'):
                chapter_subtitle_files.add(material['file'])

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

        # 复制所有场景图片
        scene_count = 0
        for scene in self.scene_images:
            shutil.copy2(scene['path'], scenes_dir / scene['name'])
            scene_count += 1

        # 复制所有角色图片
        character_count = 0
        for char in self.character_images:
            shutil.copy2(char['path'], characters_dir / char['name'])
            character_count += 1

        # 【新增】复制本章需要的背景音乐
        # 逻辑：只要音乐范围 [music.start, music.end] 与本章范围 [chap_start, chap_end] 有重叠，就复制
        music_count = 0
        for music in self.music_files:
            if max(music['start'], chap_start) <= min(music['end'], chap_end):
                shutil.copy2(music['path'], music_dir / music['name'])
                music_count += 1

        # 【新增】复制自定义章节背景图片（替换beijing.png）
        bg_count = 0
        if node_id:
            bg_source = Path("outputs") / "images" / "background" / f"{node_id}.png"
            if bg_source.exists():
                images_dir = assets_dir / "images"
                images_dir.mkdir(exist_ok=True)
                shutil.copy2(bg_source, images_dir / "beijing.png")
                bg_count = 1

        print(f"复制完成: {audio_count}音频, {subtitle_count}字幕, {scene_count}场景, {character_count}角色, {music_count}背景音乐, {bg_count}自定义背景")

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

            lines.append(f"    '{char_id}': {{")
            lines.append(f"        name: '{chinese_name}',")
            lines.append(f"        color: '{color}',")

            if image:
                lines.append(f"        sprites: {{")
                lines.append(f"            normal: '{image}'")
                lines.append(f"        }},")
                lines.append(f"        expressions: {{")
                lines.append(f"            normal: '{image}'")
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

    def generate_asset_declarations(self):
        """生成资源声明"""
        print("生成资源声明...")

        lines = []

        # 场景资源
        lines.append("// 场景资源")
        lines.append("monogatari.assets('scenes', {")
        for scene in self.scene_images:
            lines.append(f"    '{scene['stem']}': '{scene['name']}',")
        lines.append("});")
        lines.append("")

        # 音频资源
        lines.append("// 音频资源")
        lines.append("monogatari.assets('voices', {")
        for audio in self.audio_files:
            lines.append(f"    '{audio['stem']}': '{audio['name']}',")
        lines.append("});")
        lines.append("")

        # 背景音乐资源
        lines.append("// 背景音乐资源")
        lines.append("monogatari.assets('music', {")
        for music in self.music_files:
            lines.append(f"    '{music['stem']}': '{music['name']}',")
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
    def generate_script_for_chapter(self, chapter_data, node_data=None, folder_mapping=None):
        """生成本章游戏脚本（音频字幕同步）"""
        # 使用 node_data 的 depth 作为 chapter_id，保持与 main.js 一致
        chap_id = str(node_data.get('depth', 1)) if node_data else '1'
        print(f"生成第 {chap_id} 章游戏脚本...")

        lines = []
        # 小游戏文案池
        clicker_labels = ["燃起斗志！", "给我破！", "绝不放弃！", "火力全开！", "突破极限！", "粉碎它！", "觉醒时刻！", "致命连打！", "极速充能！"]
        timing_labels = ["弱点锁定！", "会心一击！", "完美闪避！", "瞬息之间！", "屏息凝神！", "一击必杀！", "捕捉气机！", "精准格挡！", "就在此刻！"]
        memory_labels = ["解析阵法...", "洞察本源...", "回溯记忆...", "寻找破绽...", "推演战术...", "灵光一闪...", "破解机关...", "识别符文...", "冥想..."]
        lines.append(f"// 游戏脚本 - 第 {chap_id} 章")
        lines.append("monogatari.script({")

        # Start标签
        lines.append("    'Start': [")
        lines.append("        'show scene #f7f6f6 with fadeIn',")
        lines.append("        {'Function':{ 'Apply': () => { setFilter('#FFD700', 0.15); return true; }, 'Reverse': () => { setFilter(null); return true; } }},")

        scene_label = f"SceneChapter{chap_id}" if chap_id != 'all' else "Sceneall"
        lines.append(f"        'jump {scene_label}',")

        lines.append("    ],")
        lines.append("")

        # 场景
        lines.append(f"    // 第 {chap_id} 章素材按索引顺序播放")
        lines.append(f"    '{scene_label}': [")

        # 初始场景图片（默认背景）
        lines.append(f"        'show scene #f7f6f6 with fadeIn',")

        # 重置角色状态
        lines.append(
            f"        {{'Function':{{ 'Apply': () => {{ StageManager.reset(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

        materials = chapter_data.get('materials', [])
        if not materials:
            lines.append(f"        '本章无素材',")
            lines.append(f"        'end',")
            lines.append(f"    ],")
            lines.append("")
            lines.append("});")
            return "\n".join(lines)

        # 按索引分组音频和字幕
        audio_by_index = {}
        subtitle_by_index = {}
        for material in materials:
            idx = material.get('index', 0)
            if material.get('is_audio'):
                audio_by_index[idx] = material
            elif material.get('is_subtitle'):
                subtitle_by_index[idx] = material

        # 创建配对并排序（只按索引）
        audio_subtitle_pairs = []
        for idx in sorted(set(list(audio_by_index.keys()) + list(subtitle_by_index.keys()))):
            audio = audio_by_index.get(idx)
            subtitle = subtitle_by_index.get(idx)
            audio_subtitle_pairs.append({
                'audio': audio,
                'subtitle': subtitle,
                'index': idx,
                'scene_id': audio.get('scene_id') if audio else subtitle.get('scene_id') if subtitle else 'default',
                'chapter': audio.get('chapter') if audio else subtitle.get('chapter') if subtitle else 1,
                'scene': audio.get('scene') if audio else subtitle.get('scene') if subtitle else 1
            })

        # 获取总步数和节点ID用于进度上报
        total_steps = len(audio_subtitle_pairs)
        node_id = node_data.get('id', '') if node_data else ''
        if not node_id and folder_mapping:
            node_id = folder_mapping.get(chap_id, '')

        # 背景音乐排序
        sorted_music = sorted(self.music_files, key=lambda x: x['start'])

        # 跟踪当前状态
        current_scene_image = None  # 当前显示的场景图片ID
        current_bgm = None  # 当前播放的背景音乐

        # 初始化进度上报系统和错误上报系统
        lines.append(f"        {{'Function':{{ 'Apply': () => {{ window.GameProgressReporter.init({total_steps}, '{chap_id}', '{node_id}'); window.GameProgressReporter.startAutoSave(); window.ErrorReporter.init('{chap_id}', '{node_id}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

        # 按索引顺序生成脚本
        for pair in audio_subtitle_pairs:
            audio = pair['audio']
            subtitle = pair['subtitle']
            chapter = pair['chapter']
            scene = pair['scene']
            idx = pair['index']

            # 0. 背景音乐处理
            # 检查是否需要停止当前背景音乐
            if current_bgm is not None and idx > current_bgm['end']:
                bgm_stem = current_bgm['stem']
                lines.append(f"        'stop music {bgm_stem} with fade 3',")
                current_bgm = None

            # 检查是否需要开始新的背景音乐
            if current_bgm is None:
                for music in sorted_music:
                    if music['start'] <= idx <= music['end']:
                        music_stem = music['stem']
                        lines.append(f"        'play music {music_stem} with fade 3 loop',")
                        current_bgm = music
                        break

            # 1. 场景图片处理
            # 获取当前素材的真实场景ID
            current_scene_id = audio.get('scene_id') if audio else subtitle.get('scene_id') if subtitle else 'default'

            # 优先使用素材自带的场景ID来匹配场景图片
            scene_image_id = current_scene_id
            has_scene_image = any(img['stem'] == scene_image_id for img in self.scene_images)

            if has_scene_image and scene_image_id != current_scene_image:
                lines.append(f"        'show scene {scene_image_id} with fadeIn',")
                lines.append(f"        {{'Function':{{ 'Apply': () => {{ startBackgroundMotion(); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                lines.append(f"        'wait 800',")
                current_scene_image = scene_image_id
            elif not has_scene_image and current_scene_image is None:
                lines.append(f"        'show scene #f7f6f6 with fadeIn',")
                lines.append(f"        'wait 500',")
                current_scene_image = "#f7f6f6"

            # 2. 对话处理
            if subtitle and subtitle.get('dialogues'):
                for dialogue in subtitle['dialogues']:
                    speaker = dialogue['speaker']
                    raw_key = dialogue.get('raw_key')
                    text = dialogue['text']

                    # ================= 【新增逻辑 1：解析并剥离小游戏标记】 =================
                    game_trigger = None
                    # 检查并移除标记，防止显示在屏幕上
                    if "(QTE)" in text or "（QTE）" in text:
                        text = text.replace("(QTE)", "").replace("（QTE）", "")
                        game_trigger = "clicker"
                    elif "(TIMING)" in text or "（TIMING）" in text:
                        text = text.replace("(TIMING)", "").replace("（TIMING）", "")
                        game_trigger = "timing"
                    elif "(MEMORY)" in text or "（MEMORY）" in text:
                        text = text.replace("(MEMORY)", "").replace("（MEMORY）", "")
                        game_trigger = "memory"

                    # ================= 【新增逻辑 2：解析并剥离氛围选择（增强正则）】 =================
                    # ================= 【修改逻辑：强力剥离氛围选择】 =================
                    import re
                    interaction_options = []

                    # 强力正则：不再纠结结尾括号，直接匹配 INTERACTION 及其后面的所有内容直到结尾
                    # 逻辑：匹配 (可选括号/空格) + INTERACTION + (冒号) + (剩余所有内容)
                    interaction_pattern = r'(?:[\(\[（\s]*)INTERACTION\s*[:：]\s*(.*)$'

                    # 使用 re.DOTALL 确保能匹配换行符，re.IGNORECASE 忽略大小写
                    interaction_match = re.search(interaction_pattern, text, re.DOTALL | re.IGNORECASE)

                    if interaction_match:
                        raw_interaction = interaction_match.group(1)

                        # 【核心】从原字幕中彻底删除匹配到的整段内容
                        # 使用 group(0) 确保删除的是正则抓到的确切字符串
                        text = text.replace(interaction_match.group(0), "")

                        # 提取选项：支持 中文引号“”、英文引号""
                        # 因为是贪婪匹配，raw_interaction 可能包含结尾的 '))'，但这不影响 findall 提取引号内的内容
                        opts = re.findall(r'["“](.*?)["”]', raw_interaction)

                        # 兜底：如果没找到引号，尝试按逗号或竖线分割
                        if not opts and raw_interaction:
                            # 先去掉可能的结尾括号，避免干扰
                            clean_raw = re.sub(r'[\)\)\]）]*\s*$', '', raw_interaction)
                            opts = [x.strip() for x in re.split(r'[,，]', clean_raw) if x.strip()]

                        for opt in opts:
                            if '|' in opt:
                                btn, feedback = opt.split('|', 1)
                            else:
                                btn, feedback = opt, ""
                            interaction_options.append((btn.strip(), feedback.strip()))
                    # ================= 【原有清理逻辑】 =================
                    # 清理完标记后，再做常规清理
                    text_clean = text.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"").replace("`", "\\`").replace("\n", " ").replace("\r", "").strip()

                    # 1. 插入小游戏代码（放在对话之前触发）
                    # 1. 插入小游戏代码（输赢均有反馈版）
                    if game_trigger:
                        import json

                        # === 文案池定义 ===
                        # 游戏标题池
                        clicker_pool = ["燃起斗志！", "给我破！", "绝不放弃！", "火力全开！", "突破极限！", "粉碎它！",
                                        "觉醒时刻！", "致命连打！", "极速充能！"]
                        timing_pool = ["弱点锁定！", "会心一击！", "完美闪避！", "瞬息之间！", "屏息凝神！", "一击必杀！",
                                       "捕捉气机！", "精准格挡！", "就在此刻！"]
                        memory_pool = ["解析阵法...", "洞察本源...", "回溯记忆...", "寻找破绽...", "推演战术...",
                                       "灵光一闪...", "破解机关...", "识别符文...", "冥想..."]

                        # 成功反馈池 (新增)
                        success_msgs = [
                            "完美！", "魂力涌动！", "精彩的操作！", "势如破竹！",
                            "毫无悬念！", "神乎其技！", "轻松拿捏！", "意念合一！"
                        ]

                        # 失败反馈池
                        fail_msgs = [
                            "气息紊乱...", "差了一点...", "大意了...", "节奏乱了...",
                            "功亏一篑...", "失误...", "精神无法集中...", "被干扰了..."
                        ]

                        # 序列化为 JS 数组
                        js_success_msgs = json.dumps(success_msgs, ensure_ascii=False)
                        js_fail_msgs = json.dumps(fail_msgs, ensure_ascii=False)

                        lines.append(f"        {{'Function':{{ 'Apply': () => {{ ")

                        # 生成游戏调用代码
                        if game_trigger == "clicker":
                            label = random.choice(clicker_pool)
                            lines.append(
                                f"            return window.MiniGame.startClicker(8, 3, '{label}').then(success => {{")

                        elif game_trigger == "timing":
                            label = random.choice(timing_pool)
                            lines.append(
                                f"            return window.MiniGame.startTiming(2.5, 20, '{label}').then(success => {{")

                        elif game_trigger == "memory":
                            label = random.choice(memory_pool)
                            lines.append(
                                f"            return window.MiniGame.startMemory(4, '{label}').then(success => {{")

                        # === 统一结算逻辑 ===
                        # 1. 成功处理
                        lines.append(f"                if (success) {{")
                        lines.append(f"                    const wins = {js_success_msgs};")
                        lines.append(f"                    const msg = wins[Math.floor(Math.random() * wins.length)];")
                        lines.append(f"                    window.showToast(msg);")
                        lines.append(f"                    return true;")
                        lines.append(f"                }}")

                        # 2. 失败处理 (不卡关)
                        lines.append(f"                const fails = {js_fail_msgs};")
                        lines.append(f"                const msg = fails[Math.floor(Math.random() * fails.length)];")
                        lines.append(f"                window.showToast(msg);")
                        lines.append(f"                return true;")

                        lines.append(f"            }});")

                        lines.append(f"        }}, 'Reverse': () => {{ window.MiniGame.cleanup(); return true; }} }} }},")
                    # 2. 插入换图/更新立绘逻辑
                    specific_image = None
                    if raw_key and hasattr(self, 'valid_image_stems') and raw_key in self.valid_image_stems:
                        specific_image = raw_key + ".png"

                    if specific_image:
                        lines.append(f"        {{'Function':{{ 'Apply': () => {{ StageManager.forceImage('{speaker}', '{specific_image}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")
                    else:
                        lines.append(f"        {{'Function':{{ 'Apply': () => {{ updateStage('{speaker}'); return true; }}, 'Reverse': () => {{ return true; }} }} }},")

                    # 3. 播放音频
                    if audio:
                        audio_id = audio['id']
                        lines.append(f"        'play voice {audio_id}',")

                    # 4. 显示字幕 (及等待)
                    char_id_for_text = self.chinese_to_id.get(speaker, 'nb')
                    if speaker == '旁白': char_id_for_text = 'nb'

                    wait_time = 500
                    lines.append(f"        'wait {wait_time}',")

                    # 只有当清理后的文本不为空时才显示（避免显示空的气泡）
                    if text_clean:
                        if char_id_for_text == 'nb':
                            lines.append(f"        '{text_clean}',")
                        else:
                            lines.append(f"        '{char_id_for_text} {text_clean}',")

                    # 5. 插入氛围选择 (在字幕显示之后)
                    if interaction_options:
                        lines.append(f"        {{")
                        lines.append(f"            'Choice': {{")
                        for idx, (btn, feedback) in enumerate(interaction_options):
                            lines.append(f"                'Flavor_{idx}': {{")
                            lines.append(f"                    'Text': '{btn}',")
                            # ✅ 修改点：调用 window.showToast 而不是 monogatari.run
                            lines.append(f"                    'Do': () => {{ window.showToast('{feedback}'); }},")
                            lines.append(f"                }},")
                        lines.append(f"            }}")
                        lines.append(f"        }},")

            else:
                # 纯音频
                if audio:
                    lines.append(f"        'play voice {audio['id']}',")
                    lines.append(f"        'wait 500',")

            # 更新进度
            lines.append(f"        {{'Function':{{ 'Apply': () => {{ window.GameProgressReporter.update({idx}); return true; }}, 'Reverse': () => {{ window.GameProgressReporter.update({idx}); return true; }} }} }},")

        # ================= 【修改点:结尾跳转逻辑】 =================
        lines.append(f"        '本章结束',")

        # 检查是否有后续节点
        if node_data and node_data.get('children') and folder_mapping:
            lines.append(f"        '请做出选择：',")
            lines.append(f"        {{")
            lines.append(f"            'Choice': {{")

            for child in node_data['children']:
                choice_text = child['choice_text']
                child_node_id = child['child_id']

                # 从映射表中查找目标文件夹名
                target_folder = folder_mapping.get(child_node_id)

                if target_folder:
                    lines.append(f"                '{child_node_id}': {{")
                    lines.append(f"                    'Text': '{choice_text}',")
                    # 生成跳转钩子
                    # 新代码（生成 JS 箭头函数，Monogatari 会直接执行它）
                    # 注意：在 f-string 中，JS 的大括号 { } 需要写成 {{ }} 来转义
                    lines.append(f"                    'Do': () => {{ window.StoryNavigator.go('{target_folder}'); }},")
                    lines.append(f"                }},")
                else:
                    print(f"警告：找不到子节点 {child_node_id} 的文件夹映射")

            lines.append(f"            }}")
            lines.append(f"        }},")
            # Choice 之后通常不需要 end，因为 Do 会处理跳转
            # 但加一个 end 作为兜底
            lines.append(f"        'end',")
        else:
            # 没有子节点，或者缺少映射表，则是大结局
            lines.append(f"        '全剧终',")
            lines.append(f"        'end',")

        # ==========================================================

        lines.append(f"    ],")
        lines.append("")

        lines.append("});")

        return "\n".join(lines)

    def generate_visual_styles(self):
        """生成视觉特效所需的CSS（调整版：人物下沉）"""
        return """
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
"""

    def generate_utility_code(self, chapter_data=None, node_id='', depth=1):
        """生成工具代码（修复版：修复 startMemory 中的语法错误）"""
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
}});

// ==================== 路由导航器 ====================
window.StoryNavigator = {{
    pendingJumpUrl: null,  // 保存待跳转的 URL

    go: function(targetFolderName) {{
        const targetUrl = "/" + targetFolderName + "/";

        // 保存目标 URL
        this.pendingJumpUrl = targetUrl;

        // 设置自动开始标记（用于无感跳转）
        sessionStorage.setItem('autostart', '1');

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
            // Iframe 模式：发送消息并等待回信
            window.parent.postMessage(requestMsg, '*');
        }} else {{
            // 独立调试模式：直接跳转
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

// ==================== 保存/加载管理器 ====================
window.SaveLoadManager = {{
    // 拦截保存操作，自动同步到 parent
    interceptSave: function() {{
        // 检查 monogatari.save 是否存在
        if (!monogatari || typeof monogatari.save !== 'function') {{
            console.warn('monogatari.save not available yet, skipping save interception');
            return;
        }}

        const originalSave = monogatari.save.bind(monogatari);
        monogatari.save = function(saveName) {{
            // 检查存档数量限制（最多3个）
            const allSaves = monogatari.saves();
            const saveNames = Object.keys(allSaves);

            if (saveNames.length >= 3 && !saveNames.includes(saveName)) {{
                // 显示警告
                alert('最多只能保存2个存档，请先删除旧存档');

                // 强制刷新存档界面，清除可能创建的空白槽位
                setTimeout(() => {{
                    const saveScreen = document.querySelector('save-screen');
                    if (saveScreen && saveScreen._refresh) {{
                        saveScreen._refresh();
                    }}
                    // 或者强制重新渲染
                    const event = new CustomEvent('save-screen-refresh');
                    document.dispatchEvent(event);
                }}, 100);

                // 抛出异常以阻止保存
                throw new Error('Save limit reached: Maximum 3 slots allowed.');
            }}

            // 执行原始保存
            const result = originalSave(saveName);

            // 保存成功后，自动发送所有存档到 parent
            if (result) {{
                window.SaveLoadManager.sendSavesToParent();
            }}

            return result;
        }};
    }},

    // 发送所有存档信息到 parent
    sendSavesToParent: function() {{
        try {{
            const allSaves = monogatari.saves();
            const saveNames = Object.keys(allSaves);
            const saveData = [];

            for (const saveName of saveNames) {{
                const saveInfo = allSaves[saveName];
                if (saveInfo) {{
                    saveData.push({{
                        name: saveName,
                        data: saveInfo.data,
                        date: saveInfo.date,
                        screenshot: saveInfo.screenshot,
                        timestamp: Date.now()
                    }});
                }}
            }}

            const message = {{
                type: "SAVE_DATA_UPDATE",
                data: {{
                    chapter_id: "{chap_id}",
                    node_id: "{node_id}",
                    saves: saveData,
                    timestamp: Date.now()
                }}
            }};

            if (window.parent && window.parent !== window) {{
                window.parent.postMessage(message, '*');
                console.log("📤 发送存档信息:", message);
            }}
        }} catch (e) {{
            console.error("发送存档信息失败:", e);
        }}
    }},

    // 监听加载请求
    listenForLoad: function() {{
        window.addEventListener('message', function(event) {{
            if (event.data && event.data.type === "LOAD_SAVE_DATA") {{
                try {{
                    console.log("📥 收到加载请求:", event.data);
                    const saveData = event.data.data;

                    // 如果数据为空或没有存档，跳过加载
                    if (!saveData || !saveData.saves || saveData.saves.length === 0) {{
                        console.log("⚠️ 没有存档数据，跳过加载");
                        return;
                    }}

                    // 清空现有存档
                    const allSaves = monogatari.saves();
                    const existingSaveNames = Object.keys(allSaves);
                    for (const saveName of existingSaveNames) {{
                        monogatari.deleteSave(saveName);
                    }}

                    // 加载新存档
                    for (const save of saveData.saves) {{
                        if (save.name && save.data) {{
                            monogatari.saveData(save.name, save.data);
                        }}
                    }}

                    window.showToast("存档加载成功");
                    console.log("✅ 存档加载完成");
                }} catch (e) {{
                    console.error("加载存档失败:", e);
                    window.showToast("加载失败");
                }}
            }}
        }});
    }},

    init: function() {{
        this.interceptSave();
        this.listenForLoad();
        console.log("SaveLoadManager initialized");
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

                    // 如果有进度数据，更新游戏进度报告器
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
        // 延迟500ms后请求存档（确保进度请求先处理）
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
        this.current = index;
        const pct = (index / this.total) * 100;
        const fillEl = document.getElementById('game-progress-fill');
        if (fillEl) {{
            fillEl.style.width = pct + '%';
        }}
    }},

    sendReport: function(triggerSource) {{
        const msg = {{
            "type": "GAME_PROGRESS_UPDATE",
            "data": {{
                "chapter_id": this.chapId,
                "node_id": this.nodeId,
                "progress_percent": parseFloat(((this.current / this.total) * 100).toFixed(2)),
                "current_index": this.current,
                "total_count": this.total,
                "timestamp": Date.now(),
                "trigger_source": triggerSource
            }}
        }};
        window.parent.postMessage(msg, '*');
        console.log('Progress Sent:', triggerSource, msg);
    }},

    startAutoSave: function() {{
        // 每30秒自动上报进度
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
    startClicker: function(target=8, time=3, label="爆发！") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // 【记录】保存句柄

            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">连点挑战</div>
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
    startTiming: function(speed=2, difficulty=20, label="精准一击") {{
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
                    <div class="qte-btn" id="t-btn" style="width:80px;height:80px;font-size:1rem">锁定</div>
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
                btn.innerText = success ? '成功' : '失败';
                setTimeout(() => this.closeOverlay(overlay, success, resolve), 800);
            }};
        }});
    }},

    // 3. 记忆阵法
    startMemory: function(count=4, label="破解阵法") {{
        return new Promise(resolve => {{
            this.activeResolve = resolve; // 【记录】保存句柄

            const overlay = this.createOverlay(`
                <div class="qte-container">
                    <div class="qte-title">${{label}}</div>
                    <div class="rune-grid" id="rune-grid">
                        ${{Array(9).fill(0).map((_,i)=>`<div class="rune-cell" data-i="${{i}}"></div>`).join('')}}
                    </div>
                    <div id="mem-msg" style="margin-top:20px">观察顺序...</div>
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
                if (overlay.querySelector('#mem-msg')) overlay.querySelector('#mem-msg').innerText = "按顺序点击";
            }};
            setTimeout(playSeq, 500);

            cells.forEach(c => c.onclick = () => {{
                if(overlay.querySelector('#mem-msg').innerText !== "按顺序点击") return;
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

const characterIdMap = {character_id_mapping_js};
const characterFileMap = {character_file_mapping_js};

// ==================== 核心：舞台管理器 (StageManager) ====================
const StageManager = {{
    leftChar: null,
    rightChar: null,
    nextReplace: 'left',

    reset() {{
        this.leftChar = null;
        this.rightChar = null;
        this.nextReplace = 'left';

        document.querySelectorAll('.manual-character-container').forEach(el => el.remove());

        const visuals = document.querySelector('game-screen [data-content="visuals"]');
        if (visuals) {{
            const nativeChars = visuals.querySelectorAll('[data-character]');
            nativeChars.forEach(el => el.remove());
        }}
    }},

    async forceImage(speakerName, imageName) {{
        let charId = null;
        if (speakerName && speakerName !== '旁白') {{
            charId = characterIdMap[speakerName] || speakerName;
        }}
        if (!charId) return;

        characterFileMap[charId] = imageName;

        const container = document.querySelector(`.manual-character-container[data-character="${{charId}}"]`);
        if (container) {{
            const img = container.querySelector('img');
            if (img) {{
                img.src = `assets/characters/${{imageName}}`;
            }}
        }}

        await this.update(charId);
    }},

    async update(speakerId) {{
        if (!speakerId || speakerId === 'nb' || speakerId === '旁白') {{
            this.setVisualState(this.leftChar, 'dimmed');
            this.setVisualState(this.rightChar, 'dimmed');
            return;
        }}

        if (speakerId === this.leftChar) {{
            if (!this.getCharImage(speakerId)) {{
                await this.addCharacter(speakerId, 'left');
            }}

            this.setVisualState(this.leftChar, 'active');
            this.setVisualState(this.rightChar, 'inactive');
            return;
        }}

        if (speakerId === this.rightChar) {{
            if (!this.getCharImage(speakerId)) {{
                await this.addCharacter(speakerId, 'right');
            }}

            this.setVisualState(this.rightChar, 'active');
            this.setVisualState(this.leftChar, 'inactive');
            return;
        }}

        let targetSlot = '';
        if (!this.leftChar) {{
            targetSlot = 'left';
        }} else if (!this.rightChar) {{
            targetSlot = 'right';
        }} else {{
            targetSlot = this.nextReplace;
            const charToRemove = (targetSlot === 'left') ? this.leftChar : this.rightChar;
            await this.removeCharacter(charToRemove);
            this.nextReplace = (targetSlot === 'left') ? 'right' : 'left';
        }}

        await this.addCharacter(speakerId, targetSlot);

        if (targetSlot === 'left') {{
            this.leftChar = speakerId;
            this.setVisualState(this.leftChar, 'active');
            this.setVisualState(this.rightChar, 'inactive');
        }} else {{
            this.rightChar = speakerId;
            this.setVisualState(this.rightChar, 'active');
            this.setVisualState(this.leftChar, 'inactive');
        }}
    }},

    setVisualState(charId, state) {{
        if (!charId) return;
        const img = this.getCharImage(charId);
        if (!img) return;

        img.classList.remove('state-active', 'state-inactive', 'state-dimmed');
        if (state === 'active') img.classList.add('state-active');
        if (state === 'inactive') img.classList.add('state-inactive');
        if (state === 'dimmed') img.classList.add('state-dimmed');
    }},

    addCharacter(charId, slot) {{
        return new Promise(resolve => {{
            const filename = characterFileMap[charId];
            if (!filename) {{
                console.error(`[StageManager] 错误：找不到ID为 ${{charId}} 的文件名`);
                resolve();
                return;
            }}

            let container = document.querySelector(`.manual-character-container[data-character="${{charId}}"]`);

            if (!container) {{
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
                img.src = `assets/characters/${{filename}}`;
                img.className = 'character-image';

                if (slot === 'left') img.classList.add('slot-left');
                else img.classList.add('slot-right');

                img.classList.add('state-hidden');
                container.appendChild(img);

                const visualsLayer = document.querySelector('game-screen [data-content="visuals"]');
                const gameScreen = document.querySelector('game-screen');

                if (visualsLayer) {{
                    visualsLayer.appendChild(container);
                    setTimeout(() => {{ img.classList.remove('state-hidden'); }}, 50);
                }} else if (gameScreen) {{
                    console.warn('[StageManager] 未找到 visuals 层，插入到 game-screen');
                    gameScreen.insertBefore(container, gameScreen.firstChild);
                    setTimeout(() => {{ img.classList.remove('state-hidden'); }}, 50);
                }} else {{
                    console.error('[StageManager] 致命错误：找不到游戏容器');
                }}
            }}
            resolve();
        }});
    }},

    removeCharacter(charId) {{
        return new Promise(resolve => {{
            if (!charId) {{ resolve(); return; }}
            const container = document.querySelector(`.manual-character-container[data-character="${{charId}}"]`);
            if (container) {{
                const img = container.querySelector('img');
                if (img) img.classList.add('state-hidden');
                setTimeout(() => {{ container.remove(); resolve(); }}, 600);
            }} else {{
                resolve();
            }}
        }});
    }},

    getCharImage(charId) {{
        const container = document.querySelector(`.manual-character-container[data-character="${{charId}}"]`);
        return container ? container.querySelector('img') : null;
    }}
}};

function updateStage(speakerName) {{
    let charId = null;
    if (speakerName && speakerName !== '旁白' && speakerName !== 'nb') {{
        charId = characterIdMap[speakerName] || speakerName;
    }}
    return StageManager.update(charId);
}}

function setFilter(color = null, opacity = 0.3) {{
    const overlay = document.getElementById('cinematic-overlay');
    if (!overlay) return;
    if (!color) {{ overlay.style.display = 'none'; }}
    else {{
        overlay.style.display = 'block';
        overlay.style.background = color;
        overlay.style.opacity = opacity;
    }}
    return Promise.resolve();
}}
function startBackgroundMotion() {{
    setTimeout(() => {{
        const bg = document.querySelector('#background');
        if (bg) bg.classList.add('bg-pan-zoom');
    }}, 100);
    return Promise.resolve();
}}
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
    #                         alert('最多只能保存2个存档，请先删除旧存档');
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
                story_tree = json.load(f)

            root_id = story_tree.get('root_id', 'chapter_1')
            nodes = story_tree.get('nodes', {})

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
                    images_base_dir=self.images_base_dir
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

                    # 复制素材
                    chapter_data = scenes.get('all', {})
                    node_converter.copy_assets_for_chapter(chapter_data, node_id)

                    # 生成脚本文件（传递node_data和folder_mapping）
                    script_file = node_converter.output_dir / "js" / "script.js"

                    script_content = "/* global monogatari */\n\n"
                    script_content += node_converter.generate_character_definitions()
                    script_content += node_converter.generate_asset_declarations()
                    script_content += "\n".join(node_converter.generate_utility_code(chapter_data, node_id, depth))
                    # 调用修改后的generate_script_for_chapter，传入node_data和folder_mapping
                    script_content += node_converter.generate_script_for_chapter(chapter_data, node_data, folder_mapping)

                    with open(script_file, 'w', encoding='utf-8') as f:
                        f.write(script_content)

                    # 更新main.js（使用字符串格式化直接写入chapter_id和node_id）
                    main_js_content = """'use strict';
                /* global Monogatari */

                const {{ $_ready, $_ }} = Monogatari;

                $_ready (() => {{
                    // 拦截 localStorage 来限制存档数量
                    const originalSetItem = localStorage.setItem.bind(localStorage);
                    localStorage.setItem = function(key, value) {{
                        // 检查是否是保存游戏存档（包含 _Save_ 或 _AutoSave_）
                        if (key.includes('_Save_') || key.includes('_AutoSave_')) {{
                            // 获取所有存档
                            const saveKeys = Object.keys(localStorage).filter(k => k.includes('_Save_') || k.includes('_AutoSave_'));

                            // 如果已有3个存档且当前key不在已有存档中，则阻止保存
                            if (saveKeys.length >= 3 && !saveKeys.includes(key)) {{
                                alert('最多只能保存2个存档，请先删除旧存档');
                                throw new Error('Save limit reached: Maximum 3 slots allowed.');
                            }}
                        }}
                        return originalSetItem(key, value);
                    }};

                    // 监控 save-screen，自动删除超过3个的槽位
                    const observer = new MutationObserver(() => {{
                        const saveScreen = document.querySelector('save-screen');
                        if (saveScreen) {{
                            const slots = saveScreen.querySelectorAll('[data-save], .save-slot, [data-slot]');
                            if (slots.length > 3) {{
                                for (let i = 3; i < slots.length; i++) {{
                                    slots[i].remove();
                                }}
                            }}
                        }}
                    }});
                    observer.observe(document.body, {{ childList: true, subtree: true }});

                    // 页面加载完成后初始化游戏
                    monogatari.init ('#monogatari').then (() => {{
                        console.log('视觉小说已启动 - 音频字幕同步版');

                        // 初始化保存/加载管理器
                        if (window.SaveLoadManager) {{
                            window.SaveLoadManager.init();
                        }}

                        // 设置音频自动播放
                        monogatari.storage({{
                            settings: {{
                                autoAdvance: false,  // 关闭自动前进，确保click命令有效
                                voiceVolume: 100,
                                musicVolume: 15,     // <--- 在这里修改数值 (0-100)，建议 30-50 之间
                                skipVoice: true  // 允许跳过音频
                            }}
                        }});

                        // 监听来自 parent 的初始进度消息
                        window.addEventListener('message', function(event) {{
                            if (event.data && event.data.type === "INITIAL_PROGRESS") {{
                                const progressData = event.data.data;
                                console.log("📥 收到初始进度:", progressData);

                                // 修改开始按钮文本
                                const startButton = document.querySelector('[data-action="start"]');
                                if (startButton) {{
                                    // 如果没有进度数据或 current_index 为空，默认显示"开始"
                                    if (!progressData || !progressData.current_index || progressData.current_index === 1) {{
                                        startButton.textContent = "开始";
                                    }} else {{
                                        startButton.textContent = "继续";
                                        // 保存进度信息，用于点击后跳转
                                        window._resumeIndex = progressData.current_index;
                                    }}
                                }}
                            }}
                        }});

                        // 请求初始进度
                        if (window.parent && window.parent !== window) {{
                            window.parent.postMessage({{
                                type: "REQUEST_INITIAL_PROGRESS",
                                data: {{
                                    chapter_id: "{chapter_id}",
                                    node_id: "{node_id}",
                                    timestamp: Date.now()
                                }}
                            }}, '*');
                        }}

                        // 检查是否从跳转加载（通过URL参数或sessionStorage）
                        const urlParams = new URLSearchParams(window.location.search);
                        const fromJump = urlParams.get('autostart') === '1' || sessionStorage.getItem('autostart') === '1';

                        if (fromJump) {{
                            // 清除标记
                            sessionStorage.removeItem('autostart');

                            // 延迟自动点击开始按钮
                            setTimeout(() => {{
                                const startButton = document.querySelector('[data-action="start"]');
                                if (startButton) {{
                                    startButton.click();
                                    console.log('Auto-started game from jump');
                                }}
                            }}, 100);
                        }}
                    }});
                }});
                """.format(chapter_id=depth, node_id=node_id)
                    main_js_file = node_converter.output_dir / "js" / "main.js"

                    with open(main_js_file, 'w', encoding='utf-8') as f:
                        f.write(main_js_content)

                    # 创建运行脚本
                    node_converter.create_run_scripts()

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
        """生成Nginx配置文件，支持多章节动态路由及API反向代理"""
        print("生成Nginx配置文件...")

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

        # 构建location块
        location_blocks = []
        for folder in sorted(chapter_folders):
            location_blocks.append(f"""
        # 章节: {folder}
        location /{folder}/ {{
            # [前端握手模式] Nginx 不做拦截，由前端 JS 控制跳转
            index index.html;
            try_files $uri $uri/ /{folder}/index.html;

            # 静态资源缓存
            location ~* \\.(js|css|png|jpg|jpeg|gif|ico|mp3|ogg|wav)$ {{
                expires 1y;
                add_header Cache-Control "public, immutable";
            }}
        }}""")

        location_config = "\\n".join(location_blocks)

        nginx_conf = f"""# Nginx配置 - 视觉小说多章节部署
# 自动生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# 章节数量: {len(chapter_folders)}

events {{
    worker_connections 1024;
}}

http {{
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
    server {{
        listen 80;
        server_name localhost;
        root /usr/share/nginx/html;

        # 首页显示章节列表
        location / {{
            index index.html;
            try_files $uri $uri/ /index.html;

            # 如果不存在index.html，则显示目录列表（仅用于调试）
            autoindex off;
        }}

        # ==========================================
        # 后端 API 转发 (解决跨域问题)
        # ==========================================
        location /api/ {{
            # 假设后端服务名为 backend_server，端口 5000 (根据 docker-compose.yml 调整)
            # 如果是宿主机上的服务，也可以尝试 host.docker.internal
            proxy_pass http://backend_server:5000/;

            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

            # 支持 WebSocket (如果需要)
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }}

        # 章节路由配置
{location_config}

        # 全局静态资源缓存
        location ~* \\.(js|css|png|jpg|jpeg|gif|ico|mp3|ogg|wav)$ {{
            expires 1y;
            add_header Cache-Control "public, immutable";
        }}

        # 禁止访问隐藏文件
        location ~ /\\. {{
            deny all;
        }}

        # 错误页面
        error_page 404 /404.html;
        error_page 500 502 503 504 /50x.html;
    }}
}}
"""
        # 写入nginx.conf文件
        nginx_conf_path = self.output_dir / "nginx.conf"
        with open(nginx_conf_path, 'w', encoding='utf-8') as f:
            f.write(nginx_conf)
        print(f"Nginx配置已生成 (含API代理): {nginx_conf_path}")

        # 生成简单的首页（章节列表）
        self.generate_index_page(chapter_folders)

    def generate_index_page(self, chapter_folders):
        """生成章节列表首页"""
        index_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>视觉小说 - 章节列表</title>
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
        <h1>🎮 视觉小说 - 章节列表</h1>
        <p style="text-align: center; margin-bottom: 30px;">请选择要开始的章节：</p>

        <div class="chapter-list">
"""

        for folder in sorted(chapter_folders):
            # 尝试从章节文件夹中读取index.html获取章节标题
            chapter_title = folder.replace('_', ' ').title()
            # 可以改进：从章节文件夹的index.html中提取标题
            index_html += f"""            <a href="/{folder}/" class="chapter-card">
                <h3>{chapter_title}</h3>
                <p>点击开始游戏</p>
            </a>
"""

        index_html += """        </div>

        <div class="footer">
            <p>Powered by Monogatari.js | 部署时间: {deploy_time}</p>
        </div>
    </div>

    <script>
        // 自动从URL参数获取部署时间
        document.addEventListener('DOMContentLoaded', function() {{
            const deployTime = new Date().toLocaleString('zh-CN');
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
        print("\n" + "="*60)
        print("生成 Nginx 配置文件 (用于 Docker 挂载)...")
        print("="*60)

        # 调用核心生成逻辑
        self.generate_nginx_config()

        print("\n✅ Nginx 配置已生成！")
        print("现在的架构是：")
        print("  1. 此脚本生成游戏文件 + nginx.conf -> shared_data 目录")
        print("  2. Docker (Nginx 容器) 挂载 shared_data 目录读取内容")
        print("="*60)


def main():
    """主函数"""
    # 计算基于脚本位置的绝对路径
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent.parent  # 从 services/game_builder 向上两级到项目根目录

    source_dir = project_root / "storage" / "outputs"
    template_dir = script_dir / "game_template" / "shijuexiaoshuo"
    output_dir = project_root / "storage" / "built_games" / "shijuexiaoshuo-temp"

    converter = FinalConverter(
        source_dir=str(source_dir),
        template_dir=str(template_dir),
        output_dir=str(output_dir)
    )

    # 1. 转换所有节点生成游戏代码
    converter.create_per_node_projects()

    # 2. 只生成 Nginx 配置 (不再生成 Dockerfile)
    # 修正点：调用正确的方法名
    converter.generate_nginx_config_only()

if __name__ == "__main__":
    main()
