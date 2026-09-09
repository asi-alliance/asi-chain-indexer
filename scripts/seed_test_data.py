#!/usr/bin/env python3
"""Seed the indexer database with transaction history test data.

Generates a keypair, prints the private key, and writes a history for that
address covering the edge cases the wallet frontend needs to handle.

The data is written straight to Postgres: most of these cases (past/future
timestamps, identical timestamps, failed/unknown statuses, exact field
lengths) cannot be produced by real deploys through the node.

Seeded blocks continue from the current chain tip, so their numbers never
collide with real blocks.

Usage:
    python scripts/seed_test_data.py
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.addr import public_key_to_asi_address  # noqa: E402

PERF_RECORDS = 2000
DUST = 100_000_000

# secp256k1
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _point_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        lam = 3 * p[0] * p[0] * pow(2 * p[1], _P - 2, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], _P - 2, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _point_mul(k, p):
    result = None
    while k:
        if k & 1:
            result = _point_add(result, p)
        p = _point_add(p, p)
        k >>= 1
    return result


def generate_account():
    """Return (private_key_hex, public_key_hex, asi_address)."""
    priv = int.from_bytes(os.urandom(32), "big") % _N
    point = _point_mul(priv, (_GX, _GY))
    pub = "04" + format(point[0], "064x") + format(point[1], "064x")
    return format(priv, "064x"), pub, public_key_to_asi_address(pub)


class Seeder:
    def __init__(self, conn, address, public_key, block_base):
        self.conn = conn
        self.address = address
        self.public_key = public_key
        self.block_no = block_base - 1
        self.deploy_seq = 0

    async def _block(self, block_hash=None):
        """Create a block row, return its hash."""
        self.block_no += 1
        if block_hash is None:
            block_hash = f"seedblock{self.block_no:055d}"
        await self.conn.execute(
            """
            INSERT INTO blocks (block_hash, block_number, timestamp, proposer,
                                finalization_status, deployment_count)
            VALUES ($1, $2, $3, $4, 'finalized', 1)
            ON CONFLICT (block_hash) DO NOTHING
            """,
            block_hash, self.block_no, 0, "seed_proposer",
        )
        return block_hash, self.block_no

    async def record(self, timestamp, *, deployer=None, transfer=None,
                     deploy_id=None, block_hash=None, term="seed"):
        """Create one deployment, optionally with one transfer.

        transfer: dict with from_address/to_address/amount_asi/status/from_public_key

        deployer defaults to whoever sent the transfer: an incoming transfer is
        deployed by the counterparty, not by us. Without this the history query
        would find every row by deployer_address alone and never exercise its
        from_address / to_address branches.
        """
        if deployer is None and transfer is not None:
            sender = transfer["from_address"].strip()
            deployer = sender if sender.lower() != self.address.lower() else self.address
        bhash, bno = await self._block(block_hash)
        self.deploy_seq += 1
        if deploy_id is None:
            deploy_id = f"seed_{bno}_{self.deploy_seq:04d}"

        await self.conn.execute(
            """
            INSERT INTO deployments (deploy_id, block_hash, block_number, deployer,
                                     deployer_address, term, timestamp, sig, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'included')
            """,
            deploy_id, bhash, bno, self.public_key,
            deployer or self.address, term, timestamp, deploy_id[:200],
        )

        if transfer is not None:
            amount = Decimal(str(transfer["amount_asi"]))
            dust = transfer.get("amount_dust")
            if dust is None:
                dust = int(amount * DUST)
            await self.conn.execute(
                """
                INSERT INTO transfers (deploy_id, block_hash, block_number, from_address,
                                       from_public_key, to_address, amount_dust,
                                       amount_asi, status, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                deploy_id, bhash, bno,
                transfer["from_address"],
                transfer.get("from_public_key"),
                transfer["to_address"],
                dust, amount, transfer.get("status", "success"), timestamp,
            )
        return deploy_id, bhash, bno

    async def extra_transfer(self, deploy_id, block_hash, block_number, timestamp, transfer):
        """Attach a second transfer to an existing deploy (duplicate deploy_id case)."""
        amount = Decimal(str(transfer["amount_asi"]))
        await self.conn.execute(
            """
            INSERT INTO transfers (deploy_id, block_hash, block_number, from_address,
                                   from_public_key, to_address, amount_dust,
                                   amount_asi, status, timestamp)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            deploy_id, block_hash, block_number,
            transfer["from_address"], transfer.get("from_public_key"),
            transfer["to_address"], int(amount * DUST), amount,
            transfer.get("status", "success"), timestamp,
        )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=(
        os.getenv("DATABASE_URL")
        or "postgresql://indexer:indexer_pass@localhost:5432/asichain"))
    args = parser.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        base = int(await conn.fetchval(
            "SELECT COALESCE(max(block_number), 0) FROM blocks")) + 1

        priv, pub, addr = generate_account()
        _, peer_pub, peer_addr = generate_account()
        _, _, peer2_addr = generate_account()

        s = Seeder(conn, addr, pub, base)
        await seed_main_account(s, peer_addr, peer2_addr, peer_pub)
        main_last = s.block_no

        perf_priv, perf_pub, perf_addr = generate_account()
        await seed_perf_account(conn, perf_addr, perf_pub, (peer_addr, peer2_addr), main_last + 1)

        total = await conn.fetchval(
            "SELECT count(*) FROM transaction_history_view WHERE block_number >= $1",
            base,
        )
        main_count = await conn.fetchval(
            """
            SELECT count(*) FROM transaction_history_view
            WHERE block_number >= $1 AND block_number <= $2
            """,
            base, main_last,
        )

        print()
        print("edge-case account")
        print(f"  address:     {addr}")
        print(f"  private key: {priv}")
        print(f"  records:     {main_count}")
        print()
        print("bulk account (pagination and scrolling)")
        print(f"  address:     {perf_addr}")
        print(f"  private key: {perf_priv}")
        print(f"  records:     {PERF_RECORDS}"
              f" ({PERF_RECORDS // 2} with each counterparty,"
              f" half sent and half received)")
        print()
        print("counterparties (no keys kept, they only appear as the other side)")
        print(f"  {peer_addr}")
        print(f"  {peer2_addr}")
        print()
        print(f"rows seeded into transaction_history_view: {total}")
        print()
    finally:
        await conn.close()


async def seed_main_account(s, peer, peer2, peer_pub):
    """Records are created oldest-first so the view returns them in spec order."""
    now = datetime.now(timezone.utc)
    base = int(now.timestamp() * 1000)
    minute = 60_000

    def ts(offset_minutes):
        return base - offset_minutes * minute

    me = s.address

    # 48: four years old
    await s.record(int((now - timedelta(days=4 * 365)).timestamp() * 1000),
                   transfer={"from_address": peer, "to_address": me, "amount_asi": "12.5"})

    # 45-46: day boundaries
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await s.record(int((midnight - timedelta(milliseconds=1)).timestamp() * 1000),
                   transfer={"from_address": me, "to_address": peer, "amount_asi": "1.11"})
    await s.record(int((midnight + timedelta(milliseconds=1)).timestamp() * 1000),
                   transfer={"from_address": peer, "to_address": me, "amount_asi": "2.22"})

    # 49-60: ordinary transfers, mixed directions
    for i in range(12):
        counterparty = peer if i % 2 == 0 else peer2
        outgoing = i % 3 != 0
        await s.record(ts(200 - i),
                       transfer={
                           "from_address": me if outgoing else counterparty,
                           "to_address": counterparty if outgoing else me,
                           "from_public_key": None if outgoing else peer_pub,
                           "amount_asi": f"{(i + 1) * 3}.{i}5",
                       })

    # 20-44: identical timestamp, straddles the page boundary
    same_ts = ts(100)
    for i in range(25):
        await s.record(same_ts,
                       transfer={"from_address": me, "to_address": peer,
                                 "amount_asi": f"0.{i + 10}"})

    # 17: counterparty address in another case and padded with spaces
    await s.record(ts(60),
                   transfer={"from_address": f"  {peer.upper()}  ", "to_address": me,
                             "amount_asi": "7.77"})

    # 16: empty block_hash
    await s.record(ts(55),
                   block_hash="",
                   transfer={"from_address": me, "to_address": peer, "amount_asi": "3.33"})

    # 15: field length limits
    long_deploy_id = (f"seed_len_{s.block_no + 1}_" + "x" * 200)[:200]
    long_addr = ("1111" + "L" * 150)[:150]
    await s.record(ts(50),
                   deploy_id=long_deploy_id,
                   transfer={"from_address": me, "to_address": long_addr,
                             "amount_asi": "4.44"})

    # 13: zero amount
    await s.record(ts(45),
                   transfer={"from_address": me, "to_address": peer, "amount_asi": "0"})

    # 10: largest amount that still fits amount_dust (BIGINT)
    await s.record(ts(40),
                   transfer={"from_address": peer, "to_address": me,
                             "amount_asi": "92233720368.54775807",
                             "amount_dust": 9223372036854775807})

    # 9: smallest amount NUMERIC(20,8) can hold
    await s.record(ts(35),
                   transfer={"from_address": me, "to_address": peer,
                             "amount_asi": "0.00000001"})

    # 8: unknown status the UI has never seen
    await s.record(ts(30),
                   transfer={"from_address": me, "to_address": peer,
                             "amount_asi": "5.55", "status": "expired"})

    # 7: null status
    await s.record(ts(25),
                   transfer={"from_address": me, "to_address": peer,
                             "amount_asi": "6.66", "status": None})

    # 6: failed transfer that still carries an amount
    await s.record(ts(20),
                   transfer={"from_address": me, "to_address": peer,
                             "amount_asi": "8.88", "status": "failed"})

    # 4: not_transfer, a deploy that moved no tokens
    await s.record(ts(15),
                   term="new x in { x!(42) }")

    # 3: self-transfer
    await s.record(ts(10),
                   transfer={"from_address": me, "to_address": me, "amount_asi": "9.99"})

    # 2: incoming
    await s.record(ts(5),
                   transfer={"from_address": peer, "to_address": me,
                             "from_public_key": peer_pub, "amount_asi": "10.10"})

    # 1: outgoing, and record 19 hangs a second transfer off the same deploy
    deploy_id, bhash, bno = await s.record(
        ts(1),
        transfer={"from_address": me, "to_address": peer, "amount_asi": "11.11"})

    # 19: second transfer sharing the deploy_id above
    await s.extra_transfer(deploy_id, bhash, bno, ts(1),
                           {"from_address": me, "to_address": peer2, "amount_asi": "0.01"})

    # 47: an hour into the future
    await s.record(int((now + timedelta(hours=1)).timestamp() * 1000),
                   transfer={"from_address": peer, "to_address": me, "amount_asi": "13.13"})


async def seed_perf_account(conn, address, public_key, peers, block_base):
    """Bulk records for pagination and scrolling.

    Half go to the first counterparty and half to the second, so filtering
    by counterparty can be exercised at volume too."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    blocks, deploys, transfers = [], [], []

    for i in range(PERF_RECORDS):
        bno = block_base + i
        bhash = f"perfblock{bno:055d}"
        did = f"perf_{bno}_{i:06d}"
        ts = now_ms - i * 30_000
        outgoing = i % 2 == 0
        peer = peers[0] if i < PERF_RECORDS // 2 else peers[1]
        amount = Decimal(f"{(i % 500) + 1}.{i % 100:02d}")

        blocks.append((bhash, bno, 0, "seed_proposer", "finalized", 1))
        deploys.append((did, bhash, bno, public_key,
                        address if outgoing else peer, "seed", ts, did, "included"))
        transfers.append((
            did, bhash, bno,
            address if outgoing else peer,
            None,
            peer if outgoing else address,
            int(amount * DUST), amount, "success", ts,
        ))

    await conn.executemany(
        """INSERT INTO blocks (block_hash, block_number, timestamp, proposer,
                               finalization_status, deployment_count)
           VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (block_hash) DO NOTHING""", blocks)
    await conn.executemany(
        """INSERT INTO deployments (deploy_id, block_hash, block_number, deployer,
                                    deployer_address, term, timestamp, sig, status)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""", deploys)
    await conn.executemany(
        """INSERT INTO transfers (deploy_id, block_hash, block_number, from_address,
                                  from_public_key, to_address, amount_dust,
                                  amount_asi, status, timestamp)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""", transfers)


if __name__ == "__main__":
    asyncio.run(main())