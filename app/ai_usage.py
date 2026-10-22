"""Contabilità delle chiamate AI di mywahoo, per sapere quando ricaricare.

OpenAI non espone il saldo residuo via API, quindi lo stimiamo: l'utente indica
quanto ha caricato e quando; ogni chiamata registra token e costo stimato;
residuo = caricato − speso da allora. Conta solo le chiamate di QUESTA app
(planmydinner usa un altro account). È una stima: fa fede la pagina Billing.
"""
import logging
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from .db import AiUsage, engine, get_setting, set_setting

logger = logging.getLogger(__name__)

# $ per milione di token (input, input in cache, output) — prezzi base GPT-5.5.
# Modificabili da Impostazioni se si cambia modello.
DEFAULT_PRICES = (5.0, 0.5, 30.0)
DEFAULT_ALERT_USD = 2.0


def _num(v) -> float | None:
    try:
        return float(v) if str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def prices() -> tuple[float, float, float]:
    keys = ("ai.price_in", "ai.price_cached", "ai.price_out")
    return tuple(_num(get_setting(k, "")) if _num(get_setting(k, "")) is not None else d
                 for k, d in zip(keys, DEFAULT_PRICES))


def cost(tok_in: int, tok_cached: int, tok_out: int, p=None) -> float:
    """Costo stimato in $: l'input in cache costa meno (è già incluso in tok_in)."""
    p_in, p_cached, p_out = p or prices()
    fresh = max(0, (tok_in or 0) - (tok_cached or 0))
    return (fresh * p_in + (tok_cached or 0) * p_cached + (tok_out or 0) * p_out) / 1e6


def record(provider: str, model: str, tok_in, tok_out, tok_cached) -> None:
    """Registra una chiamata. Non deve MAI far fallire la chiamata AI."""
    try:
        with Session(engine) as s:
            s.add(AiUsage(provider=provider, model=model or "", tok_in=tok_in or 0,
                          tok_cached=tok_cached or 0, tok_out=tok_out or 0,
                          cost_usd=round(cost(tok_in, tok_cached, tok_out), 6)))
            s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("AI usage not recorded: %s", e)


def save_credit(amount: str, day: str, alert: str, p_in: str, p_cached: str, p_out: str) -> None:
    set_setting("ai.credit_amount", str(_num(amount)) if _num(amount) is not None else "")
    try:
        set_setting("ai.credit_date", date.fromisoformat(day.strip()).isoformat())
    except ValueError:
        set_setting("ai.credit_date", "")
    set_setting("ai.credit_alert", str(_num(alert)) if _num(alert) is not None else "")
    for k, v in (("ai.price_in", p_in), ("ai.price_cached", p_cached), ("ai.price_out", p_out)):
        set_setting(k, str(_num(v)) if _num(v) is not None else "")


def credit_status(provider: str = "openai") -> dict:
    """Credito stimato: caricato, speso da allora, residuo, consumo medio e giorni
    rimanenti. `low` = sotto la soglia d'avviso."""
    amount = _num(get_setting("ai.credit_amount", ""))
    since_s = get_setting("ai.credit_date", "")
    alert = _num(get_setting("ai.credit_alert", "")) or DEFAULT_ALERT_USD
    now = datetime.utcnow()
    try:
        since = datetime.combine(date.fromisoformat(since_s), datetime.min.time())
    except ValueError:
        since = None
    week_ago = now - timedelta(days=7)
    with Session(engine) as s:
        rows = s.exec(select(AiUsage).where(AiUsage.provider == provider,
                                            AiUsage.at >= min(since or week_ago, week_ago))).all()
    spent = sum(r.cost_usd for r in rows if since and r.at >= since)
    last7 = [r for r in rows if r.at >= week_ago]
    # media sui giorni effettivamente coperti dalla registrazione (non sempre 7)
    span = (now - min(r.at for r in last7)).total_seconds() / 86400 if last7 else 0
    per_day = sum(r.cost_usd for r in last7) / max(1.0, min(7.0, span)) if last7 else 0.0
    remaining = (amount - spent) if amount is not None and since else None
    today = sum(r.cost_usd for r in rows if r.at.date() == now.date())
    return {
        "amount": amount, "since": since_s or None, "alert": alert,
        "spent": round(spent, 2), "remaining": round(remaining, 2) if remaining is not None else None,
        "per_day": round(per_day, 3), "today": round(today, 3), "calls_7d": len(last7),
        "days_left": int(remaining / per_day) if remaining is not None and per_day > 0 else None,
        "low": remaining is not None and remaining < alert,
        "prices": prices(),
    }
