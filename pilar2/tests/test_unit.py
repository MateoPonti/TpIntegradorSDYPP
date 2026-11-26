"""Tests unitarios de lo crítico: PoW, validación de tx, decisiones del pool, armado del bloque,
premio, génesis y motor CPU."""
import pytest

import miner_cpu
import verify_chain
from coordinator import blocks, ledger, reward
from coordinator.blocks import genesis_block
from pool import decision
from tpi_common import pow
from tpi_common.store import parse_sentinels


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


def _work(now, ago=10, shares=32, share_len=4, seconds=1.0, **extra):
    """Resultado como lo guarda el gateway desde el 26/11: shares verificadas con MD5 y segundos medidos por él."""
    return {"hashes": 9e12, "engine_seconds": 1e-6, "seconds": seconds, "ts": now - ago,
            "share_len": share_len, "valid_shares": shares, **extra}


def test_miner_hashrate_uses_delivered_work():
    now = 1000.0
    results = [{"hashes": 2_000_000, "engine_seconds": 1.0, "seconds": 1.5, "ts": now - 10}]
    motor, efectivo = decision.miner_rates(results, now, 300)
    assert motor == 2e6 and round(efectivo) == 1_333_333  # se siguen mostrando, pero ya no deciden nada
    # B1.2 (26/11): lo que informa el motor no se cree. Sin shares verificadas, la calibración.
    assert decision.miner_hashrate(results, {"hashrate": 5e5}, 9e9, None, False, now, 300) == (5e5, "calibracion")
    # Con shares verificadas: 3 tareas × 32 shares de largo 4 (16^4 = 65536 hashes cada una) en 3 s.
    worked = [_work(now, ago=i) for i in (5, 10, 15)]
    assert decision.miner_hashrate(worked, {"hashrate": 5e5}, 9e9, None, False, now, 300) == (32 * 65536, "shares")
    # Sin resultados: la calibración; sin calibración: lo declarado.
    assert decision.miner_hashrate([], {"hashrate": 5e5}, 9e9, None, False, now, 300) == (5e5, "calibracion")
    assert decision.miner_hashrate([], None, 7.0, None, False, now, 300) == (7.0, "declarado")


def test_zombie_gpu_does_not_count_as_alive():
    # Si la GPU colgada contara como viva, el pool pediría 0 mineros CPU y nadie minaría.
    assert decision.counts_as_alive({"quarantined": False, "source": "shares"})
    assert not decision.counts_as_alive({"quarantined": False, "source": "zombie"})
    assert not decision.counts_as_alive({"quarantined": True, "source": "cuarentena"})
    signal_cpu = decision.desired_cpu_miners(gpu_alive=0, has_work=True, idle_seconds=0, max_miners=8, idle_limit=60)
    assert signal_cpu == 8


def test_found_early_results_do_not_measure_speed():
    # Prefijo corto: encontró a los 3.000 hashes, pero el motor tardó 0,5 s en arrancar -> 6 kH/s falso.
    now = 1000.0
    early = [{"hashes": 3000, "engine_seconds": 0.5, "seconds": 0.7, "ts": now - 5, "found": True}]
    assert decision.miner_rates(early, now, 300) is None
    assert decision.miner_hashrate(early, {"hashrate": 1.4e6}, 0, None, False, now, 300) == (1.4e6, "calibracion")


def test_zombie_and_quarantined_miners_do_not_count():
    now = 1000.0
    results = [_work(now, ago=100), _work(now, ago=101), _work(now, ago=102)]
    assert decision.miner_hashrate(results, None, 5e8, now - 5, False, now, 300) == (0.0, "zombie")
    assert decision.miner_hashrate(results, None, 5e8, None, True, now, 300) == (0.0, "cuarentena")
    # Un vencimiento viejo, anterior a su último resultado, no lo marca.
    assert decision.miner_hashrate(results, None, 5e8, now - 200, False, now, 300)[1] == "shares"


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


def test_parse_sentinels():
    assert parse_sentinels("") == []
    assert parse_sentinels("redis-sentinel.tpi.svc.cluster.local:26379") == [("redis-sentinel.tpi.svc.cluster.local", 26379)]
    assert parse_sentinels(" a:1 , b ") == [("a", 1), ("b", 26379)]


def test_cuentas_con_clave():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from coordinator import accounts

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    msg = accounts.tx_message("mateo", "user-3", 1050, 1)
    from tpi_common import firmas
    sig = key.sign(firmas.domain(msg)).hex()  # desde el 26/11 se firma con el chain id adelante (A3-g)
    assert accounts.verify(pub, msg, sig)
    assert not accounts.verify(pub, accounts.tx_message("mateo", "user-3", 1050, 2), sig)  # otro seq: replay
    assert not accounts.verify(pub, msg, "00" * 64) and not accounts.verify(pub, msg, None)
    assert accounts.is_genesis("user-0") and accounts.is_genesis("user-96") and not accounts.is_genesis("user-97")
    assert accounts.reserved("user-5") and accounts.reserved("recompensa") and accounts.reserved("miner:x")
    assert not accounts.reserved("mateo")


def test_genesis_solo_dentro_del_genesis():
    """A1 (pedido del profe, 26/11): las cuentas del génesis no firman, así que su plata no puede salir del génesis.
    Si no, cualquiera se transfería los ~97M a una cuenta propia."""
    from coordinator import accounts

    assert accounts.genesis_transfer_ok("user-3", "user-4")
    assert accounts.genesis_transfer_ok("user-3", "user-3")
    assert not accounts.genesis_transfer_ok("user-3", "fede")
    assert not accounts.genesis_transfer_ok("user-3", "colecta:viaje")
    assert not accounts.genesis_transfer_ok("user-3", "miner:gpu-1")
    assert not accounts.genesis_transfer_ok("user-3", "user-97")  # parece del génesis pero no lo es
    assert accounts.genesis_transfer_ok("fede", "user-3")  # al génesis sí se le puede mandar
    assert accounts.genesis_transfer_ok("fede", "ana")


# ---- Notifier (punto 3 del contrato) ----

def _solved():
    blk = {"index": 7, "block_hash": "00ab", "timestamp": 1.0, "miner": "gpu-1",
           "transactions": [{"tx_id": "t1", "sender": "user-1", "receiver": "mateo", "amount": 500, "ts": 0},
                            {"tx_id": "t2", "sender": "mateo", "receiver": "mateo", "amount": 100, "ts": 0}]}
    return blocks.solved_event(blk, "7-abcd")


def test_solved_event_has_what_gateway_and_notifier_need():
    ev = _solved()
    assert ev["block_id"] == "7-abcd" and ev["event_id"] == "block-7" and ev["type"] == "block.solved"
    assert [t["tx_id"] for t in ev["txs"]] == ["t1", "t2"] and "ts" not in ev["txs"][0]


def test_notifier_routes_only_what_each_client_asked():
    from notifier import main as notifier

    ev = _solved()
    subs = {"s1": ("blocks", None), "s2": ("tx", "t1"), "s3": ("tx", "otra"), "s4": ("account", "mateo"),
            "s5": ("account", "user-9")}
    msgs = {m["params"]["subscription"]: m["params"]["result"]
            for m in notifier.route(ev, notifier.index_event(ev), subs)}
    assert set(msgs) == {"s1", "s2", "s4"}
    assert msgs["s1"]["event"] == "block.solved" and msgs["s1"]["txs"] == 2
    assert msgs["s2"]["event"] == "tx.confirmed" and msgs["s2"]["index"] == 7
    # mateo recibe 500 de user-1 y se manda 100 a sí mismo (una sola línea, no entrada y salida)
    assert msgs["s4"]["movements"] == [{"tx_id": "t1", "dir": "in", "other": "user-1", "amount": 500},
                                       {"tx_id": "t2", "dir": "out", "other": "mateo", "amount": 100}]


def test_notifier_origin_and_subscription_checks():
    from notifier import main as notifier

    assert notifier.origin_ok(None, "x.sslip.io", set())  # no es un navegador
    assert notifier.origin_ok("https://x.sslip.io", "x.sslip.io", set())
    assert not notifier.origin_ok("https://malo.com", "x.sslip.io", set())
    assert notifier.origin_ok("http://127.0.0.1:18080", "127.0.0.1:18280", {"http://127.0.0.1:18080"})
    assert notifier.parse_subscription(["tx", "t1"]) == ("tx", "t1")
    for bad in (["saldos"], ["account"], ["tx", "x" * 200]):
        with pytest.raises(ValueError):
            notifier.parse_subscription(bad)


def test_notifier_cuts_slow_clients_instead_of_buffering():
    import asyncio

    from notifier import main as notifier

    client = notifier.Client(ws=None)
    for i in range(notifier.QUEUE_SIZE):
        assert client.push({"n": i})
    assert not client.push({"n": "uno de más"})
    assert client.queue.qsize() == 1 and client.queue.get_nowait() is notifier.CLOSE
    assert isinstance(client.queue, asyncio.Queue)


# ---- Halving y comisión (modelo de Bitcoin, 20/11) ----

def test_subsidy_halves_and_total_is_capped():
    assert [reward.block_subsidy(i, 5000, 1000) for i in (0, 1, 1000, 1001, 2001)] == [0, 5000, 5000, 2500, 1250]
    total = sum(reward.block_subsidy(i, 5000, 10) for i in range(1, 10 * 40))
    assert total < 5000 * 10 * 2 and reward.block_subsidy(10 * 20, 5000, 10) == 0  # tope y llega a 0
    assert verify_chain.subsidy(1001, 5000, 1000) == reward.block_subsidy(1001, 5000, 1000)


def test_verify_chain_checks_subsidy_and_fees():
    eco = {"base_reward_cents": 5000, "halving_blocks": 1000}
    txs = [{"tx_id": "a", "sender": "u", "receiver": "v", "amount": 100, "fee": 10},
           {"tx_id": "r", "sender": "recompensa", "receiver": "m", "amount": 5000, "reward_for": 1}]
    ok = {"index": 2, "transactions": txs, "subsidy_cents": 5000, "fees_cents": 10, "reward_cents": 5010}
    assert verify_chain.check_economy(ok, eco) == []
    assert verify_chain.check_economy({**ok, "reward_cents": 6000}, eco)  # premio inflado
    assert verify_chain.check_economy({**ok, "subsidy_cents": 9000, "reward_cents": 9010}, eco)  # subsidio que no toca
    assert verify_chain.check_economy({"index": 1, "transactions": [], "reward_cents": 5000}, eco) == []  # bloque viejo


# ---- Cuentas compartidas (punto 2 del contrato, 20/11) ----

def test_multisig_definition_and_messages():
    from coordinator import multisig

    assert multisig.check_definition("centro", ["mateo", "ana", "mateo", "fede"], 2) == (["mateo", "ana", "fede"], 2)
    for owners, m in ((["mateo"], 1), (["a", "b"], 3), (["a", "b"], 0), (["a", "b", "c"], 1), (["centro", "a"], 2),
                      ("a,b", 2)):
        with pytest.raises(ValueError):
            multisig.check_definition("centro", owners, m)
    msg = multisig.payment_message("centro", "imprenta", 50000, "n1")
    assert msg == "multisig:centro:imprenta:50000:n1"
    # cambiar el monto o el destino da otra propuesta: las firmas de una no sirven para la otra
    assert multisig.proposal_id(msg) != multisig.proposal_id(multisig.payment_message("centro", "imprenta", 50001, "n1"))


def test_multisig_view_counts_signatures_and_expires():
    from coordinator import multisig

    raw = multisig.new_proposal("p1", "centro", "imprenta", 500, 10, "m", now=1000.0)
    raw = {k: str(v) for k, v in raw.items()} | {"sig:mateo": "aa"}
    v = multisig.view(raw, threshold=2, now=1001.0)
    assert (v["status"], v["signatures"], v["missing"], v["signers"]) == ("pendiente", 1, 1, ["mateo"])
    assert multisig.view(raw, threshold=2, now=1000.0 + multisig.TTL + 1)["status"] == "vencida"
    assert multisig.tx_extra(raw | {"sig:ana": "bb"})["multisig"]["signatures"] == {"mateo": "aa", "ana": "bb"}
    # esperando saldo (21/11, como Safe): las firmas siguen valiendo, pero igual vence
    waiting = raw | {"status": "esperando_saldo"}
    assert multisig.view(waiting, threshold=2, now=1001.0)["status"] == "esperando_saldo"
    assert multisig.view(waiting, threshold=2, now=1000.0 + multisig.TTL + 1)["status"] == "vencida"
    assert multisig.tx_id("p1") == "multisig-p1"  # fijo: un reintento no paga dos veces


def test_account_names_and_crowdfund_reserve_keys():
    from coordinator import ledger

    for ok in ("mateo", "ana.perez", "user_2", "centro-76493", "a" * 64):
        assert ledger.ACCOUNT_NAME.fullmatch(ok)
    for bad in ("", "a/b", 'x"y', "$(rm)", "con espacio", "a" * 65, "colecta:x"):
        assert not ledger.ACCOUNT_NAME.fullmatch(bad), bad
    # 21/11: un aporte reserva y se anota en el mismo Lua (las 3 claves de la colecta van en la reserva)
    base = {"sender": "ana", "amount": 500}
    assert len(ledger.reserve_keys(base | {"receiver": "beto"})) == 6
    assert ledger.reserve_keys(base | {"receiver": "colecta:viaje"})[6:] == [
        "crowdfund:viaje:contrib", "crowdfund:viaje", "crowdfund:viaje:txs"]


# ---- Shares (punto 4 del contrato, 20/11) ----

def test_share_prefix_gives_about_target_shares_and_is_shorter_than_block():
    assert pow.share_prefix("00000000", 1_000_000_000, 32) == "000000"  # 1e9 / 16^6 ~ 60 shares
    assert pow.share_prefix("00000000", 2_000_000, 32) == "0000"        # 2e6 / 16^4 ~ 30
    assert pow.share_prefix("00", 10**12, 32) == "00"                  # nunca más largo que el del bloque
    assert pow.share_prefix("000000", 10, 32) == "0"                   # al menos 1
    assert pow.share_prefix("", 10**6) == ""
    assert pow.share_weight("000") == 4096


def test_valid_shares_recomputes_and_rejects_bad_ones():
    base = "a" * 32 + "b" * 64
    real = [n for n in range(20_000) if pow.md5_hex(base, n).startswith("00")]
    fake = next(n for n in range(20_000) if not pow.md5_hex(base, n).startswith("00"))
    got = pow.valid_shares(base, "00", 0, 9_999, real + real[:3] + [fake, True, "7", 10**30])
    assert got == [n for n in real if n <= 9_999]  # fuera de rango, repetidas, falsas y basura afuera
    assert pow.valid_shares(base, "", 0, 9_999, real) == []


def test_cpu_miner_reports_all_shares_of_the_range():
    base = "a" * 32 + "b" * 64
    res = miner_cpu.search(base, "ffffffff", 0, 30_000, workers=1, share="00", max_shares=1024)
    assert res["shares"] == [n for n in range(30_001) if pow.md5_hex(base, n).startswith("00")]
    capped = miner_cpu.search(base, "ffffffff", 0, 30_000, workers=1, share="00", max_shares=5)
    assert len(capped["shares"]) == 5


# ---- Colectas (punto 2 del contrato, 20/11) ----

def test_crowdfund_outcome_and_payments():
    from coordinator import crowdfund

    raw = {"id": "viaje", "goal": "1000", "beneficiary": "centro", "status": "abierta", "deadline": "2000"}
    contrib = {"ana": "600", "fede": "500"}
    assert crowdfund.outcome(raw, contrib) == "exitosa"
    assert crowdfund.payments("exitosa", raw, contrib) == [("centro", 1100)]
    assert crowdfund.outcome(raw, {"ana": "999"}) == "fallida"
    assert crowdfund.payments("fallida", raw, contrib) == [("ana", 600), ("fede", 500)]
    assert crowdfund.outcome(raw | {"goal": "0"}, {}) == "fallida"  # sin aportes no hay pago
    tx = crowdfund.settlement_tx("viaje", "ana", 600, "fallida", 5.0)
    assert (tx["sender"], tx["fee"], tx["tx_id"], tx["crowdfund"]["kind"]) == ("colecta:viaje", 0, "colecta-viaje-ana", "devolucion")


def test_crowdfund_deadline_open_and_reserved_names():
    from coordinator import accounts, crowdfund

    now = 1_000_000.0
    assert crowdfund.check_deadline(int(now) + 3600, now) == int(now) + 3600
    for bad in (int(now) + 5, int(now) + 8 * 86400, "123", 1.5, True):
        with pytest.raises(ValueError):
            crowdfund.check_deadline(bad, now)
    raw = {"id": "viaje", "status": "abierta", "deadline": str(now + 10)}
    assert crowdfund.is_open(raw, now) and not crowdfund.is_open(raw, now + 11)
    assert crowdfund.view(raw, now=now + 11)["status"] == "cerrando"
    assert crowdfund.of_account("colecta:viaje") == "viaje" and crowdfund.of_account("viaje") is None
    assert accounts.reserved("colecta:viaje")  # nadie puede registrar la cuenta de una colecta


def test_quarantine_grows_and_resets_after_an_hour():
    import asyncio

    from tpi_common import quarantine

    class FakeRedis:
        def __init__(self):
            self.n, self.ttl = 0, None

        async def incr(self, key):
            self.n += 1
            return self.n

        async def expire(self, key, seconds):
            self.ttl = seconds

    r = FakeRedis()
    got = [asyncio.run(quarantine.strike(r, "m", 300, 3600)) for _ in range(6)]
    assert got == [(1, 300), (2, 600), (3, 1200), (4, 2400), (5, 3600), (6, 3600)]  # al doble, con tope de 1 h
    assert r.ttl == quarantine.STRIKES_WINDOW  # la cuenta vence sola a la hora de la primera


# ---- Chain id en lo que se firma (A3-g, 26/11, como EIP-155) ----

def _key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    return k, k.public_key().public_bytes_raw().hex()


def test_domain_lleva_el_chain_id():
    from tpi_common import config, firmas
    assert config.CHAIN_ID == 2026
    assert firmas.domain(b"tx:a:b:100:1") == b"sdmer:2026:tx:a:b:100:1"


def test_firma_de_otra_cadena_no_vale():
    from tpi_common import firmas
    from coordinator import accounts
    k, pub = _key()
    msg = accounts.tx_message("a", "b", 100, 1)
    assert firmas.verify(pub, msg, k.sign(b"sdmer:2026:" + msg).hex())
    assert not firmas.verify(pub, msg, k.sign(b"sdmer:2027:" + msg).hex())  # otra red: no se puede reusar acá


def test_firma_vieja_sin_chain_id_no_vale():
    """Una firma de antes del 26/11 (sin chain id) se rechaza: el front y tools/cuenta.py viejos tienen que actualizarse."""
    from tpi_common import firmas
    from coordinator import accounts, multisig
    k, pub = _key()
    for msg in (accounts.tx_message("a", "b", 100, 1), accounts.register_message("a", pub),
                firmas.worker_message("a", "w1"), multisig.reject_message("caja"),
                multisig.create_message("caja", 2, ["a", "b"])):
        assert not firmas.verify(pub, msg, k.sign(msg).hex())
        assert firmas.verify(pub, msg, k.sign(firmas.domain(msg)).hex())


def test_mensaje_de_firma_invalida_explica_el_cambio():
    from tpi_common import firmas
    assert "chain id" in firmas.INVALID and "actualizá" in firmas.INVALID


# ---- Hashrate medido con shares y Vardiff (B1.2 y A3-d, 26/11) ----

def test_shares_rate_con_shares_verificadas():
    from tpi_common import vardiff
    now = 1000.0
    entries = [_work(now, ago=5, shares=30, share_len=3, seconds=2.0), _work(now, ago=8, shares=10, share_len=4, seconds=2.0)]
    entries.append(_work(now, ago=9, shares=0, share_len=4, seconds=1.0))
    expected = (30 * 16**3 + 10 * 16**4) / 5.0
    assert vardiff.shares_rate(entries, now, 300) == expected


def test_shares_rate_necesita_suficientes_shares_y_recientes():
    from tpi_common import vardiff
    now = 1000.0
    assert vardiff.shares_rate([_work(now, shares=5)], now, 300) is None  # pocas shares: puro ruido
    assert vardiff.shares_rate([_work(now, ago=400) for _ in range(3)], now, 300) is None  # viejas
    assert vardiff.shares_rate([_work(now, found=True) for _ in range(3)], now, 300) is None  # cortadas por el nonce
    assert vardiff.shares_rate([{"hashes": 1e9, "seconds": 1, "ts": now}], now, 300) is None  # formato viejo


def test_share_len_limites():
    from tpi_common import vardiff
    # Tarea de 2^32 nonces: entre 8 y 256 shares esperadas por tarea -> largos 6 (2^32/16^6 = 256) a 7 (16 por tarea).
    assert vardiff.share_bounds(block_len=8, range_size=1 << 32) == (6, 7)
    assert vardiff.share_bounds(block_len=6, range_size=2_000_000) == (4, 4)  # 30 por tarea: un solo largo sirve
    assert vardiff.share_len(1.0, block_len=8, range_size=1 << 32) == 6  # lentísimo: el mínimo que no trunca
    assert vardiff.share_len(1e12, block_len=8, range_size=1 << 32) == 7  # rapidísimo: el máximo con 8 por tarea
    assert vardiff.share_len(1e12, block_len=2, range_size=2_000_000) == 2  # nunca más largo que el bloque


def test_vardiff_minero_rapido_prefijo_mas_largo():
    from tpi_common import vardiff
    # El prefijo es hex: cada largo es 16 veces más difícil. Con 8 a 256 shares por tarea entran 1 o 2 largos por
    # tamaño de tarea, así que Vardiff solo separa a los muy distintos (acá, un CPU de un GPU grande).
    lento = vardiff.share_len(1e6, block_len=8, range_size=1 << 36)
    rapido = vardiff.share_len(1e11, block_len=8, range_size=1 << 36)
    assert (lento, rapido) == (7, 8)


def test_clase_sale_del_hashrate():
    from tpi_common import vardiff
    assert vardiff.classify(2e8) == "rapido" and vardiff.classify(1e7) == "medio" and vardiff.classify(1e5) == "lento"
