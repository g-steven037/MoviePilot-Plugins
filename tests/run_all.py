from __future__ import annotations

import sys
import tempfile
from pathlib import Path


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import test_plugin_security as plugin
import test_rapid as rapid
import test_emby_library_cover as emby


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
    plugin.test_realtime_failure_then_retry_success_keeps_pt_file(temporary_path())
    plugin.test_retry_exhaustion_delete_switch_is_safe_and_keeps_pt_file(temporary_path())
    plugin.test_verified_unlink_rejects_replaced_file(temporary_path())
    emby.test_url_and_api_key_are_hardened()
    emby.test_library_mapping_is_bounded_and_supports_line_breaks()
    emby.test_form_defaults_to_generation_only_and_password_field()
    emby.test_title_gap_and_english_letter_spacing_affect_pixels()
    emby.test_moviepilot_emby_config_is_resolved_without_copying_secrets()
    emby.test_renderer_creates_both_styles(temporary_path())
    emby.test_embedded_font_exists_and_renders_chinese(temporary_path())
    emby.test_visual_config_is_validated_and_applied()
    emby.test_safe_filename_does_not_escape_output_directory()
    print("All 28 unit, security, rendering, and realtime integration tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
