"""Leitura de um prédio em várias páginas: soma por pavimento e total do documento."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ai_service import AnalysisReport, FloorPlanAnalyzer  # noqa: E402
from services.plan_reader import TextItem, floor_multiplier  # noqa: E402


def report(area, kind, method="areas_declaradas", confidence="alta", mult=None):
    r = AnalysisReport(
        area_m2=area, area_kind=kind, qtd_comodos=0, total_detections=0, detection_summary={},
        ocr_texts=[], annotated_image_b64="", od_boxes=[], ocr_boxes=[], area_method=method,
        confidence=confidence, rooms=[], area_externa_m2=0.0, orientation_deg=0, text_source="ocr",
    )
    if mult:
        r.multiplicador = {"vezes": mult, "texto": f"PLANTA TIPO (x{mult})"}
    return r


combine = FloorPlanAnalyzer._combine_pages


class CombinePagesTest(unittest.TestCase):
    def test_complete_building_is_summed_with_repeated_floor(self):
        r = combine([(1, report(1000.0, "construida", "carimbo")),
                     (4, report(480.0, "construida", "carimbo", mult=12)),
                     (5, report(300.0, "construida", "carimbo"))], None)
        self.assertEqual((r.area_m2, r.area_method), (1000 + 12 * 480 + 300, "pavimentos"))
        self.assertTrue(r.predio["completo"])

    def test_rooms_of_every_page_with_repetition(self):
        terreo, tipo = report(200.0, "util"), report(150.0, "util", mult=12)
        terreo.rooms, terreo.qtd_comodos, terreo.rooms_sum_m2 = [{"name": "Hall"}], 1, 200.0
        tipo.rooms, tipo.qtd_comodos, tipo.rooms_sum_m2 = [{"name": "Sala"}, {"name": "Quarto"}], 2, 150.0
        tipo.area_externa_m2 = 10.0
        r = combine([(3, terreo), (4, tipo)], None)
        self.assertEqual([(x["name"], x["pagina"]) for x in r.rooms], [("Hall", 3), ("Sala", 4), ("Quarto", 4)])
        self.assertEqual(r.qtd_comodos, 1 + 2 * 12)
        self.assertEqual(r.rooms_sum_m2, 200.0 + 12 * 150.0)
        self.assertEqual(r.area_externa_m2, 120.0)        # sacadas do tipo, nos 12 pavimentos

    def test_missing_floor_gives_no_total(self):
        # TCC da UFSC: só a planta tipo é legível; o total não pode virar prazo e custo.
        r = combine([(1, report(None, None, "indisponivel", "baixa")),
                     (4, report(372.75, "util", mult=12))], None)
        self.assertIsNone(r.area_m2)
        self.assertEqual(r.predio["sem_area"], [1])
        self.assertEqual(r.predio["soma_m2"], round(12 * 372.75, 2))
        self.assertTrue(any("incompleto" in w for w in r.warnings))

    def test_mixed_kinds_are_reported_as_useful_area(self):
        r = combine([(1, report(100.0, "construida", "carimbo")), (2, report(80.0, "util"))], None)
        self.assertEqual(r.predio["tipo_area"], "util")

    def test_document_total_wins(self):
        base = report(9127.55, "construida", "documento")
        base.pagina_area = {"area_m2": 372.75, "area_kind": "util", "area_method": "areas_declaradas",
                            "confidence": "media"}
        r = combine([(4, base)], {"valor": 9127.55, "paginas": [39, 110]})
        self.assertEqual((r.area_m2, r.area_method), (9127.55, "documento"))
        self.assertEqual(r.predio["pavimentos"][0]["area_m2"], 372.75)


class ApplyDocumentTest(unittest.TestCase):
    def test_page_without_area_takes_document_total(self):
        # TCC completo: a página escolhida (17) é texto; o total está no memorial (39 e 110).
        r = report(None, None, "indisponivel", "baixa")
        r.warnings = ["Não encontramos área no carimbo... Informe a área manualmente para calcular prazo e custo."]
        r.page = {"numero": 17, "total": 246}
        doc = {"valor": 9127.55, "paginas": [39, 110], "texto": "Área Total de Construção – 9127,55 m²",
               "rotulo": "Área Total de Construção", "outros_valores": []}
        FloorPlanAnalyzer._apply_document(r, doc)
        self.assertEqual((r.area_m2, r.area_kind, r.area_method, r.confidence),
                         (9127.55, "construida", "documento", "alta"))
        self.assertFalse(any("Informe a área manualmente" in w for w in r.warnings))
        self.assertIn("página lida (17)", r.warnings[0])


class FloorMultiplierTest(unittest.TestCase):
    def test_titles(self):
        def mult(text):
            m = floor_multiplier([TextItem(text, 0, 0, 100, 10, 0, "ocr")])
            return m[0] if m else None
        self.assertEqual(mult("A TIPO (x12)"), 12)            # lido pelo OCR no TCC
        self.assertEqual(mult("PAVIMENTO TIPO 12X"), 12)
        self.assertIsNone(mult("VAGAS (240x500)"))
        self.assertIsNone(mult("PLANTA BAIXA"))


if __name__ == "__main__":
    unittest.main()
