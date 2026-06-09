import os
import sys
import time
import socket
import threading
import queue
import webbrowser
import json
import httpx
import uvicorn
import pathlib
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# Setup Python paths
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT))
sys.path.append(str(PROJECT_ROOT / "server"))

from tests.units.test_gsa_wal_concurrency_isolation import test_gsa_wal_concurrency_isolation
from tests.units.test_scenario_agent_live_prompt_turn import test_scenario_agent_live_prompt_turn
from tests.units.test_audio_agent_tts_job_queueing import test_audio_agent_tts_job_queueing
from tests.units.test_video_agent_ltx_job_queueing import test_video_agent_ltx_job_queueing
from tests.units.test_provisioner_vast_offers_search import test_provisioner_vast_offers_search
from tests.units.test_vast_create_and_destroy_lifecycle import test_vast_create_and_destroy_lifecycle
from tests.units.test_ssh_handshake_and_docker_health import test_ssh_handshake_and_docker_health
from tests.units.test_audio_loudness_normalizer_compilation import test_audio_loudness_normalizer_compilation
from tests.units.test_coordinate_timeline_dynamic_drift import test_coordinate_timeline_dynamic_drift
from tests.units.test_budget_limit_aborted_gate import test_budget_limit_aborted_gate
from tests.units.test_agent_chooses_vm_size_and_provisioner_allocates import test_agent_chooses_vm_size_and_provisioner_allocates
from tests.units.test_provisioner_escalation_policy import test_provisioner_escalation_policy
from tests.units.test_preemption_and_recovery import test_preemption_and_recovery
from tests.units.test_localized_recovery_and_retry import test_localized_recovery_and_retry
from tests.units.test_accumulative_drift_correction import test_accumulative_drift_correction
from tests.units.test_provisioner_cli_command_invocation import test_provisioner_cli_command_invocation
from tests.units.test_assemble_final_cut_execution import test_assemble_final_cut_execution
from tests.units.test_real_qwen3_tts_script_execution import test_real_qwen3_tts_script_execution
from tests.units.test_real_ltx_video_script_execution import test_real_ltx_video_script_execution
from tests.units.test_parse_duration_all_formats import test_parse_duration_all_formats
from tests.units.test_effect_pydantic_round_trip import test_effect_pydantic_round_trip
from tests.units.test_event_store_append_replay_ordering import test_event_store_append_replay_ordering
from tests.units.test_event_store_idempotent_dedup import test_event_store_idempotent_dedup
from tests.units.test_event_store_read_since_window import test_event_store_read_since_window
from tests.units.test_timeline_projection_script_to_slots import test_timeline_projection_script_to_slots
from tests.units.test_timeline_projection_merge_and_delivered import test_timeline_projection_merge_and_delivered
from tests.units.test_timeline_projection_delete_scene import test_timeline_projection_delete_scene
from tests.units.test_timeline_projection_reorder_scenes import test_timeline_projection_reorder_scenes
from tests.units.test_timeline_validation_suite import test_timeline_validation_suite
from tests.units.test_jobs_projection_full_lifecycle import test_jobs_projection_full_lifecycle
from tests.units.test_jobs_projection_dirty_clean_tracking import test_jobs_projection_dirty_clean_tracking
from tests.units.test_vm_projection_multi_role_fleet import test_vm_projection_multi_role_fleet
from tests.units.test_budget_projection_exceeded_detection import test_budget_projection_exceeded_detection
from tests.units.test_state_projection_full_phase_machine import test_state_projection_full_phase_machine
from tests.units.test_coordinate_timeline_cascade_and_overlap import test_coordinate_timeline_cascade_and_overlap
from tests.units.test_bdd_tts_fleet_cold_start import test_bdd_tts_fleet_cold_start
from tests.units.test_bdd_single_block_tts_inference import test_bdd_single_block_tts_inference
from tests.units.test_bdd_multi_block_tts_reconciliation import test_bdd_multi_block_tts_reconciliation
from tests.units.test_bdd_voice_continuity_across_scenes import test_bdd_voice_continuity_across_scenes
from tests.units.test_bdd_ltx_fleet_scale_up import test_bdd_ltx_fleet_scale_up
from tests.units.test_bdd_single_clip_video_generation import test_bdd_single_clip_video_generation
from tests.units.test_bdd_multi_scene_video_otio_assembly import test_bdd_multi_scene_video_otio_assembly
from tests.units.test_bdd_audio_video_duration_alignment import test_bdd_audio_video_duration_alignment
from tests.units.test_bdd_tts_retry_after_failure import test_bdd_tts_retry_after_failure
from tests.units.test_bdd_vm_preemption_recovery import test_bdd_vm_preemption_recovery
from tests.units.test_bdd_budget_gated_provisioning import test_bdd_budget_gated_provisioning
from tests.units.test_bdd_script_revision_selective_requeue import test_bdd_script_revision_selective_requeue
from tests.units.test_bdd_final_assembly_real_media import test_bdd_final_assembly_real_media
from tests.units.test_bdd_partial_failure_isolated_recovery import test_bdd_partial_failure_isolated_recovery
from tests.units.test_bdd_full_fleet_teardown_cost_accounting import test_bdd_full_fleet_teardown_cost_accounting
from tests.units.test_perplexity_verify_live import test_perplexity_verify_live
from tests.units import test_max_capacity_pipeline
from tests.units.test_sim_gsa_wal_expanded import (
    test_sim_gsa_wal_concurrent_appends,
    test_sim_gsa_wal_read_during_write,
    test_sim_gsa_wal_replay_ordering,
    test_sim_gsa_wal_idempotent_dedup,
    test_sim_gsa_wal_read_since_window,
    test_sim_gsa_wal_schema_validation,
    test_sim_gsa_wal_db_lock_recovery,
    test_sim_gsa_wal_empty_store_replay,
    test_sim_gsa_wal_corrupt_payload_handling,
    test_sim_gsa_wal_massive_event_stream,
    test_sim_gsa_wal_sequential_ids,
    test_sim_gsa_wal_query_filtering,
    test_sim_gsa_wal_multi_agent_registration,
    test_sim_gsa_wal_checkpoint_generation,
    test_sim_gsa_wal_transaction_rollback,
    test_sim_gsa_wal_concurrent_readers,
    test_sim_gsa_wal_write_heavy_load,
    test_sim_gsa_wal_read_heavy_load,
    test_sim_gsa_wal_event_timestamp_ordering,
    test_sim_gsa_wal_gsa_state_reconstruction,
    test_sim_gsa_wal_event_size_limit,
    test_sim_gsa_wal_sqlite_journal_mode,
    test_sim_gsa_wal_db_vacuum_operation,
    test_sim_gsa_wal_agent_heartbeat_log,
    test_sim_gsa_wal_unexpected_db_disconnect,
    test_sim_gsa_wal_event_type_filtering,
    test_sim_gsa_wal_backup_restore_sync,
    test_sim_gsa_wal_concurrent_replays,
    test_sim_gsa_wal_read_offset_out_of_bounds,
    test_sim_gsa_wal_stale_event_discard,
    test_sim_gsa_wal_db_path_permissions,
    test_sim_gsa_wal_metadata_validation,
    test_sim_gsa_wal_gsa_state_cache,
    test_sim_gsa_wal_event_store_stats,
    test_sim_gsa_wal_gsa_lock_file_handling,
    test_sim_gsa_wal_concurrency_stress,
    test_sim_gsa_wal_isolation_guarantees,
)
from tests.units.test_sim_provisioner_expanded import (
    test_sim_provisioner_allocation_success,
    test_sim_provisioner_allocation_out_of_budget,
    test_sim_provisioner_escalation_triggers,
    test_sim_provisioner_preemption_recovery,
    test_sim_provisioner_deallocation_reasons,
    test_sim_provisioner_ssh_handshake_timeout,
    test_sim_provisioner_vast_offers_parsing,
    test_sim_provisioner_docker_health_check,
    test_sim_provisioner_dry_run_behaviors,
    test_sim_provisioner_scaling_limits,
    test_sim_provisioner_multiple_instance_types,
    test_sim_provisioner_allocation_retry_backoff,
    test_sim_provisioner_deallocated_state_sync,
    test_sim_provisioner_billing_projection,
    test_sim_provisioner_cost_accumulation,
    test_sim_provisioner_vm_heartbeat_monitoring,
    test_sim_provisioner_vast_connection_failure,
    test_sim_provisioner_escalation_limit,
    test_sim_provisioner_gpu_offer_filtering,
    test_sim_provisioner_provision_failure_cleanup,
    test_sim_provisioner_zombie_vm_cleanup,
    test_sim_provisioner_worker_scale_down,
    test_sim_provisioner_worker_scale_up,
    test_sim_provisioner_instance_state_polling,
    test_sim_provisioner_api_key_rotation,
    test_sim_provisioner_concurrent_vm_requests,
    test_sim_provisioner_invalid_offer_skipping,
    test_sim_provisioner_vast_cli_error_handling,
    test_sim_provisioner_ssh_auth_failure,
    test_sim_provisioner_docker_pull_failure,
    test_sim_provisioner_health_status_change,
    test_sim_provisioner_excessive_cost_abort,
    test_sim_provisioner_empty_offers_fallback,
    test_sim_provisioner_fleet_teardown_cost_tracking,
    test_sim_provisioner_vm_role_transitions,
    test_sim_provisioner_budget_gate_verification,
    test_sim_provisioner_vast_api_retry_behavior,
)
from tests.units.test_sim_voice_continuity_expanded import (
    test_sim_voice_continuity_loudness_normalization,
    test_sim_voice_continuity_ffmpeg_compilation,
    test_sim_voice_continuity_alignment_drift,
    test_sim_voice_continuity_multi_block_reconciliation,
    test_sim_voice_continuity_profile_matching,
    test_sim_voice_continuity_audio_format_validation,
    test_sim_voice_continuity_scene_duration_mismatch,
    test_sim_voice_continuity_duration_adjust_limits,
    test_sim_voice_continuity_tts_inference_timeout,
    test_sim_voice_continuity_tts_requeue_on_fail,
    test_sim_voice_continuity_loudness_clip_prevention,
    test_sim_voice_continuity_silence_trimming,
    test_sim_voice_continuity_audio_merge_channels,
    test_sim_voice_continuity_sample_rate_conversion,
    test_sim_voice_continuity_multiple_speaker_tracks,
    test_sim_voice_continuity_audio_agent_queueing,
    test_sim_voice_continuity_missing_tts_params,
    test_sim_voice_continuity_wav_header_parsing,
    test_sim_voice_continuity_lufs_measurement_flakiness,
    test_sim_voice_continuity_drift_accumulative_correction,
    test_sim_voice_continuity_empty_audio_file_handling,
    test_sim_voice_continuity_voice_timbre_consistency,
    test_sim_voice_continuity_audio_caching_hit,
    test_sim_voice_continuity_audio_caching_miss,
    test_sim_voice_continuity_audio_pipeline_abort,
    test_sim_voice_continuity_reconciliation_retry,
    test_sim_voice_continuity_audio_overlap_correction,
    test_sim_voice_continuity_duration_extrapolation,
    test_sim_voice_continuity_excessive_silence_fill,
    test_sim_voice_continuity_tts_failures_recovery,
    test_sim_voice_continuity_corrupt_wav_reconstruction,
    test_sim_voice_continuity_audio_duration_rounding,
    test_sim_voice_continuity_loudness_normalizer_speed,
    test_sim_voice_continuity_voice_agent_state_restoration,
    test_sim_voice_continuity_audio_subtrack_offsets,
    test_sim_voice_continuity_ffmpeg_error_handling,
    test_sim_voice_continuity_audio_channel_mixdown,
)
from tests.units.test_sim_otio_assembly_expanded import (
    test_sim_otio_assembly_track_creation,
    test_sim_otio_assembly_multi_scene_clips,
    test_sim_otio_assembly_timeline_cascade,
    test_sim_otio_assembly_delete_scene_updates,
    test_sim_otio_assembly_reorder_scenes_updates,
    test_sim_otio_assembly_script_to_slots,
    test_sim_otio_assembly_merge_delivered_clips,
    test_sim_otio_assembly_missing_media_fallback,
    test_sim_otio_assembly_timeline_serialization,
    test_sim_otio_assembly_timeline_deserialization,
    test_sim_otio_assembly_track_overlap_detection,
    test_sim_otio_assembly_transition_effects,
    test_sim_otio_assembly_empty_timeline_validation,
    test_sim_otio_assembly_audio_video_sync,
    test_sim_otio_assembly_frame_rate_conformance,
    test_sim_otio_assembly_subclip_extraction,
    test_sim_otio_assembly_timeline_validation_errors,
    test_sim_otio_assembly_metadata_preservation,
    test_sim_otio_assembly_video_agent_queueing,
    test_sim_otio_assembly_otio_schema_compliance,
    test_sim_otio_assembly_concurrent_timeline_updates,
    test_sim_otio_assembly_timeline_diffing,
    test_sim_otio_assembly_track_deletion,
    test_sim_otio_assembly_media_reference_resolution,
    test_sim_otio_assembly_gap_insertion_alignment,
    test_sim_otio_assembly_timeline_rendering,
    test_sim_otio_assembly_track_renaming,
    test_sim_otio_assembly_invalid_media_duration,
    test_sim_otio_assembly_marker_addition_retrieval,
    test_sim_otio_assembly_timeline_flattening,
    test_sim_otio_assembly_video_clip_transcoding,
    test_sim_otio_assembly_audio_track_layering,
    test_sim_otio_assembly_timeline_resolution_drift,
    test_sim_otio_assembly_clip_boundary_clipping,
    test_sim_otio_assembly_timeline_split,
    test_sim_otio_assembly_unaligned_tracks_report,
    test_sim_otio_assembly_final_render_validation,
)


# Thread-safe log collection
log_queue = queue.Queue()
logs_list = []

# List of all test cases: (name, function, category)

TEST_CASES = [
    # 1. Simulation Covers (Consequential Claims subset)
    ("test_gsa_wal_concurrency_isolation", test_gsa_wal_concurrency_isolation, "Simulation Cover"),
    ("test_scenario_agent_live_prompt_turn", test_scenario_agent_live_prompt_turn, "Simulation Cover"),
    ("test_audio_agent_tts_job_queueing", test_audio_agent_tts_job_queueing, "Simulation Cover"),
    ("test_video_agent_ltx_job_queueing", test_video_agent_ltx_job_queueing, "Simulation Cover"),
    ("test_provisioner_vast_offers_search", test_provisioner_vast_offers_search, "Simulation Cover"),
    ("test_vast_create_and_destroy_lifecycle", test_vast_create_and_destroy_lifecycle, "Simulation Cover"),
    ("test_ssh_handshake_and_docker_health", test_ssh_handshake_and_docker_health, "Simulation Cover"),
    ("test_audio_loudness_normalizer_compilation", test_audio_loudness_normalizer_compilation, "Simulation Cover"),
    ("test_coordinate_timeline_dynamic_drift", test_coordinate_timeline_dynamic_drift, "Simulation Cover"),
    ("test_budget_limit_aborted_gate", test_budget_limit_aborted_gate, "Simulation Cover"),

    # 1b. Consequential Claims (non-covering subset)
    ("test_agent_chooses_vm_size_and_provisioner_allocates", test_agent_chooses_vm_size_and_provisioner_allocates, "Consequential Claims"),
    ("test_provisioner_escalation_policy", test_provisioner_escalation_policy, "Consequential Claims"),
    ("test_preemption_and_recovery", test_preemption_and_recovery, "Consequential Claims"),
    ("test_localized_recovery_and_retry", test_localized_recovery_and_retry, "Consequential Claims"),
    ("test_accumulative_drift_correction", test_accumulative_drift_correction, "Consequential Claims"),
    ("test_provisioner_cli_command_invocation", test_provisioner_cli_command_invocation, "Consequential Claims"),
    ("test_assemble_final_cut_execution", test_assemble_final_cut_execution, "Consequential Claims"),
    ("test_real_qwen3_tts_script_execution", test_real_qwen3_tts_script_execution, "Consequential Claims"),
    ("test_real_ltx_video_script_execution", test_real_ltx_video_script_execution, "Consequential Claims"),

    # 2. Process Tests
    ("test_parse_duration_all_formats", test_parse_duration_all_formats, "Process Tests"),
    ("test_effect_pydantic_round_trip", test_effect_pydantic_round_trip, "Process Tests"),
    ("test_event_store_append_replay_ordering", test_event_store_append_replay_ordering, "Process Tests"),
    ("test_event_store_idempotent_dedup", test_event_store_idempotent_dedup, "Process Tests"),
    ("test_event_store_read_since_window", test_event_store_read_since_window, "Process Tests"),
    ("test_timeline_projection_script_to_slots", test_timeline_projection_script_to_slots, "Process Tests"),
    ("test_timeline_projection_merge_and_delivered", test_timeline_projection_merge_and_delivered, "Process Tests"),
    ("test_timeline_projection_delete_scene", test_timeline_projection_delete_scene, "Process Tests"),
    ("test_timeline_projection_reorder_scenes", test_timeline_projection_reorder_scenes, "Process Tests"),
    ("test_timeline_validation_suite", test_timeline_validation_suite, "Process Tests"),
    ("test_jobs_projection_full_lifecycle", test_jobs_projection_full_lifecycle, "Process Tests"),
    ("test_jobs_projection_dirty_clean_tracking", test_jobs_projection_dirty_clean_tracking, "Process Tests"),
    ("test_vm_projection_multi_role_fleet", test_vm_projection_multi_role_fleet, "Process Tests"),
    ("test_budget_projection_exceeded_detection", test_budget_projection_exceeded_detection, "Process Tests"),
    ("test_state_projection_full_phase_machine", test_state_projection_full_phase_machine, "Process Tests"),
    ("test_coordinate_timeline_cascade_and_overlap", test_coordinate_timeline_cascade_and_overlap, "Process Tests"),

    # GSA WAL Expanded Tests (37)
    ("test_sim_gsa_wal_concurrent_appends", test_sim_gsa_wal_concurrent_appends, "Process Tests"),
    ("test_sim_gsa_wal_read_during_write", test_sim_gsa_wal_read_during_write, "Process Tests"),
    ("test_sim_gsa_wal_replay_ordering", test_sim_gsa_wal_replay_ordering, "Process Tests"),
    ("test_sim_gsa_wal_idempotent_dedup", test_sim_gsa_wal_idempotent_dedup, "Process Tests"),
    ("test_sim_gsa_wal_read_since_window", test_sim_gsa_wal_read_since_window, "Process Tests"),
    ("test_sim_gsa_wal_schema_validation", test_sim_gsa_wal_schema_validation, "Process Tests"),
    ("test_sim_gsa_wal_db_lock_recovery", test_sim_gsa_wal_db_lock_recovery, "Process Tests"),
    ("test_sim_gsa_wal_empty_store_replay", test_sim_gsa_wal_empty_store_replay, "Process Tests"),
    ("test_sim_gsa_wal_corrupt_payload_handling", test_sim_gsa_wal_corrupt_payload_handling, "Process Tests"),
    ("test_sim_gsa_wal_massive_event_stream", test_sim_gsa_wal_massive_event_stream, "Process Tests"),
    ("test_sim_gsa_wal_sequential_ids", test_sim_gsa_wal_sequential_ids, "Process Tests"),
    ("test_sim_gsa_wal_query_filtering", test_sim_gsa_wal_query_filtering, "Process Tests"),
    ("test_sim_gsa_wal_multi_agent_registration", test_sim_gsa_wal_multi_agent_registration, "Process Tests"),
    ("test_sim_gsa_wal_checkpoint_generation", test_sim_gsa_wal_checkpoint_generation, "Process Tests"),
    ("test_sim_gsa_wal_transaction_rollback", test_sim_gsa_wal_transaction_rollback, "Process Tests"),
    ("test_sim_gsa_wal_concurrent_readers", test_sim_gsa_wal_concurrent_readers, "Process Tests"),
    ("test_sim_gsa_wal_write_heavy_load", test_sim_gsa_wal_write_heavy_load, "Process Tests"),
    ("test_sim_gsa_wal_read_heavy_load", test_sim_gsa_wal_read_heavy_load, "Process Tests"),
    ("test_sim_gsa_wal_event_timestamp_ordering", test_sim_gsa_wal_event_timestamp_ordering, "Process Tests"),
    ("test_sim_gsa_wal_gsa_state_reconstruction", test_sim_gsa_wal_gsa_state_reconstruction, "Process Tests"),
    ("test_sim_gsa_wal_event_size_limit", test_sim_gsa_wal_event_size_limit, "Process Tests"),
    ("test_sim_gsa_wal_sqlite_journal_mode", test_sim_gsa_wal_sqlite_journal_mode, "Process Tests"),
    ("test_sim_gsa_wal_db_vacuum_operation", test_sim_gsa_wal_db_vacuum_operation, "Process Tests"),
    ("test_sim_gsa_wal_agent_heartbeat_log", test_sim_gsa_wal_agent_heartbeat_log, "Process Tests"),
    ("test_sim_gsa_wal_unexpected_db_disconnect", test_sim_gsa_wal_unexpected_db_disconnect, "Process Tests"),
    ("test_sim_gsa_wal_event_type_filtering", test_sim_gsa_wal_event_type_filtering, "Process Tests"),
    ("test_sim_gsa_wal_backup_restore_sync", test_sim_gsa_wal_backup_restore_sync, "Process Tests"),
    ("test_sim_gsa_wal_concurrent_replays", test_sim_gsa_wal_concurrent_replays, "Process Tests"),
    ("test_sim_gsa_wal_read_offset_out_of_bounds", test_sim_gsa_wal_read_offset_out_of_bounds, "Process Tests"),
    ("test_sim_gsa_wal_stale_event_discard", test_sim_gsa_wal_stale_event_discard, "Process Tests"),
    ("test_sim_gsa_wal_db_path_permissions", test_sim_gsa_wal_db_path_permissions, "Process Tests"),
    ("test_sim_gsa_wal_metadata_validation", test_sim_gsa_wal_metadata_validation, "Process Tests"),
    ("test_sim_gsa_wal_gsa_state_cache", test_sim_gsa_wal_gsa_state_cache, "Process Tests"),
    ("test_sim_gsa_wal_event_store_stats", test_sim_gsa_wal_event_store_stats, "Process Tests"),
    ("test_sim_gsa_wal_gsa_lock_file_handling", test_sim_gsa_wal_gsa_lock_file_handling, "Process Tests"),
    ("test_sim_gsa_wal_concurrency_stress", test_sim_gsa_wal_concurrency_stress, "Process Tests"),
    ("test_sim_gsa_wal_isolation_guarantees", test_sim_gsa_wal_isolation_guarantees, "Process Tests"),

    # VM Provisioner Expanded Tests (37)
    ("test_sim_provisioner_allocation_success", test_sim_provisioner_allocation_success, "Process Tests"),
    ("test_sim_provisioner_allocation_out_of_budget", test_sim_provisioner_allocation_out_of_budget, "Process Tests"),
    ("test_sim_provisioner_escalation_triggers", test_sim_provisioner_escalation_triggers, "Process Tests"),
    ("test_sim_provisioner_preemption_recovery", test_sim_provisioner_preemption_recovery, "Process Tests"),
    ("test_sim_provisioner_deallocation_reasons", test_sim_provisioner_deallocation_reasons, "Process Tests"),
    ("test_sim_provisioner_ssh_handshake_timeout", test_sim_provisioner_ssh_handshake_timeout, "Process Tests"),
    ("test_sim_provisioner_vast_offers_parsing", test_sim_provisioner_vast_offers_parsing, "Process Tests"),
    ("test_sim_provisioner_docker_health_check", test_sim_provisioner_docker_health_check, "Process Tests"),
    ("test_sim_provisioner_dry_run_behaviors", test_sim_provisioner_dry_run_behaviors, "Process Tests"),
    ("test_sim_provisioner_scaling_limits", test_sim_provisioner_scaling_limits, "Process Tests"),
    ("test_sim_provisioner_multiple_instance_types", test_sim_provisioner_multiple_instance_types, "Process Tests"),
    ("test_sim_provisioner_allocation_retry_backoff", test_sim_provisioner_allocation_retry_backoff, "Process Tests"),
    ("test_sim_provisioner_deallocated_state_sync", test_sim_provisioner_deallocated_state_sync, "Process Tests"),
    ("test_sim_provisioner_billing_projection", test_sim_provisioner_billing_projection, "Process Tests"),
    ("test_sim_provisioner_cost_accumulation", test_sim_provisioner_cost_accumulation, "Process Tests"),
    ("test_sim_provisioner_vm_heartbeat_monitoring", test_sim_provisioner_vm_heartbeat_monitoring, "Process Tests"),
    ("test_sim_provisioner_vast_connection_failure", test_sim_provisioner_vast_connection_failure, "Process Tests"),
    ("test_sim_provisioner_escalation_limit", test_sim_provisioner_escalation_limit, "Process Tests"),
    ("test_sim_provisioner_gpu_offer_filtering", test_sim_provisioner_gpu_offer_filtering, "Process Tests"),
    ("test_sim_provisioner_provision_failure_cleanup", test_sim_provisioner_provision_failure_cleanup, "Process Tests"),
    ("test_sim_provisioner_zombie_vm_cleanup", test_sim_provisioner_zombie_vm_cleanup, "Process Tests"),
    ("test_sim_provisioner_worker_scale_down", test_sim_provisioner_worker_scale_down, "Process Tests"),
    ("test_sim_provisioner_worker_scale_up", test_sim_provisioner_worker_scale_up, "Process Tests"),
    ("test_sim_provisioner_instance_state_polling", test_sim_provisioner_instance_state_polling, "Process Tests"),
    ("test_sim_provisioner_api_key_rotation", test_sim_provisioner_api_key_rotation, "Process Tests"),
    ("test_sim_provisioner_concurrent_vm_requests", test_sim_provisioner_concurrent_vm_requests, "Process Tests"),
    ("test_sim_provisioner_invalid_offer_skipping", test_sim_provisioner_invalid_offer_skipping, "Process Tests"),
    ("test_sim_provisioner_vast_cli_error_handling", test_sim_provisioner_vast_cli_error_handling, "Process Tests"),
    ("test_sim_provisioner_ssh_auth_failure", test_sim_provisioner_ssh_auth_failure, "Process Tests"),
    ("test_sim_provisioner_docker_pull_failure", test_sim_provisioner_docker_pull_failure, "Process Tests"),
    ("test_sim_provisioner_health_status_change", test_sim_provisioner_health_status_change, "Process Tests"),
    ("test_sim_provisioner_excessive_cost_abort", test_sim_provisioner_excessive_cost_abort, "Process Tests"),
    ("test_sim_provisioner_empty_offers_fallback", test_sim_provisioner_empty_offers_fallback, "Process Tests"),
    ("test_sim_provisioner_fleet_teardown_cost_tracking", test_sim_provisioner_fleet_teardown_cost_tracking, "Process Tests"),
    ("test_sim_provisioner_vm_role_transitions", test_sim_provisioner_vm_role_transitions, "Process Tests"),
    ("test_sim_provisioner_budget_gate_verification", test_sim_provisioner_budget_gate_verification, "Process Tests"),
    ("test_sim_provisioner_vast_api_retry_behavior", test_sim_provisioner_vast_api_retry_behavior, "Process Tests"),

    # Voice Continuity Expanded Tests (37)
    ("test_sim_voice_continuity_loudness_normalization", test_sim_voice_continuity_loudness_normalization, "Process Tests"),
    ("test_sim_voice_continuity_ffmpeg_compilation", test_sim_voice_continuity_ffmpeg_compilation, "Process Tests"),
    ("test_sim_voice_continuity_alignment_drift", test_sim_voice_continuity_alignment_drift, "Process Tests"),
    ("test_sim_voice_continuity_multi_block_reconciliation", test_sim_voice_continuity_multi_block_reconciliation, "Process Tests"),
    ("test_sim_voice_continuity_profile_matching", test_sim_voice_continuity_profile_matching, "Process Tests"),
    ("test_sim_voice_continuity_audio_format_validation", test_sim_voice_continuity_audio_format_validation, "Process Tests"),
    ("test_sim_voice_continuity_scene_duration_mismatch", test_sim_voice_continuity_scene_duration_mismatch, "Process Tests"),
    ("test_sim_voice_continuity_duration_adjust_limits", test_sim_voice_continuity_duration_adjust_limits, "Process Tests"),
    ("test_sim_voice_continuity_tts_inference_timeout", test_sim_voice_continuity_tts_inference_timeout, "Process Tests"),
    ("test_sim_voice_continuity_tts_requeue_on_fail", test_sim_voice_continuity_tts_requeue_on_fail, "Process Tests"),
    ("test_sim_voice_continuity_loudness_clip_prevention", test_sim_voice_continuity_loudness_clip_prevention, "Process Tests"),
    ("test_sim_voice_continuity_silence_trimming", test_sim_voice_continuity_silence_trimming, "Process Tests"),
    ("test_sim_voice_continuity_audio_merge_channels", test_sim_voice_continuity_audio_merge_channels, "Process Tests"),
    ("test_sim_voice_continuity_sample_rate_conversion", test_sim_voice_continuity_sample_rate_conversion, "Process Tests"),
    ("test_sim_voice_continuity_multiple_speaker_tracks", test_sim_voice_continuity_multiple_speaker_tracks, "Process Tests"),
    ("test_sim_voice_continuity_audio_agent_queueing", test_sim_voice_continuity_audio_agent_queueing, "Process Tests"),
    ("test_sim_voice_continuity_missing_tts_params", test_sim_voice_continuity_missing_tts_params, "Process Tests"),
    ("test_sim_voice_continuity_wav_header_parsing", test_sim_voice_continuity_wav_header_parsing, "Process Tests"),
    ("test_sim_voice_continuity_lufs_measurement_flakiness", test_sim_voice_continuity_lufs_measurement_flakiness, "Process Tests"),
    ("test_sim_voice_continuity_drift_accumulative_correction", test_sim_voice_continuity_drift_accumulative_correction, "Process Tests"),
    ("test_sim_voice_continuity_empty_audio_file_handling", test_sim_voice_continuity_empty_audio_file_handling, "Process Tests"),
    ("test_sim_voice_continuity_voice_timbre_consistency", test_sim_voice_continuity_voice_timbre_consistency, "Process Tests"),
    ("test_sim_voice_continuity_audio_caching_hit", test_sim_voice_continuity_audio_caching_hit, "Process Tests"),
    ("test_sim_voice_continuity_audio_caching_miss", test_sim_voice_continuity_audio_caching_miss, "Process Tests"),
    ("test_sim_voice_continuity_audio_pipeline_abort", test_sim_voice_continuity_audio_pipeline_abort, "Process Tests"),
    ("test_sim_voice_continuity_reconciliation_retry", test_sim_voice_continuity_reconciliation_retry, "Process Tests"),
    ("test_sim_voice_continuity_audio_overlap_correction", test_sim_voice_continuity_audio_overlap_correction, "Process Tests"),
    ("test_sim_voice_continuity_duration_extrapolation", test_sim_voice_continuity_duration_extrapolation, "Process Tests"),
    ("test_sim_voice_continuity_excessive_silence_fill", test_sim_voice_continuity_excessive_silence_fill, "Process Tests"),
    ("test_sim_voice_continuity_tts_failures_recovery", test_sim_voice_continuity_tts_failures_recovery, "Process Tests"),
    ("test_sim_voice_continuity_corrupt_wav_reconstruction", test_sim_voice_continuity_corrupt_wav_reconstruction, "Process Tests"),
    ("test_sim_voice_continuity_audio_duration_rounding", test_sim_voice_continuity_audio_duration_rounding, "Process Tests"),
    ("test_sim_voice_continuity_loudness_normalizer_speed", test_sim_voice_continuity_loudness_normalizer_speed, "Process Tests"),
    ("test_sim_voice_continuity_voice_agent_state_restoration", test_sim_voice_continuity_voice_agent_state_restoration, "Process Tests"),
    ("test_sim_voice_continuity_audio_subtrack_offsets", test_sim_voice_continuity_audio_subtrack_offsets, "Process Tests"),
    ("test_sim_voice_continuity_ffmpeg_error_handling", test_sim_voice_continuity_ffmpeg_error_handling, "Process Tests"),
    ("test_sim_voice_continuity_audio_channel_mixdown", test_sim_voice_continuity_audio_channel_mixdown, "Process Tests"),

    # OTIO Assembly Expanded Tests (37)
    ("test_sim_otio_assembly_track_creation", test_sim_otio_assembly_track_creation, "Process Tests"),
    ("test_sim_otio_assembly_multi_scene_clips", test_sim_otio_assembly_multi_scene_clips, "Process Tests"),
    ("test_sim_otio_assembly_timeline_cascade", test_sim_otio_assembly_timeline_cascade, "Process Tests"),
    ("test_sim_otio_assembly_delete_scene_updates", test_sim_otio_assembly_delete_scene_updates, "Process Tests"),
    ("test_sim_otio_assembly_reorder_scenes_updates", test_sim_otio_assembly_reorder_scenes_updates, "Process Tests"),
    ("test_sim_otio_assembly_script_to_slots", test_sim_otio_assembly_script_to_slots, "Process Tests"),
    ("test_sim_otio_assembly_merge_delivered_clips", test_sim_otio_assembly_merge_delivered_clips, "Process Tests"),
    ("test_sim_otio_assembly_missing_media_fallback", test_sim_otio_assembly_missing_media_fallback, "Process Tests"),
    ("test_sim_otio_assembly_timeline_serialization", test_sim_otio_assembly_timeline_serialization, "Process Tests"),
    ("test_sim_otio_assembly_timeline_deserialization", test_sim_otio_assembly_timeline_deserialization, "Process Tests"),
    ("test_sim_otio_assembly_track_overlap_detection", test_sim_otio_assembly_track_overlap_detection, "Process Tests"),
    ("test_sim_otio_assembly_transition_effects", test_sim_otio_assembly_transition_effects, "Process Tests"),
    ("test_sim_otio_assembly_empty_timeline_validation", test_sim_otio_assembly_empty_timeline_validation, "Process Tests"),
    ("test_sim_otio_assembly_audio_video_sync", test_sim_otio_assembly_audio_video_sync, "Process Tests"),
    ("test_sim_otio_assembly_frame_rate_conformance", test_sim_otio_assembly_frame_rate_conformance, "Process Tests"),
    ("test_sim_otio_assembly_subclip_extraction", test_sim_otio_assembly_subclip_extraction, "Process Tests"),
    ("test_sim_otio_assembly_timeline_validation_errors", test_sim_otio_assembly_timeline_validation_errors, "Process Tests"),
    ("test_sim_otio_assembly_metadata_preservation", test_sim_otio_assembly_metadata_preservation, "Process Tests"),
    ("test_sim_otio_assembly_video_agent_queueing", test_sim_otio_assembly_video_agent_queueing, "Process Tests"),
    ("test_sim_otio_assembly_otio_schema_compliance", test_sim_otio_assembly_otio_schema_compliance, "Process Tests"),
    ("test_sim_otio_assembly_concurrent_timeline_updates", test_sim_otio_assembly_concurrent_timeline_updates, "Process Tests"),
    ("test_sim_otio_assembly_timeline_diffing", test_sim_otio_assembly_timeline_diffing, "Process Tests"),
    ("test_sim_otio_assembly_track_deletion", test_sim_otio_assembly_track_deletion, "Process Tests"),
    ("test_sim_otio_assembly_media_reference_resolution", test_sim_otio_assembly_media_reference_resolution, "Process Tests"),
    ("test_sim_otio_assembly_gap_insertion_alignment", test_sim_otio_assembly_gap_insertion_alignment, "Process Tests"),
    ("test_sim_otio_assembly_timeline_rendering", test_sim_otio_assembly_timeline_rendering, "Process Tests"),
    ("test_sim_otio_assembly_track_renaming", test_sim_otio_assembly_track_renaming, "Process Tests"),
    ("test_sim_otio_assembly_invalid_media_duration", test_sim_otio_assembly_invalid_media_duration, "Process Tests"),
    ("test_sim_otio_assembly_marker_addition_retrieval", test_sim_otio_assembly_marker_addition_retrieval, "Process Tests"),
    ("test_sim_otio_assembly_timeline_flattening", test_sim_otio_assembly_timeline_flattening, "Process Tests"),
    ("test_sim_otio_assembly_video_clip_transcoding", test_sim_otio_assembly_video_clip_transcoding, "Process Tests"),
    ("test_sim_otio_assembly_audio_track_layering", test_sim_otio_assembly_audio_track_layering, "Process Tests"),
    ("test_sim_otio_assembly_timeline_resolution_drift", test_sim_otio_assembly_timeline_resolution_drift, "Process Tests"),
    ("test_sim_otio_assembly_clip_boundary_clipping", test_sim_otio_assembly_clip_boundary_clipping, "Process Tests"),
    ("test_sim_otio_assembly_timeline_split", test_sim_otio_assembly_timeline_split, "Process Tests"),
    ("test_sim_otio_assembly_unaligned_tracks_report", test_sim_otio_assembly_unaligned_tracks_report, "Process Tests"),
    ("test_sim_otio_assembly_final_render_validation", test_sim_otio_assembly_final_render_validation, "Process Tests"),

    # 3. Maximum Capacity
    ("Maximum Capacity Test", test_max_capacity_pipeline.run_test, "Maximum Capacity"),

    # 4. BDD Integration Tests
    ("test_bdd_tts_fleet_cold_start", test_bdd_tts_fleet_cold_start, "BDD Integration"),
    ("test_bdd_single_block_tts_inference", test_bdd_single_block_tts_inference, "BDD Integration"),
    ("test_bdd_multi_block_tts_reconciliation", test_bdd_multi_block_tts_reconciliation, "BDD Integration"),
    ("test_bdd_voice_continuity_across_scenes", test_bdd_voice_continuity_across_scenes, "BDD Integration"),
    ("test_bdd_ltx_fleet_scale_up", test_bdd_ltx_fleet_scale_up, "BDD Integration"),
    ("test_bdd_single_clip_video_generation", test_bdd_single_clip_video_generation, "BDD Integration"),
    ("test_bdd_multi_scene_video_otio_assembly", test_bdd_multi_scene_video_otio_assembly, "BDD Integration"),
    ("test_bdd_audio_video_duration_alignment", test_bdd_audio_video_duration_alignment, "BDD Integration"),
    ("test_bdd_tts_retry_after_failure", test_bdd_tts_retry_after_failure, "BDD Integration"),
    ("test_bdd_vm_preemption_recovery", test_bdd_vm_preemption_recovery, "BDD Integration"),
    ("test_bdd_budget_gated_provisioning", test_bdd_budget_gated_provisioning, "BDD Integration"),
    ("test_bdd_script_revision_selective_requeue", test_bdd_script_revision_selective_requeue, "BDD Integration"),
    ("test_bdd_final_assembly_real_media", test_bdd_final_assembly_real_media, "BDD Integration"),
    ("test_bdd_partial_failure_isolated_recovery", test_bdd_partial_failure_isolated_recovery, "BDD Integration"),
    ("test_bdd_full_fleet_teardown_cost_accounting", test_bdd_full_fleet_teardown_cost_accounting, "BDD Integration"),
    ("test_perplexity_verify_live", test_perplexity_verify_live, "BDD Integration"),
]

# State tracking
suite_status = "idle"
suite_completed = False
stats = {"passed": 0, "failed": 0, "skipped": 0, "pending": len(TEST_CASES)}
history = []
active_test_name = "Ready"
active_category = "Suite"
start_time = time.time()
end_time = None
auto_close_countdown = 10
ai_summary = "AI Status Copilot analysis pending logs..."

# Load DeepSeek API key
DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
api_key = None
if os.path.exists(DEEPSEEK_KEY_PATH):
    try:
        with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
    except Exception:
        pass

# Interactive states
test_status = {}
test_logs = {}
bdd_verdicts_dict = {}
user_interacted = False
keep_alive = False
current_running_test = None
run_queue = [name for name, _, _ in TEST_CASES]  # Default runs all on startup

for name, _, _ in TEST_CASES:
    test_status[name] = {"status": "pending", "elapsed": 0.0, "error": None, "events": [], "db_path": None}
    test_logs[name] = []

def get_llm_summary(recent_logs: str) -> str:
    if not api_key:
        return "DeepSeek API key not found. AI Copilot summary disabled."
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    system_prompt = (
        "You are an assistant describing a running test suite. "
        "Review the recent log chunk and summarize what the system is currently doing in one short, clear, friendly sentence. "
        "Focus on the active operation (e.g. provisioning a VM, normalising audio loudness, running BDD assertions). "
        "Be extremely concise (max 15 words) and do not explain the code itself."
    )
    import re
    clean_logs = re.sub(r'<[^>]+>', '', recent_logs)
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Logs:\n{clean_logs[-4000:]}"}
        ],
        "temperature": 0.3,
        "max_tokens": 60
    }
    try:
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            return f"AI Copilot: Busy monitoring runs (HTTP {resp.status_code})"
    except Exception:
        return "AI Copilot: Analyzing execution pipeline..."

def run_subagent_audit(test_name: str) -> dict:
    if not api_key:
        return {"verdict": "FAIL", "reasoning": "DeepSeek API key not found. Congruence audit disabled."}

    # 1. Read documentation
    docs_content = ""
    
    # Try reading simulation_coverage_definition.md from brain dir
    brain_dir = None
    try:
        brain_root = pathlib.Path("/Users/orpington/.gemini/antigravity/brain")
        conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        if conv_id and (brain_root / conv_id).exists():
            brain_dir = brain_root / conv_id
        else:
            # fallback to newest
            newest_mtime = 0
            for subdir in brain_root.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    state_file = subdir / ".lock_state"
                    mtime = state_file.stat().st_mtime if state_file.exists() else subdir.stat().st_mtime
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                        brain_dir = subdir
    except Exception:
        pass

    if brain_dir:
        cov_def_path = brain_dir / "simulation_coverage_definition.md"
        if cov_def_path.exists():
            try:
                docs_content += "=== DOCUMENTATION: simulation_coverage_definition.md ===\n"
                docs_content += cov_def_path.read_text(encoding="utf-8") + "\n\n"
            except Exception:
                pass

    # Also check if there's tests/units/simulation_covers_implementation_plan.md
    impl_plan_path = PROJECT_ROOT / "tests" / "units" / "simulation_covers_implementation_plan.md"
    if impl_plan_path.exists():
        try:
            docs_content += "=== DOCUMENTATION: simulation_covers_implementation_plan.md ===\n"
            docs_content += impl_plan_path.read_text(encoding="utf-8") + "\n\n"
        except Exception:
            pass

    # If no doc content was read, let's search for any other markdown file in docs/ or tests/
    if not docs_content:
        docs_content = "No specific documentation file found."

    # 2. Read test code file
    test_code = ""
    # Map test name to code file
    test_file_path = PROJECT_ROOT / "tests" / "units" / f"{test_name}.py"
    if test_name == "Maximum Capacity Test":
        test_file_path = PROJECT_ROOT / "tests" / "units" / "test_max_capacity_pipeline.py"

    if test_file_path.exists():
        try:
            test_code = test_file_path.read_text(encoding="utf-8")
        except Exception as e:
            test_code = f"Error reading test file: {e}"
    else:
        test_code = f"Test file {test_file_path} not found on disk."

    # 3. Call DeepSeek
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    system_prompt = (
        "You are a fresh QA subagent auditor verifying test congruence.\n"
        "Your task is to compare the implementation of a specific test case (Python code) "
        "with the official documentation/specification (like Given/When/Then requirements, BDD scenarios, and coverage plans).\n"
        "Verify if the test code is congruent with the documentation. Check for:\n"
        "1. Does the test code actually implement the scenario described in the docs?\n"
        "2. Are the assertions, events, and logic in the test code matching the requirements in the docs?\n"
        "3. Is there any mismatch, mocking where real behavior is required, or missing verification steps?\n"
        "Note: The tests are integration/unit tests. If they use mocks or simulators where the docs specify real behavior, point that out.\n\n"
        "Respond with EXACTLY a JSON object containing the keys 'verdict' (must be either 'PASS' or 'FAIL') "
        "and 'reasoning' (a detailed explanation of the verdict). "
        "Do not include any explanation or markdown backticks outside the JSON."
    )
    
    user_prompt = (
        f"Test Case: {test_name}\n\n"
        f"--- DOCUMENTATION ---\n{docs_content}\n\n"
        f"--- TEST CODE ---\n{test_code}\n\n"
        f"Evaluate the congruence of the test code with the documentation and output JSON."
    )
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 1000
    }
    
    try:
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=60.0)
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            # Strip markdown fences
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            res = json.loads(raw)
            if "verdict" in res and "reasoning" in res:
                return res
            return {"verdict": "FAIL", "reasoning": f"Invalid response format from LLM: {raw}"}
        else:
            return {"verdict": "FAIL", "reasoning": f"DeepSeek API request failed (HTTP {resp.status_code})"}
    except Exception as e:
        return {"verdict": "FAIL", "reasoning": f"Error during subagent audit execution: {e}"}

def trigger_ai_summary_refresh():
    def run():
        global ai_summary
        log_chunk = "".join(logs_list[-50:])
        if log_chunk:
            ai_summary = get_llm_summary(log_chunk)
    threading.Thread(target=run, daemon=True).start()

def ai_summary_worker():
    global ai_summary
    time.sleep(3) # Initial delay
    while not suite_completed or run_queue:
        log_chunk = "".join(logs_list[-50:])
        if log_chunk:
            ai_summary = get_llm_summary(log_chunk)
        # Sleep 30 seconds
        for _ in range(30):
            if suite_completed and not run_queue:
                break
            time.sleep(1)

# Custom stream wrapper to capture stdout/stderr in real-time
class LiveStreamCapture:
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, s):
        if s:
            log_queue.put((current_running_test, s))
            if self.original_stream:
                self.original_stream.write(s)
                self.original_stream.flush()
        return len(s)

    def flush(self):
        if self.original_stream:
            self.original_stream.flush()

    def isatty(self):
        return getattr(self.original_stream, "isatty", lambda: False)()

original_stdout = sys.stdout
original_stderr = sys.stderr

def log_processor():
    while True:
        try:
            test_name, item = log_queue.get()
            cleaned = (
                item.replace("\033[92m", '<span style="color: #34d399;">')
                    .replace("\033[91m", '<span style="color: #f87171;">')
                    .replace("\033[93m", '<span style="color: #fbbf24;">')
                    .replace("\033[95m", '<span style="color: #c084fc;">')
                    .replace("\033[96m", '<span style="color: #22d3ee;">')
                    .replace("\033[36m", '<span style="color: #22d3ee;">')
                    .replace("\033[34m", '<span style="color: #60a5fa;">')
                    .replace("\033[33m", '<span style="color: #fbbf24;">')
                    .replace("\033[35m", '<span style="color: #f472b6;">')
                    .replace("\033[32m", '<span style="color: #34d399;">')
                    .replace("\033[31m", '<span style="color: #f87171;">')
                    .replace("\033[0m", '</span>')
            )
            logs_list.append(cleaned)
            if test_name:
                if test_name not in test_logs:
                    test_logs[test_name] = []
                test_logs[test_name].append(cleaned)
        except Exception:
            time.sleep(0.01)

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Master Suite Test Runner GUI</title>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Outfit:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #060913;
            --card-bg: rgba(13, 20, 38, 0.45);
            --border-color: rgba(255, 255, 255, 0.05);
            --border-color-glow: rgba(99, 102, 241, 0.15);
            --text-primary: #f8fafc;
            --text-secondary: #cbd5e1;
            --text-muted: #64748b;
            --accent-primary: #6366f1;
            --accent-primary-hover: #4f46e5;
            --accent-primary-glow: rgba(99, 102, 241, 0.15);
            
            --accent-passed: #10b981;
            --accent-passed-glow: rgba(16, 185, 129, 0.15);
            --accent-failed: #ef4444;
            --accent-failed-glow: rgba(239, 68, 68, 0.15);
            --accent-warn: #fbbf24;
            --accent-warn-glow: rgba(251, 191, 36, 0.15);
            --accent-pending: #475569;
        }

        body {
            background: radial-gradient(circle at 50% 0%, #171738 0%, var(--bg-color) 80%);
            color: var(--text-primary);
            font-family: 'Plus Jakarta Sans', 'Outfit', sans-serif;
            margin: 0;
            padding: 1.5rem;
            height: 100vh;
            box-sizing: border-box;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }

        .dashboard {
            background: var(--card-bg);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 1.5rem;
            width: 96vw;
            height: 94vh;
            max-width: 1600px;
            box-shadow: 0 30px 60px -15px rgba(0, 0, 0, 0.7);
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            position: relative;
            box-sizing: border-box;
        }

        /* Top Header Bar */
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 0.75rem;
        }

        .header-left h1 {
            margin: 0;
            font-size: 1.6rem;
            font-weight: 800;
            background: linear-gradient(to right, #a5b4fc, #e0e7ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .header-left p {
            margin: 4px 0 0 0;
            font-size: 0.8rem;
            color: var(--text-muted);
        }

        .header-right {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        /* Status Counters */
        .counters {
            display: flex;
            gap: 0.5rem;
        }

        .counter-card {
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 10px;
            padding: 6px 14px;
            text-align: center;
            min-width: 50px;
            transition: all 0.3s ease;
        }

        .counter-val {
            font-size: 1.1rem;
            font-weight: 700;
        }

        .counter-label {
            font-size: 0.6rem;
            text-transform: uppercase;
            color: var(--text-muted);
            font-weight: 600;
            margin-top: 1px;
        }

        /* Status Badge for Entire Suite */
        .status-pill {
            padding: 6px 14px;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 800;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            transition: all 0.3s ease;
        }

        .status-idle {
            background: rgba(71, 85, 105, 0.1);
            color: #94a3b8;
            border: 1px solid rgba(71, 85, 105, 0.3);
        }

        .status-running {
            background: rgba(99, 102, 241, 0.15);
            color: #818cf8;
            border: 1px solid rgba(99, 102, 241, 0.35);
            box-shadow: 0 0 12px rgba(99, 102, 241, 0.2);
            animation: glow-pulse 1.8s infinite;
        }

        .status-passed {
            background: rgba(16, 185, 129, 0.12);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.3);
            box-shadow: 0 0 12px rgba(16, 185, 129, 0.15);
        }

        .status-failed {
            background: rgba(239, 68, 68, 0.12);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.3);
            box-shadow: 0 0 12px rgba(239, 68, 68, 0.15);
        }

        @keyframes glow-pulse {
            0% { box-shadow: 0 0 0 0 rgba(99, 102, 241, 0.3); }
            70% { box-shadow: 0 0 0 8px rgba(99, 102, 241, 0); }
            100% { box-shadow: 0 0 0 0 rgba(99, 102, 241, 0); }
        }

        /* Progress Bar */
        .progress-container {
            display: flex;
            align-items: center;
            gap: 12px;
            background: rgba(15, 23, 42, 0.4);
            border: 1px solid rgba(255, 255, 255, 0.03);
            padding: 6px 12px;
            border-radius: 12px;
        }

        .progress-bar {
            flex: 1;
            height: 8px;
            background: #080b12;
            border-radius: 999px;
            overflow: hidden;
            display: flex;
        }

        .progress-fill-passed { background: var(--accent-passed); transition: width 0.4s ease; }
        .progress-fill-failed { background: var(--accent-failed); transition: width 0.4s ease; }
        .progress-fill-skipped { background: var(--accent-warn); transition: width 0.4s ease; }

        .progress-label {
            font-size: 0.75rem;
            font-weight: 700;
            color: var(--text-secondary);
            min-width: 90px;
            text-align: right;
        }

        /* Main Workspace Split */
        .workspace {
            display: grid;
            grid-template-columns: 350px 1fr;
            gap: 1.25rem;
            flex: 1;
            min-height: 0;
        }

        /* Sidebar - Left Column */
        .sidebar {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            height: 100%;
            min-height: 0;
        }

        .controls-box {
            background: rgba(20, 28, 54, 0.5);
            border: 1px solid var(--border-color);
            padding: 0.85rem;
            border-radius: 16px;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .sidebar-actions-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px;
        }

        .btn-run-all {
            background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
            border: none;
            color: #fff;
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 0.75rem;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            box-shadow: 0 4px 10px rgba(99, 102, 241, 0.25);
            transition: all 0.2s;
        }

        .btn-run-all:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 14px rgba(99, 102, 241, 0.35);
            background: linear-gradient(135deg, #7c3aed 0%, #6366f1 100%);
        }

        .btn-secondary {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: var(--text-secondary);
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 0.75rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }

        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.1);
            color: #fff;
            border-color: rgba(255, 255, 255, 0.2);
        }

        .search-bar {
            background: #05080e;
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: #fff;
            padding: 8px 12px;
            border-radius: 8px;
            font-family: inherit;
            font-size: 0.8rem;
        }

        .search-bar:focus {
            outline: none;
            border-color: var(--accent-primary);
            box-shadow: 0 0 8px rgba(99, 102, 241, 0.15);
        }

        /* Category quick chips */
        .category-filters {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
        }

        .chip {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.05);
            color: var(--text-muted);
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 0.65rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s;
        }

        .chip:hover {
            background: rgba(255, 255, 255, 0.08);
            color: var(--text-secondary);
        }

        .chip.active {
            background: rgba(99, 102, 241, 0.15);
            border-color: rgba(99, 102, 241, 0.3);
            color: #a5b4fc;
        }

        .chip-failed.active {
            background: rgba(239, 68, 68, 0.15);
            border-color: rgba(239, 68, 68, 0.3);
            color: #f87171;
        }

        /* Test List Sidebar Scroll */
        .test-list-container {
            flex: 1;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 0.85rem;
            padding-right: 4px;
        }

        .test-group {
            display: flex;
            flex-direction: column;
            gap: 3px;
        }

        .test-group-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 4px;
            margin-bottom: 2px;
        }

        .test-group-title {
            font-size: 0.65rem;
            font-weight: 800;
            text-transform: uppercase;
            color: var(--text-muted);
            letter-spacing: 0.08em;
        }

        .test-group-select-all {
            font-size: 0.6rem;
            font-weight: 700;
            color: var(--accent-primary);
            cursor: pointer;
            background: none;
            border: none;
            padding: 0;
        }

        .test-group-select-all:hover {
            text-decoration: underline;
        }

        .test-row {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 10px;
            border-radius: 8px;
            background: rgba(20, 28, 54, 0.25);
            border: 1px solid rgba(255, 255, 255, 0.02);
            cursor: pointer;
            position: relative;
            transition: all 0.2s;
            box-sizing: border-box;
        }

        .test-row:hover {
            background: rgba(255, 255, 255, 0.03);
            border-color: rgba(255, 255, 255, 0.06);
        }

        .test-row.active {
            background: rgba(99, 102, 241, 0.06);
            border-color: rgba(99, 102, 241, 0.25);
        }

        .test-row.active .test-name {
            color: #fff;
            font-weight: 600;
        }

        .test-checkbox {
            cursor: pointer;
            width: 14px;
            height: 14px;
            accent-color: var(--accent-primary);
            margin: 0;
            flex-shrink: 0;
        }

        .test-name {
            flex: 1;
            font-size: 0.75rem;
            font-family: 'Fira Code', monospace;
            color: var(--text-secondary);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .test-status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .dot-pending { background: var(--accent-pending); }
        .dot-running { background: #818cf8; box-shadow: 0 0 8px #818cf8; animation: pulse-dot-active 1.4s infinite; }
        .dot-passed { background: var(--accent-passed); box-shadow: 0 0 6px rgba(16, 185, 129, 0.4); }
        .dot-failed { background: var(--accent-failed); box-shadow: 0 0 6px rgba(239, 68, 68, 0.4); }
        .dot-skipped { background: var(--accent-warn); }

        @keyframes pulse-dot-active {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.4; transform: scale(0.9); }
        }

        .test-play-btn {
            opacity: 0;
            background: none;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            padding: 3px;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
        }

        .test-row:hover .test-play-btn {
            opacity: 1;
        }

        .test-play-btn:hover {
            color: #fff;
            background: rgba(255, 255, 255, 0.08);
        }

        /* Right Panel: Selected Test Details or Dashboard */
        .main-panel {
            display: flex;
            flex-direction: column;
            height: 100%;
            min-height: 0;
            background: rgba(10, 15, 30, 0.4);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 1.25rem;
            box-sizing: border-box;
        }

        /* Welcome / Default Suite Overview Dashboard */
        .dashboard-overview {
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            height: 100%;
            min-height: 0;
            overflow-y: auto;
        }

        .welcome-pane {
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            height: 100%;
            color: var(--text-secondary);
            text-align: center;
            gap: 8px;
            padding: 2rem;
        }

        .welcome-icon {
            font-size: 3rem;
            animation: float-icon 4s ease-in-out infinite;
        }

        @keyframes float-icon {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-8px); }
        }

        .ai-copilot-banner {
            display: flex;
            align-items: center;
            gap: 10px;
            background: rgba(99, 102, 241, 0.05);
            border: 1px solid rgba(99, 102, 241, 0.15);
            padding: 10px 14px;
            border-radius: 12px;
            font-size: 0.8rem;
            color: #a5b4fc;
        }

        .ai-copilot-icon {
            font-size: 1.1rem;
            flex-shrink: 0;
        }

        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 1rem;
        }

        .stat-card {
            background: rgba(20, 28, 54, 0.35);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 1.25rem;
            text-align: center;
            transition: all 0.3s ease;
        }

        .stat-card:hover {
            border-color: rgba(255, 255, 255, 0.08);
            transform: translateY(-2px);
        }

        .stat-card.passed { border-left: 4px solid var(--accent-passed); }
        .stat-card.failed { border-left: 4px solid var(--accent-failed); }
        .stat-card.skipped { border-left: 4px solid var(--accent-warn); }
        .stat-card.total { border-left: 4px solid var(--accent-primary); }

        .stat-num {
            font-size: 2.2rem;
            font-weight: 800;
            font-family: 'Outfit', sans-serif;
            margin-bottom: 4px;
        }

        .stat-name {
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            color: var(--text-muted);
            letter-spacing: 0.05em;
        }

        /* Detailed Selected Test View */
        .test-details-container {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            height: 100%;
            min-height: 0;
        }

        .test-details-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.015);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 12px;
            padding: 0.75rem 1rem;
        }

        .details-title-section {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .category-tag {
            font-size: 0.6rem;
            font-weight: 800;
            text-transform: uppercase;
            color: var(--accent-primary);
            letter-spacing: 0.08em;
        }

        #detail-name {
            margin: 0;
            font-size: 1.15rem;
            font-weight: 700;
            font-family: 'Fira Code', monospace;
            color: #fff;
        }

        .details-actions {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .detail-time {
            font-size: 0.8rem;
            color: var(--text-muted);
            font-weight: 500;
        }

        .btn-run-single {
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid rgba(99, 102, 241, 0.25);
            color: #a5b4fc;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.7rem;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 5px;
            transition: all 0.2s;
        }

        .btn-run-single:hover {
            background: var(--accent-primary);
            color: #fff;
            border-color: var(--accent-primary);
        }

        /* 2-Column Details Grid */
        .test-details-grid {
            display: grid;
            grid-template-columns: 1.1fr 0.9fr;
            gap: 1rem;
            flex: 1;
            min-height: 0;
        }

        .details-left-col {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            min-height: 0;
            overflow-y: auto;
            padding-right: 4px;
        }

        .details-right-col {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            min-height: 0;
        }

        .info-card {
            background: rgba(20, 28, 54, 0.2);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 16px;
            padding: 1rem;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            box-sizing: border-box;
        }

        .card-title {
            font-size: 0.75rem;
            font-weight: 800;
            text-transform: uppercase;
            color: var(--text-muted);
            letter-spacing: 0.05em;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            padding-bottom: 6px;
            margin-bottom: 2px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        /* Specification block (Given, When, Then) */
        .spec-container {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .spec-row {
            display: flex;
            gap: 8px;
            font-size: 0.8rem;
            line-height: 1.4;
        }

        .spec-label {
            font-weight: 800;
            color: #818cf8;
            width: 45px;
            flex-shrink: 0;
            text-transform: uppercase;
            font-size: 0.75rem;
        }

        .spec-val {
            color: var(--text-secondary);
        }

        /* BDD Verdict Layout */
        .bdd-verdict-grid {
            display: grid;
            grid-template-columns: auto 1fr;
            gap: 14px;
            align-items: center;
        }

        /* Confidence Circle Gauge */
        .confidence-gauge {
            position: relative;
            width: 70px;
            height: 70px;
            flex-shrink: 0;
        }

        .gauge-svg {
            width: 100%;
            height: 100%;
            transform: rotate(-90deg);
        }

        .gauge-bg {
            fill: none;
            stroke: rgba(255, 255, 255, 0.05);
            stroke-width: 3.5;
        }

        .gauge-fill {
            fill: none;
            stroke: url(#confidence-grad);
            stroke-width: 3.5;
            stroke-linecap: round;
            transition: stroke-dasharray 0.8s ease-in-out;
        }

        .gauge-value {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-size: 0.9rem;
            font-weight: 800;
            color: #fff;
        }

        .verdict-header-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .badge-large {
            padding: 3px 10px;
            border-radius: 6px;
            font-size: 0.7rem;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        .badge-large.passed, .badge-large.pass { background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.3); color: #34d399; }
        .badge-large.failed, .badge-large.fail { background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; }
        .badge-large.running { background: rgba(99, 102, 241, 0.12); border: 1px solid rgba(99, 102, 241, 0.3); color: #818cf8; }
        .badge-large.pending { background: rgba(71, 85, 105, 0.12); border: 1px solid rgba(71, 85, 105, 0.3); color: #94a3b8; }
        .badge-large.skipped, .badge-large.warn { background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.3); color: #fbbf24; }

        .verdict-reasoning {
            font-size: 0.8rem;
            line-height: 1.45;
            color: var(--text-secondary);
            background: rgba(255, 255, 255, 0.015);
            padding: 8px 12px;
            border-radius: 8px;
            border: 1px dashed rgba(255, 255, 255, 0.05);
            overflow-y: auto;
            max-height: 90px;
        }

        .issues-title {
            font-size: 0.7rem;
            font-weight: 700;
            color: #fbbf24;
            margin: 4px 0;
            text-transform: uppercase;
        }

        .issues-list {
            margin: 0;
            padding-left: 16px;
            font-size: 0.75rem;
            color: #fcd34d;
        }

        /* Event Timeline Card */
        .timeline-card {
            flex: 1;
            min-height: 0;
        }

        .timeline-scroll {
            overflow-y: auto;
            flex: 1;
            padding-right: 4px;
        }

        /* Timeline stepper styling */
        .timeline-container {
            display: flex;
            flex-direction: column;
            gap: 8px;
            padding: 4px;
        }

        .timeline-item {
            display: flex;
            gap: 12px;
            position: relative;
            padding-bottom: 8px;
        }

        .timeline-item:not(:last-child)::after {
            content: '';
            position: absolute;
            left: 21px;
            top: 22px;
            bottom: -8px;
            width: 2px;
            background: rgba(255, 255, 255, 0.04);
        }

        .timeline-icon-container {
            width: 42px;
            display: flex;
            flex-direction: column;
            align-items: center;
            flex-shrink: 0;
        }

        .timeline-seq {
            background: #0d1326;
            border: 1px solid rgba(255, 255, 255, 0.06);
            color: var(--text-muted);
            border-radius: 4px;
            padding: 1px 4px;
            font-size: 0.55rem;
            font-family: 'Fira Code', monospace;
            margin-bottom: 2px;
        }

        .timeline-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--accent-primary);
            border: 2px solid var(--bg-color);
            z-index: 1;
        }

        .timeline-dot.passed { background: var(--accent-passed); }
        .timeline-dot.failed { background: var(--accent-failed); }
        .timeline-dot.warn { background: var(--accent-warn); }

        .timeline-content-card {
            flex: 1;
            background: rgba(255, 255, 255, 0.012);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 10px;
            padding: 8px 10px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .timeline-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.7rem;
        }

        .timeline-kind {
            font-weight: 700;
            color: #fff;
            font-family: 'Fira Code', monospace;
        }

        .timeline-meta {
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .timeline-agent {
            font-size: 0.55rem;
            font-weight: 700;
            text-transform: uppercase;
            color: var(--accent-primary);
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid rgba(99, 102, 241, 0.15);
            padding: 1px 4px;
            border-radius: 3px;
        }

        .timeline-time {
            color: var(--text-muted);
        }

        .timeline-body {
            font-size: 0.75rem;
            color: var(--text-secondary);
            line-height: 1.35;
        }

        .timeline-body code {
            background: #070a14;
            padding: 1px 4px;
            border-radius: 3px;
            font-family: 'Fira Code', monospace;
            color: #cbd5e1;
            font-size: 0.7rem;
        }

        .timeline-body pre {
            margin: 4px 0 0 0;
            background: #02040a;
            border: 1px solid rgba(255, 255, 255, 0.02);
            border-radius: 6px;
            padding: 4px 8px;
            font-family: 'Fira Code', monospace;
            font-size: 0.7rem;
            color: #a5b4fc;
            overflow-x: auto;
            max-height: 60px;
        }

        /* Error/Traceback Banner */
        .callout-error {
            background: rgba(239, 68, 68, 0.04);
            border: 1px solid rgba(239, 68, 68, 0.2);
            border-left: 4px solid var(--accent-failed);
            border-radius: 12px;
            padding: 0.75rem;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .error-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .error-title {
            font-size: 0.75rem;
            color: #ef4444;
            font-weight: 700;
        }

        .error-message {
            font-size: 0.75rem;
            color: #fca5a5;
            font-family: 'Fira Code', monospace;
            word-break: break-all;
        }

        .traceback-pre {
            margin: 0;
            font-family: 'Fira Code', monospace;
            font-size: 0.7rem;
            white-space: pre-wrap;
            color: #fda4af;
            background: #070205;
            padding: 8px;
            border-radius: 6px;
            border: 1px solid rgba(239, 68, 68, 0.15);
            max-height: 120px;
            overflow-y: auto;
        }

        /* Terminal card - bottom row */
        .log-card {
            height: 180px;
            flex-shrink: 0;
        }

        .log-card-title {
            margin-bottom: 4px;
        }

        .log-controls {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .control-label {
            font-size: 0.65rem;
            color: var(--text-muted);
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 4px;
            font-weight: 500;
            text-transform: none;
        }

        .control-label input {
            cursor: pointer;
            margin: 0;
        }

        .terminal {
            background: #03050c;
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 10px;
            padding: 8px 12px;
            font-family: 'Fira Code', monospace;
            font-size: 0.75rem;
            flex: 1;
            overflow-y: auto;
            color: #cbd5e1;
            white-space: pre-wrap;
            word-break: break-all;
            line-height: 1.4;
            box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.6);
        }

        /* Scrollbars styling */
        .terminal::-webkit-scrollbar, 
        .test-list-container::-webkit-scrollbar,
        .timeline-scroll::-webkit-scrollbar,
        .traceback-pre::-webkit-scrollbar,
        .details-left-col::-webkit-scrollbar,
        .verdict-reasoning::-webkit-scrollbar {
            width: 5px;
            height: 5px;
        }
        .terminal::-webkit-scrollbar-track,
        .test-list-container::-webkit-scrollbar-track,
        .timeline-scroll::-webkit-scrollbar-track,
        .traceback-pre::-webkit-scrollbar-track,
        .details-left-col::-webkit-scrollbar-track,
        .verdict-reasoning::-webkit-scrollbar-track {
            background: transparent;
        }
        .terminal::-webkit-scrollbar-thumb,
        .test-list-container::-webkit-scrollbar-thumb,
        .timeline-scroll::-webkit-scrollbar-thumb,
        .traceback-pre::-webkit-scrollbar-thumb,
        .details-left-col::-webkit-scrollbar-thumb,
        .verdict-reasoning::-webkit-scrollbar-thumb {
            background: #141b2d;
            border-radius: 4px;
        }
        .terminal::-webkit-scrollbar-thumb:hover,
        .test-list-container::-webkit-scrollbar-thumb:hover,
        .timeline-scroll::-webkit-scrollbar-thumb:hover,
        .traceback-pre::-webkit-scrollbar-thumb:hover,
        .details-left-col::-webkit-scrollbar-thumb:hover,
        .verdict-reasoning::-webkit-scrollbar-thumb:hover {
            background: #1e2942;
        }

        /* Countdown auto-close footer */
        .countdown-banner {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(99, 102, 241, 0.08);
            border: 1px solid rgba(99, 102, 241, 0.2);
            padding: 6px 12px;
            border-radius: 10px;
            font-size: 0.75rem;
            color: #a5b4fc;
        }

        .btn-keep-open {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: #fff;
            padding: 3px 8px;
            border-radius: 5px;
            font-size: 0.7rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-keep-open:hover {
            background: rgba(255, 255, 255, 0.12);
        }
    </style>
</head>
<body>
    <!-- Hidden SVGs for gradients -->
    <svg style="width:0;height:0;position:absolute;" aria-hidden="true" focusable="false">
        <linearGradient id="confidence-grad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#10b981" />
            <stop offset="100%" stop-color="#3b82f6" />
        </linearGradient>
    </svg>

    <div class="dashboard">
        <!-- Header -->
        <div class="header">
            <div class="header-left">
                <h1>🧪 Documentary Pipeline Suite</h1>
                <p id="suite-summary-desc">Interactive execution runner and live BDD verification judge</p>
            </div>
            <div class="header-right">
                <div class="counters">
                    <div class="counter-card" style="border-color: rgba(16,185,129,0.15);">
                        <div class="counter-val" style="color: var(--accent-passed);" id="count-passed">0</div>
                        <div class="counter-label">Passed</div>
                    </div>
                    <div class="counter-card" style="border-color: rgba(239,68,68,0.15);">
                        <div class="counter-val" style="color: var(--accent-failed);" id="count-failed">0</div>
                        <div class="counter-label">Failed</div>
                    </div>
                    <div class="counter-card" style="border-color: rgba(251,191,36,0.15);">
                        <div class="counter-val" style="color: var(--accent-warn);" id="count-skipped">0</div>
                        <div class="counter-label">Skipped</div>
                    </div>
                    <div class="counter-card">
                        <div class="counter-val" id="count-pending">0</div>
                        <div class="counter-label">Pending</div>
                    </div>
                </div>
                <div class="status-pill status-idle" id="suite-status-badge">IDLE</div>
            </div>
        </div>

        <!-- Progress Bar & AI Status -->
        <div class="progress-container">
            <div class="progress-bar">
                <div class="progress-fill-passed" id="bar-passed" style="width: 0%;"></div>
                <div class="progress-fill-failed" id="bar-failed" style="width: 0%;"></div>
                <div class="progress-fill-skipped" id="bar-skipped" style="width: 0%;"></div>
            </div>
            <div class="progress-label" id="progress-percent">0% Completed</div>
        </div>

        <!-- Main Workspace split -->
        <div class="workspace">
            <!-- Left panel: list of tests -->
            <div class="sidebar">
                <div class="controls-box">
                    <div class="sidebar-actions-grid">
                        <button class="btn-run-all" onclick="runSelectedTests()">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                            RUN SELECTED
                        </button>
                        <button class="btn-secondary" onclick="runFailedTests()">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"></path></svg>
                            RUN FAILED
                        </button>
                    </div>
                    <input type="text" class="search-bar" placeholder="Search tests..." id="search-input" oninput="applySidebarFilters()">
                    <div class="category-filters">
                        <button class="chip active" data-filter="all" onclick="filterCategory('all')">All</button>
                        <button class="chip" data-filter="Simulation Cover" onclick="filterCategory('Simulation Cover')">Covers</button>
                        <button class="chip" data-filter="BDD Integration" onclick="filterCategory('BDD Integration')">BDD</button>
                        <button class="chip" data-filter="Consequential Claims" onclick="filterCategory('Consequential Claims')">Claims</button>
                        <button class="chip" data-filter="Process Tests" onclick="filterCategory('Process Tests')">Process</button>
                        <button class="chip chip-failed" data-filter="failed" onclick="filterCategory('failed')">Failed</button>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <button class="test-group-select-all" onclick="selectAll(true)">Check All</button>
                        <button class="test-group-select-all" onclick="selectAll(false)">Clear Checkbox</button>
                        <button class="test-group-select-all" onclick="clearResultsFromServer()">Reset Status</button>
                    </div>
                </div>
                
                <div class="test-list-container" id="test-list-el">
                    <!-- Populated dynamically -->
                </div>
            </div>

            <!-- Right panel: detailed view & logs -->
            <div class="main-panel" id="main-panel-view">
                <!-- Fallback dashboard display -->
                <div class="dashboard-overview" id="pane-dashboard-overview">
                    <div class="ai-copilot-banner">
                        <span class="ai-copilot-icon">🤖</span>
                        <div class="ai-copilot-text" id="ai-summary-text">AI Status Copilot analysis pending logs...</div>
                    </div>
                    
                    <div class="dashboard-grid">
                        <div class="stat-card total">
                            <div class="stat-num" id="overview-total">0</div>
                            <div class="stat-name">Total Tests</div>
                        </div>
                        <div class="stat-card passed">
                            <div class="stat-num" id="overview-passed" style="color: var(--accent-passed);">0</div>
                            <div class="stat-name">Passed</div>
                        </div>
                        <div class="stat-card failed">
                            <div class="stat-num" id="overview-failed" style="color: var(--accent-failed);">0</div>
                            <div class="stat-name">Failed</div>
                        </div>
                        <div class="stat-card skipped">
                            <div class="stat-num" id="overview-skipped" style="color: var(--accent-warn);">0</div>
                            <div class="stat-name">Skipped</div>
                        </div>
                    </div>

                    <div class="welcome-pane">
                        <div class="welcome-icon">🧪</div>
                        <h3 style="margin: 0.5rem 0 0.25rem 0;">No Test Selected</h3>
                        <p style="max-width: 450px; font-size: 0.8rem; line-height: 1.45; margin: 0;">
                            Select any test case from the sidebar to inspect its behavioral specification rules, physical trace timeline event sequence, error logs, and LLM judge verdict.
                        </p>
                    </div>
                </div>

                <!-- Test details template view (initially hidden) -->
                <div class="test-details-container" id="pane-test-details" style="display: none;">
                    <!-- Details will render dynamically via JS -->
                </div>
            </div>
        </div>

        <!-- Countdown Banner -->
        <div class="countdown-banner" id="countdown-banner-el" style="display: none;">
            <div>
                <span id="countdown-text" style="font-weight: 700;">Suite completed. Auto-closing in 10 seconds...</span>
            </div>
            <button class="btn-keep-open" onclick="keepPageOpen()">KEEP PAGE OPEN</button>
        </div>
    </div>

    <script>
        let selectedTestName = null;
        let lastGlobalLogsLength = 0;
        let lastTestLogsLength = 0;
        let testIsScrolled = true;
        let cachedState = null;
        let currentCategoryFilter = 'all';
        let lastFetchedStatus = null;

        // Predefined BDD specifications for all tests
        const BEHAVIOR_SPECS = {
            "test_scenario_agent_live_prompt_turn": { given: "Scenario Agent initialized with DeepSeek", when: "Ingesting screenplay prompts", then: "Appends UpdateScript event with valid blocks" },
            "test_provisioner_vast_offers_search": { given: "Vast.ai CLI credentials loaded", when: "Provisioner agent executes search offers", then: "Cheapest rentable GPU instances returned and parsed" },
            "test_vast_create_and_destroy_lifecycle": { given: "Cheapest GPU offer selected", when: "Create instance is issued, then destroy", then: "VM is leased, status transitions to running, and is destroyed" },
            "test_ssh_handshake_and_docker_health": { given: "Running GPU VM container on port 9001", when: "HTTP health GET probe is sent", then: "Responds with 200 text/plain conversational health description" },
            "test_audio_agent_tts_job_queueing": { given: "Audio slots with scripted prefix exist", when: "Audio agent polls GSA", then: "Queues a TTS job with chosen GPU type immediately" },
            "test_video_agent_ltx_job_queueing": { given: "Video slots with scripted prefix exist", when: "Video agent polls GSA", then: "Queues an LTX job with chosen GPU type immediately" },
            "test_audio_loudness_normalizer_compilation": { given: "Raw louder WAV file generated on disk", when: "Loudness normalizer compiles loudnorm filter", then: "Normalizes WAV file output exactly to -16.0 +/- 1.0 LUFS" },
            "test_coordinate_timeline_dynamic_drift": { given: "Timeline with 3 blocks active", when: "Block 1 duration shifts by 2.0s", then: "Blocks 2/3 coordinates shift, timeline scales exactly 2.0s" },
            "test_budget_limit_aborted_gate": { given: "Pipeline budget cap set to 1.0 USD", when: "Total spent charges cross 1.01 USD", then: "Phase transitions to aborted, all running VMs are destroyed" },
            "test_gsa_wal_concurrency_isolation": { given: "GSA configured in SQLite WAL mode", when: "Multiple parallel microservices write to GSA", then: "State reconstructs from zero sequence without transaction locking" },
            "test_perplexity_verify_live": { given: "Perplexity Sonar Pro API credentials loaded", when: "Verifying capital city fact checking claim", then: "Verifies claim successfully without [TOOL_ERROR], includes sources" },
            "test_bdd_tts_fleet_cold_start": { given: "Documentary pipeline with 0 running TTS VMs", when: "Audio agent queues TTS jobs", then: "Provisioner scales up fleet from cold start and processes jobs" },
            "test_bdd_single_block_tts_inference": { given: "A single scripted text block", when: "Audio agent processes it via TTS VM", then: "Generates correct WAV file and appends AudioGenerated event" },
            "test_bdd_multi_block_tts_reconciliation": { given: "Multiple blocks queued for TTS", when: "Reconciliation runs after partial completions", then: "Correctly merges all blocks into active timeline state" },
            "test_bdd_voice_continuity_across_scenes": { given: "3 generated WAV narration clips using same voice profile", when: "LUFS loudness is measured on each clip", then: "LUFS spread is within ±3 dB showing natural voice consistency" },
            "test_bdd_ltx_fleet_scale_up": { given: "Multiple video generation jobs queued", when: "LTX VM provisioner checks fleet capacity", then: "Scales up VM instances to meet demand within budget limits" },
            "test_bdd_single_clip_video_generation": { given: "A single video slot with descriptive prompts", when: "Video agent requests generation from LTX VM", then: "Returns valid MP4 video and registers VideoMeasured event" },
            "test_bdd_multi_scene_video_otio_assembly": { given: "Multiple scene media clips generated", when: "Assembly agent builds OpenTimelineIO composition", then: "Creates correct timeline structure with valid scene transitions" },
            "test_bdd_audio_video_duration_alignment": { given: "Timeline with duration differences between audio and script", when: "DurationAdjusted event is processed", then: "Cascades timeline offsets and corrects dynamic drift" },
            "test_bdd_tts_retry_after_failure": { given: "TTS generation job fails due to network timeout", when: "Error recovery is triggered by Audio agent", then: "Requeues job up to max retry limit, then escalates if needed" },
            "test_bdd_vm_preemption_recovery": { given: "A running VM is preempted by cloud provider", when: "VM status check detects offline heartbeat", then: "Provisioner re-allocates a new VM and resumes active jobs" },
            "test_bdd_budget_gated_provisioning": { given: "Strict project budget constraint defined", when: "VM leasing requests exceed budget limits", then: "Blocks provisioning and aborts pipeline execution gracefully" },
            "test_bdd_script_revision_selective_requeue": { given: "Screenplay revision updates specific text blocks", when: "Reconciliation checks script block hashes", then: "Only requeues modified blocks, preserving unmodified assets" },
            "test_bdd_final_assembly_real_media": { given: "All narration audio and scene video clips generated", when: "Assembly agent executes final FFmpeg render", then: "Assembles complete documentary MP4 with aligned track feeds" },
            "test_bdd_partial_failure_isolated_recovery": { given: "One of multiple parallel generation jobs fails", when: "Fault isolation recovery logic executes", then: "Only retries the failed block without interrupting active jobs" },
            "test_bdd_full_fleet_teardown_cost_accounting": { given: "Documentary production pipeline finishes or aborts", when: "Fleet teardown command is issued", then: "Destroys all leased VMs and computes accurate final charges" },
            "test_agent_chooses_vm_size_and_provisioner_allocates": { given: "Agent requires specific GPU memory size (e.g. 24GB)", when: "Provisioner allocates matching VM instance from Vast.ai", then: "Allocated VM matches resource requirements exactly" },
            "test_provisioner_escalation_policy": { given: "Cheapest GPU offers are sold out or fail to lease", when: "Provisioner triggers VM escalation policy", then: "Progressively attempts next cheapest available GPU tiers" },
            "test_preemption_and_recovery": { given: "Vast.ai spot instance VM running a generation job", when: "VM is preempted (abruptly terminated)", then: "Detects preemption, provisions replacement, and resumes job" },
            "test_localized_recovery_and_retry": { given: "Individual block generation fails due to localized error", when: "Retry policy is evaluated", then: "Retries only the failed block in-place up to limit" },
            "test_accumulative_drift_correction": { given: "Accumulating duration drift across multiple scenes", when: "Drift exceeds cumulative correction threshold", then: "Applies timeline drift adjustment offsets to restore alignment" },
            "test_provisioner_cli_command_invocation": { given: "Provisioner executing Vast.ai command integrations", when: "Validating command invocation parameters", then: "Correctly invokes CLI commands and parses JSON outputs" },
            "test_assemble_final_cut_execution": { given: "OTIO composition assembly defined", when: "Assembling final cut audio/video feeds", then: "Generates valid FFmpeg assembly command arguments" },
            "test_real_qwen3_tts_script_execution": { given: "Real Qwen3 TTS capability enabled", when: "Executing TTS audio generation script", then: "Generates real narration audio matching script specifications" },
            "test_real_ltx_video_script_execution": { given: "Real LTX video generation capability enabled", when: "Executing LTX video generation script", then: "Generates real scene video matching prompt specifications" },
            "test_parse_duration_all_formats": { given: "Various duration string formats (e.g., '12s', '1.5s', '00:02')", when: "Parsing durations through helper function", then: "Parses all formats to correct float seconds" },
            "test_effect_pydantic_round_trip": { given: "Domain events (Effects) constructed", when: "Serializing to JSON and deserializing back", then: "Reconstructs identical model objects without data loss" },
            "test_event_store_append_replay_ordering": { given: "Empty event store database", when: "Appending sequence of domain events and replaying", then: "Returns events in exact sequential append order" },
            "test_event_store_idempotent_dedup": { given: "Event with unique effect_id already appended", when: "Appending duplicate event with same effect_id", then: "Ignores append request and returns existing event record" },
            "test_event_store_read_since_window": { given: "Event store with historical events", when: "Reading events since specific sequence number", then: "Returns only events appended after the specified sequence" },
            "test_timeline_projection_script_to_slots": { given: "Raw screenplay script text blocks input", when: "Projecting screenplay onto timeline slots", then: "Initializes correct scene slots and narrator roles" },
            "test_timeline_projection_merge_and_delivered": { given: "Timeline with script slots initialized", when: "Merging audio/video assets into timeline slots", then: "Updates slot state to delivered with correct asset paths" },
            "test_timeline_projection_delete_scene": { given: "Timeline with multiple scenes", when: "Deleting a scene from the timeline projection", then: "Removes scene slots and shifts subsequent offsets" },
            "test_timeline_projection_reorder_scenes": { given: "Timeline with multiple scenes in sequence", when: "Reordering scenes within the timeline projection", then: "Updates scene sequence and cascades timeline offsets" },
            "test_timeline_validation_suite": { given: "Timeline projections with various slot configurations", when: "Running validation check suite", then: "Identifies overlapping slots, negative offsets, or missing assets" },
            "test_jobs_projection_full_lifecycle": { given: "Jobs projection tracking active queue", when: "Jobs transition from queued to started to completed", then: "Updates job status records and handles final duration tracking" },
            "test_jobs_projection_dirty_clean_tracking": { given: "Jobs projection tracking dirty/clean states", when: "State changes update job parameters", then: "Accurately marks modified jobs as dirty for reconciliation" },
            "test_vm_projection_multi_role_fleet": { given: "VM projection tracking multi-role fleet", when: "Adding/removing VMs with different roles (audio/video)", then: "Maintains correct count and status of active fleet instances" },
            "test_budget_projection_exceeded_detection": { given: "Budget projection with specified cost limit", when: "VM lease and API charges accumulate", then: "Flags budget exceeded state as soon as spent crosses limit" },
            "test_state_projection_full_phase_machine": { given: "State projection tracking documentary phases", when: "Applying phase transition events", then: "Correctly updates phase (planning, scripting, rendering, complete)" },
            "test_coordinate_timeline_cascade_and_overlap": { given: "Coordinate timeline with overlapping audio/video blocks", when: "Aligning and cascading block offsets", then: "Resolves overlaps and cascades offsets to prevent collisions" },
            "Maximum Capacity Test": { given: "Stress test pipeline with high volume of concurrent jobs", when: "Running full production pipeline under maximum load", then: "Processes all jobs concurrently without database concurrency locks" }
        };

        // DOMContentLoaded
        document.addEventListener("DOMContentLoaded", () => {
            // Signal interaction immediately to prevent headless auto-close
            keepPageOpen();
        });

        async function keepPageOpen() {
            try {
                await fetch("/api/interact", { method: "POST" });
                document.getElementById("countdown-banner-el").style.display = "none";
            } catch (e) {
                console.error(e);
            }
        }

        function filterCategory(cat) {
            currentCategoryFilter = cat;
            document.querySelectorAll(".chip").forEach(btn => {
                if (btn.getAttribute("data-filter") === cat) {
                    btn.classList.add("active");
                } else {
                    btn.classList.remove("active");
                }
            });
            applySidebarFilters();
        }

        function applySidebarFilters() {
            const searchVal = document.getElementById("search-input").value.toLowerCase();
            
            document.querySelectorAll(".test-row").forEach(row => {
                const name = row.getAttribute("data-test-name").toLowerCase();
                const cat = row.getAttribute("data-category");
                const status = row.getAttribute("data-status");
                
                let matchesSearch = name.includes(searchVal);
                let matchesCat = true;
                
                if (currentCategoryFilter === 'failed') {
                    matchesCat = (status === 'failed');
                } else if (currentCategoryFilter !== 'all') {
                    matchesCat = (cat === currentCategoryFilter);
                }
                
                if (matchesSearch && matchesCat) {
                    row.style.display = "flex";
                } else {
                    row.style.display = "none";
                }
            });

            // Hide empty group divs
            document.querySelectorAll(".test-group").forEach(group => {
                const rows = Array.from(group.querySelectorAll(".test-row"));
                const visible = rows.filter(r => r.style.display !== "none");
                if (visible.length === 0) {
                    group.style.display = "none";
                } else {
                    group.style.display = "flex";
                }
            });
        }

        function selectAll(val) {
            document.querySelectorAll(".test-checkbox").forEach(cb => {
                // Check if row is visible before changing checkbox status
                const row = cb.closest(".test-row");
                if (row && row.style.display !== "none") {
                    cb.checked = val;
                }
            });
        }

        async function clearResultsFromServer() {
            if (!confirm("Are you sure you want to reset all test execution results?")) return;
            try {
                const res = await fetch("/api/clear", { method: "POST" });
                if (res.ok) {
                    selectedTestName = null;
                    document.getElementById("pane-test-details").style.display = "none";
                    document.getElementById("pane-dashboard-overview").style.display = "flex";
                    updateState();
                }
            } catch (e) {
                console.error(e);
            }
        }

        async function selectTest(name) {
            selectedTestName = name;
            document.querySelectorAll(".test-row").forEach(row => {
                if (row.getAttribute("data-test-name") === name) {
                    row.classList.add("active");
                } else {
                    row.classList.remove("active");
                }
            });
            
            document.getElementById("pane-dashboard-overview").style.display = "none";
            document.getElementById("pane-test-details").style.display = "flex";
            
            await keepPageOpen();
            await updateTestDetails();
        }

        async function runSingleTest(name, event) {
            if (event) event.stopPropagation();
            try {
                await fetch("/api/run", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ tests: [name] })
                });
                selectTest(name);
            } catch (e) {
                console.error(e);
            }
        }

        async function runSelectedTests() {
            const checked = [];
            document.querySelectorAll(".test-checkbox").forEach(cb => {
                if (cb.checked) {
                    checked.push(cb.getAttribute("data-test-name"));
                }
            });
            if (checked.length === 0) {
                alert("Please select at least one test case.");
                return;
            }
            try {
                await fetch("/api/run", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ tests: checked })
                });
                if (checked.length === 1) {
                    selectTest(checked[0]);
                }
            } catch (e) {
                console.error(e);
            }
        }

        async function runFailedTests() {
            if (!cachedState) return;
            const failed = cachedState.test_cases
                .filter(x => x.status === "failed")
                .map(x => x.name);
            if (failed.length === 0) {
                alert("No failed tests in the current suite.");
                return;
            }
            try {
                await fetch("/api/run", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ tests: failed })
                });
                if (failed.length === 1) {
                    selectTest(failed[0]);
                }
            } catch (e) {
                console.error(e);
            }
        }

        function toggleTraceback() {
            const content = document.getElementById("detail-traceback");
            const btn = document.getElementById("btn-tb-toggle");
            if (content.style.display === "block") {
                content.style.display = "none";
                btn.textContent = "Show Full Traceback";
            } else {
                content.style.display = "block";
                btn.textContent = "Hide Traceback";
            }
        }

        function toggleLogWrap() {
            const terminal = document.getElementById("detail-logs");
            const wrap = document.getElementById("log-wrap").checked;
            if (wrap) {
                terminal.style.whiteSpace = "pre-wrap";
            } else {
                terminal.style.whiteSpace = "pre";
            }
        }

        function copyTestLogs() {
            const terminal = document.getElementById("detail-logs");
            navigator.clipboard.writeText(terminal.innerText);
            alert("Logs copied to clipboard!");
        }

        // Stepper timeline HTML generator
        function renderTimeline(events) {
            if (!events || events.length === 0) {
                return `
                    <div class="welcome-pane" style="padding: 1.5rem;">
                        <div style="font-size: 2rem; opacity: 0.5;">📋</div>
                        <h4 style="margin: 0.5rem 0 0.25rem 0; font-size: 0.85rem;">No Events Recorded</h4>
                        <p style="max-width: 250px; font-size: 0.7rem; line-height: 1.4; margin: 0; color: var(--text-muted);">
                            This execution trace is empty. Offline projection and simple unit tests do not spawn database state.
                        </p>
                    </div>
                `;
            }

            let html = `<div class="timeline-container">`;
            
            events.forEach(evt => {
                const timeStr = typeof evt.timestamp === 'number' ? evt.timestamp.toFixed(3) + 's' : evt.timestamp;
                let bodyHtml = "";
                const data = evt.data || {};
                const kind = evt.kind;
                
                if (kind === "pipeline_started") {
                    bodyHtml = `Started production pipeline. Target output MP4: <code>${data.output_path || ''}</code>`;
                } else if (kind === "pipeline_complete") {
                    bodyHtml = `Pipeline execution completed successfully! Generated file at: <code>${data.output_path || ''}</code>`;
                } else if (kind === "pipeline_aborted") {
                    bodyHtml = `<span style="color: #f87171;">Pipeline aborted!</span> Reason: <strong>${data.reason || 'Unknown'}</strong>`;
                } else if (kind === "vm_allocated") {
                    bodyHtml = `Provisioner leased <strong>${data.gpu_name || 'GPU'}</strong> instance <code>${data.instance_id || ''}</code> at <strong>$${data.hourly_rate || '0.00'}/hr</strong>`;
                } else if (kind === "vm_deallocated") {
                    bodyHtml = `Instance <code>${data.instance_id || ''}</code> terminated and deallocated. Billing halted.`;
                } else if (kind === "queue_job") {
                    bodyHtml = `Queued <strong>${data.job_type || ''}</strong> generation job <code>${data.job_id || ''}</code> for block <code>${data.block_id || ''}</code>`;
                } else if (kind === "job_started") {
                    bodyHtml = `Job <code>${data.job_id || ''}</code> dispatched to VM. Executor agent: <code>${data.agent || ''}</code>`;
                } else if (kind === "job_completed") {
                    bodyHtml = `Job <code>${data.job_id || ''}</code> completed. Runtime: <strong>${data.elapsed_sec || 0}s</strong>`;
                } else if (kind === "job_failed") {
                    bodyHtml = `<span style="color: #f87171;">Job failed:</span> <code>${data.job_id || ''}</code>. Error: <em>${data.error || 'Unknown'}</em>`;
                } else if (kind === "merge_into_otio") {
                    bodyHtml = `OTIO composition merged block <code>${data.block_id || ''}</code> into track <code>${data.track_name || ''}</code> (${data.duration_sec || 0}s)`;
                } else if (kind === "duration_adjusted") {
                    const drift = ((data.measured_sec || 0) - (data.scripted_sec || 0)).toFixed(2);
                    bodyHtml = `Timeline duration correction. Block <code>${data.block_id || ''}</code> scripted: <strong>${data.scripted_sec || 0}s</strong> &rarr; measured: <strong>${data.measured_sec || 0}s</strong> (drift: <strong>${drift > 0 ? '+' + drift : drift}s</strong>)`;
                } else if (kind === "budget_set") {
                    bodyHtml = `Budget constraint established: <strong>$${data.limit_usd || '0.00'} USD</strong> max spent.`;
                } else if (kind === "budget_exceeded") {
                    bodyHtml = `<span style="color: #f87171; font-weight: 700;">BUDGET CAP BREACHED:</span> Spent accumulated charges of <strong>$${data.spent_usd || '0.00'} USD</strong>`;
                } else if (kind === "command_executed") {
                    bodyHtml = `Shell invocation:<br><pre><code>${data.command || ''}</code></pre>`;
                } else if (kind === "file_written") {
                    bodyHtml = `Disk write:<br><pre><code>${data.path || ''} (${data.size_bytes || 0} bytes)</code></pre>`;
                } else if (kind === "human_instruction") {
                    bodyHtml = `Operator instruction: <em>"${data.instruction || ''}"</em>`;
                } else {
                    const keys = Object.keys(data).filter(k => k !== 'kind' && k !== 'effect_id' && k !== 'agent' && k !== 'timestamp');
                    if (keys.length > 0) {
                        const details = keys.map(k => `${k}: <code>${data[k]}</code>`).join(', ');
                        bodyHtml = `Event properties: ${details}`;
                    } else {
                        bodyHtml = `Event kind: <code>${kind}</code> triggered.`;
                    }
                }
                
                let dotClass = "passed";
                if (kind.includes("fail") || kind.includes("abort") || kind.includes("exceeded")) {
                    dotClass = "failed";
                } else if (kind.includes("warn") || kind.includes("requeue")) {
                    dotClass = "warn";
                }
                
                html += `
                    <div class="timeline-item">
                        <div class="timeline-icon-container">
                            <span class="timeline-seq">#${evt.seq}</span>
                            <div class="timeline-dot ${dotClass}"></div>
                        </div>
                        <div class="timeline-content-card">
                            <div class="timeline-header">
                                <span class="timeline-kind">${kind}</span>
                                <div class="timeline-meta">
                                    <span class="timeline-agent">${evt.agent}</span>
                                    <span class="timeline-time">${timeStr}</span>
                                </div>
                            </div>
                            <div class="timeline-body">
                                ${bodyHtml}
                            </div>
                        </div>
                    </div>
                `;
            });
            
            html += `</div>`;
            return html;
        }

        async function updateTestDetails() {
            if (!selectedTestName) return;
            try {
                const response = await fetch(`/api/test/${selectedTestName}`);
                const data = await response.json();
                if (data.error) return;

                const detailsContainer = document.getElementById("pane-test-details");
                
                // 1. Behavior Specification Given/When/Then
                let spec = BEHAVIOR_SPECS[selectedTestName];
                if (!spec) {
                    if (selectedTestName.startsWith("test_bdd_")) {
                        const label = selectedTestName.replace("test_bdd_", "").replace(/_/g, " ");
                        spec = {
                            given: "Event-sourced documentary production pipeline fully initialized",
                            when: `Executing integration scene flow: "${label}"`,
                            then: "BDD judge evaluates execution log and outputs finalized MP4 cut"
                        };
                    } else {
                        spec = {
                            given: "Active pipeline capability simulator modules",
                            when: `Executing unit verification method: "${selectedTestName}"`,
                            then: "System assertions validate structural properties successfully"
                        };
                    }
                }

                const specHtml = `
                    <div class="spec-row">
                        <span class="spec-label">Given</span>
                        <span class="spec-val">${spec.given}</span>
                    </div>
                    <div class="spec-row">
                        <span class="spec-label">When</span>
                        <span class="spec-val">${spec.when}</span>
                    </div>
                    <div class="spec-row">
                        <span class="spec-label">Then</span>
                        <span class="spec-val">${spec.then}</span>
                    </div>
                `;

                // 2. BDD Judge Verdict
                let verdictCardHtml = "";
                if (data.verdict) {
                    const v = data.verdict;
                    const vClass = v.verdict.toLowerCase();
                    const confidencePct = Math.round((v.confidence || 0) * 100);
                    
                    let issuesHtml = "";
                    if (v.issues && v.issues.length > 0) {
                        issuesHtml = `
                            <div>
                                <h4 class="issues-title">Identified Anomalies:</h4>
                                <ul class="issues-list">
                                    ${v.issues.map(iss => `<li>${iss}</li>`).join('')}
                                </ul>
                            </div>
                        `;
                    }

                    verdictCardHtml = `
                        <div class="info-card" id="detail-verdict-card">
                            <div class="card-title">🤖 LLM QA Judge Verdict</div>
                            <div class="bdd-verdict-grid">
                                <div class="confidence-gauge">
                                    <svg class="gauge-svg" viewBox="0 0 36 36">
                                        <path class="gauge-bg" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                                        <path class="gauge-fill" stroke-dasharray="${confidencePct}, 100" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                                    </svg>
                                    <div class="gauge-value">${confidencePct}%</div>
                                </div>
                                <div style="display: flex; flex-direction: column; gap: 4px;">
                                    <div class="verdict-header-row">
                                        <span style="font-size: 0.8rem; font-weight: 700; color: #fff;">Judge Verification Report</span>
                                        <span class="badge-large ${vClass}">VERDICT: ${v.verdict}</span>
                                    </div>
                                    <div class="verdict-reasoning">
                                        <strong>Reasoning:</strong> ${v.reasoning}
                                    </div>
                                    ${issuesHtml}
                                </div>
                            </div>
                        </div>
                    `;
                }

                // 2b. Subagent Congruence Audit Verdict
                let auditCardHtml = "";
                if (data.congruence_audit) {
                    const a = data.congruence_audit;
                    const aClass = (a.verdict || "fail").toLowerCase();
                    auditCardHtml = `
                        <div class="info-card" id="detail-audit-card">
                            <div class="card-title">🔍 Subagent Congruence Audit</div>
                            <div class="bdd-verdict-grid">
                                <div style="display: flex; flex-direction: column; gap: 4px; width: 100%;">
                                    <div class="verdict-header-row">
                                        <span style="font-size: 0.8rem; font-weight: 700; color: #fff;">Congruence Verification</span>
                                        <span class="badge-large ${aClass}">VERDICT: ${a.verdict}</span>
                                    </div>
                                    <div class="verdict-reasoning" style="max-height: 120px;">
                                        <strong>Reasoning:</strong> ${a.reasoning}
                                    </div>
                                </div>
                            </div>
                        </div>
                    `;
                }

                // 3. Error Callout
                let errorHtml = "";
                if (data.error) {
                    errorHtml = `
                        <div class="callout-error">
                            <div class="error-header">
                                <span class="error-title">❌ Test Assertion Failed</span>
                                ${data.traceback ? `<button class="btn-secondary" style="padding: 2px 8px; font-size: 0.65rem;" id="btn-tb-toggle" onclick="toggleTraceback()">Show Traceback</button>` : ''}
                            </div>
                            <div class="error-message">${data.error}</div>
                            ${data.traceback ? `<pre class="traceback-pre" id="detail-traceback" style="display: none;">${data.traceback}</pre>` : ''}
                        </div>
                    `;
                }

                // Assemble details pane contents
                detailsContainer.innerHTML = `
                    <div class="test-details-header">
                        <div class="details-title-section">
                            <span class="category-tag">${data.status} &middot; ${selectedTestName.replace('test_','').split('_')[0].toUpperCase()}</span>
                            <h2 id="detail-name">${data.name}</h2>
                        </div>
                        <div class="details-actions">
                            <span class="badge-large ${data.status}">${data.status}</span>
                            <span class="detail-time">Duration: <strong>${data.elapsed.toFixed(2)}s</strong></span>
                            <button class="btn-run-single" onclick="runSingleTest('${data.name}')">
                                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                                RUN SINGLE
                            </button>
                        </div>
                    </div>
                    
                    ${errorHtml}
 
                    <div class="test-details-grid">
                        <div class="details-left-col">
                            <div class="info-card">
                                <div class="card-title">📖 Behavior Specification</div>
                                <div class="spec-container">
                                    ${specHtml}
                                </div>
                            </div>
                            ${verdictCardHtml}
                            ${auditCardHtml}
                        </div>
                        
                        <div class="details-right-col">
                            <div class="info-card timeline-card">
                                <div class="card-title">⏳ Event Store Execution Trace</div>
                                <div class="timeline-scroll" id="detail-timeline">
                                    ${renderTimeline(data.events)}
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Test Console Log -->
                    <div class="info-card log-card">
                        <div class="card-title log-card-title">
                            <span>💻 Test Execution Console Output</span>
                            <div class="log-controls">
                                <label class="control-label"><input type="checkbox" id="log-wrap" onchange="toggleLogWrap()"> Wrap Lines</label>
                                <label class="control-label"><input type="checkbox" id="log-autoscroll" checked> Auto-Scroll</label>
                                <button class="btn-secondary" style="padding: 2px 6px; font-size: 0.65rem;" onclick="copyTestLogs()">Copy Output</button>
                            </div>
                        </div>
                        <div class="terminal" id="detail-logs"></div>
                    </div>
                `;

                // Update logs
                const terminal = document.getElementById("detail-logs");
                if (terminal) {
                    terminal.innerHTML = data.logs || "No stdout/stderr console prints captured for this test case.";
                    if (document.getElementById("log-autoscroll").checked) {
                        terminal.scrollTop = terminal.scrollHeight;
                    }
                }

            } catch (e) {
                console.error("Failed to update test details:", e);
            }
        }

        async function updateState() {
            try {
                const response = await fetch("/api/state");
                const data = await response.json();
                cachedState = data;

                // Update global AI summary
                document.getElementById("ai-summary-text").textContent = data.ai_summary;

                // Update stats badges in header
                document.getElementById("count-passed").textContent = data.passed;
                document.getElementById("count-failed").textContent = data.failed;
                document.getElementById("count-skipped").textContent = data.skipped;
                document.getElementById("count-pending").textContent = data.pending;

                // Update overview dashboard stats
                document.getElementById("overview-total").textContent = data.total_tests;
                document.getElementById("overview-passed").textContent = data.passed;
                document.getElementById("overview-failed").textContent = data.failed;
                document.getElementById("overview-skipped").textContent = data.skipped;

                // Update suite badge class and label
                const suiteBadge = document.getElementById("suite-status-badge");
                suiteBadge.className = "status-pill status-" + data.suite_status;
                suiteBadge.textContent = data.suite_status;

                // Description of active running test
                const summaryDesc = document.getElementById("suite-summary-desc");
                if (data.suite_status === "running") {
                    summaryDesc.innerHTML = `Running: <strong style="color: #fff; font-family: monospace;">${data.active_test_name}</strong> &middot; Phase: <span style="color: #818cf8; font-weight: 700;">${data.active_category}</span>`;
                } else {
                    summaryDesc.textContent = "Interactive execution runner and live BDD verification judge";
                }

                // Progress Bar fill
                const total = data.total_tests;
                if (total > 0) {
                    const completed = data.passed + data.failed + data.skipped;
                    const percent = Math.round((completed / total) * 100);
                    document.getElementById("progress-percent").textContent = `${percent}% Completed`;
                    
                    document.getElementById("bar-passed").style.width = (data.passed / total * 100) + "%";
                    document.getElementById("bar-failed").style.width = (data.failed / total * 100) + "%";
                    document.getElementById("bar-skipped").style.width = (data.skipped / total * 100) + "%";
                }

                // Sidebar rendering check to avoid flashes
                const listContainer = document.getElementById("test-list-el");
                if (listContainer.children.length === 0) {
                    renderSidebarList(data.test_cases);
                } else {
                    data.test_cases.forEach(tc => {
                        const row = document.getElementById(`row-${tc.name}`);
                        if (row) {
                            row.setAttribute("data-status", tc.status);
                            
                            // Highlight currently running test
                            if (tc.name === data.active_test_name) {
                                row.classList.add("active");
                            } else if (tc.name !== selectedTestName) {
                                row.classList.remove("active");
                            }
                            
                            if (tc.name === selectedTestName) {
                                row.classList.add("active");
                            }

                            const dot = row.querySelector(".test-status-dot");
                            dot.className = `test-status-dot dot-${tc.status}`;
                        }
                    });
                }

                // If currently running active test details, poll its console logs continuously
                if (selectedTestName) {
                    const selTc = data.test_cases.find(x => x.name === selectedTestName);
                    if (selTc) {
                        const currentStatus = selTc.status;
                        if (currentStatus === "running" || currentStatus === "pending" || currentStatus !== lastFetchedStatus) {
                            await updateTestDetails();
                            lastFetchedStatus = currentStatus;
                        }
                    }
                } else {
                    lastFetchedStatus = null;
                }

                // Auto closecountdown
                const countdownBanner = document.getElementById("countdown-banner-el");
                if (data.suite_completed && !data.user_interacted) {
                    countdownBanner.style.display = "flex";
                    const isSuccess = data.failed === 0;
                    const statusText = isSuccess ? "ALL TESTS PASSED" : `${data.failed} TEST(S) FAILED`;
                    const color = isSuccess ? "#34d399" : "#f87171";
                    document.getElementById("countdown-text").innerHTML = `<span style="color: ${color}; font-weight: 800;">${statusText}</span>: Suite execution finished. Auto-closing in ${data.auto_close_countdown} seconds...`;
                } else {
                    countdownBanner.style.display = "none";
                }

            } catch (e) {
                console.error("Failed to poll state from backend:", e);
            }
        }

        function renderSidebarList(cases) {
            const container = document.getElementById("test-list-el");
            container.innerHTML = "";

            const categories = ["Simulation Cover", "BDD Integration", "Consequential Claims", "Process Tests", "Maximum Capacity"];
            
            categories.forEach(cat => {
                const catCases = cases.filter(x => x.category === cat);
                if (catCases.length === 0) return;

                const groupDiv = document.createElement("div");
                groupDiv.className = "test-group";
                groupDiv.id = `group-div-${cat.replace(/ /g, '-')}`;
                
                const groupHeader = document.createElement("div");
                groupHeader.className = "test-group-header";
                
                const title = document.createElement("div");
                title.className = "test-group-title";
                title.textContent = cat;
                groupHeader.appendChild(title);

                const selectGroupBtn = document.createElement("button");
                selectGroupBtn.className = "test-group-select-all";
                selectGroupBtn.textContent = "Toggle Group";
                selectGroupBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const groupRows = groupDiv.querySelectorAll(".test-checkbox");
                    const allChecked = Array.from(groupRows).every(x => x.checked);
                    groupRows.forEach(cb => cb.checked = !allChecked);
                });
                groupHeader.appendChild(selectGroupBtn);
                groupDiv.appendChild(groupHeader);

                catCases.forEach(tc => {
                    const row = document.createElement("div");
                    row.className = "test-row";
                    if (tc.name === selectedTestName) row.classList.add("active");
                    row.id = `row-${tc.name}`;
                    row.setAttribute("data-test-name", tc.name);
                    row.setAttribute("data-category", tc.category);
                    row.setAttribute("data-status", tc.status);
                    row.addEventListener("click", () => selectTest(tc.name));

                    const cb = document.createElement("input");
                    cb.type = "checkbox";
                    cb.className = "test-checkbox";
                    cb.setAttribute("data-test-name", tc.name);
                    cb.addEventListener("click", (e) => e.stopPropagation());
                    row.appendChild(cb);

                    const dot = document.createElement("div");
                    dot.className = `test-status-dot dot-${tc.status}`;
                    row.appendChild(dot);

                    const nameSpan = document.createElement("span");
                    nameSpan.className = "test-name";
                    nameSpan.textContent = tc.name;
                    row.appendChild(nameSpan);

                    const playBtn = document.createElement("button");
                    playBtn.className = "test-play-btn";
                    playBtn.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>`;
                    playBtn.addEventListener("click", (e) => runSingleTest(tc.name, e));
                    row.appendChild(playBtn);

                    groupDiv.appendChild(row);
                });

                container.appendChild(groupDiv);
            });
        }

        setInterval(updateState, 300);
        updateState();
    </script>
</body>
</html>
"""

app = FastAPI()

from pydantic import BaseModel
class RunTestsRequest(BaseModel):
    tests: list[str]

@app.get("/")
def get_index():
    return HTMLResponse(content=INDEX_HTML)

def read_events_from_db(db_path: str) -> list:
    import sqlite3
    import json
    events = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Check if events table exists
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
            if not cur.fetchone():
                return []
            
            cur = conn.execute("SELECT seq, kind, agent, timestamp, effect_json FROM events ORDER BY seq")
            for row in cur.fetchall():
                evt = {
                    "seq": row["seq"],
                    "kind": row["kind"],
                    "agent": row["agent"],
                    "timestamp": row["timestamp"],
                }
                try:
                    effect_data = json.loads(row["effect_json"])
                    evt["data"] = effect_data
                except Exception:
                    evt["data"] = {}
                events.append(evt)
    except Exception as e:
        pass
    return events

@app.get("/api/state")
def get_state():
    cases_list = []
    for name, _, cat in TEST_CASES:
        status = test_status[name]["status"]
        cases_list.append({
            "name": name,
            "status": status,
            "category": cat,
            "elapsed": test_status[name]["elapsed"]
        })

    return {
        "suite_status": suite_status,
        "suite_completed": suite_completed,
        "total_tests": len(TEST_CASES),
        "passed": stats["passed"],
        "failed": stats["failed"],
        "skipped": stats["skipped"],
        "pending": stats["pending"],
        "active_test_name": active_test_name,
        "active_category": active_category,
        "test_cases": cases_list,
        "logs": "".join(logs_list),
        "ai_summary": ai_summary,
        "auto_close_countdown": auto_close_countdown,
        "user_interacted": user_interacted
    }

@app.get("/api/test/{test_name}")
def get_test_details(test_name: str):
    if test_name not in test_status:
        return {"error": "Test not found"}
        
    ts = test_status[test_name]
    verdict = bdd_verdicts_dict.get(test_name)
    if not verdict:
        try:
            verdict_path = PROJECT_ROOT / "test_outputs" / "bdd_verdicts" / f"{test_name}.json"
            if not verdict_path.exists():
                stripped = test_name.replace("test_bdd_", "").replace("test_", "")
                verdict_path = PROJECT_ROOT / "test_outputs" / "bdd_verdicts" / f"{stripped}.json"
            if verdict_path.exists():
                with open(verdict_path) as f:
                    verdict = json.load(f)
                    bdd_verdicts_dict[test_name] = verdict
        except Exception:
            pass

    events = ts.get("events", [])
    if not events and ts.get("db_path"):
        events = read_events_from_db(ts["db_path"])
        ts["events"] = events

    return {
        "name": test_name,
        "status": ts["status"],
        "elapsed": ts["elapsed"],
        "error": ts["error"],
        "traceback": ts.get("traceback"),
        "logs": "".join(test_logs.get(test_name, [])),
        "verdict": verdict,
        "congruence_audit": ts.get("congruence_audit"),
        "events": events
    }

@app.post("/api/run")
def run_tests(req: RunTestsRequest):
    global user_interacted, run_queue, suite_completed, suite_status
    user_interacted = True
    
    # Reset status of tests to be run
    for name in req.tests:
        if name in test_status:
            test_status[name] = {"status": "pending", "elapsed": 0.0, "error": None, "events": [], "db_path": None}
            test_logs[name] = []
            bdd_verdicts_dict.pop(name, None)
            
    recompute_stats()
    
    # Append to queue
    for name in req.tests:
        if name not in run_queue:
            run_queue.append(name)
            
    return {"status": "queued", "queue_len": len(run_queue)}

@app.post("/api/clear")
def clear_results():
    global suite_completed, suite_status, run_queue
    run_queue.clear()
    for name in test_status:
        test_status[name] = {"status": "pending", "elapsed": 0.0, "error": None, "events": [], "db_path": None}
        test_logs[name] = []
    bdd_verdicts_dict.clear()
    recompute_stats()
    suite_completed = False
    suite_status = "idle"
    return {"status": "cleared"}

@app.post("/api/interact")
def user_interact():
    global user_interacted
    user_interacted = True
    return {"user_interacted": True}

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def recompute_stats():
    global stats, history
    passed = sum(1 for ts in test_status.values() if ts.get("status") == "passed")
    failed = sum(1 for ts in test_status.values() if ts.get("status") == "failed")
    skipped = sum(1 for ts in test_status.values() if ts.get("status") == "skipped")
    pending = len(TEST_CASES) - passed - failed - skipped
    
    stats["passed"] = passed
    stats["failed"] = failed
    stats["skipped"] = skipped
    stats["pending"] = pending
    
    history.clear()
    for name, ts in test_status.items():
        if ts.get("status") != "pending":
            history.append((name, ts.get("status")))

def run_suite_in_thread():
    global suite_status, suite_completed, active_test_name, active_category, end_time, auto_close_countdown, run_queue, current_running_test
    
    # Start AI Summary Worker
    threading.Thread(target=ai_summary_worker, daemon=True).start()
    
    while True:
        if not run_queue:
            time.sleep(0.1)
            continue
            
        suite_status = "running"
        suite_completed = False
        
        while run_queue:
            name = run_queue.pop(0)
            test_case = next((tc for tc in TEST_CASES if tc[0] == name), None)
            if not test_case:
                continue
                
            func = test_case[1]
            category = test_case[2]
            
            active_test_name = name
            active_category = category
            current_running_test = name
            
            # Clear logs for this test
            test_logs[name] = []
            test_status[name] = {"status": "running", "elapsed": 0.0, "error": None, "events": [], "db_path": None}
            recompute_stats()
            
            trigger_ai_summary_refresh()
            
            # Run the fresh subagent congruence audit first
            print(f"🔍 Running fresh subagent congruence audit for '{name}'...")
            try:
                audit_res = run_subagent_audit(name)
                test_status[name]["congruence_audit"] = audit_res
                print(f"🔍 Subagent congruence audit verdict for '{name}': {audit_res.get('verdict')}")
            except Exception as ae:
                test_status[name]["congruence_audit"] = {"verdict": "FAIL", "reasoning": f"Audit exception: {ae}"}
                print(f"❌ Subagent congruence audit failed: {ae}")
                
            start = time.time()
            try:
                func()
                elapsed = time.time() - start
                test_status[name]["status"] = "passed"
                test_status[name]["elapsed"] = elapsed
                recompute_stats()
                passed_summary = f"SUMMARY: TEST CASE '{name}' COMPLETED SUCCESSFULLY AND PASSED IN {elapsed:.2f} SECONDS.".upper()
                print(f"\n📢  {passed_summary}\n")
            except BaseException as e:
                elapsed = time.time() - start
                err_msg = str(e)
                import traceback
                tb_str = traceback.format_exc()
                if e.__class__.__name__ == "Skipped":
                    test_status[name]["status"] = "skipped"
                    test_status[name]["elapsed"] = elapsed
                    test_status[name]["error"] = err_msg
                    test_status[name]["traceback"] = tb_str
                    recompute_stats()
                    skipped_summary = f"SUMMARY: TEST CASE '{name}' WAS SKIPPED AFTER {elapsed:.2f} SECONDS. REASON: {e}".upper()
                    print(f"\n📢  {skipped_summary}\n")
                else:
                    test_status[name]["status"] = "failed"
                    test_status[name]["elapsed"] = elapsed
                    test_status[name]["error"] = err_msg
                    test_status[name]["traceback"] = tb_str
                    recompute_stats()
                    failed_summary = f"SUMMARY: TEST CASE '{name}' FAILED AFTER {elapsed:.2f} SECONDS. ERROR: {e}".upper()
                    print(f"\n📢  {failed_summary}\n")
            
            # Scan for the BDD verdict JSON and events.db in temp directories
            try:
                import tempfile
                sys_temp = tempfile.gettempdir()
                recent_dbs = []
                recent_verdicts = []
                stripped_name = name.replace("test_bdd_", "").replace("test_", "")
                
                for root, dirs, files in os.walk(sys_temp):
                    for f in files:
                        file_path = os.path.join(root, f)
                        try:
                            mtime = os.path.getmtime(file_path)
                        except Exception:
                            continue
                        if mtime >= start - 5.0:
                            if f == "events.db":
                                recent_dbs.append((mtime, file_path))
                            elif f.endswith(".json"):
                                if "bdd_verdicts" in root or stripped_name in f or name in f:
                                    recent_verdicts.append((mtime, file_path))
                
                if recent_dbs:
                    recent_dbs.sort(reverse=True)
                    test_status[name]["db_path"] = recent_dbs[0][1]
                    
                if recent_verdicts:
                    recent_verdicts.sort(reverse=True)
                    for _, path in recent_verdicts:
                        try:
                            with open(path) as vf:
                                v_data = json.load(vf)
                                if "verdict" in v_data and "reasoning" in v_data:
                                    bdd_verdicts_dict[name] = v_data
                                    try:
                                        import shutil
                                        brain_root = pathlib.Path("/Users/orpington/.gemini/antigravity/brain")
                                        conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
                                        brain_dir = None
                                        if conv_id and (brain_root / conv_id).exists():
                                            brain_dir = brain_root / conv_id
                                        else:
                                            newest_mtime = 0
                                            for subdir in brain_root.iterdir():
                                                if subdir.is_dir() and not subdir.name.startswith("."):
                                                    state_file = subdir / ".lock_state"
                                                    mtime = state_file.stat().st_mtime if state_file.exists() else subdir.stat().st_mtime
                                                    if mtime > newest_mtime:
                                                        newest_mtime = mtime
                                                        brain_dir = subdir
                                        if brain_dir:
                                            dest_dir = brain_dir / "test_outputs" / "bdd_verdicts"
                                            dest_dir.mkdir(parents=True, exist_ok=True)
                                            s_name = name.replace("test_bdd_", "").replace("test_", "")
                                            shutil.copy2(path, dest_dir / f"{s_name}.json")
                                            
                                            proj_dest_dir = PROJECT_ROOT / "test_outputs" / "bdd_verdicts"
                                            proj_dest_dir.mkdir(parents=True, exist_ok=True)
                                            shutil.copy2(path, proj_dest_dir / f"{s_name}.json")
                                    except Exception as ce:
                                        print(f"Error copying BDD verdict: {ce}")
                                    break
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error scanning temp dirs: {e}")

            # Write/update pytest_output.log in brain test_outputs
            try:
                brain_root = pathlib.Path("/Users/orpington/.gemini/antigravity/brain")
                conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
                brain_dir = None
                if conv_id and (brain_root / conv_id).exists():
                    brain_dir = brain_root / conv_id
                else:
                    newest_mtime = 0
                    for subdir in brain_root.iterdir():
                        if subdir.is_dir() and not subdir.name.startswith("."):
                            state_file = subdir / ".lock_state"
                            mtime = state_file.stat().st_mtime if state_file.exists() else subdir.stat().st_mtime
                            if mtime > newest_mtime:
                                newest_mtime = mtime
                                brain_dir = subdir
                if brain_dir:
                    log_dir = brain_dir / "test_outputs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    log_file = log_dir / "pytest_output.log"
                    import re
                    raw_logs = "".join(logs_list)
                    clean_logs = re.sub(r'<[^>]+>', '', raw_logs)
                    with open(log_file, "w", encoding="utf-8") as lf:
                        lf.write(clean_logs)
            except Exception as le:
                print(f"Error writing pytest_output.log: {le}")
                
        current_running_test = None
        suite_completed = True
        any_failed = any(ts.get("status") == "failed" for ts in test_status.values())
        suite_status = "failed" if any_failed else "passed"
        
        # Trigger countdown only if user hasn't interacted and no-exit isn't active
        if not user_interacted and not keep_alive:
            for i in range(10, 0, -1):
                if user_interacted or run_queue:
                    break
                auto_close_countdown = i
                time.sleep(1)
            else:
                if not user_interacted and not keep_alive:
                    sys.stdout = original_stdout
                    sys.stderr = original_stderr
                    print("\n" + "=" * 80)
                    print("                      DOCUMENTARY PIPELINE TEST RUNNER - FINAL REPORT")
                    print("=" * 80)
                    print(f"PASSED:  {stats['passed']}")
                    print(f"FAILED:  {stats['failed']}")
                    print(f"SKIPPED: {stats['skipped']}")
                    print("=" * 80 + "\n")
                    os._exit(0 if suite_status == "passed" else 1)

def run_architecture_test():
    import ast
    import sys
    from pathlib import Path
    
    print("🔍 Running Architecture Test on tests/units...")
    tests_dir = PROJECT_ROOT / "tests" / "units"
    if not tests_dir.exists():
        print("✅ Architecture Test: No tests directory found.")
        return
        
    py_files = list(tests_dir.glob("*.py"))
    violations = []
    
    for py_file in py_files:
        if py_file.name in ("__init__.py", "harness.py"):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except Exception as e:
            violations.append(f"{py_file.name}: Failed to parse AST: {e}")
            continue
            
        file_violations = []
        
        class SCVisitor(ast.NodeVisitor):
            def visit_Import(self, node):
                for alias in node.names:
                    if any(x in alias.name for x in ["mock", "unittest.mock", "pytest_mock"]):
                        file_violations.append(f"Line {node.lineno}: Forbidden mock import: 'import {alias.name}'")
                self.generic_visit(node)
                
            def visit_ImportFrom(self, node):
                if node.module and any(x in node.module for x in ["mock", "unittest.mock", "pytest_mock"]):
                    file_violations.append(f"Line {node.lineno}: Forbidden mock import: 'from {node.module} import ...'")
                for alias in node.names:
                    if any(x in alias.name for x in ["mock", "MagicMock", "Mock", "PropertyMock"]):
                        file_violations.append(f"Line {node.lineno}: Forbidden mock import: '{alias.name}' from '{node.module}'")
                self.generic_visit(node)
                
            def visit_Attribute(self, node):
                if isinstance(node.value, ast.Name):
                    if node.value.id in ["mock", "unittest", "pytest"]:
                        if any(x in node.attr for x in ["patch", "MagicMock", "Mock", "PropertyMock", "skip"]):
                            file_violations.append(f"Line {node.lineno}: Forbidden mock/skip usage: '{node.value.id}.{node.attr}'")
                self.generic_visit(node)
                
            def visit_Name(self, node):
                if node.id in ["MagicMock", "Mock", "PropertyMock"]:
                    file_violations.append(f"Line {node.lineno}: Forbidden mock usage: '{node.id}'")
                self.generic_visit(node)
                
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest" and node.func.attr == "skip":
                        file_violations.append(f"Line {node.lineno}: Forbidden skip call: 'pytest.skip(...)'")
                elif isinstance(node.func, ast.Name) and node.func.id == "skip":
                    file_violations.append(f"Line {node.lineno}: Forbidden skip call: 'skip(...)'")
                self.generic_visit(node)
                
            def visit_Assert(self, node):
                test = node.test
                is_trivial = False
                if isinstance(test, ast.Constant):
                    is_trivial = True
                elif isinstance(test, ast.Compare):
                    if len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                        left = test.left
                        right = test.comparators[0]
                        if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
                            is_trivial = True
                if is_trivial:
                    file_violations.append(f"Line {node.lineno}: Forbidden trivial assertion")
                self.generic_visit(node)
                
            def visit_FunctionDef(self, node):
                for dec in node.decorator_list:
                    dec_str = ast.dump(dec)
                    if "skip" in dec_str.lower():
                        file_violations.append(f"Line {node.lineno}: Forbidden skip decorator on function '{node.name}'")
                self.generic_visit(node)
                
            def visit_ClassDef(self, node):
                for dec in node.decorator_list:
                    dec_str = ast.dump(dec)
                    if "skip" in dec_str.lower():
                        file_violations.append(f"Line {node.lineno}: Forbidden skip decorator on class '{node.name}'")
                self.generic_visit(node)
                
        visitor = SCVisitor()
        visitor.visit(tree)
        
        if file_violations:
            violations.append(f"File: {py_file.name}\n" + "\n".join(f"  - {v}" for v in file_violations))
            
    if violations:
        print("\n❌ ARCHITECTURE TEST FAILURE! Test runner execution aborted.")
        for violation in violations:
            print(f"\n{violation}")
        sys.exit(1)
    else:
        print("✅ Architecture Test: Passed (all tests compliant with simulation cover invariants).\n")

def main():
    # Execute the architecture validation check first
    run_architecture_test()

    import argparse
    parser = argparse.ArgumentParser(description="Documentary Pipeline Test Runner Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Host address to bind the server to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port to run the dashboard server on (default: 19246 or find free port)")
    parser.add_argument("--no-browser", action="store_true", help="Do not attempt to open a web browser on startup")
    parser.add_argument("--no-exit", action="store_true", help="Do not automatically exit after the tests finish running")
    args, unknown = parser.parse_known_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    # Determine port
    port = args.port
    if port is None:
        # Try preferred port 19246 first
        preferred_port = 19246
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((args.host, preferred_port))
                port = preferred_port
            except Exception:
                port = find_free_port()

    global keep_alive
    keep_alive = args.no_exit or ("ANTIGRAVITY_NO_EXIT" in os.environ)

    sys.stdout = LiveStreamCapture(original_stdout)
    sys.stderr = LiveStreamCapture(original_stderr)

    threading.Thread(target=log_processor, daemon=True).start()

    suite_thread = threading.Thread(target=run_suite_in_thread, daemon=True)
    suite_thread.start()

    def start_web_server():
        uvicorn.run(app, host=args.host, port=port, log_level="error")

    threading.Thread(target=start_web_server, daemon=True).start()

    print("\n" + "=" * 80)
    print(f"🚀  DASHBOARD SERVER IS STARTING...")
    print(f"🔗  URL: http://{args.host}:{port}")
    print(f"📖  Use this link to open the Test Runner GUI dashboard.")
    print("=" * 80 + "\n")
    sys.stdout.flush()

    time.sleep(0.8)
    if not args.no_browser:
        try:
            webbrowser.open(f"http://{args.host}:{port}")
        except Exception:
            pass

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
