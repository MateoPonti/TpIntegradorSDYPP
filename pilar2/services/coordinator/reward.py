"""Premio al minero (decidido el 13/11): proporcional a los nonces que completó cada uno en el bloque,
como un pool real. El premio del bloque i se paga con transacciones dentro del bloque i+1, porque el
tx_digest del bloque i se calcula antes de saber quién lo va a resolver."""

REWARD_SENDER = "recompensa"  # plata nueva: no sale de ninguna cuenta


def miner_account(miner_id: str) -> str:
    return f"miner:{miner_id}"


def split_reward(shares: dict[str, int], reward_cents: int, finder: str | None) -> dict[str, int]:
    """Reparte en centavos enteros. Lo que sobra del redondeo va al que encontró el nonce."""
    shares = {m: int(n) for m, n in shares.items() if int(n) > 0}
    total = sum(shares.values())
    if total == 0 or reward_cents <= 0:
        return {}
    out = {m: reward_cents * n // total for m, n in shares.items()}
    rest = reward_cents - sum(out.values())
    if rest:
        winner = finder if finder in out else max(shares, key=shares.get)
        out[winner] += rest
    return {m: c for m, c in out.items() if c > 0}


def reward_transactions(index: int, shares: dict[str, int], reward_cents: int, finder: str | None,
                        ts: float) -> list[dict]:
    """Transacciones de premio del bloque `index` (van primero en el bloque siguiente)."""
    return [{"tx_id": f"reward-{index}-{m}", "sender": REWARD_SENDER, "receiver": miner_account(m),
             "amount": cents, "ts": ts, "reward_for": index}
            for m, cents in sorted(split_reward(shares, reward_cents, finder).items())]
