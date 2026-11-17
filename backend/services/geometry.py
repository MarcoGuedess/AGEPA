# ============================================================
# AGEPA — Geometria da planta: contorno do prédio
# services/geometry.py
# ============================================================
#
# Mede a área ocupada pelo prédio NO DESENHO, sem depender de um
# total escrito: paredes → contorno → px² → m² (escala pelas cotas).
#
# Como funciona:
#   1. tinta = pixels não brancos, sem os textos (caixas do PDF/OCR);
#   2. hachuras e linhas duplas próximas viram faixa (fechamento ~3 cm);
#   3. paredes = só as faixas espessas (abertura maior que as linhas
#      finas: cotas, eixos, móveis, texto); a moldura da folha sai;
#   4. vãos de porta/janela de até ~1,5 m são fechados (dilatação);
#   5. "fora" = espaço livre ligado à borda da folha ou a um texto de
#      área externa (jardim, calçada, playground, pátio...);
#   6. prédio = blocos não alcançados que contêm ambientes internos;
#      a dilatação é desfeita (erosão) e a área vira m² pela escala.
#
# Validação (gabarito, 14/11/2026, desenho a 8 px/mm, vão de 1,6 m):
# Cantina 0,892×, creches FNDE 0,953× e 0,957×, escola UFU 0,818× da
# área construída real — sempre ABAIXO (paredes finas e janelas largas
# deixam pedaços de fora). Na PARQ ficou menor que a soma dos ambientes
# e foi descartado. Por isso o resultado é sempre uma ESTIMATIVA de
# confiança baixa, e é descartado se ficar menor que a soma dos
# ambientes, maior que 1,6× essa soma ou maior que o retângulo das cotas.
# ============================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

import cv2
import numpy as np

logger = logging.getLogger("agepa.geometry")

DOOR_GAP_M = 1.6        # vãos fechados (porta, janela). 1,5 m é largura comum de porta dupla:
                        # no limite, o vão ora fecha, ora não (escola UFU: 0,72× ↔ 0,82×)
MIN_PART_M2 = 4.0       # blocos menores que isso não contam (símbolos, pilares soltos)
# Parede desenhada como DUAS linhas finas paralelas, sem hachura (plantas-padrão da
# Prefeitura de BH): o vão entre linhas paralelas a até esta distância vira parede.
WALL_PAIR_M = 0.30
PAIR_MIN_LEN_M = 0.40   # linha mais curta que isso não forma parede (traço de tracejado)
# Área construída / soma dos ambientes no gabarito: 1,27 a 1,29 (paredes e shafts).
# Acima de 1,6× o contorno pegou área de fora (muros do terreno, estacionamento).
MAX_OVER_USEFUL = 1.6
# Contorno × retângulo do prédio pelas cotas: abaixo de 35% o contorno pegou só um
# pedaço (paredes não reconhecidas); acima de 112% pegou área de fora. (A cadeia
# interna das cotas fica ~11% menor em área que a externa: daí a folga.)
MIN_OF_BUILDING = 0.35
MAX_OF_BUILDING = 1.12
VALIDATION_NOTE = ("nas 19 plantas de teste errou de 18% para menos a 5% para mais "
                   "(erro médio de 4%)")


@dataclass
class Outline:
    area_m2: float
    blocks: int
    px_per_m: float
    mask: np.ndarray          # 1 = prédio (para desenhar na imagem anotada)


def _line_width_px(ink: np.ndarray) -> float:
    """Espessura das linhas FINAS do desenho (px).

    Larguras medidas no eixo dos traços (2 × distância à borda). Vale a menor entre
    a moda — certa nas plantas reais, onde as linhas finas dominam — e o quartil
    inferior, certo num desenho em que quase toda a tinta é parede grossa.
    """
    dt = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    ridge = (dt > 0) & (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8)) - 1e-6)
    vals = 2 * dt[ridge]
    if vals.size == 0:
        return 2.0
    hist, edges = np.histogram(vals, bins=np.arange(0.5, 60, 0.5))
    mode = float(edges[np.argmax(hist)] + 0.25)
    return min(mode, float(np.percentile(vals, 25)))


def _grow(mask: np.ndarray, r: float) -> np.ndarray:
    """Dilatação por um disco de raio r (px), pela distância euclidiana (rápida em folhas grandes)."""
    dist = cv2.distanceTransform((mask == 0).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    return (dist <= r).astype(np.uint8)


def _shrink(mask: np.ndarray, r: float) -> np.ndarray:
    """Erosão por um disco de raio r (px)."""
    dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    return (dist > r).astype(np.uint8)


def building_outline(
    gray: np.ndarray,
    text_boxes: Iterable[Tuple[float, float, float, float]],
    px_per_m: float,
    inside: Sequence[Tuple[float, float]],
    outside: Sequence[Tuple[float, float]] = (),
    door_m: float = DOOR_GAP_M,
    pair_m: float = WALL_PAIR_M,
    pair_box: Optional[Tuple[float, float, float, float]] = None,
) -> Optional[Outline]:
    """Área ocupada pelo prédio, em m² (None se não houver bloco com ambiente).

    ``gray``: desenho em tons de cinza; ``text_boxes``: caixas (x1, y1, x2, y2) dos
    textos, no mesmo referencial; ``px_per_m``: escala (px por metro), das cotas;
    ``inside``/``outside``: pontos de ambientes internos e de áreas externas.
    """
    if px_per_m <= 0 or not inside:
        return None
    h, w = gray.shape
    ink = (gray < 225).astype(np.uint8)
    for x1, y1, x2, y2 in text_boxes:
        ink[max(int(y1) - 2, 0):int(y2) + 3, max(int(x1) - 2, 0):int(x2) + 3] = 0
    c = max(int(round(0.03 * px_per_m)), 1)
    solid = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * c + 1, 2 * c + 1)))
    if pair_m > 0 and pair_box is not None:
        # Fechamento na direção perpendicular às linhas: duas horizontais próximas viram
        # faixa (kernel vertical) e duas verticais também (kernel horizontal). Linha
        # isolada continua fina e sai na abertura abaixo. Só DENTRO do retângulo do
        # prédio (cotas): fora dele, as duas linhas paralelas de uma cadeia de cotas
        # (interna e externa) virariam um "muro" em volta da casa.
        span = max(int(round(pair_m * px_per_m)), 3)
        long_px = max(int(round(PAIR_MIN_LEN_M * px_per_m)), 5)
        m = 0.3 * px_per_m
        bx1, by1 = max(int(pair_box[0] - m), 0), max(int(pair_box[1] - m), 0)
        bx2, by2 = min(int(pair_box[2] + m) + 1, w), min(int(pair_box[3] + m) + 1, h)
        if bx2 > bx1 and by2 > by1:
            region = ink[by1:by2, bx1:bx2]
            # Só linhas LONGAS formam par (traços de linha tracejada — projeção do
            # telhado, eixos — ficam de fora): horizontais com horizontais, verticais
            # com verticais.
            for along, across in (((long_px, 1), (1, span + 1)), ((1, long_px), (span + 1, 1))):
                lines = cv2.morphologyEx(region, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, along))
                solid[by1:by2, bx1:bx2] |= cv2.morphologyEx(
                    lines, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, across))
    k = max(int(round(max(2.5 * _line_width_px(ink), 0.07 * px_per_m))), 3)
    walls = cv2.morphologyEx(solid, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    nw, labw, statw, _ = cv2.connectedComponentsWithStats(walls, connectivity=8)
    for i in range(1, nw):                                    # moldura da folha
        if statw[i][2] > 0.9 * w or statw[i][3] > 0.9 * h:
            walls[labw == i] = 0
    r = max(door_m / 2 * px_per_m, 2.0)
    free = (1 - _grow(walls, r)).astype(np.uint8)
    _, lab = cv2.connectedComponents(free, connectivity=4)
    out_ids = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    for x, y in outside:
        xi, yi = int(x), int(y)
        if 0 <= yi < h and 0 <= xi < w and lab[yi, xi]:
            out_ids.add(int(lab[yi, xi]))
    body = _shrink((~np.isin(lab, list(out_ids))).astype(np.uint8), r)
    del lab, free
    n2, lab2, stats, _ = cv2.connectedComponentsWithStats(body, connectivity=8)
    seeds = {int(lab2[int(y), int(x)]) for x, y in inside if 0 <= int(y) < h and 0 <= int(x) < w} - {0}
    keep = [i for i in seeds
            if stats[i][4] / px_per_m ** 2 >= MIN_PART_M2 and stats[i][2] < 0.95 * w and stats[i][3] < 0.95 * h]
    if not keep:
        return None
    mask = np.isin(lab2, keep).astype(np.uint8)
    if pair_box is not None:
        # As faces externas do prédio estão nas cotas: nada do contorno passa delas
        # (beiral, calçada e linhas coladas à parede engordariam a medida).
        g = max(int(0.05 * px_per_m), 1)
        cx1, cy1 = max(int(pair_box[0]) - g, 0), max(int(pair_box[1]) - g, 0)
        cx2, cy2 = min(int(pair_box[2]) + g + 1, w), min(int(pair_box[3]) + g + 1, h)
        clip = np.zeros_like(mask)
        clip[cy1:cy2, cx1:cx2] = 1
        mask &= clip
    area = float(mask.sum()) / px_per_m ** 2
    logger.info("Contorno: %.1f m² em %d bloco(s) (escala %.1f px/m, abertura %d px).", area, len(keep), px_per_m, k)
    return Outline(area_m2=area, blocks=len(keep), px_per_m=px_per_m, mask=mask)


def outline_check(
    area: float, useful: Optional[float], envelope: Optional[float], useful_complete: bool = True,
    building: Optional[float] = None,
) -> Tuple[bool, str]:
    """O contorno é plausível? Conferido com a soma dos ambientes e com o retângulo das cotas.

    ``useful_complete``: todos os ambientes têm área (senão a soma é só um piso).
    ``building``: retângulo do PRÉDIO pelas cotas (as cadeias em volta dos ambientes):
    teto e piso — um prédio ocupa de 35% (pátios, recortes) a 100% do seu retângulo.
    """
    if useful and area < useful:
        return False, "menor que a soma dos ambientes — o contorno ficou incompleto"
    if useful and useful_complete and area > MAX_OVER_USEFUL * useful:
        return False, (f"mais de {MAX_OVER_USEFUL:.1f}× a soma dos ambientes — "
                       "o contorno incluiu área de fora do prédio").replace(".", ",", 1)
    if building:
        if area > building * MAX_OF_BUILDING:
            return False, "maior que o retângulo do prédio pelas cotas"
        if area < building * MIN_OF_BUILDING:
            return False, "menor que um terço do retângulo do prédio pelas cotas — o contorno ficou incompleto"
    elif envelope and area > envelope * 1.05:
        return False, "maior que o retângulo das cotas"
    return True, ""
