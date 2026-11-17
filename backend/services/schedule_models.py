# ============================================================
# AGEPA — Modelos de Prazo (ajuste, validação e seleção)
# services/schedule_models.py
# ============================================================
#
# Modelos que estimam o prazo da obra (meses) a partir da área
# (e, se houver, do número de cômodos), treinados com OBRAS REAIS
# da planilha dados/obras_reais.csv (ver train_prazo.py).
#
# Candidatos, do mais simples ao mais flexível:
#   produtividade     prazo = área ÷ P            (P = m²/mês mediano)
#   potencia          prazo = e^a · área^b        (forma do modelo de
#                     Bromilow, clássico em gestão de obras)
#   potencia_robusta  a mesma curva, ajuste de Huber (obra parada pesa menos)
#   potencia_comodos  prazo = e^a · área^b · cômodos^c
#   random_forest     RandomForest(área[, cômodos]) — só com muitos dados
#
# A escolha é por validação cruzada (o erro é medido em obras que o
# modelo NÃO viu no ajuste). Com erros parecidos (até 5%), vence o
# modelo mais simples: com poucos dados, simples generaliza melhor.
#
# Sem leitura/escrita de arquivos aqui: só matemática (testável).
# ============================================================

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

# Quantil 90% da normal padrão: intervalo central de 80%.
Z80: float = 1.2815515655446004
MIN_OBRAS: int = 8                  # menos que isso não dá para validar nada
MIN_OBRAS_COMODOS: int = 15
MIN_OBRAS_RF: int = 30
SIMPLER_TOLERANCE: float = 0.05     # até 5% pior no MAE e mais simples → vence


# ══════════════════════════════════════════════════════════════
# Modelos
# ══════════════════════════════════════════════════════════════

class PrazoModel:
    tipo: str = ""
    nome: str = ""
    descricao: str = ""
    complexidade: int = 0           # nº de parâmetros (RandomForest: muito alto)
    usa_comodos: bool = False

    def fit(self, area: np.ndarray, comodos: Optional[np.ndarray], prazo: np.ndarray) -> "PrazoModel":
        raise NotImplementedError

    def predict(self, area: np.ndarray, comodos: Optional[np.ndarray]) -> np.ndarray:
        raise NotImplementedError

    def params(self) -> Dict[str, float]:
        return {}

    def formula(self) -> str:
        return self.descricao


class Produtividade(PrazoModel):
    tipo, nome, complexidade = "produtividade", "Produtividade mediana", 1
    descricao = "prazo = área ÷ produtividade (m²/mês)"

    def fit(self, area, comodos, prazo):
        self.m2_por_mes = float(np.median(area / prazo))
        return self

    def predict(self, area, comodos):
        return area / self.m2_por_mes

    def params(self):
        return {"m2_por_mes": self.m2_por_mes}

    def formula(self):
        return f"prazo = área ÷ {self.m2_por_mes:.2f} m²/mês"


def _lstsq(columns: List[np.ndarray], target: np.ndarray) -> np.ndarray:
    a = np.vstack([np.ones_like(target), *columns]).T
    coef, *_ = np.linalg.lstsq(a, target, rcond=None)
    return coef


class Potencia(PrazoModel):
    tipo, nome, complexidade = "potencia", "Potência (log-linear)", 2
    descricao = "ln(prazo) = a + b·ln(área)  →  prazo = e^a · área^b"

    def fit(self, area, comodos, prazo):
        self.a, self.b = (float(c) for c in _lstsq([np.log(area)], np.log(prazo)))
        return self

    def predict(self, area, comodos):
        return np.exp(self.a + self.b * np.log(area))

    def params(self):
        return {"a": self.a, "b": self.b}

    def formula(self):
        return f"prazo = {math.exp(self.a):.4f} · área^{self.b:.3f}"


class PotenciaRobusta(Potencia):
    """Mesma curva, ajustada por Huber (IRLS) em ln(prazo).

    Obra pública parada meses (licitação, aditivo, falta de verba) puxa a reta dos
    mínimos quadrados para cima; no ajuste de Huber o resíduo grande pesa menos.
    Conta como um parâmetro a mais (a constante de Huber): só vence a potência
    comum com MAE mais de 5% menor. Nas 18 obras públicas (leave-one-out) ficou em
    5,57 × 5,70 meses (−2%): empate técnico, segue a potência comum.
    """
    tipo, nome, complexidade = "potencia_robusta", "Potência robusta (Huber)", 3
    descricao = "ln(prazo) = a + b·ln(área), ajuste robusto (Huber)  →  prazo = e^a · área^b"
    K: float = 1.345                    # constante de Huber (95% de eficiência na normal)

    def fit(self, area, comodos, prazo):
        x = np.vstack([np.ones_like(area, dtype=float), np.log(area)]).T
        y = np.log(prazo)
        coef, *_ = np.linalg.lstsq(x, y, rcond=None)
        for _ in range(100):
            r = y - x @ coef
            s = 1.4826 * float(np.median(np.abs(r - np.median(r))))      # desvio robusto (MAD)
            k = self.K * max(s, 1e-6)
            w = np.sqrt(np.where(np.abs(r) <= k, 1.0, k / np.maximum(np.abs(r), 1e-12)))
            new, *_ = np.linalg.lstsq(x * w[:, None], y * w, rcond=None)
            done = float(np.max(np.abs(new - coef))) < 1e-9
            coef = new
            if done:
                break
        self.a, self.b = (float(c) for c in coef)
        return self


class PotenciaComodos(PrazoModel):
    tipo, nome, complexidade, usa_comodos = "potencia_comodos", "Potência com cômodos", 3, True
    descricao = "ln(prazo) = a + b·ln(área) + c·ln(cômodos)"

    def fit(self, area, comodos, prazo):
        self.a, self.b, self.c = (float(v) for v in _lstsq([np.log(area), np.log(comodos)], np.log(prazo)))
        return self

    def predict(self, area, comodos):
        return np.exp(self.a + self.b * np.log(area) + self.c * np.log(comodos))

    def params(self):
        return {"a": self.a, "b": self.b, "c": self.c}

    def formula(self):
        return f"prazo = {math.exp(self.a):.4f} · área^{self.b:.3f} · cômodos^{self.c:.3f}"


class FlorestaAleatoria(PrazoModel):
    tipo, nome, complexidade = "random_forest", "RandomForest", 1000
    descricao = "RandomForest (média de 300 árvores de decisão)"

    def __init__(self, usa_comodos: bool = False) -> None:
        self.usa_comodos = usa_comodos
        self.estimator: Any = None

    def _x(self, area, comodos):
        cols = [area, comodos] if self.usa_comodos else [area]
        return np.vstack(cols).T

    def fit(self, area, comodos, prazo):
        from sklearn.ensemble import RandomForestRegressor
        self.estimator = RandomForestRegressor(
            n_estimators=300, min_samples_leaf=2, random_state=42, n_jobs=-1,
        ).fit(self._x(area, comodos), prazo)
        return self

    def predict(self, area, comodos):
        return self.estimator.predict(self._x(area, comodos))

    def formula(self):
        return "RandomForest(" + ("área, cômodos" if self.usa_comodos else "área") + ")"


SIMPLE_MODELS: Dict[str, type] = {
    m.tipo: m for m in (Produtividade, Potencia, PotenciaRobusta, PotenciaComodos)
}


def model_from_params(tipo: str, params: Dict[str, float]) -> PrazoModel:
    """Recria um modelo simples a partir dos parâmetros salvos no JSON."""
    model = SIMPLE_MODELS[tipo]()
    for key, value in params.items():
        setattr(model, key, float(value))
    return model


# ══════════════════════════════════════════════════════════════
# Validação cruzada e métricas
# ══════════════════════════════════════════════════════════════

def folds(n: int, seed: int = 42) -> List[np.ndarray]:
    """Leave-one-out até 40 obras; acima disso, 10 partes embaralhadas."""
    if n <= 40:
        return [np.array([i]) for i in range(n)]
    order = np.random.default_rng(seed).permutation(n)
    return [order[i::10] for i in range(10)]


def cross_val_predict(
    factory: Callable[[], PrazoModel], area: np.ndarray,
    comodos: Optional[np.ndarray], prazo: np.ndarray,
) -> np.ndarray:
    pred = np.empty_like(prazo, dtype=float)
    idx = np.arange(len(prazo))
    for test in folds(len(prazo)):
        train = np.setdiff1d(idx, test)
        c_train = comodos[train] if comodos is not None else None
        c_test = comodos[test] if comodos is not None else None
        model = factory().fit(area[train], c_train, prazo[train])
        pred[test] = model.predict(area[test], c_test)
    return pred


@dataclass
class Metrics:
    mae: float              # erro absoluto médio (meses)
    mape: float             # erro percentual médio
    rmse: float
    r2: float               # das previsões da validação cruzada
    sigma_log: float        # desvio dos erros em ln(prazo) → intervalo

    def to_dict(self) -> Dict[str, float]:
        return {"mae_meses": round(self.mae, 2), "mape": round(self.mape, 4),
                "rmse_meses": round(self.rmse, 2), "r2": round(self.r2, 3),
                "sigma_log": round(self.sigma_log, 4)}


def metrics(prazo: np.ndarray, pred: np.ndarray) -> Metrics:
    err = pred - prazo
    sst = float(np.sum((prazo - prazo.mean()) ** 2))
    log_res = np.log(prazo) - np.log(np.clip(pred, 1e-6, None))
    return Metrics(
        mae=float(np.mean(np.abs(err))),
        mape=float(np.mean(np.abs(err) / prazo)),
        rmse=float(np.sqrt(np.mean(err ** 2))),
        r2=1 - float(np.sum(err ** 2)) / sst if sst > 0 else 0.0,
        sigma_log=float(np.std(log_res, ddof=1)) if len(prazo) > 1 else 0.0,
    )


# ══════════════════════════════════════════════════════════════
# Comparação e escolha
# ══════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    tipo: str
    nome: str
    formula_geral: str
    complexidade: int
    factory: Callable[[], PrazoModel]
    metrics: Optional[Metrics] = None
    cv_pred: Optional[np.ndarray] = None
    motivo_fora: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"modelo": self.tipo, "nome": self.nome, "formula": self.formula_geral}
        if self.metrics:
            out.update(self.metrics.to_dict())
        else:
            out["fora_da_comparacao"] = self.motivo_fora
        return out


@dataclass
class TrainingResult:
    model: PrazoModel
    chosen: Candidate
    candidates: List[Candidate]
    n: int
    validacao: str
    faixa: Dict[str, Any] = field(default_factory=dict)


def train(area: np.ndarray, comodos: Optional[np.ndarray], prazo: np.ndarray) -> TrainingResult:
    """Compara os candidatos por validação cruzada e ajusta o escolhido em tudo."""
    n = len(prazo)
    if n < MIN_OBRAS:
        raise ValueError(f"São necessárias pelo menos {MIN_OBRAS} obras válidas (há {n}).")
    if np.any(area <= 0) or np.any(prazo <= 0):
        raise ValueError("Área e prazo precisam ser positivos.")
    has_comodos = comodos is not None and np.all(comodos > 0)

    candidates = [
        Candidate("produtividade", Produtividade.nome, Produtividade.descricao, 1, Produtividade),
        Candidate("potencia", Potencia.nome, Potencia.descricao, 2, Potencia),
        Candidate("potencia_robusta", PotenciaRobusta.nome, PotenciaRobusta.descricao, 3, PotenciaRobusta),
        Candidate("potencia_comodos", PotenciaComodos.nome, PotenciaComodos.descricao, 3, PotenciaComodos),
        Candidate("random_forest", FlorestaAleatoria.nome, FlorestaAleatoria.descricao, 1000,
                  lambda: FlorestaAleatoria(usa_comodos=has_comodos)),
    ]
    for cand in candidates:
        if cand.tipo == "potencia_comodos" and not has_comodos:
            cand.motivo_fora = "planilha sem cômodos em todas as obras"
        elif cand.tipo == "potencia_comodos" and n < MIN_OBRAS_COMODOS:
            cand.motivo_fora = f"precisa de {MIN_OBRAS_COMODOS}+ obras"
        elif cand.tipo == "random_forest" and n < MIN_OBRAS_RF:
            cand.motivo_fora = f"precisa de {MIN_OBRAS_RF}+ obras"
        else:
            c = comodos if has_comodos else None
            cand.cv_pred = cross_val_predict(cand.factory, area, c, prazo)
            cand.metrics = metrics(prazo, cand.cv_pred)

    valid = [c for c in candidates if c.metrics]
    best_mae = min(c.metrics.mae for c in valid)
    # Empate técnico (até 5% pior) → o mais simples.
    near = [c for c in valid if c.metrics.mae <= best_mae * (1 + SIMPLER_TOLERANCE)]
    chosen = min(near, key=lambda c: (c.complexidade, c.metrics.mae))

    model = chosen.factory().fit(area, comodos if has_comodos else None, prazo)
    faixa = {
        "area_m2": [float(area.min()), float(area.max())],
        "prazo_meses": [float(prazo.min()), float(prazo.max())],
        "comodos": [int(comodos.min()), int(comodos.max())] if has_comodos else None,
    }
    return TrainingResult(
        model=model, chosen=chosen, candidates=candidates, n=n,
        validacao="leave-one-out" if n <= 40 else "10 partes (k-fold)", faixa=faixa,
    )


MIN_OBRAS_USO: int = 100


def train_por_uso(area: np.ndarray, prazo: np.ndarray, usos: np.ndarray,
                  min_n: int = MIN_OBRAS_USO) -> Dict[str, Dict[str, Any]]:
    """Curva de potência PRÓPRIA de cada tipo de uso (casa, prédio, comercial...).

    Entra só o uso com ``min_n``+ obras cuja curva própria erra MENOS que a curva geral
    nas obras daquele uso (as duas medidas em validação cruzada). No CNO (15/11): casas
    −5%, prédios residenciais −3% (o geral superestimava 18%), comercial −8% (superestimava
    21%); galpão piorou (+2%) e fica com a curva geral.
    """
    geral = cross_val_predict(Potencia, area, None, prazo)
    out: Dict[str, Dict[str, Any]] = {}
    for uso in sorted({u for u in usos if u}):
        idx = np.where(usos == uso)[0]
        if len(idx) < min_n:
            continue
        a, p = area[idx], prazo[idx]
        proprio = cross_val_predict(Potencia, a, None, p)
        mae_uso, mae_geral = float(np.mean(np.abs(proprio - p))), float(np.mean(np.abs(geral[idx] - p)))
        if mae_uso >= mae_geral:
            continue
        model = Potencia().fit(a, None, p)
        met = metrics(p, proprio)
        out[uso] = {
            "parametros": model.params(), "formula": model.formula(), "n_obras": int(len(idx)),
            # Faixa onde estão 96% das obras do uso (há cadastro fora do padrão, como uma
            # "casa" de 24.000 m²): fora dela, vale a curva geral.
            "faixa": {"area_m2": [round(float(np.quantile(a, 0.02)), 2), round(float(np.quantile(a, 0.98)), 2)]},
            "incerteza": {"sigma_log": met.sigma_log, "nivel": 0.8},
            "metricas_cv": {**met.to_dict(), "mae_curva_geral_meses": round(mae_geral, 2)},
        }
    return out


def interval(pred: float, sigma_log: float) -> List[float]:
    """Intervalo de 80% supondo erro multiplicativo (normal em ln)."""
    spread = math.exp(Z80 * sigma_log)
    return [pred / spread, pred * spread]
