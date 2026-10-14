from pathlib import Path
import tempfile
import unittest

from services.game_builder.converter import FinalConverter, normalize_story_tree
from services.game_builder.converter_steam import FinalConverter as SteamFinalConverter


class VariableRoutingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "game_builder"
            / "converter.py"
        ).read_text(encoding="utf-8")
        cls.steam_source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "game_builder"
            / "converter_steam.py"
        ).read_text(encoding="utf-8")

    def test_variable_changes_are_clamped_to_zero_and_one_hundred(self):
        self.assertIn("Math.max(0, Math.min(100", self.source)

    def test_branch_ledger_tracks_positive_and_effective_choices(self):
        self.assertIn("window._playerVariableChoices", self.source)
        self.assertIn("positive", self.source)
        self.assertIn("effective", self.source)

    def test_ending_router_checks_normalized_ratio_margin_and_minimum_coverage(self):
        self.assertIn("ratioThreshold", self.source)
        self.assertIn("marginRatio", self.source)
        self.assertIn("minimumEffectiveChoices", self.source)
        self.assertIn("competitorTendency", self.source)
        self.assertIn("positive / effective", self.source)
        self.assertNotIn("maxVal - competitor >= requiredMargin", self.source)

    def test_each_parallel_ending_route_uses_an_independent_block_scope(self):
        loop_source = self.source.split("for child in conditional_endings:", 1)[1]
        loop_source = loop_source.split("if fallback_ending:", 1)[0]

        self.assertIn('lines.append(f"                    {{")', loop_source)
        self.assertIn('lines.append(f"                    }}")', loop_source)

    def test_choice_tendency_stats_are_carried_between_chapter_pages(self):
        self.assertIn("choice_stats=", self.source)
        self.assertIn("urlParams.get('choice_stats')", self.source)
        self.assertIn("_chapterEntryChoiceStats", self.source)

    def test_choice_tendency_stats_are_included_in_save_and_progress_state(self):
        self.assertIn("s.playerVariableChoices", self.source)
        self.assertIn("progressData.player_variable_choices", self.source)
        self.assertIn('"player_variable_choices": window._playerVariableChoices', self.source)

    def test_steam_builder_uses_the_same_choice_tendency_contract(self):
        self.assertIn("window._playerVariableChoices", self.steam_source)
        self.assertIn("positive", self.steam_source)
        self.assertIn("effective", self.steam_source)
        self.assertIn("choice_stats=", self.steam_source)
        self.assertIn("globalChoiceStats", self.steam_source)
        self.assertIn("ratioThreshold", self.steam_source)
        self.assertIn("marginRatio", self.steam_source)
        self.assertIn("minimumEffectiveChoices", self.steam_source)
        self.assertIn("competitorTendency", self.steam_source)

    def test_disabled_routes_are_ignored_by_both_builders(self):
        self.assertIn("routeEnabled", self.source)
        self.assertIn("routeEnabled", self.steam_source)

    def test_branch_marker_legacy_flags_are_parsed_by_both_builders(self):
        marker = (
            "<!-- effects_a={'self_worth': 4} effects_b={'self_worth': -4} "
            "legacy_flags_a={'signed_clarification': 1} legacy_flags_b={} -->"
        )

        for converter_cls in (FinalConverter, SteamFinalConverter):
            parsed = converter_cls._parse_branch_effects(None, marker)
            self.assertEqual({"signed_clarification": 1}, parsed["legacy_flags_a"])
            self.assertEqual({}, parsed["legacy_flags_b"])

    def test_old_branch_markers_remain_compatible_without_legacy_flags(self):
        marker = "<!-- effects_a={'courage': 3} effects_b={'courage': -3} -->"

        for converter_cls in (FinalConverter, SteamFinalConverter):
            parsed = converter_cls._parse_branch_effects(None, marker)
            self.assertEqual({}, parsed["legacy_flags_a"])
            self.assertEqual({}, parsed["legacy_flags_b"])

    def test_legacy_flags_are_reversible_and_required_by_ending_router(self):
        for source in (self.source, self.steam_source):
            self.assertIn("window._playerLegacyFlags", source)
            self.assertIn("requiredLegacyFlags", source)
            self.assertIn("legacyFlagsSatisfied", source)
            self.assertIn("legacy_flags=", source)

        self.assertIn("BranchEffectLedger.apply", self.source)
        self.assertIn("BranchEffectLedger.reverse", self.source)

    def test_legacy_flags_are_included_in_save_and_progress_state(self):
        self.assertIn("s.playerLegacyFlags", self.source)
        self.assertIn("progressData.player_legacy_flags", self.source)
        self.assertIn('"player_legacy_flags": window._playerLegacyFlags', self.source)
        self.assertIn("globalLegacyFlags", self.steam_source)

    def test_web_builder_notifies_parent_once_after_the_true_final_ending(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            task_id="task-123",
            enable_oss_upload=False,
        )
        final_script = converter.generate_script_for_chapter(
            {"materials": [], "beat_meta_by_index": {}},
            node_data={"id": "node_final", "depth": 8, "children": []},
            folder_mapping={"node_final": "chapter_8_node_final"},
            game_project="task-123",
        )

        self.assertIn("window.__gameCompletedSent", final_script)
        self.assertIn("type: 'GAME_COMPLETED'", final_script)
        self.assertIn("window.parent.postMessage", final_script)
        self.assertIn('task_id: "task-123"', final_script)
        self.assertIn('node_id: "node_final"', final_script)
        self.assertLess(
            final_script.index("type: 'GAME_COMPLETED'"),
            final_script.index("        'end',"),
        )

        non_final_script = converter.generate_script_for_chapter(
            {"materials": [], "beat_meta_by_index": {}},
            node_data={
                "id": "chapter_1",
                "depth": 1,
                "children": [{"child_id": "chapter_2", "choice_text": "Continue"}],
            },
            folder_mapping={"chapter_2": "chapter_2_chapter_2"},
            game_project="task-123",
        )
        self.assertNotIn("type: 'GAME_COMPLETED'", non_final_script)

    def test_web_builder_notifies_iframe_parent_once_when_chapter_client_is_ready(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            task_id="task-123",
            enable_oss_upload=False,
        )
        utility = "\n".join(
            converter.generate_utility_code(
                {}, "node-ready", 4, chapter_title="Ready Chapter"
            )
        )

        self.assertIn("_chapterReadySent: false", utility)
        self.assertIn("if (this._chapterReadySent) return false", utility)
        self.assertIn("type: 'CHAPTER_CLIENT_READY'", utility)
        self.assertIn("status: 'ready'", utility)
        self.assertIn("lifecycle: 'initialized'", utility)
        self.assertIn('task_id: "task-123"', utility)
        self.assertIn('chapter_id: "4"', utility)
        self.assertIn('node_id: "node-ready"', utility)
        self.assertIn("stats_enabled: !!window._enableStatsPanel", utility)
        self.assertIn("system_enabled: !!window._enableSystemPanel", utility)
        self.assertIn("window.parent.postMessage", utility)

        self.assertIn(
            "window.GameHostBridge.notifyChapterReady('monogatari_init_complete')",
            self.source,
        )

    def test_both_web_ui_modes_hide_the_visual_progress_bar(self):
        root = Path(__file__).resolve().parents[2]
        for ui_mode in ("Other", "Ancient"):
            converter = FinalConverter(
                source_dir=root,
                template_dir=root,
                output_dir=root,
                enable_oss_upload=False,
                ui_mode=ui_mode,
            )
            styles = converter.generate_visual_styles()
            progress_rule = styles.split("#game-progress-container {", 1)[1].split("}", 1)[0]
            self.assertIn("display: none !important;", progress_rule)

    def test_both_web_ui_modes_hide_main_quick_menu_hide_show_button(self):
        """Hide/Show belongs to Settings, never the in-game quick menu."""
        root = Path(__file__).resolve().parents[2]
        for ui_mode in ("Other", "Ancient"):
            converter = FinalConverter(
                source_dir=root,
                template_dir=root,
                output_dir=root,
                enable_oss_upload=False,
                ui_mode=ui_mode,
            )
            styles = converter.generate_visual_styles()
            mode = ui_mode.lower()
            self.assertIn(
                f"body.ui-mode-{mode} [data-component=\"quick-menu\"] [data-action=\"distraction-free\"]",
                styles,
            )
            self.assertIn(
                f"body.ui-mode-{mode} quick-menu [data-action=\"distraction-free\"]",
                styles,
            )

        # The Settings controls remain generated and are intentionally outside
        # the quick-menu selector.
        utility = "\n".join(
            FinalConverter(
                source_dir=root,
                template_dir=root,
                output_dir=root,
                enable_oss_upload=False,
                ui_mode="Other",
            ).generate_utility_code({}, "node-ui", 1)
        )
        # CSS covers first paint; the runtime guard covers Monogatari
        # quick-menu re-renders while its label switches Hide <-> Show.
        self.assertIn("hideMainQuickMenuDistractionFree: function()", utility)
        self.assertIn("button.classList.add('is-distraction-free-hidden')", utility)
        self.assertIn("this.hideMainQuickMenuDistractionFree();", utility)
        self.assertIn("[data-other-quick]", utility)
        self.assertIn("[data-ancient-quick]", utility)

    def test_nvl_keeps_quick_menu_controls_operable(self):
        """NVL hides the underlying ADV text, not Back/Skip controls."""
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        styles = converter.generate_visual_styles()
        hidden_block = styles.split(
            "/* NVL 激活时隐藏 ADV 对话框和名字", 1
        )[1].split("*/", 1)[1].split("/* NVL overlay is z-index", 1)[0]
        self.assertIn('body.nvl-active [data-component="text-box"]', hidden_block)
        self.assertIn('body.nvl-active [data-ui="who"]', hidden_block)
        self.assertNotIn('body.nvl-active [data-component="quick-menu"] {', hidden_block)
        self.assertIn("body.nvl-active quick-menu", styles)
        self.assertIn("pointer-events: auto !important", styles)
        self.assertIn("z-index: 9701 !important", styles)

    def test_skip_warning_places_confirm_before_cancel(self):
        """The destructive skip action is presented on the left as requested."""
        confirm = 'class=\\"skip-warning-confirm\\"'
        cancel = 'class=\\"skip-warning-cancel\\"'
        skip_warning = self.source.split("window.SkipController = {", 1)[1].split(
            "window.SkipController.install();", 1
        )[0]
        self.assertLess(skip_warning.index(confirm), skip_warning.index(cancel))

    def test_stats_panel_auto_opens_and_only_manual_actions_keep_it_closed(self):
        self.assertIn("window._statsDismissedByUser = false", self.source)
        self.assertIn("window._enableStatsPanel", self.source)
        self.assertIn(
            "if (statsPanel && gameActive && statsAvailable && !window._statsDismissedByUser)",
            self.source,
        )
        self.assertIn("statsPanel.classList.add('open')", self.source)
        self.assertIn("window._statsDismissedByUser = !shouldOpen", self.source)
        self.assertNotIn("window._openStatsWhenGameplayResumes", self.source)

    def test_both_web_ui_modes_stack_choices_at_the_sixty_five_percent_line(self):
        root = Path(__file__).resolve().parents[2]
        for ui_mode in ("Other", "Ancient"):
            converter = FinalConverter(
                source_dir=root,
                template_dir=root,
                output_dir=root,
                enable_oss_upload=False,
                ui_mode=ui_mode,
            )
            styles = converter.generate_visual_styles()
            choice_styles = styles.split(
                f'body.ui-mode-{ui_mode.lower()} choice-container [data-content="wrapper"]',
                1,
            )[1].split("}", 1)[0]
            self.assertIn("top: 65% !important;", choice_styles)
            self.assertIn("grid-template-columns: minmax(0, 1fr) !important;", choice_styles)

        ancient_styles = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
            ui_mode="Ancient",
        ).generate_visual_styles()
        ancient_choice_styles = ancient_styles.split(
            'body.ui-mode-ancient choice-container [data-content="wrapper"]',
            1,
        )[1].split("/* 设置页", 1)[0]
        self.assertNotIn("grid-template-columns: repeat(2, minmax(0, 1fr))", ancient_choice_styles)

    def test_both_web_ui_modes_use_taller_bottom_text_boxes(self):
        root = Path(__file__).resolve().parents[2]
        other_styles = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
            ui_mode="Other",
        ).generate_visual_styles()
        ancient_styles = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
            ui_mode="Ancient",
        ).generate_visual_styles()

        self.assertIn("height: clamp(300px, 35vh, 390px) !important;", other_styles)
        self.assertIn("height: clamp(240px, 30vh, 360px) !important;", ancient_styles)
        self.assertIn("max-height: clamp(240px, 30vh, 360px) !important;", ancient_styles)

    def test_source_directory_lookup_supports_node_and_chapter_depth_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node_dir = root / "node_abc123"
            chapter_dir = root / "chapter_7_def456"
            node_dir.mkdir()
            chapter_dir.mkdir()
            (node_dir / "audios" / "en-US-subtitle").mkdir(parents=True)
            (node_dir / "audios" / "en-US-subtitle" / "line-1.txt").write_text(
                "Narrator: hello", encoding="utf-8"
            )
            (chapter_dir / "audios" / "en-US-subtitle").mkdir(parents=True)
            (chapter_dir / "audios" / "en-US-subtitle" / "line-1.txt").write_text(
                "Narrator: hello", encoding="utf-8"
            )

            source_index = FinalConverter._index_source_directories(root)
            self.assertEqual(
                node_dir,
                FinalConverter._resolve_node_source_directory(
                    source_index, "node_abc123", 3
                ),
            )
            self.assertEqual(
                chapter_dir,
                FinalConverter._resolve_node_source_directory(
                    source_index, "node_def456", 7
                ),
            )

    def test_source_directory_lookup_merges_node_and_chapter_asset_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node_dir = root / "node_abc123"
            chapter_dir = root / "chapter_7_abc123"
            node_dir.mkdir()
            chapter_dir.mkdir()
            (node_dir / "audios" / "en-US-subtitle").mkdir(parents=True)
            (node_dir / "audios" / "en-US-subtitle" / "line-1.txt").write_text(
                "LaoZhe: hello", encoding="utf-8"
            )
            (chapter_dir / "audios" / "bgm").mkdir(parents=True)
            # A zero-byte placeholder is not usable material and must not
            # make the chapter directory win/appear in the merge result.
            (chapter_dir / "audios" / "bgm" / "bgm_1-1.wav").write_bytes(b"RIFF")

            source_index = FinalConverter._index_source_directories(root)
            roots = FinalConverter._resolve_node_source_directories(
                source_index, "node_abc123", 7
            )
            self.assertEqual([node_dir, chapter_dir], roots)

            converter = FinalConverter(
                source_dir=node_dir,
                template_dir=root,
                output_dir=root / "out",
                enable_oss_upload=False,
            )
            converter.asset_source_dirs = roots
            converter._source_node_id = "node_abc123"
            converter._scan_assets_from_master_json(
                {
                    "node_abc123": [
                        {
                            "beat_id": "1",
                            "subtitle_id": "line-1",
                            "speaker_id": "LaoZhe",
                            "bgm_id": "bgm",
                        }
                    ]
                }
            )
            self.assertEqual(node_dir / "audios" / "en-US-subtitle" / "line-1.txt",
                             converter.subtitle_files[0]["path"])
            self.assertEqual(chapter_dir / "audios" / "bgm" / "bgm_1-1.wav",
                             converter.music_files[0]["path"])

    def test_asset_lookup_skips_zero_byte_placeholder_in_priority_root(self):
        """A placeholder must not hide real mixed-prefix subtitle/audio."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node_dir = root / "node_abc123"
            chapter_dir = root / "chapter_7_abc123"
            node_dir.mkdir()
            chapter_dir.mkdir()
            node_audio = node_dir / "audios" / "en-US-audio"
            chapter_audio = chapter_dir / "audios" / "en-US-audio"
            node_audio.mkdir(parents=True)
            chapter_audio.mkdir(parents=True)
            (node_audio / "line-1.wav").touch()
            real = chapter_audio / "line-1.wav"
            real.write_bytes(b"RIFF")

            source_index = FinalConverter._index_source_directories(root)
            roots = FinalConverter._resolve_node_source_directories(
                source_index, "node_abc123", 7
            )
            converter = FinalConverter(
                source_dir=roots[0],
                template_dir=root,
                output_dir=root,
                enable_oss_upload=False,
            )
            converter.asset_source_dirs = roots
            converter._source_node_id = "node_abc123"
            # Exercise the same helper through a one-beat master scan.
            converter._scan_assets_from_master_json({
                "node_abc123": [{
                    "beat_id": "1",
                    "subtitle_id": "line-1",
                    "speaker_id": "Narrator",
                }]
            })
            self.assertEqual([real], [item["path"] for item in converter.audio_files])

    def test_japanese_only_assets_still_drive_material_scan_with_english_runtime_default(self):
        """Build beats from the available locale without changing URL fallback."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node_dir = root / "node_abc123"
            subtitle_dir = node_dir / "audios" / "ja-JP-subtitle"
            audio_dir = node_dir / "audios" / "ja-JP-audio"
            subtitle_dir.mkdir(parents=True)
            audio_dir.mkdir(parents=True)
            subtitle = subtitle_dir / "line-1.txt"
            voice = audio_dir / "line-1.wav"
            subtitle.write_text("Hero: こんにちは", encoding="utf-8")
            voice.write_bytes(b"RIFF")

            converter = FinalConverter(
                source_dir=node_dir,
                template_dir=root,
                output_dir=root / "built",
                enable_oss_upload=False,
            )
            converter._source_node_id = "node_abc123"
            converter._scan_assets_from_master_json({
                "node_abc123": [{
                    "beat_id": "1",
                    "subtitle_id": "line-1",
                    "speaker_id": "Hero",
                }]
            })

            self.assertEqual("en-US", converter.default_language)
            self.assertEqual("ja-JP", converter.scan_subtitle_language)
            self.assertEqual("ja-JP", converter.scan_audio_language)
            self.assertEqual([subtitle], [item["path"] for item in converter.subtitle_files])
            self.assertEqual([voice], [item["path"] for item in converter.audio_files])
            self.assertEqual("こんにちは", converter.i18n_text["ja-JP"]["line-1"])

    def test_material_scan_can_source_subtitle_and_audio_from_different_locales(self):
        """Partial locale exports must not suppress either side of a beat."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node_dir = root / "node_abc123"
            subtitle_dir = node_dir / "audios" / "ja-JP-subtitle"
            audio_dir = node_dir / "audios" / "en-US-audio"
            subtitle_dir.mkdir(parents=True)
            audio_dir.mkdir(parents=True)
            subtitle = subtitle_dir / "line-1.txt"
            voice = audio_dir / "line-1.wav"
            subtitle.write_text("Hero: こんにちは", encoding="utf-8")
            voice.write_bytes(b"RIFF")

            converter = FinalConverter(
                source_dir=node_dir,
                template_dir=root,
                output_dir=root / "built",
                enable_oss_upload=False,
            )
            converter._source_node_id = "node_abc123"
            converter._scan_assets_from_master_json({
                "node_abc123": [{
                    "beat_id": "1",
                    "subtitle_id": "line-1",
                    "speaker_id": "Hero",
                }]
            })

            self.assertEqual("en-US", converter.scan_audio_language)
            self.assertEqual("ja-JP", converter.scan_subtitle_language)
            self.assertEqual([subtitle], [item["path"] for item in converter.subtitle_files])
            self.assertEqual([voice], [item["path"] for item in converter.audio_files])

    def test_chapter_depth_directory_resolves_to_master_node_key(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        master_data = {
            "chapter_1": [{"beat_id": "1", "subtitle_id": "subtitle_1"}],
            "node_def456": [{"beat_id": "1", "subtitle_id": "subtitle_2"}],
        }
        self.assertEqual(
            "node_def456",
            converter._resolve_chapter_key(
                master_data,
                "chapter_7_def456",
            ),
        )

    def test_stats_ui_is_not_created_when_story_has_no_player_variables(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        converter._has_player_variables = False
        utility = "\n".join(converter.generate_utility_code({}, "chapter_1", 1))
        self.assertIn("window._enableStatsPanel = false;", utility)
        self.assertIn(
            'const statsToggle = window._enableStatsPanel ? document.createElement("div") : null;',
            utility,
        )

    def test_generic_chapter_continue_choice_uses_runtime_english_or_japanese(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        script = converter.generate_script_for_chapter(
            {"materials": [], "beat_meta_by_index": {}},
            node_data={
                "id": "chapter_1",
                "depth": 1,
                "children": [{"child_id": "chapter_2", "choice_text": "继续"}],
            },
            folder_mapping={"chapter_2": "chapter_2_chapter_2"},
            game_project="task-123",
        )
        # The sole ordinary child is a linear edge, so its source choice text
        # is intentionally not rendered as a Choice/Continue action.
        self.assertNotIn("␟ui:cont␟", script)
        self.assertNotIn("'Text': '继续'", script)
        # A chapter with exactly one ordinary child is linear, not a player
        # choice.  It must route directly instead of showing the engine's
        # "Please make your choice" prompt and then blocking forever.
        self.assertNotIn("␟ui:makeChoice␟", script)
        self.assertNotIn("'Choice': {", script)
        self.assertNotIn("return new Promise(() => {});", script)
        self.assertIn("线性章节自动跳转", script)
        self.assertIn("window.StoryNavigator.request(_targetUrl", script)

    def test_single_linear_child_routes_without_choice_or_blocking(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        script = converter.generate_script_for_chapter(
            {"materials": [], "beat_meta_by_index": {}},
            node_data={
                "id": "chapter_1",
                "depth": 1,
                "children": [{
                    "child_id": "node_2",
                    "choice_text": "Continue to the next chapter",
                    "branch_variable": None,
                }],
            },
            folder_mapping={"node_2": "chapter_2_node_2"},
            game_project="task-linear",
        )

        end = script.split("'EndChapter': [", 1)[1]
        self.assertNotIn("␟ui:chapterComplete␟", end)
        self.assertNotIn("␟ui:makeChoice␟", end)
        self.assertNotIn("'Choice': {", end)
        self.assertNotIn("return new Promise(() => {});", end)
        self.assertIn("/task-linear/chapter_2_node_2/?vars=", end)
        self.assertIn("window.StoryNavigator.request(_targetUrl", end)

    def test_multi_child_branch_still_renders_choice_and_wait(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        script = converter.generate_script_for_chapter(
            {"materials": [], "beat_meta_by_index": {}},
            node_data={
                "id": "chapter_1",
                "depth": 1,
                "children": [
                    {"child_id": "node_a", "choice_text": "A"},
                    {"child_id": "node_b", "choice_text": "B"},
                ],
            },
            folder_mapping={
                "node_a": "chapter_2_node_a",
                "node_b": "chapter_2_node_b",
            },
            game_project="task-branch",
        )
        end = script.split("'EndChapter': [", 1)[1]
        self.assertIn("␟ui:chapterComplete␟", end)
        self.assertIn("␟ui:makeChoice␟", end)
        self.assertIn("'Choice': {", end)
        self.assertIn("return new Promise(() => {});", end)

    def test_quit_flushes_current_beat_before_engine_reset(self):
        # Quit must synchronously publish the visible beat before the iframe or
        # Monogatari state is torn down.  A forced report is allowed through
        # the normal disable guard, and reset-time reports remain blocked.
        self.assertIn("flushForExit: function(source)", self.source)
        self.assertIn("const visible = Number(window._currentMaterialIndex)", self.source)
        self.assertIn("this.sendReport(source || 'user_exit', true)", self.source)
        self.assertIn("if (this._disableReport && !force)", self.source)
        self.assertIn("if (this._exitInProgress && !force)", self.source)
        self.assertIn("GameProgressReporter.flushForExit('user_exit')", self.source)

    def test_quit_preserves_continue_state_and_rejects_stale_progress(self):
        self.assertIn("const visibleIndex = Math.max(reporterIndex, savedIndex)", self.source)
        self.assertIn("_incomingIndex < _localIndex", self.source)
        self.assertIn("window.GameProgressReporter._exitInProgress", self.source)
        self.assertIn("Do not re-enable reporting after Quit", self.source)

    def test_progress_lifecycle_listeners_are_bound_once(self):
        self.assertIn("_lifecycleBound: false", self.source)
        self.assertIn("if (!this._lifecycleBound)", self.source)
        self.assertIn("window.addEventListener('pagehide', flush)", self.source)
        self.assertIn("document.visibilityState === 'hidden'", self.source)

    def test_custom_ui_uses_only_english_and_japanese_and_text_box_moves_down(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        script = converter.generate_script_for_chapter(
            {"materials": [], "beat_meta_by_index": {}},
            node_data={"id": "node_final", "depth": 1, "children": []},
            folder_mapping={"node_final": "chapter_1_node_final"},
            game_project="task-123",
        )
        self.assertNotIn("'zh-CN': { restart:", script)
        self.assertIn('"ja-JP": {"start": "開始"', script)
        self.assertIn('"en-US": {"start": "Start"', script)
        self.assertIn("window.__refreshEngineButtonLabels", script)
        self.assertIn("label.classList.add('custom-ui-label')", script)
        self.assertIn("candidate.replaceWith(label)", script)
        self.assertNotIn(
            "if (candidate && candidate.nodeValue !== text) candidate.nodeValue = text;",
            script,
        )
        self.assertIn(
            "action === 'save' && button.closest('save-screen, [data-component=\"save-screen\"]')",
            script,
        )

    def test_online_autoplay_uses_lexical_engine_and_single_interaction_owner(self):
        self.assertIn("typeof monogatari === 'undefined'", self.source)
        self.assertIn("window.monogatari = monogatari", self.source)
        self.assertIn("target.closest('[data-action=\\\"auto-play\\\"]')", self.source)
        self.assertIn(
            "event.stopImmediatePropagation(); event.stopPropagation(); self.set(!self.isEnabled())",
            self.source,
        )

    def test_online_autoplay_retries_engine_races_and_invalidates_old_timers(self):
        self.assertIn("_retryTimer", self.source)
        self.assertIn("_generation", self.source)
        self.assertIn("generation !== this._generation", self.source)
        self.assertIn("TypeWriter", self.source)
        self.assertIn("this.retry(100, generation)", self.source)

    def test_online_autoplay_defers_beat_timer_until_voice_setup(self):
        enter = self.source.split("lines.append(\"window.AutoVoiceAdvance = {\")", 1)[1]
        enter = enter.split("lines.append(\"    readingText:", 1)[0]
        self.assertIn(
            "setTimeout(function(){ if (window.AutoVoiceAdvance._token === expected",
            enter,
        )

    def test_seek_does_not_clamp_past_end_and_navigation_uses_native_jump(self):
        # Regression for the observed 207/208/210 -> Material_206 NVL loop.
        self.assertIn("requested > indices[indices.length - 1]", self.source)
        self.assertIn("targetLabel = 'EndBridge'", self.source)
        self.assertIn('else "EndBridge"', self.source)
        self.assertIn("'jump EndChapter'", self.source)
        self.assertIn("monogatari.run('jump ' + label, false)", self.source)

    def test_seek_rejects_non_numeric_indices_and_serializes_requests(self):
        self.assertIn("Number.isFinite(Number(beatIndex))", self.source)
        self.assertIn("if (window._seekInFlight) return", self.source)
        self.assertIn("[SEEK_TO_BEAT] request=", self.source)

    def test_autoplay_interval_is_delegated_and_remounted_after_dynamic_settings_render(self):
        self.assertIn("bindDelegated", self.source)
        self.assertIn("MutationObserver", self.source)
        self.assertIn("input.min = '0'; input.max = '3'; input.step = '0.1'", self.source)

    def test_autoplay_interval_does_not_treat_missing_storage_as_zero_seconds(self):
        self.assertIn("raw == null || raw === ''", self.source)
        self.assertIn("return NaN", self.source)
        self.assertIn("if (!isFinite(value) || value < 0 || value > 3) value = 1;", self.source)

    def test_ui_challenge_text_and_positioning_are_localized(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        utility = "\n".join(converter.generate_utility_code({}, "node_final", 1))
        self.assertIn("window.__ui('rapidClick')", utility)
        self.assertIn("window.__ui('clickInOrder')", utility)

        for ui_mode in ("Other", "Ancient"):
            styles = FinalConverter(
                source_dir=root,
                template_dir=root,
                output_dir=root,
                enable_oss_upload=False,
                ui_mode=ui_mode,
            ).generate_visual_styles()
            self.assertIn("transform: translateY(10%) !important;", styles)

    def test_online_language_parser_accepts_only_exact_supported_locales(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        script = converter.generate_script_for_chapter(
            {"materials": [], "beat_meta_by_index": {}},
            node_data={"id": "node_lang", "depth": 1, "children": []},
            folder_mapping={"node_lang": "chapter_1_node_lang"},
            game_project="task-lang",
        )
        self.assertIn("if (value !== 'en-US' && value !== 'ja-JP') return 'en-US';", script)
        self.assertIn("var value = String(raw || '');", script)
        self.assertNotIn("var value = String(raw || '').trim();", script)
        self.assertNotIn("'en':'en-US'", script)
        self.assertNotIn("urlLang", script)
        self.assertNotIn("sameBase", script)

    def test_story_tree_language_wrapper_ignores_unsupported_layers(self):
        raw = {
            "en-US": {"nodes": {"chapter_1": {"title": "English"}}},
            "ja-JP": {"nodes": {"chapter_1": {"title": "日本語"}}},
            "zh-CN": {"nodes": {"chapter_1": {"title": "中文"}}},
        }
        normalized, layers = normalize_story_tree(raw)
        self.assertEqual({"en-US", "ja-JP"}, set(layers))
        self.assertEqual("English", normalized["nodes"]["chapter_1"]["title"])

    def test_story_tree_language_wrapper_allows_one_nested_payload_layer(self):
        raw = {
            "en-US": {"story_tree": {"nodes": {"chapter_1": {"title": "English"}}}},
            "ja-JP": {"data": {"nodes": {"chapter_1": {"title": "日本語"}}}},
            "zh-CN": {"nodes": {"chapter_1": {"title": "中文"}}},
        }
        normalized, layers = normalize_story_tree(raw)
        self.assertEqual({"en-US", "ja-JP"}, set(layers))
        self.assertEqual("English", normalized["nodes"]["chapter_1"]["title"])

    def test_online_character_name_payload_excludes_unsupported_language_layers(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
            book_language="zh-CN",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping_path = Path(temp_dir) / "name_mappings.json"
            mapping_path.write_text(
                '{"change_name": {"en-US": {"Hero": "Hero"}, '
                '"ja-JP": {"Hero": "ヒーロー"}, '
                '"zh-CN": {"Hero": "中文角色"}}}',
                encoding="utf-8",
            )
            converter.images_base_dir = Path(temp_dir)
            converter.source_dir = Path(temp_dir) / "chapter_1"
            converter.source_dir.mkdir()
            converter.load_name_mappings()
            self.assertEqual({"en-US", "ja-JP"}, set(converter.names_by_lang))
            self.assertEqual("Hero", converter.name_map["Hero"])

    def test_cg_keeps_subtitles_but_omits_the_speaker_name(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        script = converter.generate_script_for_chapter(
            {
                "materials": [
                    {
                        "is_subtitle": True,
                        "index": 1,
                        "file": "subtitle_cg.txt",
                        "subtitle_id": "subtitle_cg",
                        "scene_id": "cg_scene",
                        "chapter": 1,
                        "scene": 1,
                        "dialogues": [
                            {"speaker": "Hero", "text": "CG subtitle remains visible"}
                        ],
                    }
                ],
                "beat_meta_by_index": {
                    1: {"cg_mode": True, "speaker_id": "Hero"}
                },
            },
            node_data={"id": "node_cg", "depth": 1, "children": []},
            folder_mapping={"node_cg": "chapter_1_node_cg"},
            game_project="task-123",
        )
        self.assertIn("'CG subtitle remains visible'", script)
        self.assertNotIn("'Hero CG subtitle remains visible'", script)

        styles = converter.generate_visual_styles()
        self.assertIn(
            'body.cg-active [data-component="text-box"] [data-ui="who"]',
            styles,
        )

    def test_multi_speaker_highlight_does_not_dim_or_blur_other_characters(self):
        root = Path(__file__).resolve().parents[2]
        converter = FinalConverter(
            source_dir=root,
            template_dir=root,
            output_dir=root,
            enable_oss_upload=False,
        )
        styles = converter.generate_visual_styles()
        speaking = styles.split(
            ".manual-character-container.is-speaking .character-image", 1
        )[1].split(".manual-character-container.is-not-speaking .character-image", 1)[0]
        non_speaking = styles.split(
            ".manual-character-container.is-not-speaking .character-image", 1
        )[1].split(".manual-character-container .character-image", 1)[0]

        # The speaker gets only a subtle boost/outline and a tiny anchored scale.
        self.assertIn("brightness(1.04)", speaking)
        self.assertIn("drop-shadow(0 0 1px", speaking)
        self.assertIn("scale(1.012)", speaking)
        self.assertIn("transform-origin: center bottom", speaking)
        self.assertIn(
            "opacity 0.9s ease, filter 0.25s ease, transform 0.25s ease",
            self.source,
        )

        # Other characters remain crisp and at full opacity; no blur/dimming.
        self.assertIn("opacity: 1 !important", non_speaking)
        self.assertIn("filter: none !important", non_speaking)
        self.assertIn("transform: translateX(-50%) !important", non_speaking)
        self.assertNotIn("blur(", non_speaking)
        self.assertNotIn("brightness(0.80)", non_speaking)
        self.assertNotIn("opacity: 0.72", non_speaking)
