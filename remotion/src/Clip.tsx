import React, { useMemo } from "react";
import { AbsoluteFill, Img, OffthreadVideo, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { loadFont } from "@remotion/fonts";

// Padrão de edição dos clipes (aprovado pelo Tamura em 23/11/2026, no WristClub): tela cheia no rosto com câmera
// lenta (zooms pequenos e movimentos laterais suaves), legenda palavra por palavra abaixo do rosto, banner de abertura
// em papel rasgado e um CTA escolhido para cada pedido (caixa de pergunta, faixa ou nenhum), mais faixa final opcional.

export type Word = { text: string; startMs: number; endMs: number };
export type TrackPoint = { frame: number; cx: number; cy: number; fh: number };
export type ClipStyle = {
  font: string; // arquivo em public/ (padrão Montserrat-ExtraBold.ttf)
  fontWeight: number;
  text: string; // cor da legenda
  highlight: string; // palavra falada
  hookBg: string; // banner de abertura
  hookText: string;
};
export type Cta =
  | { kind: "question"; header: string; question: string; pulse: string } // caixa "Faça uma pergunta" + frase pulsando
  | { kind: "banner"; text: string } // faixa no alto (ex.: "Siga @perfil para a parte 2")
  | null;
export type EndCard = { text: string; bg: string; logo: string | null } | null;
export type ClipProps = {
  src: string;
  durationInFrames: number;
  srcWidth: number;
  srcHeight: number;
  hook: string;
  captions: Word[];
  track: TrackPoint[];
  logo: { src: string; corner: string } | null; // logo do canal de origem recolocado
  style: ClipStyle;
  cta: Cta;
  end: EndCard;
};

const W = 1080;
const H = 1920;
const FONT = "ClipFont";
const HOOK_FRAMES = 96; // 3,2 s
const END_FRAMES = 75; // 2,5 s

// ------------------------------------------------------------------ câmera lenta

function trackAt(track: TrackPoint[], frame: number) {
  if (!track.length) return { cx: 0.5, cy: 0.42, fh: 0.3 };
  if (frame <= track[0].frame) return track[0];
  for (let i = 1; i < track.length; i++) {
    const a = track[i - 1];
    const b = track[i];
    if (frame <= b.frame) {
      const t = (frame - a.frame) / Math.max(1, b.frame - a.frame);
      return { cx: a.cx + (b.cx - a.cx) * t, cy: a.cy + (b.cy - a.cy) * t, fh: a.fh + (b.fh - a.fh) * t };
    }
  }
  return track[track.length - 1];
}

// A cada ~4 s um zoom pequeno (até ~7%) entrando ou saindo e um deslize lateral (até 70 px), com começo e fim
// suaves. Aproximação brusca foi reprovada.
const SEG_SECONDS = 4.2;
const MAX_ZOOM = 1.07;
const PAN_PX = 70;
const smooth = (t: number) => 0.5 - 0.5 * Math.cos(Math.PI * Math.min(1, Math.max(0, t)));

function cameraMove(frame: number, fps: number) {
  const seg = SEG_SECONDS * fps;
  const i = Math.floor(frame / seg);
  const t = smooth((frame - i * seg) / seg);
  const target = (k: number) => ({
    z: k % 2 === 0 ? 1.0 : MAX_ZOOM - (k % 4 === 1 ? 0 : 0.02),
    x: (k % 4 === 0 || k % 4 === 1 ? -1 : 1) * PAN_PX * (k === 0 ? 0 : 1),
  });
  const a = target(i);
  const b = target(i + 1);
  return { z: a.z + (b.z - a.z) * t, x: a.x + (b.x - a.x) * t };
}

const Camera: React.FC<ClipProps> = (p) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const f = trackAt(p.track, frame);
  const move = cameraMove(frame, fps);
  const S = (H / p.srcHeight) * move.z; // preenche a altura
  const vw = p.srcWidth * S;
  const vh = p.srcHeight * S;
  // rosto um pouco acima do meio: sobra cabeça em cima e o queixo fica acima da legenda
  const tx = Math.min(0, Math.max(W - vw, W / 2 - f.cx * vw + move.x));
  const ty = Math.min(0, Math.max(H - vh, H * 0.47 - f.cy * vh));
  return (
    <AbsoluteFill style={{ overflow: "hidden", backgroundColor: "#000" }}>
      <div style={{ position: "absolute", left: 0, top: 0, width: vw, height: vh, transform: `translate(${tx}px, ${ty}px)` }}>
        <OffthreadVideo src={staticFile(p.src)} style={{ width: "100%", height: "100%" }} />
      </div>
      {p.logo ? <ChannelLogo logo={p.logo} /> : null}
    </AbsoluteFill>
  );
};

// O enquadramento no rosto cortaria o canto do logo do canal; ele volta no mesmo canto. Nunca apagar logo.
const ChannelLogo: React.FC<{ logo: { src: string; corner: string } }> = ({ logo }) => {
  const style: React.CSSProperties = { position: "absolute", width: 240, opacity: 0.95 };
  if (logo.corner.includes("t")) style.top = 230;
  else style.bottom = 360;
  if (logo.corner.includes("r")) style.right = 24;
  else style.left = 24;
  return <Img src={staticFile(logo.src)} style={style} />;
};

// ------------------------------------------------------------------ legenda abaixo do rosto

type Page = { words: Word[]; startMs: number; endMs: number };

function paginate(words: Word[]): Page[] {
  const pages: Page[] = [];
  let cur: Word[] = [];
  const flush = () => {
    if (cur.length) pages.push({ words: cur, startMs: cur[0].startMs, endMs: cur[cur.length - 1].endMs });
    cur = [];
  };
  for (const w of words) {
    const len = cur.map((x) => x.text).join(" ").length + w.text.length;
    if (cur.length >= 3 || (cur.length && len > 18)) flush();
    cur.push(w);
    if (/[.,!?]$/.test(w.text)) flush();
  }
  flush();
  return pages;
}

const Captions: React.FC<{ words: Word[]; style: ClipStyle }> = ({ words, style }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const pages = useMemo(() => paginate(words), [words]);
  const ms = (frame / fps) * 1000;
  const page = pages.find((pg, i) => ms >= pg.startMs && ms < (pages[i + 1]?.startMs ?? pg.endMs + 400));
  if (!page) return null;
  const pop = spring({ frame: frame - Math.round((page.startMs / 1000) * fps), fps, config: { damping: 14, stiffness: 220 }, durationInFrames: 8 });
  const current = page.words.reduce((k, x, j) => (ms >= x.startMs ? j : k), -1);
  return (
    <div style={{ position: "absolute", left: 60, right: 60, bottom: 400, display: "flex", justifyContent: "center", flexWrap: "wrap", gap: "0 34px", transform: `scale(${0.85 + 0.15 * pop})` }}>
      {page.words.map((w, i) => (
        <span
          key={i}
          style={{
            fontFamily: FONT,
            fontWeight: style.fontWeight,
            fontSize: 86,
            lineHeight: 1.08,
            textTransform: "uppercase",
            color: i === current ? style.highlight : style.text,
            WebkitTextStroke: "10px #000",
            paintOrder: "stroke fill",
            textShadow: "0 6px 18px rgba(0,0,0,.45)",
            transform: i === current ? "translateY(-4px) scale(1.06)" : undefined,
            display: "inline-block",
          }}
        >
          {w.text.replace(/[.,]$/, "")}
        </span>
      ))}
    </div>
  );
};

// ------------------------------------------------------------------ banner de abertura (papel rasgado)

function tornPath(w: number, h: number, seed: number) {
  let s = seed;
  const rnd = () => (s = (s * 9301 + 49297) % 233280) / 233280;
  const pts: string[] = [];
  const step = 26;
  for (let x = 0; x <= w; x += step) pts.push(`${x},${10 + rnd() * 18}`);
  for (let y = 0; y <= h; y += step) pts.push(`${w - rnd() * 10},${y}`);
  for (let x = w; x >= 0; x -= step) pts.push(`${x},${h - 10 - rnd() * 18}`);
  for (let y = h; y >= 0; y -= step) pts.push(`${rnd() * 10},${y}`);
  return pts.join(" ");
}

const HookBanner: React.FC<{ text: string; style: ClipStyle }> = ({ text, style }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (!text || frame > HOOK_FRAMES + 10) return null;
  const inS = spring({ frame, fps, config: { damping: 12, stiffness: 180 }, durationInFrames: 12 });
  const out = interpolate(frame, [HOOK_FRAMES - 6, HOOK_FRAMES + 8], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const bw = 900;
  const bh = 230;
  return (
    <div style={{ position: "absolute", left: (W - bw) / 2, top: 270, width: bw, height: bh, transform: `rotate(-2.5deg) scale(${0.7 + 0.3 * inS})`, opacity: out, filter: "drop-shadow(0 10px 22px rgba(0,0,0,.45))" }}>
      <svg width={bw} height={bh} style={{ position: "absolute", inset: 0 }}>
        <polygon points={tornPath(bw, bh, 7)} fill={style.hookBg} />
      </svg>
      <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", padding: "0 56px", textAlign: "center", fontFamily: FONT, fontWeight: style.fontWeight, fontSize: 62, lineHeight: 1.12, color: style.hookText }}>
        {text}
      </div>
    </div>
  );
};

// ------------------------------------------------------------------ CTA

function useCtaPresence(delay: number) {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const inS = spring({ frame: frame - HOOK_FRAMES - delay, fps, config: { damping: 13, stiffness: 160 }, durationInFrames: 14 });
  const out = interpolate(frame, [durationInFrames - END_FRAMES - 8, durationInFrames - END_FRAMES + 4], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return { inS, out, frame, fps };
}

const QuestionCta: React.FC<{ cta: Extract<Cta, { kind: "question" }>; hasEnd: boolean; style: ClipStyle }> = ({ cta, hasEnd, style }) => {
  const box = useCtaPresence(0);
  const line = useCtaPresence(6);
  const keepBox = hasEnd ? box.out : 1;
  const keepLine = hasEnd ? line.out : 1;
  const pulse = 1 + 0.07 * (0.5 - 0.5 * Math.cos((2 * Math.PI * line.frame) / (line.fps * 0.9)));
  return (
    <>
      <div style={{ position: "absolute", top: 250, right: 36, width: 420, borderRadius: 26, overflow: "hidden", boxShadow: "0 10px 30px rgba(0,0,0,.35)", transform: `scale(${box.inS})`, transformOrigin: "top right", opacity: keepBox, fontFamily: FONT }}>
        <div style={{ background: "#0B0B0B", color: "#FDFDFD", fontWeight: 600, fontSize: 26, textAlign: "center", padding: "16px 18px" }}>{cta.header}</div>
        <div style={{ background: "#FDFDFD", color: "#111", fontWeight: 600, fontSize: 34, lineHeight: 1.2, textAlign: "center", padding: "22px 22px 26px" }}>{cta.question.replace(/-/g, "‑")}</div>
      </div>
      {cta.pulse ? (
        <div style={{ position: "absolute", top: 482, right: 36, width: 420, display: "flex", justifyContent: "center", opacity: keepLine * line.inS, transform: `scale(${pulse * (0.8 + 0.2 * line.inS)})`, transformOrigin: "center top" }}>
          <span style={{ fontFamily: FONT, fontWeight: style.fontWeight, fontSize: 50, color: "#FDFDFD", textTransform: "uppercase", wordSpacing: "14px", WebkitTextStroke: "8px #000", paintOrder: "stroke fill", textShadow: "0 4px 14px rgba(0,0,0,.5)" }}>
            {cta.pulse}
          </span>
        </div>
      ) : null}
    </>
  );
};

const BannerCta: React.FC<{ cta: Extract<Cta, { kind: "banner" }>; hasEnd: boolean; style: ClipStyle }> = ({ cta, hasEnd, style }) => {
  const s = useCtaPresence(0);
  const pulse = 1 + 0.04 * (0.5 - 0.5 * Math.cos((2 * Math.PI * s.frame) / (s.fps * 1.1)));
  return (
    <div style={{ position: "absolute", top: 260, left: 0, right: 0, display: "flex", justifyContent: "center", opacity: (hasEnd ? s.out : 1) * s.inS, transform: `scale(${pulse})` }}>
      <span style={{ fontFamily: FONT, fontWeight: style.fontWeight, fontSize: 54, color: "#FDFDFD", textTransform: "uppercase", background: style.hookBg, padding: "14px 30px", borderRadius: 16, boxShadow: "0 8px 26px rgba(0,0,0,.35)" }}>{cta.text}</span>
    </div>
  );
};

const EndBanner: React.FC<{ end: NonNullable<EndCard>; style: ClipStyle }> = ({ end }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const start = durationInFrames - END_FRAMES;
  if (frame < start) return null;
  const inS = spring({ frame: frame - start, fps, config: { damping: 14, stiffness: 170 }, durationInFrames: 12 });
  return (
    <div style={{ position: "absolute", left: 70, right: 70, top: 250, height: 140, borderRadius: 999, background: end.bg, display: "flex", alignItems: "center", justifyContent: "center", gap: 24, color: "#FDFDFD", fontFamily: FONT, fontWeight: 600, fontSize: 40, whiteSpace: "nowrap", transform: `translateY(${(1 - inS) * -40}px)`, opacity: inS, boxShadow: "0 10px 34px rgba(0,0,0,.35)" }}>
      {end.logo ? (
        <>
          <Img src={staticFile(end.logo)} style={{ height: 36 }} />
          <span style={{ opacity: 0.6 }}>|</span>
        </>
      ) : null}
      {end.text}
    </div>
  );
};

export const Clip: React.FC<ClipProps> = (p) => {
  useMemo(() => loadFont({ family: FONT, url: staticFile(p.style.font) }), [p.style.font]);
  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Camera {...p} />
      <Captions words={p.captions} style={p.style} />
      <HookBanner text={p.hook} style={p.style} />
      {p.cta?.kind === "question" ? <QuestionCta cta={p.cta} hasEnd={Boolean(p.end)} style={p.style} /> : null}
      {p.cta?.kind === "banner" ? <BannerCta cta={p.cta} hasEnd={Boolean(p.end)} style={p.style} /> : null}
      {p.end ? <EndBanner end={p.end} style={p.style} /> : null}
    </AbsoluteFill>
  );
};
