# ============================================================
# AGEPA — Leitura de PDF (texto vetorial)
# services/pdf_reader.py
# ============================================================
#
# PDFs exportados do CAD guardam cada texto da prancha (áreas,
# cotas, nomes, carimbo) como texto de verdade, com posição e
# direção. Lendo esse texto direto não há OCR, portanto não há
# dígito trocado nem palavra inventada — e leva segundos.
#
# PDFs escaneados (só imagem) não têm texto: nesse caso a página
# é renderizada e segue pelo OCR do Florence-2 (ai_service.py).
#
# Biblioteca: pypdfium2 (motor PDFium do Chrome, licença Apache/BSD).
# ============================================================

from __future__ import annotations

import ctypes
import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image

from services.plan_reader import (
    NUM, UNIT_CONTEXT_RE, TextItem, area_context_excluded, find_declared_area, normalize, parse_number,
    prose_built_areas, prose_prazos,
)

logger = logging.getLogger("agepa.pdf_reader")

RENDER_LONG_SIDE: int = 2400      # px no lado maior da página renderizada (PDF com texto)
# PDF sem texto (escaneado, ou CAD que exportou as letras como traços): a
# página vai para o OCR, que precisa de resolução — no mínimo ~10 px/mm (≈ 250 dpi).
SCAN_PX_PER_MM: float = 10.0
SCAN_MAX_LONG_SIDE: int = 12000
# Desenho VETORIAL em folha pequena (prancha reduzida para A3/A4/A5): as letras ficam
# com décimos de milímetro; como o vetor não perde nitidez, desenha-se com mais
# px/mm, até este lado maior (A5 → 40 px/mm). Folhas grandes (A1, A0) continuam a 10 px/mm.
VECTOR_TARGET_LONG_SIDE: int = 8400
VECTOR_MAX_PX_PER_MM: float = 40.0
# Página escaneada (uma imagem cobrindo a folha): usa a resolução da própria imagem.
RASTER_COVER_FRACTION: float = 0.5
MIN_TEXT_CHARS: int = 20          # abaixo disso o PDF é tratado como escaneado
# Desenho para medir o contorno das paredes (geometry.py): validado a 8 px/mm;
# folhas muito grandes descem de resolução para caber neste número de pixels.
GEOMETRY_PX_PER_MM: float = 8.0
GEOMETRY_MAX_PIXELS: int = 80_000_000
ANGLE_SNAP_DEG: float = 12.0      # tolerância para considerar o texto a 0/90/180/270°


def is_pdf(data: bytes) -> bool:
    return data.find(b"%PDF-", 0, 1024) >= 0


@dataclass
class PdfPage:
    image: Image.Image            # página renderizada, já "em pé"
    items: List[TextItem]         # textos no mesmo quadro da imagem
    rotation: int                 # graus (anti-horário) aplicados para ficar em pé
    page_index: int
    page_count: int

    @property
    def has_text(self) -> bool:
        return sum(len(it.text.strip()) for it in self.items) >= MIN_TEXT_CHARS


# ── Rotação de pontos (mesma convenção do PIL.Image.rotate, expand=True) ──

def rotate_point(x: float, y: float, rot: int, w: float, h: float) -> Tuple[float, float]:
    """Ponto da imagem original (w×h) na imagem girada `rot` graus anti-horário."""
    if rot == 90:
        return y, w - x
    if rot == 180:
        return w - x, h - y
    if rot == 270:
        return h - y, x
    return x, y


def unrotate_point(x: float, y: float, rot: int, w: float, h: float) -> Tuple[float, float]:
    """Inverso de rotate_point (w×h são as dimensões da imagem ORIGINAL)."""
    if rot == 90:
        return w - y, x
    if rot == 180:
        return w - x, h - y
    if rot == 270:
        return y, h - x
    return x, y


def _snap(angle_deg: float) -> Optional[int]:
    a = angle_deg % 360
    nearest = int(round(a / 90.0)) * 90 % 360
    diff = min(abs(a - nearest), 360 - abs(a - nearest))
    return nearest if diff <= ANGLE_SNAP_DEG else None


# ── Extração ─────────────────────────────────────────────────

def _page_to_device(page, size: Tuple[int, int], x: float, y: float) -> Tuple[float, float]:
    dx, dy = ctypes.c_int(), ctypes.c_int()
    pdfium_c.FPDF_PageToDevice(
        page.raw, 0, 0, size[0], size[1], 0,
        ctypes.c_double(x), ctypes.c_double(y), ctypes.byref(dx), ctypes.byref(dy),
    )
    return float(dx.value), float(dy.value)


def _run_frame(run: dict) -> Tuple[float, float, float, float]:
    """(início, fim) ao longo da leitura e (base, topo) perpendicular, no espaço do PDF."""
    th = math.radians(run["angle"])
    c, s = math.cos(th), math.sin(th)
    along = [x * c + y * s for x, y in run["pts"]]
    across = [-x * s + y * c for x, y in run["pts"]]
    return min(along), max(along), min(across), max(across)


_NUMERIC_RE = re.compile(r"^[\d.,]+$")


def _join_glyph_runs(runs: List[dict]) -> List[dict]:
    """Junta textos que o CAD gravou letra por letra ("C","o","n"... ou "1","1",",","2","0").

    Só junta trechos CONSECUTIVOS, na mesma direção e na mesma linha, em dois casos:
    - letras soltas: um dos dois tem no máximo 2 caracteres e a folga é de até meia letra;
    - palavras do mesmo texto: o PDF separou os dois com um espaço "gerado" (não havia
      caractere de espaço) e a folga é de até 0,8 letra — "Coordenação de" + "Enfermagem".
      Dois números nunca são juntados por esta regra (cotas vizinhas ficam separadas).
    Palavras de textos diferentes que só se encostam continuam separadas.
    """
    out: List[dict] = []
    for run in runs:
        if not run["pts"]:
            continue
        if out:
            prev = out[-1]
            same_dir = abs((run["angle"] - prev["angle"] + 180) % 360 - 180) <= 1.0
            short = min(len(prev["text"].strip()), len(run["text"].strip())) <= 2
            numbers = bool(_NUMERIC_RE.match(prev["text"].strip()) and _NUMERIC_RE.match(run["text"].strip()))
            word_gap = bool(run.get("after_space")) and not numbers
            if same_dir and (short or word_gap):
                a0, a1, b0, b1 = _run_frame(prev)
                # mede o trecho novo no referencial do anterior
                c0, c1, d0, d1 = _run_frame({"angle": prev["angle"], "pts": run["pts"]})
                h = max(b1 - b0, d1 - d0, 1e-6)
                overlap = min(b1, d1) - max(b0, d0)
                gap = c0 - a1
                max_gap = 0.5 * h if short else 0.8 * h
                if overlap >= 0.6 * min(b1 - b0, d1 - d0) and -0.15 * h <= gap <= max_gap:
                    numeric = prev["text"].rstrip()[-1:] in "0123456789,." and run["text"].strip()[:1] in "0123456789,."
                    sep = " " if (gap > 0.3 * h or word_gap) and not numeric else ""
                    prev["text"] = prev["text"].rstrip() + sep + run["text"].strip()
                    prev["pts"] += run["pts"]
                    prev["origins"] += run["origins"]
                    continue
        out.append(run)
    return out


# ── Fonte sem mapa de caracteres (texto "embaralhado") ───────
# Exportação de CAD com fonte TrueType sem tabela ToUnicode: o PDFium devolve o NÚMERO
# DO GLIFO, que na ordem padrão do Mac é o código da letra menos 29 — "'RUPLWyULR" é
# "Dormitório", o espaço vira 0x03 e os algarismos 0x13–0x1C (casa da UNIFAP, Macapá).
# Os glifos 98–129 são as letras acentuadas, na mesma ordem padrão (conferido:
# Circulação, Dimensões, cerâmica, Construída). Acima disso a ordem é da fonte: só o "²"
# depois de "número m" é recuperado.
_GLYPH_SHIFT = 29
_MAC_ACCENTS = "ÄÅÇÉÑÖÜáàâäãåçéèêëíìîïñóòôöõúùûü"           # glifos 98–129
_SHIFTED_CTRL = frozenset(range(0x03, 0x20)) - {0x09, 0x0A, 0x0D}
_PLAN_WORDS = frozenset((
    "AREA", "AREAS", "SALA", "SALAS", "COZINHA", "QUARTO", "DORMITORIO", "BANHEIRO", "BANHO", "LAVABO",
    "CIRCULACAO", "VARANDA", "GARAGEM", "SERVICO", "PLANTA", "CORTE", "FACHADA", "ESCALA", "TERRENO",
    "COBERTURA", "SUITE", "JANTAR", "ESTAR", "HALL", "CONSTRUIDA", "PAVIMENTO", "DIMENSOES", "PISO",
    "PORTA", "JANELA", "PROJETO", "DATA", "CLOSET", "JARDIM", "LAZER", "LAYOUT", "TELHA", "VISTA",
))


def _decode_glyph(code: int) -> str:
    if 3 <= code <= 97:
        return chr(code + _GLYPH_SHIFT)
    if 98 <= code <= 129:
        return _MAC_ACCENTS[code - 98]
    return chr(code) if code else ""


def _plan_word_hits(text: str) -> int:
    return sum(1 for w in re.findall(r"[^\W\d_]{3,}", text) if normalize(w) in _PLAN_WORDS)


def _glyph_fonts(raw_tp, n: int) -> Optional[List[bool]]:
    """Para cada caractere: vem de fonte sem mapa (decodificar)? None = nada embaralhado."""
    codes = [pdfium_c.FPDFText_GetUnicode(raw_tp, i) for i in range(n)]
    if not any(c in _SHIFTED_CTRL for c in codes):
        return None                                   # caso normal: custo zero
    # Agrupa pelo OBJETO da fonte, não pelo nome: na casa da UNIFAP há duas "Arial-ItalicMT",
    # uma com mapa ("Lavabo") e outra sem ("$ " = "A = 51,…").
    buf = ctypes.create_string_buffer(256)
    flags = ctypes.c_int()
    fonts: List[int] = []
    names: Dict[int, str] = {}
    for i in range(n):
        obj = pdfium_c.FPDFText_GetTextObject(raw_tp, i)
        handle = pdfium_c.FPDFTextObj_GetFont(obj) if obj else None
        key = ctypes.cast(handle, ctypes.c_void_p).value or 0 if handle else 0
        fonts.append(key)
        if key not in names:
            size = pdfium_c.FPDFText_GetFontInfo(raw_tp, i, buf, 256, ctypes.byref(flags))
            names[key] = buf.value.decode("latin-1", "replace") if size else "?"
    by_font: Dict[int, List[int]] = {}
    for font, code in zip(fonts, codes):
        by_font.setdefault(font, []).append(code)
    garbled = set()
    for font, cs in by_font.items():
        ctrl = sum(1 for c in cs if c in _SHIFTED_CTRL)
        if ctrl < 3 or ctrl < 0.02 * len(cs):
            continue
        raw = "".join(chr(c) for c in cs if c)
        decoded = "".join(_decode_glyph(c) for c in cs)
        # Prova de que é a fonte sem mapa: decodificada, ela forma palavras de planta OU
        # números ("1,80") onde o texto cru não tinha nenhum (a fonte só dos rótulos de área).
        raw_nums = len(re.findall(r"[0-9]+[.,][0-9]", raw))
        dec_nums = len(re.findall(r"[0-9]+[.,][0-9]", decoded))
        if _plan_word_hits(decoded) > _plan_word_hits(raw) or (dec_nums >= 2 and dec_nums > 2 * raw_nums):
            garbled.add(font)
    if not garbled:
        return None
    logger.info("Texto com fonte sem mapa de caracteres (%s): decodificado pela ordem dos glifos.",
                ", ".join(sorted(names.get(f, "?") for f in garbled)))
    return [f in garbled for f in fonts]


def _fix_superscript(text: str) -> str:
    """Glifos fora da tabela, na fonte sem mapa: "12,50 m?" → "12,50 m²" e "?rea" → "Área"."""
    text = re.sub(r"(\d\s*[mM])[^\x00-\x7f²]", r"\1²", text)
    # o glifo do "Á" também fica fora da tabela: "?rea" é "Área"
    return re.sub(r"(?<![^\W\d_])[^\x00-\x7f](?=REA\b|rea\b)", "Á", text)


def _text_runs(page, size: Tuple[int, int]) -> List[TextItem]:
    """Agrupa os caracteres em textos (um por entidade de texto do CAD)."""
    textpage = page.get_textpage()
    raw_tp = textpage.raw
    n = textpage.count_chars()
    page_rot = (page.get_rotation() or 0) % 360
    glyph_font = _glyph_fonts(raw_tp, n)

    runs: List[dict] = []
    cur: Optional[dict] = None
    after_space = False      # o próximo trecho vem depois de um espaço gerado pelo PDF

    def close() -> None:
        nonlocal cur
        if cur and cur["text"].strip():
            runs.append(cur)
        cur = None

    for i in range(n):
        code = pdfium_c.FPDFText_GetUnicode(raw_tp, i)
        ch = _decode_glyph(code) if glyph_font and glyph_font[i] else (chr(code) if code else "")
        if pdfium_c.FPDFText_IsGenerated(raw_tp, i) or ch in ("\r", "\n", ""):
            close()
            after_space = ch == " " and bool(pdfium_c.FPDFText_IsGenerated(raw_tp, i))
            continue
        angle = math.degrees(pdfium_c.FPDFText_GetCharAngle(raw_tp, i))
        if cur is not None and abs((angle - cur["angle"] + 180) % 360 - 180) > 1.0:
            close()
        l, b, r, t = textpage.get_charbox(i)
        ox, oy = ctypes.c_double(), ctypes.c_double()
        pdfium_c.FPDFText_GetCharOrigin(raw_tp, i, ctypes.byref(ox), ctypes.byref(oy))
        if cur is None:
            cur = {"text": "", "angle": angle, "pts": [], "origins": [], "after_space": after_space}
            after_space = False
        cur["text"] += ch
        if ch.strip():
            cur["pts"] += [(l, b), (r, b), (l, t), (r, t)]
            cur["origins"].append((ox.value, oy.value))
    close()
    runs = _join_glyph_runs(runs)

    items: List[TextItem] = []
    for run in runs:
        dev = [_page_to_device(page, size, x, y) for x, y in run["pts"]]
        xs, ys = [p[0] for p in dev], [p[1] for p in dev]
        origins = [_page_to_device(page, size, x, y) for x, y in run["origins"]]
        if len(origins) >= 2 and math.dist(origins[0], origins[-1]) > 1:
            (x0, y0), (x1, y1) = origins[0], origins[-1]
            angle = _snap(math.degrees(math.atan2(y1 - y0, x1 - x0)))
        else:
            angle = _snap(run["angle"] + page_rot)
        items.append(TextItem(
            text=_fix_superscript(run["text"].strip()) if glyph_font else run["text"].strip(),
            x1=min(xs), y1=min(ys), x2=max(xs), y2=max(ys),
            angle=angle, source="pdf",
        ))
    return items


def _dominant_angle(items: List[TextItem]) -> int:
    """Direção da maioria das letras (rótulos e carimbo, não números)."""
    weight = {0: 0, 90: 0, 180: 0, 270: 0}
    for it in items:
        if it.angle is not None:
            weight[it.angle] += sum(c.isalpha() for c in it.text)
    return max(weight, key=weight.get)


def _scan_px_per_mm(page) -> float:
    """Resolução (px/mm) para levar ao OCR uma página sem texto extraível.

    - Escaneada (uma imagem cobre ≥ metade da folha): a resolução da imagem
      (ampliar além dela não acrescenta detalhe).
    - Vetorial (letras exportadas como traços): mais px/mm em folhas pequenas,
      para as letras reduzidas ficarem legíveis.
    """
    w_pt, h_pt = page.get_size()
    long_mm = max(w_pt, h_pt) / 72 * 25.4
    page_area = w_pt * h_pt
    best_cover, native = 0.0, None
    try:
        for obj in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=4):
            left, bottom, right, top = obj.get_bounds()
            cover = max(right - left, 0) * max(top - bottom, 0) / page_area
            if cover > best_cover:
                px_w, px_h = obj.get_px_size()
                width_mm = max(right - left, top - bottom) / 72 * 25.4
                best_cover, native = cover, max(px_w, px_h) / width_mm if width_mm else None
    except Exception:                       # objeto que o pdfium não descreve: segue como vetor
        logger.debug("Não foi possível inspecionar as imagens da página.", exc_info=True)
    if best_cover >= RASTER_COVER_FRACTION and native:
        return min(max(native, SCAN_PX_PER_MM), VECTOR_MAX_PX_PER_MM)
    return min(max(VECTOR_TARGET_LONG_SIDE / long_mm, SCAN_PX_PER_MM), VECTOR_MAX_PX_PER_MM)


def _choose_page(pdf: pdfium.PdfDocument) -> int:
    """Primeira página com "PLANTA BAIXA"; senão a primeira com texto."""
    first_with_text = None
    for idx in range(len(pdf)):
        text = normalize(pdf[idx].get_textpage().get_text_range())
        if "PLANTA" in text and "BAIXA" in text:
            return idx
        if first_with_text is None and len(text) >= MIN_TEXT_CHARS:
            first_with_text = idx
    return first_with_text or 0


# O PDFium NÃO pode ser usado por duas threads ao mesmo tempo: duas leituras
# simultâneas (o servidor atende PDFs com texto sem fila) derrubam o processo.
_PDFIUM_LOCK = threading.Lock()


def read_pdf(data: bytes, page_number: Optional[int] = None) -> PdfPage:
    """Renderiza a página da planta e extrai seus textos, tudo "em pé".

    ``page_number`` (1 = primeira) escolhe a página; sem ele, a primeira com
    "PLANTA BAIXA" (ou, sem texto, a primeira). Uma leitura por vez (PDFium).
    """
    with _PDFIUM_LOCK:
        return _read_pdf_locked(data, page_number)


def _read_pdf_locked(data: bytes, page_number: Optional[int]) -> PdfPage:
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise RuntimeError(f"PDF inválido ou protegido: {exc}") from exc
    try:
        if len(pdf) == 0:
            raise RuntimeError("O PDF não tem páginas.")

        if page_number is not None:
            if not 1 <= page_number <= len(pdf):
                raise RuntimeError(f"A página {page_number} não existe: o PDF tem {len(pdf)} página(s).")
            idx = page_number - 1
        else:
            idx = _choose_page(pdf)
        page = pdf[idx]
        w_pt, h_pt = page.get_size()
        scale = RENDER_LONG_SIDE / max(w_pt, h_pt)
        if page.get_textpage().count_chars() < MIN_TEXT_CHARS:
            px_mm = _scan_px_per_mm(page)
            scale = min(px_mm * 25.4 / 72, SCAN_MAX_LONG_SIDE / max(w_pt, h_pt))
            logger.info("PDF sem texto: renderizando a %.0f px/mm (%.0f px no lado maior) para o OCR.",
                        scale * 72 / 25.4, max(w_pt, h_pt) * scale)
        image = page.render(scale=scale, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
        items = _text_runs(page, image.size)

        rotation = _dominant_angle(items) if items else 0
        if rotation:
            w, h = image.size
            image = image.rotate(rotation, expand=True)
            rotated: List[TextItem] = []
            for it in items:
                (ax, ay) = rotate_point(it.x1, it.y1, rotation, w, h)
                (bx, by) = rotate_point(it.x2, it.y2, rotation, w, h)
                rotated.append(TextItem(
                    text=it.text, x1=min(ax, bx), y1=min(ay, by), x2=max(ax, bx), y2=max(ay, by),
                    angle=None if it.angle is None else (it.angle - rotation) % 360,
                    source="pdf",
                ))
            items = rotated

        logger.info(
            "PDF: página %d de %d, %d textos, girada %d° para leitura.",
            idx + 1, len(pdf), len(items), rotation,
        )
        return PdfPage(image=image, items=items, rotation=rotation, page_index=idx, page_count=len(pdf))
    finally:
        pdf.close()      # fecha aqui, dentro da trava (não num coletor de lixo de outra thread)


def render_gray(data: bytes, page_index: int, rotation: int) -> np.ndarray:
    """Página em tons de cinza para a geometria, "em pé" como em ``read_pdf``.

    8 px por mm de papel (a resolução validada para o contorno), menos em folhas
    enormes (limite de ``GEOMETRY_MAX_PIXELS``).
    """
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(data)
        try:
            page = pdf[page_index]
            w_pt, h_pt = page.get_size()
            px_mm = min(GEOMETRY_PX_PER_MM, math.sqrt(GEOMETRY_MAX_PIXELS / (w_pt * h_pt)) * 72 / 25.4)
            image = page.render(scale=px_mm * 25.4 / 72, fill_color=(255, 255, 255, 255)).to_pil().convert("L")
        finally:
            pdf.close()
    if rotation:
        image = image.rotate(rotation, expand=True, fillcolor=255)
    return np.asarray(image)


# ── Área declarada em qualquer página (memorial, quadro de áreas) ──

def scan_document_areas(data: bytes, max_pages: int = 400) -> List[Dict[str, Any]]:
    """Área construída escrita em QUALQUER página com texto do documento.

    Um PDF de projeto ou um TCC pode trazer o total numa folha (quadro de áreas)
    ou no memorial, longe da planta lida. Devolve uma entrada por página onde
    achou: {pagina, valor, rotulo, texto}. Só usa o texto do PDF (sem OCR).
    """
    out: List[Dict[str, Any]] = []
    with _PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError:
            return out
        try:
            for idx in range(min(len(pdf), max_pages)):
                page = pdf[idx]
                raw = page.get_textpage().get_text_range()
                if len(raw.strip()) < MIN_TEXT_CHARS or "REA" not in normalize(raw):
                    continue
                seen = set()
                # 1) Carimbo / quadro de áreas (texto posicionado na folha).
                w_pt, h_pt = page.get_size()
                items = _text_runs(page, (int(w_pt * 2), int(h_pt * 2)))
                declared, _ = find_declared_area(items)
                # "Área do pavimento térreo" é de UM pavimento, não o total da obra.
                if declared and "PAVIMENTO" not in normalize(declared.label) \
                        and not _excluded_declared(raw, declared):
                    seen.add(round(declared.value, 2))
                    out.append({"pagina": idx + 1, "valor": round(declared.value, 2),
                                "rotulo": declared.label, "texto": declared.value_item.text.strip()[:160]})
                # 2) Texto corrido (memorial, TCC): "possui 51,34 m² de área construída".
                for value, _pos, snippet in prose_built_areas(raw):
                    if round(value, 2) in seen:
                        continue
                    seen.add(round(value, 2))
                    out.append({"pagina": idx + 1, "valor": round(value, 2),
                                "rotulo": "texto do documento", "texto": snippet[:160]})
        finally:
            pdf.close()
    return out


def scan_document_prazo(data: bytes, max_pages: int = 400) -> Optional[Dict[str, Any]]:
    """Prazo de execução da obra escrito no documento (edital, memorial, cronograma).

    É o prazo CONTRATADO/planejado — não o que a obra realmente leva. Devolve
    {meses, texto, paginas, outros_meses} com o valor que mais se repete (empate: o
    primeiro), ou None.
    """
    found: List[Tuple[float, int, str]] = []
    with _PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError:
            return None
        try:
            for idx in range(min(len(pdf), max_pages)):
                raw = pdf[idx].get_textpage().get_text_range()
                if "PRAZO" not in normalize(raw) and "MESES" not in normalize(raw):
                    continue
                for meses, _pos, snippet in prose_prazos(re.sub(r"\s+", " ", raw)):
                    found.append((meses, idx + 1, snippet))
        finally:
            pdf.close()
    if not found:
        return None
    count: Dict[float, int] = {}
    for meses, _, _ in found:
        count[meses] = count.get(meses, 0) + 1
    order = [m for m, _, _ in found]
    best = max(count, key=lambda m: (count[m], -order.index(m)))
    pages = sorted({p for m, p, _ in found if m == best})
    texto = min((s for m, _, s in found if m == best), key=len)
    return {"meses": best, "texto": texto[:200], "paginas": pages,
            "outros_meses": sorted({m for m, _, _ in found if m != best})}


def _excluded_declared(raw: str, declared: Any) -> bool:
    """A área achada no carimbo/quadro é de outra coisa (exigência mínima, apartamento...)?

    Olha o texto antes do NÚMERO — dentro do próprio texto ("de no mínimo 46,00 m²") e,
    na página, as linhas logo acima ("APARTAMENTO 403 … Área total = 75,65 m²").
    """
    text = declared.key_item.text if declared.key_item is declared.value_item else (
        declared.key_item.text + " " + declared.value_item.text)
    for m in re.finditer(NUM, text):
        if parse_number(m.group(0)) == declared.value:
            if area_context_excluded(text, m.start()):
                return True
            break
    snippet = declared.value_item.text.strip()
    pos = raw.find(snippet[:24]) if snippet else -1
    return pos >= 0 and UNIT_CONTEXT_RE.search(normalize(raw[max(pos - 160, 0):pos])) is not None


def choose_document_area(found: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """O total do documento: o valor que mais se repete entre as páginas.

    Devolve {valor, paginas, rotulo, texto, outros_valores} ou None. Valores
    diferentes em outras páginas ficam em ``outros_valores`` (para conferir).
    """
    if not found:
        return None
    groups: Dict[float, List[Dict[str, Any]]] = {}
    first: Dict[float, int] = {}
    for k, f in enumerate(found):
        groups.setdefault(f["valor"], []).append(f)
        first.setdefault(f["valor"], k)
    # O que mais se repete; empate (cada valor uma vez: opções A/B/C de um edital,
    # projeto + ampliação): o PRIMEIRO no documento — a obra descrita antes das variantes.
    best_value = max(groups, key=lambda v: (len(groups[v]), -first[v]))
    best = groups[best_value]
    cleanest = min(best, key=lambda f: len(f["texto"]))       # quadro/memorial em vez de prosa
    return {
        "valor": best_value,
        "paginas": sorted({f["pagina"] for f in best}),
        "rotulo": cleanest["rotulo"],
        "texto": cleanest["texto"],
        "outros_valores": sorted({f["valor"] for f in found if f["valor"] != best_value}),
        "repetido": len(best) >= 2,
    }


def page_count(data: bytes) -> int:
    """Número de páginas do PDF (0 se não abrir)."""
    with _PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError:
            return 0
        try:
            return len(pdf)
        finally:
            pdf.close()


def parse_pages(spec: str, total: int, limit: int = 30) -> List[int]:
    """"todas" / "1-5" / "1,3,4-6" → [1, 2, ...] (1 = primeira). Erro claro se inválido."""
    spec = (spec or "").strip().lower()
    if spec in ("todas", "all", "*"):
        pages = list(range(1, total + 1))
    else:
        pages = []
        for part in spec.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)|(\d+)", part)
            if not m:
                raise RuntimeError(f"Páginas inválidas: “{spec}”. Use, por exemplo, 1-5 ou 1,3,4.")
            a, b = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)),) * 2
            pages += list(range(min(a, b), max(a, b) + 1))
        pages = sorted(set(pages))
    bad = [n for n in pages if not 1 <= n <= total]
    if bad or not pages:
        raise RuntimeError(f"Páginas fora do PDF ({total} página(s)): {', '.join(map(str, bad)) or spec}.")
    if len(pages) > limit:
        raise RuntimeError(f"No máximo {limit} páginas por leitura (pedidas {len(pages)}).")
    return pages
