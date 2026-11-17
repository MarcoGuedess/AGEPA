# Avaliação da leitura de plantas (gabarito)

Mede quanto o AGEPA acerta a área comparando a leitura com plantas de **área conhecida**.
De onde vêm as plantas: `../dados/FONTES_PLANTAS.md`.

## Pastas

| Pasta | Conteúdo |
|---|---|
| `plantas/fnde/` | Creches Tipo 1 (R03 2024) e Tipo 2 (R03 2025): folha 01 (implantação + quadro de áreas) e 02 (planta baixa) |
| `plantas/fnde_rar/` | Pacotes originais baixados do FNDE (de onde as folhas foram extraídas) |
| `plantas/ms_ubs/` | Planta de layout das UBS Porte 1 a 5 (Ministério da Saúde, Novo PAC) |
| `plantas/pncp/` | Plantas anexadas a editais do PNCP (4 de 5 não servem; motivo no gabarito) |
| `plantas/anteriores/` | Plantas testadas antes (cópias dos Downloads): Cantina, Pronto Socorro (PARQ), UNIRIO e escola UFU, em PDF e imagem |
| `plantas/tcc_ufsc/` | Plantas do TCC da UFSC (2014) sobre o Residencial São Cristóvão: 10 páginas A5, letras em traço (sem texto no PDF) |
| `resultados/cache/` | Leituras já feitas (refeitas só se a planta ou o código de leitura mudar) |

## Gabarito (`gabarito.csv`)

Uma linha por prancha. A **área real** vem do que está impresso (quadro de áreas,
carimbo) e a coluna `fonte_gabarito` diz onde. Sem valor impresso, fica em branco:
a planta é lida, mas não pontua.

| Coluna | O que é |
|---|---|
| `arquivo` | caminho a partir desta pasta |
| `area_real_m2` | área real (ex.: `1324,27`) |
| `tipo_area` | `construida` ou `util` |
| `total_na_folha` | `sim` se o total está impresso na própria folha |
| `tolerancia_pct` | 0,5 quando o total está na folha; 10 quando o sistema precisa medir |
| `usar` | `não` deixa a linha fora |
| `pagina` | PDF de várias páginas: qual ler (1 = primeira); vazio = o sistema escolhe |

## Rodar (servidor ligado em 127.0.0.1:8000)

```
cd ../backend
py avaliar_plantas.py
py avaliar_plantas.py --como-imagem 150
```

A segunda forma transforma cada PDF em imagem, como uma prancha escaneada, e testa o
OCR. Leva de 15 a 20 minutos por folha grande na CPU.

O relatório sai em `relatorio_avaliacao.md` (e `.csv`):
- **Acerto:** mesmo tipo de área, dentro da tolerância.
- **Tipo diferente:** o sistema leu a área útil ou a envoltória, e o valor é plausível.
- **Erro:** fora disso.
- **Alucinação:** erro com confiança alta.

## Resultados (PDF original)

| Prancha | Real | Antes (13/11) | Depois das correções (14/11) |
|---|---:|---|---|
| Creche Tipo 1, folha 01 | 1.324,27 | 1.324,27, alta: acerto | 1.324,27, alta: **acerto** |
| Creche Tipo 2, folha 01 | 786,07 | 786,07, média: acerto | 786,07, alta: **acerto** |
| Creche Tipo 1, folha 02 | 1.324,27 | 8.462,01 (envoltória): erro | 1.045,25 útil (79%), média |
| Creche Tipo 2, folha 02 | 786,07 | 6.547,50 (envoltória): erro | 617,59 útil (79%), média |
| Mozarlândia (= Tipo 1, folha 02) | 1.324,27 | 10,46, **alta: alucinação** | 1.041,38 útil (79%), média |
| UBS Porte 1 a 5 | — | sem resposta / 729 a 1.536 | 607 a 1.123 útil, média (sem gabarito) |

Placar: antes, 2 acertos, 3 erros e 1 alucinação; depois, **2 acertos, 0 erros e 0 alucinações**.
A área útil (soma dos ambientes) não é a construída: o sistema diz qual das duas leu,
e a avaliação só a aceita na faixa plausível (60% a 100% da construída).

O que foi corrigido:
1. **Rótulo em pedaços:** no PDF do CAD, "A: 9,90 m²" vinha como "A:", "9", ",", "90", "m"
   e "2", fora de ordem. Agora é remontado pela posição, e só se formar
   [prefixo] número unidade.
2. **Texto gravado letra por letra** (UBS 1) é juntado ao ler o PDF.
3. **Etiqueta do ambiente:** se o nome não está na lista de palavras ("INFANTIL 1",
   "AMAMENTAÇÃO"), vale o texto logo acima do valor. Medidas ("45 metros x 65") e
   campos com ":" não contam como nome.
4. **Quadro de esquadrias tomado por quadro de áreas:** agora o quadro precisa ter
   ≥40% de linhas com nome de ambiente ou o cabeçalho "Ambiente". Se contradiz os
   rótulos da planta (mais que 2× de diferença), é descartado. Se discorda mais de
   5%, a confiança não passa de média.
5. **Áreas abertas** (pátio coberto, solário, playground, área verde, horta, "ar
   livre") vão para a área externa, como no quadro de áreas do FNDE.
6. **Envoltória das cotas:** usa a linha de cotas mais curta que cobre os ambientes;
   faixas estreitas (proporção acima de 10:1) são descartadas.

Limite que continua: sem nenhum rótulo de área, a envoltória pode incluir o terreno
(na Tipo 2 sem rótulos, testado à parte, daria 2,29× a área construída). Ela sai
sempre com confiança baixa e com aviso.

### Correções de 14/11 (plantas antigas e TCC da UFSC)

7. **Quadro com nomes em pedaços** (Pronto Socorro em PDF: "Adm." "e" "Laudo do Raio X"):
   cada linha é remontada com todos os textos entre a coluna de nomes e o valor. Valor
   da coluna sem nome = quadro incompleto → confiança no máximo média.
8. **Sobrescrito lido como "€"** ("50,00⁵" → "50,00€" no Pronto Socorro em imagem):
   o dígito sobrescrito é descartado, nunca adivinhado; antes a linha inteira (50 m²) sumia.
9. **PDF sem texto em folha pequena** (plantas do TCC reduzidas para A5): o desenho
   vetorial é renderizado a até 40 px/mm (antes 10), e as letras de 0,7 mm ficam legíveis.
   Folhas grandes (A1, A0) e escaneadas não mudam.
10. **Escolha da página** de PDFs com várias páginas (campo `pagina` na API e seletor na tela).
11. **Área de unidade/bloco** ("UNIDADE 02 / A: 81,41 m²") não é somada aos ambientes
    (seria contar duas vezes); aparece como informação.
12. **Inteiro lido pelo OCR numa folha com casas decimais** ("A=3.00 m²" → "A=300 m1"):
    o rótulo fica ilegível e não é somado (antes virava um banheiro de 300 m²). Vale só
    para OCR: no texto exato do PDF, "159 m²" é o que está escrito.
13. **Variações do "²" lidas pelo OCR** depois de "A=" ("mº", "mª", "mt", "mm2", "m+").
14. **PDFium não aceita duas leituras ao mesmo tempo**: duas análises simultâneas
    derrubavam o servidor. Agora elas entram em fila, cada uma em frações de segundo.

### Resultado final de 14/11 (22 plantas, 13 com área conferida)

| Prancha | Real | Lido | Resultado |
|---|---:|---|---|
| Creches FNDE Tipo 1 e 2, folha 01 | 1.324,27 / 786,07 | iguais, confiança alta | acerto |
| Cantina (PDF e imagem) | 402,43 | 402,43, alta | acerto |
| Pronto Socorro (PDF) | 954,71 (útil) | 954,71, alta | acerto |
| Pronto Socorro (imagem) | 954,71 (útil) | 954,68, média | acerto (antes 904,68) |
| UNIRIO (PDF em traços, OCR) | 6.962,41 | 6.962,41, alta | acerto |
| Escola UFU (PDF e imagem) | 4.042,32 | 4.042,32 | acerto |
| Creches FNDE folha 02 e Mozarlândia | 1.324,27 / 786,07 | área útil, 79% | tipo diferente (plausível) |
| **TCC UFSC, planta Tipo (A5, OCR)** | ≈ 413,41 (útil rotulada) | 372,75, média | **erro (−9,8%)**, rótulos mal lidos ficam de fora |
| TCC UFSC, Subsolos, Térreo, Cobertura | — | sem resposta | garagem/lazer quase sem rótulos de área: nada inventado |

Placar: **9 acertos, 1 erro, 0 alucinações, 0 falhas.**

### Mudanças de 14/11 (tarde): o sistema mede o desenho e lê o prédio inteiro

15. **Total escrito em outra página do documento** (memorial, quadro de áreas): todas as
    páginas com texto são varridas uma vez. O PDF completo do TCC (246 págs.) responde
    **9.127,55 m²** de área construída, escrita nas págs. 39 e 110, com confiança alta.
    A leitura da própria página continua no relatório (`pagina_area`).
16. **Pavimento repetido no título**: "PLANTA TIPO (x12)", "12X" → a área da página
    vale 12 vezes na soma do prédio.
17. **Leitura do prédio inteiro** (campo `paginas` da API: `todas`, `1-5`, `2,4`; na tela,
    "Ler como um prédio"): cada página é lida como uma planta e entra numa tabela por
    pavimento. O total só é dado se **todas** as páginas tiverem área; senão a tabela
    mostra o que foi lido e quais páginas faltam, sem soma parcial no prazo e no custo.
18. **Área medida pelo contorno das paredes** (`services/geometry.py`), sem depender de
    um total escrito: paredes (faixas espessas) → vãos de até 1,6 m fechados → fora =
    o que se alcança da borda ou de um texto de área externa → prédio = blocos com
    ambientes → m² pela escala das cotas. Validação no gabarito (desenho a 8 px/mm):

    | Prancha | Real | Contorno | Razão |
    |---|---:|---:|---:|
    | Cantina | 402,43 | 358,80 | 0,892× |
    | Creche FNDE Tipo 1, folha 02 | 1.324,27 | 1.262,46 | 0,953× |
    | Creche FNDE Tipo 2, folha 02 | 786,07 | 752,62 | 0,957× |
    | Escola UFU | 4.042,32 | 3.308,1 | 0,818× |
    | Pronto Socorro (PDF) | — | 888,76 | menor que a soma dos ambientes: **descartado** |

    Sempre abaixo do real (4% a 18%): paredes finas e janelas largas deixam pedaços de
    fora. Por isso o contorno:
    - só é medido com a escala das cotas **e** com um teto para conferir (soma de todos
      os ambientes, ou retângulo de cotas confiável) — na escola UFU nenhum dos dois
      existe, então o serviço não mede;
    - é descartado se ficar menor que a soma dos ambientes, maior que 1,6× essa soma
      (pegou o terreno: com vão de 1,5 m a escola dava 2,15×) ou maior que o retângulo;
    - vira a **resposta** só quando a planta não escreve área nenhuma (método
      `contorno`, confiança baixa, retângulo das cotas como limite superior); nos
      demais casos aparece como conferência ("1,21× a soma dos ambientes").
    O contorno aceito é desenhado em vermelho na imagem anotada.

19. **Cotas que cobrem só parte da obra** (achado na simulação abaixo): o retângulo das
    cotas guarda onde está no desenho; se a faixa central dos ambientes (10% a 90% das
    posições dos nomes) fica fora dele, as cotas são parciais — o retângulo não serve de
    área nem de limite. Antes, a escola UFU sem os textos de área respondia 994 m² (¼ do
    real) dizendo "o valor fica acima do real"; agora responde que não sabe.
20. **Soma de poucos ambientes**: se menos da metade dos ambientes tem área escrita, a
    soma é só um pedaço do pavimento; com um contorno aceito, o contorno vira a resposta.

**Simulação "planta sem área escrita"** (os rótulos de área, o carimbo e o quadro foram
apagados do PDF e do desenho; ficaram nomes e cotas):

| Prancha | Real | Resposta sem área escrita | Razão |
|---|---:|---|---:|
| Creche FNDE Tipo 1, folha 02 | 1.324,27 | 1.262,46 (contorno, confiança baixa) | 0,953× |
| Creche FNDE Tipo 2, folha 02 | 786,07 | 752,62 (contorno; só 3 de 30 ambientes ficaram com número) | 0,957× |
| Cantina | 402,43 | 358,80 (contorno; só 4 de 11 ambientes com número) | 0,892× |
| Escola UFU | 4.042,32 | sem resposta: cotas parciais, nada para conferir | — |

Com os textos, todas as plantas do gabarito dão as mesmas respostas de antes (o contorno
aparece como conferência).

Tentativas anteriores do contorno (antes desta versão) erraram de 0,3× a 10×: o
mobiliário e as linhas finas fechavam regiões falsas. O que resolveu foi separar
paredes por espessura (abertura maior que as linhas finas), fechar só vãos de porta e
semear "fora" também pelos textos de área externa (jardim, pátio, playground).

**Limite que continua (TCC da UFSC, A5):** as páginas 1–3 e 5 (subsolos, térreo,
cobertura) não têm texto legível — letras de 0,35 mm com traço de 0,18 mm, ilegíveis
mesmo a 60 px/mm — e o OCR não acha nelas cotas coerentes. Sem nenhum número, não há escala: o
desenho não pode ser convertido em m² sem inventar a escala, então essas páginas ficam
"sem área legível" e o total do prédio não é dado. As páginas 6–10 são plantas de
fôrma (estrutura), não de arquitetura.

Lendo as páginas 1–5 como um prédio (28 min de OCR): página 4 = 372,75 m² (útil) ×12
pelo título "A TIPO (x12)"; páginas 1, 2, 3 e 5 sem área; soma do que foi lido
(4.473,00 m²) mostrada, mas **não** usada no prazo e no custo. Levar a escala de uma
página para as outras também não serve: cada planta foi reduzida de um jeito para
caber na A5 (o Subsolo 1, que ocupa o terreno todo, sai com 141 mm de largura; a
torre do tipo, com 132 mm) — e nem a página 4 tem cotas legíveis. Já o PDF completo
do TCC responde pelo total do memorial (9.127,55 m², item 15).

### 15/11: plantas novas da internet (23 arquivos, nunca usados antes)

Lote baixado com autorização em `plantas/novas/` (uma pasta por fonte): 15 casas-padrão
da Prefeitura de BH, a quadra coberta do FNDE (folhas + memorial), a Policlínica de
referência do Ministério da Saúde, o edital de casas da AGEHAB-GO, o memorial de casa
rural da CAR-BA, a publicação Habitação Social em Madeira e o material de venda do
Villa Leblon (planta humanizada).

**O que a linha de base (14/11) errou e o que mudou:**

21. **Paredes desenhadas como duas linhas finas, sem hachura** (casas da PBH): o contorno
    via só os azulejos da cozinha (10–15 m² numa casa de 31–60 m²) e passava, porque não
    havia piso de conferência. Agora: (a) retângulo do PRÉDIO pelas cotas guardado à
    parte; (b) contorno abaixo de 35% dele é descartado; (c) plano B — se o contorno
    normal falhar, reconhece pares de linhas paralelas LONGAS (≥ 40 cm, a até 30 cm uma
    da outra) só dentro do retângulo do prédio, e recorta o resultado nas faces externas.
    Não é o padrão porque cercas de pátio também são linhas duplas.
22. **Cadeia interna × externa das cotas:** entre as linhas que cobrem os ambientes,
    vale a COTA TOTAL (um valor sozinho, "645"), senão a mais longa até 12% acima da
    mais curta. Antes, a interna (615) dava 6,15 m em vez de 6,45 m.
23. **Largura das portas pelos rótulos** "80X220", "90X220" (altura ≥ 200 = porta):
    o vão fechado no contorno passa a ser a maior porta +10% (antes, sempre 1,6 m).
24. **Área em texto corrido** (memoriais, TCC, publicações): "possui 51,34 m² de área
    construída", "área construída total de 52 m²", "equivalente a 3.213,00 m²".
    (Corrigido depois: "área total de 980,40 m² de **cobertura**" é a área do telhado,
    não a construída — veja a quadra no fim desta seção.)
25. **O que não é a área da obra:** exigência de edital ("no mínimo 46,00 m²"),
    terreno, ampliação (na mesma frase), a área de UM pavimento ("Área do Pavimento
    Térreo") e a de um apartamento numerado num material de venda.
26. **Documento com várias áreas** (edital com opções A/B/C): vale a que mais se
    repete; empate, a primeira citada — com confiança baixa e as outras listadas.
27. **Área privativa** de material de venda: lida (o total depois do "="), com o tipo
    "privativa — uma unidade" e aviso de que não é a área do prédio.

**Resultado (PDFs com texto, 15/11):**

| Planta | Real | Lido | |
|---|---:|---|---|
| 15 casas-padrão PBH (sem área escrita) | 31,40 a 59,99 | contorno: erro médio **2,6%**, máx. 5,2% | 15 acertos (antes: 10 a 15 m², 25–35% do real) |
| Memorial CAR-BA | 51,34 | 51,34 (documento) | acerto (antes: sem resposta) |
| Habitação em Madeira | 52 | 52 (documento) | acerto |
| Quadra FNDE (memorial) | 785,00 (só na folha) | sem resposta: o memorial só traz 980,40 m² **de cobertura** | certo (ver correção abaixo) |
| Policlínica MS | 3.213 | 3.213 (documento; antes pegaria 3.173,60, um pavimento) | acerto |
| Edital AGEHAB (3 opções) | 54,54* | 54,54, confiança baixa, demais listadas | acerto (antes: 46,00, a exigência mínima) |
| Villa Leblon, apto 101 e cobertura 403 | 51,04 / 75,65 | idem, "privativa — uma unidade" | acerto (antes: 75,65 dita área do prédio) |
| Gabarito antigo (13 PDFs) | — | mesmas respostas de antes | sem regressão |

Placar dos PDFs com texto: **27 acertos, 3 área útil (plausível), 0 erros, 0
alucinações**. Contorno validado agora em 19 plantas: de 18% para menos a 5% para mais.

**Limite do OCR (Florence-2-base, CPU):** a quadra do FNDE vem em folha de 625 × 900 mm
sem camada de texto. Foram 20 minutos de OCR, 540 textos e nenhuma cota coerente nem
área legível: o sistema respondeu "sem área" (certo, mas inútil). As 3 plantas em
imagem do gabarito continuam certas (402,43 / 954,68 / 4.042,32), mas sem contorno:
o OCR não dá cotas suficientes para a escala.

### 15/11 (tarde): 2º lote — área E prazo (9 arquivos novos, `plantas/novas2/`)

Plantas, folha estrutural, memoriais e editais com o prazo de execução, para conferir
também o prazo. O que a linha de base errou e o que mudou:

28. **"ÁREA CONSTRUÇÃO"** (sem "de") e carimbo de uma linha só com a área útil ao
    lado: as palavras que mudam o sentido ("útil", "terreno", "existente") só contam
    entre o rótulo e o número, ou logo depois dele até o próximo rótulo.
29. **Quadro com cabeçalho "ÁREAS:"** ("TERRENO 1.100,00 m² / CONSTRUÍDA 314,94 m²"):
    a linha "CONSTRUÍDA" logo abaixo do cabeçalho é a área construída. Antes, a folha
    estrutural de Luziânia dava 1.136,99 m² pelos eixos (coordenadas, não cotas).
30. **Mais frases de memorial:** "área construída útil de", "superfície total de",
    "obra com área de", e a lista por pavimento terminada em "TOTAL: 460,45 m²".
31. **Área de UM pavimento** ("Pav. Térreo: 312,45 m²", "o pavimento térreo, que possui
    área total de…") não é o total; "edificação de dois pavimentos" (contagem) não exclui.
32. **Terreno citado antes, na mesma frase,** não anula a área construída ("implantada
    num terreno de 5.650 m², com uma área construída útil de 1.161,77 m²").
33. **Número de outra oração:** em "51,34 m² de área construída e 40,01 m² de área
    útil", o 40,01 não é da área construída.
34. **Prazo de execução escrito no documento** (novo): "prazo de execução … 240
    (duzentos e quarenta) dias", "… 16 (dezesseis) meses", "12 meses para construção".
    Prazos de processo ("3 dias úteis" de recurso, validade da proposta) não contam.
    Aparece no cartão do prazo ao lado do previsto pelo modelo: é o prazo CONTRATADO.

| Arquivo | Real | Antes | Agora |
|---|---:|---|---|
| CAR-BA, plantas (folha A2) | 64,82 | 57,22 útil (soma) | **64,82** carimbo, alta |
| Luziânia, folha estrutural do CRAS | 314,94 | 1.136,99 (eixos) | **314,94** carimbo, alta |
| CRAS Glória (POA), memorial | 460,45 | 312,45 (só o térreo) | **460,45**, alta |
| CRAS Águas Lindas, memorial | 456,22 | sem resposta | **456,22** |
| UBS Mauá, termo | 1.161,77 | sem resposta | **1.161,77** |
| SUAS Goiás (tabelas, não é planta) | — | sem resposta | sem resposta (certo) |

| Documento | Prazo escrito | Modelo (obras reais) |
|---|---|---|
| UBS Mauá (1.161,77 m²) | 24 meses | 14,6 meses (faixa 80%: 7,1–30,2) |
| UBS Caçapava do Sul | 240 dias = 8 meses | — (edital sem área) |
| UBS Jd. Tropical, Salto | 16 meses | — (edital sem área) |
| Creche Santos Dumont, edital | não está neste PDF (está no termo de referência) | — |
| TCC UFSC (9.127,55 m²) | 539 dias úteis ≈ 25 meses (planejado) | 27,5 meses |

### 15/11: a IA de leitura (OCR) — limites e comparação

**Máquina:** Intel i5-12500H (16 threads), 15,6 GB de RAM, **NVIDIA RTX 3050 Laptop
(4 GB)**. O PyTorch instalado era só de CPU: a placa de vídeo ficava parada.

**Limites medidos do Florence-2-base (o OCR atual):**
- lento na CPU: 2 a 20 min por planta em imagem ou PDF sem texto;
- é um modelo geral de visão (232 M parâmetros), não um OCR dedicado: lê "ARE A
  CONSTRUUR" e "402.43 mf" e depende de releituras ampliadas para confirmar;
- na folha da quadra do FNDE (625 × 900 mm, sem camada de texto): 540 textos, nenhuma
  cota coerente e nenhuma área — o quadro "ÁREA CONSTRUÍDA: 785,00 m²" não saiu;
- as cotas que ele lê raramente bastam para dar escala ao contorno nas imagens.

**Comparação nas 5 plantas difíceis (imagem ou PDF sem texto):**

| Planta | Real | Florence-2, CPU | Florence-2, GPU | PP-OCRv6 small, CPU* |
|---|---:|---|---|---|
| Cantina (imagem) | 402,43 | 402,43 · 293 s | 402,43 · 142 s | 402,43 · 12 s |
| Pronto Socorro PARQ (imagem) | 954,71 útil | 954,68 · 686 s | 954,68 · 284 s | **954,71** · 125 s |
| Escola UFU (imagem 28 MB) | 4.042,32 | 4.042,32 · 630 s | 4.042,32 · 246 s | 4.042,32 · 142 s |
| TCC A5, planta tipo | ≈ 413,41 útil | 372,75 · 282 s | 372,75 · 120 s | 349,83 · 50 s |
| Quadra FNDE (folha sem texto) | 785,00 | nada · 1.192 s | nada · 540 s | **785,00** · 65 s |
| **Total** | | **3.083 s** | **1.332 s** | **394 s** |

\* PP-OCRv6 (PaddleOCR 3.7, jun/2026) num ambiente separado, só o OCR trocado: os
textos dele entram no MESMO interpretador do AGEPA. Sem as releituras ampliadas do
Florence-2 (que ajudam nas letras minúsculas do TCC).

**Correção que veio do teste:** o PP-OCRv6 leu na folha da quadra "ÁREA CONSTRUÍDA:
785,00 m²" e "ÁREA COBERTURA: 980,40 m²". O gabarito estava com 980,40 (tirado do
memorial, que só fala em "área total de 980,40 m² de cobertura") — era a área do
telhado. Gabarito corrigido para 785,00 e a regra de texto não aceita mais "de
cobertura" como área construída.

**Placar final dos PDFs com texto (gabarito com 3 lotes):** 31 acertos, 3 área útil
(plausível), 1 sem resposta certa (memorial só com área de cobertura), 6 sem gabarito,
**0 erros, 0 alucinações**.

### 15/11 (tarde): OCR híbrido implementado — PP-OCRv6 + Florence-2

**Como ficou a leitura de plantas em imagem ou PDF sem texto:**
1. **1ª leitura, PP-OCRv6 small** (PaddleOCR 3.7): lê a prancha inteira em blocos de
   1.280 px (15% de sobreposição), cada linha com a sua orientação (0°, 90°, 180°, 270°).
   Roda num processo auxiliar (`backend/ocr_worker.py`) no ambiente `.venv-ocr`
   (Python 3.12), porque o PaddlePaddle ainda não tem versão para o Python 3.14 do
   backend. O backend inicia esse processo sozinho, o mantém aberto e conversa com ele
   por linhas JSON (`backend/services/paddle_ocr.py`).
2. **Conferência, Florence-2 na GPU:** as releituras ampliadas continuam com ele
   (célula do carimbo, quadro de áreas, rótulos de área com 2 votos).
3. **Leitura completa:** se 3+ ambientes (e 5%+ deles) ficam sem área, o Florence-2 lê
   a prancha também e só **preenche o que o PP-OCRv6 não leu**.
4. **Reserva:** sem o `.venv-ocr`, ou se o processo falhar ou travar, a leitura volta
   inteira para o Florence-2, como antes. `AGEPA_OCR=florence` desliga o OCR rápido;
   `AGEPA_OCR_PYTHON` aponta outro Python para o processo auxiliar.
5. **Na tela:** "Servidor online · GPU · OCR rápido"; no resultado, "Fonte do texto:
   OCR (PP-OCRv6_small + Florence-2: leitura rápida + conferência dos números)".

Para iniciar o backend (de dentro de `backend/`):
`..\.venv-gpu\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000`
— o processo do PP-OCRv6 sobe sozinho a partir de `..\.venv-ocr`.

**Correções que vieram dos testes do híbrido:**

35. **Releitura ambígua não vota:** recorte que pega dois números diferentes (o vizinho
    do carimbo) não confirma nada — na escola, "2.029,49" chegou a virar "8.117,96".
36. **Quadro de áreas relido só substitui a leitura se tiver MAIS linhas** (no PARQ, a
    troca com o mesmo número de linhas tirava 0,05 m²).
37. **O Florence-2 só completa:** onde os dois leram, fica o PP-OCRv6. Juntar pelo "mais
    longo vence" trocou o "A=3.14 m²" certo do elevador do TCC por "A-5.14 mm2".
38. **Pedaço do Florence-2 dentro de uma linha que o PP-OCRv6 leu inteira não entra:** o
    fim de "PD laje=2.675m" (pé-direito) saía como "A=2.67m" e virava um ambiente de
    2,67 m² que não existe.
39. **Caixa achatada do Florence-2 (< 3 px) é descartada:** nos testes era lixo
    ("www.pixabay.com", "Portuguese-Spanish", "A =124,141 m2" numa caixa de 0 px na
    quadra) ou rótulo cortado na borda do bloco — o do elevador do TCC entrava 2 vezes.
40. **Rótulo abaixo do quadrinho da unidade é do ambiente:** "UNIDADE 02 / A: 81.41 m²"
    e, logo abaixo, "HALL / A=7.50 m²" — o hall ficava sem área.
41. **"A=7.50" sem unidade e "A=2.67m" lido pelo OCR só valem com nome.** A linha da porta
    corta o "m²" do rótulo (nos PDFs das UBS-MS o "m2" é um texto separado) e "m"
    sozinho é unidade de comprimento. Vale o nome de um ambiente por perto ou a
    etiqueta logo acima ("VARAN": a parede corta o "VARANDA"); solto, não vira ambiente.

**As 5 plantas difíceis, do começo ao fim (servidor, GPU):**

| Planta | Real | Florence-2, GPU (antes) | Híbrido (agora) |
|---|---:|---|---|
| Cantina (imagem) | 402,43 | 402,43 · 142 s | 402,43 · 14 s |
| Pronto Socorro PARQ (imagem) | 954,71 útil | 954,68 · 284 s | **954,71** · 169 s |
| Escola UFU (imagem 28 MB) | 4.042,32 | 4.042,32 · 246 s | 4.042,32 · 166 s |
| TCC A5, planta tipo | ≈ 413,41 útil | 372,75 (−9,8%) · 120 s | **390,75 (−5,5%)** · 124 s |
| Quadra FNDE (folha sem texto) | 785,00 | nada · 540 s | **785,00** · 75 s |
| **Total** | | **1.332 s** | **548 s** (2,4× mais rápido; 5,6× o Florence-2 na CPU) |

Só o PP-OCRv6, sem o Florence-2: 349,83 no TCC. Só o Florence-2 (a reserva), com as
correções acima: 373,71 · 69 s.

**TCC A5, planta tipo:** os valores conferidos na imagem batem (elevador 3,14;
dormitório 2 = 9,00; banho 3,00; varanda 4,04; halls 7,50) e o ambiente falso de
2,67 m² saiu. O que ainda falta (≈ 23 m²) são rótulos cortados por linhas do desenho
(portas, paredes, o "X" do vazio) que nenhum dos dois OCR leu.

**PDFs com texto (não usam OCR):** 31 acertos, 3 área útil, 1 sem resposta certa,
**0 erros, 0 alucinações** — as mesmas áreas de antes. Nas plantas de layout das
UBS-MS (sem gabarito) a regra 41 passou a ler rótulos como "Recepção / A: 16,51 m2"; a
área (do quadro) não mudou, só a confiança: UBS4 e UBS5 subiram para alta (soma dos
rótulos agora a 2% do quadro); UBS3 desceu para média (rótulos 5,4% acima do quadro).

**Testes automáticos do backend:** 129 (eram 122).

### 15/11 (noite): rótulos recuperados no TCC e prazo com a área construída

**Correções:**

42. **Rótulo de área que não virou número é relido ampliado, com voto.** Texto com cara
    de rótulo ("A55m", "A-1.33 m", "-0,06 m2") — fora a área da unidade, pé-direito e
    medidas — é relido pelo Florence-2 em dois tamanhos e pelo PP-OCRv6 no recorte.
    Só vota o texto lido em cima do rótulo (o recorte pode pegar o vizinho); dois votos
    iguais = o rótulo entra, conferido; sem consenso, fica de fora. Só quando a área é a
    soma dos rótulos (não no carimbo nem no quadro de áreas).
43. **Texto "de cabeça para baixo" também é lido em pé.** Na letra miúda do TCC, o
    classificador de orientação do PP-OCRv6 virou 8 linhas (180°): "A=14.75m²" saiu
    "A55m" e "ÁREA SOCIAL" saiu "ARCA SAL".
44. **Prazo e custo com a área construída.** Os modelos de prazo foram treinados com área
    construída e o CUB é por m² construído. Quando a resposta é a área útil (soma dos
    ambientes) e o contorno das paredes foi medido e conferido, prazo e custo usam o
    contorno; sem contorno, um aviso diz que os dois saem baixos. Creche FNDE tipo 1:
    útil 1.045,25 → contorno 1.262,46 (real 1.324,27); prazo 14,2 → **15,0 meses** (com a
    área real daria 15,2); CNO 18,3 → 19,3. Na tela, o card do prazo diz qual área usou.
45. **Barra de progresso:** quando a etapa de releitura voltava (quadro, depois a
    conferência dos rótulos), o tempo restante saía menor que o real.

**TCC A5, planta tipo (≈ 413,41 m² útil):**

| Rodada | Resultado | Erro |
|---|---:|---:|
| Só Florence-2 | 372,75 | −9,8% |
| Híbrido (15/11, tarde) | 390,75 | −5,5% |
| **Híbrido + releitura dos rótulos (agora)** | **407,58** | **−1,4%** |

Recuperados e conferidos na imagem: 14,75 (área social da unidade 04), 1,38 e 0,70
(vazios). O que falta (≈ 5,8 m²) são 8 shafts pequenos (0,56 a 0,84 m²) cortados pelo
"X" do símbolo: nem apagando as linhas as duas IAs leem — ficam de fora, nada inventado.
O tempo da análise variou com a máquina (194 s nesta rodada; a 1ª leitura do PP-OCRv6
sozinha foi de ~50 para 66 s com o mesmo código); a releitura nova custou ~4 s.

**Prazo — o que foi medido (validação cruzada, erro em obras que o modelo não viu):**

| Variante | Obras públicas (18) MAE | CNO (3.000) MAE |
|---|---:|---:|
| Área (modelo atual) | 5,70 meses | 5,60 meses |
| Área + tipo de uso | 5,61 | 5,46 |
| Área + tipo de uso, inclinação por uso | 5,59 | 5,36 |
| Potência robusta (Huber), só área | 5,57 | 5,60 |

Tudo dentro de 5% — pela regra do projeto, empate técnico: o modelo continua o mesmo
(a potência robusta entrou na comparação dos relatórios de treino). O intervalo de 80%
está calibrado (cobre 78% a 83% das obras). O limite é a quantidade de obras públicas
reais: no Obrasgov, das ~9.200 obras concluídas lidas (7 UFs não responderam à API), 162 têm
datas efetivas e só 5 dizem a área.

**Testes automáticos do backend:** 140.

### 15/11 (noite): prazo de obras municipais do FNDE (SIMEC, transparência de obras)

**Fonte:** planilha nacional de transparência de obras do SIMEC. Você baixou no seu
navegador, depois de passar pela verificação do Cloudflare: 30.749 obras, 19.842 concluídas.
O arquivo está em `dados/simec/`. **Não há data real de conclusão**:
- o início é a assinatura do contrato;
- os fins possíveis são o término do contrato (vigência, com aditivos) ou a última vistoria
  "Concluído", que é um limite de cima.

Extração (`avaliacao/ferramentas/simec_extrair.py` → `dados/obras_simec_fnde.csv`):
- tipologias com área construída conferida nas pranchas do FNDE: creche Tipo 1 (1.324,27 m²),
  creche Tipo 2 (786,07 m²) e quadra coberta com vestiário (785,00 m²);
- só obras concluídas, construção e 100% executadas: **3.759 obras usáveis**;
- 16 ficaram de fora porque o nome fala de outro projeto ("Tipo B" numa linha "Tipo 2").

| Projeto padrão | Obras | Mediana até o fim do contrato | 80% das obras | Modelo "Obras públicas" (18 obras) |
|---|---:|---:|---|---:|
| Quadra com vestiário (785 m²) | 2.909 | 24,0 meses | 9 a 56 | 13,0 |
| Creche Tipo 2 (786 m²) | 363 | 30,0 | 12 a 70 | 13,0 |
| Creche Tipo 1 (1.324 m²) | 487 | 30,0 | 12 a 66 | 15,2 |

**O que isso mostra:**
- Nessas obras, o modelo atual erra em média 17 a 22 meses, e o intervalo de 80% dele cobre só
  41% a 52% delas.
- O porte quase não explica o tempo: a creche de 786 m² leva o mesmo que a de 1.324 m².
- Na validação cruzada, todos os modelos por área erram ≈ 14,5 meses (relatório de teste em
  `dados/relatorio_teste_simec_fnde.md`).
- O que pesa é a gestão da obra municipal: paralisações, rescisões e aditivos. As 18 obras do
  modelo principal são, na maioria, prédios de universidade federal — outro contexto.
- **Nada disso entrou no servidor ainda:** os modelos de prazo continuam os mesmos.

**Implementado — comparação por tipologia FNDE (decisão sua, 15/11):** quando a planta é um
projeto padrão do FNDE, o card do prazo mostra também o prazo das obras **iguais** no SIMEC.
- Reconhecimento estrito (`backend/services/fnde_tipologias.py`):
  - a prancha cita FNDE ou Proinfância;
  - creche: as palavras + "Tipo 1"/"Tipo 2" (sem o tipo escrito, decide a área, ±15%);
  - quadra: "quadra" + "vestiário", sem "palco".
- Tabela gerada por `py train_prazo.py tipologias-fnde` → `backend/models/prazo_fnde_tipologias.json`.

| Planta testada | Reconhecida | Comparação no card |
|---|---|---|
| Creche FNDE Tipo 1 (planta baixa) | creche Tipo 1 | 30,0 meses (80%: 12,7–66,0), 487 obras |
| Creche FNDE Tipo 2 (implantação) | creche Tipo 2 | 30,0 meses (12,0–63,4), 363 obras |
| Creche Tipo 1 de Mozarlândia-GO | creche Tipo 1 | 30,0 meses |
| Quadra FNDE com vestiário (folha sem texto, OCR) | quadra | 24,0 meses (8,9–54,9), 2.909 obras |
| UBS 1 (MS) e casa padrão de BH | nenhuma (certo) | — |

O modelo principal (18 obras) e o do CNO continuam iguais e aparecem ao lado; nada foi
misturado. A nota do card diz que as datas são de contrato.
**Testes automáticos do backend:** 143.

### 15/11 (noite): rodada de plantas PRIVADAS

**Lote novo** (baixado com sua autorização, `avaliacao/plantas/privadas/`, ~107 MB). Áreas
conferidas no próprio documento:

| Tipo | Arquivo / fonte | Casos no gabarito |
|---|---|---:|
| Apartamentos | book b.rio (RJ), Ryad e Autêntico (Goiânia) — Invexo | 12 |
| Casa térrea | pranchas de TCC, Feituverava (Ituverava-SP) — 117,2 m² | 1 |
| Casa | pranchas de TCC, UNIFAP (Macapá) — carimbo com "Área Construída: 0,00 m²" (não preenchido) | sem gabarito |
| Comercial | memoriais do Open Mall (Palmas) e da Eco Paradise (Paraíso do Tocantins) — ULBRA-TO | 2 |

**Primeira medida: 8 de 15 certos.** Corrigido:

46. **Legenda de planta de venda é a área privativa da unidade.** Exemplos: "Apartamento Tipo
    - Final 01 - 251,57m²", "FINAL 1 – 220,71 m²", "PENTHOUSE 1 – 485,17m²". Duas legendas com
    valores diferentes na mesma página = nada. Sem isso, a página do book do Autêntico caía na
    área do empreendimento inteiro ("ÁREA CONSTRUÍDA: 71.296,16m²", pág. 11) — número certo do
    lugar errado. Agora o documento só entra como nota: "é a área do empreendimento inteiro;
    a desta página é a da unidade".
47. **Quadro "ÁREAS (m2)" de casa:** "TERRENO 223,00 / CONSTRUÇÃO 117,2 / LIVRE 105,8".
    - "Construção" sob o cabeçalho vale como área construída.
    - O valor com UMA casa decimal só é aceito nessa linha do quadro.
    - Antes, a soma dos cômodos dava 140,2 (+20%), porque somava a garagem.

**Depois: 15 de 15 certos.** A casa da UNIFAP, sem gabarito, não caiu no "0,00" nem no terreno.

**Gabarito completo dos PDFs com texto:** 46 acertos (31 + 15), 3 área útil, 1 sem resposta
certa, 7 sem gabarito, **0 erros, 0 alucinações**; as linhas antigas ficaram iguais.

**Prazo de obra privada por tipo de uso (base CNO):**

48. O tipo de uso é lido no texto da planta (`backend/services/uso_obra.py`):

    | Texto da planta | Tipo de uso |
    |---|---|
    | dormitório/quarto/suíte | casa |
    | + apartamento/unidade N/pavimento tipo, ou área privativa de planta de venda | prédio residencial |
    | loja/sala comercial/escritório/shopping | comercial |
    | galpão (mesmo com escritório) | galpão |
    | prédio público (escola, creche, UBS, FNDE…) | nenhum |

    O CNO ganhou curvas próprias (`train_prazo.py treinar … --por-uso`). Cada uma só entra se
    errar menos que a geral, em validação cruzada, nas obras daquele uso. Fora da faixa de
    área do uso (2% a 98% das obras), vale a curva geral.

| Uso (CNO) | Obras | Erro médio da curva geral | Erro da curva própria | Viés da curva geral |
|---|---:|---:|---:|---|
| Casas | 1.772 | 5,40 meses | **5,15** | subestimava 7% |
| Prédios residenciais | 370 | 6,77 | **6,57** (erro típico 4,81 → 3,65) | **superestimava 18%** |
| Comercial | 176 | 9,07 | **8,27** (erro típico 6,61 → 5,27) | **superestimava 21%** |
| Galpão | 94 | 10,00 | 10,25 (pior: fica a geral) | — |

Exemplos de ponta a ponta:
- casa de Ituverava: "CNO · casas", 10,8 meses (a geral dava 10,1);
- Open Mall: "CNO · obras comerciais", 21,3 meses;
- creche do FNDE: sem uso privado, continua na curva geral.

49. **Área privativa (uma unidade):** o prazo e o custo saem com nota de que são de UMA
    unidade; para a obra, vale a área construída do prédio.

**Testes automáticos do backend:** 150.

### 15/11 (noite): PDF de CAD com fonte "embaralhada"

50. **Fonte sem mapa de caracteres, decodificada.** Em exportações de CAD com fonte TrueType
    sem a tabela ToUnicode, o PDFium devolve o **número do glifo**, e o texto sai trocado.
    - Na casa da UNIFAP: "'RUPLWyULR" = "Dormitório", "$ \x18\x14\x0f\x14\x1b" = "A=51,18".
    - Na ordem padrão do Mac, o glifo é o código da letra menos 29; os acentos (glifos
      98–129) seguem a mesma ordem.
    - Os algarismos sobrevivem como caracteres de controle e o texto volta **exato**, sem OCR.
    - Detecção por **objeto** de fonte (o PDF tinha duas "Arial-ItalicMT", uma com mapa e
      outra sem). Só decodifica com prova: palavras de planta ou números passam a aparecer.
    - PDF normal: custo zero, nada muda.
    - Fora da tabela, só "número m?" → "m²" e "?rea" → "Área".
51. **Piscina e deck** são áreas abertas: não entram na soma dos ambientes.

| Casa da UNIFAP (sem gabarito: o carimbo diz "Área Construída: 0,00 m²") | Antes | Agora |
|---|---|---|
| Rótulos de área lidos | 0 (texto ilegível) | 19 ("A=12,80 m²", "A=51,18 m²"…) |
| Resposta | 188,1 m² pelo contorno, confiança baixa | 193,45 m² útil (soma dos rótulos, sem área descoberta, piscina e deck), média |

**Gabarito completo dos PDFs com texto:** igual ao anterior em todas as outras linhas — 46
acertos, 0 erros, 0 alucinações. **Testes automáticos do backend:** 153.

### 15/11 (noite): plantas PRIVADAS em imagem (OCR)

Imagens geradas das plantas privadas, **sem a camada de texto**
(`avaliacao/plantas/privadas_imagem/`):
- "scan" a 150 dpi;
- "foto de celular" simulada: resolução menor, 1° a 1,5° torta, desfocada e em JPEG.

Rodadas pelo servidor (PP-OCRv6 + Florence-2, GPU).

| Imagem | Real | Resultado | Tempo |
|---|---:|---|---:|
| Casa de Ituverava, scan | 117,2 construída | **117,2**, alta (quadro "ÁREAS (m2)") · uso: casa | 40 s |
| Casa de Ituverava, foto | 117,2 | **117,2**, média · casa | 22 s |
| Apto b.rio, scan | 34,51 privativa | **34,51**, alta · prédio residencial | 18 s |
| Apto b.rio, foto | 34,51 | **34,51**, alta · prédio residencial | 6 s |
| Apto Ryad, scan | 220,71 privativa | **220,71**, alta · prédio residencial | 12 s |
| Apto Autêntico, scan | 251,57 privativa | **251,57**, alta · prédio residencial | 9 s |

**Primeira rodada: 5 de 6.** No Ryad, o OCR leu a legenda colada, "FINAL1-220,71m²*", e
ela não era reconhecida.

52. **Legenda de venda lida pelo OCR:** número grudado na palavra ("FINAL1") e travessão
    sem espaços também valem. **Depois: 6 de 6**, e o gabarito dos PDFs com texto ficou igual
    (46 acertos, 0 erros).

**Limite:** as fotos são simuladas a partir do PDF. Foto real, com perspectiva, sombra e
dobra do papel, ainda não foi medida. **Testes automáticos do backend:** 153.
