#!/usr/bin/env python3
import os
import sys
import time
import argparse
import json
import httpx
import pathlib
import traceback

# Setup Python paths
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "server"))

# Import all tests
from tests.units.test_covering_gsa_wal_concurrency_isolation import test_covering_gsa_wal_concurrency_isolation
from tests.units.test_covering_scenario_agent_live_prompt_turn import test_covering_scenario_agent_live_prompt_turn
from tests.units.test_covering_audio_agent_tts_job_queueing import test_covering_audio_agent_tts_job_queueing
from tests.units.test_simulation_video_agent_ltx_job_queueing import test_simulation_video_agent_ltx_job_queueing
from tests.units.test_covering_provisioner_vast_offers_search import test_covering_provisioner_vast_offers_search
from tests.units.test_covering_vast_create_and_destroy_lifecycle import test_covering_vast_create_and_destroy_lifecycle
from tests.units.test_covering_ssh_handshake_and_docker_health import test_covering_ssh_handshake_and_docker_health
from tests.units.test_covering_audio_loudness_normalizer_compilation import test_covering_audio_loudness_normalizer_compilation
from tests.units.test_covering_coordinate_timeline_dynamic_drift import test_covering_coordinate_timeline_dynamic_drift
from tests.units.test_covering_budget_limit_aborted_gate import test_covering_budget_limit_aborted_gate
from tests.units.test_agent_chooses_vm_size_and_provisioner_allocates import test_agent_chooses_vm_size_and_provisioner_allocates
from tests.units.test_provisioner_escalation_policy import test_provisioner_escalation_policy
from tests.units.test_preemption_and_recovery import test_preemption_and_recovery
from tests.units.test_localized_recovery_and_retry import test_localized_recovery_and_retry
from tests.units.test_accumulative_drift_correction import test_accumulative_drift_correction
from tests.units.test_provisioner_cli_command_invocation import test_provisioner_cli_command_invocation
from tests.units.test_assemble_final_cut_execution import test_assemble_final_cut_execution
from tests.units.test_real_qwen3_tts_script_execution import test_real_qwen3_tts_script_execution
from tests.units.test_real_ltx_video_script_execution import test_real_ltx_video_script_execution
from tests.units.test_simulation_parse_duration_all_formats import test_simulation_parse_duration_all_formats
from tests.units.test_simulation_effect_pydantic_round_trip import test_simulation_effect_pydantic_round_trip
from tests.units.test_simulation_event_store_append_replay_ordering import test_simulation_event_store_append_replay_ordering
from tests.units.test_simulation_event_store_idempotent_dedup import test_simulation_event_store_idempotent_dedup
from tests.units.test_simulation_event_store_read_since_window import test_simulation_event_store_read_since_window
from tests.units.test_simulation_timeline_projection_script_to_slots import test_simulation_timeline_projection_script_to_slots
from tests.units.test_simulation_timeline_projection_merge_and_delivered import test_simulation_timeline_projection_merge_and_delivered
from tests.units.test_simulation_timeline_projection_delete_scene import test_simulation_timeline_projection_delete_scene
from tests.units.test_simulation_timeline_projection_reorder_scenes import test_simulation_timeline_projection_reorder_scenes
from tests.units.test_timeline_validation_suite import test_timeline_validation_suite
from tests.units.test_simulation_jobs_projection_full_lifecycle import test_simulation_jobs_projection_full_lifecycle
from tests.units.test_simulation_jobs_projection_dirty_clean_tracking import test_simulation_jobs_projection_dirty_clean_tracking
from tests.units.test_simulation_vm_projection_multi_role_fleet import test_simulation_vm_projection_multi_role_fleet
from tests.units.test_simulation_budget_projection_exceeded_detection import test_simulation_budget_projection_exceeded_detection
from tests.units.test_simulation_state_projection_full_phase_machine import test_simulation_state_projection_full_phase_machine
from tests.units.test_simulation_coordinate_timeline_cascade_and_overlap import test_simulation_coordinate_timeline_cascade_and_overlap
from tests.units.test_simulation_bdd_tts_fleet_cold_start import test_simulation_bdd_tts_fleet_cold_start
from tests.units.test_simulation_bdd_single_block_tts_inference import test_simulation_bdd_single_block_tts_inference
from tests.units.test_simulation_bdd_multi_block_tts_reconciliation import test_simulation_bdd_multi_block_tts_reconciliation
from tests.units.test_simulation_bdd_voice_continuity_across_scenes import test_simulation_bdd_voice_continuity_across_scenes
from tests.units.test_simulation_bdd_ltx_fleet_scale_up import test_simulation_bdd_ltx_fleet_scale_up
from tests.units.test_simulation_bdd_single_clip_video_generation import test_simulation_bdd_single_clip_video_generation
from tests.units.test_simulation_bdd_multi_scene_video_otio_assembly import test_simulation_bdd_multi_scene_video_otio_assembly
from tests.units.test_simulation_bdd_audio_video_duration_alignment import test_simulation_bdd_audio_video_duration_alignment
from tests.units.test_simulation_bdd_tts_retry_after_failure import test_simulation_bdd_tts_retry_after_failure
from tests.units.test_simulation_bdd_vm_preemption_recovery import test_simulation_bdd_vm_preemption_recovery
from tests.units.test_simulation_bdd_budget_gated_provisioning import test_simulation_bdd_budget_gated_provisioning
from tests.units.test_simulation_bdd_script_revision_selective_requeue import test_simulation_bdd_script_revision_selective_requeue
from tests.units.test_simulation_bdd_final_assembly_real_media import test_simulation_bdd_final_assembly_real_media
from tests.units.test_simulation_bdd_partial_failure_isolated_recovery import test_simulation_bdd_partial_failure_isolated_recovery
from tests.units.test_simulation_bdd_full_fleet_teardown_cost_accounting import test_simulation_bdd_full_fleet_teardown_cost_accounting
from tests.units.test_covering_perplexity_verify_live import test_covering_perplexity_verify_live
from tests.units import test_simulation_max_capacity_pipeline
from tests.units.test_simulation_gsa_wal_expanded import (
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
from tests.units.test_simulation_provisioner_expanded import (
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
from tests.units.test_simulation_voice_continuity_expanded import (
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
from tests.units.test_simulation_otio_assembly_expanded import (
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

# List of all test cases: (name, function, category)
TEST_CASES = [
    # 1. Simulation Covers (Consequential Claims subset)
    ("test_covering_gsa_wal_concurrency_isolation", test_covering_gsa_wal_concurrency_isolation, "Simulation Cover"),
    ("test_covering_scenario_agent_live_prompt_turn", test_covering_scenario_agent_live_prompt_turn, "Simulation Cover"),
    ("test_covering_audio_agent_tts_job_queueing", test_covering_audio_agent_tts_job_queueing, "Simulation Cover"),
    ("test_simulation_video_agent_ltx_job_queueing", test_simulation_video_agent_ltx_job_queueing, "Simulation Cover"),
    ("test_covering_provisioner_vast_offers_search", test_covering_provisioner_vast_offers_search, "Simulation Cover"),
    ("test_covering_vast_create_and_destroy_lifecycle", test_covering_vast_create_and_destroy_lifecycle, "Simulation Cover"),
    ("test_covering_ssh_handshake_and_docker_health", test_covering_ssh_handshake_and_docker_health, "Simulation Cover"),
    ("test_covering_audio_loudness_normalizer_compilation", test_covering_audio_loudness_normalizer_compilation, "Simulation Cover"),
    ("test_covering_coordinate_timeline_dynamic_drift", test_covering_coordinate_timeline_dynamic_drift, "Simulation Cover"),
    ("test_covering_budget_limit_aborted_gate", test_covering_budget_limit_aborted_gate, "Simulation Cover"),

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
    ("test_simulation_parse_duration_all_formats", test_simulation_parse_duration_all_formats, "Process Tests"),
    ("test_simulation_effect_pydantic_round_trip", test_simulation_effect_pydantic_round_trip, "Process Tests"),
    ("test_simulation_event_store_append_replay_ordering", test_simulation_event_store_append_replay_ordering, "Process Tests"),
    ("test_simulation_event_store_idempotent_dedup", test_simulation_event_store_idempotent_dedup, "Process Tests"),
    ("test_simulation_event_store_read_since_window", test_simulation_event_store_read_since_window, "Process Tests"),
    ("test_simulation_timeline_projection_script_to_slots", test_simulation_timeline_projection_script_to_slots, "Process Tests"),
    ("test_simulation_timeline_projection_merge_and_delivered", test_simulation_timeline_projection_merge_and_delivered, "Process Tests"),
    ("test_simulation_timeline_projection_delete_scene", test_simulation_timeline_projection_delete_scene, "Process Tests"),
    ("test_simulation_timeline_projection_reorder_scenes", test_simulation_timeline_projection_reorder_scenes, "Process Tests"),
    ("test_timeline_validation_suite", test_timeline_validation_suite, "Process Tests"),
    ("test_simulation_jobs_projection_full_lifecycle", test_simulation_jobs_projection_full_lifecycle, "Process Tests"),
    ("test_simulation_jobs_projection_dirty_clean_tracking", test_simulation_jobs_projection_dirty_clean_tracking, "Process Tests"),
    ("test_simulation_vm_projection_multi_role_fleet", test_simulation_vm_projection_multi_role_fleet, "Process Tests"),
    ("test_simulation_budget_projection_exceeded_detection", test_simulation_budget_projection_exceeded_detection, "Process Tests"),
    ("test_simulation_state_projection_full_phase_machine", test_simulation_state_projection_full_phase_machine, "Process Tests"),
    ("test_simulation_coordinate_timeline_cascade_and_overlap", test_simulation_coordinate_timeline_cascade_and_overlap, "Process Tests"),

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
    ("Maximum Capacity Test", test_simulation_max_capacity_pipeline.run_test, "Maximum Capacity"),

    # 4. BDD Integration Tests
    ("test_simulation_bdd_tts_fleet_cold_start", test_simulation_bdd_tts_fleet_cold_start, "BDD Integration"),
    ("test_simulation_bdd_single_block_tts_inference", test_simulation_bdd_single_block_tts_inference, "BDD Integration"),
    ("test_simulation_bdd_multi_block_tts_reconciliation", test_simulation_bdd_multi_block_tts_reconciliation, "BDD Integration"),
    ("test_simulation_bdd_voice_continuity_across_scenes", test_simulation_bdd_voice_continuity_across_scenes, "BDD Integration"),
    ("test_simulation_bdd_ltx_fleet_scale_up", test_simulation_bdd_ltx_fleet_scale_up, "BDD Integration"),
    ("test_simulation_bdd_single_clip_video_generation", test_simulation_bdd_single_clip_video_generation, "BDD Integration"),
    ("test_simulation_bdd_multi_scene_video_otio_assembly", test_simulation_bdd_multi_scene_video_otio_assembly, "BDD Integration"),
    ("test_simulation_bdd_audio_video_duration_alignment", test_simulation_bdd_audio_video_duration_alignment, "BDD Integration"),
    ("test_simulation_bdd_tts_retry_after_failure", test_simulation_bdd_tts_retry_after_failure, "BDD Integration"),
    ("test_simulation_bdd_vm_preemption_recovery", test_simulation_bdd_vm_preemption_recovery, "BDD Integration"),
    ("test_simulation_bdd_budget_gated_provisioning", test_simulation_bdd_budget_gated_provisioning, "BDD Integration"),
    ("test_simulation_bdd_script_revision_selective_requeue", test_simulation_bdd_script_revision_selective_requeue, "BDD Integration"),
    ("test_simulation_bdd_final_assembly_real_media", test_simulation_bdd_final_assembly_real_media, "BDD Integration"),
    ("test_simulation_bdd_partial_failure_isolated_recovery", test_simulation_bdd_partial_failure_isolated_recovery, "BDD Integration"),
    ("test_simulation_bdd_full_fleet_teardown_cost_accounting", test_simulation_bdd_full_fleet_teardown_cost_accounting, "BDD Integration"),
    ("test_covering_perplexity_verify_live", test_covering_perplexity_verify_live, "BDD Integration"),
]

# Load DeepSeek API key
DEEPSEEK_KEY_PATH = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
api_key = None
if os.path.exists(DEEPSEEK_KEY_PATH):
    try:
        with open(DEEPSEEK_KEY_PATH, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
    except Exception:
        pass

def run_subagent_audit(test_name: str) -> dict:
    if not api_key:
        return {"verdict": "N/A", "reasoning": "DeepSeek API key not found. Congruence audit skipped."}

    # 1. Read documentation
    docs_content = ""
    brain_dir = None
    try:
        brain_root = pathlib.Path("/Users/orpington/.gemini/antigravity/brain")
        conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
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

    impl_plan_path = PROJECT_ROOT / "tests" / "units" / "simulation_covers_implementation_plan.md"
    if impl_plan_path.exists():
        try:
            docs_content += "=== DOCUMENTATION: simulation_covers_implementation_plan.md ===\n"
            docs_content += impl_plan_path.read_text(encoding="utf-8") + "\n\n"
        except Exception:
            pass

    if not docs_content:
        docs_content = "No specific documentation file found."

    # 2. Read test code file
    test_file_path = PROJECT_ROOT / "tests" / "units" / f"{test_name}.py"
    if test_name == "Maximum Capacity Test":
        test_file_path = PROJECT_ROOT / "tests" / "units" / "test_simulation_max_capacity_pipeline.py"

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
        resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=5.0)
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip()
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
    except httpx.RequestError as re:
        return {"verdict": "N/A", "reasoning": f"DeepSeek API is offline/unreachable: {re}"}
    except Exception as e:
        return {"verdict": "FAIL", "reasoning": f"Error during subagent audit execution: {e}"}

def run_architecture_test():
    from tests.runner.architecture_checker import run_agentic_architecture_test
    run_agentic_architecture_test()

def main():
    # Execute the architecture validation check first
    run_architecture_test()

    parser = argparse.ArgumentParser(description="Documentary Pipeline CLI Test Runner")
    parser.add_argument("tests", nargs="*", help="Filter test cases by name (exact or substring).")
    parser.add_argument("--category", help="Only run tests in a specific category.")
    parser.add_argument("--list", action="store_true", help="List all available test cases and exit.")
    args = parser.parse_args()

    # 1. Handle --list
    if args.list:
        print("\n=== Available Test Cases ===")
        current_cat = None
        for name, _, category in TEST_CASES:
            if category != current_cat:
                current_cat = category
                print(f"\n[{category}]")
            print(f"  - {name}")
        print()
        sys.exit(0)

    # 2. Filter test cases
    selected_tests = []
    for name, func, category in TEST_CASES:
        # Filter by category
        if args.category and args.category.lower() not in category.lower():
            continue
        # Filter by test name
        if args.tests:
            matched = False
            for filter_term in args.tests:
                if filter_term.lower() in name.lower():
                    matched = True
                    break
            if not matched:
                continue
        selected_tests.append((name, func, category))

    if not selected_tests:
        print("\n❌ No test cases matched the filters.")
        sys.exit(1)

    print(f"\n🚀 Running {len(selected_tests)} test case(s)...")

    results = []
    passed_count = 0
    failed_count = 0
    skipped_count = 0

    for idx, (name, func, category) in enumerate(selected_tests, 1):
        print("\n" + "=" * 80)
        print(f"[{idx}/{len(selected_tests)}] RUNNING: {name} ({category})")
        print("=" * 80)

        # Run Congruence Audit first (unconditional)
        print(f"🔍 Running fresh subagent congruence audit for '{name}'...")
        audit_res = run_subagent_audit(name)
        audit_verdict = audit_res.get("verdict", "FAIL")
        audit_reasoning = audit_res.get("reasoning", "Audit failed or returned no explanation.")
        print(f"🔍 Audit Verdict: {audit_verdict}")
        if audit_verdict == "FAIL":
            print(f"⚠️  Audit Reasoning: {audit_reasoning}")

        start_time = time.time()
        status = "PASSED"
        err_msg = ""
        
        try:
            func()
        except BaseException as e:
            if e.__class__.__name__ == "Skipped":
                status = "SKIPPED"
                err_msg = str(e)
            else:
                status = "FAILED"
                err_msg = traceback.format_exc()

        elapsed = time.time() - start_time
        print("-" * 80)
        
        if status == "PASSED":
            print(f"🟢 PASSED: '{name}' in {elapsed:.2f} seconds.")
            passed_count += 1
        elif status == "SKIPPED":
            print(f"🟡 SKIPPED: '{name}' in {elapsed:.2f} seconds. Reason: {err_msg}")
            skipped_count += 1
        else:
            print(f"🔴 FAILED: '{name}' in {elapsed:.2f} seconds.")
            print(f"\n{err_msg}")
            failed_count += 1

        results.append({
            "name": name,
            "status": status,
            "elapsed": elapsed,
            "audit_verdict": audit_verdict,
            "audit_reasoning": audit_reasoning
        })

    # Render final report table
    print("\n" + "=" * 100)
    print("                                   FINAL CLI RUN REPORT")
    print("=" * 100)
    print(f"{'Test Name':<50} | {'Status':<8} | {'Time (s)':<8} | {'Audit':<8}")
    print("-" * 100)
    for res in results:
        status_color = "🟢 " if res["status"] == "PASSED" else "🔴 " if res["status"] == "FAILED" else "🟡 "
        audit_color = "🟢 " if res["audit_verdict"] == "PASS" else "🔴 " if res["audit_verdict"] == "FAIL" else "⚪ "
        
        print(f"{res['name']:<50} | {status_color + res['status']:<8} | {res['elapsed']:<8.2f} | {audit_color + res['audit_verdict']:<8}")
    print("=" * 100)
    print(f"SUMMARY: {passed_count} Passed, {failed_count} Failed, {skipped_count} Skipped.")
    print("=" * 100 + "\n")

    has_audit_failure = any(res["audit_verdict"] == "FAIL" for res in results)
    sys.exit(0 if (failed_count == 0 and not has_audit_failure) else 1)

if __name__ == "__main__":
    main()
