"""Durable native-call correlation shared by live indexing and historical repair."""

import hashlib
import json

from sqlalchemy import Text, any_, bindparam, func, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.orm import aliased

from src.config import settings
from src.models import (
    Block,
    BlockParent,
    NativeTransferCall,
    NativeTransferResult,
    NativeTransferResultAlias,
)
from src.transfer_reports import ReportUnavailable, TransferTracker


def observation_id(block_hash, deploy_id, index):
    return hashlib.sha256(
        json.dumps([block_hash, deploy_id, index]).encode()
    ).hexdigest()


def ancestor_scope(block_hash, minimum_height):
    ancestors = (
        select(BlockParent.parent_hash.label("hash"))
        .where(BlockParent.block_hash == block_hash)
        .cte("native_ancestors", recursive=True)
    )
    return ancestors.union(
        select(BlockParent.parent_hash)
        .join(ancestors, BlockParent.block_hash == ancestors.c.hash)
        .join(Block, Block.block_hash == BlockParent.block_hash)
        .where(Block.block_number > minimum_height)
    )


def equivalent_calls(calls, completed):
    groups = {}
    for call in calls:
        key = (call["deploy_id"], call["event_index"], call["return_channel"])
        groups.setdefault(key, []).append(call)
    representatives, resolved, aliases = [], set(), {}
    for group in groups.values():
        first = min(group, key=lambda call: (call["block_hash"], call["id"]))
        is_completed = any(call["id"] in completed for call in group)
        fields = ("from_address", "to_address", "amount")
        if not is_completed and any(
            any(call[field] != first[field] for field in fields) for call in group
        ):
            raise ReportUnavailable("Conflicting duplicate native invocations")
        representatives.append(first)
        aliases[first["id"]] = [
            call["id"] for call in group if call["id"] != first["id"]
        ]
        if is_completed:
            resolved.add(first["id"])
    return representatives, resolved, aliases


async def require_indexed_parents(session, block_info, client=None):
    if block_info["blockNumber"] <= settings.start_from_block:
        return
    boundary = (
        select(BlockParent.parent_hash)
        .join(Block, Block.block_hash == BlockParent.block_hash)
        .where(Block.block_number <= settings.start_from_block)
    )
    missing = await session.scalars(
        select(BlockParent.parent_hash)
        .outerjoin(Block, Block.block_hash == BlockParent.parent_hash)
        .where(
            BlockParent.block_hash == block_info["blockHash"],
            Block.block_hash.is_(None),
            BlockParent.parent_hash.not_in(boundary),
        )
    )
    for parent_hash in missing:
        if settings.start_from_block > 0 and client is not None:
            details = await client.get_block_details(parent_hash)
            info = (details or {}).get("blockInfo", {})
            height = info.get("blockNumber")
            if (
                info.get("blockHash") == parent_hash
                and type(height) is int
                and 0 <= height < settings.start_from_block
            ):
                continue
        raise ReportUnavailable(
            f"Missing parent block {parent_hash}; retry after indexing it"
        )


async def reconcile_completed_duplicates(session, block_info):
    if len(block_info.get("parentsHashList", [])) < 2:
        return
    duplicate = aliased(NativeTransferCall)
    result_block = aliased(Block)
    candidates = (
        select(
            duplicate.id.label("call_id"),
            duplicate.block_hash.label("call_block"),
            NativeTransferResult.id.label("result_id"),
            NativeTransferResult.block_hash.label("result_block"),
            func.least(Block.block_number, result_block.block_number).label("height"),
        )
        .select_from(duplicate)
        .join(
            NativeTransferCall,
            (
                (NativeTransferCall.deploy_id == duplicate.deploy_id)
                & (NativeTransferCall.event_index == duplicate.event_index)
                & (NativeTransferCall.return_channel == duplicate.return_channel)
                & (NativeTransferCall.id != duplicate.id)
            ),
        )
        .join(
            NativeTransferResult, NativeTransferResult.call_id == NativeTransferCall.id
        )
        .join(Block, Block.block_hash == duplicate.block_hash)
        .join(result_block, result_block.block_hash == NativeTransferResult.block_hash)
        .where(
            ~select(NativeTransferResultAlias.call_id)
            .where(
                NativeTransferResultAlias.call_id == duplicate.id,
                NativeTransferResultAlias.result_id == NativeTransferResult.id,
            )
            .exists()
        )
        .cte("duplicate_results")
    )
    minimum = await session.scalar(select(func.min(candidates.c.height)))
    if minimum is None:
        return
    ancestors = ancestor_scope(block_info["blockHash"], minimum)
    result_ancestors = ancestors.alias("result_ancestors")
    matches = (
        select(candidates.c.result_id, candidates.c.call_id)
        .join(ancestors, ancestors.c.hash == candidates.c.call_block)
        .join(result_ancestors, result_ancestors.c.hash == candidates.c.result_block)
    )
    await session.execute(
        insert(NativeTransferResultAlias)
        .from_select(["result_id", "call_id"], matches)
        .on_conflict_do_nothing()
    )


def stored_call(call):
    return {
        "id": call.id,
        "block_hash": call.block_hash,
        "deploy_id": call.deploy_id,
        "event_index": call.event_index,
        "return_channel": call.return_channel,
        "from_address": call.from_address,
        "to_address": call.to_address,
        "amount": int(call.requested_amount),
        "token_decimals": call.token_decimals,
    }


async def reconcile_block(session, block_info, deployments, traces, client=None):
    """Persist observations and return movements attributed to their completion."""
    block_hash = block_info["blockHash"]
    await require_indexed_parents(session, block_info, client)
    channels = {data["return_channel"] for _, events in traces for _, data in events}
    candidates = (
        list(
            await session.execute(
                select(NativeTransferCall, Block.block_number)
                .join(Block, Block.block_hash == NativeTransferCall.block_hash)
                .where(
                    NativeTransferCall.return_channel
                    == any_(
                        bindparam("channels", sorted(channels), type_=ARRAY(Text()))
                    ),
                    NativeTransferCall.block_hash != block_hash,
                )
            )
        )
        if channels
        else []
    )
    calls, completed = [], set()
    if candidates:
        ancestors = ancestor_scope(block_hash, min(height for _, height in candidates))
        ids = [call.id for call, _ in candidates]
        calls = list(
            await session.scalars(
                select(NativeTransferCall)
                .join(ancestors, ancestors.c.hash == NativeTransferCall.block_hash)
                .where(
                    NativeTransferCall.id
                    == any_(bindparam("call_ids", ids, type_=ARRAY(Text())))
                )
            )
        )
        completed = set(
            await session.scalars(
                select(NativeTransferResult.call_id)
                .join(ancestors, ancestors.c.hash == NativeTransferResult.block_hash)
                .where(
                    NativeTransferResult.call_id
                    == any_(bindparam("call_ids", ids, type_=ARRAY(Text())))
                )
            )
        )
    representatives, completed, aliases = equivalent_calls(
        [stored_call(call) for call in calls], completed
    )
    tracker = TransferTracker(representatives, completed)
    if [deploy["sig"] for deploy in deployments] != [
        deploy_id for deploy_id, _ in traces
    ]:
        raise ReportUnavailable("Native transfer deployment order mismatch")
    movements = {deploy_id: [] for deploy_id, _ in traces}
    for deploy_id, events in traces:
        local_calls = sum(kind == "call" for kind, _ in events)
        for kind, data in events:
            if kind == "call":
                data = {
                    **data,
                    "id": observation_id(block_hash, deploy_id, data["event_index"]),
                    "block_hash": block_hash,
                    "deploy_id": deploy_id,
                    "token_decimals": block_info.get(
                        "token_decimals", settings.native_token_decimals
                    ),
                }
                values = {
                    key: data[key]
                    for key in (
                        "id",
                        "block_hash",
                        "deploy_id",
                        "event_index",
                        "return_channel",
                        "from_address",
                        "to_address",
                        "token_decimals",
                    )
                }
                values.update(
                    requested_amount=str(data["amount"]),
                    timestamp=block_info["timestamp"],
                )
                existing = await session.get(NativeTransferCall, data["id"])
                if existing and any(
                    getattr(existing, key) != value for key, value in values.items()
                ):
                    raise ReportUnavailable("Native invocation changed during replay")
                await session.execute(
                    insert(NativeTransferCall)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            match = tracker.observe(kind, data)
            if match is None:
                continue
            call, success = match
            result_id = observation_id(block_hash, deploy_id, data["response_index"])
            result_values = dict(
                id=result_id,
                call_id=call["id"],
                block_hash=block_hash,
                deploy_id=deploy_id,
                response_index=data["response_index"],
                phase=data["phase"],
                success=success,
                timestamp=block_info["timestamp"],
            )
            existing = await session.get(NativeTransferResult, result_id)
            if existing and any(
                getattr(existing, key) != value for key, value in result_values.items()
            ):
                raise ReportUnavailable("Native result changed during replay")
            await session.execute(
                insert(NativeTransferResult)
                .values(**result_values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            for alias_id in aliases.get(call["id"], []):
                await session.execute(
                    insert(NativeTransferResultAlias)
                    .values(result_id=result_id, call_id=alias_id)
                    .on_conflict_do_nothing()
                )
            if success:
                immediate = (
                    call["block_hash"] == block_hash and call["deploy_id"] == deploy_id
                )
                movements[deploy_id].append(
                    {
                        "event_index": (
                            call["event_index"]
                            if immediate
                            else local_calls + data["response_index"]
                        ),
                        "from_address": call["from_address"],
                        "to_address": call["to_address"],
                        "amount": call["amount"],
                        "token_decimals": call["token_decimals"],
                        "native_result_id": result_id,
                        "timestamp": block_info["timestamp"],
                    }
                )
    return movements
