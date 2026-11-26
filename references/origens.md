# Origens e decisões de engenharia

Esta skill é própria. O desenho veio da engenharia reversa de dois projetos, feita em 20/10/2026. Foi mapeado o grafo do projeto Remotion do OpenShorts (legendas e gancho), e os pipelines Python foram lidos diretamente no código-fonte. Não há código copiado de nenhum dos dois.

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

## Revisão de 08/11/2026

Comparadas as revisões estudadas com OpenShorts `0af2185c91ef4c95a030034b85df70d9563ba639` e SupoClip `7010b4c556ced716fb70867d1149da5fa7c7ff0f`, nos caminhos acima e nos sucessores de seleção, legenda e enquadramento. Nenhum código copiado.

- **Sinal de audiência (OpenShorts, clipes feitos por espectadores da Twitch):** adaptado ao YouTube como os picos de "mais repetidos" que o yt-dlp já traz nos metadados, listados no topo da transcrição como pista.
- **Aberturas e fins (SupoClip):** regras novas contra muleta, resposta solta, conjunção de continuação, aquecimento e fim que invade o assunto seguinte; teaser não conserta abertura fraca.
- **Imagem inutilizável (SupoClip):** descarte de trechos cuja fala indica imagem fora do ar e conferência no `-qa.jpg`, sem novo detector automático.
- **Ficou de fora:** zoom do layout WIDE em torno do movimento e posição do gancho pela faixa da imagem (OpenShorts), largura renderizada de legenda, faixas de legenda queimada na fonte, histerese de layout de ~2,5 s, recorte que evita cortar texto da imagem e zoom no detalhe em plano com tela (SupoClip). Úteis, mas exigem mudança maior no render; ficam como evolução. Também ficaram de fora fluxos de série, recap e trailer, B-roll, render em GPU, visão por Gemini e publicação.

## Revisão de 22/11/2026

Comparadas OpenShorts `0af2185c91` até `60e77143fabb54d9d4c7068ce9a65ef93a69be4d` e SupoClip `7010b4c556` até `8aef4bc108d45bb01fe866db5a2cedccc30d1578`, nos mesmos caminhos. Nenhum código copiado.

- **Vídeo longo (OpenShorts):** a seleção cobre o vídeo inteiro por trechos, em vez de parar nos primeiros minutos (regra em `selection.md`). A cota proporcional de até 60 clipes do OpenShorts não foi adotada: menos clipes fortes continuam valendo mais.
- **Ficou de fora:** legenda menor sem segunda linha, título-gancho fora dos rostos em plano duplo empilhado, empilhamento de entrevista e modo pro (OpenShorts); layout de gameplay, clipes de vídeo sem fala por capítulo e intensidade de edição dinâmica (SupoClip). Exigem mudança no render (`scripts/clips.py`) ou fogem do uso atual. Download multi-conta, filas de GPU, cobrança e planos são operação de SaaS, fora do escopo.

## Revisão de 26/11/2026

Comparadas OpenShorts `60e77143fa` até `be28b3beb6c9` e SupoClip `8aef4bc108` até `29ca74f9f38f`, nos mesmos caminhos. Nenhum código copiado.

- **Bordas por frase (SupoClip):** adaptadas duas regras em `selection.md`: o teto de duração recua o fim uma frase inteira e o falso começo não abre clipe.
- **Ficou de fora:** cobertura de transmissões muito longas por janelas espaçadas (já coberta pela leitura por trechos), ajuste global de sincronia de legenda, exportação SRT/VTT e queima de legenda em GPU (OpenShorts); revisão de borda por chamada extra de modelo e por Whisper local, leitura de legenda queimada na fonte, legenda na emenda de plano duplo empilhado, recorte que desiste de seguir rosto instável e detecção de corte por mudança de cor (SupoClip). Exigem mudança no render (`scripts/clips.py`), custo extra por clipe ou layout que a skill não usa.

