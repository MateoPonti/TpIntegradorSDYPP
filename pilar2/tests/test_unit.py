"""Tests unitarios de lo crítico: PoW, validación de tx, decisiones del pool, génesis y motor CPU."""
import pytest

import miner_cpu
import verify_chain
from coordinator import ledger
from coordinator.blocks import genesis_block
from pool import decision
from tpi_common import pow


# ---- PoW ----

def test_md5_matches_kit_vector():
    assert pow.md5_hex("abc", 0) != pow.md5_hex("abc", 1)
    assert pow.md5_hex("ab", "c") == "900150983cd24fb0d6963f7d28e17f72"  # md5("abc") del kit


def test_verify_rejects_wrong_hash_and_prefix():
    base = "0" * 32 + "f" * 64
    res = miner_cpu.search(base, "00", 0, 100_000, workers=1)
    assert res["found"]
    assert pow.verify(base, "00", res["nonce"], res["hash"])
    assert not pow.verify(base, "00", res["nonce"] + 1, res["hash"])  # nonce cambiado
    assert not pow.verify(base, "0000000000", res["nonce"], res["hash"])  # prefijo no cumplido
    assert not pow.verify(base, "00", -1, res["hash"])


def test_canonical_json_ignores_key_order():
    assert pow.tx_digest([{"a": 1, "b": 2}]) == pow.tx_digest([{"b": 2, "a": 1}])


# ---- Transacciones ----

@pytest.mark.parametrize("value,cents", [(1, 100), (12.34, 1234), ("99.9", 9990), (100.0, 10000)])
def test_to_cents(value, cents):
    assert ledger.to_cents(value) == cents


@pytest.mark.parametrize("value", [0, -5, 1.234, "abc", None, True, 10**13])
def test_to_cents_rejects(value):
    with pytest.raises(ledger.InvalidTx):
        ledger.to_cents(value)


def test_normalize_bench_format():
    tx = ledger.normalize({"sender": "user-1", "receiver": "user-2", "amount": 55.5})
    assert tx["amount"] == 5550 and tx["tx_id"] and tx["ts"] > 0


def test_normalize_allows_self_transfer_and_rejects_missing():
    assert ledger.normalize({"sender": "u", "receiver": "u", "amount": 1})["sender"] == "u"
    with pytest.raises(ledger.InvalidTx):
        ledger.normalize({"sender": "u", "amount": 1})


# ---- Pool ----

def test_prefix_shrinks_without_gpu():
    assert decision.effective_prefix("000000", gpu_alive=1, min_without_gpu=4) == "000000"
    assert decision.effective_prefix("000000", gpu_alive=0, min_without_gpu=4) == "0000"


def test_range_size_follows_fragment():
    assert decision.range_size("00000", 10) == 104_858  # 16^5 * 10 %
    assert decision.range_size("0", 1) == decision.MIN_RANGE_SIZE


def test_desired_cpu_miners():
    assert decision.desired_cpu_miners(gpu_alive=1, pending_tasks=100, current=3, max_miners=8) == 0
    assert decision.desired_cpu_miners(0, 45, 1, 8, tasks_per_miner=20) == 3
    assert decision.desired_cpu_miners(0, 0, 2, 8) == 2  # entre bloques no oscila
    assert decision.desired_cpu_miners(0, 1000, 1, 8) == 8


# ---- Génesis y verificador ----

def test_genesis_is_valid_for_verify_chain():
    g = genesis_block()
    assert len(g["transactions"]) == 97
    assert verify_chain.check_block(g, "0" * 32) == []


def test_verify_chain_detects_tampering():
    g = genesis_block()
    g["transactions"][0]["amount"] += 1
    assert verify_chain.check_block(g, "0" * 32)


# ---- Motor CPU (mismo contrato que miner_gpu) ----

def test_cpu_miner_closed_range_and_offset():
    base = "abc"
    hit = miner_cpu.search(base, "000", 0, 20_000, workers=1)
    assert hit["found"]
    n = hit["nonce"]
    assert miner_cpu.search(base, "000", n, n, workers=1)["nonce"] == n  # rango de un solo nonce
    later = miner_cpu.search(base, "000", n + 1, 200_000, workers=1)  # respeta --from
    assert later["found"] and later["nonce"] > n


def test_cpu_miner_parallel_finds_valid_nonce():
    base = "0" * 32 + "a" * 64
    res = miner_cpu.search(base, "0000", 0, 3_000_000, workers=4)
    assert res["found"] and pow.verify(base, "0000", res["nonce"], res["hash"])


def test_cpu_miner_none_in_range_does_not_invent():
    res = miner_cpu.search("x", "ffffffff", 0, 1000, workers=1)
    assert res == {"found": False, "hashes": 1001, "seconds": res["seconds"]}
