import tempfile
import shutil
import os
from pathlib import Path
import pytest
from event_store import EventStore, EventRecord
from effects import NoOp, UpdateScript, ScriptBlock
from uuid_utils import uuid7


@pytest.fixture
def temp_log_dir():
    dirpath = tempfile.mkdtemp()
    yield dirpath
    shutil.rmtree(dirpath)


def test_event_store_lifecycle(temp_log_dir):
    run_id = "test-run-1"
    store = EventStore(temp_log_dir)

    # Database file should not exist initially
    db_path = Path(temp_log_dir) / f"events_{run_id}.db"
    assert not db_path.exists()

    # Append first event
    effect1 = NoOp(run_id=run_id, agent="test_agent", reason="initialization")
    record1 = store.append(run_id, effect1, otio_hash_before="hash_0")

    # Verify database was created and contains event
    assert db_path.exists()
    assert record1.seq == 1
    assert record1.effect.effect_id == effect1.effect_id
    assert record1.otio_hash_before == "hash_0"

    # Append second event
    effect2 = UpdateScript(
        run_id=run_id,
        agent="test_agent",
        blocks=[
            ScriptBlock(
                scene_num=1,
                block_id="block_1",
                speaker="narrator",
                text="Hello world",
                duration_sec=5.0
            )
        ]
    )
    record2 = store.append(run_id, effect2, otio_hash_before="hash_1")
    assert record2.seq == 2
    assert record2.effect.effect_id == effect2.effect_id

    # Test read_all
    records = store.read_all(run_id)
    assert len(records) == 2
    assert records[0].seq == 1
    assert records[1].seq == 2
    assert isinstance(records[1].effect, UpdateScript)
    assert records[1].effect.blocks[0].text == "Hello world"

    # Test read_since
    records_since = store.read_since(run_id, from_seq=1)
    assert len(records_since) == 1
    assert records_since[0].seq == 2


def test_event_store_idempotency(temp_log_dir):
    run_id = "test-run-2"
    store = EventStore(temp_log_dir)

    effect = NoOp(run_id=run_id, agent="test_agent", reason="idempotent test")
    
    # First append
    record1 = store.append(run_id, effect, otio_hash_before="hash_a")
    assert record1.seq == 1

    # Second append with same effect (should trigger unique constraint rollback and find)
    record2 = store.append(run_id, effect, otio_hash_before="hash_a")
    assert record2.seq == 1
    assert record2.effect.effect_id == effect.effect_id

    # Verify database only contains one row
    all_records = store.read_all(run_id)
    assert len(all_records) == 1
