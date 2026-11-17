# ============================================================
# AGEPA — Testes do treino e do serviço de prazo
# tests/test_schedule.py
#
# Os dados daqui são SINTÉTICOS de propósito: servem para conferir a
# matemática (o ajuste recupera coeficientes conhecidos), nunca para
# treinar o modelo usado pelo sistema.
#
# Rodar (na pasta backend):
#   py -m unittest discover -s tests -v
# ============================================================

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import train_prazo  # noqa: E402
from services.schedule_models import train  # noqa: E402
from services.schedule_service import ScheduleEstimator  # noqa: E402

HEADER = "obra;arquivo_planta;area_m2;qtd_comodos;data_inicio;data_termino;dias_paralisados;uso;pavimentos;uf;fonte;usar;observacoes"


def synthetic_rows(n=20, seed=1, a=0.35, b=0.45):
    """prazo = e^a · área^b · ruído pequeno (lei de potência conhecida)."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        area = float(rng.uniform(60, 2000))
        meses = float(np.exp(a + b * np.log(area)) * np.exp(rng.normal(0, 0.05)))
        dias = round(meses * 365.25 / 12)
        inicio = np.datetime64("2018-01-01") + np.timedelta64(int(rng.integers(0, 900)), "D")
        termino = inicio + np.timedelta64(dias, "D")
        d1 = str(inicio).split("-")
        d2 = str(termino).split("-")
        rows.append(f"Obra {i};;{area:.2f}".replace(".", ",")
                    + f";;{d1[2]}/{d1[1]}/{d1[0]};{d2[2]}/{d2[1]}/{d2[0]};;educacao;1;MG;teste;;")
    return rows


class PlanilhaTest(unittest.TestCase):
    def _sheet(self, lines, encoding="utf-8-sig"):
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False)
        tmp.write("\r\n".join(lines).encode(encoding))
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_reads_brazilian_excel_csv(self):
        sheet = self._sheet([
            HEADER,
            "# comentário: ignorado",
            "Creche A;a.pdf;1.514,30;12;15/03/2019;15/03/2020;30;educacao;1;MG;fonte;;",
            "Casa B;;85,5;;01/02/2021;01/08/2021;;residencial;1;SP;fonte;não;",
            "Erro C;;120;;10/05/2022;01/05/2022;;;;;;;",
        ], encoding="cp1252")
        obras, errors, warnings, excluded = train_prazo.load_obras(sheet)
        self.assertEqual(len(obras), 1)
        self.assertEqual(excluded, 1)                       # usar = não
        self.assertEqual(len(errors), 1)                    # término antes do início
        creche = obras[0]
        self.assertEqual(creche.area_m2, 1514.3)
        self.assertEqual(creche.comodos, 12)
        self.assertAlmostEqual(creche.prazo_meses, (366 - 30) / (365.25 / 12), places=3)

    def test_missing_required_column(self):
        sheet = self._sheet(["obra;area_m2;data_inicio", "X;100;01/01/2020"])
        with self.assertRaises(ValueError):
            train_prazo.load_obras(sheet)


class ModelsTest(unittest.TestCase):
    def test_power_law_recovers_exponent(self):
        rng = np.random.default_rng(0)
        area = rng.uniform(50, 3000, 40)
        prazo = np.exp(0.35 + 0.45 * np.log(area)) * np.exp(rng.normal(0, 0.03, 40))
        result = train(area, None, prazo)
        self.assertEqual(result.chosen.tipo, "potencia")
        self.assertAlmostEqual(result.model.params()["b"], 0.45, delta=0.02)
        self.assertLess(result.chosen.metrics.mape, 0.05)

    def test_simpler_model_wins_a_tie(self):
        # Prazo proporcional à área: produtividade e potência erram igual → vence a mais simples.
        rng = np.random.default_rng(3)
        area = rng.uniform(50, 3000, 25)
        prazo = area / 40 * np.exp(rng.normal(0, 0.02, 25))
        self.assertEqual(train(area, None, prazo).chosen.tipo, "produtividade")

    def test_robust_fit_resists_stopped_works(self):
        # Três obras "paradas" (prazo 3× o normal) puxam os mínimos quadrados; o Huber não.
        from services.schedule_models import Potencia, PotenciaRobusta
        rng = np.random.default_rng(7)
        area = np.exp(rng.uniform(np.log(60), np.log(4000), 30))
        prazo = 1.7 * area ** 0.30 * np.exp(rng.normal(0, 0.05, 30))
        prazo[:3] *= 3
        ols, rob = Potencia().fit(area, None, prazo), PotenciaRobusta().fit(area, None, prazo)
        self.assertLess(abs(rob.a - np.log(1.7)), abs(ols.a - np.log(1.7)))
        self.assertAlmostEqual(rob.b, 0.30, delta=0.03)

    def test_robust_fit_needs_a_clear_win(self):
        # Dados limpos: as duas potências empatam → fica a comum (mais simples).
        rng = np.random.default_rng(0)
        area = rng.uniform(50, 3000, 40)
        prazo = np.exp(0.35 + 0.45 * np.log(area)) * np.exp(rng.normal(0, 0.03, 40))
        result = train(area, None, prazo)
        self.assertEqual(result.chosen.tipo, "potencia")
        self.assertIn("potencia_robusta", [c.tipo for c in result.candidates if c.metrics])

    def test_too_few_works(self):
        with self.assertRaises(ValueError):
            train(np.array([100.0, 200.0]), None, np.array([6.0, 9.0]))

    def test_random_forest_only_with_enough_data(self):
        rng = np.random.default_rng(5)
        area = rng.uniform(50, 3000, 12)
        result = train(area, None, area ** 0.5)
        rf = next(c for c in result.candidates if c.tipo == "random_forest")
        self.assertIsNone(rf.metrics)
        self.assertIn("30+", rf.motivo_fora)


SIMEC_HEADER = ("ID;Ano do Convênio;Nome da Obra;Unidade Implantadora;Campus/Reitoria;Município/UF;"
                "Data de Início;Data de Término;Situação da Obra;Última Atualização;Última Vistoria;"
                "Realizado por;% Executado;Valor da Obra;Composiçao;Valor Empenhado(R$);Valor Liquidado(R$)")


def simec_row(sid, nome, ini, fim, situacao="Concluída", executado="100", composicao=""):
    return (f"{sid};;{nome};Universidade X;Campus A;Cidade / MG;{ini};{fim};{situacao};"
            f"2022-01-01 10:00:00;01/02/2022;Instituição;{executado};1000000,00;{composicao};-;-")


class ImportSimecTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        lines = [
            SIMEC_HEADER,
            simec_row(1001, "PRÉDIO DE SALAS DE AULA", "10/02/2019", "20/12/2019",
                      composicao="Edifício com dois pavimentos; área total de 1.250,40 m², salas e banheiros (12 de 40,00 m2)."),
            ";;;;;;;;;( 400 dia(s) );;;;;;;",
            simec_row(1002, "REFORMA DO BLOCO B", "01/03/2020", "01/09/2020", composicao="Reforma de 300,00 m2"),
            simec_row(1003, "LABORATÓRIO - ETAPA II", "01/03/2018", "01/08/2018", composicao="Área total de 900,00 m²"),
            simec_row(1004, "EXECUÇÃO DE ACESSO E ESTACIONAMENTO", "01/01/2021", "01/06/2021", composicao="690,42 m²"),
            simec_row(1005, "GALPÃO DE MÁQUINAS", "15/04/2017", "15/10/2017", composicao="Galpão de 320 m2"),
            simec_row(1006, "BIBLIOTECA SETORIAL", "01/01/2022", "", situacao="Em Execução", executado="60",
                      composicao="800,00 m²"),
            simec_row(1007, "CENTRO DE VIVÊNCIA", "01/02/2016", "01/02/2017", composicao="Sem metragem informada"),
        ]
        self.csv = self.dir / "simec.csv"
        self.csv.write_text("\r\n".join(lines), encoding="utf-8")
        self.sheet = self.dir / "obras.csv"

    def _run(self, simular=False):
        with redirect_stdout(io.StringIO()) as out:
            code = train_prazo.cmd_importar_simec(self.csv, self.sheet, "UFX", "educacao", simular)
        return code, out.getvalue()

    def test_reads_report_with_filler_lines_and_semicolon_in_description(self):
        obras = train_prazo.read_simec(self.csv)
        self.assertEqual([o["id"] for o in obras], ["1001", "1002", "1003", "1004", "1005", "1006", "1007"])
        self.assertIn("1.250,40", obras[0]["composicao"])     # o ";" da descrição não quebrou a linha
        self.assertEqual(obras[0]["valor_liquidado_r"], "-")

    def test_area_prefers_total(self):
        area, note = train_prazo.pick_area("PRÉDIO", "área total de 1.250,40 m², salas (12 de 40,00 m2)")
        self.assertEqual(area, 1250.4)
        self.assertIn("confira", note)

    def test_classification(self):
        self.assertEqual(train_prazo.classify_simec("REF. E AMPLIAÇÃO DO PRÉDIO")[0], "reforma")
        self.assertEqual(train_prazo.classify_simec("LABORATÓRIO - ETAPA II")[0], "etapa")
        self.assertEqual(train_prazo.classify_simec("EXECUÇÃO DE ACESSO E ESTACIONAMENTO")[0], "infraestrutura")
        self.assertEqual(train_prazo.classify_simec("GALPÃO DE MÁQUINAS"), ("construcao", "galpao"))
        # Casos reais do relatório da UFV:
        self.assertEqual(train_prazo.classify_simec("LABORATÓRIOS DE ENGENHARIA - PROJETO")[0], "projeto")
        self.assertEqual(train_prazo.classify_simec("IMPERMEABILIZAÇÃO DA COBERTURA DO CENTRO")[0], "servico")
        self.assertEqual(train_prazo.classify_simec("RECAPEAMENTO DAS VIAS CAMPUS")[0], "servico")
        self.assertEqual(train_prazo.classify_simec(
            "QUADRAS E PAREDÕES DE TÊNIS", "Reforma e adaptação das quadras")[0], "reforma")
        self.assertEqual(train_prazo.classify_simec(
            "LABS. E SALAS DE AULA", "Construção de 02 Salas de Aula. Reforma de Laboratório")[0], "misto")
        self.assertEqual(train_prazo.classify_simec(
            "PAVILHÃO DE AULAS 5", "Construção do Pavilhão de Aula 5 com um pavimento")[0], "construcao")
        self.assertEqual(train_prazo.classify_simec("CONSTRUÇÃO DA CASA DE QUIMICA PARA ETA")[1], "")

    def test_simulation_writes_nothing(self):
        code, out = self._run(simular=True)
        self.assertEqual(code, 0)
        self.assertFalse(self.sheet.exists())
        self.assertIn("nada foi gravado", out)

    def test_import_marks_what_to_use_and_never_duplicates(self):
        self._run()
        header, rows, _, _ = train_prazo.read_table(self.sheet)
        by_fonte = {r["fonte"]: r for _, r in rows}
        self.assertEqual(len(rows), 5)          # 1006 não concluída e 1007 sem área ficam de fora
        new = by_fonte["UFX dados abertos — SIMEC ID 1001"]
        self.assertEqual((new["usar"], new["area_m2"], new["pavimentos"], new["uf"]), ("sim", "1250,40", "2", "MG"))
        self.assertEqual(by_fonte["UFX dados abertos — SIMEC ID 1005"]["uso"], "galpao")
        for sid in (1002, 1003, 1004):
            self.assertEqual(by_fonte[f"UFX dados abertos — SIMEC ID {sid}"]["usar"], "não")

        self._run()                             # segunda vez: nada repetido
        _, rows_again, _, _ = train_prazo.read_table(self.sheet)
        self.assertEqual(len(rows_again), 5)

        obras, errors, _, excluded = train_prazo.load_obras(self.sheet)
        self.assertEqual((len(obras), len(errors), excluded), (2, 0, 3))


class ObrasgovTest(unittest.TestCase):
    def test_rows_keep_only_buildings_with_real_dates_and_area(self):
        records = [
            {"idUnico": "1.32-1", "uf": "ES", "nome": "Construção de CMEI (creche)", "especie": "Construção",
             "dataInicialEfetiva": "2022-08-22", "dataFinalEfetiva": "2024-08-29",
             "descricao": "Construção de creche com área de 1.098,29 m²", "tipos": [{"descricao": "Educação"}]},
            {"idUnico": "2.32-2", "uf": "DF", "nome": "Reforma de escola", "especie": "Reforma",
             "dataInicialEfetiva": "2021-01-10", "dataFinalEfetiva": "2021-06-10", "descricao": "500 m²"},
            {"idUnico": "3.32-3", "uf": "ES", "nome": "Arena multiuso", "especie": "Construção",
             "dataInicialEfetiva": "2022-10-25", "dataFinalEfetiva": "2024-08-01",
             "descricao": "19.364,28m²", "tipos": [{"descricao": "Infraestrutura Urbana e Mobilidade"}]},
            {"idUnico": "4.32-4", "uf": "MG", "nome": "Posto de saúde", "especie": "Construção",
             "dataInicialEfetiva": "2020-01-01", "dataFinalEfetiva": "2020-12-01", "descricao": "sem metragem"},
            {"idUnico": "5.32-5", "uf": "MG", "nome": "Escola", "especie": "Construção",
             "dataInicialEfetiva": "2020-01-01", "dataFinalEfetiva": "2020-12-01", "descricao": "800 m²"},
        ]
        rows, counts = train_prazo.obrasgov_rows(records, ";", known_ids={"5.32-5"})
        by_id = {r["fonte"].split()[-1]: r for r in rows}
        self.assertEqual(set(by_id), {"1.32-1", "2.32-2", "3.32-3"})
        creche = by_id["1.32-1"]
        self.assertEqual((creche["usar"], creche["area_m2"], creche["data_inicio"], creche["uso"]),
                         ("sim", "1098,29", "22/08/2022", "educacao"))
        self.assertEqual(by_id["2.32-2"]["usar"], "não")      # reforma
        self.assertEqual(by_id["3.32-3"]["usar"], "não")      # infraestrutura
        self.assertEqual(counts["sem área no texto"], 1)
        self.assertEqual(counts["já estavam na planilha"], 1)


class EndToEndTest(unittest.TestCase):
    def test_train_save_and_serve(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sheet = tmp / "obras.csv"
            sheet.write_text("\r\n".join([HEADER, *synthetic_rows()]), encoding="utf-8-sig")
            out, report = tmp / "models" / "prazo_model.json", tmp / "relatorio.md"
            with redirect_stdout(io.StringIO()):
                code = train_prazo.cmd_treinar(sheet, out, report, ignore_errors=False)
            self.assertEqual(code, 0)
            card = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(card["metricas_cv"]["n_obras"], 20)
            self.assertIn("Comparação dos modelos", report.read_text(encoding="utf-8"))

            est = ScheduleEstimator(model_dir=out.parent)
            r = est.predict(500.0, 8)
            self.assertTrue(r.dados_reais)
            expected = np.exp(0.35 + 0.45 * np.log(500.0))
            self.assertAlmostEqual(r.prazo_meses, expected, delta=0.1 * expected)
            lo, hi = r.intervalo
            self.assertLess(lo, r.prazo_meses)
            self.assertGreater(hi, r.prazo_meses)
            self.assertFalse(r.fora_da_faixa)
            self.assertTrue(est.predict(50_000.0, 8).fora_da_faixa)

            # Sem o JSON, volta ao modelo demonstrativo sem reiniciar.
            out.unlink()
            demo = est.predict(500.0, 8)
            self.assertFalse(demo.dados_reais)
            self.assertIn("demonstrativ", demo.descricao.lower())

    def test_comparison_model_shown_beside_primary(self):
        """O modelo do CNO aparece em `comparacoes`, sem mudar o principal."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            models = tmp / "models"
            primary_sheet, cno_sheet = tmp / "publicas.csv", tmp / "cno.csv"
            primary_sheet.write_text("\r\n".join([HEADER, *synthetic_rows(seed=1)]), encoding="utf-8-sig")
            cno_sheet.write_text("\r\n".join([HEADER, *synthetic_rows(seed=2, a=0.9, b=0.3)]),
                                 encoding="utf-8-sig")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(train_prazo.cmd_treinar(
                    primary_sheet, models / "prazo_model.json", tmp / "r1.md", ignore_errors=False), 0)

            est = ScheduleEstimator(model_dir=models)
            alone = est.predict(500.0, 8)
            self.assertEqual(alone.comparacoes, [])
            self.assertEqual(alone.base, "Obras públicas")

            # Treinar a comparação passa a valer sem reiniciar o servidor.
            with redirect_stdout(io.StringIO()):
                self.assertEqual(train_prazo.cmd_treinar(
                    cno_sheet, models / "prazo_model_cno.json", tmp / "r2.md", ignore_errors=False,
                    base="CNO · obras privadas", aviso="Fim administrativo."), 0)
            both = est.predict(500.0, 8)
            self.assertAlmostEqual(both.prazo_meses, alone.prazo_meses, places=6)   # principal intacto
            self.assertEqual(len(both.comparacoes), 1)
            cno = both.comparacoes[0]
            self.assertEqual((cno.base_id, cno.base, cno.aviso), ("cno", "CNO · obras privadas", "Fim administrativo."))
            self.assertTrue(cno.dados_reais)
            expected = np.exp(0.9 + 0.3 * np.log(500.0))
            self.assertAlmostEqual(cno.prazo_meses, expected, delta=0.1 * expected)
            self.assertEqual(both.to_dict()["comparacoes"][0]["base"], "CNO · obras privadas")
            self.assertEqual(est.comparisons, ["cno"])

            # Sem o modelo principal: o demonstrativo assume, e a comparação continua real.
            (models / "prazo_model.json").unlink()
            demo = est.predict(500.0, 8)
            self.assertFalse(demo.dados_reais)
            self.assertTrue(demo.comparacoes[0].dados_reais)


if __name__ == "__main__":
    unittest.main()


class AreaObraTest(unittest.TestCase):
    """Prazo e custo usam a área CONSTRUÍDA (modelos e CUB são por m² construído)."""

    def _report(self, area, kind, contorno=None):
        from types import SimpleNamespace
        return SimpleNamespace(area_m2=area, area_kind=kind, contorno=contorno)

    def test_built_area_is_used_as_is(self):
        from main import _area_obra
        self.assertEqual(_area_obra(self._report(402.43, "construida")),
                         {"area_m2": 402.43, "base": "construida", "nota": None})
        self.assertIsNone(_area_obra(self._report(None, None)))

    def test_useful_area_uses_the_measured_outline(self):
        # Creche FNDE tipo 1: útil 1.045,25; contorno 1.262,5 (real 1.324,27).
        from main import _area_obra
        c = {"area_m2": 1262.5, "valido": True, "usado": False}
        out = _area_obra(self._report(1045.25, "util", c))
        self.assertEqual((out["area_m2"], out["base"]), (1262.5, "contorno"))
        self.assertIn("contorno das paredes", out["nota"])

    def test_useful_area_without_outline_warns(self):
        from main import _area_obra
        for c in (None, {"area_m2": 888.8, "valido": False, "usado": False},
                  {"area_m2": 900.0, "valido": True, "usado": False}):      # contorno menor que a útil: não serve
            out = _area_obra(self._report(954.71, "util", c))
            self.assertEqual((out["area_m2"], out["base"]), (954.71, "util"))
            self.assertIn("sem paredes", out["nota"])


class FndeTipologiaTest(unittest.TestCase):
    """Projeto padrão do FNDE: reconhecido pelo texto da prancha (e a área, na dúvida)."""

    def test_recognises_fnde_standard_projects(self):
        from services.fnde_tipologias import match_tipologia
        # Textos reais das pranchas (carimbo do FNDE).
        self.assertEqual(match_tipologia(["FNDE", "CRECHE - TIPO 1", "BERÇÁRIO"], 1262.46), "creche_tipo1")
        self.assertEqual(match_tipologia(["Proinfância", "Creche Tipo 2"], 617.59), "creche_tipo2")
        self.assertEqual(match_tipologia(["FNDE", "QUADRA COBERTA", "VESTIÁRIO MASC."], 785.0), "quadra_vestiario")
        # Sem o tipo escrito: decide a área (±15%).
        self.assertEqual(match_tipologia(["FNDE", "CRECHE"], 1300.0), "creche_tipo1")
        self.assertIsNone(match_tipologia(["FNDE", "CRECHE"], 1000.0))

    def test_needs_fnde_and_the_right_words(self):
        from services.fnde_tipologias import match_tipologia
        self.assertIsNone(match_tipologia(["CRECHE MUNICIPAL", "TIPO 1"], 1324.27))       # sem FNDE
        self.assertIsNone(match_tipologia(["FNDE", "QUADRA 12 LOTE 3"], 785.0))           # quadra de rua
        self.assertIsNone(match_tipologia(["FNDE", "QUADRA", "VESTIÁRIO", "PALCO"], 785.0))  # outro projeto
        self.assertIsNone(match_tipologia(["FNDE", "ESCOLA 6 SALAS"], 1324.27))

    def test_fnde_comparison_beside_the_models(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "prazo_fnde_tipologias.json").write_text(json.dumps({
            "aviso": "Datas de CONTRATO.", "tipologias": {"creche_tipo1": {
                "tipologia": "Creche Pré-Escola - Tipo 1", "area_m2": 1324.27, "n_obras": 487,
                "mediana_meses": 30.0, "intervalo_80_meses": [12.7, 66.0], "media_meses": 35.6,
                "mediana_ate_ultima_vistoria_meses": 34.9, "ufs": 26}}}), encoding="utf-8")
        est = ScheduleEstimator(model_dir=tmp)
        out = est.predict(1262.46, 52, "creche_tipo1").to_dict()
        fnde = out["comparacoes"][0]
        self.assertEqual((fnde["base_id"], fnde["prazo_meses"], fnde["intervalo_meses"], fnde["n_obras"]),
                         ("fnde", 30.0, [12.7, 66.0], 487))
        self.assertIn("CONTRATO", fnde["aviso"])
        self.assertEqual(out["tipologia_fnde"], "creche_tipo1")
        self.assertEqual(est.predict(1262.46, 52).to_dict()["comparacoes"], [])     # sem tipologia, nada


class UsoObraTest(unittest.TestCase):
    """Obra privada: o tipo de uso lido na planta escolhe a curva própria do CNO."""

    def test_infer_use_from_plan_text(self):
        from services.uso_obra import infer_uso
        self.assertEqual(infer_uso(["SUÍTE", "DORMITÓRIO", "GARAGEM", "CONSTRUÇÃO DE RESIDENCIA UNIFAMILIAR"]),
                         "residencial unifamiliar")
        self.assertEqual(infer_uso(["UNIDADE 02", "DORMITÓRIO 1", "HALL"]), "residencial multifamiliar")
        self.assertEqual(infer_uso(["4 suítes", "Living"], "privativa"), "residencial multifamiliar")   # planta de venda
        self.assertEqual(infer_uso(["OPÇÃO STUDIO", "ÁREA PRIVATIVA 34,51 M2"], "privativa"), "residencial multifamiliar")
        self.assertEqual(infer_uso(["SALA COMERCIAL 1205", "ÁREA PRIVATIVA 38,00 M2"], "privativa"), "comercial")
        self.assertEqual(infer_uso(["LOJA 01", "LOJA 02", "SANITÁRIO"]), "comercial")
        self.assertEqual(infer_uso(["GALPÃO", "ESCRITÓRIO"]), "galpao")        # escritório do galpão
        self.assertEqual(infer_uso(["GALPÃO", "DOCAS"]), "galpao")
        self.assertIsNone(infer_uso(["CRECHE TIPO 1", "FNDE", "BERÇÁRIO", "SUÍTE"]))     # obra pública
        self.assertIsNone(infer_uso(["SALA", "COZINHA"]))

    def test_per_use_curve_only_when_it_helps(self):
        from services.schedule_models import train_por_uso
        rng = np.random.default_rng(5)
        n = 400
        area = np.exp(rng.uniform(np.log(50), np.log(800), n))
        usos = np.array(["casa"] * 200 + ["comercial"] * 200)
        prazo = np.where(usos == "casa", 1.3 * area ** 0.45, 6.0 * area ** 0.12) * np.exp(rng.normal(0, 0.1, n))
        out = train_por_uso(area, prazo, usos, min_n=100)
        self.assertEqual(set(out), {"casa", "comercial"})
        self.assertAlmostEqual(out["casa"]["parametros"]["b"], 0.45, delta=0.05)
        # Uso igual à curva geral: a curva própria não ganha e não entra.
        same = 2.0 * area ** 0.3 * np.exp(rng.normal(0, 0.1, n))
        self.assertEqual(train_por_uso(area, same, np.array(["x"] * n), min_n=100), {})

    def test_service_uses_the_curve_inside_its_range(self):
        tmp = Path(tempfile.mkdtemp())
        card = {"tipo": "potencia", "nome": "Potência", "formula": "geral", "parametros": {"a": 1.0, "b": 0.27},
                "usa_comodos": False, "faixa": {"area_m2": [25, 60000]}, "incerteza": {"sigma_log": 0.55},
                "metricas_cv": {"mae_meses": 5.6, "n_obras": 3000}, "base": "CNO · obras privadas", "aviso": "fim administrativo",
                "por_uso": {"residencial unifamiliar": {
                    "parametros": {"a": 0.2529, "b": 0.446}, "formula": "casas", "n_obras": 1772,
                    "faixa": {"area_m2": [40.0, 549.5]}, "incerteza": {"sigma_log": 0.5},
                    "metricas_cv": {"mae_meses": 5.15, "mae_curva_geral_meses": 5.4}}}}
        (tmp / "prazo_model_cno.json").write_text(json.dumps(card), encoding="utf-8")
        est = ScheduleEstimator(model_dir=tmp)
        casa = est.predict(150, 8, uso="residencial unifamiliar").to_dict()
        cno = casa["comparacoes"][0]
        self.assertEqual((cno["metodo"], cno["n_obras"], casa["uso_obra"]), ("potencia_por_uso", 1772, "residencial unifamiliar"))
        self.assertIn("casas", cno["base"])
        self.assertAlmostEqual(cno["prazo_meses"], round(np.exp(0.2529) * 150 ** 0.446, 1), places=1)
        # Fora da faixa das casas (2.000 m²): volta à curva geral.
        self.assertEqual(est.predict(2000, 8, uso="residencial unifamiliar").to_dict()["comparacoes"][0]["metodo"], "potencia")

    def test_private_unit_area_note(self):
        from types import SimpleNamespace
        from main import _area_obra
        out = _area_obra(SimpleNamespace(area_m2=251.57, area_kind="privativa", contorno=None))
        self.assertEqual(out["base"], "privativa")
        self.assertIn("UMA unidade", out["nota"])
