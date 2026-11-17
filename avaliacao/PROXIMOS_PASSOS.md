# Próximos passos (atualizado em 15/11/2026, noite)

## Feito nesta rodada

O plano anterior está cumprido. Detalhes no `LEIAME.md`, itens 42–45.

- **TCC A5, planta tipo:** de 390,75 para **407,58 m²** (real ≈ 413,41; erro de −1,4%).
  - A releitura ampliada recuperou três rótulos mal lidos:
    - "A55m" → 14,75;
    - "A-1.33 m" → 1,38;
    - "A0 A0.m." → 0,70.
  - Ela também lê em pé o texto que o PP-OCRv6 virou de cabeça para baixo.
- **Prazo e custo com a área construída:** quando a resposta é a área útil, os dois usam
  o contorno das paredes, se ele foi medido; sem contorno, mostram um aviso.
- **Medido no prazo:** área + tipo de uso, e ajuste robusto (Huber). Ganharam no
  máximo 4%, empate técnico pela regra do projeto, e o modelo continua o mesmo.
- 140 testes passam.

## Rodada de plantas privadas (15/11, noite)

- **Área:** 15 de 15 certos no lote novo (apartamentos, casas, comercial). Itens 46–47 do
  `LEIAME.md`.
- **Prazo:** a base CNO agora usa curvas próprias por tipo de uso, lido na planta: casas,
  prédios residenciais, comercial. Itens 48–49.

- **Imagens (scan e foto simulada) das plantas privadas:** 6 de 6 certos (item 52).
- **PDF de CAD com fonte embaralhada:** decodificado (itens 50–51).

## O que falta, em ordem de ganho

0. **Fotos reais de plantas privadas** (perspectiva, sombra, papel dobrado): ainda não
   medidas — as fotos do teste foram simuladas a partir do PDF. Não há acervo público com
   área conferida (buscado em 16/11). Quando quiser: fotografe (reta, inclinada 20°–30°,
   com sombra) a casa de Ituverava folha 1 (117,2), o b.rio pág. 2 (34,51), o Autêntico
   pág. 65 (251,57) e a casa padrão de BH 005 (42,89) e salve em
   `avaliacao/plantas/privadas_foto/` (pasta já criada).

1. **Feito:** as 3.759 obras do SIMEC/FNDE viraram a comparação por tipologia (creche Tipo
   1/2, quadra com vestiário) no card do prazo. Ver `LEIAME.md`.
2. **Mais obras públicas reais para o modelo de prazo.** É o que mais melhoraria o
   prazo: hoje são 18 obras, e o R² fica perto de zero.
   - O Obrasgov já foi varrido (ver `LEIAME.md`).
   - Caminho: relatórios de obras do SIMEC publicados por outras universidades
     federais, como já foi feito com a UFV (`train_prazo.py importar-simec`).
   - **Precisa da sua autorização para baixar** cada arquivo (nome, fonte e tamanho).
   - **Busca de 15/11 (noite):**
     - **Universidades:** em 12/11, 162 portais já tinham sido varridos; só a UFV publica
       datas e área.
     - **Dentro dos portais:**
       - TCE-MG: só obras de manutenção do próprio tribunal.
       - Dados abertos de Rondônia: fora do ar (erro 500).
       - Portais de prefeituras: têm datas, mas não a área.
       - Ficha de cada obra no SIMEC (`print.php?obra=…`), que tem a área e a data de
         início: protegida pelo Cloudflare. **Não contornar.**
     - **Única fonte nova:** FNDE, "Obras escolares concluídas em 2024", com 336 obras em
       26 planilhas XLSX de ~10 KB cada (~263 KB no total; o Amapá não tem planilha).
       Os outros anos dão 404. **Conferido (planilha de MG, baixada com autorização):**
       só tem Município, UF e Tipologia — sem datas e sem ID da obra. **Não serve para o
       prazo**; as outras 25 não foram baixadas.
     - **LAI descartada** (decisão sua, 15/11): o caminho em teste é o Painel de Obras do
       SIMEC com a verificação do Cloudflare feita **por você** no navegador do app.
       - Página: <https://www.gov.br/fnde/pt-br/acesso-a-informacao/acoes-e-programas/programas/par/obras-escolares-concluidas/obras-escolares-concluidas-em-2024>
3. **Feito (15/11): tipo de uso no prazo do CNO** (casas, prédios, comercial).
   Abaixo, a nota antiga:
   - Na validação cruzada, o erro caiu 4% no CNO e 2% nas obras públicas.
   - Para usar, o sistema teria de descobrir o uso pelos cômodos da planta
     (dormitório → residencial, sala de aula → educação), ou você escolheria na tela.
   - Vale só se vierem mais obras (item 1).
4. **Os 8 shafts pequenos do TCC** (0,56 a 0,84 m², ≈ 5,8 m² no total). O "X" do
   símbolo cruza o texto e nenhuma das duas IAs lê, nem apagando as linhas. Hoje ficam
   de fora, sem invenção. Uma saída seria um OCR treinado para letra cortada por linhas,
   o que é trabalho grande e de ganho pequeno.
5. **Conferir o modo reserva (só Florence-2) com a releitura nova.** Os testes
   automáticos cobrem esse modo, mas a medida de ponta a ponta não foi refeita. Para
   medir, suba o backend com `AGEPA_OCR=florence` e rode o TCC pág. 4.

## Como rodar

- **Backend** (dentro de `backend/`):
  `../.venv-gpu/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000`
  - Ele sobe o PP-OCRv6 sozinho.
  - Saúde em `http://127.0.0.1:8000/`.
- **Frontend** (dentro de `frontend/`): `py -m http.server 5500 --bind 127.0.0.1`
- **Testes** (dentro de `backend/`):
  `../.venv-gpu/Scripts/python.exe -m unittest discover -s tests`
- **Uma planta pelo servidor:**
  `py avaliacao/ferramentas/analisar_completo.py <arquivo> <saida.json> [pagina]`
- **Gabarito dos PDFs com texto** (dentro de `backend/`):
  `../.venv-gpu/Scripts/python.exe ../avaliacao/ferramentas/gabarito_servico.py .`
  - Referência: 31 acertos, 0 erros, 0 alucinações.
- **Retreinar o prazo** (dentro de `backend/`): `py train_prazo.py treinar`
  - Para o CNO, use as opções descritas no topo do `train_prazo.py`.
