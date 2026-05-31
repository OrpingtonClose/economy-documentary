> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Plan: Deploy infra_agent guardian on VMs

# Implementation Plan: Deploy infra_agent Guardian on VMs via Bootstrap Scripts

## Overview

Currently, `vast_provisioning.py` uses an inline `onstart_cmd` to run `gpu_worker.py` directly. We need to replace this with bootstrap scripts that also start the `infra_agent` guardian for idle/lifetime self-destruction. The bootstrap scripts already exist (`qwen3_tts_worker_bootstrap.sh` and `ltx_video_worker_bootstrap.sh`), so the main change is in `vast_provisioning.py`.

## 1. Files to Modify

### 1.1 `server/strands_agents/shared_a2a/vast_provisioning.py`

#### Change 1: Add bootstrap script path constants (after line 20)

Add constants pointing to the bootstrap scripts:

```python
# After line 20 (imports)
import os
import subprocess
import json

# Add these constants after the imports
# Paths to bootstrap scripts (relative to repo root)
BOOTSTRAP_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
TTS_BOOTSTRAP_SCRIPT = os.path.join(BOOTSTRAP_SCRIPTS_DIR, "qwen3_tts_worker_bootstrap.sh")
VIDEO_BOOTSTRAP_SCRIPT = os.path.join(BOOTSTRAP_SCRIPTS_DIR, "ltx_video_worker_bootstrap.sh")
```

#### Change 2: Modify `provision_specific_offer` function (lines ~100-200)

Replace the inline `onstart_cmd` with a reference to the bootstrap script. The key change is in the `_vast_cmd` call that creates the instance:

```python
@tool
def provision_specific_offer(
    offer_id: int,
    disk_gb: int = 150,
    docker_image: str = "pytorch/pytorch:2.10.0-cuda12.6-cudnn9-runtime",
    label: str = "documentary-ltx",
    mode: str = "auto",
    role: str = "video",  # "video" or "tts"
    worker_id: str = "",
    worker_voice_id: str = "",  # Required for TTS role
) -> str:
    """Provision a specific Vast.ai offer as a worker.

    Uses bootstrap scripts that install infra-agent guardian for
    idle/lifetime self-destruction alongside the worker service.

    Args:
        offer_id: Vast.ai offer ID from search_gpu_offers
        disk_gb: Disk size in GB
        docker_image: Docker image to use
        label: Instance label
        mode: "auto" or "manual"
        role: "video" or "tts"
        worker_id: Unique worker identifier (e.g., "video-h200-01")
        worker_voice_id: Voice ID for TTS workers (required if role="tts")
    """
    if role not in ("video", "tts"):
        return json.dumps({"status": "error", "error": f"Invalid role: {role}"})
    
    if role == "tts" and not worker_voice_id:
        return json.dumps({"status": "error", "error": "worker_voice_id required for TTS role"})
    
    if not worker_id:
        # Auto-generate worker ID if not provided
        import uuid
        worker_id = f"{role}-{uuid.uuid4().hex[:8]}"

    # Determine which bootstrap script to use
    if role == "tts":
        bootstrap_script = TTS_BOOTSTRAP_SCRIPT
    else:
        bootstrap_script = VIDEO_BOOTSTRAP_SCRIPT

    # Verify bootstrap script exists
    if not os.path.exists(bootstrap_script):
        return json.dumps({
            "status": "error",
            "error": f"Bootstrap script not found: {bootstrap_script}"
        })

    # Read the bootstrap script content
    with open(bootstrap_script) as f:
        bootstrap_content = f.read()

    # Build the onstart command that:
    # 1. Sets required environment variables
    # 2. Downloads and runs the bootstrap script
    onstart_cmd = (
        f"export WORKER_ID={worker_id} "
        f"export VAST_INSTANCE_ID=$VAST_INSTANCE_ID "
        f"export VAST_AI_API_KEY=$VAST_AI_API_KEY "
        f"export PLAYGROUND_BACKEND_URL=$PLAYGROUND_BACKEND_URL "
        f"export GUARDIAN_IDLE_SECONDS=900 "
        f"export GUARDIAN_MAX_LIFETIME_SECONDS=14400 "
    )
    
    if role == "tts":
        onstart_cmd += f"export WORKER_VOICE_ID={worker_voice_id} "
    
    # Add the bootstrap script execution
    onstart_cmd += (
        f"&& mkdir -p /opt/bootstrap "
        f"&& cat > /opt/bootstrap/bootstrap.sh << 'BOOTSTRAP_EOF'\n"
        f"{bootstrap_content}\n"
        f"BOOTSTRAP_EOF\n"
        f"&& chmod +x /opt/bootstrap/bootstrap.sh "
        f"&& bash /opt/bootstrap/bootstrap.sh"
    )

    try:
        result = _vast_cmd([
            "create", "instance",
            str(offer_id),
            "--disk", str(disk_gb),
            "--image", docker_image,
            "--label", label,
            "--onstart-cmd", onstart_cmd,
            "--raw",
        ])
        
        if isinstance(result, dict) and result.get("success"):
            instance_id = result.get("new_id") or result.get("id")
            return json.dumps({
                "status": "provisioned",
                "instance_id": instance_id,
                "worker_id": worker_id,
                "role": role,
                "bootstrap_script": os.path.basename(bootstrap_script),
            })
        else:
            return json.dumps({
                "status": "error",
                "error": f"Provision failed: {result}",
                "worker_id": worker_id,
            })
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "error": f"Provision error: {exc}",
            "worker_id": worker_id,
        })
```

#### Change 3: Add helper function for bootstrap script validation (after the constants)

```python
def _validate_bootstrap_script(role: str) -> str:
    """Validate and return the path to the bootstrap script for the given role."""
    script_map = {
        "video": VIDEO_BOOTSTRAP_SCRIPT,
        "tts": TTS_BOOTSTRAP_SCRIPT,
    }
    
    script_path = script_map.get(role)
    if not script_path:
        raise ValueError(f"Unknown role: {role}")
    
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"Bootstrap script not found: {script_path}")
    
    # Validate script has required env vars
    with open(script_path) as f:
        content = f.read()
    
    required_vars = ["WORKER_ID", "VAST_INSTANCE_ID", "VAST_AI_API_KEY"]
    for var in required_vars:
        if f":${{{var}:?" not in content and f":${{{var}:?" not in content:
            raise ValueError(f"Bootstrap script missing required env var check: {var}")
    
    return script_path
```

### 1.2 `scripts/qwen3_tts_worker_bootstrap.sh` - Minor fix

#### Change: Fix the `: "${VAR:?error}"` syntax (line ~30)

The current syntax `: "${WORKER_ID:?WORKER_ID must be set (e.g. tts-alex-01)}"` is correct but let's ensure consistency:

```bash
# Line ~30 - already correct, no change needed
: "${WORKER_ID:?WORKER_ID must be set (e.g. tts-alex-01)}"
: "${WORKER_VOICE_ID:?WORKER_VOICE_ID must be set (one voice per VM)}"
: "${VAST_INSTANCE_ID:?VAST_INSTANCE_ID must be set (guardian cannot self-destroy without it)}"
: "${VAST_AI_API_KEY:?VAST_AI_API_KEY must be set}"
```

### 1.3 `scripts/ltx_video_worker_bootstrap.sh` - Minor fix

#### Change: Fix the `: "${VAR:?error}"` syntax (line ~30)

Same check - already correct:

```bash
# Line ~30 - already correct, no change needed
: "${WORKER_ID:?WORKER_ID must be set (e.g. video-h200-01)}"
: "${VAST_INSTANCE_ID:?VAST_INSTANCE_ID must be set (guardian cannot self-destroy without it)}"
: "${VAST_AI_API_KEY:?VAST_AI_API_KEY must be set}"
```

## 2. Dependencies and Side Effects

### Dependencies:
1. **Bootstrap scripts must exist** at the expected paths relative to `vast_provisioning.py`
2. **Docker image** must have `bash`, `curl`, `git`, `python3`, `python3-pip`, `python3-venv` available
3. **Vast.ai API** must support `--onstart-cmd` with multi-line scripts
4. **Environment variables** `VAST_INSTANCE_ID`, `VAST_AI_API_KEY`, `PLAYGROUND_BACKEND_URL` must be set in the Vast.ai instance environment

### Side Effects:
1. **Existing workers** using the old inline `onstart_cmd` will continue to work until destroyed
2. **New workers** will have both the worker service AND the infra-agent guardian running
3. **VM lifecycle** changes: VMs will self-destruct after idle timeout or max lifetime
4. **Cost implications**: VMs will be more efficiently cleaned up, potentially reducing costs

## 3. Testing Approach

### Unit Tests (in `tests/test_vast_provisioning.py`):

```python
"""Tests for vast_provisioning.py bootstrap script integration."""
import json
import os
import tempfile
from unittest.mock import patch, MagicMock
import pytest

from server.strands_agents.shared_a2a.vast_provisioning import (
    provision_specific_offer,
    _validate_bootstrap_script,
    TTS_BOOTSTRAP_SCRIPT,
    VIDEO_BOOTSTRAP_SCRIPT,
)


class TestBootstrapScriptValidation:
    def test_tts_bootstrap_exists(self):
        """TTS bootstrap script should exist at expected path."""
        assert os.path.exists(TTS_BOOTSTRAP_SCRIPT), f"TTS bootstrap not found: {TTS_BOOTSTRAP_SCRIPT}"
    
    def test_video_bootstrap_exists(self):
        """Video bootstrap script should exist at expected path."""
        assert os.path.exists(VIDEO_BOOTSTRAP_SCRIPT), f"Video bootstrap not found: {VIDEO_BOOTSTRAP_SCRIPT}"
    
    def test_tts_bootstrap_has_required_vars(self):
        """TTS bootstrap should check for required env vars."""
        with open(TTS_BOOTSTRAP_SCRIPT) as f:
            content = f.read()
        assert ': "${WORKER_ID:?' in content
        assert ': "${VAST_INSTANCE_ID:?' in content
        assert ': "${VAST_AI_API_KEY:?' in content
    
    def test_video_bootstrap_has_required_vars(self):
        """Video bootstrap should check for required env vars."""
        with open(VIDEO_BOOTSTRAP_SCRIPT) as f:
            content = f.read()
        assert ': "${WORKER_ID:?' in content
        assert ': "${VAST_INSTANCE_ID:?' in content
        assert ': "${VAST_AI_API_KEY:?' in content
    
    def test_validate_tts_script(self):
        """_validate_bootstrap_script should return path for valid TTS script."""
        path = _validate_bootstrap_script("tts")
        assert path == TTS_BOOTSTRAP_SCRIPT
    
    def test_validate_video_script(self):
        """_validate_bootstrap_script should return path for valid video script."""
        path = _validate_bootstrap_script("video")
        assert path == VIDEO_BOOTSTRAP_SCRIPT
    
    def test_validate_invalid_role(self):
        """_validate_bootstrap_script should raise ValueError for unknown role."""
        with pytest.raises(ValueError, match="Unknown role"):
            _validate_bootstrap_script("invalid_role")


class TestProvisionSpecificOffer:
    @patch("server.strands_agents.shared_a2a.vast_provisioning._vast_cmd")
    def test_provision_video_worker(self, mock_vast_cmd):
        """Should provision video worker with correct bootstrap script."""
        mock_vast_cmd.return_value = {"success": True, "new_id": 12345}
        
        result = json.loads(provision_specific_offer(
            offer_id=123,
            disk_gb=150,
            role="video",
            worker_id="video-test-01"
        ))
        
        assert result["status"] == "provisioned"
        assert result["instance_id"] == 12345
        assert result["worker_id"] == "video-test-01"
        assert result["role"] == "video"
        assert "ltx_video_worker_bootstrap.sh" in result["bootstrap_script"]
        
        # Verify the onstart_cmd contains bootstrap script
        call_args = mock_vast_cmd.call_args[0][0]
        assert "create" in call_args
        assert "instance" in call_args
        assert "--onstart-cmd" in call_args
        onstart_idx = call_args.index("--onstart-cmd") + 1
        onstart_cmd = call_args[onstart_idx]
        assert "WORKER_ID=video-test-01" in onstart_cmd
        assert "ltx_video_worker_bootstrap.sh" in onstart_cmd or "BOOTSTRAP_EOF" in onstart_cmd
    
    @patch("server.strands_agents.shared_a2a.vast_provisioning._vast_cmd")
    def test_provision_tts_worker(self, mock_vast_cmd):
        """Should provision TTS worker with correct bootstrap script and voice ID."""
        mock_vast_cmd.return_value = {"success": True, "new_id": 12346}
        
        result = json.loads(provision_specific_offer(
            offer_id=124,
            disk_gb=100,
            role="tts",
            worker_id="tts-alex-01",
            worker_voice_id="alex"
        ))
        
        assert result["status"] == "provisioned"
        assert result["instance_id"] == 12346
        assert result["worker_id"] == "tts-alex-01"
        assert result["role"] == "tts"
        assert "qwen3_tts_worker_bootstrap.sh" in result["bootstrap_script"]
        
        # Verify voice ID is in onstart_cmd
        call_args = mock_vast_cmd.call_args[0][0]
        onstart_idx = call_args.index("--onstart-cmd") + 1
        onstart_cmd = call_args[onstart_idx]
        assert "WORKER_VOICE_ID=alex" in onstart_cmd
    
    @patch("server.strands_agents.shared_a2a.vast_provisioning._vast_cmd")
    def test_provision_tts_missing_voice_id(self, mock_vast_cmd):
        """Should error if TTS role without voice_id."""
        result = json.loads(provision_specific_offer(
            offer_id=125,
            disk_gb=100,
            role="tts",
            worker_id="tts-test-01"
        ))
        
        assert result["status"] == "error"
        assert "worker_voice_id" in result["error"]
        mock_vast_cmd.assert_not_called()
    
    @patch("server.strands_agents.shared_a2a.vast_provisioning._vast_cmd")
    def test_provision_invalid_role(self, mock_vast_cmd):
        """Should error for invalid role."""
        result = json.loads(provision_specific_offer(
            offer_id=126,
            disk_gb=100,
            role="invalid",
            worker_id="test-01"
        ))
        
        assert result["status"] == "error"
        assert "Invalid role" in result["error"]
        mock_vast_cmd.assert_not_called()
    
    @patch("server.strands_agents.shared_a2a.vast_provisioning._vast_cmd")
    def test_provision_failure(self, mock_vast_cmd):
        """Should handle Vast.ai API failure gracefully."""
        mock_vast_cmd.side_effect = RuntimeError("API error")
        
        result = json.loads(provision_specific_offer(
            offer_id=127,
            disk_gb=150,
            role="video",
            worker_id="video-test-02"
        ))
        
        assert result["status"] == "error"
        assert "API error" in result["error"]
    
    def test_auto_generated_worker_id(self):
        """Should auto-generate worker ID if not provided."""
        with patch("server.strands_agents.shared_a2a.vast_provisioning._vast_cmd") as mock_vast_cmd:
            mock_vast_cmd.return_value = {"success": True, "new_id": 12348}
            
            result = json.loads(provision_specific_offer(
                offer_id=128,
                disk_gb=150,
                role="video"
            ))
            
            assert result["status"] == "provisioned"
            assert result["worker_id"].startswith("video-")
            assert len(result["worker_id"]) > 10  # video- + 8 hex chars
```

### Integration Tests:

1. **Test bootstrap script execution locally**:
```bash
# Test that bootstrap scripts are syntactically valid
bash -n scripts/qwen