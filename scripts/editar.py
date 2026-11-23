#!/usr/bin/env python3
"""Edita um clipe no padrão Remotion (tela cheia no rosto, câmera lenta, legenda baixa, banner de abertura e CTA).

Usa a pasta de trabalho do `clips.py prepare` (audio.wav, transcript.json, meta.json): baixa só o trecho em 1080p,
alinha pelo áudio, recorta, acha o rosto (YuNet), monta a legenda palavra por palavra e renderiza com o template
`remotion/` da skill num runtime com node_modules (fora da skill).

  python3 editar.py --work <DIR> --start 4664.74 --end 4693.5 --slug gshock-bola \\
      --hook "A bola que deu origem ao G-Shock" --cta question --cta-question "Como entrar no grupo VIP?" \\
      --end-text "Grupo de ofertas · link na bio" --fix "Kikun=Kikuo"

--cta é obrigatório: pergunte ao usuário se o clipe terá CTA e para onde leva antes de rodar (ver SKILL.md).
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL / "remotion"
FPS = 30


def env_path(name: str, default: str) -> Path:
    cfg = SKILL / "config.env"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.startswith(f"{name}="):
                return Path(os.path.expanduser(line.split("=", 1)[1].strip().strip('"')))
    return Path(os.environ.get(name, default))


HOME = env_path("YOUTUBE_CLIPS_HOME", str(Path.home() / ".cache/youtube-clips"))
RUNTIME = env_path("CLIPS_REMOTION_RUNTIME", str(HOME / "remotion-runtime"))
YUNET = env_path("CLIPS_YUNET_MODEL", str(HOME / "models/face_detection_yunet_2023mar.onnx"))


def load_clips():
    spec = importlib.util.spec_from_file_location("clips", SKILL / "scripts" / "clips.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sync_runtime() -> None:
    """Copia src/, public/ e configs do template para o runtime e instala dependências quando o package.json muda."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "tsconfig.json", "remotion.config.ts"):
        shutil.copy2(TEMPLATE / name, RUNTIME / name)
    shutil.copytree(TEMPLATE / "src", RUNTIME / "src", dirs_exist_ok=True)
    shutil.copytree(TEMPLATE / "public", RUNTIME / "public", dirs_exist_ok=True)
    stamp = RUNTIME / ".package-stamp"
    pkg = (TEMPLATE / "package.json").read_text()
    if not (RUNTIME / "node_modules").exists() or not stamp.exists() or stamp.read_text() != pkg:
        subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=RUNTIME, check=True)
        stamp.write_text(pkg)


def words_between(transcript: dict, start: float, end: float, fixes: dict[str, str]) -> list[dict]:
    out = []
    for seg in transcript["segments"]:
        for w in seg.get("words", []):
            if w["e"] <= start or w["s"] >= end:
                continue
            text = w["w"].strip()
            for a, b in fixes.items():
                text = re.sub(rf"\b{re.escape(a)}\b", b, text)
            if text:
                out.append({"text": text, "startMs": round((max(w["s"], start) - start) * 1000),
                            "endMs": round((min(w["e"], end) - start) * 1000)})
    return out


def face_track(video: Path, step: int = 3) -> tuple[list[dict], int, int]:
    """Centro e altura do rosto dominante a cada `step` quadros (0 a 1), com zona morta e média móvel."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.7)
    raw, i, last = [], 0, None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            _, faces = det.detect(frame)
            best = None
            if faces is not None and len(faces):
                cands = sorted(faces, key=lambda f: -f[2] * f[3])[:3]
                if last:  # prefere o rosto perto do anterior, para não pular entre pessoas
                    cands.sort(key=lambda f: abs((f[0] + f[2] / 2) / w - last[0]) - f[2] * f[3] / (w * h) * 4)
                f = cands[0]
                best = (float((f[0] + f[2] / 2) / w), float((f[1] + f[3] / 2) / h), float(f[3] / h))
            best = best or last or (0.5, 0.42, 0.25)
            last = best
            raw.append({"frame": i, "cx": best[0], "cy": best[1], "fh": best[2]})
        i += 1
    cap.release()
    sm, cur = [], None
    for p in raw:
        if cur is None:
            cur = dict(p)
        else:
            for k, a in (("cx", 0.12), ("cy", 0.10), ("fh", 0.08)):
                if abs(p[k] - cur[k]) > 0.015:
                    cur[k] += (p[k] - cur[k]) * a
            cur["frame"] = p["frame"]
        sm.append({k: round(v, 4) if isinstance(v, float) else v for k, v in cur.items()})
    return sm, w, h


def copy_asset(path: str, slug: str) -> str:
    src = Path(path)
    name = f"{slug}-{src.name}"
    shutil.copy2(src, RUNTIME / "public" / name)
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="pasta do clips.py prepare")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--hook", default="", help="texto do banner de abertura (3,2 s), sem emoji; vazio desliga")
    ap.add_argument("--cta", required=True, choices=["question", "banner", "none"],
                    help="question: caixa 'Faça uma pergunta' + frase pulsando; banner: faixa no alto; none: sem CTA")
    ap.add_argument("--cta-header", default="Faça uma pergunta")
    ap.add_argument("--cta-question", default="", help="pergunta da caixa (sobre o destino do CTA)")
    ap.add_argument("--cta-pulse", default="LINK NA BIO!", help="frase pulsando sob a caixa; vazio desliga")
    ap.add_argument("--cta-text", default="", help="texto da faixa quando --cta banner")
    ap.add_argument("--end-text", default="", help="faixa final (2,5 s); vazio desliga")
    ap.add_argument("--end-logo", default="", help="logo da marca na faixa final (SVG/PNG claro)")
    ap.add_argument("--end-bg", default="rgba(0,0,0,.85)")
    ap.add_argument("--font", default="", help="arquivo de fonte da marca (.ttf/.otf); padrão Montserrat ExtraBold")
    ap.add_argument("--font-weight", type=int, default=800)
    ap.add_argument("--highlight", default="#FFD600", help="cor da palavra falada")
    ap.add_argument("--text-color", default="#FFFFFF")
    ap.add_argument("--hook-bg", default="#D7141A")
    ap.add_argument("--hook-text", default="#FFFFFF")
    ap.add_argument("--fix", action="append", default=[], help="troca na legenda, ex.: Kikun=Kikuo")
    ap.add_argument("--logo", default="", help="canto do logo do canal de origem para recolocar: tr, tl, br, bl")
    ap.add_argument("--no-render", action="store_true", help="só prepara props e vídeo (para testar com still)")
    a = ap.parse_args()

    if a.cta == "question" and not a.cta_question:
        ap.error("--cta question exige --cta-question")
    if a.cta == "banner" and not a.cta_text:
        ap.error("--cta banner exige --cta-text")

    work = Path(a.work)
    meta = json.loads((work / "meta.json").read_text())
    url = meta.get("webpage_url") or meta["source"]
    clips = load_clips()
    sync_runtime()
    pub = RUNTIME / "public"

    pad = 4.0
    section = work / f"sec-{a.slug}.mp4"
    if not section.exists():
        clips.download_section(url, max(0.0, a.start - pad), a.end + 2.0, section, None, 1080)
    sec_start = clips.locate_section(section, work / "audio.wav", max(0.0, a.start - pad))
    dur = a.end - a.start
    video = pub / f"{a.slug}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a.start - sec_start:.3f}", "-i", str(section), "-t", f"{dur:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
                    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", str(video)], check=True)

    fixes = dict(f.split("=", 1) for f in a.fix)
    words = words_between(json.loads((work / "transcript.json").read_text()), a.start, a.end, fixes)
    track, w, h = face_track(video)
    logo = None
    if a.logo:  # o enquadramento no rosto cortaria o canto do logo do canal: recorta e recoloca (nunca apagar)
        lw, lh = int(w * 0.16), int(h * 0.14)
        x = w - lw if "r" in a.logo else 0
        y = 0 if "t" in a.logo else h - lh
        png = pub / f"{a.slug}-logo.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur / 2:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"crop={lw}:{lh}:{x}:{y}", str(png)], check=True)
        logo = {"src": png.name, "corner": a.logo}

    cta = None
    if a.cta == "question":
        cta = {"kind": "question", "header": a.cta_header, "question": a.cta_question, "pulse": a.cta_pulse}
    elif a.cta == "banner":
        cta = {"kind": "banner", "text": a.cta_text}
    end = None
    if a.end_text:
        end = {"text": a.end_text, "bg": a.end_bg, "logo": copy_asset(a.end_logo, a.slug) if a.end_logo else None}

    props = {
        "src": video.name, "durationInFrames": int(round(dur * FPS)), "srcWidth": w, "srcHeight": h,
        "hook": a.hook, "captions": words, "track": track, "logo": logo,
        "style": {"font": copy_asset(a.font, a.slug) if a.font else "Montserrat-ExtraBold.ttf", "fontWeight": a.font_weight,
                  "text": a.text_color, "highlight": a.highlight, "hookBg": a.hook_bg, "hookText": a.hook_text},
        "cta": cta, "end": end,
    }
    props_file = work / f"remotion-{a.slug}.json"
    props_file.write_text(json.dumps(props, ensure_ascii=False, indent=1))
    result = {"props": str(props_file), "words": len(words), "seconds": round(dur, 2),
              "last_words": " ".join(x["text"] for x in words[-6:])}

    if not a.no_render:
        out_dir = work / "clips-remotion"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"{a.slug}.mp4"
        cmd = ["npx", "remotion", "render", "Clip", str(out), f"--props={props_file}", "--concurrency=3", "--crf=18"]
        chrome = shutil.which("google-chrome")
        if chrome:
            cmd.append(f"--browser-executable={chrome}")
        subprocess.run(cmd, cwd=RUNTIME, check=True)
        # 4 quadros para conferência: banner, CTA, meio e faixa final
        qa = out_dir / f"{a.slug}-qa.jpg"
        ts = [1.5, min(dur - 3, 5.0), dur / 2, max(0.5, dur - 1.0)]
        inputs = sum((["-ss", f"{t:.2f}", "-i", str(out)] for t in ts), [])
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                        "".join(f"[{i}:v]trim=end_frame=1,scale=360:640[v{i}];" for i in range(4)) +
                        "".join(f"[v{i}]" for i in range(4)) + "hstack=inputs=4", "-frames:v", "1", str(qa)], check=True)
        for f in pub.glob(f"{a.slug}*"):  # vídeo e assets do clipe saem do runtime
            f.unlink()
        result.update({"video": str(out), "qa": str(qa)})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
