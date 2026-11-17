# ============================================================
# AGEPA — Serviço de Estimativa de Custo
# services/cost_service.py
# ============================================================
#
# Estima o custo da obra pelo método do CUB/m² (NBR 12.721):
#
#     custo = área construída × CUB(estado, tipologia, padrão)
#
# Separado do ai_service.py de propósito: não depende do modelo
# de IA, só da área. Assim o usuário pode trocar o padrão de
# acabamento e recalcular na hora, sem refazer a análise.
#
# Os valores do CUB ficam em data/cub.json (dado, não código):
# para atualizar o mês ou incluir outro estado, basta editar
# o arquivo — nada aqui precisa mudar.
# ============================================================

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agepa.cost_service")

DEFAULT_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "cub.json"


class CostInputError(ValueError):
    """Parâmetro inválido (estado, tipologia, padrão ou área)."""


@dataclass
class CostEstimate:
    """Resultado da estimativa de custo de uma obra."""
    area_m2: float
    uf: str
    tipologia: str
    tipologia_nome: str
    padrao: str
    padrao_nome: str
    cub_m2: float
    custo_total: float
    faixa: Dict[str, float]            # custo em cada padrão disponível
    referencia: str                    # "AAAA-MM"
    regime: str
    fonte: str
    fonte_url: str
    norma: str
    nao_inclui: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "area_m2": round(self.area_m2, 2),
            "uf": self.uf,
            "tipologia": self.tipologia,
            "tipologia_nome": self.tipologia_nome,
            "padrao": self.padrao,
            "padrao_nome": self.padrao_nome,
            "cub_m2": round(self.cub_m2, 2),
            "custo_total": round(self.custo_total, 2),
            "faixa": {k: round(v, 2) for k, v in self.faixa.items()},
            "referencia": self.referencia,
            "regime": self.regime,
            "fonte": self.fonte,
            "fonte_url": self.fonte_url,
            "norma": self.norma,
            "nao_inclui": self.nao_inclui,
        }


class CostEstimator:
    """Calcula o custo de construção a partir da tabela do CUB."""

    def __init__(self, table_path: Path = DEFAULT_TABLE_PATH) -> None:
        with open(table_path, encoding="utf-8") as fh:
            self._table: Dict[str, Any] = json.load(fh)
        self._validate_table()
        logger.info(
            "Tabela CUB carregada: %s",
            ", ".join(
                f"{uf} ({est['referencia']})"
                for uf, est in self._table["estados"].items()
            ),
        )

    # ── Validação da tabela (falha cedo, no startup) ─────────
    def _validate_table(self) -> None:
        padroes = self._table.get("padroes", {})
        estados = self._table.get("estados", {})
        if not estados:
            raise ValueError("cub.json não tem nenhum estado cadastrado.")
        for uf, est in estados.items():
            for cod, tip in est.get("tipologias", {}).items():
                valores = tip.get("valores", {})
                if not valores:
                    raise ValueError(f"cub.json: {uf}/{cod} sem valores.")
                for padrao, valor in valores.items():
                    if padrao not in padroes:
                        raise ValueError(f"cub.json: padrão '{padrao}' desconhecido em {uf}/{cod}.")
                    if not isinstance(valor, (int, float)) or valor <= 0:
                        raise ValueError(f"cub.json: valor inválido em {uf}/{cod}/{padrao}.")

    # ── Consulta das opções (para montar os seletores) ───────
    def options(self) -> Dict[str, Any]:
        padroes = self._table["padroes"]
        return {
            "norma": self._table["norma"],
            "padroes": padroes,
            "nao_inclui": self._table.get("nao_inclui", []),
            "estados": {
                uf: {
                    "nome": est["nome"],
                    "fonte": est["fonte"],
                    "fonte_url": est.get("fonte_url", ""),
                    "referencia": est["referencia"],
                    "regime": est["regime"],
                    "tipologias": {
                        cod: {
                            "nome": tip["nome"],
                            "padroes": [p for p in padroes if p in tip["valores"]],
                        }
                        for cod, tip in est["tipologias"].items()
                    },
                }
                for uf, est in self._table["estados"].items()
            },
        }

    # ── Cálculo ──────────────────────────────────────────────
    def estimate(
        self,
        area_m2: float,
        uf: str = "SP",
        tipologia: str = "R1",
        padrao: Optional[str] = "N",
    ) -> CostEstimate:
        """Custo = área × CUB/m². Sem padrão disponível, usa o mais próximo do normal."""
        if area_m2 is None or area_m2 <= 0:
            raise CostInputError("A área deve ser maior que zero.")

        uf = uf.upper()
        estados = self._table["estados"]
        if uf not in estados:
            raise CostInputError(
                f"Estado '{uf}' sem tabela CUB. Disponíveis: {', '.join(estados)}."
            )
        est = estados[uf]

        tipologia = tipologia.upper()
        if tipologia not in est["tipologias"]:
            raise CostInputError(
                f"Tipologia '{tipologia}' não existe para {uf}. "
                f"Disponíveis: {', '.join(est['tipologias'])}."
            )
        tip = est["tipologias"][tipologia]
        valores: Dict[str, float] = tip["valores"]

        padroes_ordem = list(self._table["padroes"])          # B, N, A
        padrao = (padrao or "N").upper()
        if padrao not in valores:
            # Ex.: PIS só tem "B". Escolhe o disponível mais próximo de "N".
            mid = padroes_ordem.index("N")
            padrao = min(valores, key=lambda p: abs(padroes_ordem.index(p) - mid))

        faixa = {
            p: area_m2 * valores[p]
            for p in padroes_ordem if p in valores
        }

        return CostEstimate(
            area_m2=area_m2,
            uf=uf,
            tipologia=tipologia,
            tipologia_nome=tip["nome"],
            padrao=padrao,
            padrao_nome=self._table["padroes"][padrao],
            cub_m2=valores[padrao],
            custo_total=area_m2 * valores[padrao],
            faixa=faixa,
            referencia=est["referencia"],
            regime=est["regime"],
            fonte=est["fonte"],
            fonte_url=est.get("fonte_url", ""),
            norma=self._table["norma"],
            nao_inclui=self._table.get("nao_inclui", []),
        )
