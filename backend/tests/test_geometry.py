"""Contorno do prédio no desenho (services/geometry.py), com plantas sintéticas."""

import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ai_service import AnalysisReport, FloorPlanAnalyzer  # noqa: E402
from services.geometry import building_outline, outline_check  # noqa: E402
from services.plan_reader import Envelope, PlanReading, Room, TextItem  # noqa: E402

PX_M = 40.0          # 40 px por metro


def planta(walls_m=0.15):
    """Casa 10 × 8 m (80 m²) com parede interna, porta de 0,9 m e janela; folha 20 × 16 m."""
    img = np.full((int(16 * PX_M), int(20 * PX_M)), 255, np.uint8)
    t = int(walls_m * PX_M)
    x0, y0, x1, y1 = int(5 * PX_M), int(4 * PX_M), int(15 * PX_M), int(12 * PX_M)
    cv2.rectangle(img, (x0, y0), (x1, y1), 0, t)                             # paredes externas
    cv2.line(img, (int(10 * PX_M), y0), (int(10 * PX_M), y1), 0, t)        # parede interna
    door = int(0.9 * PX_M)                                                  # porta na fachada
    img[y1 - t:y1 + t, int(7 * PX_M):int(7 * PX_M) + door] = 255
    cv2.line(img, (int(12 * PX_M), y0), (int(13.5 * PX_M), y0), 255, t)    # vão de janela
    cv2.line(img, (int(12 * PX_M), y0), (int(13.5 * PX_M), y0), 0, 1)      # caixilho (linha fina)
    cv2.line(img, (0, int(2 * PX_M)), (img.shape[1], int(2 * PX_M)), 0, 1)  # linha de cota/eixo
    cv2.rectangle(img, (int(6 * PX_M), int(5 * PX_M)), (int(8 * PX_M), int(6 * PX_M)), 0, 1)  # móvel
    return img


class OutlineTest(unittest.TestCase):
    def test_house_area_close_to_real(self):
        out = building_outline(planta(), [], PX_M, inside=[(7 * PX_M, 8 * PX_M), (12 * PX_M, 8 * PX_M)])
        self.assertIsNotNone(out)
        # 10 × 8 m medidos pelos eixos das paredes; a erosão arredonda cantos: tolerância de 8%.
        self.assertAlmostEqual(out.area_m2, 80.0, delta=0.08 * 80)

    def test_no_room_inside_gives_nothing(self):
        self.assertIsNone(building_outline(planta(), [], PX_M, inside=[(1 * PX_M, 1 * PX_M)]))

    def test_outside_label_is_not_counted(self):
        # Um "JARDIM" fechado por muros grossos ao lado da casa não vira prédio.
        img = planta()
        t = int(0.15 * PX_M)
        cv2.rectangle(img, (int(15 * PX_M), int(4 * PX_M)), (int(19 * PX_M), int(12 * PX_M)), 0, t)
        out = building_outline(img, [], PX_M, inside=[(7 * PX_M, 8 * PX_M)], outside=[(17 * PX_M, 8 * PX_M)])
        self.assertAlmostEqual(out.area_m2, 80.0, delta=0.08 * 80)

    def test_plausibility(self):
        self.assertEqual(outline_check(90.0, 70.0, 100.0)[0], True)
        self.assertEqual(outline_check(60.0, 70.0, 100.0)[0], False)     # menor que a área útil
        self.assertEqual(outline_check(120.0, 70.0, 100.0)[0], False)    # maior que as cotas
        # Escola UFU com vão de 1,5 m: o contorno pegou o terreno (2,15× o real).
        self.assertEqual(outline_check(8707.0, 3127.7, None)[0], False)
        # Com ambientes sem área, a soma é só um piso: não serve de teto.
        self.assertEqual(outline_check(8707.0, 3127.7, None, useful_complete=False)[0], True)


def planta_linha_dupla():
    """Casa 10 × 8 m (80 m²) com paredes de DUAS linhas finas (sem hachura), como nas
    casas-padrão da PBH, e a projeção do telhado tracejada a 0,5 m das paredes."""
    img = np.full((int(16 * PX_M), int(20 * PX_M)), 255, np.uint8)
    t = int(0.15 * PX_M)
    x0, y0, x1, y1 = int(5 * PX_M), int(4 * PX_M), int(15 * PX_M), int(12 * PX_M)
    cv2.rectangle(img, (x0, y0), (x1, y1), 0, 1)                         # face externa
    cv2.rectangle(img, (x0 + t, y0 + t), (x1 - t, y1 - t), 0, 1)         # face interna
    xm = int(10 * PX_M)                                                  # parede interna dupla
    cv2.line(img, (xm, y0 + t), (xm, y1 - t), 0, 1)
    cv2.line(img, (xm + t, y0 + t), (xm + t, y1 - t), 0, 1)
    img[y1 - t - 1:y1 + 2, int(7 * PX_M):int(7.8 * PX_M)] = 255          # porta de 0,8 m
    d, step = int(0.5 * PX_M), int(0.3 * PX_M)                           # tracejado do beiral
    for x in range(x0 - d, x1 + d, 2 * step):
        cv2.line(img, (x, y0 - d), (min(x + step, x1 + d), y0 - d), 0, 1)
        cv2.line(img, (x, y1 + d), (min(x + step, x1 + d), y1 + d), 0, 1)
    return img, (x0, y0, x1, y1)


class DoubleLineWallsTest(unittest.TestCase):
    seeds = [(7 * PX_M, 8 * PX_M), (12 * PX_M, 8 * PX_M)]

    def test_thin_double_lines_are_missed_without_the_building_box(self):
        img, _ = planta_linha_dupla()
        out = building_outline(img, [], PX_M, self.seeds, door_m=0.9)
        self.assertTrue(out is None or out.area_m2 < 0.35 * 80)

    def test_building_box_recognises_the_walls(self):
        img, box = planta_linha_dupla()
        out = building_outline(img, [], PX_M, self.seeds, door_m=0.9, pair_box=box)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out.area_m2, 80.0, delta=0.05 * 80)

    def test_lower_bound_against_the_building_box(self):
        self.assertFalse(outline_check(11.3, None, None, building=42.9)[0])   # PBH antes: só a cozinha
        self.assertTrue(outline_check(43.6, None, None, building=42.9)[0])


def report(area, kind, method, confidence="alta"):
    return AnalysisReport(
        area_m2=area, area_kind=kind, qtd_comodos=0, total_detections=0, detection_summary={},
        ocr_texts=[], annotated_image_b64="", od_boxes=[], ocr_boxes=[], area_method=method,
        confidence=confidence, rooms=[], area_externa_m2=0.0, orientation_deg=0, text_source="pdf",
        warnings=["Área pela envoltória das cotas (20 m × 10 m, cotas em m). É um retângulo..."]
        if method == "cotas" else [],
    )


def contorno(area, valido=True, motivo="", razao=None):
    return {"area_m2": area, "valido": valido, "motivo": motivo, "blocos": 1, "razao_util": razao,
            "limite_cotas_m2": 200.0, "nota": "", "usado": False, "_mask": None}


class ReportOutlineTest(unittest.TestCase):
    apply = staticmethod(FloorPlanAnalyzer._apply_outline)

    def test_only_cotas_uses_the_outline(self):
        r = report(200.0, "envoltoria", "cotas", "baixa")
        self.apply(r, contorno(160.0))
        self.assertEqual((r.area_m2, r.area_kind, r.area_method, r.confidence),
                         (160.0, "construida", "contorno", "baixa"))
        self.assertTrue(r.contorno["usado"])
        self.assertNotIn("_mask", r.contorno)
        self.assertIn("limite superior", r.warnings[0])
        self.assertFalse(any(w.startswith("Área pela envoltória") for w in r.warnings))

    def test_written_area_stays_the_answer(self):
        r = report(1045.25, "util", "areas_declaradas", "media")
        r.qtd_comodos, r.rooms_with_area = 56, 52
        self.apply(r, contorno(1262.46, razao=1.21))
        self.assertEqual((r.area_m2, r.area_method), (1045.25, "areas_declaradas"))
        self.assertIn("1,21× a soma dos ambientes", r.checks[-1])

    def test_sum_of_few_rooms_gives_way_to_the_outline(self):
        # Creche Tipo 2 sem os textos de área: 1 de 30 ambientes com número (41 m²).
        r = report(41.03, "util", "areas_declaradas", "media")
        r.qtd_comodos, r.rooms_with_area = 30, 1
        r.warnings = ["Esta é a área útil (soma dos ambientes). A área construída é maior..."]
        self.apply(r, contorno(752.62, razao=18.34))
        self.assertEqual((r.area_m2, r.area_kind, r.area_method, r.confidence),
                         (752.62, "construida", "contorno", "baixa"))
        self.assertIn("Só 1 de 30 ambientes", r.warnings[0])
        self.assertFalse(any(w.startswith("Esta é a área útil") for w in r.warnings))

    def test_discarded_outline_only_warns(self):
        r = report(200.0, "envoltoria", "cotas", "baixa")
        self.apply(r, contorno(320.0, False, "maior que o retângulo das cotas"))
        self.assertEqual((r.area_m2, r.area_method), (200.0, "cotas"))
        self.assertIn("descartado", r.warnings[-1])


def reading(rooms_sum, complete, env_reliable, drawing=True):
    tag = TextItem("SALA", 0, 0, 10, 5, 0, "pdf")
    return PlanReading(
        area_m2=rooms_sum, area_kind="util", method="areas_declaradas", confidence="media", qtd_comodos=1,
        rooms=[], declared=None, rooms_sum_m2=rooms_sum, rooms_with_area=1, area_externa_m2=0.0,
        envelope=Envelope(20.0, 10.0, "m", 2, 10.0, env_reliable),
        drawing_rooms=[Room("Sala", rooms_sum, False, tag)] if drawing else [], rooms_complete=complete,
    )


class DoorLabelsTest(unittest.TestCase):
    def test_widest_door_from_labels(self):
        gap = FloorPlanAnalyzer._door_gap_m
        labels = [TextItem(t, 0, 0, 10, 5, 0, "pdf") for t in ("80X220", "70x220", "90X220", "160X120")]
        self.assertAlmostEqual(gap(labels), 0.99)                        # 90 cm (janela 160×120 não conta)
        self.assertAlmostEqual(gap([TextItem("SALA", 0, 0, 10, 5, 0, "pdf")]), 1.6)   # sem rótulo: 1,6 m


class OutlineNeedsALimitTest(unittest.TestCase):
    possible = staticmethod(FloorPlanAnalyzer._outline_possible)

    def test_limits(self):
        self.assertTrue(self.possible(reading(150.0, True, False)))      # soma completa: teto 1,6×
        self.assertTrue(self.possible(reading(150.0, False, True)))      # cotas confiáveis: teto
        self.assertFalse(self.possible(reading(150.0, False, False)))   # sem teto: não mede
        self.assertFalse(self.possible(reading(150.0, True, True, drawing=False)))  # nada no desenho


if __name__ == "__main__":
    unittest.main()
