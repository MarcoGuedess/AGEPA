"""uso: py -I simec_extrair.py <csv do SIMEC> <saida.csv>

Extrai creches Tipo 1/2 e quadras com vestiário CONCLUÍDAS (100%) do SIMEC para uma planilha de obras."""
import csv, sys, re, datetime as dt
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10**7)
src, out = sys.argv[1], sys.argv[2]
AREAS = {"Creche Pré-Escola - Tipo 1": (1324.27, "educacao"), "Creche Pré-Escola - Tipo 2": (786.07, "educacao"),
         "Quadra Escolar Coberta com Vestiário": (785.00, "esporte")}
COLS = ["obra", "arquivo_planta", "area_m2", "qtd_comodos", "data_inicio", "data_termino", "dias_paralisados",
        "uso", "pavimentos", "uf", "fonte", "usar", "observacoes"]
def d(s):
    try: return dt.date.fromisoformat((s or "").strip()[:10])
    except ValueError: return None
M = 365.25 / 12
kept, skip = [], {}
for r in csv.DictReader(open(src, encoding="latin-1", newline=""), delimiter=";"):
    tipo = r["Tipo do Projeto"].strip()
    if tipo not in AREAS or r["Situação"].strip() != "Concluída" or r["Tipo da Obra"].strip() != "Construção":
        continue
    def no(why): skip[why] = skip.get(why, 0) + 1
    try: pct = float(r["Percentual de Execução"] or 0)
    except ValueError: pct = 0
    if pct < 99.99: no("execução < 100%"); continue
    a, t = d(r["Data de Assinatura do Contrato"]), d(r["Data de Término do Contrato"])
    if not a or not t or t <= a: no("sem datas de contrato válidas"); continue
    meses = (t - a).days / M
    if not 3 <= meses <= 120: no("vigência fora de 3–120 meses"); continue
    area, uso = AREAS[tipo]
    # Nome que fala de outro projeto ("Tipo B" numa linha "Tipo 2"): a área fica incerta.
    nome, up = r["Nome"].upper(), tipo.upper()
    conflito = (("TIPO 1" in up and re.search(r"TIPO\s*-?\s*(B|C|2|II)\b", nome))
                or ("TIPO 2" in up and re.search(r"TIPO\s*-?\s*(B|C|1|I)\b", nome))
                or ("QUADRA" in up and re.search(r"\bCOBERTURA\b|SEM VESTI|PALCO", nome)))
    vis = d(r["Data da Última Vistoria do Estado ou Município"])
    notas = ["início = assinatura do contrato; fim = término do contrato (vigência com aditivos)"]
    if vis and r["Situação da Vistoria"].strip() == "Concluído":
        notas.append(f"última vistoria 'Concluído': {vis:%d/%m/%Y}")
    if r["Tipo Paralisação"].strip():
        notas.append(f"teve paralisação: {r['Tipo Paralisação'].strip()}")
    if conflito:
        notas.insert(0, "NOME COM OUTRO TIPO DE PROJETO: área incerta")
    kept.append({"obra": r["Nome"].strip(), "area_m2": f"{area:.2f}".replace(".", ","),
                 "data_inicio": f"{a:%d/%m/%Y}", "data_termino": f"{t:%d/%m/%Y}", "uso": uso, "uf": r["UF"].strip(),
                 "fonte": f"SIMEC transparência de obras (FNDE) ID {r['ID'].strip()} — {tipo}", "usar": "não" if conflito else "sim",
                 "observacoes": " | ".join(notas)})
with open(out, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=COLS, delimiter=";", extrasaction="ignore")
    w.writeheader(); w.writerows(kept)
print("gravadas:", len(kept)); [print("  fora:", n, k) for k, n in skip.items()]
