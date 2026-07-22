from __future__ import annotations

import sys
import tempfile
from pathlib import Path


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import test_plugin_security as plugin
import test_rapid as rapid
import test_emby_library_cover as emby
import test_download_capacity_guard as capacity
import test_drama_calendar as calendar
import test_emby_actor_chinese as actor_chinese
import test_subscribe_assistant as subscribe_assistant


def temporary_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="p115rapidretry-test-"))


def main() -> int:
    for function in (
        rapid.test_hash_and_range,
        rapid.test_reuse_is_success,
        rapid.test_miss_is_retryable_and_does_not_upload_content,
        rapid.test_known_sha1_cache_skips_full_rehash,
        rapid.test_raw_server_error_is_not_returned,
        rapid.test_hardlink_and_replacement_detection,
        rapid.test_exception_text_is_not_returned,
        rapid.test_request_guard_blocks_api_call,
        rapid.test_http_status_exception_triggers_risk_codes,
    ):
        function(temporary_path())
    plugin.test_cookie_validation_rejects_unsafe_values()
    plugin.test_client_constructor_uses_current_signature()
    plugin.test_detailed_audit_log_is_default_and_sanitized()
    plugin.test_retry_limit_and_bot_notifications_are_bounded()
    plugin.test_risk_control_persists_and_cookie_change_releases_auth_block()
    plugin.test_brief_log_contains_only_required_fields(temporary_path())
    plugin.test_scheduled_empty_cleanup_never_deletes_roots_or_nonempty_dirs(temporary_path())
    plugin.test_manual_rapid_and_retry_actions_use_the_worker_queue()
    plugin.test_realtime_failure_then_retry_success_keeps_pt_file(temporary_path())
    plugin.test_retry_exhaustion_delete_switch_is_safe_and_keeps_pt_file(temporary_path())
    plugin.test_verified_unlink_rejects_replaced_file(temporary_path())
    plugin.test_dependency_manifest_uses_correct_asynctools_distribution()
    plugin.test_p115_sources_are_strict_utf8_without_replacement_characters()
    plugin.test_cached_legacy_asynctools_is_reloaded_after_install()
    plugin.test_asynctools_021_is_rejected_even_when_exports_exist()
    emby.test_url_and_api_key_are_hardened()
    emby.test_library_mapping_is_bounded_and_supports_line_breaks()
    emby.test_form_defaults_to_generation_only_and_password_field()
    emby.test_title_gap_and_english_letter_spacing_affect_pixels()
    emby.test_style3_accent_color_tracks_background_and_stays_visible()
    emby.test_moviepilot_emby_config_is_resolved_without_copying_secrets()
    emby.test_renderer_creates_both_styles(temporary_path())
    emby.test_embedded_font_exists_and_renders_chinese(temporary_path())
    emby.test_visual_config_is_validated_and_applied()
    emby.test_safe_filename_does_not_escape_output_directory()
    capacity.test_guard_counts_active_remaining_and_concurrent_reservations(temporary_path())
    capacity.test_guard_rejects_unknown_size_and_calculates_remaining(temporary_path())
    capacity.test_guard_form_defaults_are_fail_closed()
    calendar.test_calendar_formats_multiple_days_and_episode_ranges()
    calendar.test_cache_persists_and_prunes_safely(temporary_path())
    calendar.test_moviepilot_media_config_is_resolved_without_logging_secrets()
    calendar.test_form_uses_moviepilot_notification_defaults_without_bot_commands()
    actor_chinese.test_actor_mapping_is_unique_exact_and_actor_only()
    actor_chinese.test_actor_mapping_accepts_only_unique_surname_order_variants()
    actor_chinese.test_emby_item_selection_requires_exact_title_year_and_unique_result()
    actor_chinese.test_form_defaults_to_preview_and_hides_manual_credentials()
    actor_chinese.test_emby_client_never_puts_key_in_url_and_blocks_redirect()
    actor_chinese.test_preview_never_writes_and_sync_verifies_write()
    subscribe_assistant.test_subscription_words_rename_unique_match_and_keep_original_without_match()
    subscribe_assistant.test_ambiguous_subscription_words_keep_original_name()
    subscribe_assistant.test_subscription_events_only_invalidate_cache_and_never_write_database()
    subscribe_assistant.test_link_file_uses_subscription_rename_and_preserves_source(temporary_path())
    subscribe_assistant.test_link_file_without_custom_words_uses_original_relative_path(temporary_path())
    subscribe_assistant.test_download_temp_extensions_are_skipped()
    print("All 53 unit, security, rendering, realtime, capacity-control, calendar, actor, and subscription-assistant tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
