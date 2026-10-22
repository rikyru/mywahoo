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

## Diario del test
- Settimana 1 (22/10 → 01/11):
- Settimana 2 (02/11 → 08/11):

## Motore rivisto il 22/10, prima dell'inizio del test
Quasi tutte le attività hanno FC misurata (Pixel Watch a casa/nuoto/camminate,
fascia in bici), quindi:
- **Taratura sullo storico**: il carico "tipo" di uno sport è il TRIMP/min mediano
  misurato (ultimi 90 gg, sedute ≥25′). Valori reali: corpo libero 0,49, nuoto 0,92,
  bici 1,12, camminata 0,32 (la formula generica dava ~1,7 al corpo libero).
- **Target = la settimana tipo stessa** (pianificate tarate × moltiplicatore + nuoto
  previsto), non più CTL×7: d'inverno, senza bici, il CTL estivo era irraggiungibile.
- **Asimmetrico**: se hai fatto di più (nuoto pesante, escursione) le sedute si
  alleggeriscono fino al −30%; se hai fatto di meno restano come da piano (niente
  recupero dei carichi persi). Contano anche le attività che realizzano uno slot
  (stesso giorno/sport) e gli extra ≥25′; i tragitti brevi sono attività di fondo.
- **Import automatico da Google ogni 2 ore** (+ notte): la seduta registrata col Pixel
  si spunta da sola e il budget si aggiorna senza premere Sync.

## Dubbi aperti (dal confronto sull'overengineering)
- **Il motore era più preciso dei dati** → in gran parte risolto dalla revisione sopra.
  Resta da verificare nel test se l'RPE target serve; se no → regole leggibili
  (scarico ogni 4ª settimana, alleggerire con recupero basso, salta = sposta o togli).
- ~~Troppe superfici~~ **fatto il 22/10 (72da0ce)**: Programma unica pagina
  dell'allenamento (Piani in archivio), **un solo coach** (dashboard, Salute,
  Programma) che propone azioni sul programma da confermare con "Applica";
  il box Proposte resta per le modifiche decise dal motore su oggi/domani.
  Da osservare nel test: il coach sceglie le azioni giuste? (es. "niente bici
  questo weekend" → salta + assenza sab-dom, così non la sposta a domenica).
- **M3 (punteggi/pagella)** solo se dopo il test ne sento davvero la mancanza.

## Appunti da fare dopo il test (piccoli)
- ~~Bici "sab o dom": sposta a domenica / salta la settimana~~ **fatto il 22/10**: "Non
  la faccio" con motivo (riposo, stanco, fuori casa, dolore, altro) + nota, e per gli
  slot sab-o-dom la scelta sposta/salta la settimana; il coach vede i motivi.
  Il motivo per ora non cambia il calcolo ("stanco" → alleggerire è roba da M2).
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
  Notifiche HA rimandate a dopo il test (non falsare "lo apro da solo?"). Design
  deciso: HA interroga un endpoint JSON di mywahoo (seduta di oggi, proposte in
  attesa, credito basso, promemoria) e le automazioni stanno in HA. Se durante il
  test mi dimentico di aprire il Programma → anticiparle (~30 righe).

Costo AI misurato (GPT-5.5, $5 in / $30 out per M token): ~2-5 cent/giorno.
