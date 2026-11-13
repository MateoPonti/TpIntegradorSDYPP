"""Tests unitarios de lo crítico: PoW, validación de tx, decisiones del pool, armado del bloque,
premio, génesis y motor CPU."""
import pytest

import miner_cpu
import verify_chain
from coordinator import blocks, ledger, reward
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


def test_client_cannot_choose_tx_id():
    # Si pudiera, se adelantaría al tx_id de un premio o le bloquearía ids a otro cliente.
    tx = ledger.normalize({"sender": "u", "receiver": "v", "amount": 1, "tx_id": "reward-25-mateo"})
    assert tx["tx_id"] != "reward-25-mateo"


def test_normalize_allows_self_transfer_and_rejects_missing():
    assert ledger.normalize({"sender": "u", "receiver": "u", "amount": 1})["sender"] == "u"
    with pytest.raises(ledger.InvalidTx):
        ledger.normalize({"sender": "u", "amount": 1})


# ---- Pool ----

def test_prefix_is_the_longest_that_fits_the_target():
    # 1 GH/s y 10 s de objetivo: 16^8 = 4.300 M (4,3 s) entra; 16^9 no.
    assert decision.choose_prefix_len(1, 1e9, 10, 1, 8, raise_margin=1) == 8
    # CPU de la PC con 12 procesos (8 MH/s medido): 16^6 = 2 s; 16^7 = 33 s.
    assert decision.choose_prefix_len(1, 8e6, 10, 1, 8, raise_margin=1) == 6
    assert decision.choose_prefix_len(5, 0, 10, 1, 8) == 1  # sin mineros: el primero lo resuelve enseguida


def test_prefix_hysteresis():
    # 16^6 / 1,8 MH/s = 9,3 s: entra en el objetivo, pero sin el margen del 80 % no sube.
    assert decision.choose_prefix_len(5, 1.8e6, 10, 1, 8, raise_margin=0.8) == 5
    # Bajar es inmediato: con 6 ya se pasa (16^6 / 1 MH/s = 16,8 s).
    assert decision.choose_prefix_len(6, 1e6, 10, 1, 8) == 5


def test_range_size_follows_fragment_with_minimum():
    assert decision.range_size(5, 10, 0) == 104_858  # 16^5 * 10 %
    assert decision.range_size(1, 1, 0) == decision.MIN_RANGE_SIZE
    assert decision.range_size(5, 10, 100_000_000) == 100_000_000  # con GPU: que llene sus hilos


def test_desired_cpu_miners_all_or_nothing():
    assert decision.desired_cpu_miners(gpu_alive=1, has_work=True, idle_seconds=0, max_miners=8, idle_limit=60) == 0
    assert decision.desired_cpu_miners(0, True, 0, 8, 60) == 8
    assert decision.desired_cpu_miners(0, False, 30, 8, 60) == 8  # todavía no pasó el minuto
    assert decision.desired_cpu_miners(0, False, 61, 8, 60) == 0


def test_miner_hashrate_uses_delivered_work():
    now = 1000.0
    results = [{"hashes": 2_000_000, "engine_seconds": 1.0, "seconds": 1.5, "ts": now - 10}]
    motor, efectivo = decision.miner_rates(results, now, 300)
    assert motor == 2e6 and round(efectivo) == 1_333_333
    assert decision.miner_hashrate(results, None, 9e9, None, False, now, 300) == (2e6, "motor")
    # Sin resultados: la calibración; sin calibración: lo declarado.
    assert decision.miner_hashrate([], {"hashrate": 5e5}, 9e9, None, False, now, 300) == (5e5, "calibracion")
    assert decision.miner_hashrate([], None, 7.0, None, False, now, 300) == (7.0, "declarado")


def test_found_early_results_do_not_measure_speed():
    # Prefijo corto: encontró a los 3.000 hashes, pero el motor tardó 0,5 s en arrancar -> 6 kH/s falso.
    now = 1000.0
    early = [{"hashes": 3000, "engine_seconds": 0.5, "seconds": 0.7, "ts": now - 5, "found": True}]
    assert decision.miner_rates(early, now, 300) is None
    assert decision.miner_hashrate(early, {"hashrate": 1.4e6}, 0, None, False, now, 300) == (1.4e6, "calibracion")


def test_zombie_and_quarantined_miners_do_not_count():
    now = 1000.0
    results = [{"hashes": 2_000_000, "engine_seconds": 1.0, "seconds": 1.5, "ts": now - 100}]
    assert decision.miner_hashrate(results, None, 5e8, now - 5, False, now, 300) == (0.0, "zombie")
    assert decision.miner_hashrate(results, None, 5e8, None, True, now, 300) == (0.0, "cuarentena")
    # Un vencimiento viejo, anterior a su último resultado, no lo marca.
    assert decision.miner_hashrate(results, None, 5e8, now - 200, False, now, 300)[1] == "motor"


# ---- Armado del bloque ----

def test_fixed_clock_windows():
    assert blocks.window_end(0.0, 10) == 10
    assert blocks.window_end(9.999, 10) == 10
    assert blocks.window_end(10.0, 10) == 20  # el borde es de la ventana siguiente
    assert blocks.window_end(1234.5, 10) == 1240


def test_rearm_only_when_hashrate_falls_a_lot():
    # Prefijo 8 y se fue la GPU: quedan 16 MH/s de CPU -> 268 s, más de 6 x 10 s.
    assert blocks.should_rearm(8, 6, 16e6, 10, 6, age=30, min_age=20)
    assert not blocks.should_rearm(8, 6, 16e6, 10, 6, age=5, min_age=20)  # recién armado
    assert not blocks.should_rearm(8, 8, 16e6, 10, 6, age=30, min_age=20)  # el pool no pide uno más corto
    assert not blocks.should_rearm(6, 5, 16e6, 10, 6, age=30, min_age=20)  # 1 s: sigue bien


# ---- Premio ----

def test_reward_is_proportional_and_exact():
    split = reward.split_reward({"gpu": 900, "cpu-a": 50, "cpu-b": 50}, 5000, finder="gpu")
    assert split == {"gpu": 4500, "cpu-a": 250, "cpu-b": 250}
    odd = reward.split_reward({"a": 1, "b": 1, "c": 1}, 100, finder="b")
    assert sum(odd.values()) == 100 and odd["b"] == 34  # el resto del redondeo, al que encontró
    assert reward.split_reward({}, 5000, None) == {}


def test_reward_goes_to_payout_account():
    # Dos pods de KEDA y la GPU de la misma persona cobran juntos; un minero sin cuenta cobra en miner:<id>.
    shares = {"pc-mateo-pod-a": 10, "pc-mateo-pod-b": 30, "gpu-pc": 60, "otro": 100}
    accounts = {"pc-mateo-pod-a": "mateo", "pc-mateo-pod-b": "mateo", "gpu-pc": "mateo"}
    assert reward.shares_by_account(shares, accounts) == {"mateo": 100, "miner:otro": 100}
    txs = reward.reward_transactions(3, reward.shares_by_account(shares, accounts), 5000, "mateo", ts=1.0)
    assert [(t["receiver"], t["amount"]) for t in txs] == [("mateo", 2500), ("miner:otro", 2500)]


def test_reward_transactions_and_verify_chain():
    txs = reward.reward_transactions(3, {"miner:pc": 10, "miner:pod-1": 30}, 5000, "miner:pod-1", ts=1.0)
    assert [t["receiver"] for t in txs] == ["miner:pc", "miner:pod-1"]
    prev = {"index": 3, "reward_cents": 5000}
    assert verify_chain.check_rewards({"transactions": txs}, prev) == []
    txs[0]["amount"] += 1
    assert verify_chain.check_rewards({"transactions": txs}, prev)


def test_reward_sender_is_reserved():
    with pytest.raises(ledger.InvalidTx):
        ledger.normalize({"sender": "recompensa", "receiver": "user-1", "amount": 1})


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
