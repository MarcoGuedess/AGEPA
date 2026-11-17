# Dados de obras reais — modelo de prazo do AGEPA

O prazo precisa aprender com **obras concluídas**: a área construída e quanto
tempo cada uma levou de verdade. Esta pasta guarda esses dados.

O AGEPA mostra **dois prazos lado a lado**, nunca misturados:

| Modelo | Arquivo | Dados | Papel |
|---|---|---|---|
| Obras públicas | `backend/models/prazo_model.json` | `obras_reais.csv` (UFV/SIMEC, Obrasgov) | principal |
| CNO · obras privadas | `backend/models/prazo_model_cno.json` | `obras_cno.csv` (Receita Federal) | comparação |

Sem `prazo_model.json`, o principal volta ao modelo **demonstrativo** (dados
simulados do `kaggle_training_script.py`, mostrado como tal). Sem
`prazo_model_cno.json`, a comparação some. Os dois arquivos são recarregados
sozinhos quando um novo treino os grava.

## 1. Preencha `obras_reais.csv`

Uma linha por obra **concluída**. Abra no Excel e salve como
**CSV (separado por ponto e vírgula)** ou **CSV UTF-8**.
Linhas que começam com `#` são ignoradas.

| Coluna | Obrigatória | O que colocar |
|---|---|---|
| `obra` | sim | Nome ou identificação da obra (sem repetir). |
| `arquivo_planta` | não | Nome do PDF da planta (ajuda a conferir e é preenchido pelo comando `extrair`). |
| `area_m2` | sim | **Área construída** total (carimbo, memorial ou quadro de áreas). Ex.: `402,43`. |
| `qtd_comodos` | não | Número de ambientes. Se preencher, preencha em **todas** as linhas. |
| `data_inicio` | sim | Início **real** da obra: data da ordem de serviço ou do início efetivo. Ex.: `15/03/2019`. |
| `data_termino` | sim | Fim **real**: termo de recebimento provisório (ou definitivo). |
| `dias_paralisados` | não | Dias em que a obra ficou parada (é descontado do prazo). Vazio = 0. |
| `uso` | não | `residencial`, `educacao`, `saude`, `comercial`, `industrial`... (aparece no relatório). |
| `pavimentos` | não | Número de pavimentos. |
| `uf` | não | Estado da obra. |
| `fonte` | não | Onde achou os dados (link do portal, nº do contrato, documento). **Guarde sempre:** a banca pode pedir. |
| `usar` | não | `não` para deixar a obra fora do treino sem apagar a linha. Vazio = sim. |
| `observacoes` | não | Qualquer nota. |

**Use datas reais, não as previstas.** O prazo do contrato diz quanto a obra
*deveria* levar; o modelo precisa aprender quanto ela *levou*.

### Onde encontrar (fontes conferidas em 12/11/2026)

**1. Dados abertos de universidades federais (relatório do SIMEC) — comece por aqui.**
Cada obra traz data de início, data de término, situação e, na coluna
"Composição", quase sempre a área ("área total de 599,92 m²").
- UFV: <https://dados.ufv.br/dataset/obras-e-reformas> → recurso **Obras (CSV)**.
  178 obras, 132 concluídas, 89 com área na descrição.
- Outras universidades e institutos: varri os portais `dados.<sigla>.br` de 162
  endereços (12/11/2026). **Só a UFV publica o relatório completo** (datas + área).
  - UFRN (<https://dados.ufrn.br/dataset/obras>, 840 obras finalizadas) e UFOP
    (<https://dados.ufop.br/dataset/obras>): têm o **período do contrato** (na UFRN,
    266 obras com prazo prorrogado, mais perto do real), mas **não têm a área** —
    só servem buscando a área de cada obra no edital/projeto.
  - UFVJM: ~60 obras com área, mas o fim é a **previsão** de entrega.
  - Não servem (sem início, sem área ou só obras em andamento): UNIVASF, IFMS,
    IFBA, IFRR, UFC, UFJF, UFRB e o painel de obras da Prefeitura de BH.

Ao importar um relatório no formato do SIMEC (como o da UFV), use `importar-simec`;
em outro formato, preencha a planilha à mão.

Ao copiar para a planilha:
- use só `Situação da Obra = Concluída` e `% Executado = 100`;
- prefira **construções novas**: deixe de fora (ou marque `usar = não`) reformas,
  "ETAPA I/II" (só parte do prédio) e infraestrutura (estacionamento, redes);
- `area_m2` = área da descrição; `fonte` = "UFV dados abertos, SIMEC ID 29722".
- O dicionário de dados não diz se a "Data de Término" é a **real** ou a prevista.
  Confira algumas obras no termo de recebimento (site da universidade, DOU) e
  anote em `observacoes`.

**2. Painel de Obras do SIMEC (FNDE/MEC)** — creches Proinfância e escolas dos
municípios: <https://simec.mec.gov.br/painelObras/>. Abre no seu navegador (tem
verificação anti-robô). Mostra situação, % executado, vigência do contrato e
vistorias. As creches seguem **projetos padrão** do FNDE com área conhecida
(Tipo 1: 1.317,99 m² construídos; Tipo 2: ~891,68 m² — confirme no edital da obra,
pois as fontes divergem).

**3. Diários oficiais municipais** — para achar as datas reais de uma obra
específica: busque o nome da obra com "ordem de serviço" e "termo de recebimento".
- Querido Diário (busca em diários de vários municípios): <https://queridodiario.org.br>
  (estava instável no teste; selecione um município antes de buscar).
- Diário Oficial da União, para obras federais: <https://www.in.gov.br>.

**4. Licitações** (planta + contrato): PNCP <https://pncp.gov.br> e portais das
prefeituras/universidades. O edital traz o projeto (a área está no carimbo ou no
memorial); as datas vêm dos diários oficiais.

**Não serve:** a API do Obrasgov.br tem quase só datas **previstas** (0 de 100 obras
concluídas de MG tinham data efetiva) e não informa a área.

Quantas obras: o treino roda a partir de **8**; com **20 a 30** o resultado já é
defensável; o RandomForest só entra na comparação a partir de **30**.

## 2. Importe o relatório de obras do SIMEC (universidades e institutos)

Baixe o CSV de obras (ex.: UFV → <https://dados.ufv.br/dataset/obras-e-reformas>,
recurso **Obras**) e rode, dentro de `Agepa/backend`:

```
py train_prazo.py importar-simec caminho/do/arquivo.csv --instituicao UFV --simular
py train_prazo.py importar-simec caminho/do/arquivo.csv --instituicao UFV
```

A primeira linha só mostra o que entraria; a segunda grava. O importador:
- pega só obras **Concluída** com **100% executado**, datas válidas e área na descrição;
- marca `usar = sim` só nas **construções novas**; reformas, serviços (impermeabilização,
  pintura, recapeamento), contratos de **projeto**, etapas, ampliações, obras mistas e
  infraestrutura entram com `usar = não` e o motivo em `observacoes`, para você revisar;
- nunca repete uma obra (usa o ID do SIMEC na coluna `fonte`).

Teste com o relatório real da UFV (12/11/2026): 178 obras → 84 importadas, **16 com
`usar = sim`**. Depois de importar, **revise**: obras que levaram muito mais que o normal
(ex.: 219 m² em 27 meses) provavelmente ficaram paradas — preencha `dias_paralisados`.

### Obras federais do Obrasgov.br (datas efetivas)

```
py train_prazo.py buscar-obrasgov --ufs ES,DF --simular
py train_prazo.py buscar-obrasgov --salvar ../dados/obrasgov_busca.json
py train_prazo.py buscar-obrasgov --de-arquivo ../dados/obrasgov_busca.json
```

Consulta a API pública do Obrasgov.br e guarda só obras **concluídas** com as
**datas efetivas** de início e fim (só ~1–4% das obras têm) e a área escrita no
nome ou na descrição. Mesmas regras do SIMEC: construção nova → `usar = sim`;
reforma, ampliação e infraestrutura → `usar = não`. A API é lenta (uma busca em
todos os estados leva bem mais de meia hora); use `--salvar` para não repetir.

### Cadastro Nacional de Obras — CNO (Receita Federal) — obras privadas do Brasil

Baixe o `cno.zip` (~340 MB) em <https://dados.gov.br/dados/conjuntos-dados/cadastro-nacional-de-obras-cno>
(recurso "Inscrições no CNO") e rode:

```
py train_prazo.py importar-cno caminho/cno.zip --simular
py train_prazo.py importar-cno caminho/cno.zip --amostra 3000
py train_prazo.py treinar --planilha ../dados/obras_cno.csv --saida models/prazo_model_cno.json --relatorio ../dados/relatorio_treino_prazo_cno.md --base "CNO · obras privadas" --aviso "No CNO o fim é a data de encerramento do cadastro na Receita Federal (administrativa), que pode vir depois do fim físico da obra."
```

O último comando grava o modelo de **comparação**: a tela mostra o prazo do CNO
abaixo do prazo das obras públicas, e a ressalva (`--aviso`) aparece nas notas.

O CNO registra toda obra de construção civil do país (3,7 milhões em 11/2026). Entram
só obras **encerradas**, **registradas no começo da obra** (registro até 30 dias antes ou
90 depois do início — descarta casas prontas registradas só para regularizar), **obra
nova de alvenaria**, prazo entre 3 e 60 meses. Teste de 13/11/2026: 364.248 obras
passam; a amostra vai para `dados/obras_cno.csv` (separada das obras públicas).
**Cuidado:** o fim é a data de **encerramento do cadastro** (administrativa) — pode vir
depois do fim físico da obra. Por isso o modelo do CNO é de comparação e não se mistura
às obras públicas. Treino de 13/11/2026 (3.000 obras, validação em 10 partes):
`prazo = 2,7435 · área^0,273`, erro médio 5,6 meses (RandomForest: 6,45) — relatório em
`relatorio_treino_prazo_cno.md`.

## 3. (Opcional) Deixe o AGEPA ler as plantas para você

Coloque os PDFs numa pasta e rode, dentro de `Agepa/backend`:

```
py train_prazo.py extrair ../plantas
```

Para cada PDF novo, uma linha é acrescentada à planilha com `arquivo_planta`,
`area_m2` e `qtd_comodos` lidos da planta (só PDF com texto, que é lido na hora).
**Confira a área** e complete as datas.

## 4. Treine

```
py train_prazo.py treinar
```

O comando:
1. confere a planilha e aponta linha por linha o que estiver errado;
2. compara os modelos por **validação cruzada** (o erro é medido em obras que o
   modelo não viu) e escolhe o de menor erro, preferindo o mais simples em empate;
3. salva o modelo em `backend/models/prazo_model.json` (o servidor passa a usá-lo
   sozinho, sem reiniciar);
4. gera `dados/relatorio_treino_prazo.md` com a comparação, a fórmula escolhida e
   as obras com maior erro — pronto para usar no texto do TCC.

Para voltar ao modelo demonstrativo, apague `backend/models/prazo_model.json`.
Para tirar a comparação do CNO da tela, apague `backend/models/prazo_model_cno.json`.
