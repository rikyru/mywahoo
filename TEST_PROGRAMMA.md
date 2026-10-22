# Programma adattivo — periodo di test

**Stato: IN TEST dal 22/10/2026 al ~05/11/2026.** Niente nuove feature sul programma
finché non finisce la prova; poi si decide cosa semplificare e cosa costruire.

## Cosa c'è (M1, commit 92efea2)
Programma con settimana tipo (slot pianificati/esterni, "sab o dom", intensità,
routine fisse come il core del PT), blocchi da 4 settimane con scarico, assenze.
Solo i prossimi 10 giorni diventano sedute; ricalcolo notturno e a ogni evento,
budget di carico (CTL×7×moltiplicatore − fatto) → RPE target, proposte per
oggi/domani, chat che traduce in azioni. Codice: `app/program.py` (motore puro),
`app/program_runner.py`, route `/program` in `app/main.py`.

Setup del test: bici disattivata finché non arrivano i rulli (forse 31/10).
All'arrivo: slot bici attivo + indoor, "rulli smart" in attrezzatura, prima
seduta trasformata in test FTP (l'FTP attuale, 145 W, è stimato dal GPS).

## Cosa annotare (una riga a settimana)
1. Ho seguito le sedute proposte o le ho cambiate spesso?
2. Le proposte di modifica le accettavo o le rifiutavo?
3. L'RPE target mi diceva qualcosa, o guardavo solo il contenuto?
4. Cose fastidiose: sedute assurde, motivi incomprensibili, cose fatte a mano.

## Note:

## Dubbi aperti (dal confronto sull'overengineering)
- **Il motore è più preciso dei dati.** TRIMP di sedute a casa da RPE stimato, nuoto da
  media storica, CTL da FC media: sui dati reali schiaccia tutto al minimo (la
  settimana tipo vale ~1,4× il carico sostenuto). Se l'RPE target non serve →
  sostituire il budget con regole leggibili (scarico ogni 4ª settimana,
  alleggerire con recupero basso, salta = sposta o togli).
- **Troppe superfici** (Piani, Programma, chat coach/programma/seduta). Proposta:
  Programma unica pagina dell'allenamento (Piani classici in archivio), **un solo
  coach** con le azioni del programma come strumenti, ogni modifica nel box
  Proposte (Accetta / Tieni com'era). Da fare dopo il test.
- **M3 (punteggi/pagella)** solo se dopo il test ne sento davvero la mancanza.

## Appunti da fare dopo il test (piccoli)
- Bici "sab o dom": alla rinuncia scegliere **sposta a domenica** / **salta la settimana**
  (oggi "Salta" sposta sempre); la chat deve chiedere, non decidere.
- Attività fuori programma (es. escursione in un giorno libero): già contate nel
  budget, ma da **mostrare** nella pagina Programma col loro carico.
- Promemoria **pesata** (~settimanale) e **misure col metro** (~2-4 settimane), con
  trend nel programma (era previsto in M2 insieme al semaforo recupero).
- Annullare una seduta agganciata in automatico a un'attività reale: al ricalcolo
  si riaggancia (caso limite non gestito).

## Roadmap residua (da rivalutare dopo il test)
- M2: semaforo recupero (HRV/FC riposo/sonno/TSB) nel ricalcolo + card per
  valutare le sedute senza FC + promemoria pesata/misure.
- M3: punteggi training/recupero/alimentazione + pagella settimanale.
- M4: rulli (FTP reale, export workout strutturati) e notifiche via Home Assistant.

Costo AI misurato (GPT-5.5, $5 in / $30 out per M token): ~2-5 cent/giorno.
