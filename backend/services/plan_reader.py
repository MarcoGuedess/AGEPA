# ============================================================
# AGEPA — Interpretação da Planta Baixa
# services/plan_reader.py
# ============================================================
#
# Transforma os TEXTOS de uma prancha (com posição e direção)
# em área, ambientes e cômodos. Não sabe como o texto foi lido:
# pode vir do PDF (texto exato, pdf_reader.py) ou do OCR
# (Florence-2, ai_service.py). Por isso pode ser testado sem IA.
#
# Princípio: nenhum número é inventado. Cada área devolvida tem
# uma origem (carimbo, rótulo do ambiente ou cotas) e é conferida
# contra as outras fontes quando elas existem.
#
# Ordem de confiança da área:
#   1. Carimbo / quadro de áreas ("ÁREA A CONSTRUIR: 402,43 m²").
#   2. Soma dos rótulos dos ambientes ("ÁREA=12,50m²", "12,50 m²"
#      ou o número dentro de um círculo/elipse) — área útil.
#   3. Envoltória pelas cotas (aproximação por excesso).
#   4. Nada legível → área indisponível (o usuário informa).
# ============================================================

from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

logger = logging.getLogger("agepa.plan_reader")


# ══════════════════════════════════════════════════════════════
# Texto
# ══════════════════════════════════════════════════════════════

def normalize(text: str) -> str:
    """Sem acentos, maiúsculo, espaços simples. NFKD também troca '²' por '2'."""
    text = text.replace("</s>", "").replace("<s>", "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().upper()


def parse_number(raw: str) -> Optional[float]:
    """'1.234,56' → 1234.56 · '12,5' → 12.5 · '12.50' → 12.5 · '1234' → 1234."""
    s = raw.strip().replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # padrão BR: 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                                    # padrão EN: 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):  # 1.234 = milhar
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


# Número no formato brasileiro ou simples.
NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"

# "ÁREA=36,20m²", "AREA: 7.74 m2", "A=12,5m²". O OCR troca '=' por '-'
# e '²' por '*', '"', 'º', 'ª', 't', '+' ou dobra o m ("AREA-36.20m*", "A=14,54 mº",
# "A-8.00 mm2", "A= 3.00 mt" — planta do TCC da UFSC). O prefixo "A=" é o sinal forte;
# o que vem depois do m só não pode ser o começo de outra palavra.
AREA_EXPLICIT_RE = re.compile(
    rf"(?:\bAREA(?:\s+UTIL)?\s*[=:\-]?|\bA\s*[=:\-])\s*-?\s*({NUM})\s*"
    r"M{1,2}(?:[2*'\"+1T](?![A-Z])|[OA](?![A-Z])|(?![A-Z]))"
)
# "36,20 m²" / "36.20m2" sem o prefixo ÁREA.
VALUE_M2_RE = re.compile(rf"({NUM})\s*M\s*(?:2|\*|\"|')")
# Valor numa célula do carimbo: "402,43", ou "402,43 mf" (o OCR troca o "²").
# Exige 2 casas decimais para não confundir com ano ("2010") ou escala.
DECLARED_VALUE_RE = re.compile(r"^(\d{1,3}(?:\.\d{3})*,\d{2}|\d+[.,]\d{2})\s*(?:M\S{0,2})?$")
# "A=7.50" sem a unidade: no TCC, a linha da porta corta o "m²" do rótulo do hall
# e o OCR para no número. Só vale com nome: o de um ambiente por perto ou a
# etiqueta logo acima ("VARAN" — a parede corta o "VARANDA"); solto, não entra.
AREA_NO_UNIT_RE = re.compile(r"^A[=:](\d{1,4}[.,]\d{2})$")
# "178,71" sozinho (área dentro de círculo ou cota em metros).
BARE_DECIMAL_RE = re.compile(r"^(\d{1,4}[.,]\d{2})$")
# "503" sozinho (cota em centímetros).
BARE_INT_RE = re.compile(r"^(\d{2,4})$")
# Sequência de cotas lida junta pelo OCR: "15 503 15 1248".
NUM_SEQ_RE = re.compile(r"^\d{1,4}(?:[.,]\d{2})?(?: \d{1,4}(?:[.,]\d{2})?)+$")

# Palavras de ambiente (texto normalizado).
ROOM_KEYWORDS: Tuple[str, ...] = (
    "AREA DE SERVICO", "A.S.", "SALA", "QUARTO", "DORMITORIO", "DORM", "SUITE",
    "COZINHA", "COPA", "BANHEIRO", "BANHO", "WC", "SANITARIO", "SANIT", "LAVABO",
    "SERVICO", "LAVANDERIA", "VARANDA", "SACADA", "GARAGEM", "CIRCULACAO", "CIRC",
    "HALL", "CORREDOR", "ESCRITORIO", "DEPOSITO", "DESPENSA", "CLOSET", "SOLARIO",
    "REFEITORIO", "ALMOXARIFADO", "RECEPCAO", "VESTIARIO", "ESTAR", "JANTAR",
    "GOURMET", "BERCARIO", "SECRETARIA", "DIRETORIA", "COORDENACAO", "LOJA",
    "ESCADA", "TERRACO", "LAVAGEM", "PREPARO", "CANTINA", "SALAO", "AUDITORIO",
    "LABORATORIO", "BIBLIOTECA", "ARQUIVO", "ENFERMARIA", "CONSULTORIO", "ESPERA",
    "ATENDIMENTO", "GUARITA", "PORTARIA", "CAMARA FRIA", "ACADEMIA",
    "BRINQUEDOTECA", "LACTARIO", "FRALDARIO", "ABRIGO", "CONVIVENCIA", "REUNIAO",
    "OFICINA", "DEF", "PNE", "ESCOLA",
    "CALCADA", "JARDIM", "PATIO", "QUINTAL", "PASSEIO", "ESTACIONAMENTO",
)
# Áreas abertas: entram na área externa, não na soma dos ambientes. O pátio
# coberto, o solário e o playground são "área aberta" também no quadro de áreas
# dos projetos padrão (FNDE): ficam fora da área construída.
EXTERNAL_KEYWORDS: Tuple[str, ...] = (
    "CALCADA", "JARDIM", "QUINTAL", "PASSEIO", "ESTACIONAMENTO",
    "PATIO", "SOLARIO", "PLAYGROUND", "PARQUINHO", "HORTA", "AR LIVRE", "AREA LIVRE",
    "AREA VERDE", "DESCOBERTO", "DESCOBERTA", "MANOBRA",
    # Casa da UNIFAP: "Piscina A=15,00 m²" e "Deck em madeira A=38,19 m²" — ao ar livre.
    "PISCINA", "DECK",
)


def _is_external(text: str) -> bool:
    norm = normalize(text)
    return any(_has_word(norm, k) for k in EXTERNAL_KEYWORDS)
# Textos que não são ambientes: mobiliário, equipamentos e notas.
NOT_ROOM_WORDS: Tuple[str, ...] = (
    "PAREDE", "BANCADA", "MESA", "BALCAO", "PIA", "TANQUE", "FOGAO", "GELADEIRA",
    "ARMARIO", "PRATELEIRA", "CAIXA", "DISTRIBUICAO", "FRITADEIRA", "BANCO",
    "MICTORIO", "FORNO", "ESCALA", "PLANTA", "CORTE", "FACHADA", "PROJECAO",
    "COBERTURA", "BEIRAL", "NIVEL", "LEGENDA", "OBS", "NOTA", "PORTA", "PORTAS",
    "JANELA", "JANELAS", "CAIXILHOS", "ESQUADRIAS", "RESPONSAVEL", "CONTEUDO",
    "FOLHA", "DESENHO", "DATA", "PROJETO",
    # Quadro de louças e metais ("Vaso Sanitário", "Lavatório 100x30"...)
    "VASO", "LAVATORIO", "LAVATORIOS", "CHUVEIRO", "TORNEIRA", "BACIA", "DUCHA",
    "BARRA", "ESPELHO", "SABONETEIRA", "DISPENSER", "CUBA", "QUADRO",
)

# Rótulos do carimbo / quadro de áreas com a área construída total.
# Peso maior = mais específico; empate fica com o maior valor (o total).
BUILT_AREA_KEYS: Tuple[Tuple[str, int], ...] = (
    ("AREA TOTAL A CONSTRUIR", 3), ("AREA A CONSTRUIR", 3),
    ("AREA TOTAL CONSTRUIDA", 3), ("AREA CONSTRUIDA", 3),
    ("AREA DE CONSTRUCAO", 3), ("AREA DA CONSTRUCAO", 3), ("AREA CONSTRUCAO", 3), ("AREA EDIFICADA", 3),
    ("AREA DA EDIFICACAO", 3), ("AREA BRUTA", 2),
    ("AREA A AMPLIAR", 2), ("AREA DE AMPLIACAO", 2),
    ("AREA A REFORMAR", 2), ("AREA DE REFORMA", 2),
    ("AREA TOTAL", 1), ("AREA DO PAVIMENTO", 1),
    # Material de venda (planta humanizada): a área de UMA unidade.
    ("AREA PRIVATIVA", 2),
)
BUILT_AREA_EXCLUDE: Tuple[str, ...] = (
    "TERRENO", "LOTE", "PERMEAVEL", "LIVRE", "OCUPACAO", "APROVEITAMENTO",
    "DEMOLIR", "DEMOLICAO", "EXISTENTE", "UTIL",
)


# Área construída escrita em TEXTO CORRIDO (memorial, TCC, publicação), em que o número
# pode vir antes ou depois das palavras: "possui 51,34 m² de área construída",
# "área construída total de 52 m²", "área total de 980,40 m² de cobertura".
_PROSE_NUM = r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_PROSE_M2 = r"\s*(?:m\s*²|m2|metros\s+quadrados)"
_PROSE_KEY = (r"(?:(?:á|a)rea\s+(?:total\s+)?(?:constru(?:í|i)da(?:\s+(?:útil|util))?|edificada|"
              r"de\s+constru(?:ç|c)(?:ã|a)o)|superf(?:í|i)cie\s+(?:total|constru(?:í|i)da))(?:\s+total)?")
PROSE_AREA_RES: Tuple[re.Pattern, ...] = (
    # Lista por pavimento com o total: "Área edificada: Pav. Térreo: 312,45 m² - Subsolo:
    # 148,00 m² - TOTAL: 460,45 m²" — o TOTAL é a área (vem primeiro, para ganhar).
    re.compile(rf"{_PROSE_KEY}\s*:?[^;]{{0,160}}?(?<![A-Za-z])TOTAL\s*[:=]?\s*{_PROSE_NUM}{_PROSE_M2}", re.I),
    re.compile(rf"{_PROSE_NUM}{_PROSE_M2}\s+de\s+{_PROSE_KEY}", re.I),
    re.compile(rf"{_PROSE_KEY}\s*(?:de|com|é\s+de|igual\s+a|equivalente\s+a|:|=|[-–])?\s*"
               rf"(?:aproximadamente\s+|cerca\s+de\s+)?{_PROSE_NUM}{_PROSE_M2}", re.I),
    # "área total de X m² construída" — mas NÃO "de cobertura": a folha da quadra do FNDE
    # separa "ÁREA CONSTRUÍDA: 785,00 m²" de "ÁREA COBERTURA: 980,40 m²" (telhado).
    re.compile(rf"(?:á|a)rea\s+total\s+de\s+{_PROSE_NUM}{_PROSE_M2}\s+(?:de\s+)?"
               r"(?:constru(?:í|i)da|edificada)", re.I),
    # "remanescente de obra com área de 456,22 m²", "edificação com área total de …"
    re.compile(rf"(?:obra|edifica(?:ç|c)(?:ã|a)o|edif(?:í|i)cio|pr(?:é|e)dio|constru(?:ç|c)(?:ã|a)o)\s+com\s+"
               rf"(?:uma\s+)?(?:á|a)rea\s+(?:total\s+)?de\s+{_PROSE_NUM}{_PROSE_M2}", re.I),
)
# Contexto (antes do número) que diz que a área NÃO é a da obra: limite de norma ou
# edital ("no mínimo 46,00 m²"), terreno, UM apartamento numerado de um prédio
# (material de venda: "APARTAMENTO 403 … Área total = 75,65 m²"), ampliação. Falar das
# "48 unidades" do prédio perto do total (memorial do TCC) não exclui.
# Limites e terreno valem só na MESMA frase ou linha do número (num quadro, "Área do
# terreno" na linha de cima não anula "Área Total de Construção"); o título de UM
# apartamento vale das linhas de cima (material de venda).
# Rótulo do PRÓPRIO número (depois do valor anterior): exigência ou terreno —
# "implantada num terreno de 5.650 m², com uma área construída útil de 1.161,77 m²"
# (Mauá) não perde a área construída por causa do terreno citado antes.
CLAUSE_LIMIT_RE = re.compile(
    r"MINIM|MAXIM|SUPERIOR A|INFERIOR A|TERRENO|(?<![A-Z])LOTE(?![A-Z])|(?<![A-Z])ATE(?![A-Z])"
)
# Frase inteira: cenário que não é a obra projetada ("o estudo de ampliação … 61,12 m²").
LIMIT_CONTEXT_RE = re.compile(r"AMPLIA")
UNIT_CONTEXT_RE = re.compile(
    r"APARTAMENTO\s*N?\W?\s*\d|(?<![A-Z])APTO\.?\s*\d|UNIDADE\s*N?\W?\s*\d|PRIVATIV"
)


# Área de UM pavimento ("Pav. Térreo: 312,45 m²", "o pavimento térreo, que possui área
# total de 312,45 m²"): olhada só no rótulo do próprio número, depois do valor anterior —
# senão o "TOTAL: 460,45 m²" depois de "Subsolo: 148,00 m²" cairia junto. "Edificação de
# dois pavimentos" (contagem) não é rótulo de pavimento.
FLOOR_LABEL_RE = re.compile(
    r"(?<![A-Z])(?:TERREO|SUBSOLO|MEZANINO|PAV\.|PAVIMENTO\s+(?:TERREO|SUPERIOR|INFERIOR|TIPO|\d)|"
    r"\d+\s*[º°O]?\s*(?:PAVIMENTO|ANDAR)(?!ES|S))"
)


def _own_label(text: str, start: int) -> str:
    """O rótulo do número: o texto desde o valor em m² anterior (no máximo 90 caracteres)."""
    window = text[max(start - 90, 0):start]
    units = list(re.finditer(r"m\s*[²2]", window, re.I))
    return normalize(window[units[-1].end():] if units else window)


def area_context_excluded(text: str, start: int, before: int = 160) -> bool:
    """O texto antes de ``start`` diz que a área é de outra coisa (exigência, terreno, apartamento,
    um pavimento só, ampliação)?"""
    label = _own_label(text, start)
    if FLOOR_LABEL_RE.search(label) or CLAUSE_LIMIT_RE.search(label):
        return True
    window = text[max(start - before, 0):start]
    cut = max(window.rfind(". "), window.rfind(";"))
    for m in re.finditer(r"[\r\n]+\s*(\S)", window):
        # Quebra de linha que continua a frase em minúscula é só quebra de texto, não
        # fim de frase ("…ampliação da casa abrange mais um quarto e\nassim a área…").
        if not m.group(1).islower():
            cut = max(cut, m.start())
    sentence = normalize(window[cut + 1:] if cut >= 0 else window)
    return LIMIT_CONTEXT_RE.search(sentence) is not None or UNIT_CONTEXT_RE.search(normalize(window)) is not None


def prose_built_areas(text: str) -> List[Tuple[float, int, str]]:
    """Áreas construídas ditas em texto corrido: [(valor, posição, trecho)], na ordem do texto."""
    hits: Dict[int, Tuple[float, int, str]] = {}
    for pattern in PROSE_AREA_RES:
        for m in pattern.finditer(text):
            value = parse_number(m.group(1))
            if value is None or not 10 <= value <= 1_000_000 or area_context_excluded(text, m.start(1)):
                continue
            hits.setdefault(m.start(1), (value, m.start(1), re.sub(r"\s+", " ", m.group(0)).strip()))
    return [hits[k] for k in sorted(hits)]


# Prazo de execução da OBRA escrito no edital/memorial: "O prazo de execução do contrato
# será de 240 (duzentos e quarenta) dias", "prazo de execução da obra será de 16
# (dezesseis) meses", "12 meses para construção da Creche". Prazos de processo ("3 dias
# úteis" para recurso, "60 dias" de validade da proposta) não contam.
_EXTENSO = r"(?:\s*\([^)]{2,40}\))?"
PRAZO_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"prazo\s+(?:de|para\s+a?)\s*(?:execu(?:ç|c)(?:ã|a)o|conclus(?:ã|a)o|entrega)"
               r"[^.;]{0,90}?(?<![\d.,])(\d{1,4})" + _EXTENSO + r"\s*(dias|meses|m(?:ê|e)s)(?!\s+(?:úteis|uteis))",
               re.I),
    re.compile(r"(?<![\d.,])(\d{1,3})" + _EXTENSO + r"\s*(meses)\s+para\s+(?:a\s+)?"
               r"(?:constru(?:ç|c)(?:ã|a)o|execu(?:ç|c)(?:ã|a)o)\s+d[aoe]", re.I),
)
PRAZO_EXCLUDE_RE = re.compile(r"VALIDADE|GARANTIA|RECURS|IMPUGNA|PROPOSTA|VIGENCIA|PAGAMENTO|PROJETO")


def prose_prazos(text: str) -> List[Tuple[float, int, str]]:
    """Prazos de execução da obra ditos no texto: [(meses, posição, trecho)], na ordem do texto."""
    hits: Dict[int, Tuple[float, int, str]] = {}
    for pattern in PRAZO_RES:
        for m in pattern.finditer(text):
            n, unit = int(m.group(1)), m.group(2).lower()
            meses = n / 30.0 if unit.startswith("dia") else float(n)
            if not 1.0 <= meses <= 120.0:
                continue
            # o que vem entre "prazo de execução" e o número não pode ser outra coisa
            if PRAZO_EXCLUDE_RE.search(normalize(m.group(0)[:m.start(1) - m.start()])):
                continue
            hits.setdefault(m.start(1), (round(meses, 1), m.start(1), re.sub(r"\s+", " ", m.group(0)).strip()))
    return [hits[k] for k in sorted(hits)]


def _has_word(norm: str, word: str) -> bool:
    return re.search(rf"(?<![A-Z]){re.escape(word)}S?(?![A-Z])", norm) is not None


_LOWER_WORDS = {"de", "da", "do", "das", "dos", "e", "em", "a", "o", "para", "com"}


def title_pt(text: str) -> str:
    """'DEPÓSITO DE LIXO' → 'Depósito de Lixo' (só se o texto vier todo maiúsculo)."""
    if not text.isupper():
        return text
    words = text.lower().split()
    return " ".join(
        w if i and w in _LOWER_WORDS else w[:1].upper() + w[1:]
        for i, w in enumerate(words)
    )


# ══════════════════════════════════════════════════════════════
# Estruturas
# ══════════════════════════════════════════════════════════════

@dataclass
class TextItem:
    """Um texto da prancha no quadro "em pé" (texto principal na horizontal).

    ``angle`` é a direção de leitura em graus (0 = esquerda→direita,
    90 = de cima para baixo, 180 = de cabeça para baixo, 270 = de baixo
    para cima); ``None`` para textos inclinados (símbolos).
    """
    text: str
    x1: float
    y1: float
    x2: float
    y2: float
    angle: Optional[int] = 0
    source: str = "ocr"            # "pdf" (texto exato) | "ocr"
    confirmed: bool = False        # OCR: valor conferido por releitura ampliada
    norm: str = field(init=False)

    def __post_init__(self) -> None:
        self.norm = normalize(self.text)

    def set_text(self, text: str) -> None:
        self.text = text
        self.norm = normalize(text)

    @property
    def reliable(self) -> bool:
        return self.source == "pdf" or self.confirmed

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def vertical(self) -> bool:
        return self.angle in (90, 270)

    @property
    def char_h(self) -> float:
        """Altura da letra (a dimensão perpendicular à leitura)."""
        h = (self.x2 - self.x1) if self.vertical else (self.y2 - self.y1)
        return max(h, 1.0)

    @property
    def length(self) -> float:
        return (self.y2 - self.y1) if self.vertical else (self.x2 - self.x1)

    def local(self, x: float, y: float) -> Tuple[float, float]:
        """(u, v) de um ponto no referencial deste texto: u ao longo da
        leitura, v para "baixo" das letras."""
        theta = math.radians(self.angle or 0)
        dx, dy = x - self.cx, y - self.cy
        u = dx * math.cos(theta) + dy * math.sin(theta)
        v = -dx * math.sin(theta) + dy * math.cos(theta)
        return u, v

    def box(self) -> Tuple[int, int, int, int]:
        return int(self.x1), int(self.y1), int(self.x2), int(self.y2)


@dataclass
class Room:
    name: str
    area_m2: Optional[float]
    external: bool
    item: TextItem                         # onde o nome (ou a área) está
    area_item: Optional[TextItem] = None
    area_source: Optional[str] = None      # "rotulo" | "circulo" | "quadro"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "area_m2": round(self.area_m2, 2) if self.area_m2 is not None else None,
            "external": self.external,
            "area_source": self.area_source,
        }


@dataclass
class DeclaredArea:
    value: float
    label: str               # texto do rótulo ("ÁREA A CONSTRUIR :")
    key_item: TextItem
    value_item: TextItem

    @property
    def reliable(self) -> bool:
        return self.value_item.reliable


@dataclass
class Envelope:
    largura_m: float
    profundidade_m: float
    unit: str                # "cm" | "m"
    chains: int              # cadeias de cotas coerentes usadas
    px_per_unit: float
    # Texto exato (PDF) ou conferido: só assim a envoltória serve de conferência.
    # Do OCR ela costuma ser parcial (cotas não lidas ficam de fora).
    reliable: bool = True
    # Onde as cadeias de cotas estão no desenho (x1, y1, x2, y2, em px): serve para
    # ver se o retângulo contém os ambientes ou só parte da obra.
    box: Optional[Tuple[float, float, float, float]] = None

    @property
    def area_m2(self) -> float:
        return self.largura_m * self.profundidade_m

    def covers(self, points: Sequence[Tuple[float, float]]) -> bool:
        """As cotas abrangem a faixa central (10% a 90%) dos ambientes?

        Cotas lidas só numa parte da obra dão um retângulo MENOR que o prédio:
        ambientes ficam do lado de fora. (Os 10% das pontas descontam textos do
        carimbo tomados por nomes de ambiente.)
        """
        if self.box is None or len(points) < 3:
            return True
        x1, y1, x2, y2 = self.box
        xs = np.asarray([p[0] for p in points], dtype=float)
        ys = np.asarray([p[1] for p in points], dtype=float)
        tol_x, tol_y = 0.05 * (x2 - x1), 0.05 * (y2 - y1)
        return bool(np.percentile(xs, 10) >= x1 - tol_x and np.percentile(xs, 90) <= x2 + tol_x
                    and np.percentile(ys, 10) >= y1 - tol_y and np.percentile(ys, 90) <= y2 + tol_y)


@dataclass
class PlanReading:
    """Resultado da interpretação: área, ambientes e as evidências."""
    area_m2: Optional[float]
    area_kind: Optional[str]          # construida | util | envoltoria
    method: str                       # carimbo | areas_declaradas | cotas | indisponivel
    confidence: str                   # alta | media | baixa
    qtd_comodos: int
    rooms: List[Room]
    declared: Optional[DeclaredArea]
    rooms_sum_m2: Optional[float]
    rooms_with_area: int
    area_externa_m2: float
    envelope: Optional[Envelope]
    checks: List[str] = field(default_factory=list)     # conferências que passaram
    warnings: List[str] = field(default_factory=list)   # o que pede atenção
    evidence: List[TextItem] = field(default_factory=list)  # textos que definem a área
    cota_items: List[TextItem] = field(default_factory=list)
    # Rótulo do carimbo achado sem valor legível (OCR): vale reler ampliado.
    declared_key: Optional[TextItem] = None
    # Soma de um quadro de áreas por pavimento (confere o total do carimbo).
    floors_sum: Optional[float] = None
    # Ambientes localizados NO DESENHO (com áreas de um quadro, ``rooms`` aponta
    # para as linhas do quadro; aqui ficam os nomes na planta). Servem de ponto de
    # partida para medir o contorno do prédio (geometry.py).
    drawing_rooms: List[Room] = field(default_factory=list)
    # Todos os ambientes têm área (a soma é a área útil inteira, não só um piso).
    rooms_complete: bool = False
    # Retângulo do PRÉDIO pelas cotas (as cadeias que cobrem os ambientes, sem as do
    # terreno nem as de cortes e fachadas na mesma folha). Limite do contorno.
    building_envelope: Optional[Envelope] = None


# ══════════════════════════════════════════════════════════════
# Classificação dos textos
# ══════════════════════════════════════════════════════════════

def _is_room_name(item: TextItem) -> Optional[str]:
    norm = item.norm
    if not norm or len(norm.split()) > 5:
        return None
    digits = sum(c.isdigit() for c in norm)
    if digits > 0.3 * len(norm.replace(" ", "")):
        return None
    if any(_has_word(norm, k) for k, _ in BUILT_AREA_KEYS):
        return None
    if any(_has_word(norm, w) for w in NOT_ROOM_WORDS):
        return None
    for key in ROOM_KEYWORDS:
        if _has_word(norm, key):
            return key
    return None


def enclosed_by_ellipse(gray: Optional[np.ndarray], item: TextItem) -> bool:
    """True se o texto está dentro de um círculo/elipse desenhado.

    Procura o contorno interno (buraco) que envolve o texto e mede o
    quanto ele se afasta de uma elipse perfeita: ambientes retangulares
    e símbolos quaisquer são rejeitados.
    """
    if gray is None:
        return False
    w, h = item.x2 - item.x1, item.y2 - item.y1
    margin = 1.3 * max(w, h)
    x0, y0 = int(max(item.x1 - margin, 0)), int(max(item.y1 - margin, 0))
    x1 = int(min(item.x2 + margin, gray.shape[1]))
    y1 = int(min(item.y2 + margin, gray.shape[0]))
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0 or min(crop.shape) < 8:
        return False
    ink = (crop < 170).astype(np.uint8)
    ink = cv2.dilate(ink, np.ones((2, 2), np.uint8))
    contours, hierarchy = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None:
        return False
    tx1, ty1, tx2, ty2 = item.x1 - x0, item.y1 - y0, item.x2 - x0, item.y2 - y0
    for idx, contour in enumerate(contours):
        if hierarchy[0][idx][3] < 0 or len(contour) < 20:
            continue                                   # só buracos
        bx, by, bw, bh = cv2.boundingRect(contour)
        slack = 0.15 * min(w, h)
        if not (bx <= tx1 + slack and by <= ty1 + slack
                and bx + bw >= tx2 - slack and by + bh >= ty2 - slack):
            continue                                   # não envolve o texto
        if max(bw, bh) > 3.2 * max(w, h):
            continue
        (ex, ey), (ax1, ax2), ang = cv2.fitEllipse(contour)
        if min(ax1, ax2) < 4:
            continue
        theta = math.radians(ang)
        pts = contour.reshape(-1, 2).astype(np.float64)
        dx, dy = pts[:, 0] - ex, pts[:, 1] - ey
        u = (dx * math.cos(theta) + dy * math.sin(theta)) / (ax1 / 2)
        v = (-dx * math.sin(theta) + dy * math.cos(theta)) / (ax2 / 2)
        r = np.sqrt(u * u + v * v)
        if np.mean(np.abs(r - 1)) < 0.05 and np.max(np.abs(r - 1)) < 0.15:
            return True
    return False


def split_number_sequences(items: Iterable[TextItem]) -> List[TextItem]:
    """Separa "15 503 15" (cotas lidas juntas pelo OCR) em itens próprios."""
    out: List[TextItem] = []
    for it in items:
        if it.source == "pdf" or not NUM_SEQ_RE.match(it.norm):
            out.append(it)
            continue
        n = len(it.norm)
        for m in re.finditer(r"\S+", it.norm):
            a, b = m.start() / n, m.end() / n
            if it.vertical:
                y1, y2 = it.y1 + a * (it.y2 - it.y1), it.y1 + b * (it.y2 - it.y1)
                if it.angle == 270:
                    y1, y2 = it.y2 - b * (it.y2 - it.y1), it.y2 - a * (it.y2 - it.y1)
                part = TextItem(m.group(), it.x1, y1, it.x2, y2, it.angle, it.source)
            else:
                x1, x2 = it.x1 + a * (it.x2 - it.x1), it.x1 + b * (it.x2 - it.x1)
                if it.angle == 180:
                    x1, x2 = it.x2 - b * (it.x2 - it.x1), it.x2 - a * (it.x2 - it.x1)
                part = TextItem(m.group(), x1, it.y1, x2, it.y2, it.angle, it.source)
            out.append(part)
    return out


# Pedaços de um rótulo de área que o PDF do CAD pode gravar soltos e fora de
# ordem: "A:", "9", ",", "90", "m", "2" (o "²" sobrescrito vira outro texto,
# às vezes sem direção definida).
_FRAG_PREFIX_RE = re.compile(r"^(?:A|AREA|AREA UTIL)\s*[=:]$")
_FRAG_NUMBER_RE = re.compile(r"^[\d.,]+$")
_FRAG_UNIT_RE = re.compile(r"^M2?$")
_LABEL_RE = re.compile(rf"^(?:(?P<pre>A|AREA|AREA UTIL)\s*(?P<sep>[=:])\s*)?(?P<num>{NUM})\s*M2?$")


def _fragment_kind(it: TextItem) -> Optional[str]:
    n = it.norm.replace(" ", "")
    if n == "2":
        return "two"             # unidade se vier logo depois do "m"; senão, dígito
    if _FRAG_UNIT_RE.match(n):
        return "unit"
    if it.angle is None:
        return None
    if _FRAG_PREFIX_RE.match(it.norm):
        return "prefix"
    if _FRAG_NUMBER_RE.match(n):
        return "number"
    return None


def merge_area_units(items: Iterable[TextItem]) -> List[TextItem]:
    """Remonta rótulos de área gravados em pedaços: "A:" "9" "," "90" "m" "2" → "A: 9,90 m²".

    Os pedaços são juntados pela POSIÇÃO: mesma linha de texto, encostados
    (folga de até ~1 letra), na ordem de leitura. O resultado só é aceito se
    formar exatamente [prefixo] número unidade; números sem unidade ao lado
    (cotas, células de tabela) ficam como estão.
    """
    items = list(items)
    frags = [(it, k) for it in items if (k := _fragment_kind(it))]
    if not any(k == "unit" for _, k in frags) or not any(k == "number" for _, k in frags):
        return items

    merged: List[TextItem] = []
    consumed: set = set()
    anchors = [it for it, k in frags if k == "number"]
    others = frags
    for anchor in anchors:
        if anchor.angle is None:
            continue
        if id(anchor) in consumed:
            continue
        h = anchor.char_h
        # Pedaços na mesma linha do número, ordenados ao longo da leitura.
        line = []
        for it, kind in others:
            if id(it) in consumed:
                continue
            if it.angle is not None and it.angle != anchor.angle:
                continue
            u, v = anchor.local(it.cx, it.cy)
            if abs(v) <= 0.7 * max(h, it.char_h if it.angle is not None else h) and abs(u) <= 12 * h:
                line.append((u, it, kind))
        line.sort(key=lambda p: p[0])
        # Cadeia contígua que contém o número-âncora.
        idx = next(i for i, (_, it, _) in enumerate(line) if it is anchor)

        def extent(it: TextItem) -> float:
            return (it.y2 - it.y1) if anchor.vertical else (it.x2 - it.x1)

        lo = hi = idx
        while lo > 0:
            u0, it0, _ = line[lo - 1]
            u1, it1, _ = line[lo]
            if (u1 - extent(it1) / 2) - (u0 + extent(it0) / 2) > 1.3 * h:
                break
            lo -= 1
        while hi < len(line) - 1:
            u0, it0, _ = line[hi]
            u1, it1, _ = line[hi + 1]
            if (u1 - extent(it1) / 2) - (u0 + extent(it0) / 2) > 1.3 * h:
                break
            hi += 1
        chain = line[lo:hi + 1]
        # "2" solto: expoente do "m" (unidade) ou dígito do número.
        kinds = []
        for _, _, k in chain:
            kinds.append(("unit" if kinds and kinds[-1] == "unit" else "number") if k == "two" else k)
        chain = [(u, it, k) for (u, it, _), k in zip(chain, kinds)]
        if "unit" not in kinds or "number" not in kinds:
            continue
        # Monta o texto: prefixo, dígitos colados, unidade.
        pre = "".join(it.text.strip() for _, it, k in chain if k == "prefix")
        num = "".join(it.norm.replace(" ", "") for _, it, k in chain if k == "number")
        order_ok = kinds == sorted(kinds, key=["prefix", "number", "unit"].index)
        text = (f"{pre} " if pre else "") + f"{num} m²"
        m = _LABEL_RE.match(normalize(text))
        if not order_ok or not m:
            continue
        parts = [it for _, it, _ in chain]
        new = TextItem(
            text,
            min(p.x1 for p in parts), min(p.y1 for p in parts),
            max(p.x2 for p in parts), max(p.y2 for p in parts),
            anchor.angle, "pdf" if all(p.source == "pdf" for p in parts) else "ocr",
        )
        new.confirmed = all(it.confirmed for _, it, k in chain if k == "number")
        merged.append(new)
        consumed.update(id(p) for p in parts)
    if not merged:
        return items
    return [it for it in items if id(it) not in consumed] + merged


# ══════════════════════════════════════════════════════════════
# Carimbo: área construída declarada
# ══════════════════════════════════════════════════════════════

def _match_built_key(item: TextItem) -> Optional[Tuple[str, int, str]]:
    """(chave, peso, resto do texto depois da chave) ou None.

    Texto do PDF: comparação exata. Texto do OCR: tolera letras trocadas
    ("ARE A CONSTRUR:" → "AREA A CONSTRUIR"), pois o carimbo costuma ter
    a menor letra da prancha.
    """
    norm = item.norm
    for key, weight in BUILT_AREA_KEYS:
        if key in norm:
            i = norm.index(key)
            tail = norm[i + len(key):]
            # Palavras que mudam o sentido valem perto do rótulo: logo antes dele, entre
            # ele e o número, e logo depois do número até o próximo rótulo "ÁREA" — um
            # carimbo de uma linha só ("ÁREA CONSTRUÇÃO - 64,82 m2 ÁREA ÚTIL - 53,41 m2",
            # CAR-BA) não perde a área construída por causa da útil ao lado.
            m = re.search(NUM, tail)
            near = norm[max(i - 15, 0):i] + (tail[:m.start()] if m else tail)
            if m:
                after = tail[m.end():m.end() + 25]
                near += after.split("AREA")[0]
            if any(x in near for x in BUILT_AREA_EXCLUDE):
                return None
            return key, weight, tail
    if any(x in norm for x in BUILT_AREA_EXCLUDE):
        return None
    if item.source != "ocr":
        return None
    compact = re.sub(r"[^A-Z]", "", norm)
    if not compact.startswith("AR") or len(compact) < 8:
        return None
    # A chave MAIS parecida (não a primeira que passa): "ARE A A CONSTRUIR"
    # é "ÁREA A CONSTRUIR", não "ÁREA TOTAL A CONSTRUIR".
    best: Optional[Tuple[float, str, int]] = None
    for key, weight in BUILT_AREA_KEYS:
        key_compact = key.replace(" ", "")
        ratio = SequenceMatcher(None, key_compact, compact[:len(key_compact) + 1]).ratio()
        if ratio >= 0.84 and (best is None or ratio > best[0]):
            best = (ratio, key, weight)
    if best is None:
        return None
    m = re.search(r"[:=]", norm)      # o valor, se houver, vem depois de ":" ou "="
    return best[1], best[2], norm[m.end():] if m else ""


# Legenda de planta de VENDA com a área da unidade no fim, depois de um travessão:
# "Apartamento Tipo - Final 01 - 251,57m²", "FINAL 1 – 220,71 m²", "PENTHOUSE 1 – 485,17m²",
# "Pav. Inferior DUPLEX - 525,49m²" (books de Goiânia). Sem ela, a página do book caía no
# total do empreendimento ("ÁREA CONSTRUÍDA: 71.296,16m²", pág. 11) — número certo do lugar errado.
_SALES_UNIT_RE = re.compile(
    # OCR cola tudo: "FINAL1-220,71m²*" (Ryad em imagem) — número grudado na palavra e
    # travessão sem espaços também valem.
    r"\b(?:APARTAMENTO|APTO|UNIDADE|FINAL|PENTHOUSE|COBERTURA|DUPLEX|GARDEN|STUDIO|LOFT|HOUSE)(?![A-Z])"
    rf"[^=]{{0,50}}?\s*[-–—]\s*({NUM})\s*M\s*2\b\s*\*?\s*$"
)


def sales_unit_area(items: Sequence[TextItem]) -> Optional[DeclaredArea]:
    """Área privativa da unidade na legenda da planta de venda; None se faltar ou se houver
    legendas com valores diferentes (duas unidades na mesma página)."""
    hits: List[Tuple[float, TextItem]] = []
    for it in items:
        if len(it.norm) > 90:
            continue
        m = _SALES_UNIT_RE.search(it.norm)
        value = parse_number(m.group(1)) if m else None
        if value is not None and 10 <= value <= 5000:
            hits.append((value, it))
    if len({v for v, _ in hits}) != 1:
        return None
    value, it = hits[0]
    return DeclaredArea(value, f"Área privativa (planta de venda): {it.text.strip(' *')}", it, it)


def find_declared_area(items: Sequence[TextItem]) -> Tuple[Optional[DeclaredArea], Optional[TextItem]]:
    """Procura "ÁREA A CONSTRUIR / CONSTRUÍDA / TOTAL" e o valor em m².

    O valor pode estar no mesmo texto ou numa célula vizinha do carimbo
    (à direita na mesma linha, ou logo abaixo do rótulo).
    Devolve (área declarada, rótulo achado sem valor legível).
    """
    found: List[Tuple[int, float, DeclaredArea]] = []
    keys_without_value: List[Tuple[int, TextItem]] = []

    def cell_value(it: TextItem) -> Optional[float]:
        m = VALUE_M2_RE.search(it.norm) or DECLARED_VALUE_RE.match(it.norm)
        return parse_number(m.group(1)) if m else None

    m2_items = [it for it in items if cell_value(it) is not None]

    # Quadro com cabeçalho "ÁREAS:" e linhas "TERRENO 1.100,00 m²", "CONSTRUÍDA
    # 314,94 m²" (carimbo da prefeitura de Luziânia): a linha logo abaixo do cabeçalho
    # que começa com "CONSTRUÍDA" é a área construída, mesmo sem a palavra "ÁREA".
    # Casa de Ituverava-SP: "ÁREAS (m2) / TERRENO 223,00 / CONSTRUÇÃO 117,2 / LIVRE 105,8" —
    # a unidade está no cabeçalho e o valor tem UMA casa decimal (só aceita nessa linha).
    header_keys: Dict[int, Tuple[str, int, str]] = {}
    for hd in items:
        if not re.fullmatch(r"(?:QUADRO\s+DE\s+)?AREAS?\s*(?:\(\s*M2\s*\))?\s*:?", hd.norm.strip()):
            continue
        for it in items:
            m = re.match(r"(CONSTRUIDA|EDIFICADA|A CONSTRUIR|TOTAL CONSTRUIDA|CONSTRUCAO)(?![A-Z])", it.norm.strip())
            if it is not hd and m and 0 < it.cy - hd.cy <= 8 * hd.char_h and abs(it.x1 - hd.x1) <= 4 * hd.char_h:
                header_keys[id(it)] = ("AREA " + m.group(1), 3, it.norm.strip()[m.end():])

    for key_item in items:
        hit = _match_built_key(key_item) or header_keys.get(id(key_item))
        if hit is None:
            continue
        key, weight, tail = hit

        value: Optional[float] = None
        value_item = key_item
        m = VALUE_M2_RE.search(tail) or re.search(rf"[:=\s]\s*({NUM})\s*$", tail)
        if m and re.search(r"(?<![A-Z])(?:E|COM|QUE|POSSUI|SENDO|MAIS)(?![A-Z])|[,;]|\d", tail[:m.start(1)]):
            # O número é de outra frase: "… 51,34 m² de área construída E 40,01 m² de
            # área útil" (memorial CAR-BA) — o 40,01 é da área útil.
            m = None
        if m:
            value = parse_number(m.group(1))
        elif len(key_item.text) > 60:
            pass        # rótulo no meio de um parágrafo: não há "célula vizinha" de carimbo
        else:
            best: Optional[Tuple[float, TextItem]] = None
            h = key_item.char_h
            if id(key_item) in header_keys:
                # Linha do quadro "ÁREAS (m2)": o valor na MESMA linha pode ter 1 casa ("117,2").
                for cand in items:
                    u, v = key_item.local(cand.cx, cand.cy)
                    if cand is not key_item and abs(v) <= 0.6 * h and 0 < u - key_item.length / 2 <= 30 * h \
                            and re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{1,2}|\d{1,6}[.,]\d{1,2}", cand.norm.strip()):
                        score = u - key_item.length / 2
                        if best is None or score < best[0]:
                            best = (score, cand)
                if best is not None:
                    value_item, value = best[1], parse_number(best[1].norm.strip())
                best = None if value is not None else best
            for cand in (m2_items if value is None else []):
                if cand is key_item or any(x in cand.norm for x in BUILT_AREA_EXCLUDE):
                    continue
                u, v = key_item.local(cand.cx, cand.cy)
                reach = key_item.length / 2 + cand.length / 2
                if abs(v) <= 1.0 * h and 0 < u - key_item.length / 2 <= 30 * h:
                    score = (u - key_item.length / 2) / h            # à direita
                elif 0.4 * h < v <= 8 * h and abs(u) <= reach + 2 * h:
                    score = v / h + 0.3 * abs(u) / h                 # abaixo
                else:
                    continue
                if best is None or score < best[0]:
                    best = (score, cand)
            if best is not None and value is None:
                value_item = best[1]
                # Parcelas empilhadas ("38,04 m² (apto) + 13,00 m² (terraço) = 51,04 m²"):
                # vale o total, a linha que começa com "=", logo abaixo.
                for cand in m2_items:
                    u, v = key_item.local(cand.cx, cand.cy)
                    if cand.text.lstrip().startswith("=") and 0.4 * h < v <= 8 * h \
                            and abs(u) <= key_item.length / 2 + cand.length / 2 + 2 * h:
                        value_item = cand
                        break
                value = cell_value(value_item)

        if value is not None and 10 <= value <= 1_000_000:
            label = key_item.text.strip(" :") if key_item.source == "pdf" else key.replace("AREA", "ÁREA")
            found.append((weight, value, DeclaredArea(value, label, key_item, value_item)))
        else:
            keys_without_value.append((weight, key_item))

    if not found:
        best_key = max(keys_without_value, key=lambda t: t[0])[1] if keys_without_value else None
        return None, best_key
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return found[0][2], None


# Pavimento repetido: "PLANTA TIPO (x12)", "PAVIMENTO TIPO 12X", "TIPO × 4".
_MULT_RE = re.compile(r"\(\s*[X×]\s*(\d{1,2})\s*\)|(?<![A-Z0-9])[X×]\s?(\d{1,2})(?![0-9])|(?<![0-9,.])(\d{1,2})\s?[X×](?![A-Z])")
_FLOOR_TITLE_WORDS: Tuple[str, ...] = ("TIPO", "PAVIMENTO", "PAVIMENTOS", "ANDAR", "ANDARES")


def floor_multiplier(items: Sequence[TextItem]) -> Optional[Tuple[int, str]]:
    """Quantas vezes o pavimento da planta se repete, se o título disser ("(x12)" → 12)."""
    for it in items:
        norm = it.norm
        if not any(_has_word(norm, w) for w in _FLOOR_TITLE_WORDS) and not re.search(r"\(\s*[X×]\s*\d", norm):
            continue
        m = _MULT_RE.search(norm)
        if m:
            k = int(next(g for g in m.groups() if g))
            if 2 <= k <= 60:
                return k, it.text.strip()
    return None


# ══════════════════════════════════════════════════════════════
# Ambientes
# ══════════════════════════════════════════════════════════════

def _merge_name_fragments(name_item: TextItem, items: Sequence[TextItem], used: set) -> str:
    """'BANHEIRO' + 'MASCULINO' (linha de baixo) → 'BANHEIRO MASCULINO'."""
    parts = [(0.0, name_item.text.strip())]
    h = name_item.char_h
    for other in items:
        if other is name_item or id(other) in used or other.angle != name_item.angle:
            continue
        if not other.norm or any(c.isdigit() for c in other.norm) or _FRAG_PREFIX_RE.match(other.norm):
            continue
        if _is_room_name(other) or any(_has_word(other.norm, w) for w in NOT_ROOM_WORDS):
            continue
        if len(other.norm.split()) > 3:
            continue
        u, v = name_item.local(other.cx, other.cy)
        if 0.5 * h <= abs(v) <= 2.2 * h and abs(u) <= (name_item.length + other.length) / 2:
            parts.append((v, other.text.strip()))
            used.add(id(other))
    parts.sort(key=lambda p: p[0])
    return " ".join(p[1] for p in parts)


def _tag_name_above(label: TextItem, items: Sequence[TextItem], skip: set) -> Optional[TextItem]:
    """Texto logo acima de um rótulo de área (etiqueta de ambiente do CAD), ou None.

    Aceita só texto curto com letras (sem números dominantes, sem palavras de
    mobiliário/notas, sem "ÁREA"): numa tabela de esquadrias o que fica acima de
    "1,44 m²" é outro número ou o cabeçalho, e nada é pareado.
    """
    h = label.char_h
    best: Optional[Tuple[float, TextItem]] = None
    for it in items:
        if id(it) in skip or it is label or it.angle != label.angle or not it.norm:
            continue
        letters = sum(c.isalpha() for c in it.norm)
        if letters < 3 or len(it.norm.split()) > 6:
            continue
        if sum(c.isdigit() for c in it.norm) > 0.3 * len(it.norm.replace(" ", "")):
            continue
        if "AREA" in it.norm or "M2" in it.norm.replace(" ", "") or "(M" in it.norm:
            continue
        # Medidas e campos de quadro ("45 metros x 65", "TAXA: 52%") não são nomes.
        if re.search(r"[:=]|\bMETROS?\b|\d\s*X\s*\d|\bX\s*\d|\d\s*X\b", it.norm):
            continue
        if any(_has_word(it.norm, w) for w in NOT_ROOM_WORDS + TABLE_STOP_WORDS):
            continue
        u, v = label.local(it.cx, it.cy)
        if -3.0 * max(h, it.char_h) <= v <= -0.3 * h and abs(u) <= (label.length + it.length) / 2 + h:
            if best is None or -v < best[0]:
                best = (-v, it)
    return best[1] if best else None


# Textos que marcam um rótulo de área como TOTAL de um conjunto (não um ambiente).
AGGREGATE_WORDS: Tuple[str, ...] = (
    "UNIDADE", "APARTAMENTO", "APTO", "BLOCO", "TORRE", "PAVIMENTO", "TOTAL", "SUBTOTAL",
)


def _is_aggregate_label(label: TextItem, items: Sequence[TextItem]) -> bool:
    """Rótulo de área logo abaixo (ou ao lado) de "UNIDADE 02", "BLOCO A", "TOTAL"..."""
    if any(_has_word(label.norm, w) for w in AGGREGATE_WORDS):
        return True
    h = label.char_h
    for it in items:
        if it is label or it.angle != label.angle:
            continue
        if not any(_has_word(it.norm, w) for w in AGGREGATE_WORDS):
            continue
        u, v = label.local(it.cx, it.cy)
        # Cabeçalho EM CIMA do rótulo e alinhado com ele (quadrinho "UNIDADE 02 / A: 81,41"),
        # não um texto ao lado — a área social vizinha do quadrinho continua sendo ambiente.
        if -4.5 * h <= v <= -0.3 * h and abs(u) <= (label.length + it.length) / 2 + 0.5 * h \
                and not _room_name_between(label, v, items):
            return True
    return False


def _room_name_between(label: TextItem, head_v: float, items: Sequence[TextItem]) -> bool:
    """Nome de ambiente entre o cabeçalho (a ``head_v`` acima) e o rótulo.

    TCC, planta tipo: "UNIDADE 02 / A: 81.41 m²" e, logo abaixo, "HALL / A=7.50 m²".
    O 7,50 é do hall — só o 81,41 é a área da unidade.
    """
    for o in items:
        if o is label or o.angle != label.angle or not _is_room_name(o):
            continue
        u, v = label.local(o.cx, o.cy)
        if head_v < v < 0 and abs(u) <= (label.length + o.length) / 2 + 0.5 * label.char_h:
            return True
    return False


def aggregate_areas(items: Sequence[TextItem]) -> List[Tuple[str, float]]:
    """Áreas de unidade/bloco impressas na planta ("UNIDADE 02" → 81,41), para informar."""
    out: List[Tuple[str, float]] = []
    for it in items:
        m = AREA_EXPLICIT_RE.search(it.norm) or VALUE_M2_RE.search(it.norm)
        if not m or not _is_aggregate_label(it, items):
            continue
        value = parse_number(m.group(1))
        if not value or not 0.5 <= value <= 200_000:
            continue
        h = it.char_h
        heads = [o for o in items if o is not it and o.angle == it.angle
                 and any(_has_word(o.norm, w) for w in AGGREGATE_WORDS)
                 and -4.5 * h <= it.local(o.cx, o.cy)[1] <= 0.5 * h]
        name = min(heads, key=lambda o: abs(it.local(o.cx, o.cy)[1])).text.strip() if heads else it.text.strip()
        out.append((title_pt(name), value))
    return out


def find_rooms(
    items: Sequence[TextItem],
    gray: Optional[np.ndarray],
    exclude: Iterable[TextItem] = (),
) -> List[Room]:
    """Ambientes: nomes + rótulos de área associados por proximidade."""
    excluded = {id(it) for it in exclude}

    # 1. Rótulos de área.
    labels: List[Tuple[TextItem, float, str]] = []      # (item, valor, origem)
    raw_number: Dict[int, str] = {}
    for it in items:
        if id(it) in excluded:
            continue
        m = AREA_EXPLICIT_RE.search(it.norm)
        if m:
            # "A=2.67m" lido pelo OCR: "m" sozinho é unidade de comprimento — no TCC era
            # o fim de "PD laje=2.675m" (pé-direito), logo abaixo da área do ambiente.
            # Como o "A=7.50" sem unidade, só vale com nome; solto, não vira ambiente.
            bare_m = not it.reliable and re.fullmatch(r"M{1,2}", it.norm[m.end(1):].strip())
            labels.append((it, parse_number(m.group(1)) or 0.0, "sem_unidade" if bare_m else "rotulo"))
            raw_number[id(it)] = m.group(1)
            continue
        m = VALUE_M2_RE.search(it.norm)
        if m and not any(k in it.norm for k, _ in BUILT_AREA_KEYS):
            labels.append((it, parse_number(m.group(1)) or 0.0, "m2"))
            raw_number[id(it)] = m.group(1)
            continue
        m = AREA_NO_UNIT_RE.match(it.norm.replace(" ", ""))
        if m:
            labels.append((it, parse_number(m.group(1)) or 0.0, "sem_unidade"))
            raw_number[id(it)] = m.group(1)
            continue
        m = BARE_DECIMAL_RE.match(it.norm.replace(" ", ""))
        if m and it.angle is not None and enclosed_by_ellipse(gray, it):
            labels.append((it, parse_number(m.group(1)) or 0.0, "circulo"))
            raw_number[id(it)] = m.group(1)
    labels = [(it, v, s) for it, v, s in labels if 0.5 <= v <= 5000]
    # Folha em que os rótulos têm casas decimais ("3.00", "7,32"): um inteiro solto
    # ("A=300 m1") é um separador perdido na leitura — fica ilegível, não vira 300 m².
    illegible: List[TextItem] = []
    with_decimals = [it for it, _, _ in labels if re.search(r"[.,]\d", raw_number.get(id(it), ""))]
    if len(labels) >= 5 and len(with_decimals) >= 0.7 * len(labels):
        # Só para o OCR: no texto exato do PDF um inteiro ("159 m²") é o que está escrito.
        keep = {id(it) for it in with_decimals} | {id(it) for it, _, _ in labels if it.reliable}
        illegible = [it for it, _, _ in labels if id(it) not in keep]
        labels = [(it, v, s) for it, v, s in labels if id(it) in keep]
    # Área de UNIDADE/BLOCO/PAVIMENTO ("UNIDADE 02 / A: 81,41 m²") é a soma de vários
    # ambientes: somá-la junto com eles contaria a mesma área duas vezes.
    labels = [(it, v, s) for it, v, s in labels if not _is_aggregate_label(it, items)]

    # 2. Nomes de ambientes (com complementos da linha de baixo).
    used_fragments: set = set()
    names: List[Tuple[TextItem, str, str]] = []
    for it in items:
        if id(it) in excluded:
            continue
        key = _is_room_name(it)
        if key:
            names.append((it, key, ""))
    names = [
        (it, key, _merge_name_fragments(it, items, used_fragments))
        for it, key in ((n[0], n[1]) for n in names)
    ]

    # 3. Associação rótulo ↔ nome: pares mais próximos primeiro.
    pairs: List[Tuple[float, int, int]] = []
    for li, (lab, _, _) in enumerate(labels):
        radius = 10 * lab.char_h
        for ni, (nm, _, _) in enumerate(names):
            if nm is lab:
                continue
            d = math.hypot(lab.cx - nm.cx, lab.cy - nm.cy)
            if d <= radius:
                pairs.append((d, li, ni))
    pairs.sort()
    label_to_name: Dict[int, int] = {}
    taken: set = set()
    for _, li, ni in pairs:
        if li in label_to_name or ni in taken:
            continue
        label_to_name[li] = ni
        taken.add(ni)

    # 4. Rótulo sem nome conhecido: etiqueta do CAD ("INFANTIL 1" / "35,63 m²") —
    #    o nome é o texto logo acima do valor, mesmo fora da lista de palavras.
    used_names = {id(names[ni][0]) for ni in label_to_name.values()}
    tag_names: Dict[int, Tuple[TextItem, str]] = {}
    for li, (lab, _, _) in enumerate(labels):
        if li in label_to_name:
            continue
        tag = _tag_name_above(lab, items, excluded | used_names | {id(l[0]) for l in labels})
        if tag is not None:
            used_names.add(id(tag))
            tag_names[li] = (tag, _merge_name_fragments(tag, items, used_fragments))

    rooms: List[Room] = []
    unnamed = 0
    for li, (lab, value, source) in enumerate(labels):
        ni = label_to_name.get(li)
        if ni is None and li in tag_names:
            tag, full = tag_names[li]
            rooms.append(Room(
                name=title_pt(full), area_m2=value, external=_is_external(full), item=tag, area_item=lab,
                area_source="circulo" if source == "circulo" else "rotulo",
            ))
            continue
        if ni is None:
            # Valor "12,50 m²" solto, sem nome nem círculo: pode ser tabela
            # de esquadrias ("1,44 m²"); não entra. "A=7.50" sem unidade, só com nome.
            if source in ("m2", "sem_unidade"):
                continue
            unnamed += 1
            name, external = f"Ambiente sem nome {unnamed}", False
            item = lab
        else:
            item, key, full = names[ni]
            name, external = full, _is_external(full)
        rooms.append(Room(
            name=title_pt(name),
            area_m2=value, external=external, item=item, area_item=lab,
            area_source="circulo" if source == "circulo" else "rotulo",
        ))

    for ni, (nm, key, full) in enumerate(names):
        if ni in taken:
            continue
        rooms.append(Room(
            name=title_pt(full), area_m2=None,
            external=_is_external(full), item=nm,
        ))
    out = RoomList(rooms)
    out.illegible = illegible
    return out


class RoomList(list):
    """Ambientes; ``illegible`` = rótulos de área descartados por leitura duvidosa."""
    illegible: List[TextItem] = []


# ══════════════════════════════════════════════════════════════
# Quadro de áreas (tabela "Ambiente | Área")
# ══════════════════════════════════════════════════════════════

# Valor de uma célula do quadro: "15,41", "2,595" (o "⁵" sobrescrito vira 5),
# "1.606,41" (milhar).
# O "⁵" sobrescrito às vezes sai do OCR como "º"/"°" ("2,59º"): fica de fora
# (erro máximo de 0,009 m²) em vez de virar um dígito inventado.
# Visto no relatório real: "42,15'5", "10,80s", "23,78%", "2,59º", "50,00€".
TABLE_VALUE_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{3})+,\d{2,3}|\d{1,5}[.,]\d{2,3})(?:[O°º*'\"S%€§$’‘´`]{1,2}5?)?(?:\s*M2?)?$"
)
# Linhas que são PAVIMENTOS, não ambientes ("1º PAVIMENTO", "PAV. TÉCNICO").
FLOOR_WORDS = ("PAVIMENTO", "PAV.", "PAV ", "TERREO", "SUBSOLO", "MEZANINO", "COBERTURA", "ANDAR", "PISO ")


# Numa tabela de áreas, "(Cobertura)" ou "Entrada" são linhas válidas; só
# ficam de fora os quadros de louças/esquadrias e os cabeçalhos.
TABLE_STOP_WORDS: Tuple[str, ...] = (
    "VASO", "LAVATORIO", "LAVATORIOS", "CHUVEIRO", "TORNEIRA", "BACIA", "DUCHA", "BARRA",
    "ESPELHO", "SABONETEIRA", "DISPENSER", "CUBA", "QUADRO", "AMBIENTE", "PORTA", "PORTAS",
    "JANELA", "JANELAS", "CAIXILHO", "ESQUADRIA",
)


def _is_floor_row(norm: str) -> bool:
    if any(w in f"{norm} " for w in FLOOR_WORDS):
        return True
    # OCR troca letras ("PAVIDENTO", "PAIVIMENTO"): tolera, só para esta palavra.
    return any(tok.startswith("PAV") or SequenceMatcher(None, tok, "PAVIMENTO").ratio() >= 0.75
               for tok in re.findall(r"[A-Z]{5,}", norm))


_NAME_VALUE_RE = re.compile(r"^(.*[A-Z].*?)\s+(\d{1,3}(?:\.\d{3})+,\d{2,3}|\d{1,5}[.,]\d{2,3})$")


def _split_name_value(items: Iterable[TextItem]) -> List[TextItem]:
    """"Raio X 14,405" (o OCR juntou as duas colunas) → "Raio X" + "14,405"."""
    out: List[TextItem] = []
    for it in items:
        m = _NAME_VALUE_RE.match(it.norm) if it.angle in (0, 90) else None
        if not m:
            out.append(it)
            continue
        cut = m.start(2) / max(len(it.norm), 1)
        raw = it.text.strip()
        name_text = raw[:len(raw) - len(m.group(2))].strip() or m.group(1)
        if it.vertical:
            mid = it.y1 + cut * (it.y2 - it.y1)
            name = TextItem(name_text, it.x1, it.y1, it.x2, mid, it.angle, it.source)
            value = TextItem(m.group(2), it.x1, mid, it.x2, it.y2, it.angle, it.source)
        else:
            mid = it.x1 + cut * (it.x2 - it.x1)
            name = TextItem(name_text, it.x1, it.y1, mid, it.y2, it.angle, it.source)
            value = TextItem(m.group(2), mid, it.y1, it.x2, it.y2, it.angle, it.source)
        out += [name, value]
    return out


def floors_total(items: Sequence[TextItem]) -> Optional[float]:
    """Soma de um quadro de áreas POR PAVIMENTO ("1º PAVIMENTO | 1.606,41"),
    usada para conferir a área total do carimbo pela aritmética."""
    items = _split_name_value(items)
    values = [it for it in items if it.angle is not None and TABLE_VALUE_RE.match(it.norm.replace(" ", ""))]
    total, rows, used = 0.0, 0, set()
    for name in items:
        if name.angle is None or not _is_floor_row(name.norm):
            continue
        h = name.char_h
        best = None
        for val in values:
            if id(val) in used or val.angle != name.angle:
                continue
            u, v = name.local(val.cx, val.cy)
            gap = u - name.length / 2
            if abs(v) <= 0.7 * h and 0 < gap <= 30 * h and (best is None or gap < best[0]):
                best = (gap, val)
        if best:
            used.add(id(best[1]))
            value = parse_number(TABLE_VALUE_RE.match(best[1].norm.replace(" ", "")).group(1))
            if value:
                total += value
                rows += 1
    return total if rows >= 2 else None


# Fração mínima de linhas com nome de ambiente conhecido (sem cabeçalho "Ambiente").
TABLE_MIN_ROOM_SHARE = 0.4
TABLE_HEADER_WORDS = ("AMBIENTE", "AMBIENTES", "COMPARTIMENTO", "COMPARTIMENTOS", "DEPENDENCIA",
                      "DEPENDENCIAS", "COMODO", "COMODOS", "LOCAL", "ESPACO", "ESPACOS")


def _has_room_header(rows: Sequence[Tuple[TextItem, TextItem]], items: Sequence[TextItem]) -> bool:
    """Cabeçalho "Ambiente" (ou equivalente) logo acima da coluna de nomes."""
    ref = rows[0][0]
    top = min(rows, key=lambda r: ref.local(r[0].cx, r[0].cy)[1])[0]
    h = top.char_h
    for it in items:
        if it.angle != top.angle or it.norm not in TABLE_HEADER_WORDS:
            continue
        u, v = top.local(it.cx, it.cy)
        if -6 * h <= v <= -0.3 * h and abs(u) <= top.length / 2 + 40 * h:
            return True
    return False


def find_area_table(items: Sequence[TextItem], exclude: Iterable[TextItem] = ()) -> List[Room]:
    """Linhas de um quadro de áreas: texto do ambiente + número na mesma linha.

    Só vale como quadro se pelo menos 5 linhas tiverem os números alinhados
    na mesma coluna — o que descarta textos soltos com um número ao lado.
    """
    excluded = {id(it) for it in exclude}
    items = _split_name_value(it for it in items if id(it) not in excluded)
    values = [it for it in items if it.angle is not None
              and TABLE_VALUE_RE.match(it.norm.replace(" ", ""))]
    # Todos os pares (nome à esquerda de um valor, na mesma linha) e depois os
    # mais próximos primeiro: um rótulo da planta não "rouba" o valor da linha.
    candidates: List[Tuple[float, int, int]] = []
    names = [n for n in items if n.angle is not None]
    for ni, name in enumerate(names):
        letters = sum(c.isalpha() for c in name.norm)
        if letters < 3 or sum(c.isdigit() for c in name.norm) > 0.3 * len(name.norm):
            continue
        if any(_has_word(name.norm, w) for w in TABLE_STOP_WORDS):
            continue
        h = name.char_h
        for vi, val in enumerate(values):
            if val.angle != name.angle or val is name:
                continue
            u, v = name.local(val.cx, val.cy)
            gap = u - name.length / 2
            if abs(v) <= 0.7 * h and 0 < gap <= 45 * h:
                candidates.append((gap / h, ni, vi))
    candidates.sort()
    pairs: List[Tuple[TextItem, TextItem]] = []
    used_n: set = set()
    used_v: set = set()
    for _, ni, vi in candidates:
        if ni in used_n or vi in used_v:
            continue
        used_n.add(ni)
        used_v.add(vi)
        pairs.append((names[ni], values[vi]))
    if len(pairs) < 5:
        return []
    # Coluna de valores: agrupa pela posição perpendicular à linha (x, se o texto é horizontal).
    def column_key(val: TextItem) -> float:
        return val.cy if val.vertical else val.x2
    pairs.sort(key=lambda p: column_key(p[1]))
    groups: List[List[Tuple[TextItem, TextItem]]] = []
    for pair in pairs:
        if groups and abs(column_key(pair[1]) - column_key(groups[-1][-1][1])) <= 3 * pair[1].char_h:
            groups[-1].append(pair)
        else:
            groups.append([pair])
    best_group = max(groups, key=len)
    if len(best_group) < 5:
        return []
    # Num quadro os nomes começam todos na mesma coluna: rótulos da planta
    # pareados por acaso com um valor da tabela ficam de fora.
    def name_start(n: TextItem) -> float:
        return {0: n.x1, 90: n.y1, 180: -n.x2, 270: -n.y2}.get(n.angle or 0, n.x1)
    starts = sorted(name_start(n) for n, _ in best_group)
    col = starts[len(starts) // 2]
    h_med = float(np.median([n.char_h for n, _ in best_group]))
    best_group = [(n, v) for n, v in best_group if abs(name_start(n) - col) <= 2.5 * h_med]
    if len(best_group) < 5:
        return []
    # Quadro de pavimentos (1º PAV., 2º PAV...): não é lista de ambientes.
    if sum(_is_floor_row(n.norm) for n, _ in best_group) >= len(best_group) / 2:
        return []
    # Tem de ser um quadro de AMBIENTES: boa parte das linhas são ambientes
    # conhecidos, ou há o cabeçalho "Ambiente" acima dos nomes. Uma tabela de
    # esquadrias ("Com Veneziana | 2,56") também tem nome + número alinhados.
    room_rows = sum(1 for n, _ in best_group if _is_room_name(n))
    if room_rows < TABLE_MIN_ROOM_SHARE * len(best_group) and not _has_room_header(best_group, items):
        return []
    rows, orphans = _rebuild_table_rows(best_group, items, values)
    rooms = AreaTable()
    for name, val in rows:
        value = parse_number(TABLE_VALUE_RE.match(val.norm.replace(" ", "")).group(1))
        if value is None or not 0.3 <= value <= 20000:
            continue
        rooms.append(Room(
            name=title_pt(name.text.strip()), area_m2=value,
            external=_is_external(name.text),
            item=name, area_item=val, area_source="quadro",
        ))
    rooms.orphans = orphans
    return rooms if len(rooms) >= 5 else AreaTable()


class AreaTable(list):
    """Linhas do quadro de áreas; ``orphans`` = valores da coluna sem nome identificado."""
    orphans: int = 0


def _rebuild_table_rows(
    group: List[Tuple[TextItem, TextItem]], items: Sequence[TextItem], values: Sequence[TextItem],
) -> Tuple[List[Tuple[TextItem, TextItem]], int]:
    """Remonta cada linha do quadro: TODOS os textos entre a coluna de nomes e o valor.

    O PDF pode gravar o nome em pedaços fora de ordem ("Adm." "e" "Laudo do Raio X");
    o pareamento pelo pedaço mais próximo perderia a linha. Aqui, para cada valor da
    coluna (dentro da altura do quadro), o nome é a junção, da esquerda para a direita,
    dos textos daquela linha que começam na coluna de nomes. Valores da coluna sem
    nenhum texto na linha contam como órfãos (o quadro pode estar incompleto).
    """
    ref = group[0][0]
    h = float(np.median([n.char_h for n, _ in group]))

    def uv(it: TextItem) -> Tuple[float, float]:
        return ref.local(it.cx, it.cy)

    def start(it: TextItem) -> float:
        return uv(it)[0] - it.length / 2

    def end(it: TextItem) -> float:
        return uv(it)[0] + it.length / 2

    col_u = float(np.median([start(n) for n, _ in group]))
    val_end = float(np.median([end(v) for _, v in group]))
    val_start = float(np.median([start(v) for _, v in group]))
    row_v = sorted(uv(v)[1] for _, v in group)
    pitch = float(np.median(np.diff(row_v))) if len(row_v) > 1 else 2 * h
    v_lo, v_hi = row_v[0] - 1.5 * pitch, row_v[-1] + 1.5 * pitch

    # Coluna = os valores já pareados (validados acima) + outros alinhados com eles
    # (linhas cujo nome veio em pedaços). As caixas do OCR variam: a tolerância é larga.
    paired = {id(v): n for n, v in group}
    column = [v for v in values if id(v) in paired or (
        v.angle == ref.angle and v_lo <= uv(v)[1] <= v_hi
        and (abs(end(v) - val_end) <= 3 * h or abs(start(v) - val_start) <= 3 * h))]
    texts = [it for it in items if it.angle == ref.angle and sum(c.isalpha() for c in it.norm) >= 1
             and not TABLE_VALUE_RE.match(it.norm.replace(" ", ""))]
    rows: List[Tuple[TextItem, TextItem]] = []
    orphans = 0
    for val in column:
        vu, vv = uv(val)        # (ao longo da linha, perpendicular)
        # Pedaços do nome: na linha do valor, da coluna de nomes até antes do centro do
        # valor (a caixa do nome pode encostar na do número, como no pareamento).
        parts = [it for it in texts
                 if abs(uv(it)[1] - vv) <= 0.7 * h and start(it) >= col_u - 2.5 * h and end(it) <= vu]
        if id(val) in paired and paired[id(val)] not in parts:
            parts.append(paired[id(val)])
        if not parts:
            orphans += 1
            continue
        parts.sort(key=start)
        name_text = " ".join(p.text.strip() for p in parts)
        if any(_has_word(normalize(name_text), w) for w in TABLE_STOP_WORDS):
            continue
        name = TextItem(name_text, min(p.x1 for p in parts), min(p.y1 for p in parts),
                        max(p.x2 for p in parts), max(p.y2 for p in parts), ref.angle,
                        "pdf" if all(p.source == "pdf" for p in parts) else "ocr")
        rows.append((name, val))
    return rows, orphans


# ══════════════════════════════════════════════════════════════
# Cotas: cadeias e envoltória
# ══════════════════════════════════════════════════════════════

def _cota_value(item: TextItem) -> Optional[Tuple[float, str]]:
    n = item.norm.replace(" ", "")
    m = BARE_INT_RE.match(n)
    if m:
        return float(m.group(1)), "cm"
    m = BARE_DECIMAL_RE.match(n)
    if m:
        return parse_number(m.group(1)), "m"
    return None


def _rows(items: List[Tuple[TextItem, float]], vertical: bool) -> List[List[Tuple[TextItem, float]]]:
    """Agrupa cotas alinhadas na mesma linha de cota."""
    key = (lambda it: it.cx) if vertical else (lambda it: it.cy)
    pos = (lambda it: it.cy) if vertical else (lambda it: it.cx)
    ordered = sorted(items, key=lambda p: key(p[0]))
    rows: List[List[Tuple[TextItem, float]]] = []
    for it, v in ordered:
        if rows and abs(key(it) - key(rows[-1][-1][0])) <= 0.6 * it.char_h:
            rows[-1].append((it, v))
        else:
            rows.append([(it, v)])
    return [sorted(r, key=lambda p: pos(p[0])) for r in rows]


def _fit_chain(centers: np.ndarray, values: np.ndarray) -> Tuple[float, float, float]:
    """centro_i = b0 + k·(Σv_anteriores + v_i/2) → (b0, k, resíduo máximo)."""
    p = np.cumsum(values) - values / 2
    a = np.vstack([np.ones_like(p), p]).T
    (b0, k), *_ = np.linalg.lstsq(a, centers, rcond=None)
    res = centers - (b0 + k * p)
    return float(b0), float(k), float(np.abs(res).max())


def _tightest_cover(
    extents: Dict[int, List[float]], coords: Sequence[float], trim: bool,
    counts: Optional[Dict[int, int]] = None,
) -> Optional[Tuple[float, float]]:
    """Início e fim da linha de cotas mais curta que cobre os ambientes.

    ``trim=False`` (rótulos de área, que ficam dentro dos ambientes): a linha
    tem de cobrir todos — uma linha parcial (um só bloco) não passa.
    ``trim=True`` (só nomes, que podem incluir texto do carimbo): cobre a faixa
    de 10% a 90% das posições.
    """
    if len(coords) < 3 or not extents:
        return None
    c = np.asarray(coords, dtype=float)
    if trim:
        lo, hi = float(np.percentile(c, 10)), float(np.percentile(c, 90))
    else:
        lo, hi = float(c.min()), float(c.max())
    tol = 0.02 * (hi - lo)
    covering = [(row, (e[0], e[1])) for row, e in extents.items() if e[0] <= lo + tol and e[1] >= hi - tol]
    if not covering:
        return None
    # Das linhas do PRÉDIO, a externa: a cadeia interna (entre faces internas, ex.
    # 300 + 15 + 300) é 5% a 10% mais curta que a externa (645). A do terreno, bem
    # mais longa, continua de fora. Melhor prova da face externa: a COTA TOTAL, um
    # valor sozinho na linha ("645"); sem ela, a mais longa até 12% acima da mais curta.
    shortest = min(e[1] - e[0] for _, e in covering)
    totals = [e for row, e in covering if counts and counts.get(row) == 1 and e[1] - e[0] <= 1.2 * shortest]
    pool = totals or [e for _, e in covering if e[1] - e[0] <= 1.12 * shortest]
    return max(pool, key=lambda e: e[1] - e[0])


def find_envelope(
    items: Sequence[TextItem],
    exclude: Iterable[TextItem] = (),
    focus: Sequence[Tuple[float, float]] = (),
    focus_trim: bool = False,
) -> Tuple[Optional[Envelope], List[TextItem]]:
    """Largura × profundidade pelas cotas.

    Cadeias de cotas ("15 | 503 | 15 | 1248 | 15") são conferidas pela
    posição dos números: numa cadeia verdadeira o centro de cada número
    cai no meio do seu trecho. Isso dá a escala do desenho (px por cm)
    e descarta números que não são cotas.

    ``focus``: posições dos ambientes (nomes e rótulos). Com elas, a largura e a
    profundidade vêm da linha de cotas MAIS CURTA que cobre os ambientes — a do
    prédio —, e não da união de todas as cotas da folha (que pode incluir o
    terreno ou desenhos de detalhe ao lado).
    """
    excluded = {id(it) for it in exclude}
    by_unit: Dict[str, List[Tuple[TextItem, float]]] = {"cm": [], "m": []}
    for it in items:
        if id(it) in excluded or it.angle is None:
            continue
        cv = _cota_value(it)
        if cv and cv[0] and cv[0] > 0:
            by_unit[cv[1]].append((it, cv[0]))

    best: Optional[Envelope] = None
    best_items: List[TextItem] = []
    for unit, cands in by_unit.items():
        if len(cands) < 3:
            continue
        horiz = [c for c in cands if not c[0].vertical]
        vert = [c for c in cands if c[0].vertical]
        rows = _rows(horiz, False) + _rows(vert, True)

        # 1ª passada: linhas com ≥3 cotas coerentes definem a escala.
        ks: List[float] = []
        for row in rows:
            if len(row) < 3:
                continue
            c = np.array([(it.cy if it.vertical else it.cx) for it, _ in row])
            v = np.array([val for _, val in row])
            _, k, res = _fit_chain(c, v)
            tol = 1.5 * float(np.median([it.char_h for it, _ in row]))
            if k > 0 and res <= tol:
                ks.append(k)
        if not ks:
            continue
        k_ref = float(np.median(ks))

        # 2ª passada: quebra cada linha onde a distância não bate com a escala.
        spans_h: List[Tuple[float, float]] = []
        spans_v: List[Tuple[float, float]] = []
        row_ext_h: Dict[int, List[float]] = {}     # linha de cotas → [início, fim]
        row_ext_v: Dict[int, List[float]] = {}
        row_n: Dict[int, int] = {}                  # linha → quantas cotas (1 = cota total)
        used: List[TextItem] = []
        chains = 0
        for row_id, row in enumerate(rows):
            segment: List[Tuple[TextItem, float]] = []
            segments: List[List[Tuple[TextItem, float]]] = []
            for it, val in row:
                if segment:
                    prev_it, prev_v = segment[-1]
                    pos_prev = prev_it.cy if prev_it.vertical else prev_it.cx
                    pos_cur = it.cy if it.vertical else it.cx
                    expected = (prev_v + val) / 2 * k_ref
                    if abs((pos_cur - pos_prev) - expected) > 0.25 * expected + 1.5 * it.char_h:
                        segments.append(segment)
                        segment = []
                segment.append((it, val))
            if segment:
                segments.append(segment)
            for seg in segments:
                vertical = seg[0][0].vertical
                c = np.array([(it.cy if vertical else it.cx) for it, _ in seg])
                v = np.array([val for _, val in seg])
                p = np.cumsum(v) - v / 2
                b0 = float(np.mean(c - k_ref * p))
                span = (b0, b0 + k_ref * float(v.sum()))
                if len(seg) >= 2:
                    chains += 1
                elif v[0] < 0.25 * max(val for _, val in cands):
                    continue      # cota avulsa pequena: interna, não muda a envoltória
                elif not seg[0][0].reliable:
                    continue      # cota avulsa do OCR: sem cadeia para conferir o número
                (spans_v if vertical else spans_h).append(span)
                ext = (row_ext_v if vertical else row_ext_h).setdefault(row_id, [span[0], span[1]])
                ext[0], ext[1] = min(ext[0], span[0]), max(ext[1], span[1])
                row_n[row_id] = row_n.get(row_id, 0) + len(seg)
                used.extend(it for it, _ in seg)
        if not spans_h or not spans_v or chains < 2:
            continue
        factor = 0.01 if unit == "cm" else 1.0
        xr = (min(s[0] for s in spans_h), max(s[1] for s in spans_h))
        yr = (min(s[0] for s in spans_v), max(s[1] for s in spans_v))
        tight_w = _tightest_cover(row_ext_h, [p[0] for p in focus], focus_trim, row_n)
        tight_d = _tightest_cover(row_ext_v, [p[1] for p in focus], focus_trim, row_n)
        if tight_w is not None and tight_d is not None:
            xr, yr = tight_w, tight_d
        largura = (xr[1] - xr[0]) / k_ref * factor
        profundidade = (yr[1] - yr[0]) / k_ref * factor
        if not (2 <= largura <= 300 and 2 <= profundidade <= 300):
            continue
        if max(largura, profundidade) > 10 * min(largura, profundidade):
            continue      # faixa estreita (ex.: 145 × 3,9 m numa implantação): não é um prédio
        env = Envelope(largura, profundidade, unit, chains, k_ref,
                       reliable=all(it.reliable for it in used), box=(xr[0], yr[0], xr[1], yr[1]))
        if best is None or env.chains > best.chains:
            best, best_items = env, used
    return best, best_items


# ══════════════════════════════════════════════════════════════
# Consolidação
# ══════════════════════════════════════════════════════════════

def _fmt(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def interpret(items: Sequence[TextItem], gray: Optional[np.ndarray]) -> PlanReading:
    """Lê área, ambientes e cômodos e confere as fontes entre si."""
    items = merge_area_units(split_number_sequences(items))
    declared, declared_key = find_declared_area(items)
    if declared is None:
        declared = sales_unit_area(items)
    floors_sum = floors_total(items)
    exclude: List[TextItem] = []
    if declared:
        exclude += [declared.key_item, declared.value_item]

    checks: List[str] = []
    warnings: List[str] = []
    evidence: List[TextItem] = []

    # Quadro de áreas: fonte das áreas por ambiente; os rótulos espalhados na
    # planta (os mesmos ambientes) viram só conferência — sem contar em dobro.
    table = find_area_table(items, exclude)
    table_confirmed = False
    table_conflict = False      # quadro e rótulos da planta discordam (> 5%)
    rooms: List[Room] = []
    plan_rooms: List[Room] = []
    drawing_rooms: List[Room] = []
    label_items: List[TextItem] = []
    if table:
        table_items = [r.item for r in table] + [r.area_item for r in table]
        plan_rooms = find_rooms(items, gray, exclude + table_items)
        plan_with = [r for r in plan_rooms if r.area_m2 and not r.external]
        plan_sum = sum(r.area_m2 for r in plan_with)
        table_sum = sum(r.area_m2 for r in table if not r.external)
        ratio = plan_sum / table_sum if table_sum else 0.0
        if plan_sum and not 0.5 <= ratio <= 2.0 and len(plan_with) > len(table):
            # O "quadro" não é a lista de ambientes desta planta (ex.: tabela de
            # esquadrias): valem os rótulos dos ambientes.
            warnings.append(
                f"Tabela de {len(table)} linhas ({_fmt(table_sum)} m²) descartada: não confere com os "
                f"{len(plan_with)} ambientes rotulados na planta ({_fmt(plan_sum)} m²)."
            )
            table = []
        else:
            rooms = table
            # Ambientes no desenho: os nomes da planta, fora da caixa do quadro.
            pad = float(np.median([it.char_h for it in table_items]))
            tx1, ty1 = min(it.x1 for it in table_items) - pad, min(it.y1 for it in table_items) - pad
            tx2, ty2 = max(it.x2 for it in table_items) + pad, max(it.y2 for it in table_items) + pad
            drawing_rooms = [r for r in plan_rooms
                             if not (tx1 <= r.item.cx <= tx2 and ty1 <= r.item.cy <= ty2)]
            orphans = getattr(table, "orphans", 0)
            if orphans:
                warnings.append(
                    f"{orphans} valor(es) da coluna de áreas do quadro sem nome identificado: "
                    "a soma pode estar incompleta."
                )
                table_conflict = True
            if plan_sum:
                diff = abs(plan_sum - table_sum) / table_sum
                msg = (f"Quadro de áreas ({len(table)} ambientes, {_fmt(table_sum)} m²) "
                       f"× rótulos na planta ({_fmt(plan_sum)} m²): diferença de {diff:.1%}.")
                (checks if diff <= 0.05 else warnings).append(msg)
                # duas fontes independentes concordam (e o quadro está completo)
                table_confirmed = diff <= 0.03 and not orphans
                table_conflict = table_conflict or diff > 0.05
            else:
                checks.append(f"Áreas lidas do quadro de áreas ({len(table)} ambientes).")
            label_items = table_items + [r.area_item for r in plan_rooms if r.area_item is not None]
    if not table:
        rooms = find_rooms(items, gray, exclude)
        drawing_rooms = list(rooms)
        label_items = [r.area_item for r in rooms if r.area_item is not None]

    internal = [r for r in rooms if not r.external]
    with_area = [r for r in internal if r.area_m2 is not None]
    illegible = list(getattr(rooms, "illegible", []))
    if not table and illegible:
        warnings.append(
            "Rótulos de área com leitura duvidosa (inteiro numa folha com casas decimais), "
            "não somados: " + ", ".join(f"“{it.text.strip()}”" for it in illegible[:6])
            + (f" (+{len(illegible) - 6})" if len(illegible) > 6 else "") + "."
        )
    rooms_sum = sum(r.area_m2 for r in with_area) if with_area else None
    area_externa = sum(r.area_m2 for r in rooms if r.external and r.area_m2)

    # Envoltória de TODAS as cotas: serve para CONFERIR o carimbo e a soma dos
    # ambientes — nunca menor que o prédio, então não acusa uma área certa.
    envelope, cota_items = find_envelope(items, exclude + label_items)
    room_points = [(r.item.cx, r.item.cy) for r in drawing_rooms if not r.external]
    if envelope is not None and rooms_sum and rooms_sum > envelope.area_m2 * 1.15:
        # Os ambientes não cabem nas cotas lidas: as cotas cobrem só parte da obra.
        # Uma envoltória parcial não serve para acusar o carimbo nem a soma.
        envelope.reliable = False
    if envelope is not None and not envelope.covers(room_points):
        # Ambientes do lado de fora das cadeias de cotas: mesma conclusão, pela posição.
        envelope.reliable = False
    building_envelope: Optional[Envelope] = None
    if envelope is not None and len(room_points) >= 3:
        building_envelope, _ = find_envelope(items, exclude + label_items, room_points, focus_trim=True)
        if building_envelope is not None:
            building_envelope.reliable = building_envelope.reliable and envelope.reliable \
                and building_envelope.covers(room_points)
    qtd_comodos = len(internal)
    aggregates = aggregate_areas(items)
    if aggregates:
        shown = " · ".join(f"{n}: {_fmt(v)}" for n, v in aggregates[:8])
        more = f" (+{len(aggregates) - 8})" if len(aggregates) > 8 else ""
        checks.append(f"Áreas de unidade/bloco impressas na planta (não somadas aos ambientes): {shown} m²{more}.")

    def check_against_envelope(value: float, what: str) -> bool:
        if envelope is None or not envelope.reliable:
            return True     # envoltória do OCR pode ser parcial: não serve de prova
        if value <= envelope.area_m2 * 1.15:
            checks.append(
                f"{what} cabe na envoltória das cotas "
                f"({_fmt(envelope.largura_m)} m × {_fmt(envelope.profundidade_m)} m)."
            )
            return True
        warnings.append(
            f"{what} ({_fmt(value)} m²) é maior que a envoltória das cotas "
            f"({_fmt(envelope.area_m2)} m²): confira a leitura."
        )
        return False

    unit_area = bool(declared) and (
        "PRIVATIV" in normalize(declared.label)
        or any(UNIT_CONTEXT_RE.search(it.norm) for it in items))
    if declared:
        area, kind, method = declared.value, ("privativa" if unit_area else "construida"), "carimbo"
        evidence = [declared.key_item, declared.value_item]
        if unit_area:
            warnings.append(
                "A área lida é de UMA unidade (apartamento), do material de venda — não é a área "
                "do prédio inteiro. Para a obra toda, use o quadro de áreas ou o memorial."
            )
        consistent = check_against_envelope(area, "A área do carimbo")
        if rooms_sum:
            ratio = rooms_sum / area
            if ratio > 1.05:
                warnings.append(
                    f"A soma dos ambientes ({_fmt(rooms_sum)} m²) passa da área do carimbo "
                    f"({_fmt(area)} m²): o carimbo pode se referir a só uma parte da obra."
                )
                consistent = False
            else:
                checks.append(
                    f"Soma dos {len(with_area)} ambientes com área: {_fmt(rooms_sum)} m² "
                    f"({ratio:.0%} da área do carimbo; o restante são paredes"
                    f"{' e ambientes sem área anotada' if len(with_area) < len(internal) else ''})."
                )
        reliable = declared.reliable
        if floors_sum:
            if abs(area - floors_sum) <= 0.005 * floors_sum:
                checks.append(f"A soma do quadro por pavimento ({_fmt(floors_sum)} m²) confere com o total do carimbo.")
                reliable = True
            else:
                warnings.append(f"A soma do quadro por pavimento ({_fmt(floors_sum)} m²) difere do total "
                                f"do carimbo ({_fmt(area)} m²): confira a leitura.")
                consistent = False
        if not reliable:
            warnings.append("O valor do carimbo veio do OCR e não foi confirmado na releitura.")
        confidence = "alta" if reliable and consistent else "media"

    elif with_area:
        area, kind, method = rooms_sum, "util", "areas_declaradas"
        evidence = [r.area_item for r in with_area if r.area_item]
        missing = [r.name for r in internal if r.area_m2 is None]
        if missing:
            warnings.append("Ambientes sem área legível (não somados): " + ", ".join(missing))
        warnings.append(
            "Esta é a área útil (soma dos ambientes). A área construída é maior, "
            "pois inclui as paredes; se o carimbo da prancha tiver a área total, use-a."
        )
        consistent = check_against_envelope(area, "A soma dos ambientes") and not table_conflict
        reliable = table_confirmed or all(r.area_item.reliable for r in with_area if r.area_item)
        confidence = (
            "alta" if reliable and consistent and len(with_area) >= 2 and not missing and not illegible
            else "media"
        )

    elif envelope:
        # Como RESPOSTA, a envoltória de todas as cotas pode ser a do terreno:
        # usa a linha de cotas mais curta que cobre os ambientes (nomes), se houver.
        focus_names = [(r.item.cx, r.item.cy) for r in rooms if not r.external]
        if len(focus_names) >= 3:
            tight, tight_items = find_envelope(items, exclude + label_items, focus_names, focus_trim=True)
            if tight is not None and tight.area_m2 < envelope.area_m2:
                envelope, cota_items = tight, tight_items
        if not envelope.covers(room_points):
            # Escola UFU sem os textos de área: cotas de 22 × 45 m numa obra de
            # 4.042 m² — o retângulo daria 1/4 da área, dito "acima do real".
            envelope.reliable = False
            area, kind, method, confidence = None, None, "indisponivel", "baixa"
            warnings.append(
                f"As cotas lidas cobrem só parte da planta: o retângulo delas ({_fmt(envelope.largura_m)} m × "
                f"{_fmt(envelope.profundidade_m)} m) deixa ambientes do lado de fora, então não serve de área. "
                "Informe a área manualmente para calcular prazo e custo."
            )
        else:
            area, kind, method = envelope.area_m2, "envoltoria", "cotas"
            evidence = cota_items
            warnings.append(
                f"Área pela envoltória das cotas ({_fmt(envelope.largura_m)} m × "
                f"{_fmt(envelope.profundidade_m)} m, cotas em {envelope.unit}). É um retângulo "
                "que contém a obra: em plantas com recortes o valor fica acima do real."
            )
            confidence = "baixa"

    else:
        area, kind, method, confidence = None, None, "indisponivel", "baixa"
        warnings.append(
            "Não encontramos área no carimbo, rótulos de área nos ambientes nem cotas "
            "coerentes. Informe a área manualmente para calcular prazo e custo."
        )

    if envelope and envelope.reliable and method != "cotas":
        checks.append(
            f"Cotas coerentes em {envelope.chains} cadeias (unidade: {envelope.unit})."
        )

    logger.info(
        "Leitura: método=%s área=%s confiança=%s | carimbo=%s | soma ambientes=%s (%d) | envoltória=%s",
        method, f"{area:.2f}" if area else None, confidence,
        declared.value if declared else None,
        f"{rooms_sum:.2f}" if rooms_sum else None, len(with_area),
        f"{envelope.largura_m:.2f}x{envelope.profundidade_m:.2f}" if envelope else None,
    )
    return PlanReading(
        area_m2=area, area_kind=kind, method=method, confidence=confidence,
        qtd_comodos=qtd_comodos, rooms=rooms, declared=declared,
        rooms_sum_m2=rooms_sum, rooms_with_area=len(with_area),
        area_externa_m2=area_externa, envelope=envelope,
        checks=checks, warnings=warnings, evidence=[e for e in evidence if e],
        cota_items=cota_items, declared_key=declared_key, floors_sum=floors_sum,
        drawing_rooms=drawing_rooms, building_envelope=building_envelope,
        rooms_complete=bool(with_area) and len(with_area) == len(internal) and not illegible
        and not (table and getattr(table, "orphans", 0)),
    )
