# Deploy sul server di casa (fit.rikyru.ovh)

L'app gira già in Docker, è proxy-aware e tiene tutto in un volume. Migrare =
spostare codice + segreti + dati, cambiare dominio, ripuntare gli OAuth.

## 1. Porta il codice sul server

```bash
git clone <repo> openfit        # oppure scp/rsync della cartella
cd openfit
```

## 2. Porta i dati (per non perdere storico, stream FC e analisi AI)

Sul **PC attuale** esporta il volume:

```bash
docker run --rm -v openfit_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/openfit_data.tgz -C /data .
```

Copia `openfit_data.tgz` sul server e ripristina:

```bash
docker volume create openfit_data
docker run --rm -v openfit_data:/data -v "$PWD":/backup alpine \
  tar xzf /backup/openfit_data.tgz -C /data
```

> In alternativa parti pulito: **Sync** ri-scarica i workout da Wahoo e Google
> li ri-arricchisce; perderesti solo le analisi AI in cache (si rigenerano).

## 3. `.env` per il dominio pubblico

Copia il `.env` (contiene i segreti) e cambia **solo** queste righe:

```ini
APP_BASE_URL=https://fit.rikyru.ovh
WAHOO_REDIRECT_URI=https://fit.rikyru.ovh/oauth/callback
```

`APP_BASE_URL` https => il cookie di sessione diventa automaticamente `Secure`.
Webhook token, client id/secret e `OPENAI_API_KEY` restano identici.

## 4. Avvio + instradamento (Cloudflare Tunnel)

Sul macmini il tuo cloudflared instrada verso `http://192.168.1.124:<porta>`
(come tutti gli altri servizi), quindi openfit pubblica la porta host **8090**
(libera). Avvia:

```bash
cd /home/rikyru/mywahoo
docker compose -f docker-compose.prod.yml up -d --build
curl -s http://127.0.0.1:8090/healthz      # {"status":"ok"}
```

Aggiungi la rotta in `/home/rikyru/cloudflared/config.yml`, prima della riga
finale `- service: http_status:404`:

```yaml
  - hostname: fit.rikyru.ovh
    service: http://192.168.1.124:8090
```

Crea il DNS del tunnel e ricarica cloudflared:

```bash
docker exec cloudflared cloudflared tunnel route dns \
  14fd9e38-0c94-4400-b1eb-fb5f4e2e95ab fit.rikyru.ovh
docker restart cloudflared
```

> In alternativa, per coerenza col tuo setup, puoi aggiungere il servizio
> `mywahoo` direttamente a `/home/rikyru/docker/docker-compose.yaml`
> (`build: ../mywahoo`, `ports: ["8090:8080"]`, volume `openfit_data:/data`).

## 5. Ripunta gli OAuth sul nuovo dominio

- **Wahoo** (developers.wahooligan.com): Redirect URI
  `https://fit.rikyru.ovh/oauth/callback`, Webhook URL
  `https://fit.rikyru.ovh/webhook/wahoo` (token invariato).
- **Google Cloud Console** → client OAuth: aggiungi redirect URI
  `https://fit.rikyru.ovh/oauth/google/callback`.

## 6. Verifica

```bash
curl -s https://fit.rikyru.ovh/healthz      # {"status":"ok"}
```

Apri `https://fit.rikyru.ovh`, **Accedi con Wahoo**, poi **Collega Google
Health** (i token sono legati al dominio: vanno rifatti i login una volta).

## Note
- ngrok non serve più: il tunnel di casa sostituisce `espresso-limes-quilt`.
- Backup: salva periodicamente il volume `openfit_data` (comando step 2).
- Token Google in modalità test = re-login ogni 7 giorni, invariato.
