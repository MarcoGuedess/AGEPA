# Plantas públicas para testar a leitura do AGEPA

Fontes conferidas em 13/11/2026. Todas são abertas: não pedem login nem pedido
pela Lei de Acesso à Informação. Os tamanhos vêm do próprio servidor.

**Primeiro lote baixado em 13/11/2026** (com autorização): 5 plantas de layout das UBS,
creches Tipo 1 e Tipo 2 do FNDE e 5 anexos do PNCP — estão em `../avaliacao/plantas/`
e o resultado da avaliação em `../avaliacao/LEIAME.md`.

A **área real** de cada planta (o gabarito) tem que vir do que está impresso na
prancha (quadro de áreas ou carimbo) ou na página oficial do projeto. Se não
estiver impressa, fica em branco: não se estima gabarito.

## 1. Ministério da Saúde: projetos de referência do Novo PAC Saúde

Página: <https://www.gov.br/saude/pt-br/assuntos/novo-pac-saude/projetos-arquitetonicos>.
Cada tipo tem uma pasta pública (SharePoint) com as pranchas em PDF separadas
(implantação, **planta de layout**, **planta técnica**, cortes, fachadas) e os
memoriais.

| Tipo | Plantas em PDF conferidas |
|---|---|
| UBS Porte 1 | `MS_UBS1_PE_AQ 02.12_R01 PLANTA DE LAYOUT.pdf` (3,4 MB), `03.12 PLANTA TÉCNICA` (3,5 MB) |
| UBS Porte 2 | `MS_UBS2_PE_AQ 02.12_R01 PLANTA DE LAYOUT.pdf` (3,8 MB), `03.12 PLANTA TÉCNICA` (3,9 MB) |
| UBS Porte 3 | `MS_UBS3_PE_AQ 02.13_R01 PLANTA DE LAYOUT.pdf` (4,5 MB), `03.13 PLANTA TÉCNICA` (4,6 MB) |
| UBS Porte 4 | `MS_UBS4_PE_AQ 02.22_R00 PLANTA DE LAYOUT GERAL.pdf` (5,3 MB), técnica geral (5,4 MB) |
| UBS Porte 5 | `MS_UBS5_PE_AQ 04.24_R00 PLANTA DE LAYOUT GERAL.pdf` (5,5 MB), técnica geral (5,6 MB) |
| CAPS, CER, Policlínica, Maternidade, Centro de Parto Normal, SAMU, Oficina ortopédica | pastas na mesma página, mesma organização (conteúdo ainda não listado) |

## 2. FNDE: projetos arquitetônicos padronizados (uso público e gratuito)

Página: <https://www.gov.br/fnde/pt-br/acesso-a-informacao/acoes-e-programas/acoes/obras-projetos-padronizados>.
São 23 projetos: creches (Tipo 1, Tipo 2, B, C, módulos), escolas (4, 5, 6, 9, 12 e 13 salas) e
quadras. O projeto arquitetônico vem em `.rar` com os PDFs dentro (o `tar` do Windows abre).

| Projeto | Arquivo (PDF) | Tamanho | Área declarada na página |
|---|---|---:|---|
| Creche Tipo 1, revisão 2024 | `Tipo1_REVISAO_03_2024_BIM/PDF/PDF_Projeto_Arquitetonico.rar` | 43,9 MB | — (ler na prancha) |
| Creche Tipo 2, R03 2025 | `Creche_Pre_Escola_Tipo_2/R03_2025/PDF/Projeto_Arquitetonico.rar` | 29,7 MB | — (ler na prancha) |
| Escola 13 salas | `13_salas_terreo/PDF-Projeto Arquitetnico.rar` | 61,9 MB | — (ler na prancha) |
| Quadra coberta / fechada / cobertura grande | páginas das quadras | — | 918,22 m² (área coberta) |
| Cobertura de quadra pequena | página da quadra | — | 777,51 m² |
| Quadra coberta com vestiário | página da quadra | — | 980,40 m² |
| Quadra coberta com palco | página da quadra | — | 994,08 m² |
| Quadra coberta e vestiário modelo 2 | página da quadra | — | 812,76 m² |

As revisões mudam a área: o gabarito deve citar a revisão (R02, R03...).

## 3. PNCP: plantas anexadas a licitações de obras

API pública (abre no navegador do app; pelo terminal a conexão é recusada):
- busca: `https://pncp.gov.br/api/search/?q="construção de creche"&tipos_documento=edital&pagina=1&tam_pagina=25`
- anexos: `https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos`

Amostra de 13/11/2026: 50 editais de cada busca, **48 de 200 (24%) com projeto anexado**.

| Busca | Editais (total) | Com projeto na amostra de 50 |
|---|---:|---:|
| "construção de creche" | 2.255 | 14 |
| "construção de escola" | 2.116 | 16 |
| "construção de unidade básica de saúde" | 2.120 | 10 |
| "construção de quadra" | 2.500 | 8 |

Exemplos com planta em PDF:
- Mozarlândia-GO, `01135227000107/2025/44`: `13. PLANTA BAIXA - CRECHE TIPO 1 FNDE` (5,3 MB)
- Porto-PI, `06554414000149/2026/7`: `PROJETO_COMPLETO_APROVADO_-CRECHE_TIPO_2` (1,1 MB)
- Pinhalão-PR, `76167717000194/2026/138`: `P10 ARQUITETONICO` (0,9 MB)
- Iporá-GO, `28983143000180/2026/40`: `ESCOLA MUNICIPAL DONA RITINHA - ARQUITETURA` (4,4 MB)
- Bujaru-PA, `05196563000110/2026/65`: `PLANTA.pdf` (20,4 MB)

Parte dos anexos vem em `.zip`/`.rar` ou é só ART/memorial: é preciso filtrar.
As datas do PNCP são as **do contrato** (previstas), não o início e o fim reais.

## 4. Fora do Brasil

- **CubiCasa5K** (<https://zenodo.org/records/2613548>): 5.000 plantas residenciais
  finlandesas com cômodos desenhados como polígonos (5,5 GB). Não traz área por escrito
  nem carimbo; serve para cômodos, não para o gabarito de área. Licença a conferir.

## Planta + datas reais da mesma obra

Continua sem uma fonte pronta. O caminho mais curto é o **projeto padrão do FNDE**:
a área é a do projeto (igual em todas as obras do mesmo tipo e revisão) e as datas de
cada creche construída estão no Painel de Obras do SIMEC
(<https://simec.mec.gov.br/painelObras/>). O painel abre no seu navegador, mas recusa
o navegador do app e o terminal (erro 403).
