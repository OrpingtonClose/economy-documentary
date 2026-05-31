"import pytest
from unittest.mock import AsyncMock, patch
from effect_parser import parse_agent_text_multi, parse_human_text_multi, _SingleEffect, _MultiEffect, _UpdateScriptEffect, _NoOpEffect, _VMAllocatedEffect
from effects import UpdateScript, NoOp, VMAllocated, ScriptBlock
import uuid


@pytest.mark.asyncio
async def test_parse_agent_text_permitted_kinds():
    run_id = "test-run-1"
    
    # 1. Test update_script is permitted for scenario agent
    mock_single_parsed = _SingleEffect(
        chain_of_thought="Writing script",
        effect=_UpdateScriptEffect(
            blocks=[
                ScriptBlock(
                    scene_num=1,
                    block_id="s1_block1",
                    speaker="narrator",
                    text="This is a test script narration text",
                    duration_sec=10.0
                )
            ]
        ),
        confidence=9
    )
    
    mock_client = AsyncMock()
    mock_client.chat.completions.create.return_value = mock_single_parsed
    
    with patch("effect_parser._ds_async_client", return_value=mock_client):
        effects = await parse_agent_text_multi(
            agent_id="scenario",
            text="I want to update script text",
            run_id=run_id
        )
        assert len(effects) == 1
        assert isinstance(effects[0], UpdateScript)
        assert effects[0].run_id == run_id
        assert effects[0].agent == "scenario"
        assert effects[0].blocks[0].text == "This is a test script narration text"

    # 2. Test vm_allocated is NOT permitted for scenario agent
    mock_single_forbidden = _SingleEffect(
        chain_of_thought="Allocating VM which is not permitted",
        effect=_VMAllocatedEffect(
            instance_id="123",
            role="tts",
            offer_id="456",
            worker_url="http://1.2.3.4:9000",
            gpu_type="RTX 4090",
            cost_per_hour=0.5
        ),
        confidence=9
    
<truncated 1445 bytes>