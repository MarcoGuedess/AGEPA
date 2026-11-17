# ============================================================
# AGEPA — OCR rápido (PP-OCRv6) num processo auxiliar
# services/paddle_ocr.py
# ============================================================
#
# O PP-OCRv6 (PaddleOCR 3.7) lê a prancha inteira: é ~3x mais rápido que o
# Florence-2 na GPU e ~8x mais que na CPU, e leu o que o Florence-2 não lia
# (quadra do FNDE, folha sem texto). O PaddlePaddle ainda não tem versão para
# o Python 3.14 do backend, então ele roda em ../.venv-ocr (Python 3.12), num
# processo que este módulo inicia e mantém aberto (backend/ocr_worker.py).
#
# O Florence-2 continua no backend para as RELEITURAS ampliadas que conferem os
# números da área (carimbo, quadro, rótulos). Sem o ambiente .venv-ocr — ou se o
# processo falhar —, a leitura volta inteira para o Florence-2, como antes.
#
# Desligar o OCR rápido: variável de ambiente AGEPA_OCR=florence.
# Outro Python para o processo: AGEPA_OCR_PYTHON=caminho\python.exe.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from PIL import Image

from services.plan_reader import TextItem

logger = logging.getLogger("agepa.paddle_ocr")

PREFIX = "@@AGEPA "
BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PYTHON = BACKEND_DIR.parent / ".venv-ocr" / "Scripts" / "python.exe"
WORKER_SCRIPT = BACKEND_DIR / "ocr_worker.py"
START_TIMEOUT_S = 240.0        # 1ª vez: carrega (e, se preciso, baixa) os modelos
READ_TIMEOUT_S = 1800.0
TILE_PX = 1280                 # bloco do processo auxiliar (para estimar o tempo)


def raw_to_items(raw: Sequence[Sequence[Any]], scale: float = 1.0) -> List[TextItem]:
    """Linhas lidas pelo PP-OCRv6 → TextItems no quadro de trabalho.

    ``raw``: [[texto, confiança, x1, y1, x2, y2, virado], ...] em px da imagem lida;
    ``scale``: px dessa imagem por px do quadro de trabalho.
    Linha alta (> 1,5× a largura) é lida girada 90° no sentido anti-horário: sem
    virar, o texto corre de cima para baixo (90°); virada, de baixo para cima (270°).
    Linha deitada e virada está de cabeça para baixo (180°).
    """
    items: List[TextItem] = []
    for text, _score, x1, y1, x2, y2, flipped in raw:
        text = str(text).strip()
        if not text:
            continue
        vertical = (y2 - y1) > 1.5 * (x2 - x1) and len(text) >= 2
        angle = (270 if flipped else 90) if vertical else (180 if flipped else 0)
        items.append(TextItem(text, x1 / scale, y1 / scale, x2 / scale, y2 / scale, angle, "ocr"))
    return items


def estimate_tiles(width: int, height: int) -> int:
    """Blocos de 1280 px (15% de sobreposição) com tinta, ~70% do total."""
    step = int(TILE_PX * 0.85)
    nx = max(1, -(-(width - int(TILE_PX * 0.15)) // step))
    ny = max(1, -(-(height - int(TILE_PX * 0.15)) // step))
    return max(1, round(0.7 * nx * ny))


class PaddleOcrWorker:
    """Processo auxiliar do PP-OCRv6: iniciado sob demanda e mantido aberto."""

    def __init__(self, python: Optional[Path] = None, script: Path = WORKER_SCRIPT,
                 log_path: Optional[Path] = None) -> None:
        self._python = Path(python or os.environ.get("AGEPA_OCR_PYTHON") or DEFAULT_PYTHON)
        self._script = Path(script)
        self._log_path = Path(log_path or Path(tempfile.gettempdir()) / "agepa_ocr_worker.log")
        self._proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 0
        self._failed = False
        self.model: Optional[str] = None

    # ── ciclo de vida ────────────────────────────────────────

    def available(self) -> bool:
        """Inicia o processo, se preciso. False = usar só o Florence-2."""
        with self._lock:
            return self._ensure_started()

    @property
    def ready(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self.model is not None

    def _ensure_started(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if self._failed:
            return False
        if os.environ.get("AGEPA_OCR", "").strip().lower() in ("florence", "off", "0", "nao", "não"):
            logger.info("OCR rápido desligado (AGEPA_OCR): leitura só com o Florence-2.")
            self._failed = True
            return False
        if not self._python.exists() or not self._script.exists():
            logger.info("OCR rápido indisponível (%s não encontrado): leitura só com o Florence-2.", self._python)
            self._failed = True
            return False
        logger.info("Iniciando o OCR rápido (PP-OCRv6) em %s …", self._python)
        log = open(self._log_path, "a", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                [str(self._python), "-u", str(self._script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(self._script.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            logger.exception("Não foi possível iniciar o OCR rápido.")
            self._failed = True
            return False
        finally:
            log.close()
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True).start()
        msg = self._wait(lambda m: m.get("pronto") is True, START_TIMEOUT_S)
        if msg is None:
            logger.error("O OCR rápido não respondeu (veja %s): leitura só com o Florence-2.", self._log_path)
            self._stop()
            self._failed = True
            return False
        self.model = msg.get("modelo") or "PP-OCRv6"
        logger.info("OCR rápido pronto: %s", self.model)
        return True

    @staticmethod
    def _pump(proc: subprocess.Popen, lines: "queue.Queue[Dict[str, Any]]") -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if not line.startswith(PREFIX):
                    continue                  # aviso de biblioteca que escapou para a saída
                try:
                    lines.put(json.loads(line[len(PREFIX):]))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass                              # canal fechado (processo encerrado)
        lines.put({"fim": True})

    def _wait(self, accept: Callable[[Dict[str, Any]], bool], timeout: float) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                msg = self._lines.get(timeout=left)
            except queue.Empty:
                return None
            if msg.get("fim"):
                return None
            if accept(msg):
                return msg

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._proc.stdin:
                try:
                    self._proc.stdin.write(json.dumps({"sair": True}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._stop()

    # ── leitura ──────────────────────────────────────────────

    def read(self, image: Image.Image, on_total: Optional[Callable[[int], None]] = None,
             on_tile: Optional[Callable[[], None]] = None,
             timeout: float = READ_TIMEOUT_S) -> Optional[List[List[Any]]]:
        """Lê a imagem inteira. None = falhou (o chamador usa o Florence-2).

        ``on_total(n)`` recebe o número de blocos e ``on_tile()`` é chamado a cada
        bloco lido (progresso). Se um deles levantar exceção (análise cancelada), o
        processo é encerrado e a exceção segue.
        """
        with self._lock:
            if not self._ensure_started():
                return None
            self._next_id += 1
            req_id = self._next_id
            fd, path = tempfile.mkstemp(prefix="agepa_ocr_", suffix=".png")
            os.close(fd)
            try:
                image.convert("RGB").save(path, compress_level=1)
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(json.dumps({"id": req_id, "imagem": path}) + "\n")
                self._proc.stdin.flush()
                deadline = time.monotonic() + timeout
                while True:
                    msg = self._wait(lambda m: m.get("id") == req_id, max(deadline - time.monotonic(), 0.0))
                    if msg is None:
                        logger.error("OCR rápido sem resposta (tempo esgotado ou processo encerrado).")
                        self._stop()
                        return None
                    if "blocos" in msg and on_total:
                        on_total(int(msg["blocos"]))
                    elif "bloco" in msg and on_tile:
                        on_tile()
                    elif "textos" in msg:
                        logger.info("OCR rápido: %d textos em %.1f s.", len(msg["textos"]), msg.get("segundos", 0))
                        return msg["textos"]
                    elif "erro" in msg:
                        logger.error("OCR rápido falhou: %s", msg["erro"])
                        return None
            except (OSError, ValueError):
                logger.exception("Falha ao conversar com o OCR rápido: leitura volta para o Florence-2.")
                self._stop()
                return None
            except BaseException:
                self._stop()                 # cancelada ou erro: o processo é reiniciado na próxima
                raise
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
