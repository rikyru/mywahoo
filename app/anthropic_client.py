"""AI analysis of training sessions (Anthropic Messages API or OpenAI Chat Completions).

Raw httpx (no SDK) per project constraints. The provider is selected with
AI_PROVIDER (default "anthropic"); the API key never leaves the server.
"""
import asyncio
import json
import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_VERSION = "2023-06-01"
# Generous cap: reasoning models (gpt-5*/o-series) spend part of this budget on
# hidden reasoning, so a low cap can leave zero room for the visible answer.
MAX_TOKENS = 4000
# Prefixes of OpenAI reasoning models that accept the reasoning_effort param
OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
TIMEOUT = httpx.Timeout(120.0, connect=10.0)
MAX_RETRIES = 2  # on 429 / 5xx / 529


class AnthropicError(Exception):
    """User-presentable error from the AI analysis layer."""


SESSION_SYSTEM_PROMPT = """\
Sei un allenatore esperto di endurance (ciclismo, corsa e sport affini), con
particolare competenza in allenamento a potenza. Analizza la sessione fornita
(summary + statistiche aggregate degli stream) e rispondi in italiano, in
Markdown, con queste sezioni:

## Valutazione dello sforzo
## Qualità della sessione
## Punti di forza e debolezza
## Suggerimenti per la prossima uscita
## Anomalie

Per le anomalie considera ad esempio: FC sproporzionata rispetto alla potenza,
drift cardiaco elevato (>5% indica affaticamento/disidratazione/caldo), cali di
potenza o cadenza nei decimi finali, pause anomale. Se un dato non è disponibile
dillo esplicitamente invece di inventare. Sii concreto e quantitativo."""

PERIOD_SYSTEM_PROMPT = """\
Sei un allenatore esperto di endurance. Ti viene fornito l'elenco delle sessioni
di un periodo. Scrivi in italiano, in Markdown, una sintesi del periodo con:
carico complessivo e distribuzione, progressione o regressione, equilibrio tra
intensità e recupero, e 2-3 raccomandazioni per il periodo successivo.
Sii concreto e quantitativo dove possibile."""

HEALTH_SYSTEM_PROMPT = """\
Sei un coach di salute, recupero e sonno per uno sportivo amatoriale. Ti vengono
forniti gli indicatori del periodo selezionato (FC a riposo, HRV, SpO2, frequenza
respiratoria, temperatura cutanea notturna, peso, composizione corporea, sonno)
con valori più recenti, variazioni e min/media/max, un "indice di forma" 0-100
calcolato sulla baseline personale, E l'elenco delle attività fisiche del periodo
(data, sport, durata, distanza, FC/potenza media, TSS). Rispondi in italiano, in
Markdown, conciso, con queste sezioni:

## Recupero e forma
## Sonno
## Carico e recupero
## Tendenze da tenere d'occhio
## Consigli

Interpreta i trend in modo integrato (es. HRV in calo + FC a riposo in aumento =
recupero peggiore; SpO2 bassa o respiratoria in aumento + temperatura sopra
baseline possono indicare stress/malattia in arrivo). Nella sezione "Carico e
recupero" **correla esplicitamente le attività con il recupero**: confronta le
notti/giorni dopo sessioni intense o voluminose con HRV, FC a riposo e qualità
del sonno; segnala se il corpo recupera bene dal carico o se accumula fatica
(HRV depressa o FC a riposo elevata dopo i picchi di allenamento), e se i giorni
di riposo/scarico portano un rimbalzo del recupero. Se è presente la sezione
"alimentazione" (aderenza al piano, pasti liberi, composizione), correlala con
recupero e carico (es. bassa aderenza o molti pasti liberi in una fase di carico
↔ recupero peggiore) e aggiungi 1 riga a riguardo nei Consigli; se assente, NON
parlarne. NON dare diagnosi mediche: se qualcosa appare anomalo, suggerisci
cautela o un controllo medico. Se un dato manca, dillo invece di inventare. Sii
quantitativo. Esprimi SEMPRE le durate del sonno in ore e minuti (es. "6h30"),
mai in minuti."""


def effective_provider() -> str:
    """AI provider: DB override (set from Settings) else env default."""
    from .db import get_setting
    return get_setting("ai_provider") or settings.ai_provider


def effective_model() -> str:
    from .db import get_setting
    prov = effective_provider()
    return get_setting("ai_model") or (settings.openai_model if prov == "openai"
                                       else settings.anthropic_model)


def _provider_key(provider: str) -> str:
    return settings.openai_api_key if provider == "openai" else settings.anthropic_api_key


def _build_request(system: str, messages: list) -> tuple[str, dict, dict, str, str]:
    """Return (url, payload, headers, label, provider) for the active provider."""
    provider, model = effective_provider(), effective_model()
    if provider == "openai":
        payload = {
            "model": model,
            "max_completion_tokens": MAX_TOKENS,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        # Reasoning models: keep reasoning light so the budget goes to the answer
        if model.startswith(OPENAI_REASONING_PREFIXES):
            payload["reasoning_effort"] = "low"
        headers = {"authorization": f"Bearer {settings.openai_api_key}",
                   "content-type": "application/json"}
        return OPENAI_API_URL, payload, headers, "OpenAI", provider
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    headers = {"x-api-key": settings.anthropic_api_key,
               "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    return API_URL, payload, headers, "Anthropic", provider


def _extract_text(data: dict, provider: str) -> tuple[str, int | None, int | None]:
    """Return (text, input_tokens, output_tokens) from the provider response."""
    usage = data.get("usage", {})
    if provider == "openai":
        choices = data.get("choices", [])
        text = (choices[0].get("message", {}).get("content") or "") if choices else ""
        return text, usage.get("prompt_tokens"), usage.get("completion_tokens")
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
    return text, usage.get("input_tokens"), usage.get("output_tokens")


async def _call_claude(system: str, user_content: str) -> str:
    """Single-turn call (kept for the analysis/summary callers)."""
    return await _call_messages(system, [{"role": "user", "content": user_content}])


async def _call_messages(system: str, messages: list) -> str:
    """Call the AI API with a full message list, retrying on transient errors."""
    url, payload, headers, label, provider = _build_request(system, messages)

    last_error = "unknown"
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            last_error = f"timeout della richiesta verso l'API {label}"
            logger.warning("%s API timeout (attempt %s/%s)", label, attempt + 1, MAX_RETRIES + 1)
            continue
        except httpx.HTTPError as e:
            last_error = f"errore di rete verso l'API {label}: {e}"
            logger.warning("%s API network error: %s", label, e)
            continue

        if resp.status_code == 200:
            data = resp.json()
            text, tok_in, tok_out = _extract_text(data, provider)
            if not text:
                raise AnthropicError(f"Risposta vuota dall'API {label}")
            logger.info("AI analysis ok (%s): %s in / %s out tokens", label, tok_in, tok_out)
            return text

        # Transient: rate limit / overloaded / server error -> backoff and retry
        if resp.status_code in (429, 529) or resp.status_code >= 500:
            retry_after = int(resp.headers.get("retry-after", 2 ** attempt * 5))
            last_error = f"API {label} occupata (HTTP {resp.status_code})"
            logger.warning("%s HTTP %s — retrying in %ss (attempt %s/%s)",
                           label, resp.status_code, retry_after, attempt + 1, MAX_RETRIES + 1)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(min(retry_after, 30))
            continue

        # Non-retryable 4xx: surface a clear message, never the API key
        try:
            err_msg = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            err_msg = resp.text[:200]
        logger.error("%s API error HTTP %s: %s", label, resp.status_code, err_msg)
        raise AnthropicError(f"Errore API {label} (HTTP {resp.status_code}): {err_msg}")

    raise AnthropicError(f"Analisi non disponibile: {last_error}. Riprova tra qualche minuto.")


async def analyze_workout(summary_row: dict, stream_stats: dict) -> str:
    """Generate the AI analysis for one workout: summary + aggregated stream stats."""
    body = ("Dati di riepilogo della sessione:\n"
            + json.dumps(summary_row, ensure_ascii=False, indent=2, default=str))
    if stream_stats:
        body += ("\n\nStatistiche aggregate dagli stream del file FIT "
                 "(medie per decimo di sessione, drift cardiaco):\n"
                 + json.dumps(stream_stats, ensure_ascii=False, indent=2))
    else:
        body += "\n\nNessuno stream disponibile (solo summary)."
    return await _call_claude(SESSION_SYSTEM_PROMPT, body)


async def summarize_period(period_label: str, workouts: list[dict]) -> str:
    body = (f"Periodo: {period_label}\n"
            f"Numero sessioni: {len(workouts)}\n\n"
            + json.dumps(workouts, ensure_ascii=False, indent=1, default=str))
    return await _call_claude(PERIOD_SYSTEM_PROMPT, body)


def _activity_log(workouts: list[dict] | None) -> list[dict]:
    """Compact per-activity view for correlating training load with recovery."""
    out = []
    for w in workouts or []:
        d = w.get("start_date")
        date = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        row = {"data": date, "sport": w.get("sport") or "?",
               "durata_min": round((w.get("moving_s") or 0) / 60)}
        if w.get("distance_m"):
            row["distanza_km"] = round(w["distance_m"] / 1000, 1)
        for src, dst in (("avg_hr", "fc_media"), ("avg_power", "potenza_media"),
                         ("tss", "tss")):
            if w.get(src):
                row[dst] = round(w[src], 1)
        out.append(row)
    out.sort(key=lambda r: r["data"])
    return out


def _health_payload(overview: dict, workouts: list[dict] | None,
                    nutrition: dict | None = None) -> dict:
    """Compact view of the health window (latest + trend + min/avg/max + sleep in
    hours + activity log), shared by the summary and the chat assistant."""
    def stats(series: list) -> dict:
        vals = [p["value"] for p in series]
        if not vals:
            return {}
        return {"min": min(vals), "media": round(sum(vals) / len(vals), 1), "max": max(vals)}

    def hm(mins: float) -> str:
        m = int(round(mins))
        return f"{m // 60}h{m % 60:02d}"

    metrics = {}
    for m in overview.get("metrics", {}).values():
        metrics[m["label"]] = {"unita": m["unit"], "ultimo": m["latest"],
                               "variazione_vs_media7gg": m.get("delta"),
                               "direzione": m.get("dir"), **stats(m["series"])}
    body = {m["label"]: {"unita": m["unit"], "ultimo": m["latest"]}
            for m in overview.get("body", {}).values()}

    nights = overview.get("sleep") or []
    sleep = None
    if nights:
        asleep = [n["asleep_min"] for n in nights]
        sleep = {"notti_disponibili": len(nights),
                 "media_durata": hm(sum(asleep) / len(asleep)),
                 "per_notte": [{"data": n["date"], "durata": hm(n["asleep_min"]),
                                "efficienza": n.get("efficiency")} for n in nights]}

    out = {"indice_di_forma": overview.get("score"),
           "metriche_vitali": metrics, "composizione_corporea": body,
           "sonno": sleep, "attivita_fisiche": _activity_log(workouts)}
    if nutrition:
        out["alimentazione"] = nutrition
    return out


async def summarize_health(overview: dict, workouts: list[dict] | None = None,
                           nutrition: dict | None = None) -> str:
    """Coach-style commentary on the health overview, correlated with activities
    (and nutrition adherence when available)."""
    payload = _health_payload(overview, workouts, nutrition)
    return await _call_claude(
        HEALTH_SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False, indent=1, default=str))


CHAT_SYSTEM_PROMPT = """\
Sei l'assistente di salute e allenamento di questo atleta. Rispondi in italiano,
in modo conciso e concreto, USANDO i dati del periodo forniti qui sotto
(indice di forma, metriche vitali con trend, sonno in ore, attività con carico,
ed eventuale alimentazione: aderenza al piano e pasti liberi).
Correla salute, allenamento e alimentazione quando utile. Se la domanda esce dai dati
disponibili, dillo con onestà. Niente diagnosi mediche: per sintomi o valori
anomali persistenti, suggerisci cautela o un controllo medico. Durate del sonno
sempre in ore e minuti (es. "6h30")."""


async def chat_health(overview: dict, workouts: list[dict] | None,
                      history: list[dict], nutrition: dict | None = None) -> str:
    """Answer a follow-up question grounded in the health-window data."""
    payload = _health_payload(overview, workouts, nutrition)
    system = (CHAT_SYSTEM_PROMPT + "\n\nDATI DEL PERIODO (JSON):\n"
              + json.dumps(payload, ensure_ascii=False, default=str))
    return await _call_messages(system, history)


async def list_openai_models() -> list[str]:
    """Chat-capable OpenAI model ids the key can use, for the Settings dropdown."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"authorization": f"Bearer {settings.openai_api_key}"})
        if resp.status_code != 200:
            return []
    except httpx.HTTPError:
        return []
    ids = [m["id"] for m in resp.json().get("data", [])]
    keep = [i for i in ids if i.startswith(("gpt-5", "gpt-4", "o1", "o3", "o4"))
            and not any(x in i for x in ("audio", "transcribe", "tts", "search",
                                         "image", "realtime", "moderation", "embedding"))]
    return sorted(keep)
