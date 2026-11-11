"""Identifiers for wallet administration; pure Python, safe to unit test.

A real, independently verified funding proof remains required for a corporate
credit pool. Internal keys are deterministic for funding proof and batch rows.
"""
import hashlib
import secrets


def pool_event_key(customer, proof):
    if not customer or not (proof or "").strip():
        raise ValueError("Corporate customer and real funding proof are required")
    digest = hashlib.sha256((str(customer) + "\0" + str(proof).strip()).encode()).hexdigest()[:40]
    return "CORP:FUND:" + digest


def allocation_event_key(source_type, referral_reward=None):
    if source_type == "Referral":
        if not referral_reward:
            raise ValueError("A verified referral reward is required")
        return "REFERRAL:REWARD:" + str(referral_reward)
    if source_type not in ("Corporate", "Promotion"):
        raise ValueError("Unknown wallet funding source")
    return source_type.upper() + ":ADMIN:" + secrets.token_hex(16)


def batch_allocation_key(batch, line):
    if not batch or not line:
        raise ValueError("Saved batch and row IDs are required")
    return "CORP:BATCH:" + str(batch) + ":" + str(line)
