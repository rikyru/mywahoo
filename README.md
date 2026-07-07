# OpenFit — la tua dashboard fitness & salute, self-hosted

OpenFit raccoglie i tuoi allenamenti e i tuoi dati di salute da più sorgenti, li
mostra in una dashboard con KPI, grafici e mappe, e li analizza con l'AI
(OpenAI o Anthropic) — inclusa una **chat** per fare domande sui tuoi dati.
Gira interamente in Docker sul **tuo** server: **i dati restano tuoi**.

> Il nome, il brand e il login sono configurabili via `.env` — clona e fai tuo.

## Cosa fa

- **Allenamenti da Wahoo** via webhook + sync: scarica e parsa i file **FIT**
  (potenza/FC/cadenza/velocità/quota/GPS), con KPI, grafici per-record e mappa.
- **Arricchimento da Google Health** (ex Fitbit): completa gli allenamenti che
  arrivano "scarni" (nuoto/corsa/camminate sincronizzati da terzi) con distanza,
  FC, dislivello e **stream FC intraday**; importa le attività che a Wahoo
  mancano, con **dedup/fusione** automatica.
- **Carica FIT**: importa a mano un FIT completo (es. una nuotata da swim.com),
  agganciato per orario all'attività esistente (che diventa la fonte autorevole).
- **Dislivello stimato** dal GPS (OpenTopoData/SRTM) quando il FIT non ha la quota.
- **Pagina Salute**: FC a riposo, HRV, SpO2, freq. respiratoria, sonno (in ore),
  peso/massa grassa, un **indice di forma** calcolato e un **commento AI** che
  correla recupero e carico — tutto a **finestra mobile** (1 sett / 2 sett / mese
  / personalizzata).
- **Analisi AI degli allenamenti** sulla dashboard, per finestra.
- **Chat assistente** ancorata ai tuoi dati del periodo.
- **Impostazioni**: scegli provider e modello AI dall'interfaccia.

**Stack:** Python 3.12 · FastAPI · SQLModel/SQLite · httpx · fitdecode ·
Jinja2 + Chart.js + Leaflet (CDN) · Docker.

## Avvio rapido

```bash
cp .env.example .env      # compila i valori (vedi sotto)
docker compose up -d --build
```

Apri `http://localhost:8080` (o l'URL pubblico dietro il tuo reverse proxy) e
accedi. I dati persistono nel volume `openfit_data` (`/data/app.db` + `/data/fits/`).

## Configurazione (`.env`)

| Variabile | Descrizione |
|---|---|
| `APP_NAME` | Nome mostrato nell'interfaccia (default `OpenFit`) |
| `APP_PASSWORD` | Password di accesso all'app (login rapido). Vuota = solo login Wahoo |
| `APP_SECRET_KEY` | Firma del cookie di sessione — `openssl rand -hex 32` |
| `APP_BASE_URL` | URL pubblico; se `https://…` il cookie è `Secure` |
| `HOST_PORT` | Porta host pubblicata dal compose |
| `AI_PROVIDER` | `openai` o `anthropic` (o scegli dalle Impostazioni) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | Se usi OpenAI (es. `gpt-5.5`) |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Se usi Anthropic |
| `WAHOO_CLIENT_ID` / `WAHOO_CLIENT_SECRET` / `WAHOO_REDIRECT_URI` / `WAHOO_WEBHOOK_TOKEN` | Integrazione Wahoo (opzionale) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Integrazione Google Health (opzionale) |
| `TZ` / `LOG_LEVEL` | `Europe/Rome` / `INFO` |

Le chiavi API restano **solo lato server**, mai esposte al browser.

## Accesso

Due modi, indipendenti:
- **Password app** (`APP_PASSWORD`): login rapido con un form — comodo da telefono.
- **Accedi con Wahoo**: l'OAuth Wahoo vale anche da login.

Wahoo e Google sono **sorgenti dati**, non il cancello d'accesso: puoi entrare
con la sola password e collegarle dopo.

## Sorgenti dati (tutte opzionali)

### Wahoo Cloud API
1. Crea un'app su <https://developers.wahooligan.com> (richiede approvazione).
2. Redirect URI: `{APP_BASE_URL}/oauth/callback` · Webhook URL:
   `{APP_BASE_URL}/webhook/wahoo` · Webhook token: `openssl rand -hex 24`.
3. Scope: `user_read workouts_read offline_data`.
4. Dopo il login, gli allenamenti **nuovi** arrivano dal webhook; per lo storico
   premi **Sync**.

### Google Health API (ex Fitbit)
Progetto su Google Cloud con Health API abilitata, OAuth client "Web", redirect
`{APP_BASE_URL}/oauth/google/callback`, scope di sola lettura (attività, sonno,
posizione, metriche). Collega da `/login/google`. In modalità "Test" il refresh
token scade ogni 7 giorni → basta rifare il login.

### Carica FIT (swim.com, Garmin, …)
Esporta il FIT della singola attività dal sito della sorgente e caricalo con
**Carica FIT** in dashboard. Utile quando la sorgente non ha un'API.

### AI
Metti la key di OpenAI o Anthropic nel `.env`, poi scegli **provider e modello**
dalle **Impostazioni** (per OpenAI la lista è letta dalla tua chiave).

## Come si fondono i dati

Dopo ogni sync/webhook gira un arricchimento **idempotente**: riconcilia gli
stub Wahoo senza dati con l'esercizio Google che si sovrappone nel tempo,
arricchisce campo per campo, importa ciò che manca e **deduplica** quando la
stessa attività è vista da due fonti (Wahoo resta primario per la bici col FIT;
il FIT caricato a mano vince su tutto). Le camminate auto-rilevate non vengono
importate come righe (solo arricchimento).

## Deploy sul proprio server

Vedi [`DEPLOY.md`](DEPLOY.md) e [`docker-compose.prod.yml`](docker-compose.prod.yml)
(reverse proxy / Cloudflare Tunnel).

## Struttura

```
app/
├── main.py              # FastAPI: routing, auth, webhook, dashboard, salute, AI, chat
├── config.py            # Settings da env
├── db.py                # modelli SQLModel + key/value settings
├── wahoo.py             # OAuth Wahoo, client API, download/parse FIT, sync
├── fit.py               # parsing fitdecode, NP, downsampling, dislivello da DEM
├── google_health.py     # OAuth Google, arricchimento/import/dedup, metriche Salute
├── anthropic_client.py  # client OpenAI/Anthropic, prompt coach, chat
├── templates/           # base, login, dashboard, workout, health, settings, …
└── static/style.css     # tema scuro
```

## Endpoint principali

| Metodo | Path | Descrizione |
|---|---|---|
| GET/POST | `/login` | Login (password app) |
| GET | `/login/wahoo` · `/login/google` | Collega le sorgenti |
| GET | `/` | Dashboard — finestra mobile, KPI, grafici, analisi AL |
| POST | `/analyze/period` | Analisi AI degli allenamenti del periodo |
| GET/POST | `/workout/{id}` · `/edit` · `/delete` · `/analyze` | Dettaglio e gestione attività |
| POST | `/upload/fit` | Caricamento manuale di un FIT |
| POST | `/sync` | Sync manuale da Wahoo |
| GET | `/health` · POST `/health/insight` · POST `/health/chat` | Salute: dati, commento AI, chat |
| GET/POST | `/settings` | Provider e modello AI |
| GET | `/duplicates` | Coppie sospette da rivedere |
| POST | `/webhook/wahoo` | Ricezione allenamenti (validata, async) |
| GET | `/healthz` | Healthcheck |

## Note di sicurezza

- Login via password app (confronto constant-time) o OAuth Wahoo; cookie di
  sessione firmato (`HttpOnly`, `SameSite=Lax`, `Secure` se HTTPS).
- Webhook validato con token condiviso (constant-time) → 401 se errato.
- `state` OAuth anti-CSRF su tutti i flussi. Nessun segreto nei log; container non-root.
