"""OCR rápido (PP-OCRv6) num processo auxiliar: conversão das linhas e protocolo.

O processo de verdade (ocr_worker.py) precisa do ambiente .venv-ocr; aqui um
processo FALSO, com o mesmo protocolo, testa o cliente sem o PaddleOCR.
"""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from services.paddle_ocr import PaddleOcrWorker, estimate_tiles, raw_to_items  # noqa: E402

FAKE_WORKER = textwrap.dedent('''
    import json, sys
    print("aviso de biblioteca na saída padrão")          # tem de ser ignorado
    print("@@AGEPA " + json.dumps({"pronto": True, "modelo": "PP-OCRv6_falso"}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("sair"):
            break
        if "erro" in req["imagem"]:
            print("@@AGEPA " + json.dumps({"id": req["id"], "erro": "falha simulada"}), flush=True)
            continue
        print("@@AGEPA " + json.dumps({"id": req["id"], "blocos": 2}), flush=True)
        print("@@AGEPA " + json.dumps({"id": req["id"], "bloco": 1}), flush=True)
        print("@@AGEPA " + json.dumps({"id": req["id"], "bloco": 2}), flush=True)
        print("@@AGEPA " + json.dumps({"id": req["id"], "textos": [
            ["ÁREA A CONSTRUIR :", 0.98, 231, 2893, 245, 2984, False],
            ["402,43 m", 0.97, 213, 2912, 224, 2953, False]], "segundos": 0.1}), flush=True)
''')


class RawToItemsTest(unittest.TestCase):
    def test_angles_and_scale(self):
        raw = [["SALA", 0.9, 100, 100, 160, 112, False],          # deitado
               ["402,43 m", 0.97, 213, 2912, 224, 2953, False],    # de lado, de cima para baixo (Cantina)
               ["335", 0.95, 50, 400, 60, 440, True],              # de lado, de baixo para cima
               ["COZINHA", 0.9, 300, 300, 380, 312, True]]         # de cabeça para baixo
        items = raw_to_items(raw, scale=2.0)
        self.assertEqual([it.angle for it in items], [0, 90, 270, 180])
        self.assertEqual((items[0].x1, items[0].y1, items[0].x2, items[0].y2), (50.0, 50.0, 80.0, 56.0))
        self.assertTrue(all(it.source == "ocr" and not it.confirmed for it in items))

    def test_returning_stage_keeps_what_already_ran(self):
        # Releitura do quadro (3 faixas) e depois a conferência (5 rótulos): restam 5, não 2.
        from services.ai_service import ProgressTracker
        t = ProgressTracker({"releitura": 1.0}, {"releitura": 6})
        t.set_stage("releitura", 3)
        for _ in range(3):
            t.advance()
        t.set_stage("releitura", 5)
        self.assertEqual(t._plan["releitura"] - t._done["releitura"], 5)

    def test_tile_estimate(self):
        self.assertEqual(estimate_tiles(1000, 1000), 1)
        self.assertGreater(estimate_tiles(14044, 9934), 50)            # escola UFU (imagem de 28 MB)


class RereadValueTest(unittest.TestCase):
    def test_ambiguous_crop_does_not_vote(self):
        from services.ai_service import _single_value
        self.assertEqual(_single_value("A=12,06 M2"), 12.06)
        self.assertEqual(_single_value("AREA: 2.029,49 M2"), 2029.49)
        # O recorte pegou o vizinho do carimbo: dois números, nenhum voto.
        self.assertIsNone(_single_value("2.029,49 M2 8.117,96 M2"))


class ComplementTest(unittest.TestCase):
    """O Florence-2 só preenche o que o PP-OCRv6 não leu (TCC A5, planta tipo)."""

    def test_first_reading_wins_where_both_read(self):
        from services.ai_service import _complement
        from services.plan_reader import TextItem
        fast = [TextItem("ELEVADOR 1", 987.1, 1727.2, 1064.1, 1741.3, 0, "ocr"),
                TextItem("A=3.14 m²", 988.7, 1738.8, 1062.1, 1757.0, 0, "ocr")]
        florence = [TextItem("A-5.14 mm2", 990, 1741, 1059, 1755, 0, "ocr"),   # mesma escrita, mal lida
                    TextItem("A-3.14 m2", 990, 1740, 1059, 1741, 0, "ocr"),    # caixa achatada no mesmo lugar
                    TextItem("A=9,00 m²", 2000, 500, 2070, 515, 0, "ocr")]     # o que o PP-OCRv6 não leu
        texts = [it.text for it in _complement(fast, florence)]
        self.assertEqual(texts, ["ELEVADOR 1", "A=3.14 m²", "A=9,00 m²"])

    def test_area_the_first_reading_missed_replaces_the_bare_name(self):
        from services.ai_service import _complement
        from services.plan_reader import TextItem
        fast = [TextItem("COZINHA", 100, 100, 170, 112, 0, "ocr")]
        florence = [TextItem("COZINHA A=12,00 m²", 98, 98, 260, 113, 0, "ocr")]
        self.assertEqual([it.text for it in _complement(fast, florence)], ["COZINHA A=12,00 m²"])

    def test_piece_inside_a_line_read_whole_does_not_enter(self):
        # TCC: o Florence-2 leu o fim de "PD laje-2.675m" (pé-direito) como "A=2.67m".
        from services.ai_service import _complement
        from services.plan_reader import TextItem
        fast = [TextItem("PD laje-2.675m", 824.7, 1481.3, 928.3, 1496.2, 0, "ocr")]
        florence = [TextItem("A=2.67m", 870, 1483, 928, 1495, 0, "ocr")]
        self.assertEqual([it.text for it in _complement(fast, florence)], ["PD laje-2.675m"])

    def test_area_without_unit_counts_as_read(self):
        # TCC, varanda: o PP-OCRv6 leu "A=4.04" (sem o m²); o "A=4.04m*" do Florence-2 é o mesmo rótulo.
        from services.ai_service import _complement
        from services.plan_reader import TextItem
        fast = [TextItem("A=4.04", 1786.8, 934.7, 1831.6, 950.4, 0, "ocr")]
        florence = [TextItem("A=4.04m*", 1788, 937, 1850, 946, 0, "ocr")]
        self.assertEqual([it.text for it in _complement(fast, florence)], ["A=4.04"])

    def test_flat_box_is_dropped(self):
        from services.ai_service import _flat_quad
        self.assertTrue(_flat_quad([990, 0, 1059, 0, 1059, 1, 990, 1], "A-3.14 m2"))
        self.assertTrue(_flat_quad([4805, 3993, 4805, 3993, 4805, 3993, 4805, 3993], "A =124,141 m2"))
        self.assertFalse(_flat_quad([990, 0, 1059, 0, 1059, 14, 990, 14], "A-3.14 m2"))
        self.assertFalse(_flat_quad([10, 10, 12, 10, 12, 15, 10, 15], "1"))     # um algarismo estreito


class RecoverLabelsTest(unittest.TestCase):
    """Rótulo de área mal lido ("A55m") é relido ampliado; entra só com 2 votos iguais."""

    def _analyzer(self, zoom_reads):
        import threading
        from services.ai_service import FloorPlanAnalyzer
        an = object.__new__(FloorPlanAnalyzer)
        an._lock, an._tracker = threading.Lock(), None
        an._fast_ocr = PaddleOcrWorker(python=Path("nao_existe.exe"))
        reads = iter(zoom_reads)
        an._ocr_zoom = lambda *a, **k: next(reads)
        return an

    def test_which_texts_are_almost_labels(self):
        from services.ai_service import _almost_area
        from services.plan_reader import TextItem
        def item(t, source="ocr"):
            return TextItem(t, 0, 0, 60, 14, 0, source)
        for t in ("A55m", "A-O.0.2 m2", "-0,06 m2", "A-1.33 m"):
            self.assertTrue(_almost_area(item(t)), t)
        for t in ("A=14,54 m²", "A=6.47 mº", "PD gosoo-3,50 m2", "2,0 x 2,50m", "ge880=2.50m", "2.50m", "SALA"):
            self.assertFalse(_almost_area(item(t)), t)
        self.assertFalse(_almost_area(item("A55m", source="pdf")))      # texto exato do PDF: é o que está escrito

    def test_reread_values(self):
        from services.ai_service import _label_value
        self.assertEqual(_label_value("A1.38 M"), 1.38)
        self.assertEqual(_label_value("A=14.75M3"), 14.75)
        self.assertEqual(_label_value("0.86M2"), 0.86)
        self.assertIsNone(_label_value("PD GESSO-2.50M"))               # pé-direito não vota
        self.assertIsNone(_label_value("A=3,00 M2 A=7,50 M2"))          # dois números: ambíguo

    def test_two_equal_reads_recover_the_label(self):
        from services.plan_reader import TextItem, interpret
        bad = TextItem("A55m", 1427, 1460, 1495, 1474, 0, "ocr")
        good = TextItem("A=14.75m²", 1420, 1458, 1500, 1475, 0, "ocr")
        an = self._analyzer([[good], [good]])
        items = [bad]
        self.assertEqual(an._recover_labels(None, 1.0, items, interpret(items, None)), 1)
        self.assertEqual(bad.text, "A=14,75 m²")
        self.assertTrue(bad.confirmed)
        self.assertAlmostEqual(interpret(items, None).area_m2, 14.75)

    def test_upside_down_misread_is_read_upright(self):
        # TCC: o classificador do PP-OCRv6 virou a linha (180°) e leu "A55m"; em pé, "A=14.75m²".
        from services.plan_reader import TextItem, interpret
        bad = TextItem("A55m", 1427, 1460, 1495, 1474, 180, "ocr")
        upright = TextItem("A=14.75m²", 1420, 1458, 1500, 1475, 0, "ocr")
        garbage = TextItem("WSLVI=V", 1420, 1458, 1500, 1475, 180, "ocr")
        an = self._analyzer([[upright], [upright], [garbage]])
        self.assertEqual(an._recover_labels(None, 1.0, [bad], interpret([bad], None)), 1)
        self.assertEqual((bad.text, bad.angle), ("A=14,75 m²", 0))

    def test_neighbour_or_disagreement_does_not_enter(self):
        from services.plan_reader import TextItem, interpret
        bad = TextItem("A-1.33 m", 960, 2250, 1026, 2263, 0, "ocr")
        above = TextItem("A=3,00 m²", 958, 2228, 1030, 2243, 0, "ocr")     # rótulo vizinho, fora da caixa
        an = self._analyzer([[above], [above]])
        self.assertEqual(an._recover_labels(None, 1.0, [bad], interpret([bad], None)), 0)
        self.assertEqual(bad.text, "A-1.33 m")
        a = TextItem("A1.33 M", 960, 2250, 1026, 2263, 0, "ocr")
        b = TextItem("A1.38 M", 960, 2250, 1026, 2263, 0, "ocr")
        an = self._analyzer([[a], [b]])                                    # 1,33 × 1,38: sem consenso
        self.assertEqual(an._recover_labels(None, 1.0, [bad], interpret([bad], None)), 0)


class WorkerProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "falso.py"
        self.script.write_text(FAKE_WORKER, encoding="utf-8")
        self.worker = PaddleOcrWorker(python=Path(sys.executable), script=self.script,
                                      log_path=Path(self.tmp.name) / "log.txt")

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def test_reads_with_progress(self):
        self.assertTrue(self.worker.available())
        self.assertEqual(self.worker.model, "PP-OCRv6_falso")
        totals, ticks = [], []
        raw = self.worker.read(Image.new("RGB", (64, 64), "white"), on_total=totals.append,
                               on_tile=lambda: ticks.append(1))
        self.assertEqual(totals, [2])
        self.assertEqual(len(ticks), 2)
        self.assertEqual([r[0] for r in raw], ["ÁREA A CONSTRUIR :", "402,43 m"])

    def test_missing_environment_falls_back(self):
        worker = PaddleOcrWorker(python=Path(self.tmp.name) / "nao_existe.exe", script=self.script)
        self.assertFalse(worker.available())
        self.assertIsNone(worker.read(Image.new("RGB", (8, 8))))

    def test_cancel_stops_the_worker(self):
        class Cancelled(Exception):
            pass

        def cancel(_n):
            raise Cancelled()

        self.assertTrue(self.worker.available())
        with self.assertRaises(Cancelled):
            self.worker.read(Image.new("RGB", (8, 8)), on_total=cancel)
        self.assertFalse(self.worker.ready)                         # reiniciado na próxima leitura
        self.assertIsNotNone(self.worker.read(Image.new("RGB", (8, 8))))


if __name__ == "__main__":
    unittest.main()
