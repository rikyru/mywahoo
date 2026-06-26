# MyWahoo — Personal Wahoo Analytics + AI

Webapp single-user che riceve gli allenamenti dalla **Wahoo Cloud API** via
webhook, scarica e parsa i file **FIT** (potenza/FC/cadenza/velocità/quota/GPS),
li mostra in una dashboard con KPI, grafici e mappa del percorso, e analizza le
sessioni con l'API di Anthropic (Claude). Gira interamente in Docker.

**Stack:** Python 3.12 · FastAPI · SQLModel/SQLite · httpx · fitdecode ·
Jinja2 + Chart.js + Leaflet (CDN) · Docker

---

## 1. Accesso alla Wahoo Cloud API

1. Registrati su <https://developers.wahooligan.com> e crea una nuova applicazione.
2. Le app Wahoo devono essere **approvate** prima di poter accedere ai dati.
   Nella richiesta di approvazione descrivi l'uso: *"Single-user personal
   analytics dashboard: receives workout_summary webhooks, downloads FIT files
   of my own workouts, read-only"*.
3. **Scope** da richiedere (lettura profilo + workout + file, con refresh token):
   `user_read workouts_read offline_data`
   *(verifica l'elenco esatto sulla doc live <https://cloud-api.wahooligan.com> —
   nel codice gli scope sono in `app/wahoo.py`, costante `SCOPES`)*.
4. Configura sul portale:
   - **Redirect URI**: `https://wahoo.miodominio.tld/oauth/callback`
   - **Webhook URL**: `https://wahoo.miodominio.tld/webhook/wahoo`
   - **Webhook token**: genera un segreto (`openssl rand -hex 24`) e mettilo
     sia sul portale sia in `.env` come `WAHOO_WEBHOOK_TOKEN`.

## 2. Compilare `.env`

```bash
cp .env.example .env
```

| Variabile | Descrizione |
|---|---|
| `WAHOO_CLIENT_ID` / `WAHOO_CLIENT_SECRET` | Dall'app approvata sul Developer Portal |
| `WAHOO_REDIRECT_URI` | Identico a quello registrato sul portale |
| `WAHOO_WEBHOOK_TOKEN` | Segreto condiviso per validare i webhook in ingresso |
| `ANTHROPIC_API_KEY` | API key Anthropic (solo lato server, mai esposta al browser) |
| `APP_SECRET_KEY` | Firma del cookie di sessione — `openssl rand -hex 32` |
| `APP_BASE_URL` | URL pubblico; se `https://…` il cookie è marcato `Secure` |
| `LOG_LEVEL` / `TZ` | `INFO` / `Europe/Rome` |

## 3. Avvio, primo login, webhook, primo sync

```bash
docker compose up -d --build
```

1. Apri `https://wahoo.miodominio.tld` → **Accedi con Wahoo** → autorizza.
2. Gli allenamenti **nuovi** arrivano da soli: Wahoo invia un webhook
   `workout_summary` quando l'attività viene caricata dal device.
3. Per lo **storico** (o se un webhook è andato perso) premi **Sync**: elenca i
   workout via API e ingerisce quelli mancanti. **Full resync** ricontrolla
   tutte le pagine.

I dati persistono nel volume `mywahoo_data` (`/data/wahoo.db` + `/data/fits/`).

## 4. Flusso webhook → FIT → parsing → AI

```
Wahoo device → Wahoo Cloud ── POST /webhook/wahoo (workout_summary + token)
                                   │  valida WAHOO_WEBHOOK_TOKEN, risponde 200 subito
                                   ▼  BackgroundTasks
                       upsert workout in SQLite (idempotente su id)
                                   ▼
                       download FIT → /data/fits/{id}.fit
                                   ▼
              parsing fitdecode: session → summary, record → stream
              (stream gzip in DB; NP calcolata se assente nel FIT)
                                   ▼
        dashboard / dettaglio (grafici per-record + mappa Leaflet)
```

**Analisi AI:** nella pagina di dettaglio, "Analizza con AI" invia a Claude
(`claude-sonnet-4-6`, endpoint `/v1/messages`) il summary più **statistiche
aggregate** degli stream (medie per decimo di sessione, drift cardiaco) — mai
gli array grezzi, per contenere i token. Il risultato è **cached** in DB per
`workout_id`: riaprire la pagina non costa nulla; **Rigenera** forza una nuova
chiamata. Errori API (timeout/429/5xx) → retry con backoff, poi messaggio
chiaro in un banner. Stessa cache per le **sintesi periodiche** (settimana/mese).

**Rate limit / robustezza:** tutte le chiamate API Wahoo hanno backoff
esponenziale su 429/5xx (10s → 20s → 40s → 80s, max 4 tentativi). Il token
OAuth viene rinnovato in automatico quando mancano <10 minuti alla scadenza
(il refresh token ruotato viene sempre ripersistito).

## 5. Reverse proxy HTTPS con Caddy

L'app espone HTTP su `127.0.0.1:8080` (configurabile con `HOST_PORT`) ed è
proxy-aware (`--proxy-headers`). La UI **e** l'endpoint webhook devono essere
raggiungibili pubblicamente:

```caddyfile
wahoo.miodominio.tld {
    reverse_proxy 127.0.0.1:8080
}
```

Caddy gestisce il certificato TLS e inoltra gli header `X-Forwarded-*`.
Se Caddy gira in un container, usa `mywahoo:8080` sulla stessa network Docker.

---

## Struttura del progetto

```
app/
├── main.py              # FastAPI: routing, auth guard, webhook, dashboard, AI
├── config.py            # Settings da env + logging
├── db.py                # WahooToken, Workout, WorkoutStream (gzip), AiAnalysis, PeriodSummary
├── wahoo.py             # OAuth + refresh, client API con backoff, download FIT, sync
├── fit.py               # Parsing fitdecode, NP, downsampling grafici, stats per AI
├── anthropic_client.py  # POST /v1/messages (httpx), prompt coach, retry
├── templates/           # base, login, dashboard, workout (grafici+mappa), summary
└── static/style.css
```

## Note di sicurezza

- Webhook: richieste senza `WAHOO_WEBHOOK_TOKEN` valido → **401** (confronto
  constant-time).
- Cookie di sessione firmato (`APP_SECRET_KEY`): `HttpOnly`, `SameSite=Lax`,
  `Secure` quando `APP_BASE_URL` è HTTPS.
- `state` OAuth generato per sessione e validato nel callback (anti-CSRF).
- Nessun segreto nei log; container non-root.

## Endpoint

| Metodo | Path | Descrizione |
|---|---|---|
| GET | `/` | Dashboard — filtri `period`, `sport`, `sort`, `order` |
| GET | `/login` · `/login/wahoo` | Login e redirect all'authorize Wahoo |
| GET | `/oauth/callback` | Scambio code→token, validazione `state` |
| POST | `/webhook/wahoo` | Ricezione `workout_summary` (validata, async) |
| POST | `/sync` | Sync manuale (campo `full=1` per resync completo) |
| GET | `/workout/{id}` | Dettaglio: KPI, grafici per-record, mappa, analisi AI |
| POST | `/workout/{id}/analyze` | Genera/rigenera analisi Claude |
| GET/POST | `/summary/{week\|month}` | Sintesi AI del periodo |
| GET | `/healthz` | Healthcheck (usato dal compose) |

## Punti marcati TODO nel codice

Dove la doc Wahoo serve per i nomi esatti, il codice è tollerante e marcato
`# TODO: verificare su cloud-api.wahooligan.com`:

- elenco esatto degli **scope** OAuth (`app/wahoo.py::SCOPES`)
- nomi dei campi del **workout_summary** (`workout_from_payload`)
- struttura del **payload webhook** e meccanismo del token (`main.py::webhook_wahoo`)
- tabella completa dei `workout_type_id` (`WORKOUT_TYPES`)
- path dell'endpoint summary (`fetch_workout_summary`)
