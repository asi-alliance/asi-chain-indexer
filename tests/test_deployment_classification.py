"""Deployment classification must use exact system-contract markers, not words."""

import asyncio

import pytest

from scripts.backfill_deployment_actions import backfill, classify_backfill
from src.block_indexer import BlockIndexer


def classify(term: str):
    return BlockIndexer.classify_deployment_action(term)


@pytest.mark.parametrize(
    "method,action",
    [
        ("bond", "pos_bond"),
        ("withdraw", "pos_withdraw"),
        ("delegate", "pos_delegate"),
        ("undelegate", "pos_undelegate"),
        ("completeUndelegate", "pos_complete_undelegate"),
        ("claimDelegatorRewards", "pos_claim_rewards"),
    ],
)
def test_pos_actions_are_classified_from_exact_method_calls(method, action):
    term = f'new rl(`rho:registry:lookup`), poSCh in {{ rl!(`rho:system:pos`, *poSCh) | @PoS!("{method}", *deployerId, *retCh) }}'
    assert classify(term) == ("validator_operation", action)
    assert BlockIndexer.classify_deployment(term) == "validator_operation"


@pytest.mark.parametrize("vault", ["SystemVault", "ASIVault"])
def test_vault_transfer_requires_a_vault_call_and_a_transfer_call(vault):
    registry = "new rl(`rho:vault:system`) | " if vault == "SystemVault" else ""
    term = (
        registry + f'@{vault}!("findOrCreate", *vaultCh) | @vault!("transfer", *retCh)'
    )
    assert classify(term) == ("asi_transfer", "vault_transfer")


def test_a_locally_bound_system_vault_is_not_a_vault_transfer():
    term = 'new SystemVault in { @SystemVault!("findOrCreate", *vaultCh) | @vault!("transfer", *retCh) }'
    assert classify(term) == ("smart_contract", None)


def test_registry_lookup_without_a_system_action_is_not_a_transfer():
    term = "new rl(`rho:registry:lookup`), ch in { rl!(`rho:system:pos`, *ch) }"
    assert classify(term) == ("registry_lookup", "registry_lookup")


def test_comments_and_plain_strings_do_not_supply_call_markers():
    term = """
    // @PoS!("delegate", *deployerId, *retCh) `rho:system:pos`
    /* rho:registry:lookup `rho:vault:system` */
    new x in {
      x!("@PoS!(\"delegate\", *deployerId) rho:registry:lookup")
    }
    """
    assert classify(term) == ("smart_contract", None)


def test_a_comment_marker_before_a_real_pos_call_is_ignored():
    term = (
        '// @PoS!("withdraw", *deployerId) `rho:system:pos`\n'
        'new rl(`rho:system:pos`), ch in { @PoS!("delegate", *deployerId) }'
    )
    assert classify(term) == ("validator_operation", "pos_delegate")


def test_a_locally_bound_pos_channel_is_not_a_validator_operation():
    term = 'new PoS in { @PoS!("delegate", *deployerId, *retCh) }'
    assert classify(term) == ("smart_contract", None)


def test_words_in_comments_do_not_change_the_classification():
    term = """
    // This comment mentions bond, validator and delegate.
    /* An auction or finalizer mention is not a call either. */
    new x in { x!(1) }
    """
    assert classify(term) == ("smart_contract", None)


def test_a_bare_pos_call_without_a_known_action_stays_a_validator_operation():
    term = 'new rl(`rho:system:pos`), ch in { @PoS!("getBonds", *retCh) }'
    assert classify(term) == ("validator_operation", "pos_other")


def test_empty_or_missing_term_has_no_fabricated_action():
    assert classify("") == ("smart_contract", None)


@pytest.mark.parametrize("deployment_type", ["genesis_mint", "genesis_bond"])
def test_backfill_preserves_genesis_rows(deployment_type):
    assert classify_backfill(deployment_type, "Genesis synthetic term") == (
        deployment_type,
        deployment_type,
    )


@pytest.mark.asyncio
async def test_backfill_updates_both_genesis_rows_without_reclassifying_them():
    class FakeConnection:
        def __init__(self):
            self.updates = []
            self._read = False

        async def fetch(self, query, batch_size, cursor):
            if self._read:
                return []
            self._read = True
            return [
                {
                    "deploy_id": "genesis_allocation_1",
                    "deployment_type": "genesis_mint",
                    "term": "Genesis ASI allocation",
                },
                {
                    "deploy_id": "genesis_bond_1",
                    "deployment_type": "genesis_bond",
                    "term": "Genesis validator bond",
                },
            ]

        async def executemany(self, query, changes):
            self.updates = changes

    connection = FakeConnection()
    scanned, updated = await backfill(connection, batch_size=10, dry_run=False)
    assert (scanned, updated) == (2, 2)
    assert connection.updates == [
        ("genesis_mint", "genesis_mint", "genesis_allocation_1"),
        ("genesis_bond", "genesis_bond", "genesis_bond_1"),
    ]
