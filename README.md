# OpenFit — la tua dashboard fitness & salute, self-hosted

OpenFit raccoglie allenamenti e dati di salute da più sorgenti, li analizza con
l'AI (OpenAI o Anthropic) e gestisce un **programma di allenamento adattivo** che
si ricalibra su quello che fai davvero, integrato con l'alimentazione
([planmydinner](https://github.com/rikyru/planmydinner)). Gira interamente in
Docker sul **tuo** server: **i dati restano tuoi**.

> Il nome, il brand e il login sono configurabili via `.env` — clona e fai tuo.

## Cosa fa

### Dati
- **Wahoo** via webhook + sync: file **FIT** (potenza/FC/cadenza/velocità/quota/GPS).
- **Google Health** (ex Fitbit): arricchisce gli allenamenti "scarni" (nuoto, corsa,
  camminate, sedute a casa dallo smartwatch), importa quelli che mancano, con
  **dedup/fusione** automatica. Importazione automatica **ogni 2 ore**.
- **Carica FIT** a mano (es. nuoto da swim.com, con **parziali per vasca**, SWOLF).
- **Bilancia smart** via webhook (Home Assistant) e **misure col metro**.
- **Dislivello stimato** dal GPS (DEM SRTM) quando il FIT non ha la quota.

### Allenamento
- **Programma adattivo** (`/program`): settimana tipo configurabile (sedute
  pianificate o **esterne** come un corso col coach, alternative "sab o dom",
  intensità del giorno, **routine fisse** con video), blocchi di 4 settimane con
  scarico, assenze. Solo i prossimi 10 giorni diventano sedute concrete:
  - carico **tarato sul tuo storico** (TRIMP/min misurato per sport); il target
    della settimana è la settimana tipo stessa;
  - **asimmetrico**: se hai fatto di più le sedute si alleggeriscono, se hai fatto
    di meno restano come da piano;
  - **semaforo di recupero** (HRV, FC a riposo, sonno, forma) che alleggerisce oggi
    e domani; per oggi/domani le modifiche sono **proposte** da confermare;
  - "Non la faccio" con motivo; sedute registrate dall'orologio agganciate da sole;
  - sedute **rulli** a intervalli in % FTP con export **.zwo / .mrc**;
  - **pagella settimanale** (allenamento / recupero / alimentazione + commento AI).
- **Un solo coach AI** (dashboard, Salute, Programma): risponde sui tuoi dati e
  propone modifiche al programma da confermare con un bottone. Conversazioni salvate.
- **Calendario**, **Esercizi** (glossario con dimostrazioni).

### Analisi
- **Forma** (CTL/ATL/TSB) ed **eFTP** nel tempo (dal GPS o dalla potenza reale).
- **Progressi bici**: stagione contro stagione, salite/segmenti ripetuti, efficienza.
- **Record**, **Salite** (segmenti stile Strava), **Percorsi** (fattibilità di un GPX).
- **Analisi AI** di ogni attività e dei periodi.

### Salute e alimentazione
- **Salute**: FC a riposo, HRV, SpO2, respirazione, sonno, composizione corporea,
  **indice di prontezza**, commento AI, bilancio energetico.
- **planmydinner** (opzionale): target kcal/macro calcolati da bilancia e trend del
  peso, **periodizzati** sugli allenamenti (anche retroattivi), confronto
  **consumato vs obiettivo** per giorno (solo pasti mangiati davvero).
- Promemoria **pesata** e **misure**, card "Com'è andata?" per le attività senza FC.

### Sistema
- **Credito OpenAI** stimato (OpenAI non espone il saldo via API) con avviso.
- **Home Assistant**: `GET /api/ha/status` per sensori e notifiche.

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
| `APP_BASE_URL` | URL pubblico, usato per i redirect OAuth |
| `SESSION_HTTPS_ONLY` | `true` = cookie di sessione solo in HTTPS (default off: login anche in LAN) |
| `APP_TZ` / `TZ` / `LOG_LEVEL` | Fuso dell'app per job e orari (`Europe/Rome`) / del container / `INFO` |
| `HOST_PORT` | Porta host pubblicata dal compose |
| `AI_PROVIDER` | `openai` o `anthropic` (o scegli dalle Impostazioni) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | Se usi OpenAI (es. `gpt-5.5`) |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Se usi Anthropic |
| `WAHOO_CLIENT_ID` / `WAHOO_CLIENT_SECRET` / `WAHOO_REDIRECT_URI` / `WAHOO_WEBHOOK_TOKEN` | Integrazione Wahoo (opzionale) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Integrazione Google Health (opzionale) |
| `BODY_WEBHOOK_TOKEN` | Token condiviso con Home Assistant: webhook bilancia e `/api/ha/status` |
| `PLANMYDINNER_URL` / `PLANMYDINNER_PROFILE` | Integrazione alimentazione (opzionale) |

Le chiavi API restano **solo lato server**, mai esposte al browser.

## Accesso

- **Password app** (`APP_PASSWORD`): login rapido con un form — comodo da telefono.
- **Accedi con Wahoo**: l'OAuth Wahoo vale anche da login.

Wahoo e Google sono **sorgenti dati**, non il cancello d'accesso.

## Sorgenti dati (tutte opzionali)

### Wahoo Cloud API
1. Crea un'app su <https://developers.wahooligan.com> (richiede approvazione).
2. Redirect URI: `{APP_BASE_URL}/oauth/callback` · Webhook URL:
   `{APP_BASE_URL}/webhook/wahoo` · Webhook token: `openssl rand -hex 24`.
3. Scope: `user_read workouts_read offline_data`.
4. Gli allenamenti **nuovi** arrivano dal webhook; per lo storico premi **Sync**.

### Google Health API (ex Fitbit)
Progetto su Google Cloud con Health API abilitata, OAuth client "Web", redirect
`{APP_BASE_URL}/oauth/google/callback`, scope di sola lettura. Collega da
`/login/google`. In modalità "Test" il refresh token scade ogni 7 giorni.

### Bilancia (Home Assistant)
HA invia le pesate a `POST /webhook/body` con il token `BODY_WEBHOOK_TOKEN`; ogni
pesata ricalcola i target nutrizionali.

### Home Assistant (notifiche)
`GET /api/ha/status?token=<BODY_WEBHOOK_TOKEN>` restituisce la seduta di oggi,
proposte in attesa, semaforo di recupero, promemoria pesata/misure, credito AI e
un campo `summary` pronto per una notifica (sensore REST + automazioni in HA).

### planmydinner
Con `PLANMYDINNER_URL` OpenFit legge aderenza e pasti consumati e **invia** i
target kcal/macro (`/integration/apply-targets`), periodizzati sugli allenamenti.

## Job in background

| Quando | Cosa |
|---|---|
| Ogni 2 ore | Import da Google, fusione doppioni, ricalcolo del programma (+ target se cambiano) |
| Ogni notte 03:00 | Import, ricalcolo del programma, invio dei target nutrizionali |
| Lunedì notte | Pagella della settimana appena chiusa |
| A ogni evento | Webhook/sync/FIT, seduta fatta/saltata, azioni del coach → ricalcolo |

## Struttura

```
app/
├── main.py              # FastAPI: routing, auth, webhook, pagine, coach, job
├── config.py / db.py    # settings da env, modelli SQLModel + migrazioni additive
├── wahoo.py / fit.py    # OAuth e sync Wahoo; parsing FIT, NP, parziali nuoto, DEM
├── google_health.py     # OAuth Google, import/dedup, metriche Salute, prontezza
├── anthropic_client.py  # client OpenAI/Anthropic, prompt, coach con strumenti
├── ai_usage.py          # costo stimato delle chiamate AI, credito residuo
├── program.py           # motore del programma (puro: blocchi, budget, semaforo, export)
├── program_runner.py    # programma: DB + AI (ricalcolo, azioni, pagella, promemoria)
├── nutrition.py         # target da bilancia, periodizzazione, integrazione planmydinner
├── form.py / cycling.py # CTL/ATL/TSB e TRIMP; potenza stimata, salite, eFTP
├── progress.py          # progressi bici stagione contro stagione
├── templates/ · static/ # interfaccia (Jinja2, tema scuro)
tests/                   # test del motore (program, progress, periodizzazione)
```

Test: `python -m pytest tests/` (`test_program.py` e `test_progress.py` sono puri,
gli altri richiedono le dipendenze di `requirements.txt`).

## Endpoint principali

| Metodo | Path | Descrizione |
|---|---|---|
| GET/POST | `/login` | Login (password app) |
| GET | `/login/wahoo` · `/login/google` | Collega le sorgenti |
| GET | `/` | Dashboard: KPI, nutrizione e salute di oggi, in programma, coach |
| GET | `/program` · `/program/settings` | Programma adattivo e sue impostazioni |
| POST | `/program/recalc` · `/program/apply` · `/program/report` | Ricalcolo, azioni del coach, pagella |
| GET | `/program/session/{id}/workout.zwo` · `.mrc` | Export seduta rulli |
| POST | `/analyze/chat` · `/health/chat` | Coach (stessa logica) |
| GET | `/health` · `/form` · `/progress` · `/records` · `/segments` · `/routes` | Pagine di analisi |
| GET/POST | `/workout/{id}` · `/edit` · `/delete` · `/analyze` · `/rate` | Attività |
| POST | `/upload/fit` · `/sync` | FIT manuale, sync Wahoo |
| GET/POST | `/settings` · `/settings/credit` | Impostazioni, credito OpenAI |
| POST | `/webhook/wahoo` · `/webhook/body` | Webhook (token, async) |
| GET | `/api/ha/status` | Stato per Home Assistant (token) |
| GET | `/healthz` | Healthcheck |

## Deploy sul proprio server

Vedi [`DEPLOY.md`](DEPLOY.md) e [`docker-compose.prod.yml`](docker-compose.prod.yml).
Aggiornamento: `git pull && docker compose -f docker-compose.prod.yml up -d --build`.

## Note di sicurezza

- Login via password app (confronto constant-time) o OAuth Wahoo; cookie di
  sessione firmato (`HttpOnly`, `SameSite=Lax`, `Secure` con `SESSION_HTTPS_ONLY=true`).
- Webhook ed endpoint HA validati con token condiviso (constant-time) → 401 se errato.
- `state` OAuth anti-CSRF su tutti i flussi. Nessun segreto nei log; container non-root.
- Le azioni proposte dall'AI sul programma vengono applicate solo dopo conferma.
