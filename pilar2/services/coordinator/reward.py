"""Premio al minero (decidido el 13/11): proporcional al trabajo de cada uno en el bloque, como un pool real
(PROP). Desde el 20/11 el trabajo se mide con shares ponderadas por dificultad (cada una pesa 16^largo de su
prefijo), no con los nonces que el minero dice haber recorrido. El premio del bloque i se paga con transacciones dentro del bloque i+1, porque el
tx_digest del bloque i se calcula antes de saber quién lo va a resolver.

Se paga a la cuenta de cobro de cada minero (MINER_ACCOUNT del agente; por defecto `miner:<id>`), como la
dirección que pone un minero de Bitcoin en su bloque. Así los pods de KEDA, que cambian de nombre, y la
GPU de la misma persona cobran todos en la misma cuenta: sus partes se suman."""

REWARD_SENDER = "recompensa"  # el subsidio es plata nueva; las comisiones ya las pagaron los que mandaron tx


def block_subsidy(index: int, base_cents: int, halving_blocks: int) -> int:
    """Premio nuevo del bloque `index` (como Bitcoin, decidido el 20/11): `base_cents` en los primeros
    `halving_blocks` bloques y a la mitad en cada tramo siguiente. En centavos enteros llega a 0, así que la plata
    que se emite en total tiene tope (con 50,00 y 1.000 bloques, menos de 100.000). Después los mineros cobran solo
    las comisiones."""
    if index < 1:
        return 0
    halvings = (index - 1) // halving_blocks
    return base_cents >> halvings if halvings < 64 else 0


def miner_account(miner_id: str) -> str:
    """Cuenta de cobro si el minero no declaró una."""
    return f"miner:{miner_id}"


def shares_by_account(shares: dict[str, int], accounts: dict[str, str]) -> dict[str, int]:
    """Suma el trabajo (peso de sus shares) de todos los mineros que cobran en la misma cuenta."""
    out: dict[str, int] = {}
    for miner, n in shares.items():
        account = accounts.get(miner) or miner_account(miner)
        out[account] = out.get(account, 0) + int(n)
    return out


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
    """Transacciones de premio del bloque `index` (van primero en el bloque siguiente).
    `shares` y `finder` ya están por cuenta de cobro."""
    return [{"tx_id": f"reward-{index}-{account}", "sender": REWARD_SENDER, "receiver": account,
             "amount": cents, "ts": ts, "reward_for": index}
            for account, cents in sorted(split_reward(shares, reward_cents, finder).items())]
