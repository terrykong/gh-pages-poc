"""Does a checkpoint taken after a borrow lose a prompt on restore?

Round trip with the real code on the whole path:

  SingleControllerActor._rollout_pump  (admission, spare pool, borrow, repayment)
  TQReplayBuffer / RolloutRecoveryLedger / InOrderSampler / DataPlaneCheckpointBarrier
  _capture_rollout_checkpoint_cut       (the exact bytes a rollout snapshot writes)
  _maybe_restore_replay_buffer + _maybe_restore_rollout_recovery
  _redispatch_restored_rollouts -> _admit_reserved_prompt_groups  (the discard site)

Faked: generation (``RolloutManager._impl.run_rollout``) and the tensor
converter (``record_to_train_batch``). Nothing on the checkpoint path is stubbed.
There is no train pump, so after the restore the test bumps ``trainer_version``
once -- what training step 0 would do -- so the in-order gate opens.

Two cases. With the cursor the checkpoint saved, every prompt comes back and
``groups_considered == groups_redispatched``. With the cursor rebuilt from
``trainer_version`` (the fallback for a checkpoint with no
``sampler_dispatch_index``), the re-admitted batch lands on a step that is
already full and every reserved prompt is discarded -- the one way the discard
branch in ``_admit_reserved_prompt_groups`` fires.

Run from the repo root:  PYTHONPATH=. pytest <this file> -q -s
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import torch

from nemo_rl.algorithms.async_utils import replay_buffer as _rb
from nemo_rl.algorithms.async_utils.replay_buffer import (
    REPLAY_BUFFER_METADATA_FILENAME,
    DataPlaneCheckpointBarrier,
    TQReplayBuffer,
)
from nemo_rl.algorithms.async_utils.staleness_sampler import InOrderSampler
from nemo_rl.algorithms.grpo import GRPOConfig
from nemo_rl.algorithms.single_controller import (
    DATA_PLANE_CHECKPOINT_DIR,
    SingleControllerActor,
)
from nemo_rl.algorithms.single_controller_utils.config import RolloutRecoveryConfig
from nemo_rl.data_plane.adapters.noop import NoOpDataPlaneClient
from nemo_rl.distributed.batched_data_dict import BatchedDataDict
from nemo_rl.experience.failures import RolloutDataFailure
from nemo_rl.experience.interfaces import PromptGroupRecord
from nemo_rl.experience.rollout_manager import (
    RolloutManager,
    RolloutRetryPolicy,
    RolloutStats,
)
from nemo_rl.experience.rollout_recovery import (
    ROLLOUT_RECOVERY_STATE_FILENAME,
    PromptGroupPhase,
    RolloutRecoveryLedger,
)
from tests.unit.single_controller._checkpoint_scenarios import (
    _FIELDS,
    PARTITION,
    _stub_converter,
)

GROUP_SIZE = 2  # num_generations_per_prompt
PROMPTS_PER_STEP = 3
LOOKAHEAD = 2  # in_order.max_lookahead_versions
CAPACITY = 64
_TIMEOUT_S = 20.0


# ── the two fakes ───────────────────────────────────────────────────────────


class _Generation:
    """Stands in for AsyncRolloutImpl. Finishes at once unless told to wait or fail."""

    def __init__(self) -> None:
        self.hold: dict[int, asyncio.Event] = {}
        self.fail: set[int] = set()
        self.finished: list[int] = []

    async def run_rollout(self, input_sample: dict[str, Any]) -> PromptGroupRecord:
        idx = input_sample["idx"]
        gate = self.hold.get(idx)
        if gate is not None:
            await gate.wait()
        if idx in self.fail:
            raise RolloutDataFailure(f"prompt {idx} is bad on purpose")
        self.finished.append(idx)
        return PromptGroupRecord(
            prompt_idx=idx,
            prompt=[],
            extra_env_info=None,
            metadata={},
            completions=[],
            rollout_metrics={},
        )


class _Loader:
    """A dataloader: batches to yield, plus the dataset the restore rehydrates from."""

    def __init__(self, batches: list[BatchedDataDict], dataset: dict[int, Any]) -> None:
        self._batches = batches
        self.dataset = dataset

    def __iter__(self):
        return iter(self._batches)

    def __len__(self) -> int:
        return len(self._batches)

    def state_dict(self) -> dict[str, Any]:
        return {"fake": True}


# ── the real components, wired the way the actor wires them ─────────────────


def _prompt(idx: int) -> dict[str, Any]:
    return {"idx": idx, "message_log": [{"role": "user", "content": f"p{idx}"}]}


def _batch(idxs: list[int]) -> BatchedDataDict:
    return BatchedDataDict(
        {
            "idx": list(idxs),
            "message_log": [[{"role": "user", "content": f"p{i}"}] for i in idxs],
        }
    )


class _TrackingRolloutManager(RolloutManager):
    """The real RolloutManager, plus a note of which prompt each group id belongs to."""

    def reserve_prompt_group(self, cut, input_sample, **kwargs) -> str:
        group_id = super().reserve_prompt_group(cut, input_sample, **kwargs)
        self.prompt_of[group_id] = input_sample["idx"]
        return group_id

    def discard_prompt_group(self, cut, group_id: str) -> None:
        group = self._recovery_ledger.get_group(group_id)
        self.discarded.append((group_id, int(group.prompt_ref.sample_id), group.phase))
        super().discard_prompt_group(cut, group_id)


def _manager(
    buffer: TQReplayBuffer, barrier: DataPlaneCheckpointBarrier, gen: _Generation
) -> _TrackingRolloutManager:
    mgr = object.__new__(_TrackingRolloutManager)
    mgr._impl = gen
    mgr._tokenizer = None
    mgr._num_generations_per_prompt = GROUP_SIZE
    mgr._rollout_recovery_config = RolloutRecoveryConfig()
    mgr._tq_buffer = buffer
    mgr._recovery_ledger = RolloutRecoveryLedger()
    mgr._data_plane_checkpoint_barrier = barrier
    mgr._env_handles = {}
    mgr._weight_version = 0
    mgr._retry_policy = RolloutRetryPolicy(
        max_infra_attempts=1,
        max_data_attempts=1,
        max_gym_row_attempts=1,
        max_skipped_prompts=8,
    )
    mgr._stats = RolloutStats()
    mgr._canonical_groups_finalized = 0
    mgr._canonical_output_tokens = 0
    mgr._recovery_siblings_reused = 0
    mgr._recovery_siblings_redispatched = 0
    mgr._skipped_prompts = 0
    mgr._consecutive_infra_drops = 0
    mgr.prompt_of: dict[str, int] = {}
    mgr.discarded: list[tuple[str, int, PromptGroupPhase]] = []
    return mgr


def _client(register: bool) -> NoOpDataPlaneClient:
    dp = NoOpDataPlaneClient()
    if register:
        dp.register_partition(
            partition_id=PARTITION,
            fields=list(_FIELDS),
            num_samples=CAPACITY * GROUP_SIZE,
            consumer_tasks=["train"],
        )
    return dp


def _controller(
    *,
    dp: NoOpDataPlaneClient,
    loader: _Loader,
    gen: _Generation,
    dispatch_index: int | None = None,
    spares: list[dict[str, Any]] | None = None,
    checkpoint_path: str | None = None,
    checkpoint_metadata: dict[str, Any] | None = None,
) -> Any:
    barrier = DataPlaneCheckpointBarrier()
    buffer = TQReplayBuffer(
        dp,
        partition_id=PARTITION,
        pad_value_dict={"input_ids": 0},
        include_message_violation_fields=False,
        require_routed_experts=False,
    )
    buffer.set_data_plane_checkpoint_barrier(barrier)

    cls = SingleControllerActor.__ray_metadata__.modified_class
    c = object.__new__(cls)
    c._data_plane_checkpoint_barrier = barrier
    c._buffer = buffer
    c._dp_client = dp
    c._partition_id = PARTITION
    c._rollout_manager = _manager(buffer, barrier, gen)
    c._sampler = InOrderSampler(buffer, max_lookahead_versions=LOOKAHEAD)
    if dispatch_index is not None:
        c._sampler.restore_dispatch_index(dispatch_index)
    elif checkpoint_path is not None:
        # single_controller.py: a checkpoint without sampler_dispatch_index
        # rebuilds the cursor from the restored trainer version.
        c._sampler.set_dispatch_index(0)
    c._async_cfg = SimpleNamespace(
        max_inflight_prompts=16,
        max_buffered_rollouts=CAPACITY,
        diagnostics=False,
        sampler=SimpleNamespace(name="in_order"),
        rollout_failure=SimpleNamespace(
            on_dropped_prompt="replace",
            max_replacement_attempts=1,
            replacement_reserve_prompts=PROMPTS_PER_STEP,
            min_step_batch_fraction=0.9,
        ),
    )
    c._algo_cfg = GRPOConfig.model_construct(
        max_num_epochs=1,
        num_prompts_per_step=PROMPTS_PER_STEP,
        num_generations_per_prompt=GROUP_SIZE,
    )
    c._master_config = SimpleNamespace(
        grpo=c._algo_cfg,
        token_capture=SimpleNamespace(enabled=False),
    )
    c._dataloader = loader
    c._rollout_permitted = asyncio.Event()
    c._rollout_permitted.set()
    c._rollout_exhausted = asyncio.Event()
    c._buffer_capacity = asyncio.Semaphore(CAPACITY)
    c._inflight_rollouts = 0
    c._inflight_by_group_id = {}
    c._dispatched_rollouts = set()
    c._trainer_version = 0
    c._train_steps = 0
    c._current_epoch = 0
    c._sampler_stamps_target_steps = False
    c._rollout_recovery_enabled = True
    c._batch_shortfall = {}
    c._batch_replacements = {}
    c._batch_promotions = {}
    c._finalizer_actors = []
    c._replacement_reserve = deque(spares or [])
    c._rollout_slot_waiters = 0
    c._rollout_permitted_waiters = 0
    c._buffer_capacity_waiters = 0
    c._rollout_completion_durations_s = deque(maxlen=10_000)
    c._rollout_queue_wait_durations_s = deque(maxlen=10_000)
    c._telemetry_sample_index = 0
    c._telemetry_started_at = 0.0
    c._logger = MagicMock()
    c._last_checkpoint_path = checkpoint_path
    c._data_plane_checkpoint_metadata = checkpoint_metadata
    return c


async def _wait_for(pred, what: str, pump: asyncio.Task) -> None:
    deadline = asyncio.get_running_loop().time() + _TIMEOUT_S
    while not pred():
        if pump.done():
            pump.result()  # re-raise a pump failure instead of timing out on it
            raise AssertionError(f"pump exited before: {what}")
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"timed out waiting for: {what}")
        await asyncio.sleep(0.005)


def _stamps(buffer: TQReplayBuffer, prompt_of: dict[str, int]) -> dict[int, list[int]]:
    """target_step -> prompt idx of every slot stamped for it (ready or not)."""
    out: dict[int, list[int]] = {}
    for gid, step in zip(buffer._group_ids, buffer.target_step_list):
        out.setdefault(step, []).append(prompt_of[gid])
    return {k: sorted(v) for k, v in sorted(out.items())}


# ── the round trip ──────────────────────────────────────────────────────────


async def _round_trip(tmp_path: Path, *, saved_cursor: bool = True) -> dict[str, Any]:
    """saved_cursor=False restores the way a checkpoint with no
    ``sampler_dispatch_index`` would: cursor rebuilt from trainer_version."""
    _rb.record_to_train_batch = _stub_converter  # tensor converter only

    dataset = {i: _prompt(i) for i in range(15)}
    # B0 -> step 0 | B1 -> spare pool | B2 -> step 1 | B3 -> step 2 | B4 -> waits at gate
    batches = [_batch([0, 1, 2]), _batch([3, 4, 5]), _batch([6, 7, 8]),
               _batch([9, 10, 11]), _batch([12, 13, 14])]

    gen_a = _Generation()
    gen_a.hold[1] = asyncio.Event()  # step 0's second prompt runs long ...
    gen_a.fail.add(1)  # ... and then fails, so step 0 needs a replacement
    gen_a.hold[3] = asyncio.Event()  # the spare that repays the lender stays in flight

    dp_a = _client(register=True)
    a = _controller(dp=dp_a, loader=_Loader(batches, dataset), gen=gen_a)
    ledger_a = a._rollout_manager.recovery_ledger
    pump_a = asyncio.create_task(a._rollout_pump())

    # Steps 0..2 admitted and finished except prompt 1; B4 reserved at the gate.
    await _wait_for(
        lambda: sum(a._buffer.ready_list) == 8
        and sum(1 for g in ledger_a.groups() if g.phase is PromptGroupPhase.RESERVED) == 3,
        "steps 0-2 generated (minus prompt 1) and batch B4 held at the gate",
        pump_a,
    )
    before_drop = _stamps(a._buffer, a._rollout_manager.prompt_of)

    gen_a.hold[1].set()  # prompt 1 now fails -> replace -> borrow -> repay
    await _wait_for(
        lambda: a._batch_promotions == {0: 1}
        and any(
            a._rollout_manager.prompt_of.get(gid) == 3 for gid in a._buffer._group_ids
        ),
        "step 0 borrowed a finished group and the spare (prompt 3) was dispatched",
        pump_a,
    )
    after_borrow = _stamps(a._buffer, a._rollout_manager.prompt_of)
    ledger_at_save = [
        (int(g.prompt_ref.sample_id), g.phase.name, g.target_step)
        for g in ledger_a.groups()
    ]

    print("\n--- live, before the drop        :", before_drop)
    print("--- live, after borrow + repay   :", after_borrow)
    print("--- ledger in the checkpoint     :", sorted(ledger_at_save))

    # ---- the checkpoint, exactly as a rollout snapshot takes it ----
    ckpt = tmp_path / "step_0"
    ckpt.mkdir()
    async with a._data_plane_checkpoint_barrier.checkpoint() as cut:
        snap = await a._capture_rollout_checkpoint_cut(cut, ckpt)
    torch.save(snap.replay_metadata, ckpt / REPLAY_BUFFER_METADATA_FILENAME)
    (ckpt / ROLLOUT_RECOVERY_STATE_FILENAME).write_bytes(snap.rollout_recovery_payload)

    pump_a.cancel()
    try:
        await pump_a
    except (asyncio.CancelledError, Exception):
        pass

    # ---- restart: new process, nothing in memory ----
    dp_b = _client(register=False)
    metadata = dp_b.load_checkpoint(ckpt / DATA_PLANE_CHECKPOINT_DIR)
    gen_b = _Generation()
    b = _controller(
        dp=dp_b,
        loader=_Loader([], dataset),  # the loader resumes after B4: nothing left
        gen=gen_b,
        dispatch_index=snap.sampler_dispatch_index if saved_cursor else None,
        spares=snap.replacement_reserve,
        checkpoint_path=str(ckpt),
        checkpoint_metadata=metadata,
    )
    restored = await b._maybe_restore_replay_buffer()
    await b._maybe_restore_rollout_recovery(restored_replay_groups=restored)
    ledger_b = b._rollout_manager.recovery_ledger
    restored_ledger = {g.group_id: int(g.prompt_ref.sample_id) for g in ledger_b.groups()}
    b._rollout_manager.prompt_of.update(restored_ledger)
    b._rollout_manager.prompt_of.update(a._rollout_manager.prompt_of)

    pump_b = asyncio.create_task(b._rollout_pump())
    # The reserved batch is still behind the in-order gate, exactly as it was at
    # save time. There is no train pump here, so stand in for it: once the
    # restored ADMITTED work is back in flight, "train" step 0 so the gate opens.
    await _wait_for(
        lambda: any(b._rollout_manager.prompt_of.get(g) == 3 for g in b._buffer._group_ids),
        "the restored spare (prompt 3) was redispatched",
        pump_b,
    )
    b._trainer_version = 1
    await asyncio.wait_for(pump_b, timeout=_TIMEOUT_S)
    print("--- restored cursor              :",
          snap.sampler_dispatch_index if saved_cursor else "rebuilt from trainer_version")

    telemetry = {}
    for call in b._logger.log_metrics.call_args_list:
        telemetry.update({k: v for k, v in call.args[0].items() if k.startswith("groups_")})

    return {
        "before_drop": before_drop,
        "after_borrow": after_borrow,
        "ledger_at_save": sorted(ledger_at_save),
        "dispatch_index_saved": snap.sampler_dispatch_index,
        "restored_canonical": restored,
        "restored_ledger": sorted(restored_ledger.values()),
        "after_restore": _stamps(b._buffer, b._rollout_manager.prompt_of),
        "discarded_on_restore": b._rollout_manager.discarded,
        "shortfall": b._batch_shortfall,
        "telemetry": telemetry,
        "spares_left": [p["idx"] for p in b._replacement_reserve],
    }


@pytest.mark.parametrize(
    ("saved_cursor", "expected_lost"),
    [
        # What the code does today: the saved cursor puts the reserved batch on a
        # fresh step, so nothing is already buffered there and nothing is dropped.
        (True, []),
        # A cursor rebuilt from trainer_version (the fallback for a checkpoint with
        # no sampler_dispatch_index) lands the batch on step 0, which is full, so
        # every reserved prompt is discarded. This is what the discard branch does.
        (False, [12, 13, 14]),
    ],
    ids=["saved-cursor", "cursor-rebuilt-from-trainer-version"],
)
def test_restore_after_a_borrow(tmp_path: Path, saved_cursor: bool, expected_lost: list[int]) -> None:
    r = asyncio.run(_round_trip(tmp_path, saved_cursor=saved_cursor))

    print("--- sampler dispatch_index saved :", r["dispatch_index_saved"])
    print("--- restored canonical groups    :", r["restored_canonical"])
    print("--- restored ledger prompts      :", r["restored_ledger"])
    print("--- after restore                :", r["after_restore"])
    print("--- discarded on restore         :", r["discarded_on_restore"])
    print("--- shortfall after restore      :", r["shortfall"])
    print("--- recovery telemetry           :", r["telemetry"])
    print("--- spares left in pool          :", r["spares_left"])

    handed_out = set(range(15))
    given_up_on = {1}  # the one deliberate data failure
    still_spare = set(r["spares_left"])
    present = {idx for idxs in r["after_restore"].values() for idx in idxs}
    lost = sorted(handed_out - given_up_on - still_spare - present)
    assert lost == expected_lost, f"restore lost prompts {lost}: {r['discarded_on_restore']}"
    assert sorted(idx for _, idx, _ in r["discarded_on_restore"]) == expected_lost
    t = r["telemetry"]
    assert t["groups_considered"] - t["groups_redispatched"] == len(expected_lost), (
        "the gap between the two counters is exactly the number of discarded prompts"
    )
