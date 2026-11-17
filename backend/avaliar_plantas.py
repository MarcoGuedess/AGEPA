# ============================================================
# AGEPA — Avaliação da leitura de plantas contra um gabarito
# avaliar_plantas.py
# ============================================================
#
# Uso (na pasta backend, com o servidor rodando em 127.0.0.1:8000):
#   py avaliar_plantas.py                       # PDFs como estão
#   py avaliar_plantas.py --como-imagem 150     # cada PDF vira PNG (prancha "escaneada")
#   py avaliar_plantas.py --so TIPO1            # só as linhas cujo arquivo contém "TIPO1"
#
# Gabarito: ../avaliacao/gabarito.csv (guia: ../avaliacao/LEIAME.md)
# Saídas:   ../avaliacao/relatorio_avaliacao[_imagem150].md e .csv
#
# Cada planta é enviada ao servidor (/api/analisar), o mesmo caminho do app.
# O resultado fica em cache (avaliacao/resultados/cache): só é refeito se o
# arquivo, o modo ou o código de leitura (services/*.py) mudar.
# ============================================================

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

BACKEND_DIR = Path(__file__).resolve().parent
AVALIACAO_DIR = BACKEND_DIR.parent / "avaliacao"
DEFAULT_GABARITO = AVALIACAO_DIR / "gabarito.csv"
DEFAULT_SERVER = "http://127.0.0.1:8000"

# Campos da resposta guardados no cache (sem as imagens em base64).
KEEP = ("area_m2", "area_kind", "area_method", "confidence", "qtd_comodos", "text_source",
        "rooms_with_area", "rooms_sum_m2", "declared_area", "checks", "warnings", "page",
        "processing_time_s")


@dataclass
class Linha:
    arquivo: str
    area_real: Optional[float]
    tipo_real: str
    total_na_folha: bool
    tolerancia_pct: float
    usar: bool
    fonte: str
    observacoes: str
    pagina: Optional[int] = None      # PDF de várias páginas: qual ler (1 = primeira)


@dataclass
class Resultado:
    linha: Linha
    lido: Optional[float]
    tipo_lido: Optional[str]
    metodo: str
    confianca: str
    fonte_texto: str
    segundos: float
    categoria: str              # acerto | erro | sem resposta | tipo diferente | sem gabarito | falhou
    erro_pct: Optional[float]
    alucinacao: bool
    detalhe: str = ""


# ── Gabarito ─────────────────────────────────────────────────

def _num(text: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    return float(text)


def _sim(text: str) -> bool:
    return (text or "").strip().lower() in ("sim", "s", "yes", "1", "true")


def carregar_gabarito(path: Path) -> List[Linha]:
    raw = path.read_bytes().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw), delimiter=";"))
    obrigatorias = {"arquivo", "area_real_m2", "tipo_area", "total_na_folha", "tolerancia_pct", "usar"}
    faltando = obrigatorias - set(rows[0].keys() if rows else [])
    if faltando:
        raise ValueError(f"Colunas ausentes no gabarito: {', '.join(sorted(faltando))}")
    out = []
    for r in rows:
        if not (r.get("arquivo") or "").strip() or r["arquivo"].lstrip().startswith("#"):
            continue
        out.append(Linha(
            arquivo=r["arquivo"].strip(),
            area_real=_num(r["area_real_m2"]),
            tipo_real=(r["tipo_area"] or "construida").strip().lower(),
            total_na_folha=_sim(r["total_na_folha"]),
            tolerancia_pct=_num(r["tolerancia_pct"]) or 1.0,
            usar=_sim(r["usar"]) if (r["usar"] or "").strip() else True,
            fonte=(r.get("fonte_gabarito") or "").strip(),
            observacoes=(r.get("observacoes") or "").strip(),
            pagina=int(r["pagina"]) if (r.get("pagina") or "").strip().isdigit() else None,
        ))
    return out


# ── Classificação ────────────────────────────────────────────

# Razão (lido ÷ real) aceita quando o tipo lido difere do gabarito:
# - área útil (soma dos ambientes) fica abaixo da construída (paredes, ~10–25%);
# - a envoltória das cotas é um retângulo que contém a obra: nunca menor que ela
#   e, numa planta de um pavimento, raramente mais que o dobro.
FAIXA_OUTRO_TIPO = {
    ("util", "construida"): (0.60, 1.00),
    ("envoltoria", "construida"): (0.95, 2.00),
}

def classificar(linha: Linha, lido: Optional[float], tipo_lido: Optional[str],
                confianca: str) -> tuple[str, Optional[float], bool]:
    """(categoria, erro_pct, alucinação).

    - sem gabarito: a área real não está conferida; só registra o que foi lido.
    - sem resposta: o sistema não deu área (não inventou um número).
    - tipo diferente: leu outro tipo de área (soma dos ambientes = área útil, ou
      envoltória das cotas) e o gabarito é a área construída. Só é aceito se o valor
      for plausível para aquele tipo (FAIXA_OUTRO_TIPO); fora disso é erro.
    - acerto / erro: mesmo tipo de área, dentro ou fora da tolerância.
    - alucinação: erro com confiança "alta" (o pior caso: errado e seguro de si).
    """
    if linha.area_real is None:
        return "sem gabarito", None, False
    if lido is None:
        return "sem resposta", None, False
    erro = 100.0 * (lido - linha.area_real) / linha.area_real
    if tipo_lido != linha.tipo_real:
        faixa = FAIXA_OUTRO_TIPO.get((tipo_lido or "", linha.tipo_real))
        if faixa and faixa[0] <= lido / linha.area_real <= faixa[1]:
            return "tipo diferente", erro, False
        return "erro", erro, confianca == "alta"
    if abs(erro) <= linha.tolerancia_pct:
        return "acerto", erro, False
    return "erro", erro, confianca == "alta"


# ── Execução ─────────────────────────────────────────────────

def _code_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((BACKEND_DIR / "services").glob("*.py")):
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _render_png(pdf_bytes: bytes, dpi: int, pagina: Optional[int] = None) -> bytes:
    """Página lida pelo AGEPA (a escolhida, ou a primeira com 'PLANTA BAIXA') em PNG."""
    import pypdfium2 as pdfium

    from services.pdf_reader import read_pdf

    page_index = read_pdf(pdf_bytes, pagina).page_index
    page = pdfium.PdfDocument(pdf_bytes)[page_index]
    image = page.render(scale=dpi / 72).to_pil().convert("L")
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _post(server: str, filename: str, data: bytes, mime: str, timeout: float,
          pagina: Optional[int] = None) -> Dict:
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + data + b"\r\n"
    if pagina:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"pagina\"\r\n\r\n{pagina}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        server.rstrip("/") + "/api/analisar", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def analisar(linha: Linha, server: str, dpi: Optional[int], cache_dir: Path,
             timeout: float, sem_cache: bool) -> Resultado:
    path = (AVALIACAO_DIR / linha.arquivo)
    data = path.read_bytes()
    modo = (f"imagem{dpi}" if dpi else "original") + (f"_p{linha.pagina}" if linha.pagina else "")
    key = hashlib.sha256(data + modo.encode() + _code_hash().encode()).hexdigest()[:24]
    cache_file = cache_dir / f"{key}.json"

    resp: Optional[Dict] = None
    if cache_file.exists() and not sem_cache:
        resp = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        if dpi and path.suffix.lower() == ".pdf":
            payload, nome, mime = _render_png(data, dpi, linha.pagina), path.stem + f"_{dpi}dpi.png", "image/png"
        else:
            mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png"
            payload, nome = data, path.name
        t0 = time.perf_counter()
        try:
            # Imagem gerada de uma página já é aquela página; PDF vai com a página escolhida.
            full = _post(server, nome, payload, mime, timeout,
                         None if (dpi and path.suffix.lower() == ".pdf") else linha.pagina)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return Resultado(linha, None, None, "-", "-", "-", 0.0, "falhou", None, False,
                             f"HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            return Resultado(linha, None, None, "-", "-", "-", 0.0, "falhou", None, False, str(exc))
        resp = {k: full.get(k) for k in KEEP}
        resp["segundos_total"] = round(time.perf_counter() - t0, 1)
        resp["modo"] = modo
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(resp, ensure_ascii=False, indent=1), encoding="utf-8")

    lido = resp.get("area_m2")
    categoria, erro, aluc = classificar(linha, lido, resp.get("area_kind"), resp.get("confidence") or "")
    return Resultado(
        linha=linha, lido=lido, tipo_lido=resp.get("area_kind"),
        metodo=resp.get("area_method") or "-", confianca=resp.get("confidence") or "-",
        fonte_texto=resp.get("text_source") or "-", segundos=resp.get("segundos_total") or 0.0,
        categoria=categoria, erro_pct=erro, alucinacao=aluc,
    )


# ── Relatório ────────────────────────────────────────────────

def _br(v: Optional[float], d: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def escrever_relatorio(resultados: List[Resultado], md_path: Path, csv_path: Path, modo: str) -> str:
    com_gabarito = [r for r in resultados if r.categoria not in ("sem gabarito", "falhou")]
    cont = {c: sum(r.categoria == c for r in resultados)
            for c in ("acerto", "erro", "sem resposta", "tipo diferente", "sem gabarito", "falhou")}
    aluc = [r for r in resultados if r.alucinacao]
    comparaveis = [abs(r.erro_pct) for r in resultados if r.categoria in ("acerto", "erro")]

    lines = [
        "# Avaliação da leitura de plantas — AGEPA", "",
        f"Gerado em {dt.datetime.now().isoformat(timespec='seconds')}; modo: **{modo}**.", "",
        "## Resumo", "",
        f"- Plantas avaliadas: {len(resultados)} ({len(com_gabarito)} com área real conferida)",
        f"- Acertos (mesmo tipo de área, dentro da tolerância): **{cont['acerto']}**",
        f"- Erros: **{cont['erro']}** — com confiança alta (alucinação): **{len(aluc)}**",
        f"- Sem resposta (o sistema não inventou área): {cont['sem resposta']}",
        f"- Outro tipo de área (útil ou envoltória, gabarito = construída): {cont['tipo diferente']}",
        f"- Sem gabarito (só registrado): {cont['sem gabarito']}; falhas: {cont['falhou']}",
    ]
    if comparaveis:
        lines.append(f"- Erro absoluto médio nas comparáveis: {_br(sum(comparaveis) / len(comparaveis))}%")
    lines += [
        "", "## Planta por planta", "",
        "| Arquivo | Real (m²) | Lido (m²) | Tipo lido | Método | Confiança | Erro | Resultado | Tempo |",
        "|---|---:|---:|---|---|---|---:|---|---:|",
    ]
    for r in resultados:
        nome = Path(r.linha.arquivo).name
        res = r.categoria + (" ⚠ alucinação" if r.alucinacao else "")
        erro = f"{'+' if (r.erro_pct or 0) > 0 else ''}{_br(r.erro_pct)}%" if r.erro_pct is not None else "—"
        lines.append(
            f"| {nome} | {_br(r.linha.area_real)} | {_br(r.lido)} | {r.tipo_lido or '—'} | {r.metodo} | "
            f"{r.confianca} | {erro} | {res}{(' — ' + r.detalhe) if r.detalhe else ''} | {_br(r.segundos, 1)} s |"
        )
    lines += [
        "", "## Como ler", "",
        "- **Real**: área do gabarito, lida à mão na prancha (fonte em `gabarito.csv`).",
        "- **Tolerância**: 0,5% quando o total está impresso na folha (o sistema deve lê-lo); "
        "10% quando precisa medir.",
        "- **Alucinação**: área errada com confiança alta — o caso que o sistema não pode ter.",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["arquivo", "area_real_m2", "area_lida_m2", "tipo_lido", "metodo", "confianca",
                    "fonte_texto", "erro_pct", "resultado", "alucinacao", "segundos"])
        for r in resultados:
            w.writerow([r.linha.arquivo, _br(r.linha.area_real), _br(r.lido), r.tipo_lido or "",
                        r.metodo, r.confianca, r.fonte_texto, _br(r.erro_pct), r.categoria,
                        "sim" if r.alucinacao else "não", _br(r.segundos, 1)])
    return "\n".join(lines[4:12])


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(BACKEND_DIR))
    ap = argparse.ArgumentParser(description="Compara a leitura do AGEPA com o gabarito de plantas.")
    ap.add_argument("--gabarito", type=Path, default=DEFAULT_GABARITO)
    ap.add_argument("--servidor", default=DEFAULT_SERVER)
    ap.add_argument("--como-imagem", type=int, metavar="DPI",
                    help="envia cada PDF como PNG nessa resolução (simula prancha escaneada)")
    ap.add_argument("--so", default="", help="só arquivos cujo caminho contém este texto")
    ap.add_argument("--sem-cache", action="store_true", help="refaz todas as leituras")
    ap.add_argument("--timeout", type=float, default=3600, help="segundos por planta (padrão: 3600)")
    args = ap.parse_args(argv)

    linhas = [l for l in carregar_gabarito(args.gabarito) if l.usar and args.so.lower() in l.arquivo.lower()]
    faltando = [l.arquivo for l in linhas if not (AVALIACAO_DIR / l.arquivo).exists()]
    if faltando:
        print("Arquivos do gabarito não encontrados:\n  " + "\n  ".join(faltando))
        return 1
    modo = f"imagem {args.como_imagem} dpi" if args.como_imagem else "arquivo original"
    sufixo = f"_imagem{args.como_imagem}" if args.como_imagem else ""
    print(f"{len(linhas)} plantas · modo: {modo} · servidor: {args.servidor}")

    resultados = []
    for i, linha in enumerate(linhas, 1):
        print(f"[{i}/{len(linhas)}] {Path(linha.arquivo).name} …", flush=True)
        r = analisar(linha, args.servidor, args.como_imagem, AVALIACAO_DIR / "resultados" / "cache",
                     args.timeout, args.sem_cache)
        print(f"        lido {_br(r.lido)} m² ({r.tipo_lido or '—'}, {r.metodo}, {r.confianca}) · "
              f"real {_br(r.linha.area_real)} → {r.categoria}{' ⚠ ALUCINAÇÃO' if r.alucinacao else ''}"
              f"{(' — ' + r.detalhe) if r.detalhe else ''}", flush=True)
        resultados.append(r)

    out_dir = AVALIACAO_DIR
    md = out_dir / f"relatorio_avaliacao{sufixo}.md"
    resumo = escrever_relatorio(resultados, md, out_dir / f"resultado_avaliacao{sufixo}.csv", modo)
    print("\n" + resumo + f"\n\nRelatório: {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
