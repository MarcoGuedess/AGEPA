# ============================================================
# AGEPA — Treino do modelo de prazo com OBRAS REAIS
# train_prazo.py
# ============================================================
#
# Uso (na pasta backend):
#   py train_prazo.py importar-simec obras.csv --instituicao UFV   # dados abertos do SIMEC
#   py train_prazo.py buscar-obrasgov --ufs ES,DF --simular        # obras federais (datas efetivas)
#   py train_prazo.py extrair ../plantas     # lê os PDFs e preenche área/cômodos
#   py train_prazo.py treinar                # valida, compara modelos e salva o melhor
#   py train_prazo.py importar-cno cno.zip   # amostra de obras privadas (Receita Federal)
#   py train_prazo.py treinar --planilha ../dados/obras_cno.csv --saida models/prazo_model_cno.json \n#       --relatorio ../dados/relatorio_treino_prazo_cno.md --base "CNO · obras privadas"
#                                            # modelo de COMPARAÇÃO, mostrado ao lado do principal
#
# Planilha padrão: ../dados/obras_reais.csv  (guia: ../dados/LEIAME.md)
# Saídas: models/prazo_model.json (usado pelo servidor sem reiniciar)
#         models/prazo_model_cno.json (opcional: comparação, também sem reiniciar)
#         ../dados/relatorio_treino_prazo.md (comparação para o TCC)
#
# Substitui o kaggle_training_script.py, que treinava com dados simulados.
# ============================================================

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from services.plan_reader import parse_number  # noqa: E402
from services.schedule_models import TrainingResult, interval, train, train_por_uso  # noqa: E402

DEFAULT_SHEET = BACKEND_DIR.parent / "dados" / "obras_reais.csv"
DEFAULT_MODEL = BACKEND_DIR / "models" / "prazo_model.json"
CNO_MODEL = BACKEND_DIR / "models" / "prazo_model_cno.json"   # comparação (obras privadas)
DEFAULT_REPORT = BACKEND_DIR.parent / "dados" / "relatorio_treino_prazo.md"

COLUMNS: Tuple[str, ...] = (
    "obra", "arquivo_planta", "area_m2", "qtd_comodos", "data_inicio", "data_termino",
    "dias_paralisados", "uso", "pavimentos", "uf", "fonte", "usar", "observacoes",
)
REQUIRED: Tuple[str, ...] = ("obra", "area_m2", "data_inicio", "data_termino")
ALIASES: Dict[str, str] = {
    "area": "area_m2", "area_construida": "area_m2", "area_construida_m2": "area_m2",
    "comodos": "qtd_comodos", "ambientes": "qtd_comodos",
    "inicio": "data_inicio", "data_de_inicio": "data_inicio",
    "termino": "data_termino", "fim": "data_termino", "data_fim": "data_termino",
    "data_de_termino": "data_termino", "conclusao": "data_termino",
    "paralisacao": "dias_paralisados", "dias_parados": "dias_paralisados",
    "planta": "arquivo_planta", "arquivo": "arquivo_planta",
}
DATE_FORMATS: Tuple[str, ...] = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y")
DAYS_PER_MONTH: float = 365.25 / 12


# ══════════════════════════════════════════════════════════════
# Leitura da planilha
# ══════════════════════════════════════════════════════════════

def _norm_header(name: str) -> str:
    name = unicodedata.normalize("NFKD", name.strip().lower())
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    name = name.replace("m2_", "m2").replace("_m_2", "_m2")
    return ALIASES.get(name, name)


def _read_text(path: Path) -> Tuple[str, str]:
    """Excel salva CSV em UTF-8 (com BOM) ou em Windows-1252."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def read_table(path: Path) -> Tuple[List[str], List[Tuple[int, Dict[str, str]]], str, str]:
    """(colunas normalizadas, [(nº da linha, valores)], separador, codificação)."""
    text, encoding = _read_text(path)
    lines = text.splitlines()
    header_line = next((l for l in lines if l.strip() and not l.lstrip().startswith("#")), "")
    delimiter = max(";,\t", key=header_line.count) if header_line else ";"
    reader = csv.reader(lines, delimiter=delimiter)
    header: List[str] = []
    rows: List[Tuple[int, Dict[str, str]]] = []
    for number, cells in enumerate(reader, start=1):
        if not cells or not "".join(cells).strip() or cells[0].lstrip().startswith("#"):
            continue
        if not header:
            header = [_norm_header(c) for c in cells]
            continue
        rows.append((number, {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header)}))
    return header, rows, delimiter, encoding


def parse_date(text: str) -> Optional[dt.date]:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class Obra:
    linha: int
    obra: str
    area_m2: float
    prazo_meses: float
    comodos: Optional[int]
    uso: str
    inicio: dt.date
    termino: dt.date


def load_obras(path: Path) -> Tuple[List[Obra], List[str], List[str], int]:
    """Lê e confere a planilha. Devolve (obras válidas, erros, avisos, nº de excluídas)."""
    header, rows, _, _ = read_table(path)
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ValueError(
            f"A planilha não tem a(s) coluna(s) {', '.join(missing)}. "
            f"Colunas esperadas: {'; '.join(COLUMNS)}"
        )
    obras: List[Obra] = []
    errors: List[str] = []
    warnings: List[str] = []
    excluded = 0
    seen: Dict[str, int] = {}

    for line, row in rows:
        name = row.get("obra", "") or f"(linha {line})"
        where = f"Linha {line} ({name})"
        if _norm_header(row.get("usar", "")) in ("nao", "n", "no", "0", "false"):
            excluded += 1
            continue

        problems: List[str] = []
        area = parse_number(row.get("area_m2", "")) if row.get("area_m2") else None
        if area is None:
            problems.append("área vazia ou inválida")
        elif not 5 <= area <= 1_000_000:
            problems.append(f"área fora do plausível ({row['area_m2']} m²)")
        inicio, termino = parse_date(row.get("data_inicio", "")), parse_date(row.get("data_termino", ""))
        if inicio is None:
            problems.append(f"data de início inválida ('{row.get('data_inicio', '')}'; use dd/mm/aaaa)")
        if termino is None:
            problems.append(f"data de término inválida ('{row.get('data_termino', '')}'; use dd/mm/aaaa)")
        paralisados = parse_number(row.get("dias_paralisados", "") or "0")
        if paralisados is None or paralisados < 0:
            problems.append(f"dias paralisados inválido ('{row.get('dias_paralisados')}')")
            paralisados = 0.0

        prazo = None
        if inicio and termino:
            dias = (termino - inicio).days - paralisados
            if termino <= inicio:
                problems.append("término não é posterior ao início")
            elif dias <= 0:
                problems.append("dias paralisados ≥ duração total")
            else:
                prazo = dias / DAYS_PER_MONTH
                if prazo < 0.5 or prazo > 240:
                    problems.append(f"prazo de {prazo:.1f} meses fora do plausível (0,5 a 240)")

        comodos: Optional[int] = None
        if row.get("qtd_comodos"):
            value = parse_number(row["qtd_comodos"])
            if value is None or value < 1 or value != int(value):
                warnings.append(f"{where}: cômodos '{row['qtd_comodos']}' ignorado (use um número inteiro).")
            else:
                comodos = int(value)

        if problems:
            errors.append(f"{where}: " + "; ".join(problems) + ".")
            continue

        produtividade = area / prazo
        if produtividade > 3000 or produtividade < 1:
            warnings.append(
                f"{where}: {produtividade:.0f} m²/mês é incomum; confira área e datas."
            )
        key = name.strip().lower()
        if key in seen:
            warnings.append(f"{where}: mesmo nome da linha {seen[key]} (obra repetida?).")
        seen[key] = line
        obras.append(Obra(line, name, area, prazo, comodos, row.get("uso", "").strip().lower(), inicio, termino))
    return obras, errors, warnings, excluded


# ══════════════════════════════════════════════════════════════
# extrair: lê os PDFs e acrescenta linhas à planilha
# ══════════════════════════════════════════════════════════════

def _fmt_decimal(value: float, delimiter: str) -> str:
    text = f"{value:.2f}"
    return text.replace(".", ",") if delimiter == ";" else text


def _open_sheet(sheet: Path) -> Tuple[List[str], List[Tuple[int, Dict[str, str]]], str, str]:
    """Abre a planilha de obras (cria só com o cabeçalho, se não existir)."""
    if not sheet.exists():
        sheet.parent.mkdir(parents=True, exist_ok=True)
        sheet.write_text(";".join(COLUMNS) + "\r\n", encoding="utf-8-sig")
    return read_table(sheet)


def _append_rows(sheet: Path, header: List[str], delimiter: str, encoding: str,
                 rows: List[Dict[str, str]]) -> None:
    """Acrescenta linhas no FIM da planilha, sem reescrever o que o usuário editou
    (mantém separador e codificação do arquivo)."""
    if not rows:
        return
    text = sheet.read_text(encoding=encoding)
    with sheet.open("a", encoding=encoding.replace("-sig", ""), newline="") as fh:
        if text and not text.endswith(("\n", "\r")):
            fh.write("\r\n")
        csv.writer(fh, delimiter=delimiter, lineterminator="\r\n").writerows(
            [[row.get(col, "") for col in header] for row in rows]
        )


def cmd_extrair(paths: Sequence[str], sheet: Path) -> int:
    from services.pdf_reader import is_pdf, read_pdf
    from services.plan_reader import interpret

    files: List[Path] = []
    for p in map(Path, paths):
        if p.is_dir():   # sem glob("*.pdf") + glob("*.PDF"): no Windows duplicaria
            files += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")
        else:
            files.append(p)
    if not files:
        print("Nenhum PDF encontrado.")
        return 1

    header, rows, delimiter, encoding = _open_sheet(sheet)
    known = {r.get("arquivo_planta", "").lower() for _, r in rows}

    new_rows: List[Dict[str, str]] = []
    for file in files:
        if file.name.lower() in known:
            print(f"  = {file.name}: já está na planilha")
            continue
        data = file.read_bytes()
        if not is_pdf(data):
            print(f"  ! {file.name}: não é PDF (imagens: use a interface do AGEPA)")
            continue
        page = read_pdf(data)
        if not page.has_text:
            print(f"  ! {file.name}: PDF escaneado, sem texto (use a interface do AGEPA)")
            continue
        reading = interpret(page.items, np.asarray(page.image.convert("L")))
        values = {
            "obra": file.stem, "arquivo_planta": file.name,
            "area_m2": _fmt_decimal(reading.area_m2, delimiter) if reading.area_m2 else "",
            "qtd_comodos": str(reading.qtd_comodos) if reading.qtd_comodos else "",
            "observacoes": (
                f"área lida pelo AGEPA ({reading.method}, confiança {reading.confidence}) — confira"
                if reading.area_m2 else "área não encontrada na planta — preencha"
            ),
        }
        if reading.area_kind == "util":
            values["observacoes"] += " — é área ÚTIL (soma dos ambientes), não construída"
        new_rows.append(values)
        shown = f"{reading.area_m2:.2f} m² ({reading.method}, {reading.confidence})" if reading.area_m2 else "área não encontrada"
        print(f"  + {file.name}: {shown}, {reading.qtd_comodos} cômodos")

    _append_rows(sheet, header, delimiter, encoding, new_rows)
    print(f"\n{len(new_rows)} obra(s) acrescentada(s) em {sheet}.")
    print("Confira as áreas e preencha data_inicio e data_termino antes de treinar.")
    return 0


# ══════════════════════════════════════════════════════════════
# importar-simec: relatório de obras do SIMEC → planilha
# ══════════════════════════════════════════════════════════════
#
# Universidades e institutos federais publicam em dados abertos o
# "relatório de obras" do SIMEC (ex.: dados.ufv.br, conjunto "Obras e
# Reformas"): ID; Nome da Obra; Município/UF; Data de Início; Data de
# Término; Situação da Obra; % Executado; Composição (descrição, com a
# área) ... Aqui só entram obras CONCLUÍDAS com 100% executado e área
# na descrição. Construções novas entram com usar = sim; reformas,
# etapas, ampliações e infraestrutura entram com usar = não e o motivo
# em observacoes, para revisão manual.

# Área na descrição: "área total de 8.153,58 m²", "599,92 m2", "320 m²".
AREA_TEXT_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)\s*M\s*(?:2|²)(?!\d)")
# Palavras procuradas no NOME da obra (normalizado: maiúsculo, sem acento).
# Ajustadas com o relatório real da UFV (11/2026).
SIMEC_KINDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # "LABORATÓRIOS DE ENGENHARIA - PROJETO": contrato do projeto, não da obra.
    ("projeto", (" PROJETO", "PROJETOS ", "ELABORACAO DE")),
    ("reforma", ("REFORMA", "REF.", "RESTAURA", "ADEQUACAO", "ADEQUACOES", "RECUPERACAO",
                 "REESTRUTURA", "REVITALIZA", "MANUTENCAO", "REPAROS", "SUBSTITUICAO", "MODERNIZA")),
    ("servico", ("IMPERMEABILIZA", "PINTURA", "RECAPEAMENTO", "TELHADO", "COBERTURA DO",
                 "COBERTURA DA", "INSTALACAO DE", "INSTALACOES ELETRICAS", "CLIMATIZA")),
    ("etapa", ("ETAPA", "FASE", "CONCLUSAO D", "CONTINUIDADE", "REMANESCENTE", "COMPLEMENTA")),
    ("infraestrutura", ("ESTACIONAMENTO", "PAVIMENTACAO", "ACESSO", "VIAS ", "REDE DE", "REDES DE",
                        "DRENAGEM", "ESGOT", "ABASTECIMENTO", "TRATAMENTO DE AGUA", "TRAT. AGUA",
                        "SUBESTACAO", "CERCAMENTO", "MURO", "URBANIZACAO", "ILUMINACAO",
                        "CALCADA", "RESERVATORIO", "CISTERNA", "PAISAGISMO", "SISTEMA DE")),
    ("ampliacao", ("AMPLIACAO", "AMPLIACOES", "ACRESCIMO")),
)
# Início da DESCRIÇÃO que denuncia reforma/etapa quando o nome não diz
# ("QUADRAS DE TÊNIS" → "Reforma e adaptação das quadras...").
SIMEC_DESC_STARTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("reforma", ("REFORMA", "REF.", "ADAPTACAO", "ADEQUACAO", "RECUPERACAO", "RESTAURACAO",
                 "MANUTENCAO", "SUBSTITUICAO")),
    ("etapa", ("CONCLUSAO", "CONTINUIDADE", "COMPLEMENTACAO")),
)
SIMEC_USES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("galpao", ("GALPAO", "GALPOES", "BARRACAO", "ARMAZEM", "SILO", "ESTUFA", "CURRAL", "ABRIGO",
                "SUINOCULTURA", "AVICULTURA", "CAPRINOCULTURA", "BOVINOCULTURA", "AVIARIO")),
    ("residencial", ("MORADIA", "ALOJAMENTO", "RESIDENCIA ESTUDANTIL", "APARTAMENTO")),
    ("saude", ("HOSPITAL", "CLINICA", "AMBULATORIO", "SAUDE", "ODONTO")),
    ("alimentacao", ("RESTAURANTE", "CANTINA", "REFEITORIO", "LANCHONETE")),
)
NUMBER_WORDS = {"UM": 1, "UMA": 1, "DOIS": 2, "DUAS": 2, "TRES": 3, "QUATRO": 4,
                "CINCO": 5, "SEIS": 6, "SETE": 7, "OITO": 8}


def _upper_plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).upper()


def pick_area(name: str, description: str) -> Tuple[Optional[float], str]:
    """Área da obra a partir do nome/descrição.

    Com várias áreas, prefere a precedida de "total"/"construída"; senão a
    maior (costuma ser o total). Devolve (área, nota para conferência).
    """
    found: List[Tuple[float, bool]] = []
    for text in (description, name):
        plain = _upper_plain(text)
        for m in AREA_TEXT_RE.finditer(plain):
            value = parse_number(m.group(1))
            if value is None or not 5 <= value <= 500_000:
                continue
            before = plain[max(0, m.start() - 45):m.start()]
            found.append((value, "TOTAL" in before or "CONSTRU" in before))
    if not found:
        return None, ""
    preferred = [v for v, p in found if p]
    value = max(preferred) if preferred else max(v for v, _ in found)
    distinct = sorted({v for v, _ in found})
    note = ""
    if len(distinct) > 1:
        listed = ", ".join(_fmt_decimal(v, ";") for v in distinct)
        note = f"várias áreas na descrição ({listed} m²): usada {_fmt_decimal(value, ';')} — confira"
    return value, note


def classify_simec(name: str, description: str = "") -> Tuple[str, str]:
    """(tipo, uso) pelo nome da obra e pelo começo da descrição."""
    plain = f" {_upper_plain(name)} "
    kind = next((k for k, words in SIMEC_KINDS if any(w in plain for w in words)), "construcao")
    desc = _upper_plain(description).strip()
    if kind == "construcao" and desc:
        kind = next((k for k, starts in SIMEC_DESC_STARTS if desc.startswith(starts)), kind)
    if kind == "construcao" and "CONSTRU" in desc and "REFORM" in desc:
        kind = "misto"        # "Construção de 2 salas. Reforma do laboratório..."
    use = next((u for u, words in SIMEC_USES if any(w in plain for w in words)), "")
    return kind, use


def _pavimentos(description: str) -> str:
    m = re.search(r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\s+PAVIMENTOS?\b", _upper_plain(description))
    if not m:
        return ""
    token = m.group(1)
    return token if token.isdigit() else str(NUMBER_WORDS[token])


def read_simec(path: Path) -> List[Dict[str, str]]:
    """Linhas de obra do relatório do SIMEC (ignora as linhas "( N dia(s) )")."""
    text, _ = _read_text(path)
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    delimiter = max(";,\t", key=lines[0].count)
    reader = csv.reader(lines, delimiter=delimiter)
    header = [_norm_header(h) for h in next(reader)]
    comp_idx = header.index("composicao") if "composicao" in header else None
    obras: List[Dict[str, str]] = []
    for cells in reader:
        if not cells or not cells[0].strip().isdigit():
            continue
        extra = len(cells) - len(header)
        if extra > 0 and comp_idx is not None:   # ";" dentro da descrição
            cells = cells[:comp_idx] + [";".join(cells[comp_idx:comp_idx + extra + 1])] + cells[comp_idx + extra + 1:]
        obras.append({h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header)})
    return obras


def cmd_importar_simec(path: Path, sheet: Path, instituicao: str, uso_padrao: str,
                       simular: bool) -> int:
    if not path.exists():
        print(f"Arquivo não encontrado: {path}")
        return 1
    simec = read_simec(path)
    if not simec or "data_inicio" not in simec[0] or "nome_da_obra" not in simec[0]:
        print("O arquivo não parece um relatório de obras do SIMEC "
              "(faltam colunas como 'Nome da Obra' e 'Data de Início').")
        return 1

    header, rows, delimiter, encoding = (
        read_table(sheet) if sheet.exists() else (list(COLUMNS), [], ";", "utf-8-sig")
    )
    known_ids = {
        sid for _, r in rows
        for sid in re.findall(r"SIMEC ID (\d+)", r.get("fonte", "") + " " + r.get("observacoes", ""))
    }
    prefix = f"{instituicao} dados abertos — " if instituicao else ""

    counts: Dict[str, int] = {}
    new_rows: List[Dict[str, str]] = []

    def skip(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    for ob in simec:
        sid = ob.get("id", "")
        if sid in known_ids:
            skip("já estavam na planilha")
            continue
        if _norm_header(ob.get("situacao_da_obra", "")) != "concluida":
            skip("não concluídas")
            continue
        executed = parse_number(ob.get("executado", "") or "0") or 0
        if executed < 100:
            skip("concluídas com menos de 100% executado")
            continue
        inicio, termino = parse_date(ob.get("data_inicio", "")), parse_date(ob.get("data_termino", ""))
        if not inicio or not termino or termino <= inicio:
            skip("sem datas válidas de início e término")
            continue
        name = ob.get("nome_da_obra", "").strip()
        area, area_note = pick_area(name, ob.get("composicao", ""))
        if area is None:
            skip("sem área na descrição")
            continue

        kind, use = classify_simec(name, ob.get("composicao", ""))
        notes = ["importado do SIMEC; confira se a Data de Término é a real"]
        if kind != "construcao":
            notes.insert(0, {
                "projeto": "PROJETO: contrato de elaboração do projeto, não da obra",
                "reforma": "REFORMA: não é construção nova",
                "servico": "SERVIÇO (impermeabilização, pintura, cobertura...): não é edificação nova",
                "etapa": "ETAPA/conclusão: só parte da obra",
                "infraestrutura": "INFRAESTRUTURA: não é edificação",
                "ampliacao": "AMPLIAÇÃO: confira se a área é só do acréscimo",
                "misto": "MISTO: construção e reforma no mesmo contrato",
            }[kind])
        if area_note:
            notes.insert(0, area_note)
        municipio = ob.get("municipio_uf", "")
        uf = municipio.rsplit("/", 1)[-1].strip() if "/" in municipio else ""
        new_rows.append({
            "obra": f"{name} ({municipio.replace(' / ', '/')})" if municipio else name,
            "area_m2": _fmt_decimal(area, delimiter),
            "data_inicio": inicio.strftime("%d/%m/%Y"),
            "data_termino": termino.strftime("%d/%m/%Y"),
            "uso": use or uso_padrao,
            "pavimentos": _pavimentos(ob.get("composicao", "")),
            "uf": uf,
            "fonte": f"{prefix}SIMEC ID {sid}",
            "usar": "sim" if kind == "construcao" else "não",
            "observacoes": " | ".join(notes),
        })
        skip(f"importadas — {'usar = sim' if kind == 'construcao' else 'usar = não (' + kind + ')'}")

    print(f"Relatório do SIMEC: {len(simec)} obras em {path.name}")
    for reason, n in sorted(counts.items(), key=lambda kv: (not kv[0].startswith("importadas"), kv[0])):
        print(f"  {n:4d}  {reason}")
    usable = [r for r in new_rows if r["usar"] == "sim"]
    for r in usable[:8]:
        meses = (parse_date(r["data_termino"]) - parse_date(r["data_inicio"])).days / DAYS_PER_MONTH
        print(f"    + {r['obra'][:70]:<70} {r['area_m2']:>10} m²  {meses:5.1f} meses")
    if len(usable) > 8:
        print(f"    ... e mais {len(usable) - 8}")

    if simular:
        print("\nSimulação: nada foi gravado. Rode sem --simular para acrescentar à planilha.")
        return 0
    if not sheet.exists():
        _open_sheet(sheet)
    _append_rows(sheet, header, delimiter, encoding, new_rows)
    print(f"\n{len(new_rows)} obra(s) acrescentada(s) em {sheet} ({len(usable)} com usar = sim).")
    print("Revise as linhas com usar = não e confira algumas datas antes de treinar.")
    return 0


# ══════════════════════════════════════════════════════════════
# buscar-obrasgov: obras federais concluídas com datas EFETIVAS
# ══════════════════════════════════════════════════════════════
#
# A API pública do Obrasgov.br (Ministério da Gestão) tem milhares de
# obras concluídas, mas só ~1–4% trazem as datas EFETIVAS de início e
# fim (as demais têm só as previstas, que não servem). Destas, entram
# as que escrevem a área no nome/descrição ("1.098,29 m²").

OBRASGOV_API = "https://api.obrasgov.gestao.gov.br/obrasgov/api/projeto-investimento"
OBRASGOV_UFS: Tuple[str, ...] = tuple(
    "AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO".split()
)
OBRASGOV_INFRA_TYPES = ("INFRAESTRUTURA", "HIDRICA", "MOBILIDADE", "TRANSPORTE", "SANEAMENTO",
                        "ENERGIA", "RODOV", "PORTO", "AEROPORTO", "IRRIGACAO", "DEFESA CIVIL")
OBRASGOV_USES = (("EDUCACAO", "educacao"), ("SAUDE", "saude"), ("ESPORTE", "esporte"),
                 ("HABITACAO", "residencial"), ("ADMINISTRATIVO", "administrativo"),
                 ("CULTURA", "cultura"), ("SEGURANCA", "seguranca"))


def fetch_obrasgov(ufs: Sequence[str], max_pages: int, workers: int = 6) -> List[Dict]:
    """Obras concluídas COM datas efetivas (as demais nem são guardadas)."""
    import time
    import urllib.parse
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def get(uf: str, page: int) -> Optional[List[Dict]]:
        query = urllib.parse.urlencode({"situacao": "Concluída", "uf": uf, "pagina": page,
                                        "tamanhoDaPagina": 100})
        request = urllib.request.Request(f"{OBRASGOV_API}?{query}",
                                         headers={"User-Agent": "agepa-tcc/1.0"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=90) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace")).get("content", [])
            except Exception:
                time.sleep(3 * (attempt + 1))
        return None

    def scan(uf: str) -> List[Dict]:
        kept, seen = [], 0
        for page in range(max_pages):
            items = get(uf, page)
            if not items:
                break
            seen += len(items)
            kept += [o for o in items if o.get("dataInicialEfetiva") and o.get("dataFinalEfetiva")]
        print(f"  {uf}: {seen} concluídas lidas, {len(kept)} com datas efetivas", flush=True)
        return kept

    with ThreadPoolExecutor(workers) as pool:
        return [o for batch in pool.map(scan, ufs) for o in batch]


def obrasgov_rows(records: Sequence[Dict], delimiter: str, known_ids: set) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Converte obras do Obrasgov em linhas da planilha (mesmas regras do SIMEC)."""
    counts: Dict[str, int] = {}
    rows: List[Dict[str, str]] = []

    def count(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    for ob in records:
        oid = str(ob.get("idUnico") or ob.get("id") or "")
        if not oid or oid in known_ids:
            count("já estavam na planilha")
            continue
        inicio = parse_date(str(ob.get("dataInicialEfetiva") or ob.get("inicio") or ""))
        termino = parse_date(str(ob.get("dataFinalEfetiva") or ob.get("fim") or ""))
        if not inicio or not termino or termino <= inicio:
            count("sem datas efetivas válidas")
            continue
        name = (ob.get("nome") or "").strip()
        text = " ".join(str(ob.get(k) or "") for k in ("descricao", "metaGlobal", "funcaoSocial", "meta"))
        area, area_note = pick_area(name, text)
        if area is None:
            count("sem área no texto")
            continue
        types = _upper_plain(" ".join(
            t if isinstance(t, str) else t.get("descricao", "") for t in ob.get("tipos", []) or []
        ))
        kind, use = classify_simec(name, ob.get("descricao") or "")
        especie = _upper_plain(ob.get("especie") or "")
        if kind == "construcao" and especie and especie != "CONSTRUCAO":
            kind = {"AMPLIACAO": "ampliacao", "REFORMA": "reforma", "RECUPERACAO": "reforma"}.get(especie, "servico")
        if kind == "construcao" and any(w in types for w in OBRASGOV_INFRA_TYPES):
            kind = "infraestrutura"
        use = use or next((u for key, u in OBRASGOV_USES if key in types), "")
        notes = ["datas EFETIVAS do Obrasgov.br"]
        if kind != "construcao":
            notes.insert(0, f"{kind.upper()}: não é construção nova de edificação")
        if area_note:
            notes.insert(0, area_note)
        rows.append({
            "obra": f"{name} ({ob.get('uf', '')})" if ob.get("uf") else name,
            "area_m2": _fmt_decimal(area, delimiter),
            "data_inicio": inicio.strftime("%d/%m/%Y"),
            "data_termino": termino.strftime("%d/%m/%Y"),
            "uso": use,
            "uf": ob.get("uf", ""),
            "fonte": f"Obrasgov.br idUnico {oid}",
            "usar": "sim" if kind == "construcao" else "não",
            "observacoes": " | ".join(notes),
        })
        count(f"importadas — {'usar = sim' if kind == 'construcao' else 'usar = não (' + kind + ')'}")
    return rows, counts


def cmd_buscar_obrasgov(sheet: Path, ufs: Sequence[str], paginas: int, simular: bool,
                        de_arquivo: Optional[Path], salvar: Optional[Path]) -> int:
    if de_arquivo:
        data = json.loads(de_arquivo.read_text(encoding="utf-8"))
        records = data.get("found", data) if isinstance(data, dict) else data
        print(f"Lendo {len(records)} obras salvas em {de_arquivo.name}")
    else:
        print(f"Consultando o Obrasgov.br ({len(ufs)} UF, até {paginas} páginas de 100 por UF)... "
              "a API é lenta, pode levar dezenas de minutos.")
        records = fetch_obrasgov(ufs, paginas)
        if salvar:
            salvar.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    header, rows, delimiter, encoding = (
        read_table(sheet) if sheet.exists() else (list(COLUMNS), [], ";", "utf-8-sig")
    )
    known_ids = {i for _, r in rows for i in re.findall(r"Obrasgov\.br idUnico (\S+)", r.get("fonte", ""))}
    new_rows, counts = obrasgov_rows(records, delimiter, known_ids)

    for reason, n in sorted(counts.items(), key=lambda kv: (not kv[0].startswith("importadas"), kv[0])):
        print(f"  {n:4d}  {reason}")
    for r in [r for r in new_rows if r["usar"] == "sim"][:10]:
        meses = (parse_date(r["data_termino"]) - parse_date(r["data_inicio"])).days / DAYS_PER_MONTH
        print(f"    + {r['obra'][:70]:<70} {r['area_m2']:>10} m²  {meses:5.1f} meses")
    if simular:
        print("\nSimulação: nada foi gravado. Rode sem --simular para acrescentar à planilha.")
        return 0
    if not sheet.exists():
        _open_sheet(sheet)
    _append_rows(sheet, header, delimiter, encoding, new_rows)
    usable = sum(r["usar"] == "sim" for r in new_rows)
    print(f"\n{len(new_rows)} obra(s) acrescentada(s) em {sheet} ({usable} com usar = sim).")
    return 0


# ══════════════════════════════════════════════════════════════
# importar-cno: Cadastro Nacional de Obras (Receita Federal)
# ══════════════════════════════════════════════════════════════
#
# O CNO (dados.gov.br, "Cadastro Nacional de Obras - CNO", cno.zip) registra
# toda obra de construção civil do país: data de início, área por categoria
# (obra nova, acréscimo, reforma...) e destinação, e a situação — ENCERRADA
# (código 15) com a data do encerramento.
#
# Armadilha: obras REGULARIZADAS (casa pronta registrada só para obter o
# habite-se) têm início declarado retroativo e encerramento logo depois.
# Por isso só entram obras registradas no começo (registro até 30 dias
# antes ou 90 dias depois do início). O encerramento é a data ADMINISTRATIVA
# de fim (pode atrasar em relação ao fim físico).

CNO_SITUACAO_ENCERRADA = "15"
CNO_USES = {
    "RESIDENCIAL UNIFAMILIAR": "residencial unifamiliar",
    "RESIDENCIAL MULTIFAMILIAR": "residencial multifamiliar",
    "COMERCIAL SALAS E LOJAS": "comercial",
    "EDIFICIO DE GARAGENS": "garagem",
    "GALPAO INDUSTRIAL": "galpao",
    "CASA POPULAR": "casa popular",
    "CONJUNTO HABITACIONAL POPULAR": "conjunto habitacional",
}


def _iso(text: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(text.strip())
    except ValueError:
        return None


def select_cno(zip_path: Path, since: int = 2019, min_months: float = 3, max_months: float = 60):
    """Obras do CNO que passam nos filtros. Devolve (lista, contagens)."""
    import io
    import zipfile

    counts: Dict[str, int] = {}

    def count(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    zf = zipfile.ZipFile(zip_path)
    names = {n.lower(): n for n in zf.namelist()}

    # 1) Obras encerradas, registradas no começo, com prazo plausível.
    works: Dict[str, Dict] = {}
    with zf.open(names["cno.csv"]) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline=""))
        for row in reader:
            count("obras no CNO")
            if row["Situação"].strip() != CNO_SITUACAO_ENCERRADA:
                continue
            count("encerradas")
            ini, fim, reg = _iso(row["Data de início"]), _iso(row["Data da situação"]), _iso(row["Data de registro"])
            if not ini or not fim or not reg or ini.year < since:
                continue
            if not (-30 <= (reg - ini).days <= 90):
                count("descartadas: registradas fora do começo (regularização)")
                continue
            months = (fim - ini).days / DAYS_PER_MONTH
            if not (min_months <= months <= max_months):
                count(f"descartadas: prazo fora de {min_months:g}–{max_months:g} meses")
                continue
            works[row["CNO"].strip()] = {
                "inicio": ini, "fim": fim, "municipio": row["Nome do município"].strip(),
                "uf": row["Estado"].strip(), "nome": row["Nome"].strip(),
            }
    count_ok = len(works)

    # 2) Áreas: só "Obra Nova" de alvenaria, área principal, uma destinação.
    areas: Dict[str, Dict] = {}
    bad: set = set()
    with zf.open(names["cno_areas.csv"]) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline=""))
        for row in reader:
            cno = row["CNO"].strip()
            if cno not in works or cno in bad:
                continue
            cat = _upper_plain(row["Categoria"]).strip()
            if cat != "OBRA NOVA" or _upper_plain(row["Tipo de obra"]).strip() != "ALVENARIA":
                bad.add(cno)
                continue
            if _upper_plain(row["Tipo de Área"]).strip() != "PRINCIPAL":
                continue
            dest = _upper_plain(row["Destinação"]).strip()
            try:
                metros = float(row["Metragem"])
            except ValueError:
                bad.add(cno)
                continue
            entry = areas.setdefault(cno, {"area": 0.0, "dest": dest})
            if entry["dest"] != dest:
                bad.add(cno)
                continue
            entry["area"] += metros

    selected = []
    for cno, w in works.items():
        a = areas.get(cno)
        if cno in bad or not a:
            count("descartadas: reforma/acréscimo/não alvenaria ou sem área")
            continue
        if not 20 <= a["area"] <= 200_000 or a["dest"] not in CNO_USES:
            count("descartadas: área ou destinação fora do padrão")
            continue
        selected.append({"cno": cno, "area": a["area"], "uso": CNO_USES[a["dest"]], **w})
    counts["encerradas, registradas no começo e com prazo plausível"] = count_ok
    counts["selecionadas"] = len(selected)
    return selected, counts


def cmd_importar_cno(zip_path: Path, sheet: Path, amostra: int, simular: bool) -> int:
    import random

    print(f"Lendo {zip_path.name} (pode levar alguns minutos)...")
    selected, counts = select_cno(zip_path)
    for reason, n in counts.items():
        print(f"  {n:>10,}".replace(",", ".") + f"  {reason}")
    if not selected:
        return 1
    by_use: Dict[str, List[Dict]] = {}
    for s in selected:
        by_use.setdefault(s["uso"], []).append(s)
    print("\nPor destinação (mediana de área e de prazo):")
    for use, group in sorted(by_use.items(), key=lambda kv: -len(kv[1])):
        meses = [(g["fim"] - g["inicio"]).days / DAYS_PER_MONTH for g in group]
        print(f"  {use:<26} {len(group):>8,} obras | área {statistics.median(g['area'] for g in group):8.1f} m² "
              f"| prazo {statistics.median(meses):5.1f} meses".replace(",", "."))

    # Amostra estratificada por destinação (proporcional), reprodutível.
    rng = random.Random(42)
    sample: List[Dict] = []
    total = len(selected)
    for use, group in by_use.items():
        k = max(1, round(amostra * len(group) / total)) if amostra < total else len(group)
        sample += rng.sample(group, min(k, len(group)))

    header, rows, delimiter, encoding = (
        read_table(sheet) if sheet.exists() else (list(COLUMNS), [], ";", "utf-8-sig")
    )
    known = {m for _, r in rows for m in re.findall(r"CNO (\d+)", r.get("fonte", ""))}
    new_rows = [{
        "obra": f"{s['nome'] or 'Obra'} ({s['municipio']}/{s['uf']})",
        "area_m2": _fmt_decimal(s["area"], delimiter),
        "data_inicio": s["inicio"].strftime("%d/%m/%Y"),
        "data_termino": s["fim"].strftime("%d/%m/%Y"),
        "uso": s["uso"], "uf": s["uf"],
        "fonte": f"Receita Federal CNO {s['cno']}",
        "usar": "sim",
        "observacoes": "CNO: fim = data de ENCERRAMENTO do cadastro (administrativa)",
    } for s in sample if s["cno"] not in known]
    print(f"\nAmostra: {len(new_rows)} obras (de {total:,} selecionadas).".replace(",", "."))
    if simular:
        print("Simulação: nada foi gravado.")
        return 0
    if not sheet.exists():
        _open_sheet(sheet)
    _append_rows(sheet, header, delimiter, encoding, new_rows)
    print(f"Gravado em {sheet}.")
    return 0


# ══════════════════════════════════════════════════════════════
# treinar
# ══════════════════════════════════════════════════════════════

def _br(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def model_card(result: TrainingResult, obras: List[Obra], sheet: Path, rf_file: Optional[str],
               base: str = "", aviso: str = "") -> Dict:
    chosen = result.chosen
    by_use: Dict[str, List[float]] = {}
    for o in obras:
        by_use.setdefault(o.uso or "não informado", []).append(o.area_m2 / o.prazo_meses)
    return {
        "versao": 1,
        "base": base or None,      # de onde vêm as obras (aparece na interface)
        "aviso": aviso or None,    # ressalva sobre os dados (ex.: fim administrativo)
        "tipo": chosen.tipo,
        "nome": chosen.nome,
        "formula": result.model.formula(),
        "parametros": result.model.params(),
        "usa_comodos": result.model.usa_comodos,
        "arquivo_modelo": rf_file,
        "faixa": result.faixa,
        "incerteza": {"sigma_log": chosen.metrics.sigma_log, "nivel": 0.8},
        "metricas_cv": {**chosen.metrics.to_dict(), "validacao": result.validacao, "n_obras": result.n},
        "comparacao": [c.to_dict() for c in result.candidates],
        "treino": {
            "data": dt.datetime.now().isoformat(timespec="seconds"),
            "planilha": str(sheet),
            "sha256_planilha": hashlib.sha256(sheet.read_bytes()).hexdigest(),
            "n_obras": result.n,
            "produtividade_mediana_por_uso": {
                k: {"m2_por_mes": round(statistics.median(v), 2), "obras": len(v)} for k, v in sorted(by_use.items())
            },
        },
    }


def write_report(path: Path, result: TrainingResult, obras: List[Obra], card: Dict,
                 errors: List[str], warnings: List[str], excluded: int) -> None:
    m = result.chosen.metrics
    areas = [o.area_m2 for o in obras]
    prazos = [o.prazo_meses for o in obras]
    lines = [
        "# Treino do modelo de prazo — AGEPA", "",
        f"Gerado em {card['treino']['data']} a partir de `{Path(card['treino']['planilha']).name}`.", "",
        "## Dados", "",
        f"- Obras usadas: **{result.n}** (excluídas pela coluna `usar`: {excluded}; com erro: {len(errors)})",
        f"- Área: {_br(min(areas))} a {_br(max(areas))} m² (mediana {_br(statistics.median(areas))} m²)",
        f"- Prazo real: {_br(min(prazos), 1)} a {_br(max(prazos), 1)} meses (mediana {_br(statistics.median(prazos), 1)})",
        "", "| Uso | Obras | Produtividade mediana (m²/mês) |", "|---|---:|---:|",
    ]
    for uso, info in card["treino"]["produtividade_mediana_por_uso"].items():
        lines.append(f"| {uso} | {info['obras']} | {_br(info['m2_por_mes'], 1)} |")
    lines += [
        "", f"## Comparação dos modelos (validação cruzada {result.validacao})", "",
        "Erro medido em obras que o modelo não viu no ajuste.", "",
        "| Modelo | Fórmula | MAE (meses) | MAPE | RMSE (meses) | R² |", "|---|---|---:|---:|---:|---:|",
    ]
    for c in result.candidates:
        mark = " **(escolhido)**" if c is result.chosen else ""
        if c.metrics:
            lines.append(
                f"| {c.nome}{mark} | {c.formula_geral} | {_br(c.metrics.mae)} | {_br(100 * c.metrics.mape, 1)}% | "
                f"{_br(c.metrics.rmse)} | {_br(c.metrics.r2, 3)} |"
            )
        else:
            lines.append(f"| {c.nome} | {c.formula_geral} | — | — | — | — ({c.motivo_fora}) |")
    spread = interval(1.0, m.sigma_log)
    lines += [
        "", "## Modelo escolhido", "",
        f"**{result.chosen.nome}**: `{card['formula']}`", "",
        f"- Erro médio esperado: **{_br(m.mae)} meses** ({_br(100 * m.mape, 1)}% do prazo).",
        f"- Intervalo de 80%: de {_br(spread[0])}× a {_br(spread[1])}× o valor previsto.",
        f"- Válido para áreas entre {_br(result.faixa['area_m2'][0])} e {_br(result.faixa['area_m2'][1])} m²; "
        "fora disso o sistema avisa.",
        "- Critério: menor MAE; com diferença de até 5%, vence o modelo mais simples.",
        "", "## Obras com maior erro (vale revisar os dados)", "",
        "| Linha | Obra | Área (m²) | Prazo real | Previsto (validação) |", "|---:|---|---:|---:|---:|",
    ]
    pred = result.chosen.cv_pred
    order = np.argsort(-np.abs(pred - np.array(prazos)))[:5]
    for i in order:
        o = obras[i]
        lines.append(f"| {o.linha} | {o.obra} | {_br(o.area_m2)} | {_br(o.prazo_meses, 1)} | {_br(pred[i], 1)} |")
    if errors or warnings:
        lines += ["", "## Problemas na planilha", ""] + [f"- {e}" for e in errors + warnings]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_treinar(sheet: Path, out: Path, report: Path, ignore_errors: bool,
                base: str = "", aviso: str = "", por_uso: bool = False) -> int:
    if not sheet.exists():
        print(f"Planilha não encontrada: {sheet}")
        return 1
    try:
        obras, errors, warnings, excluded = load_obras(sheet)
    except ValueError as exc:
        print(f"Erro: {exc}")
        return 1

    for msg in errors:
        print(f"  ✗ {msg}")
    for msg in warnings:
        print(f"  ! {msg}")
    if errors and not ignore_errors:
        print(f"\n{len(errors)} linha(s) com erro. Corrija-as ou rode com --ignorar-erros para treinar sem elas.")
        return 1

    area = np.array([o.area_m2 for o in obras], dtype=float)
    prazo = np.array([o.prazo_meses for o in obras], dtype=float)
    comodos = (np.array([o.comodos for o in obras], dtype=float)
               if obras and all(o.comodos for o in obras) else None)
    try:
        result = train(area, comodos, prazo)
    except ValueError as exc:
        print(f"Não foi possível treinar: {exc}")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    rf_file: Optional[str] = None
    if result.chosen.tipo == "random_forest":
        import joblib
        rf_file = out.stem + "_rf.joblib"
        joblib.dump(result.model.estimator, out.parent / rf_file)
    card = model_card(result, obras, sheet, rf_file, base, aviso)
    if por_uso:
        # Curva própria por tipo de uso (casa, prédio, comercial): só a que erra menos que a geral.
        usos = np.array([o.uso or "" for o in obras])
        card["por_uso"] = train_por_uso(area, prazo, usos)
        for uso, sub in card["por_uso"].items():
            mc = sub["metricas_cv"]
            print(f"  curva própria '{uso}': {sub['n_obras']} obras, MAE {_br(mc['mae_meses'])} × "
                  f"{_br(mc['mae_curva_geral_meses'])} da curva geral")
    out.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    report.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, result, obras, card, errors, warnings, excluded)

    print(f"\nComparação ({result.validacao}, {result.n} obras):")
    for c in result.candidates:
        if c.metrics:
            flag = "  ← escolhido" if c is result.chosen else ""
            print(f"  {c.nome:<24} MAE {_br(c.metrics.mae):>6} meses   MAPE {_br(100 * c.metrics.mape, 1):>5}%{flag}")
        else:
            print(f"  {c.nome:<24} fora da comparação ({c.motivo_fora})")
    print(f"\nModelo: {card['formula']}")
    if out.resolve() == DEFAULT_MODEL.resolve():
        print(f"Salvo em {out} (o servidor passa a usá-lo sozinho).")
    elif out.resolve() == CNO_MODEL.resolve():
        print(f"Salvo em {out} (o servidor passa a mostrá-lo como comparação).")
    else:
        print(f"Salvo em {out}.")
    print(f"Relatório: {report}")
    return 0


# ══════════════════════════════════════════════════════════════
# tipologias-fnde: prazo das obras padrão do FNDE (SIMEC)
# ══════════════════════════════════════════════════════════════
#
# Planilha: dados/obras_simec_fnde.csv (avaliacao/ferramentas/simec_extrair.py, a partir
# do CSV nacional de transparência de obras do SIMEC). Todas as obras de uma tipologia
# têm a MESMA área (projeto padrão): não há curva por área a ajustar — o que serve é a
# distribuição dos prazos das obras iguais (mediana e faixa de 80%).

FNDE_SHEET = BACKEND_DIR.parent / "dados" / "obras_simec_fnde.csv"
FNDE_TABLE = BACKEND_DIR / "models" / "prazo_fnde_tipologias.json"
FNDE_AVISO = ("Datas de CONTRATO do SIMEC: início = assinatura do contrato; fim = término do "
              "contrato (vigência com aditivos). O SIMEC não publica a data real de conclusão.")


def cmd_tipologias_fnde(sheet: Path, out: Path) -> int:
    from services.fnde_tipologias import TIPOLOGIAS
    obras, errors, _, excluded = load_obras(sheet)
    if errors:
        print(f"{len(errors)} linha(s) com erro na planilha (ignoradas).")
    _, rows, _, _ = read_table(sheet)
    vistoria: Dict[int, float] = {}
    for line, r in rows:
        m = re.search(r"vistoria 'Conclu[ií]do': (\d{2}/\d{2}/\d{4})", r.get("observacoes", ""))
        ini = parse_date(r.get("data_inicio", ""))
        fim = parse_date(m.group(1)) if m else None
        if ini and fim and fim > ini:
            vistoria[line] = (fim - ini).days / DAYS_PER_MONTH
    by_line = {line: r for line, r in rows}
    table: Dict[str, Dict] = {}
    for key, (label, area) in TIPOLOGIAS.items():
        sel = [o for o in obras if label in by_line.get(o.linha, {}).get("fonte", "")]
        if len(sel) < MIN_OBRAS_TIPOLOGIA:
            print(f"  {label}: só {len(sel)} obra(s) — fica de fora.")
            continue
        prazos = np.array([o.prazo_meses for o in sel])
        vis = np.array([vistoria[o.linha] for o in sel if o.linha in vistoria])
        table[key] = {
            "tipologia": label, "area_m2": area, "n_obras": int(len(prazos)),
            "mediana_meses": round(float(np.median(prazos)), 1),
            "intervalo_80_meses": [round(float(np.quantile(prazos, 0.1)), 1), round(float(np.quantile(prazos, 0.9)), 1)],
            "media_meses": round(float(prazos.mean()), 1),
            "mediana_ate_ultima_vistoria_meses": round(float(np.median(vis)), 1) if len(vis) else None,
            "ufs": len({by_line[o.linha].get("uf", "") for o in sel} - {""}),
        }
        t = table[key]
        print(f"  {label:40} {t['n_obras']:5d} obras  mediana {t['mediana_meses']:5.1f} meses  "
              f"80%: {t['intervalo_80_meses'][0]}–{t['intervalo_80_meses'][1]}")
    card = {
        "versao": 1,
        "fonte": "SIMEC — transparência de obras (FNDE), planilha nacional",
        "planilha": str(sheet), "sha256_planilha": hashlib.sha256(sheet.read_bytes()).hexdigest(),
        "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "excluidas_coluna_usar": excluded, "aviso": FNDE_AVISO, "tipologias": table,
    }
    out.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Salvo em {out} (o servidor passa a usá-lo sozinho).")
    return 0


MIN_OBRAS_TIPOLOGIA: int = 30


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Treina o modelo de prazo do AGEPA com obras reais.")
    sub = parser.add_subparsers(dest="comando", required=True)

    p_ext = sub.add_parser("extrair", help="lê PDFs de plantas e acrescenta área/cômodos à planilha")
    p_ext.add_argument("caminhos", nargs="+", help="pasta(s) ou arquivo(s) PDF")
    p_ext.add_argument("--planilha", type=Path, default=DEFAULT_SHEET)

    p_imp = sub.add_parser("importar-simec",
                           help="importa o relatório de obras do SIMEC (dados abertos de universidades)")
    p_imp.add_argument("arquivo", type=Path, help="CSV do relatório de obras do SIMEC")
    p_imp.add_argument("--planilha", type=Path, default=DEFAULT_SHEET)
    p_imp.add_argument("--instituicao", default="", help='sigla para a coluna fonte (ex.: "UFV")')
    p_imp.add_argument("--uso-padrao", default="educacao",
                       help="uso das obras que o nome não indica (padrão: educacao)")
    p_imp.add_argument("--simular", action="store_true", help="só mostra o que entraria, sem gravar")

    p_og = sub.add_parser("buscar-obrasgov",
                          help="busca no Obrasgov.br obras concluídas com datas efetivas e área")
    p_og.add_argument("--planilha", type=Path, default=DEFAULT_SHEET)
    p_og.add_argument("--ufs", default=",".join(OBRASGOV_UFS), help="ex.: ES,DF,MG (padrão: todas)")
    p_og.add_argument("--paginas", type=int, default=25, help="páginas de 100 obras por UF (padrão: 25)")
    p_og.add_argument("--de-arquivo", type=Path, help="usa uma busca já salva (JSON) em vez de consultar a API")
    p_og.add_argument("--salvar", type=Path, help="salva o resultado da busca em JSON para reusar")
    p_og.add_argument("--simular", action="store_true", help="só mostra o que entraria, sem gravar")

    p_cno = sub.add_parser("importar-cno", help="amostra de obras novas encerradas do CNO (Receita Federal)")
    p_cno.add_argument("arquivo", type=Path, help="cno.zip do dados.gov.br")
    p_cno.add_argument("--planilha", type=Path, default=BACKEND_DIR.parent / "dados" / "obras_cno.csv")
    p_cno.add_argument("--amostra", type=int, default=3000, help="quantas obras gravar (padrão: 3000)")
    p_cno.add_argument("--simular", action="store_true", help="só mostra as contagens, sem gravar")

    p_tr = sub.add_parser("treinar", help="valida a planilha, compara modelos e salva o melhor")
    p_tr.add_argument("--planilha", type=Path, default=DEFAULT_SHEET)
    p_tr.add_argument("--saida", type=Path, default=DEFAULT_MODEL)
    p_tr.add_argument("--relatorio", type=Path, default=DEFAULT_REPORT)
    p_tr.add_argument("--ignorar-erros", action="store_true", help="treina só com as linhas válidas")
    p_tr.add_argument("--base", default="", help='de onde vêm as obras, para a interface (ex.: "CNO · obras privadas")')
    p_tr.add_argument("--aviso", default="", help="ressalva sobre os dados, mostrada junto do prazo")
    p_tr.add_argument("--por-uso", action="store_true",
                      help="curva própria por tipo de uso (coluna uso), quando erra menos que a geral")

    p_tip = sub.add_parser("tipologias-fnde",
                           help="prazo das obras padrão do FNDE (SIMEC) por tipologia → models/prazo_fnde_tipologias.json")
    p_tip.add_argument("--planilha", type=Path, default=FNDE_SHEET)
    p_tip.add_argument("--saida", type=Path, default=FNDE_TABLE)

    args = parser.parse_args(argv)
    if args.comando == "tipologias-fnde":
        return cmd_tipologias_fnde(args.planilha, args.saida)
    if args.comando == "extrair":
        return cmd_extrair(args.caminhos, args.planilha)
    if args.comando == "importar-simec":
        return cmd_importar_simec(args.arquivo, args.planilha, args.instituicao,
                                  args.uso_padrao, args.simular)
    if args.comando == "importar-cno":
        return cmd_importar_cno(args.arquivo, args.planilha, args.amostra, args.simular)
    if args.comando == "buscar-obrasgov":
        ufs = [u.strip().upper() for u in args.ufs.split(",") if u.strip()]
        return cmd_buscar_obrasgov(args.planilha, ufs, args.paginas, args.simular,
                                   args.de_arquivo, args.salvar)
    return cmd_treinar(args.planilha, args.saida, args.relatorio, args.ignorar_erros,
                       args.base, args.aviso, args.por_uso)


if __name__ == "__main__":
    sys.exit(main())
