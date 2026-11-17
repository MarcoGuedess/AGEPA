# Treino do modelo de prazo — AGEPA

Gerado em 2026-11-15T19:43:25 a partir de `obras_reais.csv`.

## Dados

- Obras usadas: **18** (excluídas pela coluna `usar`: 72; com erro: 0)
- Área: 59,23 a 3.789,63 m² (mediana 345,59 m²)
- Prazo real: 3,9 a 34,8 meses (mediana 9,6)

| Uso | Obras | Produtividade mediana (m²/mês) |
|---|---:|---:|
| educacao | 14 | 46,7 |
| galpao | 2 | 32,4 |
| saude | 2 | 33,3 |

## Comparação dos modelos (validação cruzada leave-one-out)

Erro medido em obras que o modelo não viu no ajuste.

| Modelo | Fórmula | MAE (meses) | MAPE | RMSE (meses) | R² |
|---|---|---:|---:|---:|---:|
| Produtividade mediana | prazo = área ÷ produtividade (m²/mês) | 12,76 | 101,9% | 23,63 | -7,390 |
| Potência (log-linear) **(escolhido)** | ln(prazo) = a + b·ln(área)  →  prazo = e^a · área^b | 5,70 | 43,0% | 8,40 | -0,061 |
| Potência robusta (Huber) | ln(prazo) = a + b·ln(área), ajuste robusto (Huber)  →  prazo = e^a · área^b | 5,57 | 39,4% | 8,46 | -0,075 |
| Potência com cômodos | ln(prazo) = a + b·ln(área) + c·ln(cômodos) | — | — | — | — (planilha sem cômodos em todas as obras) |
| RandomForest | RandomForest (média de 300 árvores de decisão) | — | — | — | — (precisa de 30+ obras) |

## Modelo escolhido

**Potência (log-linear)**: `prazo = 1.6743 · área^0.307`

- Erro médio esperado: **5,70 meses** (43,0% do prazo).
- Intervalo de 80%: de 0,48× a 2,07× o valor previsto.
- Válido para áreas entre 59,23 e 3.789,63 m²; fora disso o sistema avisa.
- Critério: menor MAE; com diferença de até 5%, vence o modelo mais simples.

## Obras com maior erro (vale revisar os dados)

| Linha | Obra | Área (m²) | Prazo real | Previsto (validação) |
|---:|---|---:|---:|---:|
| 45 | UNIDADE DE ATENDIMENTO ESPECIALIZADO EM SAÚDE (Viçosa/MG) | 1.281,89 | 34,8 | 13,5 |
| 31 | CONSTRUÇÃO DO ALMOXARIFADO DE PRODUTOS QUÍMICOS (Viçosa/MG) | 219,50 | 27,6 | 8,0 |
| 78 | BIBLIOTECA CENTRAL - RIO PARANAÍBA (Rio Paranaíba/MG) | 3.789,63 | 13,9 | 25,1 |
| 93 | Construção de Centro Municipal de Educação Infantil - CMEI SEDE (CRECHE) (ES) | 1.098,29 | 24,2 | 13,5 |
| 50 | LABORATÓRIO DE MUSCULAÇÃO (Viçosa/MG) | 135,28 | 15,2 | 6,9 |
