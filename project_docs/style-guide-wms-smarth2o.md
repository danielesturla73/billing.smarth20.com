# Style guide WMS SmartH2O — riferimento per il frontend di Fatturazione Utenze
Estratto da Daniele direttamente dal codice di WMS SmartH2O (`wms-frontend/index.html`), settembre 2026. Da usare come base per costruire un frontend visivamente coerente per l'applicativo Fatturazione Utenze (container Docker separato, vedi documento di riepilogo progetto).

**Nota**: alcune parti del testo originale incollato da Daniele erano spezzettate (probabile problema di copia-incolla dall'altra chat). Dove il senso era chiaro ma i valori esatti no, è segnalato esplicitamente sotto con **[ricostruito]** — da verificare con il codice sorgente vero prima di considerarli definitivi.

## 1. Palette colori (variabili CSS `:root`)

```css
--ink:            #0F2438  /* blu-navy scuro — header, testo KPI principale */
--ink-2:          #16324A  /* blu-navy secondario — gradiente header */
--bg:             #F5F8FA  /* sfondo pagina */
--surface:        #FFFFFF  /* sfondo card/pannelli/tabelle */
--surface-2:      #EFF3F6  /* sfondo secondario (hover righe, badge neutri) */
--border:         #DCE4E9  /* bordo standard */
--border-strong:  #C3CFD6  /* bordo enfatizzato (es. header tabella) */

--teal:           #0E7C86  /* PRIMARIO — accenti, tab attivo, bottoni azione */
--teal-dark:      #0A5C63  /* primario hover/testo su sfondo chiaro */
--teal-light:     #E4F3F3  /* sfondo chip/badge primario */

--text:           #152631  /* testo principale */
--text-2:         #57707B  /* testo secondario/etichette */
--text-muted:     #8B9DA6  /* testo terziario/placeholder */

--amber:          #B9700B  /* stato "attenzione" */
--amber-bg:       #FBF1E0

--red:            #B3261E  /* stato "errore/anomalia" */
--red-bg:         #FBEAE8

--green:          #1C7C4C  /* stato "ok/completo" */
--green-bg:       #E7F5EE
```

**Header**: sfondo `linear-gradient(180deg, #0F2438, #16324A)`, con una linea animata in fondo. **[Ricostruito]**: `linear-gradient(90deg, transparent, #0E7C86 20%, #4FD1C5 50%, #0E7C86 80%, transparent)` con `animation: flow 5s linear infinite` — il senso (una riga colorata che "scorre" sotto l'header) è chiaro, ma i valori esatti dei color-stop vanno confermati sul codice sorgente.

## 2. Tipografia

- Font primario: **IBM Plex Sans** (pesi 400/500, possibilmente anche altri — testo originale troncato qui)
- Font monospace: **IBM Plex Mono** (pesi 400/500/600) — usato per TUTTI i valori numerici (KPI, celle tabella con classe `.num`), mai per il testo normale
- Dichiarazione CSS:
  ```css
  --font: 'IBM Plex Sans', system-ui, sans-serif;
  --mono: 'IBM Plex Mono', 'SF Mono', monospace;
  ```
- Dimensioni:
  - corpo: 14px **[ricostruito: line-height ~19-20px, testo originale troncato]**
  - titolo pannello (`.panel h2`): 14px / peso 600
  - tab: 13.5px / peso 500
  - etichette maiuscolo: 10.5-11.5px / peso 500-600, letter-spacing 0.3-0.4px
  - valore KPI: 22px / peso 600, in mono
  - tabelle: 12.5px testo, 10.5px header

## 3. Framework CSS

**Nessuno** — CSS scritto a mano, niente Bootstrap/Tailwind/altri framework. Librerie JS esterne via CDN:
- Chart.js 4.4.1
- D3.js 7.9.0
- Leaflet 1.9.4 (per la mappa)

Zero build step: un unico file HTML statico.

## 4. Layout

Menu orizzontale in alto, **non** una sidebar laterale:

1. `<header>` — logo/titolo a sinistra, filtri a destra, sfondo scuro sfumato (vedi palette).
2. `<nav class="tabs">` — tab orizzontali: Bilancio, Minimo notturno, Connettività, Trend, Analisi dati device, Dati grezzi, Dati Distretti, Manutenzione **[lista troncata nel testo originale — potrebbero essercene altre, da confermare con Daniele]**, con un bottone "?" di aiuto a destra.
3. Dentro ogni tab, eventuali subtab (stesso pattern grafico, badge più piccoli) per le sotto-sezioni.
4. `<main>` centrato, `max-width: 1500px`, contiene `.kpi-grid` (griglia responsive di statistiche) e/o tabelle.
5. `<footer>` semplice in fondo.
6. Schermata di login obbligatoria a schermo intero (overlay) prima di accedere all'app.

## 5. Logo / favicon

Non esiste un logo grafico né un favicon dedicato: il "logo" è l'emoji 💧 (font-size 40px) accanto al testo "WMS SmartH2O" — presente SOLO nella schermata di login, non nell'header dell'app. Nessun tag `<link rel="icon">`: il browser usa il favicon di default.

## 6. Componenti riutilizzabili

- **Card statistica (KPI)**: classe `.kpi` — box bianco, bordo `var(--border)`, radius 14px, padding 16px 18px; etichetta piccola sopra, valore grande in mono sotto (colorabile con classi modificatrici tipo `.warn`/`.danger`). Raggruppate in `.kpi-grid` (`grid-template-columns: repeat(auto-fit, minmax(190px,1fr))`).
- **Pannello/card contenuto**: classe `.panel` — sfondo bianco, radius 8-20px a seconda della variante, con `<h2>` che può contenere un bottone mini a destra.
- **Bottoni**:
  - `.btn-ghost` — trasparente su sfondo scuro (header), usato per azioni nella barra filtri.
  - `.btn-mini` — bottone piccolo grigio di default; la variante primaria si ottiene sovrascrivendo inline con `style="background:var(--teal);color:#fff"` (pattern usato ovunque per "Applica/Ricalcola/Crea/Salva").
  - `.btn-login` — bottone a larghezza piena **[dettaglio colore troncato nel testo originale, presumibilmente teal/primario]**.
- **Badge/stato**: `.badge` (forma a pillola, es. `.real`=verde, `.stimato`=ambra, `.nodata`=grigio) e `.lvl-*` (Buona=verde, Discreto/Sufficiente=teal/blu, Attenzione/Critico/Anomalia/Allarme=rosso) — stesso schema colori riusato anche per le heatmap (`.hm-*`, solo sfondo).
- **Tabelle**: header maiuscolo piccolo, bordo leggero, celle numeriche allineate a destra in mono; variante `.tabella-sticky` con prima/seconda colonna e header sticky per tabelle larghe/scrollabili orizzontalmente (`.table-scroll`).
- **Modali**: `.modale-overlay` (overlay scuro semi-trasparente) + `.modale-box` (card bianca, radius 14px, ombra, max-width 580px o 900px per la variante larga).
- **Grafici**: Chart.js dentro `.chart-wrap` (altezza fissa 260px, `position: relative`).

## 7. Come verrà applicato a Fatturazione Utenze

Riferimento per quando costruiremo il frontend (FastAPI + HTML/CSS, vedi specifiche tecniche, sezione 6): stessa palette, stessa tipografia, stesso pattern di header scuro + tab orizzontali + `.kpi-grid` per la pagina di diagnostica, stesse classi di badge/stato per segnalare anomalie (`.lvl-*`: es. verde per "consolidato", ambra per "provvisorio", rosso per "anomalia da verificare" — coerente con il concetto già introdotto nel motore di calcolo).
