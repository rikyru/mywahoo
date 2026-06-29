# MyWahoo — Personal Wahoo Analytics + AI

Webapp single-user che riceve gli allenamenti dalla **Wahoo Cloud API** via
webhook, scarica e parsa i file **FIT** (potenza/FC/cadenza/velocità/quota/GPS),
li mostra in una dashboard con KPI, grafici e mappa del percorso, e analizza le
sessioni con l'AI (**OpenAI** o **Anthropic**, configurabile). Gira interamente
in Docker.

In più si integra con la **Google Health API** (ex Fitbit) per:
- **arricchire** gli allenamenti che arrivano a Wahoo senza dati (nuoto, corsa,
  camminate sincronizzati da app di terze parti) con distanza, FC, calorie;
- **importare** le attività che a Wahoo mancano del tutto, con **dedup/fusione**
  automatica quando lo stesso allenamento è visto da entrambe le fonti;
- ricostruire lo **stream FC intraday** (grafico FC-nel-tempo) anche senza FIT;
- una pagina **Salute** con metriche vitali (FC a riposo, HRV, SpO2, frequenza
  respiratoria, sonno, peso), un **indice di forma** calcolato e un **commento
  AI** del periodo.

Il dislivello dei giri registrati col telefono (FIT senza quota) viene **stimato
dal GPS** contro un modello digitale del terreno (OpenTopoData / SRTM).

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
| `AI_PROVIDER` | `openai` (default `anthropic`) — sceglie il motore delle analisi |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | Key OpenAI + modello (es. `gpt-5.5`) se `AI_PROVIDER=openai` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Key Anthropic + modello se `AI_PROVIDER=anthropic` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth Google Health (opzionale) — abilita la pagina Salute e l'arricchimento |
| `APP_SECRET_KEY` | Firma del cookie di sessione — `openssl rand -hex 32` |
| `APP_BASE_URL` | URL pubblico; se `https://…` il cookie è marcato `Secure` |
| `HOST_PORT` | Porta host pubblicata dal compose (default `8080`) |
| `LOG_LEVEL` / `TZ` | `INFO` / `Europe/Rome` |

La chiave AI è usata **solo lato server**, mai esposta al browser.

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

## 4b. Integrazione Google Health (ex Fitbit)

Opzionale: imposta `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (progetto su Google
Cloud con Health API abilitata, redirect `{APP_BASE_URL}/oauth/google/callback`)
e collega l'account da **`/login/google`**. Gli scope richiesti coprono attività,
sonno, posizione e metriche vitali (sola lettura).

> App in modalità "Test" su Google Cloud ⇒ il refresh token scade ogni 7 giorni:
> basta rifare il login da `/login/google`.

Dopo ogni Sync (e dopo ogni webhook) gira `enrich_workouts`, in tre fasi
**idempotenti**:

1. **Dedup** — se un allenamento importato da Google ha poi un gemello su Wahoo
   (stesso orario ±15 min **e stesso sport**), la copia Google viene rimossa.
2. **Arricchimento** — gli allenamenti Wahoo senza FIT (nuoto/corsa/camminate
   sincronizzati da terzi) vengono completati campo per campo dall'esercizio
   Google corrispondente, e ne viene ricostruito lo **stream FC intraday**.
3. **Import** — le attività presenti solo su Google vengono importate come nuove
   righe, dopo un *grace period* di 12 h (così una consegna tardiva di Wahoo ha
   la precedenza) e solo se non c'è già un'attività **dello stesso sport** vicina.

Risultato: lo stesso allenamento visto da entrambe le fonti resta **una sola
riga, fusa** (Wahoo resta primario per la bici con FIT); due attività diverse a
orari ravvicinati restano separate.

La pagina **Salute** (`/health`) mostra FC a riposo, HRV, SpO2, frequenza
respiratoria, temperatura cutanea, peso/massa grassa e sonno (fasi per notte),
con frecce di tendenza, un **indice di forma** 0-100 calcolato sulla baseline
personale e un **commento AI** del periodo (in cache giornaliera). I punteggi
proprietari di Fitbit (Sleep Score, Readiness…) non sono esposti dall'API.

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
├── main.py              # FastAPI: routing, auth guard, webhook, dashboard, Salute, AI
├── config.py            # Settings da env (incl. AI_PROVIDER, Google) + logging
├── db.py                # WahooToken, GoogleToken, Workout, WorkoutStream, AiAnalysis, PeriodSummary
├── wahoo.py             # OAuth + refresh, client API con backoff, download FIT, sync
├── fit.py               # Parsing fitdecode, NP, downsampling, stats AI, dislivello da DEM
├── google_health.py     # OAuth Google, arricchimento/import/dedup, stream FC, metriche Salute
├── anthropic_client.py  # Client OpenAI/Anthropic (httpx), prompt coach allenamento e salute
├── templates/           # base, login, dashboard, workout, summary, health
└── static/style.css     # tema scuro (Inter + JetBrains Mono)
```

Per il **deploy sul proprio server** (reverse proxy / Cloudflare Tunnel) vedi
[`DEPLOY.md`](DEPLOY.md) e [`docker-compose.prod.yml`](docker-compose.prod.yml).

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
| GET | `/` | Dashboard — finestra mobile `win` (7/14/30/custom) + `end`/`from`/`to`, `sport`, `sort`, `order` |
| GET/POST | `/workout/{id}/edit` | Modifica manuale dei campi di un'attività |
| POST | `/workout/{id}/delete` | Elimina un'attività (gli import Google cancellati non rientrano) |
| GET | `/duplicates` | Coppie di attività ravvicinate (candidate doppioni) da rivedere |
| GET | `/login` · `/login/wahoo` | Login e redirect all'authorize Wahoo |
| GET | `/oauth/callback` | Scambio code→token, validazione `state` |
| POST | `/webhook/wahoo` | Ricezione `workout_summary` (validata, async) |
| POST | `/sync` | Sync manuale (campo `full=1` per resync completo) |
| GET | `/workout/{id}` | Dettaglio: KPI, grafici per-record, mappa, analisi AI |
| POST | `/workout/{id}/analyze` | Genera/rigenera analisi AI |
| GET/POST | `/summary/{week\|month}` | Sintesi AI del periodo |
| GET | `/health` | Pagina Salute: metriche vitali, indice di forma, commento AI |
| POST | `/health/insight` | Genera/rigenera il commento AI sulla salute |
| GET | `/login/google` · `/oauth/google/callback` | OAuth Google Health |
| GET | `/google/probe` | Diagnostica: dump JSON degli esercizi Google |
| GET | `/healthz` | Healthcheck (usato dal compose) |

## Punti marcati TODO nel codice

Dove la doc Wahoo serve per i nomi esatti, il codice è tollerante e marcato
`# TODO: verificare su cloud-api.wahooligan.com`:

- elenco esatto degli **scope** OAuth (`app/wahoo.py::SCOPES`)
- nomi dei campi del **workout_summary** (`workout_from_payload`)
- struttura del **payload webhook** e meccanismo del token (`main.py::webhook_wahoo`)
- tabella completa dei `workout_type_id` (`WORKOUT_TYPES`)
- path dell'endpoint summary (`fetch_workout_summary`)
