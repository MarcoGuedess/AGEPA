# Avaliação da leitura de plantas — AGEPA

Gerado em 2026-11-14T04:23:13; modo: **arquivo original**.

## Resumo

- Plantas avaliadas: 22 (13 com área real conferida)
- Acertos (mesmo tipo de área, dentro da tolerância): **9**
- Erros: **1** — com confiança alta (alucinação): **0**
- Sem resposta (o sistema não inventou área): 0
- Outro tipo de área (útil ou envoltória, gabarito = construída): 3
- Sem gabarito (só registrado): 9; falhas: 0
- Erro absoluto médio nas comparáveis: 0,98%

## Planta por planta

| Arquivo | Real (m²) | Lido (m²) | Tipo lido | Método | Confiança | Erro | Resultado | Tempo |
|---|---:|---:|---|---|---|---:|---|---:|
| TIPO1-ARQ-01-IMP-GER0_R03.pdf | 1.324,27 | 1.324,27 | construida | carimbo | alta | 0,00% | acerto | 2,0 s |
| TIPO2-ARQ-01-IMP-GER0_R03.pdf | 786,07 | 786,07 | construida | carimbo | alta | 0,00% | acerto | 1,5 s |
| TIPO1-ARQ-02-PLB-GER0_R03.pdf | 1.324,27 | 1.045,25 | util | areas_declaradas | media | -21,07% | tipo diferente | 7,9 s |
| TIPO2-ARQ-02-PLB-GER0_R03.pdf | 786,07 | 617,59 | util | areas_declaradas | media | -21,43% | tipo diferente | 5,7 s |
| Mozarlandia-GO_creche_tipo1__13._PLANTA_BAIXA_-_CRECHE_TIPO_1_FNDE_-_ST._DOS_LAGOS_-_MOZARLANDIA.pdf | 1.324,27 | 1.041,38 | util | areas_declaradas | media | -21,36% | tipo diferente | 8,2 s |
| MS_UBS1_PE_AQ 02.12_R01 PLANTA DE LAYOUT.pdf | — | 607,00 | util | areas_declaradas | media | — | sem gabarito | 13,8 s |
| MS_UBS2_PE_AQ 02.12_R01 PLANTA DE LAYOUT.pdf | — | 464,23 | util | areas_declaradas | media | — | sem gabarito | 12,0 s |
| MS_UBS3_PE_AQ 02.13_R01 PLANTA DE LAYOUT.pdf | — | 669,68 | util | areas_declaradas | alta | — | sem gabarito | 27,7 s |
| MS_UBS4_PE_AQ 02.22_R00 PLANTA DE LAYOUT GERAL.pdf | — | 891,01 | util | areas_declaradas | media | — | sem gabarito | 41,6 s |
| MS_UBS5_PE_AQ 04.24_R00 PLANTA DE LAYOUT GERAL.pdf | — | 1.132,46 | util | areas_declaradas | media | — | sem gabarito | 54,4 s |
| ANEXO IV_planta baixa Cantina CAP.pdf | 402,43 | 402,43 | construida | carimbo | alta | 0,00% | acerto | 0,3 s |
| ANEXO IV_planta baixa Cantina CAP.png | 402,43 | 402,43 | construida | carimbo | alta | 0,00% | acerto | 314,9 s |
| PARQ_SAP_AP_01_-Planta-Baixa.pdf | 954,71 | 954,71 | util | areas_declaradas | alta | 0,00% | acerto | 7,1 s |
| PARQ_SAP_AP_01_-Planta-Baixa.png | 954,71 | 954,68 | util | areas_declaradas | media | -0,00% | acerto | 742,7 s |
| 10-15 PLANTA BAIXA PAV TEC ARQ24.pdf | 6.962,41 | 6.962,41 | construida | carimbo | alta | 0,00% | acerto | 1.045,5 s |
| 03 PLANTA BAIXA.pdf | 4.042,32 | 4.042,32 | construida | carimbo | alta | 0,00% | acerto | 3,8 s |
| 03 PLANTA BAIXA.png | 4.042,32 | 4.042,32 | construida | carimbo | media | 0,00% | acerto | 662,8 s |
| Residencial_Sao_Cristovao_plantas_A5.pdf | 413,41 | 372,75 | util | areas_declaradas | media | -9,84% | erro | 281,8 s |
| Residencial_Sao_Cristovao_plantas_A5.pdf | — | — | — | indisponivel | baixa | — | sem gabarito | 338,5 s |
| Residencial_Sao_Cristovao_plantas_A5.pdf | — | — | — | indisponivel | baixa | — | sem gabarito | 442,6 s |
| Residencial_Sao_Cristovao_plantas_A5.pdf | — | — | — | indisponivel | baixa | — | sem gabarito | 176,9 s |
| Residencial_Sao_Cristovao_plantas_A5.pdf | — | — | — | indisponivel | baixa | — | sem gabarito | 287,7 s |

## Como ler

- **Real**: área do gabarito, lida à mão na prancha (fonte em `gabarito.csv`).
- **Tolerância**: 0,5% quando o total está impresso na folha (o sistema deve lê-lo); 10% quando precisa medir.
- **Alucinação**: área errada com confiança alta — o caso que o sistema não pode ter.
