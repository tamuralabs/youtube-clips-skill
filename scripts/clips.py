#!/usr/bin/env python3
"""youtube-clips: vídeo longo (YouTube ou arquivo) -> clipes verticais 9:16 legendados.

Fluxo em três etapas, com a escolha dos momentos feita pelo agente entre elas:

  clips.py prepare <url|arquivo> --out DIR     baixa, transcreve com timestamps por palavra
  (agente lê DIR/transcript.txt e grava DIR/plan.json seguindo references/selection.md)
  clips.py render DIR                          reenquadra, legenda, aplica gancho e exporta
  clips.py cleanup DIR                         apaga vídeo-fonte e áudio para liberar disco

Ideias de pipeline estudadas em OpenShorts (MIT) e SupoClip (AGPL, só conceitos);
nenhum código desses projetos foi copiado.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("YOUTUBE_CLIPS_CONFIG", Path.home() / ".config" / "youtube-clips")).expanduser()


def _load_kv(path: Path) -> dict[str, str]:
    """Lê um arquivo KEY=VALUE (comentários com #). Arquivo ausente vira dicionário vazio."""
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#") and v.strip():
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


# config.env (caminhos, sem segredos): ~/.config/youtube-clips/config.env e, por cima, <skill>/config.env
_SETTINGS = {**_load_kv(CONFIG_DIR / "config.env"), **_load_kv(SKILL / "config.env")}


def setting(name: str, default: str) -> str:
    return os.environ.get(name) or _SETTINGS.get(name) or default


TOOLS = Path(setting("YOUTUBE_CLIPS_HOME", str(Path.home() / ".local" / "share" / "youtube-clips"))).expanduser()
VENV_PY = TOOLS / ".venv" / "bin" / "python"
FONTS_DIR = SKILL / "assets" / "fonts"
YUNET = TOOLS / "models" / "face_detection_yunet_2023mar.onnx"
DEFAULT_COOKIES = Path(setting("YOUTUBE_COOKIES", str(CONFIG_DIR / "youtube-cookies.txt"))).expanduser()
# credenciais: arquivos .env em ordem de prioridade (YOUTUBE_CLIPS_SECRETS aceita vários, separados por ":")
SECRET_FILES = [Path(x).expanduser() for x in setting("YOUTUBE_CLIPS_SECRETS", "").split(":") if x] + \
    [CONFIG_DIR / ".env", SKILL / ".env"]
# streamers: funciona em qualquer plano, cobra por MB (FREE US$ 0,006/MB); epctex: por segundo, mas limita
# contas FREE a US$ 0,20 por execução (verificado em 20/10/2026)
APIFY_AUDIO_ACTOR = "lurkapi~youtube-to-mp3-audio-downloader"  # só áudio, US$ 0,002/min (20/10/2026)
APIFY_ACTORS = {
    "streamers": "streamers~youtube-video-downloader",
    "epctex": "epctex~youtube-video-downloader",
}
YTDLP_CANDIDATES = [Path.home() / ".local" / "bin" / "yt-dlp"]

OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30
ASR_CHUNK, ASR_OVERLAP = 600.0, 6.0  # blocos de 10 min com 6 s de sobreposição nas emendas
ANALYSIS_FPS = 4          # amostras/s para detecção de rosto e cortes de cena
SCENE_CUT_CORR = 0.55     # correlação de histograma abaixo disso = corte de cena
FACE_MIN_SCORE = 0.75
TRACK_MIN_COVERAGE = 0.4  # fração de amostras do plano com rosto para usar o modo track
DEAD_ZONE = 0.06          # fração da largura de origem em que a câmera não se move
CAM_ALPHA = 0.10          # suavização exponencial por frame quando a câmera se move
STATIC_STD = 0.035        # desvio do rosto (fração da largura) abaixo disso = enquadramento fixo


def ensure_venv() -> None:
    if Path(sys.prefix).resolve() != (TOOLS / ".venv").resolve() and VENV_PY.exists():
        os.environ.setdefault("PYTHONUNBUFFERED", "1")  # log de progresso em tempo real
        os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    num, den = (int(x) for x in (v or {}).get("avg_frame_rate", "30/1").split("/"))
    return {
        "width": int(v["width"]) if v else None,
        "height": int(v["height"]) if v else None,
        "fps": num / den if den else 30.0,
        "duration": float(data["format"]["duration"]),
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def slugify(text: str, maxlen: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "clip"


def fmt_ts(sec: float) -> str:
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"


# ---------------------------------------------------------------- prepare

def find_ytdlp() -> str:
    for c in YTDLP_CANDIDATES:
        if c.exists():
            return str(c)
    found = shutil.which("yt-dlp")
    if not found:
        sys.exit("yt-dlp não encontrado. Instale com: uv tool install yt-dlp")
    return found


class Blocked(Exception):
    """YouTube recusou o IP da VPS (pede login)."""


def download(url: str, out: Path, cookies: Path | None, max_height: int) -> Path:
    ytdlp_run(["-f", f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
               "--merge-output-format", "mp4", "--write-info-json", "-o", str(out / "source.%(ext)s"), url], cookies)
    return out / "source.mp4"


def has_cookies(path: Path | None) -> bool:
    """Arquivo existe e tem ao menos uma linha de cookie (o placeholder só tem comentários)."""
    if not path or not path.exists():
        return False
    return any(l.strip() and (not l.startswith("#") or l.startswith("#HttpOnly_"))
               for l in path.read_text(errors="ignore").splitlines())


def ytdlp_run(args: list[str], cookies: Path | None) -> None:
    """Roda o yt-dlp; com proxies, tenta um por um até o YouTube aceitar e lembra o que funcionou."""
    base = [find_ytdlp(), "--no-playlist", "--no-progress"]
    if has_cookies(cookies):
        base += ["--cookies", str(cookies)]
    candidates = proxy_candidates() or [None]
    last_err = "erro desconhecido"
    for pid, url in candidates:
        # proxy por variável de ambiente (yt-dlp e ffmpeg leem): na linha de comando a senha apareceria em `ps`
        env = {**os.environ, **({k: url for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
                                if url else {})}
        proc = subprocess.run(base + args, capture_output=True, text=True, env=env)
        if proc.returncode == 0:
            if pid:
                remember_proxy(pid)
            return
        last_err = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "erro desconhecido"
        blocked = "not a bot" in proc.stderr or "Sign in" in proc.stderr
        if url and (blocked or "Unable to connect to proxy" in proc.stderr or "407" in proc.stderr):
            print(f"  proxy {pid} recusado; tentando outro")
            continue
        if blocked:
            raise Blocked(last_err)
        sys.exit(f"Falha no yt-dlp: {last_err}")
    raise Blocked(last_err)


def download_audio(url: str, out: Path, cookies: Path | None) -> tuple[Path, dict]:
    """Só o áudio, para transcrever vídeos longos sem baixar a imagem inteira."""
    ytdlp_run(["-f", "ba[ext=m4a]/ba/b", "--write-info-json", "-o", str(out / "audio_src.%(ext)s"), url], cookies)
    info_file = out / "audio_src.info.json"
    info = json.loads(info_file.read_text()) if info_file.exists() else {}
    info_file.unlink(missing_ok=True)
    audio = next(p for p in out.glob("audio_src.*"))
    return audio, info


def download_section(url: str, start: float, end: float, dest: Path, cookies: Path | None, max_height: int) -> None:
    """Baixa só o trecho [start, end] na melhor qualidade até max_height, com corte preciso."""
    ytdlp_run(["-f", f"bv*[vcodec^=avc1][height<={max_height}]+ba[ext=m4a]/bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
               "--download-sections", f"*{start:.2f}-{end:.2f}",
               "--merge-output-format", "mp4", "-o", str(dest), url], cookies)


def read_pcm(path: Path, start: float = 0.0, dur: float | None = None, rate: int = 8000):
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(path)]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    raw = subprocess.run(cmd + ["-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)


def locate_section(section: Path, audio: Path, approx: float, rate: int = 8000) -> float:
    """Tempo da fonte em que o trecho baixado começa (o corte sem recodificar cai no keyframe anterior).

    Correlaciona os primeiros segundos de áudio do trecho com o áudio completo já extraído."""
    import numpy as np

    probe_len, search = 12.0, 15.0
    a = read_pcm(section, 0.0, probe_len, rate)
    base = max(0.0, approx - search)
    b = read_pcm(audio, base, probe_len + 2 * search, rate)
    if len(a) < rate or len(b) < len(a):
        return approx
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)[: len(b) - len(a) + 1]
    return base + int(np.argmax(corr)) / rate


def apify_token() -> str | None:
    """APIFY_ORG_API_TOKEN (conta de organização) primeiro; depois login da CLI; por fim APIFY_TOKEN."""
    tok = read_env("APIFY_ORG_API_TOKEN")
    cli = Path.home() / ".apify" / "auth.json"
    if not tok and cli.exists():
        tok = json.loads(cli.read_text()).get("token")
    return tok or read_env("APIFY_TOKEN") or read_env("APIFY_API_TOKEN")


PROXY_STATE = TOOLS / "proxy-state.json"  # só o id do último proxy que o YouTube aceitou (sem credencial)
_PROXIES: list = []


def proxy_candidates() -> list[tuple[str, str]]:
    """[(id, url)] na ordem de tentativa: YOUTUBE_PROXY_URL, ou proxies da Webshare pela API (WEBSHARE_API_KEY).
    Plano residencial: uma conexão p.webshare.io com sessão fixa. ISP/datacenter: os proxies diretos, EUA
    primeiro (em 22/10/2026 o YouTube aceitou os dos EUA e recusou FR, CA e DE) e o último que funcionou à frente."""
    if _PROXIES:
        return _PROXIES
    if read_env("YOUTUBE_PROXY_URL"):
        _PROXIES.append(("env", read_env("YOUTUBE_PROXY_URL")))
    else:
        _PROXIES.extend(webshare_proxies())
    return _PROXIES


def remember_proxy(pid: str) -> None:
    if pid != "env":
        PROXY_STATE.write_text(json.dumps({"last_ok": pid}))


def webshare_proxies() -> list[tuple[str, str]]:
    import random
    import urllib.request

    key = read_env("WEBSHARE_API_KEY")
    if not key:
        return []

    def get(path: str) -> dict:
        req = urllib.request.Request(f"https://proxy.webshare.io{path}",
                                     headers={"Authorization": f"Token {key}", "User-Agent": "youtube-clips/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    try:
        plans = [x for x in get("/api/v2/subscription/plan/").get("results", []) if x.get("status") == "active"]
        resi = next((x for x in plans if x.get("proxy_type") == "residential"
                     or "residential" in json.dumps(x.get("pool_filter") or "").lower()), None)
        if resi:
            cfg = get(f"/api/v3/proxy/config?plan_id={resi['id']}")
            country = (read_env("WEBSHARE_COUNTRY") or "").lower()
            user = "-".join(x for x in (cfg["username"], country, str(random.randint(10000, 99999))) if x)
            return [("residencial", f"http://{user}:{cfg['password']}@p.webshare.io:80")]
        rows = [r for r in get("/api/v2/proxy/list/?mode=direct&page=1&page_size=100").get("results", [])
                if r.get("valid")]
    except Exception as err:  # noqa: BLE001
        print(f"  Webshare: não consegui ler os proxies ({err}); seguindo sem proxy")
        return []
    last = json.loads(PROXY_STATE.read_text()).get("last_ok") if PROXY_STATE.exists() else None
    pref = (read_env("WEBSHARE_COUNTRY") or "US").upper()
    rows.sort(key=lambda r: (str(r["id"]) != str(last), r.get("country_code") != pref))
    return [(str(r["id"]), f"http://{r['username']}:{r['password']}@{r['proxy_address']}:{r['port']}")
            for r in rows]


def read_env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    for f in SECRET_FILES:
        v = _load_kv(f).get(name)
        if v:
            return v
    return None


def apify_api(method: str, path: str, body: dict | None = None):
    import urllib.error
    import urllib.request

    token = apify_token()
    if not token:
        sys.exit("Token da Apify ausente no arquivo de credenciais (veja o README); sem como contornar o bloqueio do YouTube.")
    req = urllib.request.Request(f"https://api.apify.com/v2{path}", method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Apify recusou {path.split('?')[0]}: HTTP {e.code} {e.read()[:300].decode(errors='ignore')}")


def apify_start(actor: str, body: dict, max_usd: float) -> str:
    run_ = apify_api("POST", f"/acts/{actor}/runs?maxTotalChargeUsd={max(max_usd, 0.5):.2f}", body)["data"]
    return run_["id"]


def apify_result(run_id: str, what: str) -> dict:
    """Espera a execução terminar e devolve o primeiro item do dataset."""
    import time

    run_ = apify_api("GET", f"/actor-runs/{run_id}")["data"]
    while run_["status"] in ("READY", "RUNNING"):
        time.sleep(5)
        run_ = apify_api("GET", f"/actor-runs/{run_id}")["data"]
    if run_["status"] != "SUCCEEDED":
        sys.exit(f"Apify ({what}) terminou com status {run_['status']} (run {run_id}).")
    items = apify_api("GET", f"/datasets/{run_['defaultDatasetId']}/items?clean=true")
    if not items:
        msg = run_.get("statusMessage") or "sem mensagem"
        if "paid plan" in msg:
            sys.exit(f"Apify não devolveu o {what}: a conta está no plano FREE e este actor limita contas gratuitas "
                     "a US$ 0,20 por execução. Use --apify-actor streamers, conta paga ou cookies do YouTube.")
        sys.exit(f"Apify não devolveu o {what}: {msg}. Se o custo passaria do teto, aumente --apify-max-usd "
                 "ou baixe em qualidade menor.")
    usage = run_.get("usageTotalUsd")
    print(f"  Apify ({what}) concluiu" + (f", US$ {usage:.2f}" if usage else ""))
    return {**items[0], "_usd": usage}


def apify_fetch(link: str, dest: Path) -> None:
    import urllib.request

    headers = {"Authorization": f"Bearer {apify_token()}"} if "api.apify.com" in link else {}
    with urllib.request.urlopen(urllib.request.Request(link, headers=headers), timeout=120) as r, \
            open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    print(f"  baixado {dest.name}: {dest.stat().st_size / 1e6:.0f} MB")


def apify_video_link(item: dict) -> str:
    link = next((item[k] for k in ("downloadedFileUrl", "downloadUrl", "url", "videoUrl", "fileUrl")
                 if isinstance(item.get(k), str) and item[k].startswith("http") and "youtube.com" not in item[k]), None)
    if not link:
        sys.exit(f"Item da Apify sem link de vídeo reconhecido; campos: {sorted(item)}")
    return link


def apify_prepare(url: str, out: Path, a: argparse.Namespace, meta: dict) -> Path:
    """Dispara o vídeo e o áudio na Apify ao mesmo tempo. O áudio chega rápido e libera a transcrição;
    o vídeo continua baixando na Apify e só é buscado no render."""
    quality = str(max(q for q in (360, 480, 720, 1080) if q <= a.max_height) if a.max_height >= 360 else 360)
    vbody = ({"videos": [{"url": url}], "preferredQuality": f"{quality}p", "preferredFormat": "mp4"}
             if a.apify_actor == "streamers" else {"startUrls": [url], "quality": quality})
    video_run = apify_start(APIFY_ACTORS[a.apify_actor], vbody, a.apify_max_usd)
    audio_run = apify_start(APIFY_AUDIO_ACTOR, {"videoUrls": [url], "audioFormat": "opus", "audioBitrate": "64k"},
                            a.apify_max_usd)
    print(f"  Apify: vídeo {quality}p (run {video_run}) e áudio (run {audio_run}) em paralelo")
    item = apify_result(audio_run, "áudio")
    audio_src = out / "audio_src.opus"
    apify_fetch(item["audioFileUrl"], audio_src)
    meta.update({"title": item.get("title"), "channel": item.get("channel"), "downloader": "apify",
                 "apify_audio_usd": item.get("_usd"),
                 "remote": {"apify_run": video_run, "actor": APIFY_ACTORS[a.apify_actor]}})
    return audio_src


class RateLimited(Exception):
    def __init__(self, wait: float):
        super().__init__(f"cota esgotada por {wait:.0f}s")
        self.wait = wait


GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODELS = ["whisper-large-v3", "whisper-large-v3-turbo"]  # a cota gratuita de áudio/hora é por modelo
LANG_CODES = {"portuguese": "pt", "english": "en", "spanish": "es", "french": "fr", "german": "de",
              "italian": "it", "japanese": "ja"}


def groq_chunk(path: Path, model: str, language: str | None, offset: float) -> tuple[list[dict], str | None]:
    import urllib.error
    import urllib.request
    import uuid

    b = uuid.uuid4().hex
    fields = [("model", model), ("response_format", "verbose_json"), ("temperature", "0"),
              ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
    if language:
        fields.append(("language", language))
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
             f"Content-Type: audio/ogg\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {read_env('GROQ_API_KEY')}", "User-Agent": "youtube-clips/1.0",
        "Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            msg = e.read().decode(errors="ignore")
            wait = float(e.headers.get("retry-after") or 0)
            m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", msg)
            if m:
                wait = max(wait, int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + float(m[3]))
            raise RateLimited(wait or 60)
        raise
    words = [{"w": w["word"].strip(), "s": round(w["start"] + offset, 3), "e": round(w["end"] + offset, 3), "p": 1.0}
             for w in d.get("words", []) if w["word"].strip()]
    lang = d.get("language")
    return words, LANG_CODES.get(str(lang).lower(), lang)


def transcribe(media: Path, audio: Path, wav_ready, total: float, out: Path, language: str | None, asr: str,
               local_model: str, beam: int) -> dict:
    """Transcreve em blocos de 10 min com checkpoint por bloco (out/asr/NNNN.json).

    asr=auto: blocos vão para a Groq em paralelo (alternando os modelos, cuja cota é separada); se a cota
    acabar, o Whisper local assume blocos a partir do fim até a Groq liberar. asr=local: só CPU.
    Os blocos da Groq saem direto de `media`; o WAV (`audio`) é extraído em paralelo e só o local espera por ele."""
    import math
    import threading
    import time
    import wave
    from collections import deque

    import numpy as np

    n = max(1, math.ceil(total / ASR_CHUNK))
    cdir = out / "asr"
    cdir.mkdir(exist_ok=True)
    use_groq = asr in ("auto", "groq") and bool(read_env("GROQ_API_KEY"))
    if asr == "groq" and not use_groq:
        sys.exit("GROQ_API_KEY ausente no arquivo de credenciais (veja o README).")
    todo = deque(i for i in range(n) if not (cdir / f"{i:04d}.json").exists())
    if len(todo) < n:
        print(f"  retomando: {n - len(todo)} de {n} blocos já transcritos")
    lock, state = threading.Lock(), {"lang": language, "done": n - len(todo), "engines": {}}
    blocked, fails = {m: 0.0 for m in GROQ_MODELS}, {}
    need_local = threading.Event()

    def bounds(i: int) -> tuple[float, float]:
        return max(0.0, i * ASR_CHUNK - ASR_OVERLAP), min(total, (i + 1) * ASR_CHUNK + ASR_OVERLAP)

    def save(i: int, words: list[dict], engine: str) -> None:
        (cdir / f"{i:04d}.json").write_text(json.dumps({"engine": engine, "words": words}, ensure_ascii=False))
        with lock:
            state["done"] += 1
            state["engines"][engine] = state["engines"].get(engine, 0) + 1
            print(f"\r  blocos prontos: {state['done']}/{n} ({', '.join(f'{k} {v}' for k, v in state['engines'].items())})",
                  end="", flush=True)

    def groq_worker() -> None:
        while True:
            with lock:
                if not todo:
                    return
                i = todo.popleft()
            now = time.time()
            model = next((m for m in GROQ_MODELS if blocked[m] <= now), None)
            if not model:
                with lock:
                    todo.appendleft(i)
                if asr == "auto":
                    need_local.set()
                time.sleep(min(30.0, max(1.0, min(blocked.values()) - now)))
                continue
            s, e = bounds(i)
            ogg = cdir / f".{i:04d}.ogg"
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(media), "-vn",
                 "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(ogg)])
            try:
                words, lang = groq_chunk(ogg, model, state["lang"], s)
            except RateLimited as rl:
                blocked[model] = time.time() + rl.wait
                with lock:
                    todo.appendleft(i)
                continue
            except Exception as err:  # falha de rede/servidor: tenta de novo; na 3ª, o bloco vai para o local
                fails[i] = fails.get(i, 0) + 1
                print(f"\n  Groq falhou no bloco {i} ({err})")
                if fails[i] >= 3:
                    if asr != "auto":
                        sys.exit("Groq falhou 3 vezes no mesmo bloco; rode de novo (retoma) ou use --asr local.")
                    need_local.set()
                    with lock:
                        todo.append(i)
                else:
                    time.sleep(5)
                    with lock:
                        todo.appendleft(i)
                continue
            finally:
                ogg.unlink(missing_ok=True)
            with lock:
                state["lang"] = state["lang"] or lang
            save(i, words, model.replace("whisper-", "groq-"))

    def local_worker() -> None:
        from faster_whisper import WhisperModel

        model = None
        wav_ready.wait()
        with wave.open(str(audio), "rb") as wf:
            nframes = wf.getnframes()
        # lido do disco sob demanda (memmap): o áudio de horas não ocupa RAM
        pcm = np.memmap(audio, np.int16, "r", offset=audio.stat().st_size - nframes * 2, shape=(nframes,))
        while True:
            with lock:
                if not todo:
                    return
                i = todo.pop()  # pega do fim: encontra a Groq no meio
            model = model or WhisperModel(local_model, device="cpu", compute_type="int8",
                                          cpu_threads=os.cpu_count() or 4)
            s, e = bounds(i)
            block = pcm[int(s * 16000):int(e * 16000)].astype(np.float32) / 32768.0
            segs, info = model.transcribe(block, language=state["lang"], word_timestamps=True, vad_filter=True,
                                          vad_parameters={"min_silence_duration_ms": 500}, beam_size=beam,
                                          condition_on_previous_text=False)
            words = [{"w": w.word.strip(), "s": round(w.start + s, 3), "e": round(w.end + s, 3),
                      "p": round(w.probability, 3)} for seg in segs for w in (seg.words or []) if w.word.strip()]
            with lock:
                state["lang"] = state["lang"] or info.language
            save(i, words, f"local-{local_model}")

    if todo:
        if use_groq:
            threads = [threading.Thread(target=groq_worker, daemon=True) for _ in range(3)]
            for t in threads:
                t.start()
            local = None
            while any(t.is_alive() for t in threads) or (local and local.is_alive()):
                if asr == "auto" and need_local.is_set() and not local:
                    print("\n  cota da Groq esgotada; Whisper local assume blocos a partir do fim")
                    local = threading.Thread(target=local_worker, daemon=True)
                    local.start()
                time.sleep(1)
        else:
            local_worker()
        print()

    # junta os blocos: na sobreposição de cada emenda, alinha o texto dos dois lados e corta no meio da maior
    # sequência de palavras iguais (os tempos dos dois blocos divergem um pouco, o texto não). Sem acordo,
    # corta na maior pausa do bloco anterior.
    from difflib import SequenceMatcher

    norm = lambda x: re.sub(r"\W", "", x.lower())  # noqa: E731
    chunks = [json.loads((cdir / f"{i:04d}.json").read_text())["words"] for i in range(n)]
    keep = [[0, len(c)] for c in chunks]  # fatia [início, fim) de cada bloco
    for i in range(1, n):
        border, prev, nxt = i * ASR_CHUNK, chunks[i - 1], chunks[i]
        pi = [k for k, w in enumerate(prev) if w["s"] >= border - ASR_OVERLAP]
        ni = [k for k, w in enumerate(nxt) if w["s"] <= border + ASR_OVERLAP]
        m = SequenceMatcher(None, [norm(prev[k]["w"]) for k in pi], [norm(nxt[k]["w"]) for k in ni],
                            autojunk=False).find_longest_match(0, len(pi), 0, len(ni))
        if m.size >= 2:
            half = m.size // 2
            keep[i - 1][1] = pi[m.a + half]          # anterior vai até antes da palavra do meio
            keep[i][0] = max(keep[i][0], ni[m.b + half])  # seguinte começa nela
        else:
            near = [k for k in pi if prev[k]["s"] <= border + ASR_OVERLAP - 0.5]
            gaps = [(prev[b2]["s"] - prev[a2]["e"], (prev[a2]["e"] + prev[b2]["s"]) / 2) for a2, b2 in zip(near, near[1:])]
            cut = max(gaps)[1] if gaps else border
            keep[i - 1][1] = next((k for k, w in enumerate(prev) if w["s"] >= cut), len(prev))
            keep[i][0] = max(keep[i][0], next((k for k, w in enumerate(nxt) if w["s"] >= cut), len(nxt)))
    words = [w for c, (lo, hi) in zip(chunks, keep) for w in c[lo:hi]]
    for k in range(1, len(words)):  # mantém a ordem do texto; só impede tempo andando para trás na emenda
        if words[k]["s"] < words[k - 1]["s"]:
            words[k] = {**words[k], "s": words[k - 1]["s"], "e": max(words[k]["e"], words[k - 1]["s"])}
    # a Groq às vezes repete a palavra com tempo sobreposto; repetição falada de verdade não se sobrepõe
    words = [w for k, w in enumerate(words)
             if not (k and norm(w["w"]) == norm(words[k - 1]["w"]) and w["s"] < words[k - 1]["e"] - 0.01)]
    segs, cur = [], []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > 1.0:
            segs.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w["w"]) or len(cur) >= 40:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    engines = sorted({json.loads((cdir / f"{i:04d}.json").read_text())["engine"] for i in range(n)})
    shutil.rmtree(cdir)
    return {"language": state["lang"] or "?", "duration": total, "model": "+".join(engines),
            "segments": [{"s": ws[0]["s"], "e": ws[-1]["e"], "text": " ".join(x["w"] for x in ws), "words": ws}
                         for ws in segs]}


def sentences(transcript: dict) -> list[dict]:
    """Reagrupa palavras em frases pela pontuação final, para o agente ler e escolher cortes."""
    out, cur = [], []
    for seg in transcript["segments"]:
        for w in seg["words"]:
            if cur and w["s"] - cur[-1]["e"] > 1.2:
                out.append(cur)
                cur = []
            cur.append(w)
            if re.search(r"[.!?…]$", w["w"]) or len(cur) >= 45:
                out.append(cur)
                cur = []
    if cur:
        out.append(cur)
    return [{"s": ws[0]["s"], "e": ws[-1]["e"], "text": " ".join(w["w"] for w in ws)} for ws in out]


def cmd_prepare(a: argparse.Namespace) -> None:
    import time

    t0 = time.time()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    src = Path(a.source)
    meta: dict = {"source": a.source}
    if src.exists():
        video = out / f"source{src.suffix.lower()}"
        if not video.exists():
            os.symlink(src.resolve(), video)
    else:
        cookies = Path(a.cookies) if a.cookies else DEFAULT_COOKIES
        meta["webpage_url"] = a.source
        print("Baixando...")
        if a.downloader == "apify":
            video = apify_prepare(a.source, out, a, meta)
        else:
            try:
                if a.full_video:
                    video = download(a.source, out, cookies, a.max_height)
                else:
                    video, info = download_audio(a.source, out, cookies)
                    meta.update({k: info.get(k) for k in ("id", "title", "channel", "upload_date", "description")})
                    meta["remote"] = {"url": a.source, "max_height": a.max_height}
            except Blocked as err:
                if a.downloader == "ytdlp":
                    sys.exit(f"YouTube bloqueou o IP da VPS. Use cookies em {DEFAULT_COOKIES} ou --downloader apify.\n{err}")
                print("  yt-dlp bloqueado pelo YouTube; usando Apify (pago).")
                video = apify_prepare(a.source, out, a, meta)
        info_file = out / "source.info.json"
        if info_file.exists():
            info = json.loads(info_file.read_text())
            meta.update({k: info.get(k) for k in ("id", "title", "channel", "upload_date", "webpage_url", "description")})
            info_file.unlink()
    meta["probe"] = ffprobe(video)
    meta["video"] = None if meta.get("remote") else video.name
    if not meta["probe"]["has_audio"]:
        sys.exit("O arquivo não tem faixa de áudio; não há o que transcrever.")
    print(f"  pronto para transcrever em {time.time() - t0:.0f}s")

    import threading

    # WAV 16 kHz em paralelo: só o Whisper local e a localização de trechos precisam dele
    audio, wav_ready = out / "audio.wav", threading.Event()
    if audio.exists() and abs(ffprobe_duration(audio) - meta["probe"]["duration"]) < 1.5:
        wav_ready.set()
    else:
        def extract() -> None:
            try:
                run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
            finally:
                wav_ready.set()  # mesmo se falhar, ninguém fica esperando para sempre
        threading.Thread(target=extract, daemon=True).start()
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    t1 = time.time()
    engine = "Groq (local se a cota acabar)" if a.asr != "local" and read_env("GROQ_API_KEY") else f"faster-whisper {a.model} local"
    print(f"Transcrevendo com {engine}...")
    tr = transcribe(video, audio, wav_ready, meta["probe"]["duration"], out, a.language, a.asr, a.model, a.beam)
    print(f"  transcrição em {time.time() - t1:.0f}s ({tr['model']})")
    wav_ready.wait()
    if meta.get("remote"):
        video.unlink()  # só o áudio interessava; a imagem vem no render (trecho ou vídeo da Apify)
    (out / "transcript.json").write_text(json.dumps(tr, ensure_ascii=False))
    if str(tr["language"]).lower() not in PT_CODES:
        print(f"  vídeo em '{tr['language']}': no render, as legendas serão traduzidas para PT-BR")
    sents = sentences(tr)
    lines = [f"# {meta.get('title') or src.name} | idioma {tr['language']} | duração {fmt_ts(tr['duration'])}",
             "# formato: [início-fim em segundos] (mm:ss) frase"]
    sig = signal_lines(sents)
    if sig:
        lines += ["# pistas automáticas (só indicam onde olhar; a escolha é editorial):", *sig]
    lines += [f"[{s['s']:.2f}-{s['e']:.2f}] ({fmt_ts(s['s'])}) {s['text']}" for s in sents]
    (out / "transcript.txt").write_text("\n".join(lines) + "\n")
    print(json.dumps({"out": str(out), "language": tr["language"], "duration": round(tr["duration"], 1),
                      "sentences": len(sents), "seconds": round(time.time() - t0),
                      "transcript": str(out / "transcript.txt")}, ensure_ascii=False))


# ---------------------------------------------------------------- render: tempo e palavras

def all_words(transcript: dict) -> list[dict]:
    return [w for seg in transcript["segments"] for w in seg["words"]]


def _ends_sentence(w: dict) -> bool:
    return bool(re.search(r"[.!?…]$", w["w"]))


def snap(start: float, end: float, words: list[dict]) -> tuple[float, float, dict]:
    """Ajusta início/fim a limites de frase (até 3 s) e de palavra, sem cortar fala no meio.
    Devolve também as bordas das palavras vizinhas, para refinar o corte pelo áudio."""
    idx = [i for i, w in enumerate(words) if w["e"] > start + 0.05 and w["s"] < end - 0.05]
    if not idx:
        raise ValueError(f"nenhuma palavra entre {start} e {end}")
    a, b = idx[0], idx[-1]
    # início: volta até o começo da frase (pontuação anterior ou pausa >= 0,8 s), no máximo 3 s
    i = a
    while i > 0 and not _ends_sentence(words[i - 1]) and words[i]["s"] - words[i - 1]["e"] < 0.8 \
            and words[a]["s"] - words[i - 1]["s"] <= 3.0:
        i -= 1
    if i > 0 and not (_ends_sentence(words[i - 1]) or words[i]["s"] - words[i - 1]["e"] >= 0.8):
        i = a  # não achou começo de frase perto: mantém a palavra proposta
    a = i
    # fim: avança até terminar a frase, no máximo 3 s
    j = b
    while j + 1 < len(words) and not _ends_sentence(words[j]) and words[j + 1]["s"] - words[j]["e"] < 0.8 \
            and words[j + 1]["e"] - words[b]["e"] <= 3.0:
        j += 1
    if _ends_sentence(words[j]) or j + 1 >= len(words) or words[j + 1]["s"] - words[j]["e"] >= 0.8:
        b = j
    prev_end = words[a - 1]["e"] if a > 0 else 0.0
    s = max(prev_end, words[a]["s"] - 0.12, 0.0)
    nxt = words[b + 1]["s"] if b + 1 < len(words) else words[b]["e"] + 1.0
    e = min(words[b]["e"] + 0.35, max(words[b]["e"] + 0.05, nxt - 0.03))
    return s, e, {"first_s": words[a]["s"], "prev_e": prev_end, "last_e": words[b]["e"], "next_s": nxt}


def quiet_point(media: Path, lo: float, hi: float, prefer: float) -> float | None:
    """Instante mais silencioso (janelas de 10 ms) entre lo e hi. O tempo das palavras do Whisper erra por
    alguns centésimos; cortar ali deixa o fim da palavra vizinha no clipe (o "engasgo" no começo)."""
    import numpy as np

    lo = max(0.0, lo)
    if hi - lo < 0.03:
        return None
    a = read_pcm(media, lo, hi - lo, 8000)
    n = len(a) // 80
    if n < 3:
        return None
    rms = np.sqrt((a[:n * 80].reshape(n, 80) ** 2).mean(1))
    quiet = np.where(rms <= rms.min() * 1.5 + 30)[0]  # perto do mínimo conta como silêncio
    times = lo + (quiet + 0.5) * 0.01
    return float(times[np.argmin(np.abs(times - prefer))])


def refine_cut(media: Path, s: float, e: float, b: dict) -> tuple[float, float]:
    """Move início e fim para o ponto mais silencioso perto das palavras de borda."""
    qs = quiet_point(media, max(b["prev_e"] - 0.05, b["first_s"] - 0.40), b["first_s"] + 0.02, b["first_s"] - 0.10)
    qe = quiet_point(media, b["last_e"] - 0.02, min(b["next_s"] + 0.02, b["last_e"] + 0.45), b["last_e"] + 0.15)
    return (qs if qs is not None else s), (qe if qe is not None else e)


TRANSLATE_MODEL = "openai/gpt-oss-120b"  # Groq; mesma chave da transcrição
PT_CODES = {"pt", "pt-br", "pt_br", "portuguese"}


def caption_phrases(words: list[dict]) -> list[dict]:
    """Agrupa palavras em frases inteiras para traduzir (pontuação, pausa de 0,8 s ou 30 palavras); a quebra
    em blocos curtos de legenda acontece depois, já sobre o texto traduzido."""
    out, cur = [], []
    for w in words:
        if cur and (w["s"] - cur[-1]["e"] > 0.8 or len(cur) >= 30):
            out.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w["w"]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"s": ws[0]["s"], "e": ws[-1]["e"], "text": " ".join(x["w"] for x in ws)} for ws in out]


def translate_texts(texts: list[str], src_lang: str, cache_path: Path) -> list[str]:
    # cada trecho vai com o anterior e o seguinte como contexto (não traduzidos), para não repetir nem perder
    # palavras quando a frase foi partida no meio
    """Traduz frases de legenda para PT-BR pela Groq, com cache em translations.json (editável à mão:
    corrija a tradução lá e rode o render de novo)."""
    import urllib.request

    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = list(dict.fromkeys(t for t in texts if t not in cache))
    ctx = {t: (texts[k - 1] if k else "", texts[k + 1] if k + 1 < len(texts) else "") for k, t in enumerate(texts)}
    system = ("Você traduz legendas de vídeo para português do Brasil. Regras: tradução fiel ao sentido, em "
              "português falado e natural (como um brasileiro diria), curta o bastante para legenda; mantenha "
              "nomes, marcas, números e termos técnicos consagrados; não acrescente nem resuma nada; "
              "palavrões continuam palavrões. Cada item é um trecho seguido da fala, pode começar ou terminar "
              "no meio de uma ideia: traduza só o campo texto; os campos antes e depois são só contexto, para "
              "você não repetir nem omitir palavras na emenda. Responda só JSON: "
              "{\"t\": [{\"id\": <id>, \"pt\": \"<tradução>\"}, ...]} com todos os ids recebidos.")

    def ask(items: list[tuple[int, str]]) -> dict[int, str]:
        body = {"model": TRANSLATE_MODEL, "temperature": 0.2, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(
                                 {"idioma_origem": src_lang, "itens": [{"id": i, "antes": ctx[t][0], "texto": t,
                                                                       "depois": ctx[t][1]} for i, t in items]},
                                 ensure_ascii=False)}]}
        req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers={
                                         "Authorization": f"Bearer {read_env('GROQ_API_KEY')}",
                                         "Content-Type": "application/json", "User-Agent": "youtube-clips/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                rows = json.loads(json.loads(r.read())["choices"][0]["message"]["content"]).get("t", [])
            return {int(x["id"]): str(x["pt"]).strip() for x in rows if isinstance(x, dict) and "id" in x and x.get("pt")}
        except Exception as err:  # noqa: BLE001
            print(f"    tradução falhou ({err})")
            return {}

    pending = list(enumerate(todo))
    for attempt in range(4):
        if not pending:
            break
        size = 30 if attempt < 2 else 1  # nas últimas tentativas, um item por vez
        for k in range(0, len(pending), size):
            got = ask(pending[k:k + size])
            cache.update({todo[i]: got[i] for i, _ in pending[k:k + size] if i in got})
        pending = [(i, t) for i, t in pending if t not in cache]
    if pending:
        sys.exit("Não consegui traduzir as legendas pela Groq; rode de novo ou use style.translate=false.")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    return [cache[t] for t in texts]


def translated_words(words: list[dict], src_lang: str, cache_path: Path) -> list[dict]:
    """Troca as palavras da fala pela tradução, frase a frase. Cada palavra traduzida recebe uma fatia do
    tempo da frase original proporcional ao seu tamanho, para o destaque continuar acompanhando a fala."""
    phrases = caption_phrases(words)
    out = []
    for ph, tr in zip(phrases, translate_texts([p["text"] for p in phrases], src_lang, cache_path)):
        toks = tr.split()
        if not toks:
            continue
        weights = [len(t) + 2 for t in toks]
        t, step = ph["s"], (ph["e"] - ph["s"]) / sum(weights)
        for tok, wgt in zip(toks, weights):
            out.append({"w": tok, "s": t, "e": t + wgt * step, "p": 1.0})
            t += wgt * step
    return out


def words_in(words: list[dict], s: float, e: float) -> list[dict]:
    # a palavra pertence ao trecho se o meio dela estiver dentro: a última sílaba da frase anterior, que
    # termina bem no corte, não vira legenda solta
    return [w for w in words if s <= (w["s"] + w["e"]) / 2 < e]


SIGNAL_RE = re.compile(
    r"\b(nunca|sempre|ninguém|todo mundo|segredo|erro|errado|verdade|mentira|absurdo|incrível|"
    r"cuidado|pare|porque|mas|só que|descobri|aprendi|perdi|ganhei|milh(ão|ões)|mil|"
    r"never|always|nobody|secret|mistake|actually|because|wrong|truth|insane|crazy|"
    r"learned|realized|lost|won|million)\b", re.I)


def signal_lines(sents: list[dict], top: int = 15) -> list[str]:
    """Pistas determinísticas (não decidem nada): perguntas, números, pausas e palavras de gatilho."""
    scored = []
    for k, s in enumerate(sents):
        reasons, score = [], 0.0
        if "?" in s["text"]:
            score += 1.5; reasons.append("pergunta")
        if "!" in s["text"]:
            score += 1; reasons.append("exclamação")
        if re.search(r"\d", s["text"]):
            score += 1; reasons.append("número")
        hits = SIGNAL_RE.findall(s["text"])
        if hits:
            score += min(2 * len(hits), 4); reasons.append("gatilho")
        if k and s["s"] - sents[k - 1]["e"] >= 1.0:
            score += 1; reasons.append("pausa antes")
        if score >= 2:
            scored.append((score, s, reasons))
    scored.sort(key=lambda x: -x[0])
    return [f"# [{s['s']:.2f}] score={sc:g} ({', '.join(r)}): {s['text'][:110]}" for sc, s, r in scored[:top]]


# ---------------------------------------------------------------- render: análise visual

def read_frames(video: Path, start: float, dur: float, fps: float, width: int, height: int):
    """Decodifica um trecho com ffmpeg (seek preciso) e entrega frames BGR."""
    import numpy as np

    vf = f"fps={fps},scale={width}:{height}:flags=bicubic"
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{dur:.3f}",
         "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE,
    )
    size = width * height * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(height, width, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def analyze(video: Path, probe: dict, start: float, dur: float) -> dict:
    """Amostra o trecho, detecta rostos (YuNet) e cortes de cena (histograma HSV)."""
    import cv2

    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    aw = 640
    ah = int(round(probe["height"] * aw / probe["width"] / 2) * 2)
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (aw, ah), FACE_MIN_SCORE, 0.3, 50)
    samples, prev_hist = [], None
    for i, frame in enumerate(read_frames(video, start, dur, ANALYSIS_FPS, aw, ah)):
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        cut = prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < SCENE_CUT_CORR
        prev_hist = hist
        _, faces = det.detect(frame)
        fl = []
        if faces is not None:
            for f in faces:
                x, y, w, h, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])
                if w * h >= (aw * ah) * 0.004:  # descarta rostos minúsculos ao fundo
                    fl.append({"cx": (x + w / 2) / aw, "cy": (y + h / 2) / ah, "w": w / aw, "h": h / ah, "score": score})
        samples.append({"t": i / ANALYSIS_FPS, "cut": cut, "faces": fl})
    return {"samples": samples}


def pick_face(faces: list[dict], prev_cx: float | None) -> dict | None:
    if not faces:
        return None
    biggest = max(faces, key=lambda f: f["w"] * f["h"])
    if prev_cx is None:
        return biggest
    # mantém o mesmo rosto se ele ainda for relevante (evita saltar entre pessoas)
    near = min(faces, key=lambda f: abs(f["cx"] - prev_cx))
    if abs(near["cx"] - prev_cx) < 0.08 and near["w"] * near["h"] >= 0.5 * biggest["w"] * biggest["h"]:
        return near
    return biggest


def plan_shots(analysis: dict, dur: float, layout: str, src_aspect: float) -> list[dict]:
    """Divide em planos por corte de cena e decide modo/enquadramento de cada plano."""
    samples = analysis["samples"]
    if not samples:
        return [{"t0": 0.0, "t1": dur, "mode": "blur", "targets": []}]
    bounds = [0] + [i for i, s in enumerate(samples) if s["cut"]] + [len(samples)]
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        chunk, targets, prev = samples[a:b], [], None
        for s in chunk:
            f = pick_face(s["faces"], prev)
            if f:
                prev = f["cx"]
            targets.append((s["t"], f["cx"] if f else None, len(s["faces"])))
        coverage = sum(1 for t in targets if t[1] is not None) / len(targets)
        multi = sum(1 for t in targets if t[2] >= 2) / len(targets)
        xs = sorted(t[1] for t in targets if t[1] is not None)
        spread = (xs[-1] - xs[0]) if xs else 0
        mode = layout
        if layout == "auto":
            # duas pessoas afastadas no mesmo plano não cabem num recorte 9:16: usa fundo desfocado
            mode = "track" if coverage >= TRACK_MIN_COVERAGE and not (multi > 0.5 and spread > 0.35) else "blur"
        if src_aspect <= 9 / 16 + 0.01:
            mode = "fit"
        shots.append({"t0": chunk[0]["t"], "t1": samples[b]["t"] if b < len(samples) else dur,
                      "mode": mode, "targets": targets, "coverage": round(coverage, 2)})
    # planos muito curtos (<0,6 s) herdam o modo do anterior para evitar piscar
    for i in range(1, len(shots)):
        if shots[i]["t1"] - shots[i]["t0"] < 0.6:
            shots[i]["mode"] = shots[i - 1]["mode"]
    return shots


def camera_path(shot: dict, fps: int, src_w: int, crop_w: int) -> list[float]:
    """Trajetória horizontal (centro do recorte, px de origem) para cada frame do plano."""
    import numpy as np

    n = max(1, int(round((shot["t1"] - shot["t0"]) * fps)))
    known = [(t - shot["t0"], cx * src_w) for t, cx, _ in shot["targets"] if cx is not None]
    half = crop_w / 2
    if not known:
        return [src_w / 2] * n
    ts = np.array([k[0] for k in known])
    xs = np.array([k[1] for k in known])
    frame_t = np.arange(n) / fps
    target = np.interp(frame_t, ts, xs)
    lo, hi = half, src_w - half
    if np.std(xs) / src_w < STATIC_STD:
        return [float(np.clip(np.median(xs), lo, hi))] * n
    cam, path, dz = float(np.median(xs[: max(1, len(xs) // 4)])), [], DEAD_ZONE * src_w
    for x in target:
        diff = x - cam
        if abs(diff) > dz:
            cam += CAM_ALPHA * (diff - np.sign(diff) * dz)
        path.append(float(np.clip(cam, lo, hi)))
    return path


# ---------------------------------------------------------------- render: legendas ASS

def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def clean_word(w: str, upper: bool) -> str:
    w = re.sub(r"^[\"'“”‘’«»(\[]+|[\"'“”‘’«»)\],.;:]+$", "", w.strip())
    return w.upper() if upper else w


def caption_blocks(words: list[dict], max_chars: int, max_dur: float) -> list[list[dict]]:
    blocks, cur = [], []
    for w in words:
        if cur:
            length = sum(len(x["t"]) + 1 for x in cur) + len(w["t"])
            if length > max_chars or w["e"] - cur[0]["s"] > max_dur or w["s"] - cur[-1]["e"] > 0.6 \
                    or re.search(r"[.!?…]$", cur[-1]["raw"]):
                blocks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        blocks.append(cur)
    return blocks


def wrap(text: str, width: int) -> str:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines)


def build_ass(words: list[dict], clip_dur: float, hook: str | None, st: dict) -> str:
    """words já em tempo de saída (segundos desde o início do clipe)."""
    upper = st["uppercase"]
    ws = []
    for w in words:
        t = clean_word(w["w"], upper)
        if t:
            ws.append({"t": t, "raw": w["w"], "s": max(0.0, w["s"]), "e": min(clip_dur, w["e"])})
    hl, base = ass_color(st["highlight_color"]), ass_color(st["text_color"])
    margin_v = int(OUT_H * st["caption_position"])
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{st['font']},{st['font_size']},{base},{base},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{st['outline']},3,2,90,90,{margin_v},1
Style: Hook,{st['font']},{st['hook_font_size']},{ass_color(st['hook_text_color'])},&H00FFFFFF,{ass_color(st['hook_box_color'])},&H00000000,-1,0,0,0,100,100,0,0,3,26,0,8,70,70,{int(OUT_H * st['hook_position'])},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    blocks = caption_blocks(ws, st["max_chars"], st["max_block_seconds"])
    for bi, block in enumerate(blocks):
        block_end = block[-1]["e"]
        if bi + 1 < len(blocks) and blocks[bi + 1][0]["s"] - block_end < 0.35:
            block_end = blocks[bi + 1][0]["s"]  # evita piscar entre blocos próximos
        for i, w in enumerate(block):
            t0 = w["s"] if i else block[0]["s"]
            t1 = block[i + 1]["s"] if i + 1 < len(block) else block_end
            if t1 - t0 < 0.01:
                continue
            # destaque só por cor: escalar uma palavra reflui a linha centralizada e faz o texto tremer
            parts = [f"{{\\c{hl}}}{ass_escape(x['t'])}{{\\c{base}}}" if j == i else ass_escape(x["t"])
                     for j, x in enumerate(block)]
            entrance = f"{{\\fscx88\\fscy88\\t(0,120,\\fscx100\\fscy100)}}" if i == 0 and st["pop_in"] else ""
            events.append(f"Dialogue: 1,{ass_time(t0)},{ass_time(t1)},Caption,,0,0,0,,{entrance}{' '.join(parts)}")
    if hook:
        hook_text = ass_escape(hook.upper() if st["hook_uppercase"] else hook)
        events.append(f"Dialogue: 2,{ass_time(0.05)},{ass_time(min(st['hook_seconds'], clip_dur))},Hook,,0,0,0,,"
                      f"{{\\fad(150,250)\\fscx92\\fscy92\\t(0,160,\\fscx100\\fscy100)}}{wrap(hook_text, st['hook_wrap'])}")
    return header + "\n".join(events) + "\n"


# ---------------------------------------------------------------- render: composição e encode

def compose_frame(frame, mode: str, cx: float, crop_w: int, cv2, np):
    h, w = frame.shape[:2]
    if mode == "track":
        x0 = int(round(min(max(cx - crop_w / 2, 0), w - crop_w)))
        return cv2.resize(frame[:, x0:x0 + crop_w], (OUT_W, OUT_H), interpolation=cv2.INTER_CUBIC)
    # blur/fit: vídeo inteiro na largura total sobre fundo desfocado (recorte central, sem distorcer)
    fg_h = int(round(h * OUT_W / w))
    if fg_h > OUT_H:
        fg_w, fg_h = int(round(w * OUT_H / h)), OUT_H
    else:
        fg_w = OUT_W
    cw = min(w, int(h * 9 / 16))
    center = frame[:, (w - cw) // 2:(w - cw) // 2 + cw]
    small = cv2.resize(center, (OUT_W // 8, OUT_H // 8), interpolation=cv2.INTER_AREA)
    bg = cv2.GaussianBlur(small, (0, 0), 5)
    bg = cv2.resize((bg * 0.5).astype(np.uint8), (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
    fg = cv2.resize(frame, (fg_w, fg_h), interpolation=cv2.INTER_CUBIC)
    y0, x0 = (OUT_H - fg_h) // 2, (OUT_W - fg_w) // 2
    bg[y0:y0 + fg_h, x0:x0 + fg_w] = fg
    return bg


def render_clip(video: Path, probe: dict, ranges: list[tuple[float, float]], shots_per_range: list[list[dict]],
                ass_path: Path, out_path: Path, encode: dict) -> dict:
    """ranges: trechos da fonte tocados em sequência (teaser opcional + corpo)."""
    import cv2
    import numpy as np

    src_w, src_h = probe["width"], probe["height"]
    crop_w = min(src_w, int(round(src_h * 9 / 16 / 2) * 2))
    plans = []
    for (s, e), shots in zip(ranges, shots_per_range):
        plan = []
        for shot in shots:
            plan += [(shot["mode"], x) for x in camera_path(shot, OUT_FPS, src_w, crop_w)]
        n = int(round((e - s) * OUT_FPS))
        plan = plan or [("blur", src_w / 2)]
        plans.append(plan + [plan[-1]] * max(0, n - len(plan)))

    fonts = str(FONTS_DIR).replace(":", r"\:")
    ass = str(ass_path).replace(":", r"\:").replace("'", r"\'")
    audio_inputs = sum((["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(video)] for s, e in ranges), [])
    # fade curto em cada trecho: sem estalo na entrada, na saída nem na emenda teaser -> corpo
    def audio_graph(first_input: int) -> str:
        chains = "".join(f"[{first_input + k}:a:0]aresample=48000,afade=t=in:d=0.03,"
                         f"afade=t=out:st={max(0.0, e - s - 0.06):.3f}:d=0.06[a{k}];" for k, (s, e) in enumerate(ranges))
        return chains + "".join(f"[a{k}]" for k in range(len(ranges))) + f"concat=n={len(ranges)}:v=0:a=1"

    loud = ""
    if encode["loudnorm"]:
        # duas passadas: mede o clipe inteiro e aplica ganho linear (a passada única ajusta o volume enquanto toca)
        out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *audio_inputs, "-filter_complex",
                              audio_graph(0) + ",loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out)
        if m:
            st = json.loads(m.group(0))
            loud = (f",loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={st['input_i']}:measured_TP={st['input_tp']}"
                    f":measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}"
                    f":offset={st['target_offset']}:linear=true")
        else:
            loud = ",loudnorm=I=-14:TP=-1.5:LRA=11"
    fc = (f"[0:v]unsharp=5:5:0.5:5:5:0.0,subtitles='{ass}':fontsdir='{fonts}'[v];"
          f"{audio_graph(1)}{loud},aresample=48000[a]")
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(OUT_FPS), "-i", "-",
         *audio_inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", encode["preset"], "-crf", str(encode["crf"]), "-pix_fmt", "yuv420p",
         "-profile:v", "high", "-level", "4.1", "-r", str(OUT_FPS), "-g", "60",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out_path)],
        stdin=subprocess.PIPE,
    )
    count, modes = 0, {}
    for (s, e), plan in zip(ranges, plans):
        for i, frame in enumerate(read_frames(video, s, e - s, OUT_FPS, src_w, src_h)):
            mode, cx = plan[min(i, len(plan) - 1)]
            enc.stdin.write(compose_frame(frame, mode, cx, crop_w, cv2, np).tobytes())
            modes[mode] = modes.get(mode, 0) + 1
            count += 1
    enc.stdin.close()
    if enc.wait() != 0:
        raise RuntimeError(f"ffmpeg falhou ao gerar {out_path.name}")
    return {"frames": count, "modes": {k: round(v / max(1, count), 2) for k, v in modes.items()}}


def contact_sheet(clip: Path, dur: float, out: Path) -> None:
    times = [min(dur - 0.1, x) for x in (1.0, dur * 0.35, dur * 0.65, dur - 1.0)]
    tiles = []
    for k, t in enumerate(times):
        tile = out.with_name(f".{out.stem}-{k}.jpg")
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", str(clip), "-frames:v", "1",
             "-vf", "scale=270:480", str(tile)])
        tiles.append(tile)
    inputs = sum((["-i", str(t)] for t in tiles), [])
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", f"hstack=inputs={len(tiles)}", str(out)])
    for t in tiles:
        t.unlink()


DEFAULT_STYLE = {
    "font": "Montserrat ExtraBold", "font_size": 92, "outline": 8, "uppercase": True,
    "text_color": "#FFFFFF", "highlight_color": "#FFD600", "pop_in": True,
    "caption_position": 0.27, "max_chars": 18, "max_block_seconds": 1.6,
    "hook_seconds": 4.0, "hook_font_size": 76, "hook_text_color": "#111111", "hook_box_color": "#FFFFFF",
    "hook_uppercase": False, "hook_wrap": 20, "hook_position": 0.12, "translate": "auto",
}
DEFAULT_ENCODE = {"preset": "veryfast", "crf": 20, "loudnorm": True}  # veryfast: ~2x o fast, mesmo tamanho
MIN_DUR, MAX_DUR = 12.0, 90.0


def validate_plan(plan: dict, total: float) -> list[str]:
    problems, spans = [], []
    for idx, c in enumerate(plan.get("clips", []), 1):
        s, e = float(c.get("start", -1)), float(c.get("end", -1))
        if not (0 <= s < e <= total + 0.5):
            problems.append(f"clipe {idx}: intervalo inválido {s}-{e} (vídeo tem {total:.1f}s)")
            continue
        if not (MIN_DUR <= e - s <= MAX_DUR):
            problems.append(f"clipe {idx}: duração {e - s:.1f}s fora de {MIN_DUR:g}-{MAX_DUR:g}s")
        t = c.get("teaser")
        if t and not (s + 2 <= float(t[0]) < float(t[1]) <= e and float(t[1]) - float(t[0]) <= 6):
            problems.append(f"clipe {idx}: teaser {t} precisa estar dentro do clipe, após 2 s e ter até 6 s")
        for j, (ps, pe) in enumerate(spans, 1):
            ov = min(e, pe) - max(s, ps)
            if ov > 0.25 * min(e - s, pe - ps):
                problems.append(f"clipe {idx}: sobrepõe mais de 25% o clipe {j}")
        spans.append((s, e))
    return problems


def cmd_render(a: argparse.Namespace) -> None:
    d = Path(a.dir).resolve()
    plan = json.loads((d / a.plan).read_text())
    meta = json.loads((d / "meta.json").read_text())
    transcript = json.loads((d / "transcript.json").read_text())
    remote = meta.get("remote")
    if remote and remote.get("apify_run"):
        # o vídeo baixou na Apify em paralelo com a transcrição; busca uma vez e passa a usar o arquivo local
        print("Buscando o vídeo na Apify (aguarda se ainda estiver baixando)...")
        item = apify_result(remote["apify_run"], "vídeo")
        apify_fetch(apify_video_link(item), d / "source.mp4")
        meta.update({"video": "source.mp4", "probe": ffprobe(d / "source.mp4"), "apify_video_usd": item.get("_usd")})
        meta.pop("remote")
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        remote = None
    video = d / meta["video"] if meta.get("video") else None
    if not remote and not (video and video.exists()):
        sys.exit(f"Vídeo-fonte ausente em {video}; rode prepare de novo.")
    probe = meta["probe"]
    problems = validate_plan(plan, probe["duration"])
    if problems and not a.force:
        sys.exit("Plano com problemas (corrija ou use --force):\n- " + "\n- ".join(problems))
    style = {**DEFAULT_STYLE, **plan.get("style", {})}
    encode = {**DEFAULT_ENCODE, **plan.get("encode", {})}
    words = all_words(transcript)
    # vídeo em outro idioma: legenda traduzida para PT-BR (style.translate: "auto" | true | false)
    lang = str(transcript.get("language", "")).lower()
    translate = style.get("translate", "auto")
    translate = translate is True or (translate == "auto" and lang not in PT_CODES)
    if translate:
        print(f"Idioma do vídeo: {lang}; legendas serão traduzidas para PT-BR (revisáveis em translations.json)")
    # áudio de referência na linha do tempo da fonte, para achar os pontos de silêncio dos cortes
    cut_media = d / "audio.wav" if (d / "audio.wav").exists() else video
    clips_dir = d / "clips"
    clips_dir.mkdir(exist_ok=True)
    only = {int(x) for x in a.only.split(",")} if a.only else None
    results = []
    prev = d / "render.json"
    if only and prev.exists():
        results = [r for r in json.loads(prev.read_text()) if r["index"] not in only]
    for idx, clip in enumerate(plan["clips"], 1):
        if only and idx not in only:
            continue
        start, end, edges = snap(float(clip["start"]), float(clip["end"]), words)
        start, end = refine_cut(cut_media, start, end, edges)
        ranges = [(start, end)]
        if clip.get("teaser"):
            ts, te, tedges = snap(float(clip["teaser"][0]), float(clip["teaser"][1]), words)
            ts, te = refine_cut(cut_media, ts, te, tedges)
            ranges.insert(0, (ts, min(te, ts + 6.0)))
        name = f"{idx:02d}-{slugify(clip.get('title') or clip.get('hook') or 'clip')}"
        dur = sum(e - s for s, e in ranges)
        print(f"[{idx}/{len(plan['clips'])}] {name}  {fmt_ts(start)}-{fmt_ts(end)} ({dur:.1f}s"
              f"{', com teaser' if len(ranges) > 1 else ''})")
        layout = clip.get("layout", plan.get("layout", "auto"))
        src, src_probe, shift = video, probe, 0.0
        if remote:
            want = max(0.0, min(r[0] for r in ranges) - 4.0)  # folga para o keyframe anterior
            src = clips_dir / f".src-{idx:02d}.mp4"
            print(f"    baixando trecho em até {remote['max_height']}p...")
            download_section(remote["url"], want, max(r[1] for r in ranges) + 1.0, src,
                             Path(a.cookies) if a.cookies else DEFAULT_COOKIES, remote["max_height"])
            src_probe = ffprobe(src)
            shift = locate_section(src, d / "audio.wav", want)
            print(f"    trecho começa em {fmt_ts(shift)} (pedido {fmt_ts(want)})")
        vranges = [(s - shift, e - shift) for s, e in ranges]
        shots_per_range, out_words, offset = [], [], 0.0
        for (s, e), (vs, ve) in zip(ranges, vranges):
            shots_per_range.append(plan_shots(analyze(src, src_probe, vs, ve - vs), ve - vs, layout,
                                              src_probe["width"] / src_probe["height"]))
            ws = words_in(words, s, e)
            if translate:
                ws = translated_words(ws, lang, d / "translations.json")
            out_words += [{**w, "s": w["s"] - s + offset, "e": min(w["e"], e) - s + offset} for w in ws]
            offset += e - s
        ass_path = clips_dir / f"{name}.ass"
        hook = clip.get("hook") if style["hook_seconds"] > 0 else None
        ass_path.write_text(build_ass(out_words, dur, hook, style), encoding="utf-8")
        out_path = clips_dir / f"{name}.mp4"
        stats = render_clip(src, src_probe, vranges, shots_per_range, ass_path, out_path, encode)
        if remote and not remote.get("direct"):
            src.unlink(missing_ok=True)
        contact_sheet(out_path, dur, clips_dir / f"{name}-qa.jpg")
        if not a.keep_ass:
            ass_path.unlink()
        results.append({"index": idx, "file": str(out_path), "qa": str(clips_dir / f"{name}-qa.jpg"),
                        "start": round(start, 2), "end": round(end, 2), "duration": round(dur, 1),
                        "teaser": [round(x, 2) for x in ranges[0]] if len(ranges) > 1 else None,
                        "shots": sum(len(x) for x in shots_per_range), **stats,
                        **{k: clip.get(k) for k in ("title", "hook", "score", "reason", "post_caption", "hashtags")}})
        print(f"    ok: {stats['modes']}")
    results.sort(key=lambda r: r["index"])
    prev.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    write_publication(d, meta, results)
    print(json.dumps({"rendered": len(results), "dir": str(clips_dir), "summary": str(d / "publicacao.md")},
                     ensure_ascii=False))


def write_publication(d: Path, meta: dict, results: list[dict]) -> None:
    src = meta.get("webpage_url") or meta.get("source")
    lines = [f"# Clipes de: {meta.get('title') or src}", "", f"Fonte: {src}", ""]
    for r in sorted(results, key=lambda r: -(r.get("score") or 0)):
        lines += [f"## {r['index']:02d}. {r.get('title') or ''}",
                  f"- Arquivo: `{Path(r['file']).name}` ({r['duration']}s, trecho {fmt_ts(r['start'])}-{fmt_ts(r['end'])})",
                  f"- Nota: {r.get('score', '-')}/100. {r.get('reason') or ''}",
                  f"- Gancho na tela: {r.get('hook') or '(sem)'}", ""]
        if r.get("post_caption"):
            lines += ["Legenda do post:", "", r["post_caption"], ""]
        if r.get("hashtags"):
            lines += [" ".join(h if h.startswith("#") else f"#{h}" for h in r["hashtags"]), ""]
    (d / "publicacao.md").write_text("\n".join(lines))


def cmd_cleanup(a: argparse.Namespace) -> None:
    d = Path(a.dir).resolve()
    freed = 0
    for p in list(d.glob("source.*")) + list(d.glob("audio_src.*")) + list(d.glob("clips/.src-*")) + [d / "audio.wav"]:
        if p.exists() or p.is_symlink():
            freed += p.stat().st_size if p.exists() and not p.is_symlink() else 0
            p.unlink()
    if (d / "asr").exists():
        shutil.rmtree(d / "asr")
    print(f"Removidos vídeo-fonte e áudio ({freed / 1e6:.0f} MB). Transcrição, plano e clipes mantidos.")


def main() -> None:
    ensure_venv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="baixa e transcreve")
    p.add_argument("source", help="URL do YouTube ou caminho de arquivo de vídeo")
    p.add_argument("--out", required=True)
    p.add_argument("--asr", choices=["auto", "groq", "local"], default="auto",
                   help="auto: Groq (envia o áudio) e Whisper local se a cota acabar; local: só CPU, nada sai da VPS")
    p.add_argument("--model", default="small", help="modelo do Whisper local: tiny|base|small|medium (padrão small)")
    p.add_argument("--language", default=None, help="código ISO (pt, en...); vazio = detectar")
    p.add_argument("--cookies", default=None, help=f"cookies.txt do YouTube (padrão {DEFAULT_COOKIES})")
    p.add_argument("--max-height", type=int, default=1080)
    p.add_argument("--beam", type=int, default=5, help="1 = ~2x mais rápido (vídeos longos), 5 = mais preciso")
    p.add_argument("--full-video", action="store_true",
                   help="baixa o vídeo inteiro (padrão para URL: só áudio agora e trechos em alta no render)")
    p.add_argument("--apify-max-usd", type=float, default=1.0,
                   help="teto de custo da execução na Apify (padrão US$ 1; 1 h em 720p ≈ US$ 1,62)")
    p.add_argument("--apify-actor", choices=sorted(APIFY_ACTORS), default="streamers",
                   help="streamers (qualquer plano, por MB) ou epctex (por segundo; só plano pago)")
    p.add_argument("--downloader", choices=["auto", "ytdlp", "apify"], default="auto",
                   help="auto: yt-dlp e, se o YouTube bloquear, Apify")
    p.set_defaults(func=cmd_prepare)
    r = sub.add_parser("render", help="gera os clipes a partir do plano")
    r.add_argument("dir")
    r.add_argument("--plan", default="plan.json")
    r.add_argument("--only", default=None, help="índices separados por vírgula, ex.: 1,3")
    r.add_argument("--keep-ass", action="store_true")
    r.add_argument("--cookies", default=None)
    r.add_argument("--force", action="store_true", help="renderiza mesmo com avisos de validação")
    r.set_defaults(func=cmd_render)
    c = sub.add_parser("cleanup", help="apaga vídeo-fonte e áudio")
    c.add_argument("dir")
    c.set_defaults(func=cmd_cleanup)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
