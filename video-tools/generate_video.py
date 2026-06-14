#!/usr/bin/env python3
"""Genera un vero video MP4 narrativo dall'articolo HTML.

La pipeline estrae solo il testo narrativo, associa le immagini presenti
nell'articolo, genera audio TTS scena per scena e monta un MP4 con ffmpeg.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
ARTICLE = ROOT / "quando-l-arte-era-una-soglia.html"
OUT_DIR = ROOT / "assets" / "video"
WORK_DIR = ROOT / "video-tools" / ".cache" / "quando-l-arte-era-una-soglia"
MP4_OUT = OUT_DIR / "quando-l-arte-era-una-soglia.mp4"
VTT_OUT = OUT_DIR / "quando-l-arte-era-una-soglia.vtt"

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


def require_binary(name: str) -> None:
    if not shutil.which(name):
        raise RuntimeError(f"'{name}' non trovato nel PATH. Installa ffmpeg prima di eseguire la pipeline.")


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def download_image(image: ImageRef, dest_dir: Path, resolution: tuple[int, int]) -> Path:
    digest = hashlib.sha256(image.src.encode("utf-8")).hexdigest()[:16]
    raw = dest_dir / f"{digest}.img"
    jpg = dest_dir / f"{digest}.jpg"
    if not jpg.exists():
        response = requests.get(image.src, timeout=60, headers={"User-Agent": "video-pipeline/1.0"})
        response.raise_for_status()
        raw.write_bytes(response.content)
        with Image.open(raw) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            # Normalizza in 16:9 con crop centrale: ffmpeg applicherà poi il Ken Burns.
            im = ImageOps.fit(im, resolution, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            im.save(jpg, quality=92, optimize=True)
        raw.unlink(missing_ok=True)
    image.local_path = str(jpg)
    return jpg


def synthesize(scene: Scene, audio_dir: Path, env: dict[str, str]) -> Path:
    fmt = env.get("TTS_OUTPUT_FORMAT", "mp3")
    audio_path = audio_dir / f"scene-{scene.index:03d}.{fmt}"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        scene.audio_path = str(audio_path)
        return audio_path
    api_key = env.get("TTS_API_KEY")
    api_url = env.get("TTS_API_URL", "https://api.openai.com/v1/audio/speech")
    if not api_key or api_key == "inserisci_qui_la_tua_api_key":
        raise RuntimeError("TTS_API_KEY mancante. Copia .env.example in .env e inserisci una API key valida.")
    payload = {
        "model": env.get("TTS_MODEL", "gpt-4o-mini-tts"),
        "voice": env.get("TTS_VOICE", "alloy"),
        "input": scene.text,
        "speed": float(env.get("TTS_SPEED", "0.95")),
        "response_format": fmt,
    }
    response = requests.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=180,
    )
    response.raise_for_status()
    audio_path.write_bytes(response.content)
    scene.audio_path = str(audio_path)
    return audio_path


def media_duration(path: Path) -> float:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], check=True, capture_output=True, text=True)
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
    # Zoom quasi impercettibile e pan alternato, sobrio e lento.
    x_expr = "iw/2-(iw/zoom/2)" if scene.index % 2 else "(iw-iw/zoom)*on/{frames}".format(frames=frames)
    y_expr = "ih/2-(ih/zoom/2)"
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"zoompan=z='min(zoom+0.00055,1.055)':d={frames}:x='{x_expr}':y='{y_expr}':s={resolution}:fps={fps},"
        "fade=t=in:st=0:d=0.7,"
        f"fade=t=out:st={max(scene.duration - 0.7, 0):.3f}:d=0.7"
    )
    run([
        "ffmpeg", "-y", "-loop", "1", "-i", scene.image.local_path, "-i", scene.audio_path,
        "-t", f"{scene.duration:.3f}", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-crf", crf, "-preset", preset, str(out)
    ])
    return out


def concat_videos(parts: list[Path], out: Path) -> None:
    list_file = WORK_DIR / "concat.txt"
    list_file.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)])


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera MP4 e VTT dall'articolo quando-l-arte-era-una-soglia.html")
    parser.add_argument("--max-chars", type=int, default=420, help="lunghezza massima indicativa del testo di una scena")
    parser.add_argument("--manifest-only", action="store_true", help="estrae scene e immagini senza chiamare TTS/ffmpeg")
    args = parser.parse_args()

    load_dotenv(ROOT / "video-tools" / ".env")
    env = os.environ.copy()
    resolution = env.get("VIDEO_RESOLUTION", "1920x1080")
    fps = int(env.get("VIDEO_FPS", "25"))
    crf = env.get("VIDEO_CRF", "20")
    preset = env.get("VIDEO_PRESET", "medium")
    size = tuple(map(int, resolution.lower().split("x", 1)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    image_dir = WORK_DIR / "images"
    audio_dir = WORK_DIR / "audio"
    scene_dir = WORK_DIR / "scenes"
    for folder in (image_dir, audio_dir, scene_dir):
        folder.mkdir(parents=True, exist_ok=True)

    scenes = extract_scenes(ARTICLE, args.max_chars)
    for scene in scenes:
        download_image(scene.image, image_dir, size)  # cache locale per montaggio stabile

    manifest = WORK_DIR / "scenes.json"
    manifest.write_text(json.dumps([asdict(scene) for scene in scenes], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Scene estratte: {len(scenes)}")
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
    write_vtt(scenes, VTT_OUT)
    manifest.write_text(json.dumps([asdict(scene) for scene in scenes], ensure_ascii=False, indent=2), encoding="utf-8")

    parts = [make_scene_video(scene, scene_dir, resolution, fps, crf, preset) for scene in scenes]
    concat_videos(parts, MP4_OUT)
    print(f"Video generato: {MP4_OUT}")
    print(f"Sottotitoli generati: {VTT_OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        sys.exit(1)
