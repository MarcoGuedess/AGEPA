# ============================================================
# AGEPA — Backend FastAPI
# main.py
# ============================================================
#
# Arquivo exclusivo para:
#   - Inicializar a aplicação FastAPI.
#   - Configurar o middleware de CORS.
#   - Definir as rotas HTTP (endpoints).
#
# A lógica fica nos serviços (Separação de Preocupações):
#   services/ai_service.py       → lê a planta (PDF ou OCR)
#   services/schedule_service.py → prazo (obras públicas + comparação CNO)
#   services/cost_service.py     → custo (CUB/m²)
#
# Para executar:
#   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# ============================================================

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from services.ai_service import AnalysisCancelled, FloorPlanAnalyzer, ProgressTracker
from services.cost_service import CostEstimator, CostInputError
from services.pdf_reader import is_pdf, page_count, parse_pages
from services.fnde_tipologias import TIPOLOGIAS, match_tipologia
from services.schedule_service import USO_LABELS, ScheduleEstimator
from services.uso_obra import infer_uso

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agepa.main")

# ── Formatos aceitos ─────────────────────────────────────────
ALLOWED_EXTENSIONS: set[str] = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
MAX_FILE_SIZE_MB: int = 50

# ── Variável global do serviço de IA ─────────────────────────
# Inicializada no lifespan para carregamento único (startup).
analyzer: FloorPlanAnalyzer | None = None

# ── Serviço de custo (CUB) ───────────────────────────────────
# Leve e independente do modelo de IA: carregado já na importação,
# para que uma tabela cub.json inválida derrube o servidor no startup.
cost_estimator = CostEstimator()

# ── Serviço de prazo (modelo principal + comparações) ────────
schedule_estimator = ScheduleEstimator()

# ── Análises em andamento (job_id → progresso) ───────────────
# O job_id é gerado pelo frontend (UUID) e enviado junto do arquivo.
jobs: Dict[str, ProgressTracker] = {}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


# ══════════════════════════════════════════════════════════════
# Lifespan — Inicialização e encerramento do app
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Gerencia o ciclo de vida da aplicação.

    No startup: carrega o modelo Florence-2 uma única vez.
    No shutdown: libera recursos (se necessário).
    """
    global analyzer

    logger.info("══════════════════════════════════════════════")
    logger.info("  AGEPA — Inicializando servidor …")
    logger.info("══════════════════════════════════════════════")

    # SEM try/except: qualquer falha no carregamento (ImportError de
    # einops/timm, download, dtype, etc.) aborta o startup e o uvicorn
    # imprime o Traceback completo no terminal.
    t0 = time.perf_counter()
    analyzer = FloorPlanAnalyzer()
    elapsed = time.perf_counter() - t0
    logger.info(
        "Modelo carregado em %.1f s  |  Dispositivo: %s",
        elapsed,
        analyzer.device,
    )

    yield  # ← Aplicação rodando.

    logger.info("AGEPA — Encerrando servidor.")
    analyzer = None


# ══════════════════════════════════════════════════════════════
# Instância FastAPI
# ══════════════════════════════════════════════════════════════

app = FastAPI(
    title="AGEPA — Análise de Plantas Baixas",
    description=(
        "API para extração de topologias e estimativa de prazos "
        "de obras a partir de plantas baixas arquitetônicas, "
        "utilizando o modelo fundacional Microsoft Florence-2."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── CORS (permite requisições do frontend local) ────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # Em produção, restringir ao domínio.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════
# Rotas
# ══════════════════════════════════════════════════════════════

# ── Tela (frontend) servida pelo próprio backend — acesso pelo túnel da Cloudflare ──
# AGEPA_FRONTEND_DIR=/caminho/frontend: a tela fica em /ui/ e quem abre "/" no
# navegador é levado até ela; o "/" continua respondendo JSON para a própria tela.
FRONTEND_DIR = os.environ.get("AGEPA_FRONTEND_DIR")
if FRONTEND_DIR:
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")


@app.get("/", tags=["Health"])
async def root(request: Request) -> Any:
    if FRONTEND_DIR and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/ui/")
    return await _health()


async def _health() -> Dict[str, Any]:
    """Health check — verifica se o servidor está ativo.

    Também informa o tempo típico de uma análise (folha A a 2000 px)
    e se há uma análise em andamento, para o frontend avisar o usuário
    antes do envio.
    """
    return {
        "status": "online",
        "service": "AGEPA — Análise de Plantas Baixas",
        "model": analyzer.model_id if analyzer else "não carregado",
        "device": str(analyzer.device) if analyzer else "n/a",
        "estimated_s": round(analyzer.estimate_seconds()) if analyzer else None,
        "busy": analyzer.busy if analyzer else False,
        "ocr": analyzer.ocr_engine if analyzer else None,
    }


@app.get("/api/estimativa", tags=["Análise"])
async def estimativa(
    largura: int = Query(2000, ge=1, le=50_000, description="Largura da imagem (px)."),
    altura: int = Query(1414, ge=1, le=50_000, description="Altura da imagem (px)."),
) -> Dict[str, Any]:
    """Tempo previsto da análise para uma imagem desse tamanho.

    A previsão parte de medições do Florence-2 e é refinada a cada
    análise concluída neste servidor.
    """
    if analyzer is None:
        raise HTTPException(status_code=503, detail="Modelo de IA não está carregado.")
    return {
        "estimated_s": round(analyzer.estimate_seconds(largura, altura)),
        "device": str(analyzer.device),
        "busy": analyzer.busy,
    }


@app.get("/api/prazo", tags=["Prazo"])
async def prazo(
    area_m2: float = Query(..., gt=0, le=1_000_000, description="Área construída (m²)."),
    comodos: int = Query(1, ge=1, le=500, description="Quantidade de cômodos."),
    tipologia: Optional[str] = Query(None, description="Projeto padrão do FNDE reconhecido na análise."),
    uso: Optional[str] = Query(None, description="Tipo de uso lido na análise (ex.: residencial unifamiliar)."),
) -> Dict[str, Any]:
    """Prazo estimado (meses) para uma área — usado quando o usuário corrige a área."""
    if tipologia is not None and tipologia not in TIPOLOGIAS:
        raise HTTPException(status_code=400, detail=f"Tipologia desconhecida: {tipologia}")
    if uso is not None and uso not in USO_LABELS:
        raise HTTPException(status_code=400, detail=f"Tipo de uso desconhecido: {uso}")
    return schedule_estimator.predict(area_m2, comodos, tipologia, uso).to_dict()


@app.get("/api/custo/opcoes", tags=["Custo"])
async def custo_opcoes() -> Dict[str, Any]:
    """Estados, tipologias e padrões disponíveis na tabela do CUB."""
    return cost_estimator.options()


@app.get("/api/custo", tags=["Custo"])
async def custo(
    area_m2: float = Query(..., gt=0, le=1_000_000, description="Área construída (m²)."),
    uf: str = Query("SP", min_length=2, max_length=2, description="Estado da tabela CUB."),
    tipologia: str = Query("R1", max_length=10, description="Projeto-padrão (R1, R8, PP4…)."),
    padrao: str = Query("N", max_length=1, description="Padrão de acabamento: B, N ou A."),
) -> Dict[str, Any]:
    """Custo estimado da obra = área × CUB/m² (NBR 12.721).

    Separado de /api/analisar para que trocar o padrão de acabamento
    recalcule na hora, sem refazer a leitura da planta.
    """
    try:
        return cost_estimator.estimate(area_m2, uf, tipologia, padrao).to_dict()
    except CostInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/progresso/{job_id}", tags=["Análise"])
async def progresso(job_id: str) -> Dict[str, Any]:
    """Etapa atual, % concluído e tempo restante de uma análise."""
    tracker = jobs.get(job_id)
    if tracker is None:
        raise HTTPException(
            status_code=404,
            detail="Análise não encontrada (já terminou ou ainda não começou).",
        )
    return tracker.snapshot()


@app.post("/api/cancelar/{job_id}", tags=["Análise"], status_code=202)
async def cancelar(job_id: str) -> Dict[str, str]:
    """Pede o cancelamento; a análise para na próxima etapa (segundos)."""
    tracker = jobs.get(job_id)
    if tracker is None:
        raise HTTPException(status_code=404, detail="Análise não encontrada.")
    tracker.cancel()
    return {"status": "cancelando"}


def _br(value: float) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _area_obra(report: Any) -> Optional[Dict[str, Any]]:
    """Área da obra para o PRAZO e o CUSTO: a construída.

    Os modelos de prazo foram treinados com área construída e o CUB é por m²
    construído. Quando a resposta é a área útil (soma dos ambientes, sem paredes)
    e o contorno das paredes foi medido e conferido, o prazo e o custo usam o
    contorno (creche FNDE tipo 1: útil 1.045 m², contorno 1.262, real 1.324).
    Sem contorno, fica a útil — com o aviso de que prazo e custo saem baixos.
    """
    if not report.area_m2:
        return None
    kind = report.area_kind or "construida"
    c = report.contorno
    if kind == "util" and c and c.get("valido") and not c.get("usado") and c["area_m2"] > report.area_m2:
        return {"area_m2": float(c["area_m2"]), "base": "contorno", "nota": (
            f"Prazo e custo calculados com a área do contorno das paredes ({_br(c['area_m2'])} m², área "
            f"construída medida), não com a área útil ({_br(report.area_m2)} m²): os modelos de prazo e o "
            "CUB são por m² construído.")}
    if kind == "util":
        return {"area_m2": float(report.area_m2), "base": "util", "nota": (
            "Prazo e custo calculados com a área útil (soma dos ambientes, sem paredes). Os modelos de "
            "prazo e o CUB são por m² construído, que é maior: nas creches do FNDE testadas, 27% maior "
            "(o prazo sairia ≈ 8% maior e o custo ≈ 27% maior).")}
    if kind == "privativa":
        return {"area_m2": float(report.area_m2), "base": "privativa", "nota": (
            "Prazo e custo calculados com a área privativa de UMA unidade (planta de venda). "
            "Para a obra do prédio, informe a área construída total (quadro de áreas ou memorial).")}
    return {"area_m2": float(report.area_m2), "base": kind, "nota": None}


@app.post("/api/analisar", tags=["Análise"])
async def analisar_planta(
    file: UploadFile = File(
        ...,
        description="Planta baixa em PDF (do CAD: leitura exata) ou imagem (PNG, JPG, BMP, TIFF).",
    ),
    job_id: Optional[str] = Form(
        None,
        description=(
            "Identificador gerado pelo cliente para acompanhar a análise "
            "em /api/progresso/{job_id} e cancelá-la em /api/cancelar/{job_id}."
        ),
    ),
    pagina: Optional[int] = Form(
        None, ge=1, le=10000,
        description="PDF com várias páginas: qual ler (1 = primeira). Sem ele, a primeira com planta baixa.",
    ),
    paginas: Optional[str] = Form(
        None, max_length=200,
        description=(
            "Prédio em várias páginas: \"todas\" ou \"1-5\" / \"1,3,4\". Cada página é lida "
            "como uma planta (o título \"TIPO (x12)\" multiplica) e as áreas são somadas."
        ),
    ),
) -> JSONResponse:
    """Analisa uma planta baixa e retorna área, cômodos e prazo.

    **Fluxo:**
    1. Valida o arquivo recebido (tipo e tamanho).
    2. ``FloorPlanAnalyzer`` lê o texto da prancha (PDF exato ou OCR) e
       interpreta área (carimbo, ambientes ou cotas) e cômodos.
    3. ``ScheduleEstimator`` prevê o prazo a partir da área (modelo das obras
       públicas; o do CNO vem ao lado, em ``prazo.comparacoes``).

    **Retorno (JSON, principais campos):**
    ```json
    {
        "area_m2": 402.43,
        "area_kind": "construida",
        "area_method": "carimbo",
        "confidence": "alta",
        "qtd_comodos": 11,
        "prazo_meses": 10.6,
        "prazo": {"base": "Obras públicas", "intervalo_meses": [5.1, 21.8],
                  "comparacoes": [{"base": "CNO · obras privadas", "prazo_meses": 14.1}]},
        "checks": ["Soma dos 4 ambientes com área: 330,23 m² (82% …)"],
        "warnings": [],
        "rooms": [{"name": "Cozinha", "area_m2": 72.76, "external": false}],
        "annotated_image_b64": "data:image/png;base64,..."
    }
    ```
    ``area_m2`` vem ``null`` quando nada legível permite medir a área.

    Raises:
        HTTPException 400: Arquivo inválido (tipo ou tamanho).
        HTTPException 500: Erro interno durante o processamento.
        HTTPException 503: Modelo não carregado.
    """
    # ── Validação: modelo disponível ─────────────────────────
    if analyzer is None:
        raise HTTPException(
            status_code=503,
            detail="Modelo de IA não está carregado. Reinicie o servidor.",
        )

    # ── Validação: nome do arquivo ───────────────────────────
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="O arquivo deve ter um nome válido.",
        )

    # ── Validação: extensão do arquivo ───────────────────────
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Formato '{ext}' não suportado. "
                f"Use: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
            ),
        )

    # ── Leitura dos bytes ────────────────────────────────────
    try:
        image_bytes: bytes = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Erro ao ler o arquivo: {exc}",
        )
    if (ext == ".pdf") != is_pdf(image_bytes):
        raise HTTPException(
            status_code=400,
            detail="O conteúdo do arquivo não corresponde à extensão (PDF × imagem).",
        )

    # ── Validação: tamanho ───────────────────────────────────
    size_mb = len(image_bytes) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Arquivo muito grande ({size_mb:.1f} MB). "
                f"Máximo permitido: {MAX_FILE_SIZE_MB} MB."
            ),
        )

    logger.info(
        "Recebido: '%s' (%.2f MB)", file.filename, size_mb,
    )

    # Leitura de várias páginas (um prédio): pedido inválido é erro do pedido (400).
    pages = None
    if paginas and ext == ".pdf":
        try:
            pages = parse_pages(paginas, page_count(image_bytes))
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ── Acompanhamento de progresso ──────────────────────────
    if job_id is not None and not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="job_id inválido.")
    tracker = analyzer.new_tracker_for(image_bytes)
    if job_id:
        jobs[job_id] = tracker

    # ── Processamento (delegado ao serviço de IA) ────────────
    # Numa thread: a inferência leva minutos na CPU e, rodando direto
    # na rota async, travaria o servidor (inclusive /api/progresso).
    t0 = time.perf_counter()
    try:
        if pages:
            report = await run_in_threadpool(
                analyzer.analyze_pages, image_bytes, file.filename, tracker, pages
            )
        else:
            report = await run_in_threadpool(
                analyzer.analyze, image_bytes, file.filename, tracker, pagina
            )
    except AnalysisCancelled:
        raise HTTPException(status_code=409, detail="Análise cancelada pelo usuário.")
    except RuntimeError as exc:
        logger.error("Erro no processamento: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao processar a imagem: {exc}",
        )
    finally:
        if job_id:
            jobs.pop(job_id, None)

    # ── Prazo (só com área medida: nunca a partir de um chute) ─
    response_data = report.to_dict()
    response_data["prazo_meses"] = None
    obra = _area_obra(report)
    response_data["area_obra"] = obra
    if obra:
        # Projeto padrão do FNDE (creche Tipo 1/2, quadra): prazo das obras iguais no SIMEC ao lado.
        tipologia = match_tipologia(report.ocr_texts, obra["area_m2"])
        # Tipo de uso (casa, prédio, comercial): a base de obras privadas usa a curva própria.
        uso = infer_uso(report.ocr_texts, report.area_kind)
        schedule = schedule_estimator.predict(obra["area_m2"], report.qtd_comodos, tipologia, uso)
        response_data["prazo_meses"] = round(schedule.prazo_meses, 1)
        response_data["prazo"] = schedule.to_dict()
        response_data["prazo"]["area_usada_m2"] = round(obra["area_m2"], 2)
        response_data["prazo"]["area_base"] = obra["base"]
        response_data["warnings"] = response_data["warnings"] + schedule.warnings
        if obra["nota"]:
            response_data["checks"] = [obra["nota"]] + response_data["checks"]
    pd = report.prazo_documento
    if pd:
        # O prazo do edital/memorial é o CONTRATADO; o do modelo vem de obras reais.
        meses = f"{pd['meses']:.1f}".replace(".", ",").replace(",0", "")
        paginas = ", ".join(str(p) for p in pd["paginas"][:4])
        nota = f"Prazo de execução escrito no documento (pág. {paginas}): {meses} meses — “{pd['texto'][:120]}”."
        if response_data.get("prazo_meses"):
            diff = response_data["prazo_meses"] - pd["meses"]
            nota += (f" O modelo prevê {response_data['prazo_meses']:.1f} meses pelo histórico de obras reais"
                     .replace(".", ",") + (" (obras públicas costumam passar do prazo contratado)." if diff > 0
                                           else "."))
        response_data["checks"] = [nota] + response_data["checks"]

    elapsed = time.perf_counter() - t0
    response_data["processing_time_s"] = round(elapsed, 2)
    logger.info(
        "Processamento concluído em %.1f s  |  Área: %s m² (%s)  |  Prazo: %s meses",
        elapsed,
        f"{report.area_m2:.2f}" if report.area_m2 else "—",
        report.area_method,
        response_data["prazo_meses"] if response_data["prazo_meses"] is not None else "—",
    )

    return JSONResponse(content=response_data)
