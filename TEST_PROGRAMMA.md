# Programma adattivo — test

**Stato: IN TEST dal 22/10/2026**, con tutto il pacchetto attivo (deciso di testare
tutto insieme invece di procedere a tappe). A fine prova: cosa tenere, cosa
semplificare, cosa togliere.

## Cosa c'è
- **Programma** (`/program`): settimana tipo (slot pianificati/esterni, "sab o dom",
  intensità, routine fisse come il core del PT, indoor/rulli), blocchi da 4 settimane
  con scarico, assenze. Solo i prossimi 10 giorni diventano sedute.
- **Motore**: target = la settimana tipo stessa (carichi tarati sul tuo storico FC:
  corpo libero 0,49 TRIMP/min, nuoto 0,92, bici 1,12, camminata 0,32) × moltiplicatore
  del blocco + nuoto previsto. **Asimmetrico**: se hai fatto di più le sedute si
  alleggeriscono fino al −30%, se hai fatto di meno restano come da piano.
- **Semaforo recupero**: prontezza (HRV, FC a riposo, sonno vs tua media), forma (TSB),
  "Non la faccio: stanco/dolore" → giallo ×0,85 / rosso ×0,7 su oggi e domani. Se Google
  non risponde usa l'ultima prontezza delle 12 ore.
- **Proposte** per oggi/domani (Accetta / Tieni com'era); "Non la faccio" con motivo e,
  per la bici sab-o-dom, sposta o salta la settimana.
- **Un solo coach** (dashboard, Salute, Programma) che propone azioni sul programma da
  confermare con "Applica".
- **Import automatico** da Google ogni 2 ore (+ notte): le sedute registrate col Pixel si
  spuntano da sole.
- **Attività fuori programma** visibili nel Programma (≥25′ contano nel carico).
- **Promemoria** pesata (7 gg) e misure col metro (21 gg), con trend peso e girovita.
- **Card "Com'è andata?"** in dashboard per le rare attività senza FC (RPE + note).
- **Pagella settimanale**: voti allenamento / recupero / alimentazione + commento AI,
  generata il lunedì notte (o a mano).
- **Rulli**: sedute a intervalli in % FTP con watt, export **.zwo** e **.mrc**; l'FTP passa
  da "stimato dal GPS" a "potenza reale" appena ci sono uscite con potenza misurata.
- **Home Assistant**: `GET /api/ha/status?token=<BODY_WEBHOOK_TOKEN>` → seduta di oggi,
  proposte in attesa, semaforo, promemoria, credito AI (`summary` pronto per una notifica).
- **Credito OpenAI** stimato in Impostazioni, con avviso in dashboard.

Setup: bici disattivata finché non arrivano i rulli (forse 31/10). All'arrivo: slot bici
attivo + indoor, "rulli smart" in attrezzatura, prima seduta → test FTP.

## Cosa annotare (una riga a settimana)
1. Ho seguito le sedute proposte o le ho cambiate spesso?
2. Proposte (motore e coach): le accettavo o le rifiutavo?
3. L'RPE target mi diceva qualcosa, o guardavo solo il contenuto?
4. Il semaforo era credibile (giallo/rosso quando mi sentivo davvero stanco)?
5. La pagella del lunedì mi è servita o l'ho ignorata?
6. Cose fastidiose: sedute assurde, motivi incomprensibili, cose fatte a mano.

## Diario del test
- Settimana 1 (22/10 → 01/11):
- Settimana 2 (02/11 → 08/11):

## Dubbi aperti
- Se l'RPE target non serve → regole leggibili al posto del budget.
- Pagella: "alimentazione" conta i giorni entro ±150 kcal dall'obiettivo; con i pasti
  non tutti tracciati può risultare severa.
- Caso noto: annullare una seduta agganciata in automatico ora la scollega per sempre
  da quell'attività (corretto), ma la seduta resta "da fare".

## Costi AI
GPT-5.5 ($5 in / $30 out per M token): ~2-5 cent/giorno per il programma + coach +
pagella settimanale (~1 cent). Credito stimato in Impostazioni.
