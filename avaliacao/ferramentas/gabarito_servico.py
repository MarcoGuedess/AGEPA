"""Todas as plantas PDF (com texto) do gabarito pelo FloorPlanAnalyzer.analyze, sem servidor e sem Florence.

uso: gabarito_servico.py <pasta backend> [trecho do caminho, ex.: privadas]
"""
import logging, sys, threading, time
from pathlib import Path
BACKEND = Path(sys.argv[1]); sys.path.insert(0, str(BACKEND))
sys.stdout.reconfigure(encoding="utf-8"); logging.disable(logging.INFO)
import avaliar_plantas as av
from services.ai_service import FloorPlanAnalyzer
from services.pdf_reader import read_pdf
an = object.__new__(FloorPlanAnalyzer); an._lock = threading.Lock(); an._tracker = None
from services.paddle_ocr import PaddleOcrWorker
an._fast_ocr = PaddleOcrWorker(python=Path("nao_existe.exe"))
FILTRO = sys.argv[2] if len(sys.argv) > 2 else ""
for linha in av.carregar_gabarito(av.DEFAULT_GABARITO):
    if FILTRO and FILTRO not in linha.arquivo:
        continue
    if not linha.usar or not linha.arquivo.lower().endswith(".pdf"):
        continue
    data = (av.AVALIACAO_DIR / linha.arquivo).read_bytes()
    if not read_pdf(data, linha.pagina).has_text:
        continue
    t = time.time()
    r = an.analyze(data, linha.arquivo, None, linha.pagina)
    cat, erro, aluc = av.classificar(linha, r.area_m2, r.area_kind, r.confidence)
    c = r.contorno
    ctx = "—" if not c else f"{c['area_m2']:.1f} {'usado' if c['usado'] else ('ok' if c['valido'] else 'descartado')}"
    print(f"{Path(linha.arquivo).name[:40]:40} p{linha.pagina or '-'} {r.area_m2} {r.area_kind} {r.area_method} {r.confidence} "
          f"| real {linha.area_real} → {cat}{' ALUCINAÇÃO' if aluc else ''} | contorno {ctx} | {time.time() - t:.0f}s")
