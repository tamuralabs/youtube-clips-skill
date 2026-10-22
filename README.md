# youtube-clips

Skill para agentes de IA (Claude Code, Codex e afins) que transforma um vídeo longo, link do YouTube ou arquivo, em **clipes verticais 9:16** prontos para Reels, TikTok e Shorts:

- corte nos momentos mais fortes, escolhidos pelo próprio agente lendo a transcrição;
- enquadramento automático no rosto, ou vídeo inteiro sobre fundo desfocado quando há duas pessoas ou tela;
- legenda queimada palavra a palavra, com destaque, **traduzida para português do Brasil** quando o vídeo é em outro idioma;
- gancho na tela, teaser de abertura opcional, áudio normalizado em -14 LUFS e texto de publicação (título, legenda e hashtags).

*English summary: an agent skill that turns long videos (YouTube or local files) into captioned 9:16 short clips. The agent picks the moments; the script handles download (only audio plus the chosen segments), word-level transcription (Groq Whisper, local fallback), face reframing, burned karaoke captions (auto-translated to Brazilian Portuguese) and export. Docs are in Portuguese.*

## Como funciona

```
prepare  ->  baixa só o áudio (yt-dlp, por proxy se precisar) e transcreve com tempo por palavra
(agente) ->  lê a transcrição e grava plan.json com os trechos, ganchos e textos
render   ->  baixa só os trechos em até 1080p, reenquadra, legenda e exporta
```

Num podcast de 4h17, numa CPU de 4 núcleos com proxy: ~5 min de download do áudio, ~3,5 min de transcrição e ~23 min para 10 clipes em 1080p.

## Requisitos

- Linux ou macOS, Python 3.10+, `ffmpeg` com libass, `curl`.
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (`uv tool install yt-dlp` ou `pipx install yt-dlp`) e [Deno](https://deno.land), que o yt-dlp usa nos desafios JS do YouTube.
- Uma chave da [Groq](https://console.groq.com/keys) para transcrição e tradução (há plano gratuito). Sem ela, use `--asr local` e a legenda não é traduzida.
- Rodando em servidor (VPS ou nuvem): um proxy residencial ou ISP. Veja abaixo.

## Instalação

```bash
# 1. clone na pasta de skills do seu agente (Claude Code: ~/.claude/skills)
git clone https://github.com/tamuralabs/youtube-clips-skill ~/.claude/skills/youtube-clips

# 2. instale o runtime (venv com faster-whisper e OpenCV, modelo de rosto, pasta de config)
bash ~/.claude/skills/youtube-clips/setup.sh

# 3. preencha as credenciais
nano ~/.config/youtube-clips/.env
```

O `setup.sh` é idempotente: rode de novo para conferir ou reparar a instalação. Os caminhos padrão podem ser trocados no `config.env` (veja `config.env.example`).

## Configuração

Credenciais em `~/.config/youtube-clips/.env` (modelo em `env.example`). O arquivo fica com permissão 600 e nunca deve ir para o git.

| Variável | Para quê | Obrigatória |
|---|---|---|
| `GROQ_API_KEY` | transcrição (Whisper large-v3) e tradução das legendas | sim, no modo padrão |
| `WEBSHARE_API_KEY` | proxy da Webshare montado automaticamente pela API | em servidor, uma forma de proxy |
| `YOUTUBE_PROXY_URL` | proxy de qualquer fornecedor (`http://usuario:senha@host:porta`) | alternativa à Webshare |
| `WEBSHARE_COUNTRY` | país preferido dos proxies (padrão `US`) | não |
| `APIFY_TOKEN` | plano B pago se o yt-dlp continuar bloqueado | não |

### Por que proxy, e como criar

O YouTube bloqueia downloads vindos de IP de datacenter, com o erro *"Sign in to confirm you're not a bot"*. Isso acontece com qualquer VPS ou máquina de nuvem e vale para IPv4 e IPv6. No seu computador de casa, normalmente não precisa de nada.

**Opção recomendada: Webshare**
1. Crie uma conta em [webshare.io](https://www.webshare.io).
2. Contrate um plano **Residential** ou **ISP**. **Não** use *Datacenter*, que o YouTube também bloqueia. O plano ISP compartilhado mais barato funcionou nos nossos testes.
3. No painel, vá em **API Keys**, crie uma chave e coloque em `WEBSHARE_API_KEY`.
4. Pronto: o script lê o plano pela API. No residencial, usa `p.webshare.io` com sessão fixa, o mesmo IP do começo ao fim do download. No ISP, testa os proxies um a um até o YouTube aceitar, com os do país de `WEBSHARE_COUNTRY` primeiro, e lembra o que funcionou. Nos nossos testes, os dos EUA passaram e os da França, do Canadá e da Alemanha foram recusados.

**Qualquer outro fornecedor** (IPRoyal, Decodo, Bright Data, Oxylabs...): contrate proxy residencial ou ISP, gere usuário e senha com sessão fixa (*sticky*) de 30 min ou mais, e preencha `YOUTUBE_PROXY_URL=http://usuario:senha@host:porta`. Se a senha tiver `@`, `:` ou `/`, use `%40`, `%3A` e `%2F`.

**Consumo:** só o áudio e os trechos passam pelo proxy. Um episódio de 4 h usa ~250 MB de áudio mais algumas dezenas de MB por clipe.

**Segurança:** a credencial do proxy é passada ao yt-dlp por variável de ambiente, não por argumento, então não aparece na lista de processos (`ps`).

**Alternativas ao proxy:**
- Cookies do YouTube: exporte de um navegador logado com a extensão *Get cookies.txt LOCALLY* (formato Netscape) para `~/.config/youtube-clips/youtube-cookies.txt`. Os cookies expiram de tempos em tempos e há um pequeno risco de a conta ser marcada.
- Apify, pago: com `APIFY_TOKEN`, entra sozinho se o yt-dlp continuar bloqueado. Baixa o áudio, por US$ 0,002/min, e o vídeo inteiro em paralelo. Atenção: actors no plano gratuito têm limites baixos por execução.

## Uso

Peça ao agente: *"cria clipes de https://www.youtube.com/watch?v=..."*. Ele segue o `SKILL.md`. À mão:

```bash
S=~/.claude/skills/youtube-clips/scripts/clips.py
python3 $S prepare "https://www.youtube.com/watch?v=..." --out ./clipes/meu-video --language pt
# leia ./clipes/meu-video/transcript.txt e grave plan.json (formato no SKILL.md)
python3 $S render ./clipes/meu-video
python3 $S cleanup ./clipes/meu-video   # apaga áudio e vídeo-fonte, mantém clipes
```

Saída: `clips/NN-titulo.mp4` (1080x1920, H.264, 30 fps), `clips/NN-titulo-qa.jpg` (4 quadros para conferência) e `publicacao.md` (títulos, legendas e hashtags).

## Privacidade e custos

- No modo padrão (`--asr auto`), o áudio é enviado à Groq para transcrição, e o texto dos trechos é enviado de novo para tradução. Para material privado (reuniões, clientes, dados pessoais), use `--asr local`: transcreve só na CPU e nada sai da máquina.
- A Groq cobra US$ 0,111 por hora de áudio no plano pago e tem cota gratuita (2 h de áudio por hora, por modelo). A tradução usa `openai/gpt-oss-120b`, também na Groq.
- Proxy e Apify são cobrados pelos respectivos fornecedores.
- Cortar vídeo de terceiros para publicar exige autorização do titular dos direitos.

## Créditos e licença

- Código: MIT (veja `LICENSE`).
- Ideias de pipeline estudadas no [OpenShorts](https://github.com/mutonby/openshorts) (MIT) e no [SupoClip](https://github.com/FujiwaraChoki/supoclip) (AGPL-3.0, só conceitos). Nenhum código desses projetos foi copiado. Detalhes em `references/origens.md`.
- Fonte Montserrat ExtraBold: SIL Open Font License 1.1 (`assets/fonts/Montserrat-OFL.txt`).
- Detector de rosto YuNet (OpenCV Zoo), baixado pelo `setup.sh`.
