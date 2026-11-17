# ============================================================
# AGEPA — Tipo de uso da obra, lido no texto da planta
# services/uso_obra.py
# ============================================================
#
# O prazo das obras PRIVADAS (base CNO) muda com o tipo de uso: no mesmo
# tamanho, casa, prédio residencial e obra comercial levam tempos diferentes
# (curvas próprias no models/prazo_model_cno.json, `train_prazo.py treinar
# --por-uso`). Aqui só se lê, pelas palavras da prancha, qual é o uso — com as
# mesmas categorias da coluna "uso" das planilhas. Na dúvida, nada (curva geral).
#
#   prédio público (escola, creche, UBS, CRAS, FNDE...) → nada (não é obra privada)
#   dormitório/quarto/suíte + apartamento/apto/pavimento tipo/unidade N/
#       penthouse ou área privativa (planta de venda)  → residencial multifamiliar
#   área privativa (planta de venda), sem loja/sala comercial → residencial multifamiliar
#   dormitório/quarto/suíte, sem sinal de várias unidades → residencial unifamiliar
#   galpão, armazém (mesmo com "escritório")               → galpao
#   loja, sala comercial, escritório, shopping, mall      → comercial
# ============================================================

from __future__ import annotations

import re
from typing import Iterable, Optional

from services.plan_reader import normalize

_PUBLIC_RE = re.compile(
    r"\b(?:ESCOLA|CRECHE|PRE-?ESCOLA|EDUCACAO INFANTIL|UBS|UNIDADE BASICA|POSTO DE SAUDE|HOSPITAL|"
    r"POLICLINICA|PRONTO SOCORRO|CRAS|CREAS|FNDE|PROINFANCIA|SALA DE AULA)\b")
_RES_RE = re.compile(r"\b(?:DORMITORIOS?|DORM\.|QUARTOS?|SUITES?)\b")
_MULTI_RE = re.compile(
    r"\b(?:APARTAMENTOS?|APTOS?\.?|PAVIMENTO TIPO|PLANTA TIPO|PENTHOUSE|UNIDADE\s*(?:N\W?\s*)?\d+|"
    r"EDIFICIO RESIDENCIAL|RESIDENCIAL MULTIFAMILIAR|HALL SOCIAL)\b")
_COM_RE = re.compile(
    r"\b(?:LOJAS?|SALAS? COMERCIA(?:L|IS)|ESCRITORIOS?|SHOPPING|MALL|PRACA COMERCIAL|CENTRO COMERCIAL|"
    r"PRACA DE ALIMENTACAO)\b")
_GALPAO_RE = re.compile(r"\b(?:GALPAO|GALPOES|ARMAZEM)\b")


def infer_uso(texts: Iterable[str], area_kind: Optional[str] = None) -> Optional[str]:
    """Uso da obra (categorias do CNO) pelo texto da prancha, ou None."""
    t = " ".join(normalize(x) for x in texts if x)
    if not t or _PUBLIC_RE.search(t):
        return None
    if area_kind == "privativa" and not _COM_RE.search(t):
        return "residencial multifamiliar"     # planta de venda de unidade (book b.rio: "studio")
    if _RES_RE.search(t):
        if area_kind == "privativa" or _MULTI_RE.search(t):
            return "residencial multifamiliar"
        return "residencial unifamiliar"
    if _GALPAO_RE.search(t):
        return "galpao"          # galpão costuma ter "escritório": vem antes do comercial
    if _COM_RE.search(t):
        return "comercial"
    return None
