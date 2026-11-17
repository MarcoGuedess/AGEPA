# ============================================================
# AGEPA — Serviço de Leitura da Planta (PDF + Visão Computacional)
# services/ai_service.py
# ============================================================
#
# DEPENDÊNCIAS OBRIGATÓRIAS DO FLORENCE-2:
#   pip install einops timm
#   (sem elas o modelo carrega mas falha silenciosamente)
#
# Responsabilidades (SoC):
#   - Obter os TEXTOS da prancha, com posição e direção:
#       · PDF do CAD → texto exato (pdf_reader.py), sem IA.
#       · Imagem ou PDF escaneado → OCR do Florence-2 em blocos.
#   - Conferir por releitura ampliada os números que definem a área.
#   - Delegar a interpretação (área, ambientes, cotas) ao
#     plan_reader.py, que não depende de como o texto foi lido.
#   - Desenhar a imagem anotada.
#
# Prazo (schedule_service.py) e custo (cost_service.py) ficam fora:
# este módulo só LÊ a planta. Não conhece FastAPI nem HTTP.
# ============================================================

from __future__ import annotations

import base64
import io
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
from PIL import Image
from transformers import AutoModelForCausalLM, AutoProcessor

from services.geometry import (
    DOOR_GAP_M, MIN_OF_BUILDING, VALIDATION_NOTE, building_outline, outline_check,
)
from services.paddle_ocr import PaddleOcrWorker, estimate_tiles, raw_to_items
from services.pdf_reader import (
    GEOMETRY_MAX_PIXELS, choose_document_area, is_pdf, read_pdf, render_gray, rotate_point,
    scan_document_areas, scan_document_prazo, unrotate_point,
)
from services.plan_reader import (
    AREA_EXPLICIT_RE, AREA_NO_UNIT_RE, BARE_DECIMAL_RE, NUM, ROOM_KEYWORDS, VALUE_M2_RE,
    PlanReading, Room, TextItem, _is_aggregate_label, floor_multiplier, interpret, merge_area_units,
    normalize, parse_number,
)

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agepa.ai_service")

# Pranchas A0 em alta resolução passam de 100 milhões de pixels (o limite
# padrão do Pillow, contra "bombas de descompressão"). O arquivo já é
# limitado a 50 MB na API.
Image.MAX_IMAGE_PIXELS = 300_000_000

# ── Constantes ───────────────────────────────────────────────
MODEL_ID: str = "microsoft/Florence-2-base"
"""Modelo fundacional Florence-2-base (232 M parâmetros)."""

# Paleta BGR.
COLOR_PALETTE: Dict[str, Tuple[int, int, int]] = {
    "door":     (0, 200, 80),
    "window":   (255, 160, 0),
    "wall":     (0, 80, 255),
    "room":     (60, 150, 0),
    "declared": (200, 60, 140),
    "cota":     (230, 150, 40),
    "text":     (190, 190, 190),
    "default":  (200, 200, 0),
    "contorno": (40, 40, 220),
}
# Contorno das paredes em imagens: só com fundo branco (desenho exportado ou
# escaneado limpo). Foto ou escaneado acinzentado viraria "parede" inteira.
OUTLINE_MIN_BACKGROUND: int = 235
# Rótulo de porta/janela "80X220" (largura × altura, cm).
DOOR_LABEL_RE = re.compile(r"^\s*(\d{2,3})\s*[xX×]\s*(\d{2,3})\s*$")
BOX_THICKNESS: int = 2
FONT_SCALE: float = 0.55
FONT_THICKNESS: int = 1
LABEL_PADDING: int = 4

# ── OCR em blocos (tiling) ───────────────────────────────────
# O Florence-2 redimensiona toda entrada para 768x768. Numa prancha
# inteira os textos (cotas, "ÁREA=…") somem; recortando em blocos
# sobrepostos cada texto chega ao modelo em tamanho legível.
WORK_LONG_SIDE: int = 2000      # escala mínima de trabalho (lado maior, px)
# Pranchas grandes (A0/A1 exportadas em alta resolução) teriam a letra
# reduzida a 3–4 px se fossem a 2000 px: a escala de trabalho é escolhida
# medindo a altura da letra, para ela chegar ao modelo com ~TARGET_CHAR_PX.
MAX_WORK_LONG_SIDE: int = 7000
TARGET_CHAR_PX: float = 11.0
TILE_MIN_GLYPHS: int = 4        # bloco sem nenhum caractere legível é pulado
TILE_SIZE: int = 512
TILE_OVERLAP: float = 0.15
TILE_MIN_INK: float = 0.004     # fração mínima de pixels escuros (pula margens)
OCR_MAX_TOKENS: int = 512       # corta saídas degeneradas ("00 09 04 …")
# Caixa mais alta que larga no próprio passe = texto lido de lado (não confiável).
SIDEWAYS_RATIO: float = 1.3
# Releituras ampliadas: com margem 1,5 o modelo vê o nome do ambiente
# junto e acerta dígitos que erra no bloco (36,30→36,20).
REFINE_PADS: Tuple[float, ...] = (1.5, 1.0)
MAX_CONFIRMATIONS: int = 16

# Palavras de prancha usadas para decidir em qual orientação o texto é
# legível. Em rotação errada o Florence-2 "inventa" palavras (ex.:
# PROFESSIONAL); por isso só contam palavras deste vocabulário.
PLAN_VOCAB: Tuple[str, ...] = tuple(sorted(set(
    k for k in ROOM_KEYWORDS if len(k) >= 4 and " " not in k and "." not in k
) | {
    "PLANTA", "BAIXA", "ESCALA", "LEGENDA", "AREA", "COBERTURA", "PROJETO",
    "PROJECAO", "PAREDE", "PISO", "PORTA", "PORTAS", "JANELA", "JANELAS",
    "FACHADA", "CORTE", "ALUNOS", "MURO", "ALVENARIA", "ESQUADRIAS", "MADEIRA",
    "ALUMINIO", "CONCRETO", "CERAMICA", "BANCADA", "DIVISA", "EXISTENTE",
    "NIVEL", "MASC", "FEMININO", "MASCULINO", "INFANTIL", "CONSTRUIR",
    "RESPONSAVEL", "TECNICO", "CONTEUDO", "FOLHA", "DESENHO", "DATA",
}))


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class BoundingBox:
    """Caixa delimitadora [x1, y1, x2, y2] com rótulo."""
    x1: int
    y1: int
    x2: int
    y2: int
    label: str
    confidence: Optional[float] = None

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def area(self) -> int:
        return self.width * self.height

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2,
            "label": self.label, "confidence": self.confidence,
            "width": self.width, "height": self.height,
        }


@dataclass
class AnalysisReport:
    """Leitura consolidada de uma planta (sem prazo/custo)."""
    area_m2: Optional[float]
    area_kind: Optional[str]
    qtd_comodos: int
    total_detections: int
    detection_summary: Dict[str, int]
    ocr_texts: List[str]
    annotated_image_b64: str
    od_boxes: List[Dict[str, Any]]
    ocr_boxes: List[Dict[str, Any]]
    area_method: str
    confidence: str
    rooms: List[Dict[str, Any]]
    area_externa_m2: float
    orientation_deg: int
    text_source: str                          # "pdf" | "ocr"
    checks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    declared_area: Optional[Dict[str, Any]] = None
    rooms_sum_m2: Optional[float] = None
    rooms_with_area: int = 0
    envelope: Optional[Dict[str, Any]] = None
    original_image_b64: Optional[str] = None
    page: Optional[Dict[str, int]] = None
    multiplicador: Optional[Dict[str, Any]] = None    # pavimento repetido ("(x12)")
    documento: Optional[Dict[str, Any]] = None        # área declarada em outra página do PDF
    pagina_area: Optional[Dict[str, Any]] = None      # leitura da página, se o total veio do documento
    predio: Optional[Dict[str, Any]] = None           # leitura de várias páginas: tabela por pavimento
    contorno: Optional[Dict[str, Any]] = None         # área medida pelo contorno das paredes
    prazo_documento: Optional[Dict[str, Any]] = None  # prazo de execução escrito no edital/memorial
    ocr_engine: Optional[str] = None                  # imagens: "PP-OCRv6_small + Florence-2" ou "Florence-2"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "area_m2": round(self.area_m2, 2) if self.area_m2 is not None else None,
            "area_kind": self.area_kind,
            "qtd_comodos": self.qtd_comodos,
            "total_detections": self.total_detections,
            "detection_summary": self.detection_summary,
            "ocr_texts": self.ocr_texts,
            "annotated_image_b64": self.annotated_image_b64,
            "original_image_b64": self.original_image_b64,
            "od_boxes": self.od_boxes,
            "ocr_boxes": self.ocr_boxes,
            "area_method": self.area_method,
            "confidence": self.confidence,
            "rooms": self.rooms,
            "area_externa_m2": round(self.area_externa_m2, 2),
            "orientation_deg": self.orientation_deg,
            "text_source": self.text_source,
            "checks": self.checks,
            "warnings": self.warnings,
            "declared_area": self.declared_area,
            "rooms_sum_m2": round(self.rooms_sum_m2, 2) if self.rooms_sum_m2 else None,
            "rooms_with_area": self.rooms_with_area,
            "envelope": self.envelope,
            "page": self.page,
            "multiplicador": self.multiplicador,
            "documento": self.documento,
            "pagina_area": self.pagina_area,
            "predio": self.predio,
            "contorno": self.contorno,
            "prazo_documento": self.prazo_documento,
            "ocr_engine": self.ocr_engine,
        }


# ══════════════════════════════════════════════════════════════
# Progresso e Estimativa de Tempo
# ══════════════════════════════════════════════════════════════

STAGE_ORDER: Tuple[str, ...] = (
    "fila", "preparo", "pdf", "ocr_rapido", "ocr", "ocr_vertical", "ocr_extra",
    "releitura", "deteccao", "calculo",
)

# Segundos por unidade (uma chamada ao Florence-2: um bloco, um rótulo
# relido...), medidos com Florence-2-base numa CPU de 16 threads; na GPU
# (RTX 3050) cerca de 2,5x menos. "ocr_rapido" = um bloco de 1280 px do
# PP-OCRv6, que roda na CPU nos dois casos. Cada análise refina a média (EMA).
DEFAULT_UNIT_SECONDS: Dict[str, Dict[str, float]] = {
    "cpu":  {"pdf": 1.0, "ocr_rapido": 1.5, "ocr": 7.0, "ocr_vertical": 7.0, "ocr_extra": 7.0,
             "releitura": 3.0, "deteccao": 2.5},
    "cuda": {"pdf": 1.0, "ocr_rapido": 1.5, "ocr": 2.8, "ocr_vertical": 2.8, "ocr_extra": 2.8,
             "releitura": 1.2, "deteccao": 1.0},
}
EXPECTED_CONFIRMATIONS: int = 6
EMA_ALPHA: float = 0.3


class AnalysisCancelled(Exception):
    """O usuário cancelou a análise em andamento."""


class ProgressTracker:
    """Estado de uma análise, lido por outra thread (rota de progresso)."""

    def __init__(self, unit_seconds: Dict[str, float], plan: Dict[str, int]) -> None:
        self._unit_seconds = unit_seconds          # compartilhado: aprende entre análises
        self._plan: Dict[str, int] = dict(plan)
        self._done: Dict[str, int] = {}
        self._started = time.monotonic()
        self._unit_started = self._started
        self._cancel = threading.Event()
        self.stage: str = "fila"
        self.step: int = 0
        self.total: int = 0
        self.page_label: Optional[str] = None       # leitura de várias páginas: "página 3 (2 de 5)"

    def cancel(self) -> None:
        self._cancel.set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise AnalysisCancelled()

    def set_stage(self, stage: str, total: int = 0) -> None:
        self.check()
        # A etapa que termina vale o que de fato rodou, para não inflar o restante.
        self._plan[self.stage] = self._done.get(self.stage, 0)
        self.stage, self.step, self.total = stage, 0, total
        if total:
            # Etapa que volta (releitura do quadro, depois a conferência dos rótulos):
            # o que já rodou nela soma, senão o restante sai menor que o real.
            self._plan[stage] = self._done.get(stage, 0) + total
        self._unit_started = time.monotonic()

    def skip(self, stage: str) -> None:
        """Etapa prevista que não vai rodar (ex.: só o PDF já bastou)."""
        self._plan[stage] = 0

    def advance(self) -> None:
        now = time.monotonic()
        if self.stage in self._unit_seconds:
            old = self._unit_seconds[self.stage]
            self._unit_seconds[self.stage] = (1 - EMA_ALPHA) * old + EMA_ALPHA * (now - self._unit_started)
        self.step += 1
        self._done[self.stage] = self._done.get(self.stage, 0) + 1
        self._unit_started = now
        self.check()

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        remaining = 0.0
        for stage, units in self._plan.items():
            left = max(units - self._done.get(stage, 0), 0)
            remaining += left * self._unit_seconds.get(stage, 0.0)
        if self.stage in self._unit_seconds and self.step < self.total:
            remaining -= min(now - self._unit_started, 0.9 * self._unit_seconds[self.stage])
        elapsed = now - self._started
        remaining = max(remaining, 0.0)
        return {
            "stage": self.stage,
            "step": self.step,
            "total": self.total,
            "pagina": self.page_label,
            "elapsed_s": round(elapsed, 1),
            "eta_s": round(remaining, 1),
            "percent": round(100 * elapsed / (elapsed + remaining), 1) if elapsed + remaining else 0.0,
        }


# ══════════════════════════════════════════════════════════════
# Funções auxiliares
# ══════════════════════════════════════════════════════════════

def _legibility_score(texts: List[str]) -> int:
    """Conta palavras de prancha reconhecíveis (tolera 1 letra trocada).

    Números não entram: o modelo "lê" cotas até em texto de lado.
    """
    score = 0
    for text in texts:
        for word in re.findall(r"[A-Z]{4,}", normalize(text)):
            if any(
                word == v or (len(v) >= 6 and SequenceMatcher(None, word, v).ratio() >= 0.85)
                for v in PLAN_VOCAB
            ):
                score += 1
    return score


def _fmt_br(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _fmt_area(value: float) -> str:
    """1234.5 → "1.234,50"."""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Saídas que o Florence-2 "inventa" em áreas vazias ou hachuradas.
_HALLUCINATION_RE = re.compile(r"<loc_|</?poly|www\.|\.com\b|https?:", re.IGNORECASE)


def _plausible_ocr(label: str) -> bool:
    """Descarta alucinações típicas do OCR generativo (marcas d'água, lixo)."""
    if _HALLUCINATION_RE.search(label):
        return False
    stripped = label.replace(" ", "")
    if not stripped:
        return False
    alnum = sum(c.isalnum() for c in stripped)
    return alnum / len(stripped) >= 0.4


def _flat_quad(quad: Sequence[float], label: str) -> bool:
    """Caixa do Florence-2 achatada (< 3 px de altura ou de largura) com 2+ letras.

    Nas plantas de teste, essas caixas eram leitura inventada ("www.pixabay.com",
    "Portuguese-Spanish", "A =124,141 m2" numa caixa de 0 px na quadra do FNDE) ou
    rótulo cortado na borda do bloco — que o bloco vizinho (15% de sobreposição)
    lê inteiro; no TCC, o "A=3.14 m²" do elevador entrava duas vezes na soma.
    """
    xs, ys = quad[0::2], quad[1::2]
    return min(max(xs) - min(xs), max(ys) - min(ys)) < 3 and len(label.replace(" ", "")) >= 2


def _pad_square(image: Image.Image) -> Image.Image:
    """O Florence-2 achata toda entrada em 768×768; recortes compridos
    viram letras deformadas. Completar com branco preserva a proporção."""
    w, h = image.size
    side = max(w, h)
    if side <= 1.6 * min(w, h):
        return image
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(image, ((side - w) // 2, (side - h) // 2))
    return canvas


def _extract_value(norm: str) -> Optional[float]:
    """Valor de área de um texto: 'ÁREA=12,5m²', '12,50 m²' ou '178,71'."""
    for pattern in (AREA_EXPLICIT_RE, VALUE_M2_RE):
        m = pattern.search(norm)
        if m:
            return parse_number(m.group(1))
    m = re.search(r"(\d{1,4}[.,]\d{1,2})", norm)
    return parse_number(m.group(1)) if m else None


def _single_value(norm: str) -> Optional[float]:
    """Valor de uma RELEITURA ampliada — None se o recorte tiver dois números diferentes.

    O recorte em volta de um rótulo pode pegar o vizinho do carimbo; as duas releituras
    leem o mesmo vizinho e "confirmam" o número errado (a escola UFU trocou "2.029,49"
    por "8.117,96"). Recorte ambíguo não vota.
    """
    values = {parse_number(m) for m in re.findall(r"\d{1,3}(?:\.\d{3})+,\d{1,2}|\d{1,4}[.,]\d{1,2}", norm)}
    values.discard(None)
    if len(values) > 1:
        return None
    return _extract_value(norm)


def _replace_value(text: str, old: float, new: float) -> str:
    """Troca no texto original o número `old` pelo `new` (formato BR)."""
    for m in re.finditer(NUM, text):
        if parse_number(m.group()) == old:
            return text[:m.start()] + _fmt_br(new) + text[m.end():]
    return _fmt_br(new)


def _inter(a: TextItem, b: TextItem) -> float:
    ix = max(0.0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y1, b.y1))
    return ix * iy


def _box_area(a: TextItem) -> float:
    return max((a.x2 - a.x1) * (a.y2 - a.y1), 1.0)


def _has_value(it: TextItem) -> bool:
    """O texto forma área ou cota válida ("A=9,00 m²", "A=4.04", "12,40")."""
    n = it.norm
    compact = n.replace(" ", "")
    return bool(AREA_EXPLICIT_RE.search(n) or VALUE_M2_RE.search(n)
                or AREA_NO_UNIT_RE.match(compact) or BARE_DECIMAL_RE.match(compact))


# Linhas que NÃO são área: pé-direito ("PD laje=2.675m"), cotas, medidas "2,0 x 2,50m".
_NOT_AREA_RE = re.compile(
    r"\b(?:PD|P\.D\.?|LAJE|LAJO|GESSO|FORRO|PISO|COTA|NIVEL|ESC|ESCALA)\b|\d\s*X\s*\d|=\s*\d[.,]\d{3}"
)
MAX_RECOVERIES: int = 12


def _almost_area(it: TextItem) -> bool:
    """Texto do OCR com cara de rótulo de área que NÃO virou um número válido.

    TCC, planta tipo: "A=14.75m²" saiu "A55m"; "VAZIO A=1.38 m²", cortado pelo "X"
    do shaft, saiu "A-1.33 m". Rótulo bem lido (mesmo que fora da soma por outro
    motivo: total da unidade, valor sem nome) não entra.
    """
    n = it.norm
    c = n.replace(" ", "")
    if it.source != "ocr" or not re.search(r"\d", c) or len(c) > 14 or _NOT_AREA_RE.search(n):
        return False
    if not (re.match(r"^A[=:\-.]?[0-9O]", c) or re.search(r"\dM{1,2}[2*'\"]$", c)):
        return False
    m = AREA_EXPLICIT_RE.search(n) or VALUE_M2_RE.search(n)
    if m:
        value = parse_number(m.group(1))
        with_unit = not re.fullmatch(r"M{1,2}", n[m.end(1):].strip())
        if value is not None and 0.5 <= value <= 5000 and with_unit:
            return False
    return True


def _label_value(norm: str) -> Optional[float]:
    """Valor de área numa releitura ("A1.38 M", "A=14.75M3", "0.86M2"), ou None.

    Exige cara de rótulo de área e UM só número com 2 casas: "PD GESSO-2.50M" não vota.
    """
    c = norm.replace(" ", "")
    if _NOT_AREA_RE.search(norm) or not (re.match(r"^A[=:\-.]?\d", c) or re.search(r"\d[.,]\d{2}M", c)):
        return None
    values = {parse_number(x) for x in re.findall(r"(?<![\d.,])\d{1,4}[.,]\d{2}(?!\d)", c)}
    values.discard(None)
    return values.pop() if len(values) == 1 else None


def _dedupe(items: List[TextItem]) -> List[TextItem]:
    """Remove o mesmo texto lido em blocos (ou passes) sobrepostos."""
    # Leituras que formam área/cota válidas primeiro, depois as mais longas:
    # o mesmo rótulo pode sair "AREA=7.74m" num bloco e "AREA-7/4m" no outro.
    def quality(it: TextItem) -> Tuple[int, int]:
        return int(_has_value(it)), len(it.text)

    kept: List[TextItem] = []
    for it in sorted(items, key=quality, reverse=True):
        dup = False
        for k in kept:
            i = _inter(it, k)
            union = _box_area(it) + _box_area(k) - i
            if (union and i / union > 0.5) or i / min(_box_area(it), _box_area(k)) > 0.7:
                dup = True
                break
        if not dup:
            kept.append(it)
    return kept


def _complement(primary: List[TextItem], extra: List[TextItem]) -> List[TextItem]:
    """Junta à 1ª leitura (PP-OCRv6) só o que ela NÃO leu.

    Texto da 2ª leitura (Florence-2) em cima de um texto da 1ª é a mesma escrita
    lida de novo. Juntar as duas pelo _dedupe ("a mais longa vence") trocou, no
    TCC, o "A=3.14 m²" certo do elevador por "A-5.14 mm2" — e uma 2ª caixa achatada
    do Florence-2 no mesmo lugar contou o elevador duas vezes. Aqui a 1ª leitura
    fica; da 2ª entra o texto em lugar vazio, ou a área que a 1ª não leu naquele
    ponto — quando a 2ª leitura COBRE os pedaços sem número da 1ª (o nome do
    cômodo, um "m²" solto) e os substitui. Pedaço DENTRO de uma linha que a 1ª leu
    inteira não entra: no TCC, o fim de "PD laje=2.675m" (pé-direito) saiu do
    Florence-2 como "A=2.67m" e virou um ambiente de 2,67 m² que não existe.
    """
    out = list(primary)
    for it in extra:
        near = [p for p in primary
                if _inter(it, p) / min(_box_area(it), _box_area(p)) > 0.3]
        if not near:
            out.append(it)
        elif (_has_value(it) and not any(_has_value(p) for p in near)
              and all(_inter(it, p) / _box_area(p) > 0.7 for p in near)):
            gone = {id(p) for p in near}
            out = [o for o in out if id(o) not in gone] + [it]
    return out


# ══════════════════════════════════════════════════════════════
# Classe Principal — FloorPlanAnalyzer
# ══════════════════════════════════════════════════════════════

class FloorPlanAnalyzer:
    """Lê plantas baixas: texto exato do PDF ou OCR do Florence-2."""

    def __init__(self, model_id: str = MODEL_ID, device: Optional[str] = None) -> None:
        self._model_id: str = model_id
        self._device: torch.device = torch.device(
            device or ("cuda" if torch.cuda.is_available() else "cpu")
        )
        # float16 na CPU causa falha silenciosa no Florence-2.
        self._dtype: torch.dtype = torch.float16 if self._device.type == "cuda" else torch.float32
        logger.info("Dispositivo: %s | dtype: %s", self._device, self._dtype)

        # SEM try/except genérico: se falhar, o servidor DEVE parar.
        self._model: AutoModelForCausalLM = self._load_model()
        self._processor: AutoProcessor = self._load_processor()

        # Uma análise por vez: duas em paralelo na CPU só dobram o tempo.
        self._lock = threading.Lock()
        self._unit_seconds: Dict[str, float] = dict(
            DEFAULT_UNIT_SECONDS["cuda" if self._device.type == "cuda" else "cpu"]
        )
        self._tracker: Optional[ProgressTracker] = None

        # OCR rápido (PP-OCRv6) num processo auxiliar, iniciado já em segundo plano;
        # sem ele, a leitura de imagens segue toda com o Florence-2.
        self._fast_ocr = PaddleOcrWorker()
        threading.Thread(target=self._fast_ocr.available, daemon=True).start()

    @property
    def ocr_engine(self) -> str:
        """Motor de OCR das imagens, para a tela ("PP-OCRv6 + Florence-2" ou "Florence-2")."""
        if self._fast_ocr.ready:
            return f"{self._fast_ocr.model} + Florence-2"
        return "Florence-2"

    @property
    def device(self) -> torch.device:
        return self._device

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def model_id(self) -> str:
        return self._model_id

    # ── Estimativa de tempo ──────────────────────────────────

    @staticmethod
    def _work_size(width: int, height: int) -> Tuple[int, int]:
        """Estimativa (só pelas dimensões) da escala de trabalho — para o ETA."""
        long_side = max(width, height, 1)
        target = WORK_LONG_SIDE if long_side <= 4000 else min(long_side * 0.5, MAX_WORK_LONG_SIDE)
        scale = target / long_side
        if abs(scale - 1) <= 0.05:
            return width, height
        return round(width * scale), round(height * scale)

    @staticmethod
    def _glyph_stats(gray: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Centros e alturas dos componentes com cara de letra (nem traço longo,
        nem mancha cheia)."""
        ink = (gray < 140).astype(np.uint8)
        _, _, stats, centroids = cv2.connectedComponentsWithStats(ink, connectivity=8)
        w, h, a = stats[1:, cv2.CC_STAT_WIDTH], stats[1:, cv2.CC_STAT_HEIGHT], stats[1:, cv2.CC_STAT_AREA]
        mask = (h >= 4) & (h <= 80) & (w <= 1.6 * h) & (w >= 0.12 * h) & (a >= 0.12 * w * h) & (a <= 0.85 * w * h)
        return centroids[1:][mask], h[mask]

    def _work_factor(self, image: Image.Image) -> float:
        """Fator (≤ 1) que leva a letra mediana da prancha a ~TARGET_CHAR_PX."""
        long_side = max(image.size)
        probe_f = min(1.0, 4000 / long_side)
        probe = image if probe_f == 1 else image.resize(
            (round(image.width * probe_f), round(image.height * probe_f)), Image.LANCZOS)
        _, heights = self._glyph_stats(np.asarray(probe.convert("L")))
        lo = WORK_LONG_SIDE / long_side
        hi = min(1.0, MAX_WORK_LONG_SIDE / long_side)
        if len(heights) < 80:
            factor = lo
        else:
            char_native = float(np.median(heights)) / probe_f
            factor = TARGET_CHAR_PX / char_native
            logger.info("Altura mediana da letra: %.1f px (original) → escala de trabalho %.2f",
                        char_native, min(max(factor, lo), hi))
        return min(max(factor, lo), max(hi, lo))

    @staticmethod
    def _tile_origins(length: int) -> List[int]:
        step = max(int(TILE_SIZE * (1 - TILE_OVERLAP)), 1)
        if length <= TILE_SIZE:
            return [0]
        pos = list(range(0, length - TILE_SIZE + 1, step))
        if pos[-1] + TILE_SIZE < length:
            pos.append(length - TILE_SIZE)
        return pos

    def _plan_for(self, width: int, height: int) -> Dict[str, int]:
        if self._fast_ocr.ready:
            return {"ocr_rapido": estimate_tiles(width, height),
                    "releitura": EXPECTED_CONFIRMATIONS, "deteccao": 1}
        w, h = self._work_size(width, height)
        tiles = len(self._tile_origins(w)) * len(self._tile_origins(h))
        return {"ocr": tiles, "ocr_vertical": tiles,
                "releitura": EXPECTED_CONFIRMATIONS, "deteccao": 1}

    def estimate_seconds(self, width: int = 2000, height: int = 1414) -> float:
        """Tempo previsto para uma IMAGEM desse tamanho (PDF com texto: segundos)."""
        plan = self._plan_for(width, height)
        return sum(n * self._unit_seconds.get(stage, 0.0) for stage, n in plan.items())

    def new_tracker_for(self, data: bytes) -> ProgressTracker:
        if is_pdf(data):
            return ProgressTracker(self._unit_seconds, {"pdf": 1})
        try:
            width, height = Image.open(io.BytesIO(data)).size  # só lê o cabeçalho
        except Exception:
            width, height = 2000, 1414
        return ProgressTracker(self._unit_seconds, self._plan_for(width, height))

    # ── Carregamento ─────────────────────────────────────────

    def _load_model(self) -> AutoModelForCausalLM:
        logger.info("Carregando modelo '%s' (dtype=%s) …", self._model_id, self._dtype)
        model = AutoModelForCausalLM.from_pretrained(
            self._model_id,
            dtype=self._dtype,
            trust_remote_code=True,   # obrigatório para Florence-2
            # O código remoto do Florence-2 não declara suporte a SDPA.
            attn_implementation="eager",
        )
        model = model.to(self._device)
        model.eval()
        # O código remoto só entende o cache no formato antigo (tuplas); no
        # transformers >= 4.5x o generate() cria um EncoderDecoderCache e
        # quebra. Sem o objeto, volta às tuplas e o cache segue ativo.
        type(model.language_model)._supports_default_dynamic_cache = classmethod(
            lambda cls: False
        )
        n_params = sum(p.numel() for p in model.parameters())
        logger.info("Florence-2 carregado: %.1f M params @ %s", n_params / 1e6, self._device)
        return model

    def _load_processor(self) -> AutoProcessor:
        logger.info("Carregando processador …")
        return AutoProcessor.from_pretrained(self._model_id, trust_remote_code=True)

    # ── Inferência (Florence-2) ───────────────────────────────

    def _run_single_task(
        self,
        image: Image.Image,
        task_prompt: str,
        num_beams: int = 3,
        max_new_tokens: int = 1024,
    ) -> Dict[str, Any]:
        inputs = self._processor(text=task_prompt, images=image, return_tensors="pt")
        inputs = {
            k: v.to(self._device, dtype=self._dtype) if v.dtype.is_floating_point else v.to(self._device)
            for k, v in inputs.items()
        }
        with torch.inference_mode():
            generated_ids: torch.Tensor = self._model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=max_new_tokens,
                num_beams=num_beams,
                do_sample=False,
            )
        generated_text: str = self._processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
        return self._processor.post_process_generation(
            generated_text, task=task_prompt, image_size=(image.width, image.height),
        )

    # ── Progresso (no-op quando analyze() roda sem tracker) ──

    def _set_stage(self, stage: str, total: int = 0) -> None:
        if self._tracker:
            self._tracker.set_stage(stage, total)

    def _skip_stage(self, stage: str) -> None:
        if self._tracker:
            self._tracker.skip(stage)

    def _advance(self) -> None:
        if self._tracker:
            self._tracker.advance()

    # ── OCR em blocos ────────────────────────────────────────

    def _ocr_tiled(self, image: Image.Image, stage: str) -> List[BoundingBox]:
        """OCR com regiões em blocos sobrepostos, em coordenadas da imagem."""
        w, h = image.size
        gray = np.asarray(image.convert("L"))
        centers, _ = self._glyph_stats(gray)

        def has_text(x0: int, y0: int) -> bool:
            if (gray[y0:y0 + TILE_SIZE, x0:x0 + TILE_SIZE] < 128).mean() < TILE_MIN_INK:
                return False
            inside = ((centers[:, 0] >= x0) & (centers[:, 0] < x0 + TILE_SIZE)
                      & (centers[:, 1] >= y0) & (centers[:, 1] < y0 + TILE_SIZE))
            return int(inside.sum()) >= TILE_MIN_GLYPHS

        tiles = [
            (x0, y0, min(x0 + TILE_SIZE, w), min(y0 + TILE_SIZE, h))
            for y0 in self._tile_origins(h)
            for x0 in self._tile_origins(w)
            if has_text(x0, y0)
        ]
        self._set_stage(stage, len(tiles))
        boxes: List[BoundingBox] = []
        for x0, y0, x1, y1 in tiles:
            parsed = self._run_single_task(
                image.crop((x0, y0, x1, y1)), "<OCR_WITH_REGION>",
                num_beams=1, max_new_tokens=OCR_MAX_TOKENS,
            )
            self._advance()
            ocr = parsed.get("<OCR_WITH_REGION>", {})
            for quad, label in zip(ocr.get("quad_boxes", []), ocr.get("labels", [])):
                label = label.replace("</s>", "").replace("<s>", "").strip()
                if not _plausible_ocr(label) or _flat_quad(quad, label):
                    continue
                xs, ys = quad[0::2], quad[1::2]
                boxes.append(BoundingBox(
                    int(min(xs)) + x0, int(min(ys)) + y0,
                    int(max(xs)) + x0, int(max(ys)) + y0, label,
                ))
        return boxes

    def _ocr_pass(self, work: Image.Image, rotation: int, stage: str) -> Tuple[List[TextItem], int]:
        """OCR da imagem girada `rotation` graus (anti-horário).

        Só guarda o texto que ficou NA HORIZONTAL nesse giro (caixa mais
        larga que alta): texto de lado vira chute do modelo. Devolve os
        textos no quadro da imagem de trabalho e a nota de legibilidade.
        """
        w, h = work.size
        rotated = work.rotate(rotation, expand=True) if rotation else work
        boxes = self._ocr_tiled(rotated, stage)
        items: List[TextItem] = []
        for b in boxes:
            if b.height > SIDEWAYS_RATIO * max(b.width, 1):
                continue
            ax, ay = unrotate_point(b.x1, b.y1, rotation, w, h)
            bx, by = unrotate_point(b.x2, b.y2, rotation, w, h)
            items.append(TextItem(
                text=b.label, x1=min(ax, bx), y1=min(ay, by), x2=max(ax, bx), y2=max(ay, by),
                angle=rotation, source="ocr",
            ))
        items = _dedupe(items)
        score = _legibility_score([it.text for it in items])
        logger.info("OCR %3d°: %d textos na horizontal, legibilidade=%d", rotation, len(items), score)
        return items, score

    def _read_fast(self, full: Image.Image, work: Image.Image) -> Optional[List[TextItem]]:
        """1ª leitura da prancha com o PP-OCRv6 (processo auxiliar), na resolução
        original; textos devolvidos no quadro de trabalho. None = indisponível."""
        if not self._fast_ocr.available():
            return None
        self._set_stage("ocr_rapido")
        raw = self._fast_ocr.read(full, on_total=lambda n: self._set_stage("ocr_rapido", n),
                                  on_tile=self._advance)
        if raw is None:
            return None
        items = _dedupe(raw_to_items(raw, full.width / work.width))
        logger.info("OCR rápido: %d textos (%d de lado).", len(items),
                    sum(1 for it in items if it.angle in (90, 270)))
        return items

    def _read_image_text(self, work: Image.Image) -> Tuple[int, List[TextItem], List[str]]:
        """Lê a prancha nas orientações necessárias e junta os textos.

        Passes a 0° e 90° pegam o texto horizontal e o vertical (cotas e
        rótulos de lado). Se nenhum dos dois for legível, a prancha pode
        estar de cabeça para baixo: tenta 180° e 270°.
        Devolve (giro que deixa o texto principal em pé, textos nesse
        quadro, avisos).
        """
        warnings: List[str] = []
        passes: Dict[int, Tuple[List[TextItem], int]] = {0: self._ocr_pass(work, 0, "ocr")}
        # Planta em pé que já deu área confiável no passe horizontal: o passe
        # vertical (só cotas de lado) não mudaria o resultado — economiza minutos.
        items0, score0 = passes[0]
        quick = interpret(items0, np.asarray(work.convert("L"))) if score0 >= 8 else None
        if quick is not None and (quick.declared is not None or quick.rooms_with_area >= 5):
            logger.info("Passe horizontal suficiente (%s); passe vertical dispensado.", quick.method)
            self._skip_stage("ocr_vertical")
            self._skip_stage("ocr_extra")
            return 0, items0, warnings
        passes[90] = self._ocr_pass(work, 90, "ocr_vertical")
        if max(score for _, score in passes.values()) < 3:
            passes[180] = self._ocr_pass(work, 180, "ocr_extra")
            passes[270] = self._ocr_pass(work, 270, "ocr_extra")
        else:
            self._skip_stage("ocr_extra")

        best = max(passes, key=lambda r: passes[r][1])
        best_score = passes[best][1]
        if best_score == 0:
            best = 0
            warnings.append(
                "Não reconhecemos palavras da planta em nenhuma orientação; "
                "a imagem pode estar com resolução baixa demais."
            )
        # Passes de cabeça para baixo só entram se forem tão legíveis quanto o melhor.
        used = [r for r in passes if r in (0, 90) or passes[r][1] >= best_score / 2]
        if best:
            logger.info("Texto principal em pé com giro de %d°.", best)

        w, h = work.size
        merged: List[TextItem] = []
        for r in used:
            for it in passes[r][0]:
                ax, ay = rotate_point(it.x1, it.y1, best, w, h)
                bx, by = rotate_point(it.x2, it.y2, best, w, h)
                merged.append(TextItem(
                    text=it.text, x1=min(ax, bx), y1=min(ay, by), x2=max(ax, bx), y2=max(ay, by),
                    angle=(r - best) % 360, source="ocr",
                ))
        return best, _dedupe(merged), warnings

    # ── Conferência dos números que definem a área ───────────

    @staticmethod
    def _crop_full(
        full: Image.Image, scale: float, box: Tuple[float, float, float, float],
    ) -> Tuple[Image.Image, Tuple[int, int]]:
        """Recorta da imagem em resolução ORIGINAL uma caixa dada no quadro
        de trabalho (que foi reduzido para ~2000 px): mais pixels por letra.
        Devolve o recorte e sua origem (px da imagem original)."""
        x1, y1, x2, y2 = (v * scale for v in box)
        ox, oy = int(max(x1, 0)), int(max(y1, 0))
        crop = full.crop((ox, oy, int(min(x2, full.width)), int(min(y2, full.height))))
        return crop, (ox, oy)

    def _reread(self, full: Image.Image, scale: float, item: TextItem, pad: float) -> Optional[float]:
        margin = pad * max(item.char_h, 8)
        crop, _ = self._crop_full(full, scale, (
            item.x1 - margin, item.y1 - margin, item.x2 + margin, item.y2 + margin,
        ))
        if item.angle:
            crop = crop.rotate(item.angle, expand=True)
        text = normalize(self._run_single_task(
            _pad_square(crop), "<OCR>", num_beams=1, max_new_tokens=40,
        ).get("<OCR>", ""))
        return _single_value(text)

    def _zoom_fast(self, full: Image.Image, scale: float,
                   box: Tuple[float, float, float, float]) -> Optional[List[TextItem]]:
        """PP-OCRv6 num recorte ampliado (letra ~64 px de linha). None sem o OCR rápido."""
        if not self._fast_ocr.ready:
            return None
        crop, (ox, oy) = self._crop_full(full, scale, box)
        if crop.width < 4 or crop.height < 4:
            return None
        z = max(1.0, min(4.0, 64.0 / crop.height))
        raw = self._fast_ocr.read(crop.resize((round(crop.width * z), round(crop.height * z)), Image.LANCZOS))
        if raw is None:
            return None
        return [TextItem(it.text, (ox + it.x1 / z) / scale, (oy + it.y1 / z) / scale,
                         (ox + it.x2 / z) / scale, (oy + it.y2 / z) / scale, angle=it.angle, source="ocr")
                for it in raw_to_items(raw)]

    def _recover_labels(self, full: Image.Image, scale: float, items: List[TextItem],
                        reading: PlanReading) -> int:
        """Relê ampliado o texto com cara de rótulo de área que não virou número.

        Votos: Florence-2 em dois tamanhos de letra e o PP-OCRv6 no recorte ampliado.
        Só vota o texto lido EM CIMA do rótulo (o recorte pode pegar o vizinho de cima
        ou de baixo). Duas leituras iguais = o rótulo entra, marcado como conferido;
        sem consenso, fica de fora.
        Texto "de cabeça para baixo" (180°) também é lido em pé: na letra miúda do TCC
        o classificador de orientação do PP-OCRv6 virou "A=14.75m²" e leu "A55m".
        Devolve quantos rótulos foram recuperados.
        """
        used = {id(r.area_item) for r in reading.rooms if r.area_item is not None}
        cands = [it for it in items if id(it) not in used and _almost_area(it)
                 and not _is_aggregate_label(it, items)][:MAX_RECOVERIES]
        if not cands:
            return 0
        self._set_stage("releitura", len(cands))
        fixed = 0
        for it in cands:
            margin = 0.6 * max(it.char_h, 6.0)
            box = (it.x1 - margin, it.y1 - margin, it.x2 + margin, it.y2 + margin)
            ang = it.angle or 0
            tries = [(ang, 32.0), (ang, 44.0)] if ang in (90, 270) else \
                [(0, 32.0), (0, 44.0)] + ([(180, 32.0)] if ang == 180 else [])
            reads = [self._ocr_zoom(full, scale, box, a, it.char_h, max_tokens=64, target_px=t) for a, t in tries]
            if ang in (0, 180):
                fast = self._zoom_fast(full, scale, box)       # o PP-OCRv6 decide a orientação de novo
                if fast is not None:
                    reads.append(fast)
            votes: List[Tuple[float, int]] = []
            for found in reads:
                values = {(_label_value(f.norm), f.angle or 0) for f in found
                          if _inter(f, it) / min(_box_area(f), _box_area(it)) > 0.3}
                values = {v for v in values if v[0] is not None}
                if len({v for v, _ in values}) == 1:
                    votes.append(values.pop())
            self._advance()
            top, count = Counter(v for v, _ in votes).most_common(1)[0] if votes else (None, 0)
            if top is not None and count >= 2 and 0.5 <= top <= 5000:
                logger.info("Rótulo recuperado na releitura: '%s' → %s m² (votos: %s)",
                            it.text, _fmt_br(top), [v for v, _ in votes])
                it.set_text(f"A={_fmt_br(top)} m²")
                it.angle = Counter(a for v, a in votes if v == top).most_common(1)[0][0]
                it.confirmed = True
                fixed += 1
            else:
                logger.info("Rótulo '%s' sem consenso na releitura (votos: %s): fica de fora.", it.text, [v for v, _ in votes])
        return fixed

    def _read_declared_cell(
        self, full: Image.Image, scale: float, key: TextItem,
    ) -> List[TextItem]:
        """Relê ampliada a célula do carimbo ao redor de "ÁREA A CONSTRUIR".

        A letra do carimbo costuma ser a menor da prancha: no bloco de
        512 px o valor sai ilegível ("43 x 30"). Aqui a região abaixo e à
        direita do rótulo é lida da imagem original, ampliada, com regiões.
        Devolve os textos encontrados no quadro de trabalho.
        """
        h, length = key.char_h, key.length
        # Região no referencial do rótulo (u ao longo da leitura, v para baixo).
        u0, u1 = -length / 2 - 3 * h, length / 2 + max(length, 12 * h)
        v0, v1 = -1.5 * h, 6 * h
        theta = np.radians(key.angle or 0)
        corners = [
            (key.cx + u * np.cos(theta) - v * np.sin(theta),
             key.cy + u * np.sin(theta) + v * np.cos(theta))
            for u in (u0, u1) for v in (v0, v1)
        ]
        box = (min(c[0] for c in corners), min(c[1] for c in corners),
               max(c[0] for c in corners), max(c[1] for c in corners))
        found = self._ocr_zoom(full, scale, box, key.angle or 0, h, max_tokens=256)
        logger.info("Carimbo relido ampliado: %s", [it.text for it in found])
        return found

    def _ocr_zoom(
        self, full: Image.Image, scale: float, box: Tuple[float, float, float, float],
        angle: int, char_h: float, max_tokens: int = 512, target_px: float = 32.0,
    ) -> List[TextItem]:
        """OCR com regiões de uma caixa (quadro de trabalho), lida da imagem
        ORIGINAL e ampliada até a letra ter ~target_px. Devolve TextItems no
        quadro de trabalho."""
        crop, (ox, oy) = self._crop_full(full, scale, box)
        if crop.width < 4 or crop.height < 4:
            return []
        zoom = max(1.0, min(4.0, target_px / max(char_h * scale, 1.0)))
        crop = crop.resize((round(crop.width * zoom), round(crop.height * zoom)), Image.LANCZOS)
        upright = crop.rotate(angle, expand=True) if angle else crop
        padded = _pad_square(upright)
        off_x = (padded.width - upright.width) // 2
        off_y = (padded.height - upright.height) // 2

        parsed = self._run_single_task(padded, "<OCR_WITH_REGION>", num_beams=3, max_new_tokens=max_tokens)
        ocr = parsed.get("<OCR_WITH_REGION>", {})
        found: List[TextItem] = []
        cw, ch = crop.size
        for quad, label in zip(ocr.get("quad_boxes", []), ocr.get("labels", [])):
            label = label.replace("</s>", "").replace("<s>", "").strip()
            if not _plausible_ocr(label) or _flat_quad(quad, label):
                continue
            pts = []
            for px, py in zip(quad[0::2], quad[1::2]):
                # quadrado → recorte em pé → recorte ampliado → original → trabalho
                zx, zy = unrotate_point(px - off_x, py - off_y, angle, cw, ch)
                pts.append(((ox + zx / zoom) / scale, (oy + zy / zoom) / scale))
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            found.append(TextItem(label, min(xs), min(ys), max(xs), max(ys), angle=angle, source="ocr"))
        return found

    def _reread_table(self, full: Image.Image, scale: float, rows: List[Room]) -> Tuple[Tuple[float, float, float, float], List[TextItem]]:
        """Relê o quadro de áreas inteiro, ampliado, em faixas de ~12 linhas.

        No bloco de 512 px o OCR perde linhas e troca letras dos nomes
        ("Observoção"). Lido ampliado da imagem original, o quadro sai
        completo. Devolve (caixa do quadro, textos relidos).
        """
        items = [r.item for r in rows] + [r.area_item for r in rows if r.area_item]
        angle = rows[0].item.angle or 0
        h = float(np.median([it.char_h for it in items]))
        x1, y1 = min(it.x1 for it in items), min(it.y1 for it in items)
        x2, y2 = max(it.x2 for it in items), max(it.y2 for it in items)
        # Folga para pegar a 1ª/última linha perdidas e o cabeçalho.
        across = [it.cy if angle in (0, 180) else it.cx for it in (r.item for r in rows)]
        step = float(np.median(np.diff(sorted(across)))) if len(across) > 2 else 3 * h
        step = step if step > h else 2 * h
        if angle in (0, 180):
            x1, x2, y1, y2 = x1 - 2 * h, x2 + 2 * h, y1 - 2.5 * step, y2 + 2.5 * step
        else:
            x1, x2, y1, y2 = x1 - 2.5 * step, x2 + 2.5 * step, y1 - 2 * h, y2 + 2 * h
        box = (x1, y1, x2, y2)
        span = (y2 - y1) if angle in (0, 180) else (x2 - x1)
        n_chunks = max(1, int(np.ceil(span / (12 * step))))
        self._set_stage("releitura", n_chunks)
        found: List[TextItem] = []
        for i in range(n_chunks):
            a = i * span / n_chunks - (0.6 * step if i else 0)          # sobreposição de meia linha
            b = (i + 1) * span / n_chunks + (0.6 * step if i < n_chunks - 1 else 0)
            chunk = (x1, y1 + a, x2, y1 + b) if angle in (0, 180) else (x1 + a, y1, x1 + b, y2)
            found += self._ocr_zoom(full, scale, chunk, angle, h, max_tokens=768, target_px=26.0)
            self._advance()
        logger.info("Quadro de áreas relido ampliado em %d faixa(s): %d textos", n_chunks, len(found))
        return box, _dedupe(found)

    def _confirm(self, full: Image.Image, scale: float, items: List[TextItem],
                 validate: Optional[Any] = None) -> None:
        """Relê cada número ampliado e só aceita um valor com prova.

        Prova = duas leituras independentes iguais, ou (sem consenso) um único
        candidato que passa em ``validate`` — ex.: igual à soma do quadro por
        pavimento. Sem prova, o número lido NÃO é trocado por um palpite.
        """
        targets: List[TextItem] = []
        for it in items:
            if (it.source == "ocr" and not it.confirmed and it not in targets
                    and _extract_value(it.norm) is not None):
                targets.append(it)
        targets = targets[:MAX_CONFIRMATIONS]
        self._set_stage("releitura", len(targets))
        for item in targets:
            first = _extract_value(item.norm)
            votes: List[float] = [first] if first is not None else []
            for pad in REFINE_PADS:
                value = self._reread(full, scale, item, pad)
                if value is not None:
                    votes.append(value)
                if len(votes) >= 2 and votes[-1] == votes[0]:
                    break
            self._advance()
            if not votes:
                continue
            top, count = Counter(votes).most_common(1)[0]
            checked = [v for v in set(votes) if validate is not None and validate(v)]
            if count >= 2:
                value, item.confirmed = top, True          # duas leituras concordam
            elif len(checked) == 1:
                value, item.confirmed = checked[0], True   # confirmado pela aritmética
                logger.info("Releitura: %.2f confirmado pela soma dos pavimentos (votos: %s)", value, votes)
            else:
                # Sem consenso: NÃO troca o número por um palpite; fica a leitura
                # original, marcada como não confirmada.
                logger.info("Releitura sem consenso para '%s' (votos: %s): mantida a leitura original.",
                            item.text, votes)
                continue
            if first is not None and value != first:
                logger.info("Releitura: '%s' → %.2f (votos: %s)", item.text, value, votes)
                item.set_text(_replace_value(item.text, first, value))

    # ── Visualização ─────────────────────────────────────────

    @staticmethod
    def _draw_box(img: np.ndarray, box: Tuple[int, int, int, int], color, text: str = "", thickness: int = BOX_THICKNESS) -> None:
        x1, y1, x2, y2 = box
        cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
        if not text:
            return
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, FONT_THICKNESS)
        y_top = max(y1 - th - 2 * LABEL_PADDING, 0)
        x_end = min(x1 + tw + 2 * LABEL_PADDING, img.shape[1])
        cv2.rectangle(img, (x1, y_top), (x_end, y1), color, cv2.FILLED)
        cv2.putText(img, text, (x1 + LABEL_PADDING, y1 - LABEL_PADDING),
                    cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, (255, 255, 255), FONT_THICKNESS, cv2.LINE_AA)

    def _render(self, image: Image.Image, items: List[TextItem], reading: PlanReading,
                od_boxes: List[BoundingBox], outline_mask: Optional[np.ndarray] = None) -> np.ndarray:
        base = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        overlay = base.copy()
        if outline_mask is not None:
            # Contorno medido (geometria): desenhado por baixo dos rótulos.
            small = cv2.resize(outline_mask.astype(np.float32), (base.shape[1], base.shape[0]),
                               interpolation=cv2.INTER_AREA)
            contours, _ = cv2.findContours((small > 0.5).astype(np.uint8), cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(overlay, contours, -1, COLOR_PALETTE["contorno"], 3)
        for it in items:
            self._draw_box(overlay, it.box(), COLOR_PALETTE["text"], thickness=1)
        for it in reading.cota_items:
            self._draw_box(overlay, it.box(), COLOR_PALETTE["cota"], thickness=1)
        for box in od_boxes:
            color = next((c for k, c in COLOR_PALETTE.items() if k in box.label.lower()), COLOR_PALETTE["default"])
            self._draw_box(overlay, (box.x1, box.y1, box.x2, box.y2), color, box.label)
        for room in reading.rooms:
            # Fontes Hershey do OpenCV não têm acentos.
            name = normalize(room.name).title()
            if room.area_item is not None:
                self._draw_box(overlay, room.area_item.box(), COLOR_PALETTE["room"],
                               f"{name} {room.area_m2:.2f} m2")
                if room.item is not room.area_item:
                    self._draw_box(overlay, room.item.box(), COLOR_PALETTE["room"], thickness=1)
            else:
                self._draw_box(overlay, room.item.box(), COLOR_PALETTE["room"], name, thickness=1)
        if reading.declared:
            d = reading.declared
            self._draw_box(overlay, d.key_item.box(), COLOR_PALETTE["declared"], thickness=2)
            self._draw_box(overlay, d.value_item.box(), COLOR_PALETTE["declared"],
                           f"Area total {d.value:.2f} m2", thickness=3)
        return cv2.addWeighted(overlay, 0.85, base, 0.15, 0)

    @staticmethod
    def _to_base64(image: np.ndarray) -> str:
        _, buffer = cv2.imencode(".png", image)
        return "data:image/png;base64," + base64.b64encode(buffer).decode("utf-8")

    # ── Contorno das paredes (área medida no desenho) ────────

    @staticmethod
    def _outline_possible(reading: PlanReading) -> bool:
        """Há escala (cotas), ambiente no desenho e um limite superior para conferir?

        Limite: a soma de TODOS os ambientes (o contorno não passa de 1,6× ela) ou
        o retângulo de cotas confiáveis. Sem limite, um contorno que pegou o terreno
        passaria sem aviso — então nem se mede.
        """
        env = reading.envelope
        if env is None or not any(not r.external for r in reading.drawing_rooms):
            return False
        building = reading.building_envelope
        return bool((reading.rooms_sum_m2 and reading.rooms_complete) or env.reliable
                    or (building is not None and building.reliable))

    @staticmethod
    def _door_gap_m(items: List[TextItem]) -> float:
        """Vão a fechar no contorno: a porta mais larga da planta, se ela disser.

        Rótulos de porta "80X220", "90x210" (largura × altura, cm; altura de porta ≥ 200 —
        "160X120" é janela). Sem rótulos, 1,6 m (porta dupla), o valor validado.
        """
        widths = []
        for it in items:
            m = DOOR_LABEL_RE.match(it.text)
            if m and 50 <= int(m.group(1)) <= 200 and 200 <= int(m.group(2)) <= 300:
                widths.append(int(m.group(1)))
        if not widths:
            return DOOR_GAP_M
        return min(max(1.1 * max(widths) / 100, 0.9), DOOR_GAP_M)

    @staticmethod
    def _measure_outline(gray: np.ndarray, f: float, items: List[TextItem],
                         reading: PlanReading) -> Optional[Dict[str, Any]]:
        """Área pelo contorno das paredes (geometry.py), com a escala das cotas.

        ``gray``: o desenho na resolução da geometria; ``f``: px de ``gray`` por px do
        referencial dos textos (o da leitura).
        """
        env = reading.envelope
        building = reading.building_envelope if (reading.building_envelope
                                                 and reading.building_envelope.reliable) else None
        scale = building or env
        px_m = scale.px_per_unit * (100 if scale.unit == "cm" else 1) * f
        rooms = reading.drawing_rooms
        inside = [((r.area_item or r.item).cx * f, (r.area_item or r.item).cy * f) for r in rooms if not r.external]
        outside = [(r.item.cx * f, r.item.cy * f) for r in rooms if r.external]
        boxes = [(it.x1 * f, it.y1 * f, it.x2 * f, it.y2 * f) for it in items]
        door_m = FloorPlanAnalyzer._door_gap_m(items)
        try:
            out = building_outline(gray, boxes, px_m, inside, outside, door_m=door_m)
            # Plano B — paredes de LINHA DUPLA (sem hachura, como nas casas-padrão da
            # PBH): se o contorno normal pegou só um pedaço do prédio (ou nada), mede de
            # novo reconhecendo pares de linhas paralelas, só dentro do retângulo do
            # prédio. Não é o padrão porque cercas de pátio também são linhas duplas.
            if building is not None and building.box and (
                    out is None or out.area_m2 < MIN_OF_BUILDING * building.area_m2):
                pair_box = tuple(v * f for v in building.box)
                second = building_outline(gray, boxes, px_m, inside, outside, door_m=door_m, pair_box=pair_box)
                if second is not None:
                    logger.info("Contorno com paredes de linha dupla: %.1f m² (antes %s).",
                                second.area_m2, f"{out.area_m2:.1f}" if out else "nada")
                    out = second
        except (cv2.error, MemoryError):
            logger.exception("Falha ao medir o contorno das paredes.")
            return None
        if out is None:
            logger.info("Contorno: nenhum bloco fechado com ambiente dentro.")
            return None
        useful = reading.rooms_sum_m2
        limit = env.area_m2 if env.reliable else None
        ok, why = outline_check(out.area_m2, useful, limit, reading.rooms_complete,
                                building.area_m2 if building is not None else None)
        logger.info("Contorno %.1f m² %s%s", out.area_m2, "aceito" if ok else "descartado: ", why)
        ref = building.area_m2 if building is not None else limit
        return {
            "area_m2": round(out.area_m2, 2), "valido": ok, "motivo": why, "blocos": out.blocks,
            "razao_util": round(out.area_m2 / useful, 2) if useful else None,
            "limite_cotas_m2": round(ref, 2) if ref else None,
            "nota": VALIDATION_NOTE, "usado": False,
            "_mask": out.mask,
        }

    def _outline_pdf(self, data: bytes, page: Any, reading: PlanReading) -> Optional[Dict[str, Any]]:
        if not self._outline_possible(reading):
            return None
        try:
            gray = render_gray(data, page.page_index, page.rotation)
        except Exception:                       # noqa: BLE001 — o contorno é complementar
            logger.exception("Falha ao desenhar a página para medir o contorno.")
            return None
        return self._measure_outline(gray, gray.shape[1] / page.image.size[0], page.items, reading)

    def _outline_image(self, full: Image.Image, work_width: int, items: List[TextItem],
                       reading: PlanReading) -> Optional[Dict[str, Any]]:
        if not self._outline_possible(reading):
            return None
        g = full.convert("L")
        n = g.width * g.height
        if n > GEOMETRY_MAX_PIXELS:
            s = (GEOMETRY_MAX_PIXELS / n) ** 0.5
            g = g.resize((max(round(g.width * s), 1), max(round(g.height * s), 1)), Image.LANCZOS)
        gray = np.asarray(g)
        background = float(np.median(gray[::8, ::8]))
        if background < OUTLINE_MIN_BACKGROUND:
            logger.info("Contorno não medido: fundo da imagem não é branco (mediana %.0f).", background)
            return None
        return self._measure_outline(gray, gray.shape[1] / work_width, items, reading)

    @staticmethod
    def _apply_outline(report: AnalysisReport, contorno: Optional[Dict[str, Any]]) -> None:
        """Contorno no relatório: resposta só quando a planta não escreve área nenhuma."""
        if not contorno:
            return
        info = {k: v for k, v in contorno.items() if not k.startswith("_")}
        report.contorno = info
        area = _fmt_area(info["area_m2"])
        if not info["valido"]:
            report.warnings.append(f"Contorno das paredes medido no desenho ({area} m²) descartado: {info['motivo']}.")
            return
        # Soma de poucos ambientes (a maioria sem área escrita) não é a área do pavimento.
        partial_sum = (report.area_method == "areas_declaradas" and report.qtd_comodos >= 5
                       and report.rooms_with_area < 0.5 * report.qtd_comodos)
        if report.area_method == "cotas" or partial_sum:
            # Sem área escrita (ou quase): a medida do desenho é a melhor resposta.
            before = report.area_m2
            report.warnings = [w for w in report.warnings
                               if not w.startswith(("Área pela envoltória das cotas", "Esta é a área útil"))]
            report.area_m2, report.area_kind = info["area_m2"], "construida"
            report.area_method, report.confidence = "contorno", "baixa"
            info["usado"] = True
            if partial_sum:
                why = (f"Só {report.rooms_with_area} de {report.qtd_comodos} ambientes têm área escrita "
                       f"(soma de {_fmt_area(before)} m², parcial). ")
                limit = (f" O retângulo das cotas ({_fmt_area(info['limite_cotas_m2'])} m²) é o limite superior."
                         if info.get("limite_cotas_m2") else "")
            else:
                why = ""
                limit = f" O retângulo das cotas ({_fmt_area(before)} m²) é o limite superior."
            report.warnings.insert(0, (
                f"{why}Área medida no próprio desenho, pelo contorno das paredes, com a escala das cotas: "
                f"{area} m². É uma estimativa: {VALIDATION_NOTE}.{limit}"
            ))
            return
        if report.area_method == "carimbo" and report.area_m2:
            compare = f"{info['area_m2'] / report.area_m2:.0%} da área do carimbo"
        elif info["razao_util"]:
            compare = f"{info['razao_util']:.2f}× a soma dos ambientes".replace(".", ",")
        else:
            compare = "sem outra área para comparar"
        report.checks.append(
            f"Área construída estimada pelo contorno das paredes, medida no desenho: {area} m² "
            f"({compare}; {VALIDATION_NOTE})."
        )

    # ── Pipeline Completo ────────────────────────────────────

    def analyze(
        self,
        data: bytes,
        filename: str = "upload.png",
        tracker: Optional[ProgressTracker] = None,
        page_number: Optional[int] = None,
        document: Any = "procurar",
    ) -> AnalysisReport:
        """Lê uma planta (PDF ou imagem). ``page_number`` escolhe a página do PDF (1 = primeira).

        PDF com texto não usa IA: é lido na hora, sem entrar na fila.
        Imagens (e PDFs escaneados) usam o Florence-2, uma análise por vez;
        as demais esperam (o tracker fica na etapa 'fila' até a vez delas).

        Raises:
            AnalysisCancelled: se ``tracker.cancel()`` for chamado.
            RuntimeError: arquivo inválido ou falha de inferência.
        """
        logger.info("═══ Iniciando análise: '%s' ═══", filename)
        warnings: List[str] = []
        page_info: Optional[Dict[str, int]] = None
        scan_document = document == "procurar"      # leitura de várias páginas já procurou uma vez
        if scan_document:
            document = None
        # Prazo de execução escrito no próprio documento (edital, memorial): mostrado ao
        # lado do prazo previsto pelo modelo. A leitura de várias páginas busca uma vez.
        prazo_doc = scan_document_prazo(data) if scan_document and is_pdf(data) else None

        if is_pdf(data):
            if tracker:
                tracker.set_stage("pdf", 1)
            page = read_pdf(data, page_number)
            if tracker:
                tracker.advance()
            page_info = {"numero": page.page_index + 1, "total": page.page_count}
            if page.page_count > 1:
                # Total do empreendimento escrito em outra página (quadro, memorial).
                if scan_document:
                    document = choose_document_area(scan_document_areas(data))
                why = "escolhida" if page_number else (
                    "a primeira que cita “planta baixa”" if page.has_text else
                    "a primeira; o PDF não tem texto para identificar a planta — escolha outra se preciso")
                warnings.append(
                    f"O PDF tem {page.page_count} páginas; foi lida a página {page.page_index + 1} ({why})."
                )
            if page.has_text:
                if tracker:
                    tracker.set_stage("calculo")
                reading = interpret(page.items, np.asarray(page.image.convert("L")))
                contorno = self._outline_pdf(data, page, reading)
                original = self._to_base64(cv2.cvtColor(np.array(page.image), cv2.COLOR_RGB2BGR))
                result = self._apply_document(self._build_report(
                    page.image, page.items, reading, [], "pdf", page.rotation,
                    warnings, page_info, original, contorno,
                ), document)
                result.prazo_documento = prazo_doc
                return result
            warnings.append("PDF sem texto (escaneado ou com as letras em traços): a leitura foi feita por OCR.")
            image = page.image
        else:
            try:
                image = Image.open(io.BytesIO(data)).convert("RGB")
            except Exception as exc:
                raise RuntimeError(f"Erro ao decodificar imagem: {exc}") from exc

        with self._lock:
            self._tracker = tracker
            try:
                result = self._apply_document(self._analyze_image(image, filename, warnings, page_info), document)
                result.prazo_documento = prazo_doc
                return result
            finally:
                self._tracker = None

    def analyze_pages(
        self, data: bytes, filename: str, tracker: Optional[ProgressTracker], pages: List[int],
    ) -> AnalysisReport:
        """Lê várias páginas de um PDF (um prédio: subsolos, térreo, tipo ×N...) e soma.

        Cada página é lida como uma planta. O total só é dado se TODAS tiverem área;
        senão, a tabela mostra o que foi lido e quais páginas faltam (sem soma parcial
        alimentando prazo e custo).
        """
        document = choose_document_area(scan_document_areas(data))
        reports: List[Tuple[int, AnalysisReport]] = []
        for k, n in enumerate(pages, 1):
            if tracker:
                tracker.page_label = f"página {n} ({k} de {len(pages)})"
            reports.append((n, self.analyze(data, filename, tracker, n, document=document)))
        if tracker:
            tracker.page_label = None
        result = self._combine_pages(reports, document)
        result.prazo_documento = scan_document_prazo(data)
        return result

    @staticmethod
    def _combine_pages(reports: List[Tuple[int, AnalysisReport]], document: Optional[Dict[str, Any]]) -> AnalysisReport:
        floors: List[Dict[str, Any]] = []
        for n, rep in reports:
            own = rep.pagina_area or {"area_m2": rep.area_m2, "area_kind": rep.area_kind,
                                      "area_method": rep.area_method, "confidence": rep.confidence}
            floors.append({"pagina": n, **own,
                           "multiplicador": (rep.multiplicador or {}).get("vezes", 1),
                           "texto_multiplicador": (rep.multiplicador or {}).get("texto")})
        with_area = [f for f in floors if f["area_m2"]]
        missing = [f["pagina"] for f in floors if not f["area_m2"]]
        kinds = {f["area_kind"] for f in with_area}
        soma = sum(f["area_m2"] * f["multiplicador"] for f in with_area)
        predio = {
            "pavimentos": floors,
            "soma_m2": round(soma, 2) if with_area else None,
            "tipo_area": "construida" if kinds == {"construida"} else ("util" if kinds else None),
            "completo": not missing,
            "sem_area": missing,
        }
        base = next((rep for _, rep in reports if (rep.pagina_area or {}).get("area_m2") or rep.area_m2),
                    reports[0][1])
        base.predio = predio
        # Ambientes do prédio inteiro: os de cada página (marcados com ela), e as
        # contagens e somas com os pavimentos repetidos multiplicados.
        times = {n: (rep.multiplicador or {}).get("vezes", 1) for n, rep in reports}
        base.rooms = [{**room, "pagina": n} for n, rep in reports for room in rep.rooms]
        base.qtd_comodos = sum(rep.qtd_comodos * times[n] for n, rep in reports)
        base.rooms_with_area = sum(rep.rooms_with_area * times[n] for n, rep in reports)
        sums = [(rep.rooms_sum_m2 or 0.0) * times[n] for n, rep in reports]
        base.rooms_sum_m2 = round(sum(sums), 2) if any(sums) else None
        base.area_externa_m2 = round(sum(rep.area_externa_m2 * times[n] for n, rep in reports), 2)
        fmt = lambda v: f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # noqa: E731
        if document:
            return base                       # o total escrito no documento já é a área (exata)
        if missing:
            base.area_m2, base.area_kind, base.area_method, base.confidence = None, None, "indisponivel", "baixa"
            base.warnings.insert(0, (
                f"Total do prédio incompleto: {len(missing)} página(s) sem área legível "
                f"({', '.join(str(p) for p in missing)}). "
                + (f"Soma do que foi lido: {fmt(soma)} m² — não usada no prazo e no custo." if with_area else "")
            ))
        else:
            base.area_m2 = round(soma, 2)
            base.area_kind = predio["tipo_area"]
            base.area_method = "pavimentos"
            order = {"alta": 2, "media": 1, "baixa": 0}
            base.confidence = min((f["confidence"] for f in floors), key=lambda c: order.get(c, 0))
            base.checks.insert(0, f"Soma de {len(floors)} página(s) de planta, com os pavimentos repetidos: {fmt(soma)} m².")
        return base

    @staticmethod
    def _apply_document(report: AnalysisReport, document: Optional[Dict[str, Any]]) -> AnalysisReport:
        """Usa o total do empreendimento escrito no documento quando a página não tem total.

        A leitura da própria página (um pavimento, às vezes só a área útil) continua
        no relatório, em ``pagina_area``, e nas notas.
        """
        if not document:
            return report
        report.documento = document
        pages = document["paginas"]
        where = (f"página {pages[0]}" if len(pages) == 1
                 else "páginas " + ", ".join(str(p) for p in pages[:-1]) + f" e {pages[-1]}")
        if report.area_method == "carimbo":
            if report.area_kind == "privativa":
                # Planta de venda: a página traz a área da UNIDADE; o documento, a do prédio.
                report.checks.append(
                    f"O documento declara {document['valor']:,.2f} m² na {where} (“{document['texto']}”): "
                    "é a área do empreendimento inteiro; a desta página é a da unidade."
                    .replace(",", "X").replace(".", ",").replace("X", "."))
            elif abs(report.area_m2 - document["valor"]) > 0.005 * document["valor"]:
                report.warnings.append(
                    f"O documento declara {document['valor']:,.2f} m² na {where} (“{document['texto']}”), "
                    "diferente do total desta folha: confira a qual parte da obra cada um se refere."
                    .replace(",", "X").replace(".", ",").replace("X", "."))
            return report
        report.pagina_area = {"area_m2": report.area_m2, "area_kind": report.area_kind,
                              "area_method": report.area_method, "confidence": report.confidence}
        lida = (f"{report.area_m2:,.2f} m² ({'área útil' if report.area_kind == 'util' else report.area_kind})"
                .replace(",", "X").replace(".", ",").replace("X", ".")) if report.area_m2 else "sem área legível"
        report.area_m2 = document["valor"]
        report.area_kind = "construida"
        report.area_method = "documento"
        if not document["outros_valores"]:
            report.confidence = "alta" if len(pages) >= 2 else "media"
        else:
            # Várias áreas diferentes (opções de projeto, ampliação...): a que mais se
            # repete, ou a primeira — escolha fraca, dita como tal.
            report.confidence = "media" if document.get("repetido") else "baixa"
        # O pedido de área manual da página não vale mais: o documento dá a área.
        report.warnings = [w for w in report.warnings if "Informe a área manualmente" not in w]
        report.checks.insert(0, f"Área construída do empreendimento escrita no documento ({where}): “{document['texto']}”.")
        numero = (report.page or {}).get("numero")
        report.warnings.insert(0, f"Na página lida{f' ({numero})' if numero else ''}: {lida}. "
                                  "O total acima, escrito no documento, vale para o empreendimento inteiro.")
        if document["outros_valores"]:
            outros = ", ".join(f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                               for v in document["outros_valores"][:4])
            why = ("é a que mais se repete" if document.get("repetido")
                   else "é a primeira citada — as outras podem ser opções de projeto ou ampliação")
            report.warnings.insert(1, f"O documento traz áreas construídas diferentes (também {outros} m²); "
                                      f"a usada {why}. Confira qual vale para a obra.")
        return report

    def _analyze_image(
        self, full: Image.Image, filename: str,
        warnings: List[str], page_info: Optional[Dict[str, int]],
    ) -> AnalysisReport:
        """Leitura por OCR: passes em blocos, interpretação e conferência."""
        self._set_stage("preparo")
        # O tamanho dos blocos foi calibrado para ~2000 px no lado maior;
        # a imagem original fica guardada para as releituras ampliadas.
        factor = self._work_factor(full)
        work = full
        if abs(factor - 1) > 0.05:
            work = full.resize((round(full.width * factor), round(full.height * factor)), Image.LANCZOS)

        od_boxes: List[BoundingBox] = []
        engine = "Florence-2"
        try:
            # 1ª leitura: PP-OCRv6 (rápido, cada linha com sua orientação); sem ele,
            # os passes do Florence-2 a 0°/90° (e 180°/270° se preciso).
            fast = self._read_fast(full, work)
            if fast is not None:
                rotation, items = 0, fast
                for stage in ("ocr", "ocr_vertical", "ocr_extra"):
                    self._skip_stage(stage)
                engine = f"{self._fast_ocr.model} + Florence-2"
            else:
                rotation, items, ocr_warnings = self._read_image_text(work)
                warnings += ocr_warnings
            if rotation:
                work = work.rotate(rotation, expand=True)
                full = full.rotate(rotation, expand=True)
            scale = full.width / work.width          # px da original por px de trabalho
            gray = np.asarray(work.convert("L"))
            # Rótulos lidos em pedaços ("9,90" + "m²") viram um texto só ANTES da
            # interpretação: assim a releitura confere o mesmo objeto que soma a área.
            items = merge_area_units(items)
            reading = interpret(items, gray)

            # Leitura rápida que deixou VÁRIOS ambientes sem área (letras minúsculas, como
            # na planta A5 do TCC): o Florence-2 lê a prancha também e preenche o que o
            # PP-OCRv6 não leu (_complement: onde os dois leram, fica o PP-OCRv6). No TCC,
            # 349,83 (só PP-OCRv6) e 372,75 (só Florence-2) viraram ~390 m² (real ≈ 413).
            # Com carimbo ou quadro completo, não precisa. Gatilho: 3+ ambientes e 5%+
            # sem área (TCC: 5 de 56; Pronto Socorro PARQ, completo: 0 de 51).
            if fast is not None and reading.method == "areas_declaradas":
                internal = [r for r in reading.rooms if not r.external]
                missing = sum(1 for r in internal if r.area_m2 is None)
                if missing >= 3 and missing >= 0.05 * len(internal):
                    logger.info("Leitura rápida com %d de %d ambientes sem área: o Florence-2 completa.",
                                missing, len(internal))
                    rot2, more, more_warnings = self._read_image_text(work)
                    if rot2 == 0:
                        items = merge_area_units(_complement(items, merge_area_units(more)))
                        reading = interpret(items, gray)
                        engine += " (leitura completa)"
                    else:
                        warnings += more_warnings

            # Área = soma dos rótulos: o rótulo mal lido ("A55m") fica fora da soma.
            # Relê ampliado o que tem cara de rótulo de área e não virou número.
            if (reading.method == "areas_declaradas"
                    and not any(r.area_source == "quadro" for r in reading.rooms)):
                if self._recover_labels(full, scale, items, reading):
                    reading = interpret(items, gray)

            # Carimbo com rótulo legível e valor ilegível: relê a célula ampliada.
            if reading.declared is None and reading.declared_key is not None:
                self._set_stage("releitura", 1)
                extra = self._read_declared_cell(full, scale, reading.declared_key)
                self._advance()
                if extra:
                    items = _dedupe(items + extra)
                    reading = interpret(items, gray)

            # Quadro de áreas: relê a tabela inteira ampliada (linhas perdidas,
            # nomes com letras trocadas) e substitui a leitura dos blocos.
            table_rows = [r for r in reading.rooms if r.area_source == "quadro"]
            if table_rows:
                (tx1, ty1, tx2, ty2), reread = self._reread_table(full, scale, table_rows)
                if reread:
                    def inside(it: TextItem) -> bool:
                        return tx1 <= it.cx <= tx2 and ty1 <= it.cy <= ty2
                    candidate = [it for it in items if not inside(it)] + reread
                    new_reading = interpret(candidate, gray)
                    # Só troca se a releitura achou pelo menos as mesmas linhas. Depois do
                    # PP-OCRv6 (que leu o quadro do Pronto Socorro exato), só com MAIS linhas.
                    new_rows = sum(r.area_source == "quadro" for r in new_reading.rooms)
                    if new_rows > len(table_rows) or (fast is None and new_rows == len(table_rows)):
                        logger.info("Quadro: %d → %d linhas após a releitura ampliada.", len(table_rows), new_rows)
                        items, reading = candidate, new_reading

            # Confere primeiro o que define a área; depois os rótulos dos ambientes.
            to_confirm = list(reading.evidence) if reading.method != "cotas" else []
            to_confirm += [r.area_item for r in reading.rooms if r.area_item is not None]
            if to_confirm:
                floors = reading.floors_sum
                validate = (lambda v: abs(v - floors) <= 0.005 * floors) if floors else None
                self._confirm(full, scale, to_confirm, validate)
                reading = interpret(items, gray)
            else:
                self._skip_stage("releitura")

            # Detecção de objetos é complementar: se falhar, segue sem ela.
            self._set_stage("deteccao", 1)
            try:
                od = self._run_single_task(work, "<OD>").get("<OD>", {})
                od_boxes = [
                    BoundingBox(int(b[0]), int(b[1]), int(b[2]), int(b[3]), label)
                    for b, label in zip(od.get("bboxes", []), od.get("labels", []))
                ]
            except Exception:
                logger.exception("Falha na detecção de objetos (OD).")
                warnings.append("Detecção de objetos falhou; área e cômodos vieram do texto.")
            self._advance()
        except AnalysisCancelled:
            logger.info("Análise cancelada pelo usuário: '%s'", filename)
            raise
        except Exception as exc:
            logger.exception("Falha na inferência do Florence-2 (OCR).")
            raise RuntimeError(f"Falha na inferência do Florence-2: {exc}") from exc

        self._set_stage("calculo")
        contorno = self._outline_image(full, work.width, items, reading)
        report = self._build_report(work, items, reading, od_boxes, "ocr", rotation,
                                    warnings, page_info, None, contorno)
        report.ocr_engine = engine
        return report

    def _build_report(
        self, image: Image.Image, items: List[TextItem], reading: PlanReading,
        od_boxes: List[BoundingBox], text_source: str, orientation: int,
        warnings: List[str], page_info: Optional[Dict[str, int]],
        original_b64: Optional[str], contorno: Optional[Dict[str, Any]] = None,
    ) -> AnalysisReport:
        mask = contorno["_mask"] if contorno and contorno["valido"] else None
        annotated = self._render(image, items, reading, od_boxes, mask)
        report = AnalysisReport(
            area_m2=reading.area_m2,
            area_kind=reading.area_kind,
            qtd_comodos=reading.qtd_comodos,
            total_detections=len(od_boxes),
            detection_summary=dict(Counter(b.label for b in od_boxes)),
            ocr_texts=[it.text for it in items],
            annotated_image_b64=self._to_base64(annotated),
            od_boxes=[b.to_dict() for b in od_boxes],
            ocr_boxes=[
                {"x1": round(it.x1, 1), "y1": round(it.y1, 1), "x2": round(it.x2, 1), "y2": round(it.y2, 1),
                 "label": it.text, "angle": it.angle, "source": it.source, "confirmed": it.confirmed}
                for it in items
            ],
            area_method=reading.method,
            confidence=reading.confidence,
            rooms=[r.to_dict() for r in reading.rooms],
            area_externa_m2=reading.area_externa_m2,
            orientation_deg=orientation,
            text_source=text_source,
            checks=reading.checks,
            warnings=reading.warnings + warnings,
            declared_area=(
                {"valor_m2": reading.declared.value, "rotulo": reading.declared.label,
                 "exato": reading.declared.value_item.source == "pdf",
                 "conferido": reading.declared.reliable}
                if reading.declared else None
            ),
            rooms_sum_m2=reading.rooms_sum_m2,
            rooms_with_area=reading.rooms_with_area,
            envelope=(
                {"largura_m": round(reading.envelope.largura_m, 2),
                 "profundidade_m": round(reading.envelope.profundidade_m, 2),
                 "area_m2": round(reading.envelope.area_m2, 2),
                 "unidade": reading.envelope.unit, "cadeias": reading.envelope.chains,
                 "confiavel": reading.envelope.reliable}
                if reading.envelope else None
            ),
            original_image_b64=original_b64,
            page=page_info,
        )
        self._apply_outline(report, contorno)
        mult = floor_multiplier(items)
        if mult:
            report.multiplicador = {"vezes": mult[0], "texto": mult[1]}
            report.checks.append(f"O título indica pavimento repetido {mult[0]}× (“{mult[1]}”).")
        logger.info(
            "═══ Leitura concluída: %s m² (%s, confiança %s, fonte %s) ═══",
            f"{reading.area_m2:.2f}" if reading.area_m2 else "—",
            reading.method, reading.confidence, text_source,
        )
        return report
