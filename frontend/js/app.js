/* ============================================================
   AGEPA — Application Logic
   js/app.js
   ============================================================
   Responsabilidades:
     - Verificar o status do servidor de IA (online / ocupado / offline).
     - Receber o arquivo (PDF ou imagem; arraste, clique ou Ctrl+V).
     - Estimar o tempo da análise antes do envio.
     - Enviar ao backend e acompanhar o progresso real (etapa, %,
       tempo restante), com opção de cancelar.
     - Exibir os resultados (métricas, origem da área e conferências,
       ambientes, imagem anotada, custo).
     - Permitir corrigir a área e recalcular prazo e custo.
   ============================================================ */

// ── Configuração ────────────────────────────────────────────
// Mesmo endereço em que a página foi aberta (127.0.0.1 ou localhost): "localhost"
// pode cair no IPv6 (::1), onde o servidor (127.0.0.1) não escuta.
// Local (servidor estático na 5500 ou arquivo aberto): API na porta 8000.
// Pelo túnel (link trycloudflare.com): a tela vem do próprio backend, mesma origem.
/** Link do túnel para a tela do GitHub Pages: "?api=https://xxx.trycloudflare.com" fica guardado. */
function tunnelApi() {
    const given = new URLSearchParams(location.search).get("api");
    try {
        if (given) localStorage.setItem("agepa_api", given.replace(/\/+$/, ""));
        return localStorage.getItem("agepa_api") || "";
    } catch {
        return given ? given.replace(/\/+$/, "") : "";
    }
}

const API_BASE = (location.protocol === "file:" || location.port === "5500")
    ? `http://${["localhost", "127.0.0.1"].includes(location.hostname) ? location.hostname : "127.0.0.1"}:8000`
    // GitHub Pages: só a tela está lá; a IA roda no PC, pelo túnel da Cloudflare.
    : location.hostname.endsWith("github.io") ? tunnelApi()
    : location.origin;
const API = {
    health:     `${API_BASE}/`,
    analyze:    `${API_BASE}/api/analisar`,
    estimate:   (w, h) => `${API_BASE}/api/estimativa?largura=${w}&altura=${h}`,
    progress:   (id) => `${API_BASE}/api/progresso/${id}`,
    cancel:     (id) => `${API_BASE}/api/cancelar/${id}`,
    // tipologia: projeto padrão do FNDE reconhecido na análise (mantém a comparação com o SIMEC).
    schedule:   (area, comodos, tipologia, uso) =>
        `${API_BASE}/api/prazo?${new URLSearchParams({ area_m2: area, comodos: Math.max(comodos || 1, 1),
                                                       ...(tipologia ? { tipologia } : {}), ...(uso ? { uso } : {}) })}`,
    costOptions: `${API_BASE}/api/custo/opcoes`,
    cost:       (area, uf, tipologia, padrao) =>
        `${API_BASE}/api/custo?${new URLSearchParams({ area_m2: area, uf, tipologia, padrao })}`,
};

const MAX_FILE_MB = 50;
const ALLOWED_EXT = ["pdf", "png", "jpg", "jpeg", "bmp", "tif", "tiff"];
const PDF_ESTIMATE_S = 3;              // PDF com texto: leitura direta, sem IA
const MIN_GOOD_LONG_SIDE = 1500;      // px — abaixo disso textos pequenos se perdem
const HEALTH_INTERVAL_MS = 15000;
const HEALTH_RETRY_MS = 5000;
const POLL_INTERVAL_MS = 1000;
const BASE_TITLE = "AGEPA — Análise Inteligente de Plantas Baixas";

const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Etapas informadas pelo backend → texto para o usuário.
 * `step` é a posição no stepper (0–4).
 */
const STAGES = {
    fila:         { step: 0, title: "Aguardando na fila",            detail: () => "Outra análise está em andamento; a sua começa em seguida." },
    preparo:      { step: 0, title: "Preparando o arquivo",          detail: () => "Abrindo a planta" },
    pdf:          { step: 1, title: "Lendo o texto do PDF",          detail: () => "Texto exato do arquivo, sem reconhecimento por IA" },
    ocr:          { step: 1, title: "Lendo textos, áreas e cotas",   detail: (c, t) => `Bloco ${c} de ${t} da planta` },
    ocr_vertical: { step: 2, title: "Lendo os textos na vertical",   detail: (c, t) => `Bloco ${c} de ${t} (cotas e rótulos de lado)` },
    ocr_extra:    { step: 2, title: "Testando outras orientações",   detail: (c, t) => `Bloco ${c} de ${t} (a prancha parece invertida)` },
    ocr_rapido:   { step: 1, title: "Lendo textos, áreas e cotas",   detail: (c, t) => t ? `Bloco ${c} de ${t} (PP-OCRv6, todas as orientações)` : "Abrindo o OCR rápido" },
    releitura:    { step: 3, title: "Conferindo os números",         detail: (c, t) => t ? `Relendo ampliado ${c} de ${t}` : "Nada a conferir" },
    deteccao:     { step: 4, title: "Detectando elementos",          detail: () => "Portas, janelas e objetos" },
    calculo:      { step: 4, title: "Calculando área e prazo",       detail: () => "Conferindo as fontes e aplicando o modelo" },
};

/** Como a área foi obtida (o texto muda se veio do PDF ou do OCR). */
const AREA_METHODS = {
    carimbo: {
        title: "Área declarada no carimbo da prancha",
        text: (d) => `Valor do campo “${d.declared_area?.rotulo || "ÁREA"}”` + (
            d.text_source === "pdf"
                ? ", lido exatamente como está no PDF."
                : (d.declared_area?.conferido ? ", confirmado por duas leituras." : ", lido por OCR (sem confirmação).")
        ),
    },
    areas_declaradas: {
        title: "Área somada dos ambientes anotados na planta",
        text: (d) => d.text_source === "pdf"
            ? "Soma das áreas escritas em cada ambiente, lidas exatamente do PDF. É a área útil: não inclui paredes."
            : "Soma das áreas escritas em cada ambiente, conferidas por releitura ampliada. É a área útil: não inclui paredes.",
    },
    cotas: {
        title: "Área estimada pelas cotas (envoltória)",
        text: () => "Sem carimbo nem áreas nos ambientes: usamos o retângulo que contém a obra, medido pelas cadeias de cotas. Em plantas com recortes o valor fica acima do real.",
        warning: true,
    },
    indisponivel: {
        title: "Não foi possível medir a área",
        text: (d) => d.predio
            ? "Alguma página do prédio não tem área legível nem cotas para a escala, então o total não foi calculado (a tabela mostra o que foi lido). Informe a área abaixo para calcular prazo e custo — nenhum valor foi inventado."
            : "Não há carimbo com área, rótulos de área nem cotas coerentes legíveis. Informe a área abaixo para calcular prazo e custo — nenhum valor foi inventado.",
        warning: true,
    },
    documento: {
        title: "Área do empreendimento escrita no documento",
        text: (d) => `Total declarado ${pagesText((d.documento || {}).paginas)} do PDF (“${(d.documento || {}).texto || ""}”), ` +
            "lido exatamente do texto do arquivo. A página da planta é só um pavimento: a leitura dela está nas notas.",
    },
    pavimentos: {
        title: "Área somada dos pavimentos do prédio",
        text: () => "Soma das páginas lidas, com os pavimentos repetidos (ex.: tipo ×12) multiplicados. Veja a tabela por pavimento.",
    },
    contorno: {
        title: "Área medida no desenho (contorno das paredes)",
        text: (d) => "A planta não escreve a área: o sistema mediu o contorno das paredes no próprio desenho, " +
            `com a escala tirada das cotas. É uma estimativa — ${(d.contorno || {}).nota || "fica abaixo da área real"}. ` +
            "O contorno medido aparece em vermelho na imagem anotada.",
        warning: true,
    },
};

/** [39] → "na pág. 39"; [39, 110] → "nas págs. 39 e 110". */
function pagesText(pages) {
    const list = pages || [];
    if (!list.length) return "no documento";
    if (list.length === 1) return `na pág. ${list[0]}`;
    return `nas págs. ${list.slice(0, -1).join(", ")} e ${list[list.length - 1]}`;
}

const AREA_LABELS = {
    construida: "Área construída",
    util:       "Área útil",
    envoltoria: "Área (envoltória)",
    privativa:  "Área privativa (uma unidade)",
};

const AREA_HELP = {
    carimbo:          () => "Declarada no carimbo da prancha",
    areas_declaradas: (d) => `Soma de ${d.rooms_with_area} ambiente(s) com área`,
    cotas:            () => "Retângulo pelas cotas (por excesso)",
    indisponivel:     (d) => d.predio ? "Total do prédio incompleto" : "Não encontrada na planta",
    documento:        (d) => `Escrita no documento, ${pagesText((d.documento || {}).paginas).replace(/^na?s? /, "")}`,
    pavimentos:       (d) => `Soma de ${((d.predio || {}).pavimentos || []).length} página(s) do prédio`,
    contorno:         () => "Medida pelo contorno das paredes (estimativa)",
};

/** Como cada página do prédio foi lida (tabela por pavimento). */
const METHOD_SHORT = {
    carimbo:          "carimbo",
    areas_declaradas: "soma dos ambientes",
    cotas:            "retângulo das cotas",
    contorno:         "contorno das paredes",
    documento:        "documento",
    indisponivel:     "sem área legível",
};

const CONFIDENCE = {
    alta:  { label: "Alta",  help: "Áreas lidas diretamente da planta" },
    media: { label: "Média", help: "Parte das medidas foi inferida" },
    baixa: { label: "Baixa", help: "Sem medidas legíveis na imagem" },
};

// ── Referências do DOM ──────────────────────────────────────
const $ = (id) => document.getElementById(id);
const dom = {
    // Status
    serverStatus:     $("server-status"),
    serverStatusText: $("server-status-text"),
    offlineBanner:    $("offline-banner"),
    btnReconnect:     $("btn-reconnect"),

    // Telas
    viewUpload:   $("view-upload"),
    viewAnalysis: $("view-analysis"),
    viewError:    $("view-error"),

    // Envio
    dropzone:         $("dropzone"),
    fileInput:        $("file-input"),
    fileError:        $("file-error"),
    fileCard:         $("file-card"),
    fileThumb:        $("file-thumb"),
    fileName:         $("file-name"),
    fileMeta:         $("file-meta"),
    fileEstimateText: $("file-estimate-text"),
    fileWarning:      $("file-warning"),
    fileWarningText:  $("file-warning-text"),
    btnChangeFile:    $("btn-change-file"),
    btnAnalyze:       $("btn-analyze"),
    analyzeHint:      $("analyze-hint"),

    // Análise — cabeçalho
    analysisHeading: $("analysis-heading"),
    analysisFile:    $("analysis-file"),
    pagePick:        $("page-pick"),
    pageSelect:      $("page-select"),
    pageTotal:       $("page-total"),
    btnReadPage:     $("btn-read-page"),
    pagesInput:      $("pages-input"),
    btnReadAll:      $("btn-read-all"),
    resultActions:   $("result-actions"),
    btnDownload:     $("btn-download"),
    btnNewAnalysis:  $("btn-new-analysis"),

    // Progresso
    progressCard:    $("progress-card"),
    progressTitle:   $("progress-title"),
    progressDetail:  $("progress-detail"),
    progressPercent: $("progress-percent"),
    progressBar:     $("progress-bar"),
    progressFill:    $("progress-fill"),
    progressLive:    $("progress-live"),
    timeElapsed:     $("time-elapsed"),
    timeRemaining:   $("time-remaining"),
    timeFinish:      $("time-finish"),
    stepper:         $("stepper"),
    btnCancel:       $("btn-cancel"),

    // Métricas
    metrics:              $("metrics"),
    metricAreaLabel:      $("metric-area-label"),
    metricArea:           $("metric-area"),
    metricAreaHelp:       $("metric-area-help"),
    btnEditArea:          $("btn-edit-area"),
    metricPrazo:          $("metric-prazo"),
    metricPrazoHelp:      $("metric-prazo-help"),
    metricPrazoAlt:       $("metric-prazo-alt"),

    // Correção manual da área
    areaEdit:       $("area-edit"),
    areaEditTitle:  $("area-edit-title"),
    areaInput:      $("area-input"),
    areaEditError:  $("area-edit-error"),
    btnAreaCancel:  $("btn-area-cancel"),
    metricRooms:          $("metric-rooms"),
    metricConfidence:     $("metric-confidence"),
    metricConfidenceHelp: $("metric-confidence-help"),

    // Origem da área
    basis:         $("basis"),
    basisTitle:    $("basis-title"),
    basisText:     $("basis-text"),
    basisWarnings: $("basis-warnings"),

    // Prédio (várias páginas)
    buildingCard:   $("building-card"),
    buildingStatus: $("building-status"),
    buildingRows:   $("building-rows"),
    buildingSum:    $("building-sum"),
    buildingNote:   $("building-note"),

    // Custo (CUB)
    costCard:      $("cost-card"),
    costRef:       $("cost-ref"),
    costTipologia: $("cost-tipologia"),
    costPadrao:    $("cost-padrao"),
    costTotal:     $("cost-total"),
    costFormula:   $("cost-formula"),
    costRange:     $("cost-range"),
    costExcluded:  $("cost-excluded"),
    costSource:    $("cost-source"),

    // Imagem
    imageFrame:  $("image-frame"),
    resultImage: $("result-image"),
    imageToggle: $("image-toggle"),
    imageOpen:   $("image-open"),
    imageLegend: $("image-legend"),
    legendContorno: $("legend-contorno"),

    // Ambientes
    roomList:      $("room-list"),
    roomTotals:    $("room-totals"),
    totalBuilt:    $("total-built"),
    totalExternal: $("total-external"),

    // Detalhes técnicos
    techDetails: $("tech-details"),
    techFacts:   $("tech-facts"),
    ocrList:     $("ocr-list"),
    ocrCount:    $("ocr-count"),
    odList:      $("od-list"),
    odCount:     $("od-count"),

    // Erro
    errorTitle:   $("error-title"),
    errorText:    $("error-text"),
    errorTips:    $("error-tips"),
    btnRetry:     $("btn-retry"),
    btnOtherFile: $("btn-other-file"),

    toast: $("toast"),
};

// Esqueleto original da lista de ambientes (restaurado a cada análise).
const ROOM_LIST_SKELETON = dom.roomList.innerHTML;

// ── Estado da Aplicação ─────────────────────────────────────
const state = {
    /** @type {File | null} */
    file: null,
    /** URL local do arquivo enviado (imagem: preview e "Original"). */
    originalUrl: null,
    /** PDF: página renderizada pelo servidor (o navegador não mostra PDF em <img>). */
    previewUrl: null,
    isPdf: false,
    /** PDF com várias páginas: página escolhida (null = o servidor escolhe). */
    page: null,
    /** Leitura de um prédio: páginas pedidas ("todas", "1-6"...); null = uma página. */
    pages: null,
    /** Último resultado do servidor (base da correção manual). */
    result: null,
    /** URL (blob) da imagem anotada devolvida pela IA. */
    annotatedUrl: null,
    dims: null,                 // { width, height }
    estimatedS: null,           // previsão antes do envio
    server: { online: false, busy: false, device: null, estimatedS: null },

    // Análise em andamento
    running: false,
    jobId: null,
    abort: null,                // AbortController
    startedAt: 0,
    snapshot: null,             // último progresso do backend
    snapshotAt: 0,
    pollTimer: null,
    tickTimer: null,
    healthTimer: null,

    // Custo (CUB): escolhas do usuário valem para as próximas análises.
    cost: {
        options: null,          // resposta de /api/custo/opcoes
        uf: "SP",
        tipologia: "R1",        // casa térrea: o caso mais comum de planta baixa
        padrao: "N",
        area: null,             // área da análise atual
        requestId: 0,           // descarta respostas fora de ordem
    },
};


// ══════════════════════════════════════════════════════════════
// Utilidades
// ══════════════════════════════════════════════════════════════

/** Escapa texto (nomes vindos do OCR) antes de injetar como HTML. */
function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = String(str);
    return div.innerHTML;
}

const numberFmt = (value, decimals) =>
    Number(value).toLocaleString("pt-BR", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });

const currencyFmt = (value, decimals = 0) =>
    Number(value).toLocaleString("pt-BR", {
        style: "currency",
        currency: "BRL",
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });

/** 75 → "1:15" (relógio). */
function formatClock(seconds) {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

/** 285 → "4 min 45 s" (arredondado para não "piscar" a cada segundo). */
function formatDuration(seconds) {
    const s = Math.max(0, seconds);
    if (s < 10) return "alguns segundos";
    if (s < 60) return `${Math.round(s / 5) * 5} s`;
    const rounded = Math.round(s / 15) * 15;
    const m = Math.floor(rounded / 60);
    const rest = rounded % 60;
    return rest ? `${m} min ${rest} s` : `${m} min`;
}

/** Hora local daqui a N segundos → "21:45". */
function formatFinishTime(secondsFromNow) {
    const d = new Date(Date.now() + secondsFromNow * 1000);
    return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

function formatBytes(bytes) {
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${numberFmt(bytes / 1024 / 1024, 1)} MB`;
}

function newJobId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return `job-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function refreshIcons() {
    if (window.lucide) lucide.createIcons();
}

let toastTimer = null;
function showToast(message) {
    dom.toast.textContent = message;
    dom.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { dom.toast.hidden = true; }, 4000);
}

/** Conta de 0 até o valor (desligado com "reduzir movimento"). */
function animateNumber(element, value, decimals, unit) {
    const render = (v) => {
        element.innerHTML = `${numberFmt(v, decimals)}<span class="metric-card__unit">${unit}</span>`;
    };
    if (prefersReducedMotion) {
        render(value);
        return;
    }
    const duration = 900;
    const start = performance.now();
    const step = (now) => {
        const t = Math.min((now - start) / duration, 1);
        render(value * (1 - Math.pow(1 - t, 3)));
        if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
}


// ══════════════════════════════════════════════════════════════
// Telas
// ══════════════════════════════════════════════════════════════

/** @param {"upload" | "analysis" | "error"} view */
function showView(view) {
    dom.viewUpload.hidden = view !== "upload";
    dom.viewAnalysis.hidden = view !== "analysis";
    dom.viewError.hidden = view !== "error";
    window.scrollTo({ top: 0, behavior: prefersReducedMotion ? "auto" : "smooth" });
}


// ══════════════════════════════════════════════════════════════
// Status do Servidor
// ══════════════════════════════════════════════════════════════

async function checkServer() {
    clearTimeout(state.healthTimer);
    try {
        const response = await fetch(API.health, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        state.server = {
            online: true,
            busy: Boolean(data.busy),
            device: data.device,
            estimatedS: data.estimated_s,
            ocr: data.ocr || null,
        };
    } catch {
        state.server = { online: false, busy: false, device: null, estimatedS: null };
    }
    renderServerStatus();
    state.healthTimer = setTimeout(
        checkServer, state.server.online ? HEALTH_INTERVAL_MS : HEALTH_RETRY_MS
    );
}

function renderServerStatus() {
    const { online, busy, device } = state.server;
    const pill = dom.serverStatus;
    pill.className = "status-pill";

    if (!online) {
        pill.classList.add("status-pill--offline");
        dom.serverStatusText.textContent = "Servidor offline";
    } else if (busy && !state.running) {
        pill.classList.add("status-pill--busy");
        dom.serverStatusText.textContent = "Servidor ocupado";
    } else {
        pill.classList.add("status-pill--online");
        const fast = state.server.ocr && state.server.ocr.includes("PP-OCR");
        dom.serverStatusText.textContent =
            `Servidor online · ${device === "cuda" ? "GPU" : "CPU"}${fast ? " · OCR rápido" : ""}`;
    }
    dom.offlineBanner.hidden = online;
    updateAnalyzeButton();
    // Primeira resposta do servidor chegou depois da escolha do arquivo.
    if (online && state.file && state.estimatedS === null) fetchEstimate();
}


// ══════════════════════════════════════════════════════════════
// Escolha do Arquivo
// ══════════════════════════════════════════════════════════════

function fileExtension(name) {
    return (name.split(".").pop() || "").toLowerCase();
}

function showFileError(message) {
    dom.fileError.textContent = message;
    dom.fileError.hidden = false;
}

// Miniatura para PDF (o navegador não desenha PDF dentro de <img>).
const PDF_THUMB = "data:image/svg+xml;utf8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
    '<rect width="64" height="64" rx="12" fill="#efebff"/>' +
    '<path d="M22 14h14l10 10v26a2 2 0 0 1-2 2H22a2 2 0 0 1-2-2V16a2 2 0 0 1 2-2z" fill="#fff" stroke="#5b4cd1" stroke-width="2"/>' +
    '<path d="M36 14v10h10" fill="none" stroke="#5b4cd1" stroke-width="2"/>' +
    '<text x="32" y="44" text-anchor="middle" font-family="Inter,Arial,sans-serif" font-size="10" font-weight="700" fill="#5b4cd1">PDF</text>' +
    "</svg>"
);

function isPdfFile(file) {
    return file.type === "application/pdf" || fileExtension(file.name || "") === "pdf";
}

/**
 * Valida e carrega o arquivo escolhido (input, arraste ou colar).
 * Erros ficam na própria tela, sem perder o arquivo anterior.
 * @param {File} file
 */
function handleFile(file) {
    dom.fileError.hidden = true;
    state.page = null;
    state.pages = null;

    const ext = fileExtension(file.name || "imagem.png");
    if (!ALLOWED_EXT.includes(ext) && !file.type.startsWith("image/") && file.type !== "application/pdf") {
        showFileError(`“${file.name}” não é um formato aceito. Use PDF, PNG, JPG, BMP ou TIFF.`);
        return;
    }
    if (file.size > MAX_FILE_MB * 1024 * 1024) {
        showFileError(`O arquivo tem ${formatBytes(file.size)}; o limite é ${MAX_FILE_MB} MB. Reduza a resolução ou exporte em JPG.`);
        return;
    }

    if (state.originalUrl) URL.revokeObjectURL(state.originalUrl);
    state.file = file;
    state.isPdf = isPdfFile(file);
    state.originalUrl = URL.createObjectURL(file);
    state.dims = null;
    state.estimatedS = null;

    dom.fileName.textContent = file.name || "Imagem colada";
    dom.fileMeta.textContent = formatBytes(file.size);
    dom.fileWarning.hidden = true;
    dom.fileCard.hidden = false;
    dom.dropzone.classList.add("dropzone--compact");
    updateAnalyzeButton();

    if (state.isPdf) {
        // Texto vetorial do PDF: leitura direta, sem IA (PDF escaneado cai no OCR).
        dom.fileThumb.src = PDF_THUMB;
        dom.fileMeta.textContent = `PDF · ${formatBytes(file.size)}`;
        state.estimatedS = PDF_ESTIMATE_S;
        dom.fileEstimateText.textContent =
            "PDF: leitura direta do texto, em segundos (se for escaneado, leva alguns minutos)";
        return;
    }

    dom.fileThumb.src = state.originalUrl;
    dom.fileEstimateText.textContent = "Calculando tempo estimado…";

    // Dimensões em px → qualidade e estimativa de tempo.
    const probe = new Image();
    probe.onload = () => {
        state.dims = { width: probe.naturalWidth, height: probe.naturalHeight };
        dom.fileMeta.textContent =
            `${state.dims.width} × ${state.dims.height} px · ${formatBytes(file.size)}`;
        if (Math.max(state.dims.width, state.dims.height) < MIN_GOOD_LONG_SIDE) {
            dom.fileWarningText.textContent =
                "Resolução baixa: textos pequenos (áreas e cotas) podem não ser lidos.";
            dom.fileWarning.hidden = false;
        }
        fetchEstimate();
    };
    probe.onerror = () => {
        // TIFF não abre em todos os navegadores; o servidor ainda consegue ler.
        dom.fileThumb.removeAttribute("src");
        fetchEstimate();
    };
    probe.src = state.originalUrl;
}

async function fetchEstimate() {
    if (!state.file || state.isPdf) return;
    if (!state.server.online) {
        dom.fileEstimateText.textContent = "Tempo estimado indisponível (servidor offline)";
        return;
    }
    const { width, height } = state.dims || { width: 2000, height: 1414 };
    try {
        const response = await fetch(API.estimate(width, height));
        if (!response.ok) throw new Error();
        const data = await response.json();
        state.estimatedS = data.estimated_s;
        const where = data.device === "cuda" ? "GPU" : "CPU";
        let text = `Tempo estimado: cerca de ${formatDuration(data.estimated_s)} (processamento em ${where})`;
        if (data.busy) text += " — há outra análise na fila antes da sua";
        dom.fileEstimateText.textContent = text;
    } catch {
        dom.fileEstimateText.textContent = "Não foi possível estimar o tempo agora";
    }
}

function updateAnalyzeButton() {
    const ready = Boolean(state.file) && state.server.online;
    dom.btnAnalyze.disabled = !ready;
    if (!state.file) {
        dom.analyzeHint.textContent = "Escolha a planta (PDF ou imagem) para começar.";
    } else if (!state.server.online) {
        dom.analyzeHint.textContent = "Aguardando o servidor de IA ficar online…";
    } else {
        dom.analyzeHint.textContent = "Os valores aparecem assim que cada etapa terminar.";
    }
}

function resetToUpload() {
    stopAnalysisTimers();
    state.running = false;
    state.file = null;
    state.dims = null;
    state.estimatedS = null;
    if (state.originalUrl) URL.revokeObjectURL(state.originalUrl);
    if (state.annotatedUrl) URL.revokeObjectURL(state.annotatedUrl);
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.originalUrl = null;
    state.annotatedUrl = null;
    state.previewUrl = null;
    state.isPdf = false;
    state.result = null;

    dom.fileInput.value = "";
    dom.fileCard.hidden = true;
    dom.fileError.hidden = true;
    dom.dropzone.classList.remove("dropzone--compact");
    document.title = BASE_TITLE;
    updateAnalyzeButton();
    showView("upload");
    dom.dropzone.focus();
}

// ── Eventos: dropzone, colar ────────────────────────────────

dom.dropzone.addEventListener("click", () => dom.fileInput.click());
dom.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dom.fileInput.click();
    }
});
dom.fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) handleFile(e.target.files[0]);
});

["dragenter", "dragover"].forEach((type) =>
    dom.dropzone.addEventListener(type, (e) => {
        e.preventDefault();
        dom.dropzone.classList.add("dropzone--dragover");
    })
);
["dragleave", "drop"].forEach((type) =>
    dom.dropzone.addEventListener(type, (e) => {
        e.preventDefault();
        dom.dropzone.classList.remove("dropzone--dragover");
    })
);
dom.dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file) handleFile(file);
});

// Soltar a imagem fora da área não deve abrir o arquivo no navegador.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

document.addEventListener("paste", (e) => {
    if (dom.viewUpload.hidden) return;
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) {
        const blob = item.getAsFile();
        const ext = item.type.split("/")[1] || "png";
        handleFile(new File([blob], `imagem-colada.${ext}`, { type: item.type }));
    }
});

dom.btnChangeFile.addEventListener("click", () => dom.fileInput.click());


// ══════════════════════════════════════════════════════════════
// Análise — envio e progresso
// ══════════════════════════════════════════════════════════════

/** Prepara a tela de análise: esqueletos nos valores, imagem "varrendo". */
function prepareAnalysisView() {
    dom.analysisHeading.textContent = "Analisando sua planta";
    dom.analysisFile.textContent = (state.file.name || "Imagem colada") + (
        state.pages ? ` · prédio, páginas: ${state.pages}` : state.page ? ` · página ${state.page}` : "");
    dom.resultActions.hidden = true;
    dom.pagePick.hidden = true;
    dom.buildingCard.hidden = true;

    // Progresso
    dom.progressCard.className = "progress-card";
    dom.progressCard.innerHTML = PROGRESS_CARD_HTML;
    bindProgressCard();
    dom.progressBar.classList.add("progress-bar--indeterminate");
    dom.progressTitle.textContent = "Enviando imagem…";
    dom.progressDetail.textContent = "Transferindo o arquivo para o servidor";
    dom.timeRemaining.textContent = state.estimatedS
        ? `~${formatDuration(state.estimatedS)}`
        : "calculando…";
    dom.timeFinish.textContent = state.estimatedS ? formatFinishTime(state.estimatedS) : "—";
    renderStepper(0, false);

    // Métricas em esqueleto
    dom.metrics.setAttribute("aria-busy", "true");
    [dom.metricArea, dom.metricPrazo, dom.metricRooms, dom.metricConfidence].forEach((el) => {
        el.innerHTML = '<span class="skeleton skeleton--value"></span>';
    });
    dom.metricAreaLabel.textContent = "Área construída";
    dom.metricAreaHelp.textContent = "Lendo a planta…";
    dom.metricPrazoHelp.textContent = "Previsto pelo modelo de prazo";
    dom.metricPrazoHelp.classList.remove("metric-card__help--warn");
    dom.metricPrazoAlt.hidden = true;
    dom.metricConfidenceHelp.textContent = "Quão confiável é a área";
    dom.btnEditArea.hidden = true;
    closeAreaEdit();
    state.result = null;

    dom.basis.hidden = true;
    dom.costCard.hidden = true;
    state.cost.area = null;
    dom.roomList.innerHTML = ROOM_LIST_SKELETON;
    dom.roomTotals.hidden = true;
    dom.techDetails.hidden = true;
    dom.techDetails.open = false;

    // Imagem enviada com animação de leitura (PDF: a página vem do servidor)
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
    if (state.isPdf) dom.resultImage.src = PDF_THUMB;
    else dom.resultImage.src = state.originalUrl;
    dom.resultImage.alt = "Planta baixa enviada, em análise";
    dom.imageFrame.classList.add("image-frame--scanning");
    dom.imageToggle.hidden = true;
    dom.imageOpen.hidden = true;
    dom.imageLegend.hidden = true;

    showView("analysis");
}

async function handleAnalyze() {
    if (!state.file || state.running) return;

    state.running = true;
    state.jobId = newJobId();
    state.abort = new AbortController();
    state.startedAt = Date.now();
    state.snapshot = null;
    prepareAnalysisView();
    startAnalysisTimers();

    const formData = new FormData();
    formData.append("file", state.file, state.file.name || "planta.png");
    formData.append("job_id", state.jobId);
    if (state.pages) formData.append("paginas", state.pages);
    else if (state.page) formData.append("pagina", String(state.page));

    try {
        const response = await fetch(API.analyze, {
            method: "POST",
            body: formData,
            signal: state.abort.signal,
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 409) return;               // cancelada
        if (!response.ok) throw Object.assign(new Error(data.detail || `Erro HTTP ${response.status}`), { status: response.status });

        stopAnalysisTimers();
        state.running = false;
        renderResults(data);
    } catch (error) {
        if (error.name === "AbortError") return;           // cancelada pelo usuário
        stopAnalysisTimers();
        state.running = false;
        console.error("Erro na análise:", error);
        showAnalysisError(error);
    }
}

function startAnalysisTimers() {
    stopAnalysisTimers();
    state.pollTimer = setInterval(pollProgress, POLL_INTERVAL_MS);
    state.tickTimer = setInterval(renderTimes, 1000);
    renderTimes();
}

function stopAnalysisTimers() {
    clearInterval(state.pollTimer);
    clearInterval(state.tickTimer);
    state.pollTimer = null;
    state.tickTimer = null;
}

async function pollProgress() {
    if (!state.running || !state.jobId) return;
    const jobId = state.jobId;
    try {
        const response = await fetch(API.progress(jobId), { cache: "no-store" });
        if (!response.ok) return;     // 404: ainda não registrada ou já terminou
        const snapshot = await response.json();
        // A resposta pode chegar depois de um cancelamento ou do resultado.
        if (!state.running || state.jobId !== jobId) return;
        state.snapshot = snapshot;
        state.snapshotAt = Date.now();
        renderProgress(state.snapshot);
    } catch {
        /* falha momentânea de rede: tenta de novo no próximo ciclo */
    }
}

function renderProgress(snap) {
    const meta = STAGES[snap.stage] || { step: 0, title: "Processando", detail: () => "" };
    const current = Math.min(snap.step + 1, snap.total || 1);
    const percent = Math.min(Math.round(snap.percent), 99);   // 100% só com o resultado

    dom.progressBar.classList.remove("progress-bar--indeterminate");
    dom.progressFill.style.width = `${Math.max(percent, 2)}%`;
    dom.progressBar.setAttribute("aria-valuenow", String(percent));
    dom.progressPercent.textContent = `${percent}%`;
    dom.progressTitle.textContent = meta.title;
    // Prédio: diz qual página está sendo lida ("página 4 (2 de 6)").
    const where = snap.pagina ? `${snap.pagina.charAt(0).toUpperCase()}${snap.pagina.slice(1)} · ` : "";
    dom.progressDetail.textContent = where + meta.detail(current, snap.total);
    renderStepper(meta.step, true);

    document.title = `${percent}% · Analisando — AGEPA`;

    // Leitores de tela: anuncia só a troca de etapa, não cada bloco.
    if (dom.progressLive.dataset.stage !== snap.stage) {
        dom.progressLive.dataset.stage = snap.stage;
        dom.progressLive.textContent = `${meta.title}. ${percent}% concluído.`;
    }
    renderTimes();
}

/** Atualiza decorrido / restante / término a cada segundo. */
function renderTimes() {
    if (!state.running) return;
    const elapsed = (Date.now() - state.startedAt) / 1000;
    dom.timeElapsed.textContent = formatClock(elapsed);

    let remaining = null;
    if (state.snapshot) {
        remaining = state.snapshot.eta_s - (Date.now() - state.snapshotAt) / 1000;
    } else if (state.estimatedS) {
        remaining = state.estimatedS - elapsed;
    }
    if (remaining === null) return;

    if (remaining <= 3) {
        dom.timeRemaining.textContent = "quase pronto…";
        dom.timeFinish.textContent = formatFinishTime(0);
    } else {
        dom.timeRemaining.textContent = `~${formatDuration(remaining)}`;
        dom.timeFinish.textContent = formatFinishTime(remaining);
    }
}

function renderStepper(activeStep, running) {
    dom.stepper.querySelectorAll(".stepper__item").forEach((item) => {
        const step = Number(item.dataset.step);
        item.classList.toggle("is-done", step < activeStep);
        item.classList.toggle("is-active", running && step === activeStep);
        item.setAttribute("aria-current", running && step === activeStep ? "step" : "false");
    });
}

async function handleCancel() {
    if (!state.running) return;
    const ok = window.confirm("Cancelar a análise? O progresso até aqui será perdido.");
    if (!ok) return;

    const jobId = state.jobId;
    state.running = false;
    stopAnalysisTimers();
    // Avisa o servidor para parar (libera a CPU) e encerra a requisição.
    fetch(API.cancel(jobId), { method: "POST" }).catch(() => {});
    if (state.abort) state.abort.abort();

    document.title = BASE_TITLE;
    showView("upload");          // mantém o arquivo escolhido
    showToast("Análise cancelada. Sua imagem continua selecionada.");
    dom.btnAnalyze.focus();
}

// O cartão de progresso é recriado a cada análise (ao concluir ele vira
// um resumo compacto); guardamos o HTML original e religamos as refs.
const PROGRESS_CARD_HTML = dom.progressCard.innerHTML;

function bindProgressCard() {
    dom.progressTitle   = $("progress-title");
    dom.progressDetail  = $("progress-detail");
    dom.progressPercent = $("progress-percent");
    dom.progressBar     = $("progress-bar");
    dom.progressFill    = $("progress-fill");
    dom.progressLive    = $("progress-live");
    dom.timeElapsed     = $("time-elapsed");
    dom.timeRemaining   = $("time-remaining");
    dom.timeFinish      = $("time-finish");
    dom.stepper         = $("stepper");
    dom.btnCancel       = $("btn-cancel");
    dom.btnCancel.addEventListener("click", handleCancel);
    refreshIcons();
}


// ══════════════════════════════════════════════════════════════
// Resultados
// ══════════════════════════════════════════════════════════════

/** PDF com várias páginas: mostra qual foi lida e permite ler outra. */
function renderPagePick(page) {
    if (!page || !(page.total > 1)) {
        dom.pagePick.hidden = true;
        return;
    }
    dom.pageSelect.innerHTML = Array.from({ length: page.total }, (_, i) =>
        `<option value="${i + 1}"${i + 1 === page.numero ? " selected" : ""}>${i + 1}</option>`).join("");
    dom.pageTotal.textContent = `de ${page.total}`;
    // Prédio: sugere todas as páginas quando cabem numa leitura (até 30).
    dom.pagesInput.value = state.pages || (page.total <= MAX_BUILDING_PAGES ? "todas" : "");
    dom.pagePick.hidden = false;
}

const MAX_BUILDING_PAGES = 30;

/** Quantas páginas a especificação pede ("todas", "1-6", "2,4,7-9"); 0 se inválida. */
function countPages(spec, total) {
    const s = spec.trim().toLowerCase();
    if (["todas", "all", "*"].includes(s)) return total;
    const pages = new Set();
    for (const part of s.split(/[,;]/)) {
        const p = part.trim();
        if (!p) continue;
        const m = p.match(/^(\d+)\s*-\s*(\d+)$|^(\d+)$/);
        if (!m) return 0;
        const [a, b] = m[1] ? [Number(m[1]), Number(m[2])] : [Number(m[3]), Number(m[3])];
        for (let n = Math.min(a, b); n <= Math.max(a, b); n++) {
            if (n < 1 || n > total) return 0;
            pages.add(n);
        }
    }
    return pages.size;
}

/** Prédio lido em várias páginas: área de cada pavimento, repetição e soma. */
function renderBuilding(data) {
    const p = data.predio;
    if (!p) {
        dom.buildingCard.hidden = true;
        return;
    }
    dom.buildingRows.innerHTML = (p.pavimentos || []).map((f) => {
        const has = f.area_m2 != null;
        const mult = f.multiplicador || 1;
        const util = f.area_kind === "util" ? " (útil)" : "";
        const conf = (CONFIDENCE[f.confidence] || {}).label;
        const how = (METHOD_SHORT[f.area_method] || f.area_method || "—") +
            (has && conf ? ` · confiança ${conf.toLowerCase()}` : "");
        return `
            <tr${has ? "" : ' class="is-missing"'}>
                <td>${f.pagina}</td>
                <td>${escapeHtml(how)}</td>
                <td class="num">${has ? `${numberFmt(f.area_m2, 2)} m²${util}` : "—"}</td>
                <td class="num"${f.texto_multiplicador ? ` title="${escapeHtml(f.texto_multiplicador)}"` : ""}>${mult > 1 ? `×${mult}` : "—"}</td>
                <td class="num">${has ? `${numberFmt(f.area_m2 * mult, 2)} m²` : "—"}</td>
            </tr>`;
    }).join("");
    dom.buildingSum.textContent = p.soma_m2 != null
        ? `${numberFmt(p.soma_m2, 2)} m²${p.tipo_area === "util" ? " (útil)" : ""}` : "—";
    dom.buildingStatus.textContent = p.completo
        ? "Todas as páginas com área"
        : `${p.sem_area.length} página(s) sem área`;
    dom.buildingStatus.classList.toggle("is-warn", !p.completo);

    let note = "";
    if (data.area_method === "documento") {
        note = `A área usada no prazo e no custo é o total escrito no documento (${pagesText((data.documento || {}).paginas).replace(/^na?s? /, "")}). ` +
            "A tabela mostra o que cada página da planta trouxe.";
    } else if (!p.completo) {
        note = `Total não calculado: a(s) página(s) ${p.sem_area.join(", ")} não têm área escrita legível nem cotas ` +
            "para dar a escala do desenho. A soma acima é só do que foi lido — não entra no prazo nem no custo.";
    } else if (p.tipo_area === "util") {
        note = "Alguma página trouxe só a área útil (soma dos ambientes): o total fica abaixo da área construída.";
    }
    dom.buildingNote.textContent = note;
    dom.buildingNote.hidden = !note;
    dom.buildingCard.hidden = false;
}

function renderResults(data) {
    const seconds = data.processing_time_s || (Date.now() - state.startedAt) / 1000;

    // Progresso → resumo compacto
    dom.progressCard.className = "progress-card progress-card--done";
    dom.progressCard.innerHTML = `
        <i data-lucide="circle-check"></i>
        <span>Análise concluída em ${formatDuration(seconds)}.</span>
    `;
    dom.analysisHeading.textContent = "Resultado da análise";
    dom.resultActions.hidden = false;
    renderPagePick(data.page);
    document.title = `✓ Análise concluída — AGEPA`;

    // Métricas (área e prazo podem faltar: nada é inventado)
    state.result = data;
    dom.metrics.setAttribute("aria-busy", "false");
    renderAreaMetric(data.area_m2, AREA_LABELS[data.area_kind] || "Área",
        (AREA_HELP[data.area_method] || (() => ""))(data) +
        (data.area_externa_m2 > 0 ? ` · + ${numberFmt(data.area_externa_m2, 2)} m² externos` : ""));
    renderPrazoMetric(data.prazo_meses, data.prazo);
    animateNumber(dom.metricRooms, data.qtd_comodos, 0, "");
    const conf = CONFIDENCE[data.confidence] || CONFIDENCE.baixa;
    dom.metricConfidence.innerHTML =
        `<span class="conf-badge conf-badge--${escapeHtml(data.confidence || "baixa")}">${conf.label}</span>`;
    dom.metricConfidenceHelp.textContent = conf.help;
    dom.btnEditArea.hidden = false;
    if (data.area_m2 == null) openAreaEdit(true);

    renderBasis(data);
    renderBuilding(data);
    // Custo por m² CONSTRUÍDO: com área útil, o servidor manda o contorno das paredes, se medido.
    if (data.area_m2 != null) showCost((data.area_obra && data.area_obra.area_m2) || data.area_m2);
    else dom.costCard.hidden = true;
    renderRooms(data);
    renderImage(data);
    renderTechDetails(data, seconds);

    refreshIcons();
    dom.analysisHeading.focus();
}

// ── Custo estimado (CUB) ────────────────────────────────────

async function loadCostOptions() {
    if (state.cost.options) return state.cost.options;
    const response = await fetch(API.costOptions);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.cost.options = await response.json();
    return state.cost.options;
}

/** Monta o seletor de tipologia e os botões de padrão do estado atual. */
function renderCostControls() {
    const opts = state.cost.options;
    const estado = opts.estados[state.cost.uf];
    if (!estado.tipologias[state.cost.tipologia]) {
        state.cost.tipologia = Object.keys(estado.tipologias)[0];
    }
    dom.costTipologia.innerHTML = Object.entries(estado.tipologias)
        .map(([cod, tip]) =>
            `<option value="${escapeHtml(cod)}">${escapeHtml(tip.nome)} (${escapeHtml(cod)})</option>`)
        .join("");
    dom.costTipologia.value = state.cost.tipologia;

    const disponiveis = estado.tipologias[state.cost.tipologia].padroes;
    dom.costPadrao.innerHTML = Object.entries(opts.padroes)
        .map(([cod, nome]) => {
            const ok = disponiveis.includes(cod);
            return `<button type="button" class="segmented__btn" data-padrao="${escapeHtml(cod)}"
                        ${ok ? "" : "disabled title=\"Sem valor publicado para esta tipologia\""}
                        aria-pressed="false">${escapeHtml(nome)}</button>`;
        })
        .join("");

    const [ano, mes] = estado.referencia.split("-");
    dom.costRef.textContent = `CUB ${state.cost.uf} · ${mes}/${ano}`;
    dom.costExcluded.innerHTML = opts.nao_inclui.map((i) => `<li>${escapeHtml(i)}</li>`).join("");
    dom.costSource.textContent =
        `Fonte: ${estado.fonte} (${estado.regime.toLowerCase()}), método da ${opts.norma}. ` +
        `Valores de referência; não substituem um orçamento.`;
}

function setActivePadrao(padrao) {
    dom.costPadrao.querySelectorAll(".segmented__btn").forEach((btn) => {
        const active = btn.dataset.padrao === padrao;
        btn.classList.toggle("is-active", active);
        btn.setAttribute("aria-pressed", String(active));
    });
}

/** Busca o custo no backend e atualiza o card (ignora respostas antigas). */
async function updateCost() {
    const { area, uf, tipologia, padrao } = state.cost;
    if (!area) return;
    const requestId = ++state.cost.requestId;
    try {
        const response = await fetch(API.cost(area, uf, tipologia, padrao));
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        if (requestId !== state.cost.requestId) return;

        // O backend pode ajustar o padrão (ex.: PIS só tem "Baixo").
        state.cost.padrao = data.padrao;
        setActivePadrao(data.padrao);
        dom.costTotal.textContent = currencyFmt(data.custo_total);
        dom.costFormula.textContent =
            `${numberFmt(data.area_m2, 2)} m² × ${currencyFmt(data.cub_m2, 2)}/m² (padrão ${data.padrao_nome.toLowerCase()})`;

        const nomes = state.cost.options.padroes;
        dom.costRange.innerHTML = Object.entries(data.faixa)
            .map(([p, valor]) => `
                <div class="${p === data.padrao ? "is-current" : ""}">
                    <dt>${escapeHtml(nomes[p])}</dt>
                    <dd>${currencyFmt(valor)}</dd>
                </div>`)
            .join("");
    } catch (error) {
        if (requestId !== state.cost.requestId) return;
        dom.costTotal.textContent = "Indisponível";
        dom.costFormula.textContent = `Não foi possível calcular o custo: ${error.message}`;
        dom.costRange.innerHTML = "";
    }
}

/** Exibe o card de custo para a área da análise concluída. */
async function showCost(area) {
    state.cost.area = area;
    try {
        await loadCostOptions();
    } catch {
        dom.costCard.hidden = true;     // backend sem rota de custo: não mostra o card
        return;
    }
    if (state.cost.area !== area) return;
    renderCostControls();
    setActivePadrao(state.cost.padrao);
    dom.costCard.hidden = false;
    refreshIcons();
    updateCost();
}

// ── Métricas de área e prazo ────────────────────────────────

function renderAreaMetric(area, label, help) {
    dom.metricAreaLabel.textContent = label;
    if (area == null) {
        dom.metricArea.innerHTML = '<span class="metric-card__empty">—</span>';
    } else {
        animateNumber(dom.metricArea, area, 2, "m²");
    }
    dom.metricAreaHelp.textContent = help;
}

/** "Entre 5,1 e 21,8 meses (80%) · Obras públicas · 18 obras" (ou o aviso do demonstrativo). */
function prazoDetail(info) {
    const parts = [];
    if (info.intervalo_meses) {
        const [lo, hi] = info.intervalo_meses;
        parts.push(`Entre ${numberFmt(lo, 1)} e ${numberFmt(hi, 1)} meses (80%)`);
    }
    if (info.dados_reais && info.base) {
        parts.push(info.base);
        if (info.n_obras) parts.push(`${numberFmt(info.n_obras, 0)} obras reais`);
    } else {
        parts.push(info.descricao || "Modelo de prazo");
    }
    if (info.fora_da_faixa) parts.push("fora da faixa de treino");
    if (info.area_base === "contorno") {
        parts.push(`com ${numberFmt(info.area_usada_m2, 2)} m² do contorno das paredes`);
    } else if (info.area_base === "util") {
        parts.push("com a área útil (sem paredes): tende a sair baixo");
    }
    return parts.join(" · ");
}

function renderPrazoMetric(prazo, info) {
    dom.metricPrazoAlt.hidden = true;
    if (prazo == null) {
        dom.metricPrazo.innerHTML = '<span class="metric-card__empty">—</span>';
        dom.metricPrazoHelp.textContent = "Precisa da área para calcular";
        renderPrazoDocumento();
        return;
    }
    animateNumber(dom.metricPrazo, prazo, 1, "meses");
    if (!info) {
        dom.metricPrazoHelp.textContent = "Previsto pelo modelo de prazo";
        renderPrazoDocumento();
        return;
    }
    // Modelo real: faixa de 80% + base e quantas obras; demonstrativo: dito com todas as letras.
    dom.metricPrazoHelp.textContent = prazoDetail(info);
    dom.metricPrazoHelp.classList.toggle("metric-card__help--warn", !info.dados_reais);

    // Outras bases (ex.: CNO): mostradas ao lado, nunca misturadas ao principal.
    // A ressalva de cada base (aviso) vai para a lista de notas.
    const others = info.comparacoes || [];
    if (!others.length) {
        renderPrazoDocumento();
        return;
    }
    dom.metricPrazoAlt.innerHTML = others.map((c) => {
        const detail = [];
        if (c.intervalo_meses) {
            detail.push(`Entre ${numberFmt(c.intervalo_meses[0], 1)} e ${numberFmt(c.intervalo_meses[1], 1)} meses (80%)`);
        }
        if (c.n_obras) detail.push(`${numberFmt(c.n_obras, 0)} obras reais`);
        if (c.fora_da_faixa) detail.push("fora da faixa de treino");
        return `
        <div class="metric-card__alt-row"${c.aviso ? ` title="${escapeHtml(c.aviso)}"` : ""}>
            <span class="metric-card__alt-value">${numberFmt(c.prazo_meses, 1)} meses</span>
            · ${escapeHtml(c.base || "Comparação")}<br>${escapeHtml(detail.join(" · "))}
        </div>`;
    }).join("");
    dom.metricPrazoAlt.hidden = false;
    renderPrazoDocumento();
}

/** Prazo de execução escrito no edital/memorial (o contratado), ao lado do previsto. */
function renderPrazoDocumento() {
    const pd = state.result && state.result.prazo_documento;
    if (!pd) return;
    const dias = Math.round(pd.meses * 30);
    const row = `
        <div class="metric-card__alt-row" title="${escapeHtml(pd.texto)}">
            <span class="metric-card__alt-value">${numberFmt(pd.meses, pd.meses % 1 ? 1 : 0)} meses</span>
            · no documento (pág. ${escapeHtml(pd.paginas.slice(0, 3).join(", "))})<br>prazo contratado, ≈ ${dias} dias
        </div>`;
    dom.metricPrazoAlt.innerHTML = row + (dom.metricPrazoAlt.hidden ? "" : dom.metricPrazoAlt.innerHTML);
    dom.metricPrazoAlt.hidden = false;
}

/** Notas do prazo: ressalvas e avisos dos modelos de comparação. */
function prazoNotes(info) {
    const notes = [];
    for (const c of (info && info.comparacoes) || []) {
        if (c.aviso) notes.push({ kind: "info", text: `Prazo pelo modelo ${c.base}: ${c.aviso}` });
        for (const w of c.warnings || []) notes.push({ kind: "warn", text: w });
    }
    return notes;
}

// ── Correção manual da área ─────────────────────────────────

function openAreaEdit(required = false) {
    dom.areaEdit.hidden = false;
    dom.areaEdit.classList.toggle("area-edit--required", required);
    dom.areaEditTitle.textContent = required
        ? "Informe a área para calcular prazo e custo"
        : "Corrigir a área da obra";
    dom.btnAreaCancel.hidden = required;
    dom.areaEditError.hidden = true;
    const current = state.result && state.result.area_m2;
    dom.areaInput.value = current ? numberFmt(current, 2).replace(/\./g, "") : "";
    if (!required) dom.areaInput.focus();
}

function closeAreaEdit() {
    dom.areaEdit.hidden = true;
    dom.areaEditError.hidden = true;
}

/** "1.234,56" / "402,43" / "402.43" → número (ou NaN). */
function parseAreaInput(text) {
    let s = String(text).trim().replace(/\s|m²|m2/gi, "");
    if (s.includes(",")) s = s.replace(/\./g, "").replace(",", ".");
    return Number(s);
}

async function applyManualArea(event) {
    event.preventDefault();
    const area = parseAreaInput(dom.areaInput.value);
    if (!Number.isFinite(area) || area <= 0 || area > 1_000_000) {
        dom.areaEditError.textContent = "Digite a área em m², por exemplo 402,43.";
        dom.areaEditError.hidden = false;
        dom.areaInput.focus();
        return;
    }
    const data = state.result || {};
    try {
        const response = await fetch(API.schedule(area, data.qtd_comodos,
            data.prazo && data.prazo.tipologia_fnde, data.prazo && data.prazo.uso_obra));
        const prazo = await response.json();
        if (!response.ok) throw new Error(prazo.detail || `HTTP ${response.status}`);

        const lida = data.area_m2 != null ? ` · lida: ${numberFmt(data.area_m2, 2)} m²` : "";
        renderAreaMetric(area, "Área informada", `Corrigida por você${lida}`);
        renderPrazoMetric(prazo.prazo_meses, prazo);
        dom.metricConfidence.innerHTML = '<span class="conf-badge conf-badge--alta">Informada</span>';
        dom.metricConfidenceHelp.textContent = "Área digitada por você";
        renderBasisNotes(data, [
            { kind: "info", text: `Área corrigida manualmente para ${numberFmt(area, 2)} m²; prazo e custo foram recalculados.` },
            ...(prazo.warnings || []).map((w) => ({ kind: "warn", text: w })),
            ...prazoNotes(prazo),
        ]);
        closeAreaEdit();
        showCost(area);
        showToast("Área atualizada: prazo e custo recalculados.");
    } catch (error) {
        dom.areaEditError.textContent = `Não foi possível recalcular: ${error.message}`;
        dom.areaEditError.hidden = false;
    }
}

// ── Origem da área: método, conferências e avisos ───────────

function renderBasis(data) {
    const method = AREA_METHODS[data.area_method] || {
        title: "Área estimada", text: () => "", warning: true,
    };
    const warn = method.warning || data.confidence !== "alta";
    dom.basis.className = warn ? "notice notice--warning" : "notice";
    dom.basisTitle.textContent = method.title;
    dom.basisText.textContent = method.text(data);
    renderBasisNotes(data, prazoNotes(data.prazo));
    dom.basis.hidden = false;
}

function renderBasisNotes(data, extra = []) {
    const notes = [
        ...extra,
        ...(data.checks || []).map((t) => ({ kind: "ok", text: t })),
        ...(data.warnings || []).map((t) => ({ kind: "warn", text: t })),
    ];
    if (data.orientation_deg) {
        notes.push({ kind: "info", text: `A prancha foi girada ${data.orientation_deg}° para a leitura.` });
    }
    dom.basisWarnings.className = "notice__list notice__list--notes";
    dom.basisWarnings.innerHTML = notes
        .map((n) => `<li class="note note--${n.kind}">${escapeHtml(n.text)}</li>`)
        .join("");
}

/**
 * Lista os ambientes: internos (maior área primeiro) e depois externos.
 * Nomes repetidos sem área (ex.: 3× "Jardim") viram uma linha só.
 */
function renderRooms(data) {
    const rooms = data.rooms || [];
    if (rooms.length === 0) {
        dom.roomList.innerHTML = `
            <div class="empty-state">
                <strong>Nenhum ambiente identificado</strong>
                Os nomes dos cômodos não ficaram legíveis nesta imagem.
            </div>`;
        dom.roomTotals.hidden = true;
        return;
    }

    const grouped = [];
    const byName = new Map();
    for (const room of rooms) {
        if (room.area_m2 == null) {
            const key = `${room.name.toLowerCase()}|${room.external}|${room.pagina || ""}`;
            if (byName.has(key)) {
                byName.get(key).count += 1;
                continue;
            }
            const entry = { ...room, count: 1 };
            byName.set(key, entry);
            grouped.push(entry);
        } else {
            grouped.push({ ...room, count: 1 });
        }
    }
    grouped.sort((a, b) =>
        (a.external - b.external) || ((b.area_m2 ?? -1) - (a.area_m2 ?? -1))
    );

    const built = data.area_m2 || 0;
    const shareOf = data.area_kind === "util" ? "da área útil" : "da área total";
    // No PDF o texto é exato: ambiente sem área é porque a planta não a informa.
    const missing = data.text_source === "pdf" ? "sem área na planta" : "área não lida";
    dom.roomList.innerHTML = grouped.map((room) => {
        const hasArea = room.area_m2 != null;
        const share = hasArea && !room.external && built ? (room.area_m2 / built) * 100 : null;
        const name = escapeHtml(room.name) + (room.count > 1 ? ` <span class="room__tag">${room.count}×</span>` : "");
        const tag = (room.area_source === "circulo"
            ? '<span class="room__tag" title="Valor escrito dentro de um círculo na planta">◯ círculo</span>' : "") +
            (room.pagina ? `<span class="room__tag" title="Página do PDF onde está este ambiente">pág. ${room.pagina}</span>` : "");
        return `
            <div class="room${room.external ? " room--external" : ""}">
                <div class="room__row">
                    <span class="room__name">${name}${room.external ? '<span class="room__tag">externa</span>' : ""}${tag}</span>
                    <span class="room__area${hasArea ? "" : " room__area--missing"}">
                        ${hasArea ? `${numberFmt(room.area_m2, 2)} m²` : missing}
                    </span>
                </div>
                ${share !== null ? `
                    <div class="room__bar" aria-hidden="true"><span style="width:${Math.min(share, 100).toFixed(1)}%"></span></div>
                    <p class="room__share">${numberFmt(share, 1)}% ${shareOf}</p>` : ""}
            </div>`;
    }).join("");

    dom.totalBuilt.textContent = data.rooms_sum_m2
        ? `${numberFmt(data.rooms_sum_m2, 2)} m² (${data.rooms_with_area})`
        : "—";
    dom.totalExternal.textContent = data.area_externa_m2 > 0
        ? `${numberFmt(data.area_externa_m2, 2)} m²`
        : "—";
    dom.roomTotals.hidden = false;
}

function base64ToBlobUrl(dataUrl) {
    const [header, b64] = dataUrl.split(",");
    const mime = (header.match(/data:(.*?);/) || [])[1] || "image/png";
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    return URL.createObjectURL(new Blob([bytes], { type: mime }));
}

function renderImage(data) {
    dom.imageFrame.classList.remove("image-frame--scanning");
    if (state.annotatedUrl) URL.revokeObjectURL(state.annotatedUrl);
    state.annotatedUrl = data.annotated_image_b64 ? base64ToBlobUrl(data.annotated_image_b64) : null;
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = data.original_image_b64 ? base64ToBlobUrl(data.original_image_b64) : null;

    dom.legendContorno.hidden = !(data.contorno && data.contorno.valido);
    if (state.annotatedUrl) {
        dom.imageToggle.hidden = false;
        dom.imageLegend.hidden = false;
        dom.btnDownload.disabled = false;
        setImageVersion("annotated");
    } else {
        dom.btnDownload.disabled = true;
        setImageVersion("original");
    }
    dom.imageOpen.hidden = false;
}

/** @param {"annotated" | "original"} version */
function setImageVersion(version) {
    const annotated = version === "annotated" && state.annotatedUrl;
    // PDF: "Original" é a página renderizada pelo servidor.
    const original = state.isPdf ? (state.previewUrl || state.annotatedUrl) : state.originalUrl;
    const url = annotated ? state.annotatedUrl : original;
    dom.resultImage.src = url;
    dom.resultImage.alt = annotated
        ? "Planta baixa com os ambientes e textos identificados destacados"
        : "Planta baixa original enviada";
    dom.imageOpen.href = url;
    dom.imageToggle.querySelectorAll(".segmented__btn").forEach((btn) => {
        const active = btn.dataset.version === (annotated ? "annotated" : "original");
        btn.classList.toggle("is-active", active);
        btn.setAttribute("aria-pressed", String(active));
    });
}

function renderTechDetails(data, seconds) {
    const methodTitle = (AREA_METHODS[data.area_method] || {}).title || data.area_method;
    const env = data.envelope;
    const facts = [
        ["Tempo de processamento", formatDuration(seconds)],
        ["Fonte do texto", data.text_source === "pdf" ? "PDF (texto exato)"
            : `OCR (${data.ocr_engine || "Florence-2"}${data.ocr_engine && data.ocr_engine.includes("+") ? ": leitura rápida + conferência dos números" : ""})`],
        ["Dispositivo", data.text_source === "pdf" ? "— (sem IA)" : (state.server.device === "cuda" ? "GPU (CUDA)" : "CPU")],
        ["Rotação aplicada", `${data.orientation_deg || 0}°`],
        ["Método da área", methodTitle],
        ["Área do carimbo", data.declared_area ? `${numberFmt(data.declared_area.valor_m2, 2)} m²` : "não encontrada"],
        ["Envoltória das cotas", env
            ? `${numberFmt(env.largura_m, 2)} × ${numberFmt(env.profundidade_m, 2)} m (cotas em ${env.unidade})` +
              (env.confiavel === false ? " · parcial ou não conferida (não serve de prova)" : "")
            : "não calculada"],
        ["Textos lidos", String((data.ocr_texts || []).length)],
        ["Modelo de prazo", data.prazo
            ? (data.prazo.dados_reais
                ? `${data.prazo.base} · ${data.prazo.formula} · ${data.prazo.descricao} · erro médio ${numberFmt(data.prazo.mae_meses, 1)} meses`
                : `${data.prazo.descricao} — treine com obras reais (dados/obras_reais.csv)`)
            : "—"],
        ...((data.prazo && data.prazo.comparacoes) || []).map((c) => [
            "Comparação de prazo",
            `${c.base} · ${c.formula} · ${c.descricao}` +
            (c.mae_meses != null ? ` · erro médio ${numberFmt(c.mae_meses, 1)} meses` : ""),
        ]),
    ];
    if (data.page && data.page.total > 1) facts.push(["Página lida", `${data.page.numero} de ${data.page.total}`]);
    const c = data.contorno;
    if (c) {
        facts.push(["Contorno das paredes", `${numberFmt(c.area_m2, 2)} m² · ` + (c.valido
            ? (c.usado ? "usado como área (a planta não escreve área)" : "estimativa conferida")
            : `descartado: ${c.motivo}`)]);
    }
    if (data.documento) {
        facts.push(["Área no documento", `${numberFmt(data.documento.valor, 2)} m² · ${pagesText(data.documento.paginas).replace(/^na?s? /, "")}`]);
    }
    if (data.pagina_area) {
        const pa = data.pagina_area;
        facts.push(["Leitura da página", pa.area_m2 != null
            ? `${numberFmt(pa.area_m2, 2)} m² (${AREA_LABELS[pa.area_kind] || pa.area_kind}) · ${METHOD_SHORT[pa.area_method] || pa.area_method}`
            : "sem área legível"]);
    }
    dom.techFacts.innerHTML = facts
        .map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd></div>`)
        .join("");

    const texts = data.ocr_texts || [];
    dom.ocrCount.textContent = String(texts.length);
    dom.ocrList.innerHTML = texts.length
        ? texts.map((t) => {
            const key = /[ÁA]REA|M²|M2/i.test(t) ? ' class="is-key"' : "";
            return `<li${key}>${escapeHtml(t)}</li>`;
        }).join("")
        : "<li>Nenhum texto lido</li>";

    const od = Object.entries(data.detection_summary || {});
    dom.odCount.textContent = String(od.reduce((sum, [, n]) => sum + n, 0));
    dom.odList.innerHTML = od.length
        ? od.map(([label, n]) => `<li>${escapeHtml(label)} × ${n}</li>`).join("")
        : "<li>Nenhum objeto detectado</li>";

    dom.techDetails.hidden = false;
}

function downloadAnnotatedImage() {
    if (!state.annotatedUrl) return;
    const link = document.createElement("a");
    link.href = state.annotatedUrl;
    const base = (state.file?.name || "planta").replace(/\.[^.]+$/, "");
    link.download = `${base}_anotada_AGEPA.png`;
    document.body.appendChild(link);
    link.click();
    link.remove();
}


// ══════════════════════════════════════════════════════════════
// Erros
// ══════════════════════════════════════════════════════════════

function showAnalysisError(error) {
    const offline = error instanceof TypeError;      // fetch não alcançou o servidor
    const tips = [];

    if (offline) {
        dom.errorTitle.textContent = "Sem conexão com o servidor de IA";
        dom.errorText.textContent = "A imagem não chegou ao backend em localhost:8000.";
        tips.push("Confira se o backend está rodando: uvicorn main:app --port 8000");
        tips.push("Se o servidor reiniciou durante a análise, envie novamente.");
        checkServer();
    } else if (error.status === 400) {
        dom.errorTitle.textContent = "Arquivo não aceito";
        dom.errorText.textContent = error.message;
        tips.push("Use PDF, PNG, JPG, BMP ou TIFF com até 50 MB.");
    } else {
        dom.errorTitle.textContent = "Não foi possível analisar a planta";
        dom.errorText.textContent = error.message;
        tips.push("Tente novamente; se persistir, verifique o terminal do backend.");
        tips.push("Imagens muito escuras, borradas ou cortadas podem falhar na leitura.");
    }
    dom.errorTips.innerHTML = tips.map((t) => `<li>${escapeHtml(t)}</li>`).join("");
    document.title = BASE_TITLE;
    showView("error");
}


// ══════════════════════════════════════════════════════════════
// Eventos — Botões
// ══════════════════════════════════════════════════════════════

dom.btnAnalyze.addEventListener("click", handleAnalyze);
dom.btnNewAnalysis.addEventListener("click", resetToUpload);
dom.btnReadPage.addEventListener("click", () => {
    const n = Number(dom.pageSelect.value);
    if (!n || state.running) return;
    state.page = n;
    state.pages = null;
    handleAnalyze();
});
dom.btnReadAll.addEventListener("click", () => {
    if (state.running) return;
    const spec = dom.pagesInput.value.trim() || "todas";
    const total = (state.result && state.result.page && state.result.page.total) || 0;
    const n = countPages(spec, total);
    if (!n) {
        showToast(`Páginas inválidas: use “todas”, 1-6 ou 2,4,7 (o PDF tem ${total}).`);
        dom.pagesInput.focus();
        return;
    }
    if (n > MAX_BUILDING_PAGES) {
        showToast(`No máximo ${MAX_BUILDING_PAGES} páginas por leitura: escolha as plantas (ex.: 1-6).`);
        dom.pagesInput.focus();
        return;
    }
    const ok = window.confirm(
        `Ler ${n} página(s) como um prédio e somar os pavimentos?\n\n` +
        "Páginas com texto no PDF levam segundos. Páginas sem texto passam pelo OCR: cerca de " +
        "5 minutos cada. Dá para cancelar a qualquer momento."
    );
    if (!ok) return;
    state.pages = spec;
    state.page = null;
    handleAnalyze();
});
dom.btnRetry.addEventListener("click", () => (state.file ? handleAnalyze() : resetToUpload()));
dom.btnOtherFile.addEventListener("click", resetToUpload);
dom.btnDownload.addEventListener("click", downloadAnnotatedImage);
dom.btnReconnect.addEventListener("click", () => {
    dom.serverStatusText.textContent = "Conectando ao servidor…";
    checkServer();
});
dom.btnEditArea.addEventListener("click", () => openAreaEdit(false));
dom.btnAreaCancel.addEventListener("click", closeAreaEdit);
dom.areaEdit.addEventListener("submit", applyManualArea);
dom.costTipologia.addEventListener("change", () => {
    state.cost.tipologia = dom.costTipologia.value;
    renderCostControls();
    setActivePadrao(state.cost.padrao);
    updateCost();
});
dom.costPadrao.addEventListener("click", (e) => {
    const btn = e.target.closest(".segmented__btn");
    if (!btn || btn.disabled) return;
    state.cost.padrao = btn.dataset.padrao;
    setActivePadrao(state.cost.padrao);
    updateCost();
});
dom.imageToggle.addEventListener("click", (e) => {
    const btn = e.target.closest(".segmented__btn");
    if (btn) setImageVersion(btn.dataset.version);
});

// Evita fechar a aba sem querer no meio de uma análise longa.
window.addEventListener("beforeunload", (e) => {
    if (state.running) {
        e.preventDefault();
        e.returnValue = "";
    }
});


// ══════════════════════════════════════════════════════════════
// Inicialização
// ══════════════════════════════════════════════════════════════

document.addEventListener("DOMContentLoaded", () => {
    bindProgressCard();
    showView("upload");
    checkServer();
});
