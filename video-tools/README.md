# Pipeline video — “Quando l’arte era una soglia”

Questa cartella contiene una pipeline reale per generare un file `.mp4` pubblicabile a partire dall’articolo `quando-l-arte-era-una-soglia.html`.

La pipeline:

- legge l’articolo HTML;
- estrae soltanto il testo narrativo;
- esclude laboratori, schede di metodo, bibliografia, sitografia e materiali di servizio;
- divide il testo in scene brevi;
- associa alle scene le immagini già presenti nell’articolo;
- genera audio con una voce italiana tramite API TTS configurabile;
- monta un video `.mp4` con zoom lento, movimento leggero tipo Ken Burns e dissolvenze;
- crea un file `.vtt` per i sottotitoli.

## Requisiti di sistema

Servono Python 3.10+ e `ffmpeg` con `ffprobe` disponibili nel `PATH`.

Verifica:

```bash
ffmpeg -version
ffprobe -version
```

## Installazione dipendenze

Dalla radice del progetto:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r video-tools/requirements.txt
```

## Configurazione API TTS

Copia il file di esempio:

```bash
cp video-tools/.env.example video-tools/.env
```

Poi modifica `video-tools/.env` e inserisci la tua chiave:

```env
TTS_API_KEY=la_tua_api_key
```

Le variabili principali sono:

- `TTS_API_URL`: endpoint del servizio TTS compatibile con OpenAI Audio Speech API;
- `TTS_API_KEY`: chiave API;
- `TTS_MODEL`: modello TTS;
- `TTS_VOICE`: voce da usare;
- `TTS_SPEED`: velocità della voce;
- `TTS_OUTPUT_FORMAT`: formato audio richiesto all’API, di default `mp3`;
- `VIDEO_RESOLUTION`: risoluzione finale, di default `1920x1080`;
- `VIDEO_FPS`: fotogrammi al secondo;
- `VIDEO_CRF` e `VIDEO_PRESET`: qualità e preset di codifica `libx264`.

## Generazione del video

Esegui:

```bash
python video-tools/generate_video.py
```

Il risultato viene salvato in:

```text
assets/video/quando-l-arte-era-una-soglia.mp4
assets/video/quando-l-arte-era-una-soglia.vtt
```

La cartella `video-tools/.cache/` conserva immagini, audio e clip intermedie: le esecuzioni successive riusano i file già generati quando possibile.

## Controllo preliminare senza TTS

Per verificare estrazione del testo, immagini e scene senza chiamare l’API TTS né montare il video:

```bash
python video-tools/generate_video.py --manifest-only
```

Il manifest delle scene viene scritto in:

```text
video-tools/.cache/quando-l-arte-era-una-soglia/scenes.json
```
