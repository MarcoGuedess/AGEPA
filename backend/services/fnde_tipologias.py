# ============================================================
# AGEPA — Projetos padrão do FNDE (creches Tipo 1/2, quadra)
# services/fnde_tipologias.py
# ============================================================
#
# Obras municipais feitas com projeto padrão do FNDE têm o prazo medido em
# milhares de obras iguais no SIMEC (dados/obras_simec_fnde.csv; tabela em
# models/prazo_fnde_tipologias.json, gerada por `train_prazo.py tipologias-fnde`).
# Aqui só se RECONHECE, pelo texto da prancha, se a planta é um desses projetos.
#
# Reconhecimento estrito (na dúvida, nada):
#   - a prancha cita o FNDE (ou o Proinfância / Fundo Nacional de Desenv. da Educação);
#   - creche: "CRECHE"/"PRÉ-ESCOLA"/"EDUCAÇÃO INFANTIL" + "TIPO 1" ou "TIPO 2";
#     sem o tipo escrito (ou com os dois), decide a área (±15% da área do projeto);
#   - quadra: "QUADRA" + "VESTIÁRIO" (sem "PALCO") e nenhuma palavra de creche.
# ============================================================

from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Tuple

from services.plan_reader import normalize

# chave → (tipologia no SIMEC, área construída do projeto padrão, conferida nas pranchas do FNDE)
TIPOLOGIAS: Dict[str, Tuple[str, float]] = {
    "creche_tipo1": ("Creche Pré-Escola - Tipo 1", 1324.27),
    "creche_tipo2": ("Creche Pré-Escola - Tipo 2", 786.07),
    "quadra_vestiario": ("Quadra Escolar Coberta com Vestiário", 785.00),
}
AREA_TOLERANCE: float = 0.15        # contorno mede ~5% a menos; área útil, ~20% a menos

_FNDE_RE = re.compile(r"\bFNDE\b|PROINFANCIA|FUNDO NACIONAL DE DESENVOLVIMENTO DA EDUCACAO")
_CRECHE_RE = re.compile(r"\bCRECHE\b|PRE-?ESCOLA|EDUCACAO INFANTIL|PROINFANCIA")
_TIPO1_RE = re.compile(r"\bTIPO\s*0?1\b")
_TIPO2_RE = re.compile(r"\bTIPO\s*0?2\b")


def _near(area: Optional[float], key: str) -> bool:
    ref = TIPOLOGIAS[key][1]
    return area is not None and abs(area - ref) <= AREA_TOLERANCE * ref


def match_tipologia(texts: Iterable[str], area_m2: Optional[float]) -> Optional[str]:
    """Chave do projeto padrão do FNDE que a prancha é, ou None."""
    t = " ".join(normalize(x) for x in texts if x)
    if not _FNDE_RE.search(t):
        return None
    if _CRECHE_RE.search(t):
        t1, t2 = bool(_TIPO1_RE.search(t)), bool(_TIPO2_RE.search(t))
        if t1 != t2:
            return "creche_tipo1" if t1 else "creche_tipo2"
        near = [k for k in ("creche_tipo1", "creche_tipo2") if _near(area_m2, k)]
        return near[0] if len(near) == 1 else None
    if re.search(r"\bQUADRA\b", t) and "VESTIARIO" in t and not re.search(r"\bPALCO\b", t):
        return "quadra_vestiario"
    return None
