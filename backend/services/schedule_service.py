# ============================================================
# AGEPA — Serviço de Estimativa de Prazo
# services/schedule_service.py
# ============================================================
#
# Prevê o prazo da obra (meses) a partir da área e dos cômodos.
#
# Modelo PRINCIPAL (nesta ordem):
#   1. models/prazo_model.json — treinado com OBRAS REAIS por
#      train_prazo.py (dados/obras_reais.csv: obras públicas). Traz a
#      fórmula, a faixa de dados, o erro medido em validação cruzada
#      e o intervalo.
#   2. models/random_forest_model.joblib — DEMONSTRATIVO: treinado com
#      dados simulados (kaggle_training_script.py). Mostrado como tal.
#   3. Nenhum arquivo: regra área/15 + 0,2·cômodos (a mesma dos dados
#      simulados), também identificada como demonstrativa.
#
# Modelos de COMPARAÇÃO (opcionais, mostrados ao lado do principal,
# nunca misturados a ele):
#   - models/prazo_model_cno.json — obras privadas do Cadastro Nacional
#     de Obras (Receita Federal), também treinado por train_prazo.py.
#   - models/prazo_fnde_tipologias.json — quando a planta é um projeto padrão
#     do FNDE (creche Tipo 1/2, quadra com vestiário): prazo das obras IGUAIS
#     no SIMEC (mediana e faixa de 80%), por `train_prazo.py tipologias-fnde`.
#
# Todo arquivo JSON que mudar (novo treino) é recarregado sem reiniciar.
# Separado da leitura da planta: o prazo é recalculado quando o
# usuário corrige a área, sem refazer a análise da imagem.
# ============================================================

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np

from services.schedule_models import PrazoModel, interval, model_from_params

logger = logging.getLogger("agepa.schedule_service")

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
REAL_MODEL_FILE = "prazo_model.json"
DEMO_MODEL_FILE = "random_forest_model.joblib"
PRIMARY_BASE = "Obras públicas"          # rótulo quando o JSON não traz "base"

# Comparações: (id, arquivo, rótulo padrão). A ordem é a da interface.
COMPARISON_MODELS = [
    ("cno", "prazo_model_cno.json", "CNO · obras privadas"),
]

# Faixa dos dados simulados do modelo demonstrativo (kaggle_training_script.py).
DEMO_RANGE = {"area_m2": [40.0, 300.0], "comodos": [2, 9]}


@dataclass
class ScheduleEstimate:
    prazo_meses: float
    metodo: str                      # tipo do modelo (potencia, random_forest_demo, ...)
    descricao: str                   # texto curto para a interface
    dados_reais: bool
    fora_da_faixa: bool
    intervalo: Optional[List[float]] = None
    mae_meses: Optional[float] = None
    n_obras: Optional[int] = None
    faixa: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    base_id: Optional[str] = None           # "principal", "cno", "demonstrativo"
    base: Optional[str] = None              # de onde vêm as obras do treino
    formula: Optional[str] = None
    aviso: Optional[str] = None             # ressalva sobre os dados
    comparacoes: List["ScheduleEstimate"] = field(default_factory=list)
    tipologia: Optional[str] = None         # projeto padrão do FNDE reconhecido na planta
    uso: Optional[str] = None               # tipo de uso da obra lido na planta (casa, prédio...)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prazo_meses": round(self.prazo_meses, 1),
            "metodo": self.metodo,
            "descricao": self.descricao,
            "base_id": self.base_id,
            "base": self.base,
            "formula": self.formula,
            "aviso": self.aviso,
            "dados_reais": self.dados_reais,
            "intervalo_meses": [round(v, 1) for v in self.intervalo] if self.intervalo else None,
            "nivel_intervalo": 0.8 if self.intervalo else None,
            "mae_meses": round(self.mae_meses, 1) if self.mae_meses is not None else None,
            "n_obras": self.n_obras,
            "fora_da_faixa": self.fora_da_faixa,
            "faixa_treino": self.faixa,
            "warnings": self.warnings,
            "comparacoes": [c.to_dict() for c in self.comparacoes],
            "tipologia_fnde": self.tipologia,
            "uso_obra": self.uso,
        }


class _CardModel:
    """Um modelo treinado com obras reais (JSON gerado pelo train_prazo.py)."""

    def __init__(self, path: Path, base_id: str, default_base: str) -> None:
        self.path = path
        self.base_id = base_id
        self.default_base = default_base
        self.card: Optional[Dict[str, Any]] = None
        self._model: Optional[PrazoModel] = None
        self._rf: Optional[Any] = None
        self._mtime: Optional[float] = None
        self.load()

    def load(self) -> None:
        self.card, self._model, self._rf = None, None, None
        self._mtime = self.path.stat().st_mtime if self.path.exists() else None
        if self._mtime is None:
            return
        try:
            card = json.loads(self.path.read_text(encoding="utf-8"))
            if card["tipo"] == "random_forest":
                self._rf = joblib.load(self.path.parent / card["arquivo_modelo"])
            else:
                self._model = model_from_params(card["tipo"], card["parametros"])
            self.card = card
            logger.info(
                "Modelo de prazo REAL carregado (%s): %s, %d obras, MAE %.1f meses.",
                self.base, card["nome"], card["metricas_cv"]["n_obras"], card["metricas_cv"]["mae_meses"],
            )
        except Exception:
            logger.exception("%s inválido; ignorado.", self.path.name)

    def maybe_reload(self) -> bool:
        """True se o arquivo mudou (novo treino ou remoção) e foi recarregado."""
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self._mtime:
            logger.info("%s mudou: recarregando.", self.path.name)
            self.load()
            return True
        return False

    @property
    def base(self) -> str:
        return (self.card or {}).get("base") or self.default_base

    def _predict_uso(self, area_m2: float, uso: str, sub: Dict[str, Any]) -> ScheduleEstimate:
        return _predict_uso_estimate(self.card, self.base_id, self.base, area_m2, uso, sub)

    def predict(self, area_m2: float, comodos: int, uso: Optional[str] = None) -> ScheduleEstimate:
        card = self.card
        sub = (card.get("por_uso") or {}).get(uso) if uso else None
        if sub and sub["faixa"]["area_m2"][0] * 0.9 <= area_m2 <= sub["faixa"]["area_m2"][1] * 1.1:
            return self._predict_uso(area_m2, uso, sub)
        area = np.array([area_m2], dtype=float)
        c = np.array([comodos], dtype=float) if card.get("usa_comodos") else None
        if self._rf is not None:
            x = np.vstack([area, c]).T if c is not None else area.reshape(-1, 1)
            prazo = float(self._rf.predict(x)[0])
        else:
            prazo = float(self._model.predict(area, c)[0])

        faixa = card["faixa"]
        lo, hi = faixa["area_m2"]
        out = not (lo * 0.9 <= area_m2 <= hi * 1.1)
        if card.get("usa_comodos") and faixa.get("comodos"):
            out = out or not (faixa["comodos"][0] <= comodos <= faixa["comodos"][1])
        notes = []
        if out:
            notes.append(
                f"Área fora da faixa das obras usadas no treino ({self.base}: {lo:,.0f}–{hi:,.0f} m²): "
                "o prazo é uma extrapolação e pode errar mais.".replace(",", ".")
            )
        n = card["metricas_cv"]["n_obras"]
        return ScheduleEstimate(
            prazo_meses=prazo,
            metodo=card["tipo"],
            descricao=f"{card['nome']} · {n} obras reais",
            dados_reais=True,
            fora_da_faixa=out,
            intervalo=interval(prazo, card["incerteza"]["sigma_log"]),
            mae_meses=card["metricas_cv"]["mae_meses"],
            n_obras=n,
            faixa=faixa,
            warnings=notes,
            base_id=self.base_id,
            base=self.base,
            formula=card.get("formula"),
            aviso=card.get("aviso"),
        )


FNDE_TABLE_FILE = "prazo_fnde_tipologias.json"


# Rótulo curto de cada tipo de uso (coluna "uso" das planilhas de obras).
USO_LABELS: Dict[str, str] = {
    "residencial unifamiliar": "casas", "residencial multifamiliar": "prédios residenciais",
    "comercial": "obras comerciais", "galpao": "galpões", "casa popular": "casas populares",
}


def _predict_uso_estimate(card: Dict[str, Any], base_id: str, base: str, area_m2: float,
                          uso: str, sub: Dict[str, Any]) -> ScheduleEstimate:
    model = model_from_params("potencia", sub["parametros"])
    prazo = float(model.predict(np.array([area_m2], dtype=float), None)[0])
    n, label = int(sub["n_obras"]), USO_LABELS.get(uso, uso)
    return ScheduleEstimate(
        prazo_meses=prazo, metodo="potencia_por_uso",
        descricao=f"curva própria de {label} · {n} obras reais",
        dados_reais=True, fora_da_faixa=False,
        intervalo=interval(prazo, sub["incerteza"]["sigma_log"]),
        mae_meses=sub["metricas_cv"]["mae_meses"], n_obras=n, faixa=sub["faixa"],
        base_id=base_id, base=f"{base} · {label}", formula=sub.get("formula"), aviso=card.get("aviso"),
        uso=uso,
    )


class _FndeTable:
    """Prazo das obras padrão do FNDE iguais à da planta (SIMEC), por tipologia."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.card: Optional[Dict[str, Any]] = None
        self._mtime: Optional[float] = None
        self.maybe_reload()

    def maybe_reload(self) -> None:
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime == self._mtime:
            return
        self._mtime, self.card = mtime, None
        if mtime is None:
            return
        try:
            self.card = json.loads(self.path.read_text(encoding="utf-8"))
            logger.info("Prazos do FNDE por tipologia carregados: %s.",
                        ", ".join(f"{k} ({v['n_obras']} obras)" for k, v in self.card["tipologias"].items()))
        except Exception:
            logger.exception("%s inválido; ignorado.", self.path.name)

    def estimate(self, key: str) -> Optional[ScheduleEstimate]:
        t = (self.card or {}).get("tipologias", {}).get(key)
        if not t:
            return None
        nome = t["tipologia"].replace("Creche Pré-Escola - ", "creches ").replace(
            "Quadra Escolar Coberta com Vestiário", "quadras cobertas com vestiário")
        extra = (f" Até a última vistoria 'Concluído', a mediana é {t['mediana_ate_ultima_vistoria_meses']:.1f} meses."
                 .replace(".", ",", 1) if t.get("mediana_ate_ultima_vistoria_meses") else "")
        return ScheduleEstimate(
            prazo_meses=float(t["mediana_meses"]),
            metodo="tipologia_fnde",
            descricao=f"mediana de {t['n_obras']} obras iguais concluídas ({t['ufs']} UFs)",
            dados_reais=True,
            fora_da_faixa=False,
            intervalo=[float(v) for v in t["intervalo_80_meses"]],
            n_obras=int(t["n_obras"]),
            base_id="fnde",
            base=f"SIMEC/FNDE · {nome} iguais a esta",
            formula="mediana das obras iguais (projeto padrão)",
            aviso=(self.card or {}).get("aviso", "") + extra,
        )


class ScheduleEstimator:
    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        self._dir = Path(model_dir)
        self._primary = _CardModel(self._dir / REAL_MODEL_FILE, "principal", PRIMARY_BASE)
        self._comparisons = [
            _CardModel(self._dir / fname, base_id, label) for base_id, fname, label in COMPARISON_MODELS
        ]
        self._fnde = _FndeTable(self._dir / FNDE_TABLE_FILE)
        self._demo: Optional[Any] = None
        self._demo_loaded = False
        self._ensure_demo()

    # ── Carregamento ─────────────────────────────────────────

    def _ensure_demo(self) -> None:
        """O demonstrativo só é carregado quando não há modelo principal real."""
        if self._primary.card is None and not self._demo_loaded:
            if self._primary.path.exists():
                logger.warning("%s inválido; usando o modelo demonstrativo.", self._primary.path.name)
            self._demo = self._load_demo()
            self._demo_loaded = True

    def _load_demo(self) -> Optional[Any]:
        path = self._dir / DEMO_MODEL_FILE
        if not path.exists():
            logger.warning("Sem modelo de prazo: usando a regra demonstrativa área/15.")
            return None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            model = joblib.load(path)
        if any("InconsistentVersion" in type(w.message).__name__ for w in caught):
            logger.warning("Modelo demonstrativo salvo com outra versão do scikit-learn.")
        logger.info("Modelo de prazo DEMONSTRATIVO carregado (dados simulados).")
        return model

    def _maybe_reload(self) -> None:
        """Novo treino (ou remoção de um JSON) vale sem reiniciar o servidor."""
        if self._primary.maybe_reload():
            self._ensure_demo()
        for model in self._comparisons:
            model.maybe_reload()
        self._fnde.maybe_reload()

    @property
    def real(self) -> bool:
        return self._primary.card is not None

    @property
    def comparisons(self) -> List[str]:
        """Bases de comparação carregadas (ex.: ["cno"])."""
        return [m.base_id for m in self._comparisons if m.card is not None]

    # ── Previsão ─────────────────────────────────────────────

    def predict(self, area_m2: float, qtd_comodos: int, tipologia: Optional[str] = None,
                uso: Optional[str] = None) -> ScheduleEstimate:
        """Prazo do modelo principal, com os modelos de comparação em ``comparacoes``.

        ``tipologia``: projeto padrão do FNDE reconhecido na planta (fnde_tipologias):
        acrescenta o prazo das obras iguais no SIMEC às comparações.
        ``uso``: tipo de uso lido na planta (uso_obra): a base que tiver curva própria
        para ele (CNO: casas, prédios residenciais, comerciais) usa essa curva.
        """
        self._maybe_reload()
        comodos = max(int(qtd_comodos or 1), 1)
        if self._primary.card is not None:
            estimate = self._primary.predict(area_m2, comodos, uso)
        else:
            estimate = self._predict_demo(area_m2, comodos)
        estimate.comparacoes = [
            m.predict(area_m2, comodos, uso) for m in self._comparisons if m.card is not None
        ]
        estimate.uso = uso
        fnde = self._fnde.estimate(tipologia) if tipologia else None
        if fnde is not None:
            estimate.comparacoes.insert(0, fnde)
            estimate.tipologia = tipologia
        return estimate

    def _predict_demo(self, area_m2: float, comodos: int) -> ScheduleEstimate:
        lo, hi = DEMO_RANGE["area_m2"]
        clo, chi = DEMO_RANGE["comodos"]
        out = not (lo <= area_m2 <= hi and clo <= comodos <= chi)
        notes = []
        if out:
            notes.append(
                f"Área ou cômodos fora da faixa do modelo demonstrativo ({lo:.0f}–{hi:.0f} m², "
                f"{clo}–{chi} cômodos): o RandomForest não extrapola e devolve um valor "
                "próximo do limite da faixa."
            )
        if self._demo is not None:
            with warnings.catch_warnings():
                # Treinado com DataFrame; aqui recebe lista (mesma ordem de colunas).
                warnings.filterwarnings("ignore", message="X does not have valid feature names")
                prazo = float(self._demo.predict([[area_m2, comodos]])[0])
            metodo, descricao = "random_forest_demo", "Modelo demonstrativo (dados simulados)"
        else:
            prazo = max(area_m2 / 15.0 + 0.2 * comodos, 3.0)
            metodo, descricao = "regra_demo", "Regra demonstrativa (área ÷ 15)"
        return ScheduleEstimate(
            prazo_meses=prazo, metodo=metodo, descricao=descricao, dados_reais=False,
            fora_da_faixa=out, faixa=DEMO_RANGE, warnings=notes,
            base_id="demonstrativo", base="Dados simulados",
        )
