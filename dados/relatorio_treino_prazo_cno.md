# Treino do modelo de prazo — AGEPA

Gerado em 2026-11-15T21:46:29 a partir de `obras_cno.csv`.

## Dados

- Obras usadas: **3000** (excluídas pela coluna `usar`: 0; com erro: 0)
- Área: 24,40 a 59.999,20 m² (mediana 96,97 m²)
- Prazo real: 3,0 a 60,0 meses (mediana 10,1)

| Uso | Obras | Produtividade mediana (m²/mês) |
|---|---:|---:|
| casa popular | 545 | 6,5 |
| comercial | 176 | 33,8 |
| conjunto habitacional | 39 | 42,2 |
| galpao | 94 | 49,5 |
| garagem | 4 | 92,2 |
| residencial multifamiliar | 370 | 29,6 |
| residencial unifamiliar | 1772 | 11,0 |

## Comparação dos modelos (validação cruzada 10 partes (k-fold))

Erro medido em obras que o modelo não viu no ajuste.

| Modelo | Fórmula | MAE (meses) | MAPE | RMSE (meses) | R² |
|---|---|---:|---:|---:|---:|
| Produtividade mediana | prazo = área ÷ produtividade (m²/mês) | 38,45 | 206,6% | 211,06 | -465,324 |
| Potência (log-linear) **(escolhido)** | ln(prazo) = a + b·ln(área)  →  prazo = e^a · área^b | 5,60 | 48,9% | 8,72 | 0,205 |
| Potência robusta (Huber) | ln(prazo) = a + b·ln(área), ajuste robusto (Huber)  →  prazo = e^a · área^b | 5,60 | 48,7% | 8,74 | 0,200 |
| Potência com cômodos | ln(prazo) = a + b·ln(área) + c·ln(cômodos) | — | — | — | — (planilha sem cômodos em todas as obras) |
| RandomForest | RandomForest (média de 300 árvores de decisão) | 6,45 | 63,5% | 9,40 | 0,076 |

## Modelo escolhido

**Potência (log-linear)**: `prazo = 2.7435 · área^0.273`

- Erro médio esperado: **5,60 meses** (48,9% do prazo).
- Intervalo de 80%: de 0,49× a 2,05× o valor previsto.
- Válido para áreas entre 24,40 e 59.999,20 m²; fora disso o sistema avisa.
- Critério: menor MAE; com diferença de até 5%, vence o modelo mais simples.

## Obras com maior erro (vale revisar os dados)

| Linha | Obra | Área (m²) | Prazo real | Previsto (validação) |
|---:|---|---:|---:|---:|
| 2812 | GLEDISON LUIZ TUCHTENHAGEN DE LIMA (PELOTAS/RS) | 45,00 | 60,0 | 7,8 |
| 562 | EDUARDO MACEDO ROMAN (NOVO HAMBURGO/RS) | 48,82 | 56,0 | 7,9 |
| 1741 | VEREDAS EMPREENDIMENTOS E SERVICOS DE ENGENHARIA LTDA- OBRA RES LT 04A-03 QD- S (ARCOVERDE/PE) | 79,50 | 56,6 | 9,1 |
| 1300 | CONSTRUCAO RES UNIFAMILIAR (SÃO MANUEL/SP) | 234,01 | 59,6 | 12,3 |
| 36 | SERGIO ADEMIR PARCIANELLO (GIRUÁ/RS) | 200,03 | 58,1 | 11,7 |

## Problemas na planilha

- Linha 236 (L M CONSTRUCOES LTDA (BELO JARDIM/PE)): mesmo nome da linha 234 (obra repetida?).
- Linha 281 (CHIOSSI & BIELUCZYK LTDA (XANXERÊ/SC)): mesmo nome da linha 255 (obra repetida?).
- Linha 430 (PARCERIA GLOBAL INCORPORACOES LTDA (LUZIÂNIA/GO)): mesmo nome da linha 285 (obra repetida?).
- Linha 517 (CASAFORT CONSTRUCOES LTDA (CAMPO GRANDE/MS)): mesmo nome da linha 441 (obra repetida?).
- Linha 562 (EDUARDO MACEDO ROMAN (NOVO HAMBURGO/RS)): 1 m²/mês é incomum; confira área e datas.
- Linha 661 (INCORPORADORA E IMOBILIARIA FREIRES LTDA (BOA VISTA/RR)): mesmo nome da linha 653 (obra repetida?).
- Linha 781 (PLARENO EMPREENDIMENTOS IMOBILIARIOS LTDA (IBAITI/PR)): mesmo nome da linha 368 (obra repetida?).
- Linha 870 (C AQUINO COSTA ADMINSTRADORA DE BENS LTDA (SARANDI/PR)): mesmo nome da linha 679 (obra repetida?).
- Linha 891 (IMOBILIARIA DO LITORAL CONSTRUTORA INCORPORADORA LTDA (MONGAGUÁ/SP)): mesmo nome da linha 670 (obra repetida?).
- Linha 911 (L M CONSTRUCOES LTDA (BELO JARDIM/PE)): mesmo nome da linha 236 (obra repetida?).
- Linha 946 (JOAO PAULO SENCI (JANDAIA DO SUL/PR)): mesmo nome da linha 831 (obra repetida?).
- Linha 959 (WW PETROCHELI CONSTRUTORA E INCORPORADORA LTDA (CALDAS NOVAS/GO)): mesmo nome da linha 753 (obra repetida?).
- Linha 1007 (BSB CONSTRUCOES E INCORPORACOES LTDA (LUÍS EDUARDO MAGALHÃES/BA)): mesmo nome da linha 907 (obra repetida?).
- Linha 1016 (RESIDENCIAL HOLLYWOOD I (VÁRZEA GRANDE/MT)): mesmo nome da linha 763 (obra repetida?).
- Linha 1043 (BEM LAR CONSTRUCOES LTDA (MATEUS LEME/MG)): mesmo nome da linha 338 (obra repetida?).
- Linha 1082 (BDD EMPREENDIMENTOS IMOBILIARIOS LTDA (SARANDI/PR)): mesmo nome da linha 561 (obra repetida?).
- Linha 1317 (CONSTRUTORA ELOS LTDA (ITAITINGA/CE)): mesmo nome da linha 867 (obra repetida?).
- Linha 1329 (ILAMAR PEREIRA DE OLIVEIRA (IGUATU/CE)): mesmo nome da linha 966 (obra repetida?).
- Linha 1411 (C AQUINO COSTA ADMINSTRADORA DE BENS LTDA (SARANDI/PR)): mesmo nome da linha 870 (obra repetida?).
- Linha 1522 (ANNE CAROLYNNE FRANCISCA DOS SANTOS GRAIA (SALTO/SP)): 1 m²/mês é incomum; confira área e datas.
- Linha 1684 (RESIDENCIAL NOVO TEMPO (CUIABÁ/MT)): mesmo nome da linha 1526 (obra repetida?).
- Linha 1971 (C AQUINO COSTA ADMINSTRADORA DE BENS LTDA (SARANDI/PR)): mesmo nome da linha 1411 (obra repetida?).
- Linha 1972 (CONSTRUTORA SEMPRE VIVA LTDA (GURUPI/TO)): mesmo nome da linha 1919 (obra repetida?).
- Linha 1981 (ELIFELETE FRANCISCO GOMES (OURINHOS/SP)): 1 m²/mês é incomum; confira área e datas.
- Linha 2056 (POLO CONSTRUCOES LTDA (PARNAMIRIM/RN)): mesmo nome da linha 967 (obra repetida?).
- Linha 2105 (L M CONSTRUCOES LTDA (BELO JARDIM/PE)): mesmo nome da linha 911 (obra repetida?).
- Linha 2124 (RESIDENCIAL JOAO E JOANA II SPE LTDA (BREJO SANTO/CE)): mesmo nome da linha 2083 (obra repetida?).
- Linha 2208 (ANDRE LUIZ MULLER & CIA LTDA (SINOP/MT)): mesmo nome da linha 1255 (obra repetida?).
- Linha 2210 (CONSTRUTORA QUATRO RODAS LTDA (IMPERATRIZ/MA)): mesmo nome da linha 2194 (obra repetida?).
- Linha 2242 (CONSTRUTORA RAMDY RAYDAN EIRELI (NANUQUE/MG)): mesmo nome da linha 2156 (obra repetida?).
- Linha 2248 (GRAN VILLE (VÁRZEA GRANDE/MT)): mesmo nome da linha 2062 (obra repetida?).
- Linha 2463 (S C COSTA SERVICOS E CONSTRUCOES LTDA (SÃO BENTO DO UNA/PE)): mesmo nome da linha 2415 (obra repetida?).
- Linha 2486 (RETANGULO CONSTRUCOES E EMPREENDIMENTOS LTDA (EXTREMOZ/RN)): mesmo nome da linha 2429 (obra repetida?).
- Linha 2543 (TC CONSTRUTORA E INCORPORADORA LTDA (VÁRZEA GRANDE/MT)): mesmo nome da linha 1478 (obra repetida?).
- Linha 2559 (K. G. D. OLIVEIRA FRISON LTDA (VÁRZEA GRANDE/MT)): mesmo nome da linha 2371 (obra repetida?).
- Linha 2575 (RETANGULO CONSTRUCOES E EMPREENDIMENTOS LTDA (EXTREMOZ/RN)): mesmo nome da linha 2486 (obra repetida?).
- Linha 2580 (VEZUVIO CONSTRUTORA E INCORPORADORA EIRELI (PALMEIRAS DE GOIÁS/GO)): mesmo nome da linha 2339 (obra repetida?).
- Linha 2613 (LOTEAMENTO JARDIM ARVOREDO (SÃO GONÇALO DO AMARANTE/RN)): mesmo nome da linha 2595 (obra repetida?).
- Linha 2628 (P. JOSE REGIS - CONSTRUCOES (VÁRZEA GRANDE/MT)): mesmo nome da linha 2375 (obra repetida?).
- Linha 2634 (J E CONSTRUCOES LTDA (VÁRZEA GRANDE/MT)): mesmo nome da linha 2476 (obra repetida?).
- Linha 2638 (P. JOSE REGIS - CONSTRUCOES (VÁRZEA GRANDE/MT)): mesmo nome da linha 2628 (obra repetida?).
- Linha 2664 (THEO CONSTRUCOES LTDA (VÁRZEA GRANDE/MT)): mesmo nome da linha 2190 (obra repetida?).
- Linha 2694 (RESIDENCIAL CONCEICAO PARK SPE LTDA (FEIRA DE SANTANA/BA)): mesmo nome da linha 2684 (obra repetida?).
- Linha 2746 (J E CONSTRUCOES LTDA (VÁRZEA GRANDE/MT)): mesmo nome da linha 2634 (obra repetida?).
- Linha 2747 (VEZUVIO CONSTRUTORA E INCORPORADORA LTDA (PALMEIRAS DE GOIÁS/GO)): mesmo nome da linha 2609 (obra repetida?).
- Linha 2778 (VHR ENGENHARIA LTDA (TRINDADE/GO)): mesmo nome da linha 2471 (obra repetida?).
- Linha 2786 (J E CONSTRUCOES LTDA (VÁRZEA GRANDE/MT)): mesmo nome da linha 2746 (obra repetida?).
- Linha 2812 (GLEDISON LUIZ TUCHTENHAGEN DE LIMA (PELOTAS/RS)): 1 m²/mês é incomum; confira área e datas.
- Linha 2826 (SATELITE CONSTRUCOES LTDA (VÁRZEA GRANDE/MT)): mesmo nome da linha 1860 (obra repetida?).
- Linha 2831 (LOTEAMENTO JARDIM ARVOREDO (SÃO GONÇALO DO AMARANTE/RN)): mesmo nome da linha 2613 (obra repetida?).
- Linha 2941 (WALTER YUKIO HORITA (CORRENTINA/BA)): mesmo nome da linha 2915 (obra repetida?).
