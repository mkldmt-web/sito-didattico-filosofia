#!/usr/bin/env python3
"""Genera un vero video MP4 narrativo dall'articolo HTML.

La pipeline:
- estrae solo il testo narrativo dell'articolo;
- associa le immagini già presenti nella pagina;
- genera una traccia audio TTS scena per scena;
- monta un MP4 con ffmpeg;
- produce anche un file VTT per i sottotitoli.

Questa versione è pensata per GitHub Actions: non deve fallire se una
immagine esterna è irraggiungibile e può generare il video in due parti
per ridurre il rischio di rate limit sul TTS.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
ARTICLE = ROOT / "quando-l-arte-era-una-soglia.html"
OUT_DIR = ROOT / "assets" / "video"
WORK_DIR_BASE = ROOT / "video-tools" / ".cache" / "quando-l-arte-era-una-soglia"
BASE_SLUG = "quando-l-arte-era-una-soglia"

EXCLUDED_H2 = {
    "Laboratori della soglia",
    "Bibliografia ragionata",
    "Sitografia e materiali visivi",
}
EXCLUDED_H3_PREFIXES = (
    "Per chi studia",
    "Scheda di metodo",
)
DEFAULT_TITLE = "Quando l’arte era una soglia"

IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FilosofiaPerLiceiArtisticiVideo/1.3; "
        "+https://mkldmt-web.github.io/sito-didattico-filosofia/)"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}


@dataclass
class ImageRef:
    src: str
    alt: str
    caption: str
    local_path: str | None = None


@dataclass
class Scene:
    index: int
    title: str
    text: str
    image: ImageRef
    audio_path: str | None = None
    duration: float = 0.0
    starts_at: float = 0.0


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def iter_article_nodes(article_content: Tag) -> Iterable[Tag]:
    for node in article_content.children:
        if isinstance(node, Tag):
            yield node


def extract_image(figure: Tag) -> ImageRef | None:
    img = figure.find("img")
    if not img or not img.get("src"):
        return None
    caption = figure.find("figcaption")
    return ImageRef(
        src=img["src"],
        alt=clean_text(img.get("alt", "")),
        caption=clean_text(caption.get_text(" ") if caption else ""),
    )


def split_into_scenes(text: str, max_chars: int) -> list[str]:
    """Divide il testo in scene abbastanza lunghe da ridurre le chiamate TTS."""
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ü0-9])", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            wrapped = textwrap.wrap(sentence, width=max_chars, break_long_words=False, break_on_hyphens=False)
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(wrapped)
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def extract_scenes(article_path: Path, max_chars: int) -> list[Scene]:
    soup = BeautifulSoup(article_path.read_text(encoding="utf-8"), "html.parser")
    content = soup.select_one(".article-content")
    if not content:
        raise RuntimeError("Impossibile trovare .article-content nell'articolo.")

    images: list[ImageRef] = []
    blocks: list[tuple[str, str, ImageRef | None]] = []
    current_h2 = ""
    h2_allowed = False
    inside_excluded_h3 = False
    current_image: ImageRef | None = None

    for node in iter_article_nodes(content):
        if node.name == "h2":
            current_h2 = clean_text(node.get_text(" "))
            h2_allowed = current_h2 not in EXCLUDED_H2
            inside_excluded_h3 = False
            continue
        if not h2_allowed:
            continue
        if node.name == "h3":
            heading = clean_text(node.get_text(" "))
            inside_excluded_h3 = heading.startswith(EXCLUDED_H3_PREFIXES)
            continue
        if node.name == "hr":
            inside_excluded_h3 = False
            continue
        if node.select("figure"):
            for figure in node.select("figure"):
                image = extract_image(figure)
                if image:
                    images.append(image)
                    current_image = image
            continue
        if node.name == "p" and not inside_excluded_h3:
            text = clean_text(node.get_text(" "))
            if text:
                blocks.append((current_h2 or DEFAULT_TITLE, text, current_image))

    if not images:
        raise RuntimeError("Nessuna immagine trovata nell'articolo.")

    scenes: list[Scene] = []
    for title, text, image in blocks:
        for chunk in split_into_scenes(text, max_chars):
            scenes.append(Scene(len(scenes) + 1, title, chunk, image or images[0]))
    if not scenes:
        raise RuntimeError("Nessun testo narrativo valido estratto dall'articolo.")
    return scenes


def select_part(scenes: list[Scene], part: str) -> list[Scene]:
    """Restituisce tutte le scene oppure solo metà articolo.

    La divisione serve a generare due video separati e a dimezzare le richieste TTS
    per ogni esecuzione della GitHub Action.
    """
    if part == "all":
        selected = scenes
    else:
        midpoint = math.ceil(len(scenes) / 2)
        if part == "1":
            selected = scenes[:midpoint]
        elif part == "2":
            selected = scenes[midpoint:]
        else:
            raise RuntimeError("Parametro --part non valido: usa 1, 2 oppure all.")
    for index, scene in enumerate(selected, start=1):
        scene.index = index
        scene.duration = 0.0
        scene.starts_at = 0.0
        scene.audio_path = None
    return selected


def output_paths(part: str) -> tuple[Path, Path, Path]:
    if part == "all":
        suffix = ""
        work = WORK_DIR_BASE / "intero"
    else:
        suffix = f"-parte-{part}"
        work = WORK_DIR_BASE / f"parte-{part}"
    mp4 = OUT_DIR / f"{BASE_SLUG}{suffix}.mp4"
    vtt = OUT_DIR / f"{BASE_SLUG}{suffix}.vtt"
    return mp4, vtt, work


def require_binary(name: str) -> None:
    if not shutil.which(name):
        raise RuntimeError(f"'{name}' non trovato nel PATH. Installa ffmpeg prima di eseguire la pipeline.")


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def fallback_image_text(image: ImageRef) -> str:
    text = image.alt or image.caption or "Immagine dell’articolo non disponibile durante la generazione automatica."
    text = re.sub(r"Fonte:.*$", "", text).strip()
    return text[:420]


def create_fallback_image(image: ImageRef, jpg: Path, resolution: tuple[int, int]) -> Path:
    width, height = resolution
    canvas = Image.new("RGB", (width, height), (24, 22, 20))
    draw = ImageDraw.Draw(canvas)

    try:
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(34, width // 32))
        body_font = ImageFont.truetype("DejaVuSans.ttf", max(24, width // 48))
    except OSError:
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()

    margin = int(width * 0.08)
    y = int(height * 0.28)
    draw.text((margin, y), DEFAULT_TITLE, fill=(238, 232, 218), font=title_font)
    y += int(height * 0.09)
    for line in textwrap.wrap(fallback_image_text(image), width=58):
        draw.text((margin, y), line, fill=(206, 196, 176), font=body_font)
        y += int(height * 0.055)
    draw.text(
        (margin, height - int(height * 0.12)),
        "Sorgente visiva non scaricabile dal runner: scheda sostitutiva generata automaticamente.",
        fill=(150, 140, 125),
        font=body_font,
    )
    canvas.save(jpg, quality=92, optimize=True)
    print(f"Avviso: creata immagine sostitutiva per {image.src}")
    image.local_path = str(jpg)
    return jpg


def download_with_retries(url: str, raw: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = requests.get(url, timeout=90, headers=IMAGE_HEADERS)
            if response.status_code == 429:
                wait = 8 * attempt
                print(f"Rate limit immagine 429. Attendo {wait}s e riprovo ({attempt}/5): {url}")
                time.sleep(wait)
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "image" not in content_type and not url.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                raise RuntimeError(f"Risposta non immagine: {content_type}")
            raw.write_bytes(response.content)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            wait = 3 * attempt
            print(f"Avviso: download immagine fallito ({attempt}/5): {exc}. Riprovo tra {wait}s.")
            time.sleep(wait)
    raise RuntimeError(f"Download immagine fallito dopo vari tentativi: {last_error}")


def download_image(image: ImageRef, dest_dir: Path, resolution: tuple[int, int]) -> Path:
    digest = hashlib.sha256(image.src.encode("utf-8")).hexdigest()[:16]
    raw = dest_dir / f"{digest}.img"
    jpg = dest_dir / f"{digest}.jpg"
    if not jpg.exists():
        try:
            download_with_retries(image.src, raw)
            with Image.open(raw) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im = ImageOps.fit(im, resolution, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                im.save(jpg, quality=92, optimize=True)
            raw.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Avviso: uso immagine sostitutiva per errore download: {exc}")
            raw.unlink(missing_ok=True)
            create_fallback_image(image, jpg, resolution)
    image.local_path = str(jpg)
    return jpg


def parse_tts_error(response: requests.Response) -> str:
    try:
        data = response.json()
        error = data.get("error", data)
        if isinstance(error, dict):
            message = error.get("message") or str(error)
            code = error.get("code")
            error_type = error.get("type")
            details = f"{message}"
            if code:
                details += f" | code={code}"
            if error_type:
                details += f" | type={error_type}"
            return details
        return str(data)
    except Exception:  # noqa: BLE001
        return response.text[:800]


def synthesize(scene: Scene, audio_dir: Path, env: dict[str, str]) -> Path:
    fmt = env.get("TTS_OUTPUT_FORMAT", "mp3")
    audio_path = audio_dir / f"scene-{scene.index:03d}.{fmt}"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        scene.audio_path = str(audio_path)
        return audio_path

    api_key = env.get("TTS_API_KEY")
    api_url = env.get("TTS_API_URL", "https://api.openai.com/v1/audio/speech")
    if not api_key or api_key == "inserisci_qui_la_tua_api_key":
        raise RuntimeError("TTS_API_KEY mancante. Inserisci OPENAI_API_KEY nei repository secrets GitHub.")

    payload = {
        "model": env.get("TTS_MODEL", "gpt-4o-mini-tts"),
        "voice": env.get("TTS_VOICE", "alloy"),
        "input": scene.text,
        "speed": float(env.get("TTS_SPEED", "0.95")),
        "response_format": fmt,
    }

    max_attempts = int(env.get("TTS_MAX_ATTEMPTS", "10"))
    base_wait = int(env.get("TTS_RETRY_BASE_SECONDS", "35"))
    pause_between_calls = float(env.get("TTS_PAUSE_SECONDS", "15"))
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        print(f"TTS scena {scene.index}: tentativo {attempt}/{max_attempts}")
        response = requests.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=180,
        )

        if response.ok:
            audio_path.write_bytes(response.content)
            scene.audio_path = str(audio_path)
            if pause_between_calls > 0:
                time.sleep(pause_between_calls)
            return audio_path

        details = parse_tts_error(response)
        last_error = f"HTTP {response.status_code}: {details}"
        print(f"Avviso TTS scena {scene.index}: {last_error}")
        lower = details.lower()

        if "insufficient_quota" in lower or "billing" in lower or "exceeded your current quota" in lower:
            raise RuntimeError(
                "OpenAI API non ha quota/credito disponibile per generare l'audio. "
                "Controlla billing e crediti su platform.openai.com. Dettaglio: " + last_error
            )

        if response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts:
            retry_after = response.headers.get("Retry-After")
            wait = int(float(retry_after)) if retry_after else base_wait * attempt
            print(f"Attendo {wait}s prima di riprovare il TTS.")
            time.sleep(wait)
            continue

        response.raise_for_status()

    raise RuntimeError(f"TTS fallito dopo {max_attempts} tentativi. Ultimo errore: {last_error}")


def media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def vtt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_vtt(scenes: list[Scene], path: Path) -> None:
    lines = ["WEBVTT", ""]
    for scene in scenes:
        start = scene.starts_at
        end = scene.starts_at + scene.duration
        lines.append(f"{vtt_time(start)} --> {vtt_time(end)}")
        lines.append(scene.text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def make_scene_video(scene: Scene, scene_dir: Path, resolution: str, fps: int, crf: str, preset: str) -> Path:
    assert scene.audio_path and scene.image.local_path
    out = scene_dir / f"scene-{scene.index:03d}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    width, height = resolution.split("x", 1)
    frames = max(1, int(scene.duration * fps))
    x_expr = "iw/2-(iw/zoom/2)" if scene.index % 2 else f"(iw-iw/zoom)*on/{frames}"
    y_expr = "ih/2-(ih/zoom/2)"
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"zoompan=z='min(zoom+0.00045,1.045)':d={frames}:x='{x_expr}':y='{y_expr}':s={resolution}:fps={fps},"
        "fade=t=in:st=0:d=0.7,"
        f"fade=t=out:st={max(scene.duration - 0.7, 0):.3f}:d=0.7"
    )
    run(
        [
            "ffmpeg", "-y", "-loop", "1", "-i", scene.image.local_path, "-i", scene.audio_path,
            "-t", f"{scene.duration:.3f}", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-crf", crf, "-preset", preset, str(out),
        ]
    )
    return out


def concat_videos(parts: list[Path], out: Path, work_dir: Path) -> None:
    list_file = work_dir / "concat.txt"
    list_file.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)])


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera MP4 e VTT dall'articolo quando-l-arte-era-una-soglia.html")
    parser.add_argument("--max-chars", type=int, default=1500, help="lunghezza massima indicativa del testo di una scena")
    parser.add_argument("--part", choices=["1", "2", "all"], default="all", help="genera solo parte 1, parte 2, oppure tutto")
    parser.add_argument("--manifest-only", action="store_true", help="estrae scene e immagini senza chiamare TTS/ffmpeg")
    args = parser.parse_args()

    load_dotenv(ROOT / "video-tools" / ".env")
    env = os.environ.copy()
    resolution = env.get("VIDEO_RESOLUTION", "1280x720")
    fps = int(env.get("VIDEO_FPS", "24"))
    crf = env.get("VIDEO_CRF", "28")
    preset = env.get("VIDEO_PRESET", "medium")
    size = tuple(map(int, resolution.lower().split("x", 1)))
    mp4_out, vtt_out, work_dir = output_paths(args.part)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    image_dir = work_dir / "images"
    audio_dir = work_dir / "audio"
    scene_dir = work_dir / "scenes"
    for folder in (image_dir, audio_dir, scene_dir):
        folder.mkdir(parents=True, exist_ok=True)

    all_scenes = extract_scenes(ARTICLE, args.max_chars)
    scenes = select_part(all_scenes, args.part)
    for scene in scenes:
        download_image(scene.image, image_dir, size)

    manifest = work_dir / "scenes.json"
    manifest.write_text(json.dumps([asdict(scene) for scene in scenes], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Scene totali articolo: {len(all_scenes)}")
    print(f"Scene selezionate per parte {args.part}: {len(scenes)}")
    print(f"Manifest: {manifest}")
    if args.manifest_only:
        return

    require_binary("ffmpeg")
    require_binary("ffprobe")

    elapsed = 0.0
    for scene in scenes:
        audio = synthesize(scene, audio_dir, env)
        scene.duration = media_duration(audio)
        scene.starts_at = elapsed
        elapsed += scene.duration
    write_vtt(scenes, vtt_out)
    manifest.write_text(json.dumps([asdict(scene) for scene in scenes], ensure_ascii=False, indent=2), encoding="utf-8")

    parts = [make_scene_video(scene, scene_dir, resolution, fps, crf, preset) for scene in scenes]
    concat_videos(parts, mp4_out, work_dir)
    print(f"Video generato: {mp4_out}")
    print(f"Sottotitoli generati: {vtt_out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"Errore: {exc}", file=sys.stderr)
        sys.exit(1)
