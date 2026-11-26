"""Deployment classification must use exact system-contract markers, not words."""

import pytest

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
    term = f'@{vault}!("findOrCreate", *vaultCh) | @vault!("transfer", *retCh)'
    assert classify(term) == ("asi_transfer", "vault_transfer")


def test_registry_lookup_without_a_system_action_is_not_a_transfer():
    term = "new rl(`rho:registry:lookup`), ch in { rl!(`rho:system:pos`, *ch) }"
    assert classify(term) == ("registry_lookup", "registry_lookup")


def test_words_in_comments_do_not_change_the_classification():
    term = """
    // This comment mentions bond, validator and delegate.
    /* An auction or finalizer mention is not a call either. */
    new x in { x!(1) }
    """
    assert classify(term) == ("smart_contract", None)


def test_a_bare_pos_call_without_a_known_action_stays_a_validator_operation():
    term = '@PoS!("getBonds", *retCh)'
    assert classify(term) == ("validator_operation", "pos_other")


def test_empty_or_missing_term_has_no_fabricated_action():
    assert classify("") == ("smart_contract", None)
