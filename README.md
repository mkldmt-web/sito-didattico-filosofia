# Filosofia per Licei Artistici

Scheletro iniziale di un sito didattico statico in HTML e CSS, pensato per raccogliere materiali, percorsi e attività di filosofia per classi del liceo artistico.

## Struttura del progetto

```text
.
├── index.html   # Home page con menù, sezioni introduttive e macro-aree
├── styles.css   # Stili globali, layout responsive e palette sobria
└── README.md    # Descrizione del progetto e indicazioni di sviluppo
```

## Pagine e sezioni principali

- **Home page**: presenta il progetto e introduce l'approccio didattico.
- **Menù di navigazione**: collega rapidamente a percorsi, macro-aree, attività e risorse.
- **Macro-aree**: raccoglie i grandi nuclei tematici, dalla filosofia antica alla cittadinanza critica.
- **Attività**: suggerisce usi laboratoriali adatti a classi creative.
- **Risorse**: spazio predisposto per futuri materiali, bibliografie e schede.

## Scelte tecniche

Il sito non usa framework o dipendenze esterne: è composto solo da HTML e CSS. Questa scelta lo rende facile da modificare, pubblicare su hosting statici e adattare a esigenze didattiche diverse.

## Come visualizzare il sito

Apri `index.html` direttamente nel browser oppure avvia un piccolo server locale, ad esempio:

```bash
python3 -m http.server 8000
```

Poi visita `http://localhost:8000`.

## Possibili sviluppi

- Aggiungere pagine dedicate ai singoli autori.
- Creare schede operative per l'analisi di testi e opere d'arte.
- Inserire una linea del tempo della storia della filosofia.
- Preparare materiali scaricabili per studenti e docenti.
