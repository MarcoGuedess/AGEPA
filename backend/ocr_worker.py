# ============================================================
# AGEPA — Processo auxiliar de OCR (PP-OCRv6 / PaddleOCR 3.7)
# backend/ocr_worker.py
# ============================================================
#
# Roda no ambiente ../.venv-ocr (Python 3.12: o PaddlePaddle ainda não tem
# versão para o Python 3.14 do backend). O backend (services/paddle_ocr.py)
# inicia este processo sozinho e conversa por linhas JSON:
#
#   pedido (entrada):  {"id": 1, "imagem": "C:/.../planta.png"}
#   resposta (saída):  @@AGEPA {"id": 1, "blocos": 42}          ← total de blocos
#                      @@AGEPA {"id": 1, "bloco": 7}             ← progresso
#                      @@AGEPA {"id": 1, "textos": [[texto, conf, x1, y1, x2, y2, virado], ...],
#                               "segundos": 12.3}
#                      @@AGEPA {"id": 1, "erro": "..."}          ← em caso de falha
#
# As bibliotecas (Paddle, oneDNN) escrevem avisos na saída padrão: por isso as
# linhas do protocolo levam o prefixo "@@AGEPA " e o resto vai para o log.
#
# Leitura: a imagem inteira em blocos de 1280 px (15% de sobreposição), sem
# reduzir — o PP-OCRv6 detecta cada linha de texto (inclusive de lado) e o
# classificador de orientação diz se ela está de cabeça para baixo.
# ============================================================

import json
import os
import sys
import time

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

PREFIX = "@@AGEPA "
TILE = 1280
OVERLAP = 0.15
MIN_SCORE = 0.5            # leitura com confiança menor é descartada
BLANK_INK = 0.002          # bloco quase vazio (margem branca) é pulado

# O protocolo sai por uma cópia da saída padrão; a saída "normal" passa a ir para
# o log de erros, onde ficam os avisos que as bibliotecas imprimem.
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def send(obj) -> None:
    _proto.write(PREFIX + json.dumps(obj, ensure_ascii=False) + "\n")
    _proto.flush()


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def read_image(ocr, path: str, req_id) -> list:
    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    arr = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1]          # BGR
    h, w = arr.shape[:2]
    step = int(TILE * (1 - OVERLAP))
    tiles = []
    for y0 in range(0, max(h - int(TILE * OVERLAP), 1), step):
        for x0 in range(0, max(w - int(TILE * OVERLAP), 1), step):
            tile = arr[y0:y0 + TILE, x0:x0 + TILE]
            if (tile < 200).mean() >= BLANK_INK:
                tiles.append((x0, y0))
    send({"id": req_id, "blocos": len(tiles)})
    found = []
    for k, (x0, y0) in enumerate(tiles, 1):
        tile = np.ascontiguousarray(arr[y0:y0 + TILE, x0:x0 + TILE])
        for res in ocr.predict(tile):
            flips = list(res.get("textline_orientation_angles") or [])
            for i, (text, score, box) in enumerate(zip(res["rec_texts"], res["rec_scores"], res["rec_boxes"])):
                if not str(text).strip() or float(score) < MIN_SCORE:
                    continue
                x1, y1, x2, y2 = (float(v) for v in box)
                flipped = i < len(flips) and flips[i] not in (0, "0", None, -1)
                found.append((str(text), float(score), (x1 + x0, y1 + y0, x2 + x0, y2 + y0), flipped))
        send({"id": req_id, "bloco": k})
    kept = []
    for text, score, box, flipped in sorted(found, key=lambda t: -t[1]):       # sobreposição dos blocos
        if all(_iou(box, other[2]) < 0.5 for other in kept):
            kept.append((text, score, box, flipped))
    return [[t, round(s, 4), *[round(v, 1) for v in b], f] for t, s, b, f in kept]


def main() -> None:
    from paddleocr import PaddleOCR

    tier = os.environ.get("AGEPA_OCR_TIER", "small")
    ocr = PaddleOCR(
        use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=True,
        text_det_limit_side_len=TILE, text_det_limit_type="max",
        text_detection_model_name=f"PP-OCRv6_{tier}_det", text_recognition_model_name=f"PP-OCRv6_{tier}_rec",
    )
    send({"pronto": True, "modelo": f"PP-OCRv6_{tier}"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if req.get("sair"):
            break
        t0 = time.time()
        try:
            textos = read_image(ocr, req["imagem"], req.get("id"))
            send({"id": req.get("id"), "textos": textos, "segundos": round(time.time() - t0, 1)})
        except Exception as exc:           # noqa: BLE001 — o backend cai no Florence-2
            send({"id": req.get("id"), "erro": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
