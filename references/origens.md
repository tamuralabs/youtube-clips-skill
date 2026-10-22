# Origens e decisões de engenharia

Esta skill é própria. O desenho veio da engenharia reversa de dois projetos, feita em 20/10/2026. O REA mapeou o grafo do projeto Remotion do OpenShorts (legendas e gancho), e os pipelines Python foram lidos diretamente no código-fonte, porque o REA não se aplica a repositório aberto. Não há código copiado de nenhum dos dois.

| Projeto | Revisão estudada | Licença | Uso |
|---|---|---|---|
| mutonby/openshorts | 5a56929fb59c257fdf42aa5fdf4d8a1f49b1bc86 | MIT | conceitos e parâmetros |
| FujiwaraChoki/supoclip | f1a0c30f8d9ea9e6944b12cef054d92d65219a14 | AGPL-3.0 | só ideias, reescritas |

## O que foi adotado

- **Transcrição em blocos de 10 min.** Os blocos vão em paralelo para a Groq (`whisper-large-v3`, depois o `-turbo`, que tem cota própria), com checkpoint por bloco. As emendas são alinhadas pelo texto da sobreposição, e o faster-whisper `small` local (padrão do OpenShorts) assume quando a cota acaba. Otimização feita em 21/10/2026: a transcrição das 4h17 caiu de ~2 h para ~3 min.
- **Seleção com tempos reais.** A transcrição entregue ao agente tem frases com segundos exatos. O OpenShorts manda só janelas sem tempo por palavra, e o SupoClip arredonda para segundos inteiros; os dois acabam cortando palavras.
- **Ajuste de borda por frase.** O início volta até 3 s para o começo da frase (pontuação ou pausa de 0,8 s ou mais) e o fim avança até 3 s para fechar a frase. Vem do SupoClip.
- **Regras editoriais combinadas.** Teste dos 2 segundos, clipe que se sustenta sozinho e correção pelo início (OpenShorts); nota em 4 partes de 25, descarte de intro, patrocínio e CTA, e calibração comparativa (SupoClip); diversidade e sobreposição máxima de 25% (OpenShorts).
- **Teaser de abertura opcional.** A frase mais forte toca antes do clipe (SupoClip).
- **Pistas determinísticas no topo da transcrição.** Pergunta, número, pausa e palavras de gatilho em PT e EN, servindo só como dica (SupoClip).
- **YuNet do OpenCV para rostos e histograma HSV para cortes de cena**, numa única decodificação de 4 fps. O SupoClip usa YuNet; o OpenShorts usa MediaPipe, YOLOv8 e TransNetV2, que exigem PyTorch, e o disco da VPS não comporta isso.
- **Câmera com zona morta.** Fica parada enquanto o rosto se move até 6% da largura, depois segue com suavização. Recorte fixo quando o rosto quase não se move. Reinicia a cada corte de cena.
- **Modo por plano.** Rosto em 9:16 para uma pessoa dominante; vídeo inteiro sobre fundo desfocado para tela, slide ou duas pessoas afastadas (ideia das duas fontes).
- **Legendas ASS com PlayRes 1080x1920 explícito**, blocos de até 18 caracteres e 1,6 s que quebram na pontuação. A palavra ativa muda só de cor e o bloco entra com um pop. Escalar a palavra faz a linha centralizada tremer, como alerta o código do SupoClip.
- **Corte de áudio no silêncio real, com fade e loudnorm em duas passadas.** Correção feita em 21/10/2026 depois de um relato de áudio engasgando no começo dos clipes. Medição: a 1ª amostra caía no meio de um som (pico de 14.763 em 5 ms) e, depois da correção, passou a 0. O loudnorm agora mede o clipe inteiro e aplica ganho linear, chegando a -14 LUFS.
- **Uma única codificação final.** Composição em Python, `unsharp` leve depois do upscale, H.264 high 4.1 a 30 fps, AAC 192k, loudnorm em -14 LUFS. O OpenShorts recodifica 3 vezes.

## O que ficou de fora e por quê

- **Dublagem, B-roll, publicação e atores de IA:** fora do pedido. O B-roll do SupoClip nem está ligado no pipeline.
- **Câmera por quem fala** (movimento da boca + diarização): é complexa e frágil em CPU. Fica como evolução.
- **PO token para o YouTube:** em IP de datacenter não basta sozinho; usamos proxy residencial/ISP (Webshare ou qualquer `YOUTUBE_PROXY_URL`) ou cookies.
