---
name: youtube-clips
description: "Transforma vídeo longo (YouTube ou arquivo) em clipes verticais 9:16 com corte nos melhores momentos, rosto enquadrado, legenda em PT-BR e gancho. Use em cria clipes desse vídeo, cortes virais ou shorts de podcast."
---

# Clipes a partir de vídeo longo

Pipeline: baixa só o que precisa (áudio primeiro), transcreve com tempo por palavra (Groq, com Whisper local de reserva), **você** escolhe os momentos lendo a transcrição e o script reenquadra, legenda e exporta. A seleção editorial é feita pelo agente nesta sessão, sem chave de LLM. Desenho estudado por engenharia reversa do OpenShorts (MIT) e do SupoClip (AGPL, só ideias); detalhes em [references/origens.md](references/origens.md).

Script: `python3 <pasta-da-skill>/scripts/clips.py` (reexecuta sozinho no venv criado pelo `setup.sh`). Se o script disser que falta dependência ou credencial, siga o `README.md` da skill.

**Se existir `LOCAL.md` nesta pasta, leia antes de tudo:** ele traz as convenções da instalação (onde salvar, contas, limites de custo) e tem prioridade sobre os padrões abaixo.

## 1. Destino

Sem `LOCAL.md`, salve em `./clipes/<AAAA-MM-DD>-<slug-do-video>/` a partir do diretório de trabalho, ou onde o usuário pedir. Vídeos de horas ocupam alguns GB durante o processo: confira o espaço em disco e rode `cleanup` ao final.

## 2. Preparar (download + transcrição)

```bash
python3 .../clips.py prepare "<URL ou arquivo>" --out <DIR> --language pt
```

Passe `--language` quando souber o idioma. Gera `transcript.txt` (frases com `[início-fim]` em segundos e, no topo, pistas automáticas e os picos de "mais repetidos" do YouTube quando a plataforma os publica), `transcript.json` (palavras) e `meta.json`.

**Transcrição (`--asr`)**, em blocos de 10 min com checkpoint por bloco (um reinício retoma de onde parou):
- `auto` (padrão): os blocos vão em paralelo para a Groq (`whisper-large-v3` e `-turbo`, com `GROQ_API_KEY`). 10 min de áudio levam ~4 s. O plano gratuito da Groq dá 2 h de áudio por hora **por modelo**; se a cota acabar, o Whisper local assume blocos a partir do fim. Plano pago: US$ 0,111/h de áudio.
- `local`: só CPU (faster-whisper `--model small`, ~2x o tempo real com `--beam 1`). **Use `local` para material privado ou sensível** (reunião, cliente, dado pessoal): no `auto` o áudio é enviado à Groq.

**Como o vídeo é obtido (URL):**

1. **yt-dlp, só o áudio** (padrão). A imagem não é baixada no `prepare`; no `render`, cada clipe baixa só o seu trecho em até 1080p (`--max-height`), e o início exato é localizado por correlação com o áudio. `--full-video` baixa o vídeo inteiro.
2. **YouTube bloqueado** ("Sign in to confirm you're not a bot"): acontece em IP de datacenter (VPS, nuvem). Resolva com **proxy** (preferido) ou cookies:
   - **Proxy:** `WEBSHARE_API_KEY` (o script lê a conta Webshare pela API: plano residencial vira `p.webshare.io` com sessão fixa; plano ISP/datacenter vira a lista de proxies, testados um a um até o YouTube aceitar, com o país de `WEBSHARE_COUNTRY`, padrão US, primeiro e o último que funcionou lembrado). Qualquer outro fornecedor: `YOUTUBE_PROXY_URL=http://usuario:senha@host:porta`. Use proxy residencial ou ISP, nunca de datacenter. A credencial vai para o yt-dlp por variável de ambiente, fora da linha de comando.
   - **Cookies:** exportados de um navegador logado no YouTube (extensão "Get cookies.txt LOCALLY", formato Netscape) no arquivo de `YOUTUBE_COOKIES` (padrão `~/.config/youtube-clips/youtube-cookies.txt`). Nunca peça o conteúdo do cookie no chat.
3. **Plano B automático: Apify** (`APIFY_TOKEN`), quando yt-dlp continua bloqueado. Dispara ao mesmo tempo **só o áudio** (`lurkapi/youtube-to-mp3-audio-downloader`, US$ 0,002/min, libera a transcrição em minutos) e o **vídeo inteiro** (`--apify-actor`), que segue baixando enquanto você transcreve e escolhe; o `render` busca o vídeo. Actors de vídeo: `streamers` (padrão, qualquer plano, por MB; em plano FREE e vídeo longo, use `--max-height 360`) e `epctex` (por segundo de vídeo, mais barato em plano pago, mas limita contas FREE a US$ 0,20 por execução). Teto por execução: `--apify-max-usd` (padrão US$ 1, mínimo US$ 0,50); ao bater no teto, o actor encerra sem entregar. Antes de vídeos acima de ~1 h, estime o custo e confirme com o usuário. `--downloader ytdlp|apify` força um caminho.
4. Arquivo local: passe o caminho no lugar da URL.

**Tempos de referência** (CPU de 4 núcleos, podcast de 4h17 com proxy): áudio ~5 min, transcrição ~3,5 min, 10 clipes em 1080p ~23 min.

## Texto público dos clipes

Preferência do Tamura (18/11/2026): não citar fonte, canal de origem, autor como crédito, URL de origem ou data/ano do vídeo original na legenda do post nem dentro do vídeo (rodapé, gancho, legenda queimada ou encerramento). A legenda pública contém apenas o conteúdo do trecho, CTA e hashtags pertinentes. Preservar origem e datas somente nos metadados internos para rastreabilidade; não copiar esses campos para a publicação. Isso não altera direitos de uso nem dispensa eventual atribuição legalmente exigida: nesse caso, resolver a licença antes de publicar.

Antes de entregar ou programar, conferir o texto público e amostras do início, meio e fim do MP4 final. Remover créditos e datas também de templates, overlays ASS e renderizadores customizados; limpar apenas a legenda do post não corrige texto já queimado no vídeo. Não declarar arquivos antigos corrigidos sem reexportar e conferir a versão final usada pela fila.

## 3. Escolher os momentos (trabalho editorial seu)

Leia `transcript.txt` inteiro e aplique [references/selection.md](references/selection.md). Acima de ~1 h, divida a transcrição em partes de ~60 min e leia em paralelo com subagentes (cada um devolve seus melhores candidatos com tempos exatos, trecho literal e textos); depois compare todos, confira o começo dos finalistas na transcrição e escolha. Grave `<DIR>/plan.json`:

```json
{
  "clips": [
    {
      "start": 312.4, "end": 351.9,
      "teaser": [340.1, 343.8],
      "title": "Título do Short no YouTube (até 100 caracteres)",
      "hook": "Gancho na tela, até 10 palavras",
      "score": 86,
      "reason": "Uma frase dizendo o que torna ESTE trecho forte",
      "post_caption": "1-2 frases que provocam sem entregar o final",
      "hashtags": ["#tema", "#nicho"],
      "layout": "auto"
    }
  ],
  "style": {"highlight_color": "#FFD600"}
}
```

- `start`/`end` vêm dos números entre colchetes da transcrição. O script ajusta para o começo e o fim da frase (até 3 s), corta no ponto mais silencioso do áudio perto da primeira e da última palavra (evita o "engasgo" de pegar o fim da palavra vizinha) e aplica fade de 30/60 ms em cada trecho.
- `teaser` é opcional: a frase mais forte do clipe (até 6 s, depois dos 2 primeiros segundos), tocada antes como abertura. Use só quando o clipe não abre na frase mais forte.
- `layout`: `auto` (padrão: rosto em 9:16 quando há uma pessoa dominante, vídeo inteiro sobre fundo desfocado quando há tela, slide ou duas pessoas afastadas), `track` ou `blur`. Fonte em 360p fica borrada no `track`: prefira `blur`.
- **Vídeo em outro idioma:** o `prepare` avisa, e o `render` traduz a legenda para PT-BR frase a frase (Groq, `openai/gpt-oss-120b`, com a frase anterior e a seguinte como contexto). As palavras traduzidas recebem o tempo da frase original proporcional ao tamanho de cada uma, então o destaque segue a fala sem sincronia exata por palavra. As traduções ficam em `<DIR>/translations.json`: revise as frases dos clipes escolhidos, corrija o que soar estranho e rode `render --only N` de novo. `"style": {"translate": false}` mantém o idioma original; `true` força tradução.
- `style` aceita: `font_size`, `uppercase`, `text_color`, `highlight_color`, `caption_position` (fração da altura a partir de baixo, padrão 0.27, acima da interface das redes), `max_chars`, `hook_seconds` (0 desliga o gancho), `hook_text_color`, `hook_box_color`, `hook_position`, `translate`. Para vídeo de marca, use as cores da identidade dela.
- `encode` aceita `preset`, `crf`, `loudnorm`.

## 4. Editar no padrão Remotion (padrão dos clipes)

Padrão aprovado pelo Tamura em 23/11/2026: tela cheia no rosto com zooms pequenos e lentos (até ~7% a cada ~4 s) e deslizes laterais suaves (nada de aproximação brusca), legenda palavra por palavra **abaixo do rosto** (só a palavra falada em destaque), banner de abertura em papel rasgado por 3,2 s no alto (sem emoji), CTA e faixa final opcionais. Template em `remotion/` (`src/Clip.tsx`); `scripts/editar.py` baixa o trecho em 1080p, alinha, acha o rosto, monta a legenda e renderiza num runtime com `node_modules` fora da skill (`YOUTUBE_CLIPS_HOME/remotion-runtime`; instala sozinho na primeira vez). Precisa de Node 18+ e Chrome. Para a mecânica do Remotion, use as skills `remotion-*`.

**Antes de editar, pergunte ao usuário (obrigatório, uma vez por lote):** "Os clipes vão ter CTA? Para onde ele leva?" e escolha o elemento pela resposta:

| Resposta | Elemento | Opções do `editar.py` |
|---|---|---|
| Link na bio (grupo, site, produto, lista) | Caixa "Faça uma pergunta" no canto superior direito, com frase pulsando embaixo | `--cta question --cta-question "<pergunta sobre o destino>" [--cta-pulse "LINK NA BIO!"]` |
| Seguir o perfil, parte 2, comentar uma palavra | Faixa no alto, pulsando de leve | `--cta banner --cta-text "Siga para a parte 2"` |
| Sem CTA | Nada | `--cta none` |

A pergunta da caixa fala do destino, não do assunto do clipe (ex.: "Como entrar no grupo VIP de relógios?"). Pergunte também se há faixa final da marca (`--end-text`, `--end-logo`, `--end-bg`) e use as cores e a fonte do `DESIGN.md` da marca (`--highlight`, `--hook-bg`, `--font`).

```bash
python3 .../scripts/editar.py --work <DIR> --start <s> --end <s> --slug <slug> --hook "<gancho>" \
    --cta question --cta-question "<pergunta>" --end-text "<faixa final>" --fix "Errado=Certo" [--logo tr]
```

- `--logo tr|tl|br|bl`: canto onde o canal de origem tem logo fixo. O enquadramento cortaria esse canto, então ele é recortado e recolocado. **Nunca apagar o logo do dono do vídeo.**
- Saída em `<DIR>/clips-remotion/<slug>.mp4` e `<slug>-qa.jpg` (banner, CTA, meio e faixa final). Cerca de 3,5 min para 30 s de clipe; um render por vez. Antes do render, conferir a última palavra e os nomes no `remotion-<slug>.json` (`--no-render` só prepara).
- Mais de 3 clipes: 1 piloto para o usuário validar antes do lote.
- O render antigo (`clips.py render`, legenda ASS) fica como reserva para quando não houver Node/Chrome ou para layouts `blur` (tela, slide, duas pessoas).

## 4b. Render simples (reserva)

```bash
python3 .../clips.py render <DIR>            # todos
python3 .../clips.py render <DIR> --only 2,4 # refaz só alguns
```

O plano é validado antes: intervalo dentro do vídeo, duração de 12 a 90 s, sobreposição de até 25% entre clipes, teaser dentro do clipe. Saída em `<DIR>/clips/`: `NN-slug.mp4` (1080x1920, 30 fps, H.264, áudio em -14 LUFS com ganho linear), `NN-slug-qa.jpg` (4 quadros) e `<DIR>/publicacao.md` (títulos, legendas e hashtags por nota).

**Confira cada `-qa.jpg` visualmente** antes de entregar: rosto dentro do quadro, legenda legível e sem cobrir o rosto, gancho sem cortar palavra. Se algo falhar, ajuste o plano e rode `--only`. O render leva ~1x a duração do clipe numa CPU de 4 núcleos; paralelizar clipes não ajuda porque o encode já usa todos os núcleos.

## 5. Entregar e limpar

Entregue os caminhos dos clipes e o resumo do `publicacao.md`. Depois rode `clips.py cleanup <DIR>`: apaga vídeo-fonte, áudio e checkpoints e mantém transcrição, plano e clipes.

## Limites conhecidos

- O enquadramento segue o maior rosto do plano, não quem está falando. Em conversa com duas pessoas no mesmo quadro, o modo `auto` mostra o quadro inteiro com fundo desfocado.
- Só há rastreamento horizontal. Vídeo já vertical é apenas ajustado ao quadro.
- Sem dublagem, B-roll ou publicação automática. Postar em redes é ação externa e exige pedido explícito.
- Respeite direitos autorais: cortar vídeo de terceiros para publicar exige autorização do titular.
