# ============================================================
# AGEPA — Testes do interpretador de plantas (sem IA)
# tests/test_plan_reader.py
#
# Rodar (na pasta backend):
#   py -m unittest discover -s tests -v
# ============================================================

import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.pdf_reader import _join_glyph_runs  # noqa: E402
from services.plan_reader import (  # noqa: E402
    TextItem, enclosed_by_ellipse, find_area_table, find_declared_area, find_envelope,
    find_rooms, interpret, merge_area_units, parse_number, title_pt,
)


def pdf(text, x1, y1, x2, y2, angle=0):
    return TextItem(text, x1, y1, x2, y2, angle=angle, source="pdf")


def ocr(text, x1, y1, x2, y2, angle=0):
    return TextItem(text, x1, y1, x2, y2, angle=angle, source="ocr")


class ParseNumberTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_number("402,43"), 402.43)
        self.assertEqual(parse_number("1.234,56"), 1234.56)
        self.assertEqual(parse_number("12.50"), 12.5)
        self.assertEqual(parse_number("1.234"), 1234.0)
        self.assertEqual(parse_number("503"), 503.0)
        self.assertIsNone(parse_number("abc"))

    def test_title_pt(self):
        self.assertEqual(title_pt("DEPÓSITO DE LIXO"), "Depósito de Lixo")
        self.assertEqual(title_pt("Sala de estar"), "Sala de estar")


class DeclaredAreaTest(unittest.TestCase):
    def test_value_in_cell_below_label(self):
        key = pdf("ÁREA A CONSTRUIR :", 100, 100, 200, 110)
        value = pdf("402,43 m²", 120, 116, 170, 126)
        other = pdf("SETEMBRO / 2010", 260, 116, 330, 126)
        declared, _ = find_declared_area([key, value, other])
        self.assertIsNotNone(declared)
        self.assertEqual(declared.value, 402.43)

    def test_value_in_same_text(self):
        declared, _ = find_declared_area([pdf("ÁREA CONSTRUÍDA: 85,00 m²", 0, 0, 150, 10)])
        self.assertEqual(declared.value, 85.0)

    def test_rotated_title_block(self):
        # Texto lido de cima para baixo (90°): "abaixo" do rótulo fica à esquerda.
        key = pdf("ÁREA A CONSTRUIR :", 56, 126, 58, 147, angle=90)
        value = pdf("402,43 m²", 51, 133, 53, 142, angle=90)
        declared, _ = find_declared_area([key, value])
        self.assertEqual(declared.value, 402.43)

    def test_ocr_typo_in_label(self):
        key = ocr("ARE A CONSTRUR:", 1649, 1274, 1702, 1281)
        declared, pending = find_declared_area([key])
        self.assertIsNone(declared)
        self.assertIs(pending, key)          # rótulo achado: vale reler o valor ampliado
        value = ocr("402,43 m²", 1660, 1285, 1690, 1292)
        declared, _ = find_declared_area([key, value])
        self.assertEqual(declared.value, 402.43)

    def test_zoomed_cell_with_misread_unit(self):
        # Leitura real da célula ampliada: "m²" saiu como "mf"; o ano ao lado não conta.
        key = ocr("ARE A A CONSTRUIR :", 1649, 1274, 1702, 1281)
        value = ocr("402,43 mf", 1656, 1285, 1690, 1292)
        year = ocr("SETEMBRO / 2010", 1729, 1286, 1777, 1293)
        declared, _ = find_declared_area([key, value, year])
        self.assertEqual(declared.value, 402.43)

    def test_fuzzy_picks_most_similar_key(self):
        declared, _ = find_declared_area([
            ocr("ARE A A CONSTRUIR :", 1649, 1274, 1702, 1281), ocr("402,43 mf", 1656, 1285, 1690, 1292),
        ])
        self.assertEqual(declared.label, "ÁREA A CONSTRUIR")

    def test_pdf_text_is_never_fuzzy(self):
        declared, pending = find_declared_area([pdf("ARE A CONSTRUR:", 0, 0, 50, 7)])
        self.assertIsNone(declared)
        self.assertIsNone(pending)

    def test_land_area_is_ignored(self):
        declared, _ = find_declared_area([pdf("ÁREA DO TERRENO: 450,00 m²", 0, 0, 120, 10)])
        self.assertIsNone(declared)

    def test_total_wins_over_floor(self):
        items = [
            pdf("ÁREA DO PAVIMENTO: 80,00 m²", 0, 0, 150, 10),
            pdf("ÁREA TOTAL CONSTRUÍDA: 150,00 m²", 0, 20, 170, 30),
        ]
        declared, _ = find_declared_area(items)
        self.assertEqual(declared.value, 150.0)


class EllipseTest(unittest.TestCase):
    def setUp(self):
        self.gray = np.full((300, 300), 255, np.uint8)

    def test_circled_number(self):
        cv2.ellipse(self.gray, (150, 150), (40, 20), 0, 0, 360, 0, 2)
        self.assertTrue(enclosed_by_ellipse(self.gray, pdf("178,71", 125, 143, 175, 157)))

    def test_rectangle_room_is_not_ellipse(self):
        cv2.rectangle(self.gray, (110, 130), (190, 170), 0, 2)
        self.assertFalse(enclosed_by_ellipse(self.gray, pdf("178,71", 125, 143, 175, 157)))

    def test_no_shape(self):
        self.assertFalse(enclosed_by_ellipse(self.gray, pdf("178,71", 125, 143, 175, 157)))


class RoomsTest(unittest.TestCase):
    def test_explicit_label_gets_name_above(self):
        items = [pdf("QUARTO", 100, 100, 140, 110), pdf("ÁREA=12,50m²", 95, 114, 150, 124)]
        rooms = find_rooms(items, None)
        self.assertEqual([(r.name, r.area_m2) for r in rooms], [("Quarto", 12.5)])

    def test_loose_m2_without_name_is_ignored(self):
        # Tabela de esquadrias: "1,44 m²" solto não é ambiente.
        rooms = find_rooms([pdf("1,44 m²", 500, 500, 530, 510)], None)
        self.assertEqual(rooms, [])

    def test_name_fragments_are_merged(self):
        items = [pdf("BANHEIRO", 100, 100, 150, 110), pdf("MASCULINO", 98, 113, 152, 123)]
        self.assertEqual(find_rooms(items, None)[0].name, "Banheiro Masculino")

    def test_furniture_is_not_a_room(self):
        self.assertEqual(find_rooms([pdf("BANCADA", 0, 0, 40, 10), pdf("MESA DE APOIO", 0, 30, 70, 40)], None), [])


class EnvelopeTest(unittest.TestCase):
    def _chain(self, values, start, k, fixed, vertical):
        items, pos = [], start
        for v in values:
            center = pos + v * k / 2
            if vertical:
                items.append(pdf(str(v), fixed, center - 4, fixed + 3, center + 4, angle=90))
            else:
                items.append(pdf(str(v), center - 4, fixed, center + 4, fixed + 3))
            pos += v * k
        return items

    def test_chains_in_cm(self):
        k = 0.5   # px por cm
        items = (
            self._chain([15, 503, 15, 1248, 15], 100, k, 50, False)   # 17,96 m
            + self._chain([15, 410, 15, 900], 100, k, 1200, False)
            + self._chain([15, 300, 212, 303, 585, 15], 100, k, 1000, True)  # 14,30 m
            + self._chain([15, 524, 240, 15], 100, k, 40, True)
        )
        env, used = find_envelope(items)
        self.assertIsNotNone(env)
        self.assertEqual(env.unit, "cm")
        self.assertAlmostEqual(env.largura_m, 17.96, delta=0.05)
        self.assertAlmostEqual(env.profundidade_m, 14.30, delta=0.05)

    def test_random_numbers_are_not_chains(self):
        items = [pdf("165", 10, 10, 18, 13), pdf("145", 300, 10, 308, 13), pdf("410", 320, 10, 328, 13)]
        env, _ = find_envelope(items)
        self.assertIsNone(env)


class AreaTableTest(unittest.TestCase):
    """Quadro de áreas ("Ambiente | Área"), como na planta do Pronto Socorro."""

    def _table(self):
        rows = [("Adm. / Coordenação de Enfermagem", "15,41"), ("Almoxarifado / Farmácia", "14,335"),
                ("Ante-câmara Procedimentos", "3,67"), ("Sala de Sutura / Curativo", "10,53"),
                ("San. Fem.", "2,37"), ("Raio X", "14,40"), ("Recepção", "22,50")]
        items = [pdf("Quadro de Áreas - Térreo", 900, 80, 1100, 92), pdf("Ambiente", 900, 100, 960, 110),
                 pdf("Área", 1180, 100, 1210, 110)]
        for i, (name, value) in enumerate(rows):
            y = 120 + 20 * i
            items.append(pdf(name, 900, y, 900 + 7 * len(name), y + 10))
            items.append(pdf(value, 1240 - 7 * len(value), y, 1240, y + 10))   # valores alinhados à direita
        return items, rows

    def test_table_rooms(self):
        items, rows = self._table()
        rooms = find_area_table(items)
        self.assertEqual(len(rooms), len(rows))
        self.assertEqual({r.area_source for r in rooms}, {"quadro"})
        self.assertAlmostEqual(sum(r.area_m2 for r in rooms), 15.41 + 14.335 + 3.67 + 10.53 + 2.37 + 14.40 + 22.50)

    def test_table_and_plan_labels_are_not_counted_twice(self):
        items, rows = self._table()
        # Os mesmos ambientes rotulados na planta, longe do quadro.
        items += [pdf("Recepção", 100, 600, 160, 610), pdf("A: 22,50 m²", 100, 614, 170, 624),
                  pdf("Raio X", 300, 600, 340, 610), pdf("A: 14,40 m²", 300, 614, 370, 624)]
        r = interpret(items, None)
        self.assertEqual(r.rooms_with_area, len(rows))
        self.assertEqual(r.qtd_comodos, len(rows))
        self.assertAlmostEqual(r.area_m2, 83.215, places=2)
        # O contorno parte dos ambientes NO DESENHO, não das linhas do quadro (PARQ).
        self.assertEqual(sorted(x.name for x in r.drawing_rooms), ["Raio X", "Recepção"])
        self.assertTrue(all(x.item.cy > 500 for x in r.drawing_rooms))
        self.assertTrue(r.rooms_complete)

    def test_row_read_as_one_text_is_split(self):
        # O OCR às vezes junta as duas colunas: "Raio X 14,405".
        items, rows = self._table()
        items.append(pdf("Raio X 14,405", 900, 120 + 20 * len(rows), 1240, 130 + 20 * len(rows)))
        rooms = find_area_table(items)
        self.assertIn(14.405, [r.area_m2 for r in rooms])
        self.assertIn("Raio X", [r.name for r in rooms])

    def test_floor_table_confirms_title_block(self):
        # Quadro resumo por pavimento (UNIRIO): não é lista de cômodos; a soma confere o total.
        floors = [("1º PAVIMENTO", "1.606,41"), ("2º PAVIMENTO", "1.251,43"), ("3º PAVIMENTO", "1.251,43"),
                  ("4º PAVIMENTO", "1.251,43"), ("5º PAVIMENTO", "1.232,90"), ("PAV. TÉCNICO", "368,81")]
        items = [pdf("ÁREA CONSTRUÍDA TOTAL : 6.962,41m²", 400, 120, 600, 130)]
        for i, (name, value) in enumerate(floors):
            y = 100 + 20 * i
            items += [pdf(name, 200, y, 290, y + 10), pdf(value, 310, y, 360, y + 10)]
        self.assertEqual(find_area_table(items), [])           # pavimentos ≠ ambientes
        r = interpret(items, None)
        self.assertAlmostEqual(r.floors_sum, 6962.41, places=2)
        self.assertEqual((r.method, r.area_m2, r.confidence), ("carimbo", 6962.41, "alta"))
        self.assertTrue(any("por pavimento" in c for c in r.checks))

    def test_fixture_list_is_not_rooms(self):
        items = [pdf("Vaso Sanitário", 0, 0, 80, 10), pdf("Lavatório 100x30", 0, 20, 90, 30)]
        self.assertEqual(find_rooms(items, None), [])


class InterpretTest(unittest.TestCase):
    def test_title_block_with_consistent_rooms(self):
        items = [
            pdf("ÁREA A CONSTRUIR :", 900, 900, 1000, 910), pdf("120,00 m²", 920, 916, 970, 926),
            pdf("SALA", 100, 100, 130, 110), pdf("ÁREA=40,00m²", 95, 114, 150, 124),
            pdf("QUARTO", 300, 100, 340, 110), pdf("ÁREA=30,00m²", 295, 114, 350, 124),
        ]
        r = interpret(items, None)
        self.assertEqual((r.method, r.area_m2, r.area_kind, r.confidence), ("carimbo", 120.0, "construida", "alta"))
        self.assertEqual(r.rooms_sum_m2, 70.0)

    def test_rooms_bigger_than_title_block_lower_confidence(self):
        items = [
            pdf("ÁREA A CONSTRUIR :", 900, 900, 1000, 910), pdf("50,00 m²", 920, 916, 970, 926),
            pdf("SALA", 100, 100, 130, 110), pdf("ÁREA=40,00m²", 95, 114, 150, 124),
            pdf("QUARTO", 300, 100, 340, 110), pdf("ÁREA=30,00m²", 295, 114, 350, 124),
        ]
        r = interpret(items, None)
        self.assertEqual(r.confidence, "media")
        self.assertTrue(any("passa da área do carimbo" in w for w in r.warnings))

    def test_partial_ocr_envelope_does_not_accuse_title_block(self):
        # Cadeias de cotas lidas por OCR cobrem só parte da obra (envoltória pequena):
        # não podem derrubar a confiança de um carimbo conferido.
        k, items = 0.5, []
        for fixed, vertical in ((50, False), (1200, False), (1000, True), (40, True)):
            pos = 100
            for v in (15, 300, 15, 400, 15):
                c = pos + v * k / 2
                box = (fixed, c - 4, fixed + 3, c + 4) if vertical else (c - 4, fixed, c + 4, fixed + 3)
                items.append(ocr(str(v), *box, angle=90 if vertical else 0))
                pos += v * k
        key, value = ocr("ÁREA A CONSTRUIR :", 900, 900, 1000, 910), ocr("402,43 m²", 920, 916, 970, 926)
        value.confirmed = True
        r = interpret(items + [key, value], None)
        self.assertIsNotNone(r.envelope)
        self.assertFalse(r.envelope.reliable)
        self.assertLess(r.envelope.area_m2, 402.43)
        self.assertEqual((r.method, r.confidence), ("carimbo", "alta"))

    def test_rooms_only_is_useful_area(self):
        items = [pdf("SALA", 100, 100, 130, 110), pdf("ÁREA=40,00m²", 95, 114, 150, 124)]
        r = interpret(items, None)
        self.assertEqual((r.method, r.area_kind, r.area_m2), ("areas_declaradas", "util", 40.0))

    def test_nothing_readable_gives_no_area(self):
        r = interpret([pdf("PLANTA BAIXA", 0, 0, 80, 10)], None)
        self.assertIsNone(r.area_m2)
        self.assertEqual(r.method, "indisponivel")


class FragmentedLabelTest(unittest.TestCase):
    """PDFs do CAD gravam o rótulo em pedaços (casos reais: creches FNDE, UBS do Novo PAC)."""

    def test_label_in_six_pieces_out_of_order(self):
        # "A: 9,90 m²" da UBS 1: pedaços fora de ordem, "²" como texto sem direção.
        items = [pdf("2", 778, 670, 779, 672, angle=None), pdf("A:", 760, 670, 763, 673),
                 pdf("90", 768, 670, 773, 673), pdf("m", 774, 671, 777, 673, angle=None),
                 pdf("9", 765, 670, 767, 673), pdf(",", 767, 673, 768, 674)]
        merged = merge_area_units(items)
        self.assertEqual([it.text for it in merged], ["A: 9,90 m²"])

    def test_value_and_unit_apart_with_name_above(self):
        # Creche FNDE: "DESPENSA" / "8,81" + "m2" (unidade sem direção).
        items = [pdf("DESPENSA", 1582, 1062, 1612, 1066), pdf("8,81", 1587, 1068, 1597, 1073),
                 pdf("m2", 1600, 1068, 1606, 1072, angle=None)]
        rooms = find_rooms(merge_area_units(items), None)
        self.assertEqual([(r.name, r.area_m2) for r in rooms], [("Despensa", 8.81)])

    def test_digit_two_before_unit_is_part_of_number(self):
        items = [pdf("1", 100, 100, 103, 106), pdf("2", 103, 100, 106, 106), pdf(",50", 106, 100, 114, 106),
                 pdf("m", 115, 101, 119, 106), pdf("2", 119, 100, 121, 103, angle=None)]
        self.assertEqual([it.text for it in merge_area_units(items)], ["12,50 m²"])

    def test_number_without_unit_is_untouched(self):
        items = [pdf("1,40", 100, 100, 110, 105), pdf("m2", 300, 100, 306, 105, angle=None)]
        self.assertEqual({it.text for it in merge_area_units(items)}, {"1,40", "m2"})

    def test_cad_tag_name_above_value(self):
        # "INFANTIL 1" não está na lista de palavras de ambiente: vale a etiqueta (nome logo acima).
        items = [pdf("INFANTIL 1", 100, 100, 140, 106), pdf("35,63 m²", 105, 108, 135, 114)]
        self.assertEqual([(r.name, r.area_m2) for r in find_rooms(items, None)], [("Infantil 1", 35.63)])

    def test_measure_above_value_is_not_a_name(self):
        # Quadro de áreas da creche: "45 metros x 65" acima de "1.545,99 m²" (área ocupada).
        items = [pdf("45 metros x 65", 100, 100, 160, 106), pdf("1.545,99 m²", 110, 108, 150, 114)]
        self.assertEqual(find_rooms(items, None), [])

    def test_unit_total_is_not_summed_with_its_rooms(self):
        # Planta tipo do TCC (UFSC): "UNIDADE 02 / A: 81.41 m²" é a soma dos ambientes da unidade.
        items = [pdf("UNIDADE", 500, 100, 540, 106), pdf("02", 500, 108, 510, 114), pdf("A: 81.41 m²", 500, 116, 545, 122),
                 pdf("DORMITÓRIO 1", 100, 100, 160, 106), pdf("A=11.51m²", 105, 108, 150, 114),
                 pdf("ÁREA SOCIAL", 300, 100, 355, 106), pdf("A=17.43 m²", 305, 108, 350, 114)]
        r = interpret(items, None)
        self.assertAlmostEqual(r.area_m2, 28.94, places=2)
        self.assertTrue(any("81,41" in c for c in r.checks))

    def test_room_label_below_the_unit_box_is_the_room(self):
        # TCC, planta tipo (caixas do PP-OCRv6): "UNIDADE / 02 / 81.41" e, logo abaixo,
        # "HALL / 7.50 m²". O 7,50 é do hall; antes o hall ficava sem área.
        items = [ocr("UNIDADE", 1298.7, 1110.5, 1353.9, 1125.8), ocr("02", 1299.1, 1123.8, 1318.0, 1139.5),
                 ocr("81.41", 1320.1, 1139.5, 1353.5, 1152.8),
                 ocr("HALL", 1315.6, 1158.4, 1350.7, 1177.0), ocr("7.50 m²", 1310.8, 1171.3, 1372.1, 1191.9)]
        self.assertEqual([(r.name, r.area_m2) for r in find_rooms(items, None)], [("Hall", 7.5)])

    def test_area_without_unit_needs_a_room_name(self):
        # Hall da unidade 01: a linha da porta corta o "m²" e o OCR lê só "A=7.50".
        named = [ocr("HALL", 995.6, 1160.0, 1030.2, 1176.6), ocr("A=7.50", 978.7, 1174.1, 1048.0, 1189.5)]
        self.assertEqual([(r.name, r.area_m2) for r in find_rooms(named, None)], [("Hall", 7.5)])
        # Sem nome por perto, "A=1.10" pode ser outra coisa: não vira ambiente.
        self.assertEqual(find_rooms([ocr("A=1.10", 978.7, 1174.1, 1048.0, 1189.5)], None), [])
        # "A=2.67m" (o fim de "PD laje=2.675m") logo abaixo da área do ambiente: sem nome, não entra.
        block = [ocr("ÁREA SOCIAL", 835, 1454, 918, 1468), ocr("A=14,54 m²", 837, 1468, 916, 1482),
                 ocr("A=2.67m", 870, 1483, 928, 1495)]
        self.assertEqual([r.area_m2 for r in find_rooms(block, None)], [14.54])
        # No texto exato do PDF, "A=2,67m" é o que está escrito: continua valendo.
        self.assertEqual([r.area_m2 for r in find_rooms([pdf("A=2,67m", 870, 1483, 928, 1495)], None)], [2.67])
        # Varanda: a parede corta o "VARANDA" e o OCR lê "VARAN" — a etiqueta logo acima vale.
        tag = [ocr("VARAN", 1789, 921, 1832, 934), ocr("A=4.04", 1787, 935, 1832, 950)]
        self.assertEqual([(r.name, r.area_m2) for r in find_rooms(tag, None)], [("Varan", 4.04)])

    def test_ocr_integer_on_decimal_sheet_is_not_summed(self):
        # TCC: "A=3.00 m²" lido pelo OCR como "A=300 m1" — não pode virar um banheiro de 300 m².
        rows = [("SALA", "A=17.42 m²"), ("QUARTO", "A=11.52 m²"), ("COZINHA", "A=7.50 m²"),
                ("VARANDA", "A=4.04 m²"), ("CIRCULAÇÃO", "A=3.69 m²"), ("BANHO", "A=300 m1")]
        items = []
        for i, (name, lab) in enumerate(rows):
            x = 100 + 120 * i
            items += [ocr(name, x, 100, x + 50, 106), ocr(lab, x, 108, x + 45, 114)]
        r = interpret(items, None)
        self.assertAlmostEqual(r.area_m2, 44.17, places=2)
        self.assertNotEqual(r.confidence, "alta")
        self.assertTrue(any("A=300 m1" in w for w in r.warnings))

    def test_pdf_integer_label_is_kept(self):
        # Escola UFU (texto exato do PDF): "159 m²" é o que está escrito.
        rows = [("SALA 1", "75,50 m²"), ("SALA 2", "61,96 m²"), ("SALA 3", "99,48 m²"),
                ("SALA 4", "113,13 m²"), ("SALA 5", "39,46 m²"), ("PÁTIO", "159 m²"), ("COZINHA", "22 m²")]
        items = []
        for i, (name, lab) in enumerate(rows):
            x = 100 + 120 * i
            items += [pdf(name, x, 100, x + 50, 106), pdf(lab, x, 108, x + 45, 114)]
        r = interpret(items, None)
        self.assertAlmostEqual(r.area_m2, 75.50 + 61.96 + 99.48 + 113.13 + 39.46 + 22.0, places=2)   # pátio = externa

    def test_room_label_beside_unit_box_is_still_a_room(self):
        # Unidade 03: o quadrinho "UNIDADE 03" fica AO LADO de "ÁREA SOCIAL A=14.54 m²".
        items = [pdf("ÁREA SOCIAL", 100, 100, 155, 106), pdf("A=14.54 m²", 105, 108, 150, 114),
                 pdf("UNIDADE", 165, 100, 205, 106), pdf("03", 165, 108, 175, 114), pdf("A: 65.92 m²", 165, 116, 210, 122)]
        r = interpret(items, None)
        self.assertEqual(r.area_m2, 14.54)

    def test_open_areas_are_external(self):
        items = [pdf("SALA", 100, 100, 130, 106), pdf("40,00 m²", 100, 108, 130, 114),
                 pdf("PÁTIO COBERTO", 300, 100, 360, 106), pdf("190,52 m²", 305, 108, 345, 114),
                 pdf("PLAYGROUND", 500, 100, 550, 106), pdf("75,69 m²", 505, 108, 540, 114)]
        r = interpret(items, None)
        self.assertEqual(r.area_m2, 40.0)
        self.assertAlmostEqual(r.area_externa_m2, 266.21, places=2)


class GlyphRunsTest(unittest.TestCase):
    """Texto gravado letra por letra no PDF (UBS 1 do Novo PAC)."""

    @staticmethod
    def glyph(text, x0, w=3.0, y=100.0, h=5.0):
        pts = [(x0, y), (x0 + w, y), (x0, y + h), (x0 + w, y + h)]
        return {"text": text, "angle": 0.0, "pts": pts, "origins": [(x0, y)]}

    def test_letters_are_joined(self):
        runs = [self.glyph("C", 0), self.glyph("o", 3.2), self.glyph("n", 6.4)]
        self.assertEqual([r["text"] for r in _join_glyph_runs(runs)], ["Con"])

    def test_digits_never_get_a_space(self):
        runs = [self.glyph("1", 0), self.glyph("5", 5.0)]        # folga de 0,4 letra
        self.assertEqual([r["text"] for r in _join_glyph_runs(runs)], ["15"])

    def test_far_or_long_texts_stay_apart(self):
        far = [self.glyph("A", 0), self.glyph("B", 20)]
        self.assertEqual(len(_join_glyph_runs(far)), 2)
        words = [self.glyph("SALA", 0, w=12), self.glyph("COZINHA", 12.5, w=20)]
        self.assertEqual(len(_join_glyph_runs(words)), 2)
        other_line = [self.glyph("A", 0), self.glyph("B", 3.2, y=110)]
        self.assertEqual(len(_join_glyph_runs(other_line)), 2)


class TableGuardTest(unittest.TestCase):
    def test_window_schedule_is_not_an_area_table(self):
        # Mozarlândia: quadro de esquadrias tomado por quadro de áreas (10,46 m² com confiança alta).
        rows = [("Com Vidro e Veneziana", "1,60"), ("Com Veneziana", "2,56"), ("Com Vidro", "1,05"),
                ("Com Veneziana", "3,15"), ("Com Veneziana", "2,10")]
        items = []
        for i, (name, value) in enumerate(rows):
            y = 100 + 16 * i
            items += [pdf(name, 600, y, 600 + 6 * len(name), y + 8), pdf(value, 800, y, 820, y + 8)]
        self.assertEqual(find_area_table(items), [])

    def test_header_accepts_unknown_room_names(self):
        rows = [("Raio X", "14,40"), ("Tomografia", "30,10"), ("Expurgo", "4,20"),
                ("Triagem", "9,80"), ("Observação", "22,00")]
        items = [pdf("Ambiente", 600, 80, 650, 88)]
        for i, (name, value) in enumerate(rows):
            y = 100 + 16 * i
            items += [pdf(name, 600, y, 600 + 6 * len(name), y + 8), pdf(value, 800, y, 820, y + 8)]
        self.assertEqual(len(find_area_table(items)), 5)

    def test_table_that_contradicts_plan_labels_is_dropped(self):
        rows = [("Sala A", "1,60"), ("Sala B", "2,56"), ("Sala C", "1,05"), ("Sala D", "3,15"), ("Sala E", "2,10")]
        items = [pdf("Ambiente", 600, 80, 650, 88)]
        for i, (name, value) in enumerate(rows):
            y = 100 + 16 * i
            items += [pdf(name, 600, y, 640, y + 8), pdf(value, 800, y, 820, y + 8)]
        for i in range(6):     # seis ambientes rotulados na planta, ~30 m² cada
            x = 100 + 80 * i
            items += [pdf(f"QUARTO {i + 1}", x, 400, x + 50, 406), pdf("30,00 m²", x, 408, x + 40, 414)]
        r = interpret(items, None)
        self.assertEqual(r.area_m2, 180.0)
        self.assertTrue(any("descartada" in w for w in r.warnings))

    def test_table_and_plan_disagreeing_is_not_high_confidence(self):
        rows = [("Sala A", "10,00"), ("Sala B", "10,00"), ("Sala C", "10,00"), ("Sala D", "10,00"), ("Sala E", "10,00")]
        items = [pdf("Ambiente", 600, 80, 650, 88)]
        for i, (name, value) in enumerate(rows):
            y = 100 + 16 * i
            items += [pdf(name, 600, y, 640, y + 8), pdf(value, 800, y, 820, y + 8)]
        for i, v in enumerate(("10,00", "10,00", "10,00", "10,00", "14,00")):   # planta: 54 m² (8% a mais)
            x = 100 + 80 * i
            items += [pdf(f"QUARTO {i + 1}", x, 400, x + 50, 406), pdf(f"{v} m²", x, 408, x + 40, 414)]
        r = interpret(items, None)
        self.assertEqual((r.area_m2, r.confidence), (50.0, "media"))


class TableRowsTest(unittest.TestCase):
    """Quadro de áreas do Pronto Socorro em PDF: nomes em pedaços fora de ordem."""

    def _table(self, orphan=False):
        # Como no PDF real: a maioria das linhas vem inteira; duas vêm em pedaços.
        rows = [(["Adm. / Coordenação de Enfermagem"], "15,41"), (["Adm.", "e", "Laudo do Raio X"], "2,595"),
                (["Almoxarifado / Farmácia"], "14,335"), (["Ante-câmara Procedimentos"], "3,67"),
                (["Central", "de Vagas"], "5,315"), (["Recepção"], "22,50"),
                (["Sala de Sutura / Curativo"], "10,53"), (["Raio X"], "14,40"), (["DML"], "3,20")]
        items = [pdf("Ambiente", 1744, 50, 1790, 56)]
        for i, (parts, value) in enumerate(rows):
            y, x = 66 + 13 * i, 1744
            for part in parts:
                items.append(pdf(part, x, y, x + 3 * len(part), y + 5))
                x += 3 * len(part) + 2
            items.append(pdf(value, 1890 - 5 * len(value), y, 1890, y + 5))
        if orphan:
            items.append(pdf("7,77", 1870, 66 + 13 * len(rows), 1890, 71 + 13 * len(rows)))
        return items

    def test_split_names_are_rebuilt(self):
        rooms = find_area_table(self._table())
        self.assertEqual(len(rooms), 9)
        self.assertIn("Adm. e Laudo do Raio X", [r.name for r in rooms])
        self.assertIn("Central de Vagas", [r.name for r in rooms])
        self.assertAlmostEqual(sum(r.area_m2 for r in rooms),
                               15.41 + 2.595 + 14.335 + 3.67 + 5.315 + 22.50 + 10.53 + 14.40 + 3.20)

    def test_superscript_read_as_euro_keeps_the_row(self):
        # Pronto Socorro em imagem: "50,00⁵" lido pelo OCR como "50,00€" — a linha não pode sumir.
        items = self._table()
        items.append(pdf("Embarque e Desembarque", 1744, 66 + 13 * 9, 1810, 71 + 13 * 9))
        items.append(pdf("50,00€", 1862, 66 + 13 * 9, 1890, 71 + 13 * 9))
        rooms = find_area_table(items)
        self.assertIn(50.0, [r.area_m2 for r in rooms])      # o sobrescrito é descartado, não adivinhado

    def test_value_without_name_lowers_confidence(self):
        r = interpret(self._table(orphan=True), None)
        self.assertEqual(r.rooms_with_area, 9)
        self.assertNotEqual(r.confidence, "alta")
        self.assertTrue(any("sem nome identificado" in w for w in r.warnings))

    def test_partial_envelope_does_not_accuse_title_block(self):
        # Escola UFU: cotas legíveis cobrem só parte da obra; os ambientes não cabem nela.
        k, items = 10.0, []
        for fixed, vertical in ((60, False), (300, False), (60, True), (300, True)):
            pos = 100
            for v in (5.0, 5.0, 5.0):
                c = pos + v * k / 2
                box = (fixed, c - 4, fixed + 3, c + 4) if vertical else (c - 4, fixed, c + 4, fixed + 3)
                items.append(pdf(f"{v:.2f}".replace(".", ","), *box, angle=90 if vertical else 0))
                pos += v * k
        items += [pdf("ÁREA CONSTRUÍDA", 900, 900, 980, 906), pdf("4042,32 m²", 990, 900, 1040, 906)]
        for i in range(4):
            x = 100 + 60 * i
            items += [pdf(f"SALA {i + 1}", x, 150, x + 30, 156), pdf("700,00 m²", x, 158, x + 40, 164)]
        r = interpret(items, None)
        self.assertEqual((r.method, r.area_m2, r.confidence), ("carimbo", 4042.32, "alta"))


class EnvelopeFocusTest(unittest.TestCase):
    def test_building_chain_wins_over_lot_chain(self):
        # Prédio 20 × 10 m dentro de um terreno 40 × 30 m (cotas em metros, k = 10 px/m).
        k, items = 10.0, []

        def chain(values, start, fixed, vertical):
            pos = start
            for v in values:
                c = pos + v * k / 2
                box = (fixed, c - 4, fixed + 3, c + 4) if vertical else (c - 4, fixed, c + 4, fixed + 3)
                items.append(pdf(f"{v:.2f}".replace(".", ","), *box, angle=90 if vertical else 0))
                pos += v * k

        chain([5.0, 10.0, 5.0], 200, 180, False)       # prédio: x 200..400 (20 m)
        chain([4.0, 6.0], 300, 180, True)              # prédio: y 300..400 (10 m)
        chain([10.0, 20.0, 10.0], 100, 60, False)      # terreno: x 100..500 (40 m)
        chain([10.0, 10.0, 10.0], 100, 60, True)       # terreno: y 100..400 (30 m)
        env_all, _ = find_envelope(items)
        self.assertAlmostEqual(env_all.largura_m, 40.0, delta=0.5)
        focus = [(250, 330), (350, 370), (300, 350), (220, 390)]
        env, _ = find_envelope(items, focus=focus)
        self.assertAlmostEqual(env.largura_m, 20.0, delta=0.5)
        self.assertAlmostEqual(env.profundidade_m, 10.0, delta=0.5)
        self.assertEqual(env.box, (200.0, 300.0, 400.0, 400.0))


class OuterChainTest(unittest.TestCase):
    """Casas-padrão da PBH: a cadeia interna (615) e a externa (645, a cota total)."""

    def test_total_dimension_wins(self):
        from services.plan_reader import _tightest_cover
        extents = {0: [100.0, 715.0], 1: [85.0, 730.0], 2: [70.0, 745.0]}   # 615, 645 (total), 675 (mistura)
        names = [150.0, 300.0, 450.0, 600.0, 650.0]
        self.assertEqual(_tightest_cover(extents, names, True, {0: 3, 1: 1, 2: 5}), (85.0, 730.0))

    def test_lot_chain_stays_out(self):
        from services.plan_reader import _tightest_cover
        extents = {0: [100.0, 715.0], 1: [85.0, 730.0], 2: [0.0, 1000.0]}   # terreno: 1000
        names = [150.0, 300.0, 450.0, 600.0, 650.0]
        self.assertEqual(_tightest_cover(extents, names, True, {0: 3, 1: 3, 2: 1}), (85.0, 730.0))


class SalesPlanTest(unittest.TestCase):
    def test_private_area_of_one_unit(self):
        # Villa Leblon: "Área privativa: 38,04m² (apto) + 13,00m² (terraço) = 51,04m²".
        items = [pdf("APARTAMENTO 101", 100, 40, 260, 52), pdf("Área privativa:", 120, 70, 240, 82),
                 pdf("38,04m2 (apto)", 120, 95, 230, 107), pdf("+", 200, 112, 206, 124),
                 pdf("13,00m2 (terraço)", 110, 130, 240, 142), pdf("= 51,04m2", 170, 150, 240, 162)]
        r = interpret(items, None)
        self.assertEqual((r.area_m2, r.area_kind), (51.04, "privativa"))
        self.assertTrue(any("UMA unidade" in w for w in r.warnings))


class PartialCotasTest(unittest.TestCase):
    """Cotas lidas só numa parte da obra (escola UFU sem os textos de área: 1/4 da área real)."""

    def _plan(self, xs):
        k, items = 10.0, []

        def chain(values, start, fixed, vertical):
            pos = start
            for v in values:
                c = pos + v * k / 2
                box = (fixed, c - 4, fixed + 3, c + 4) if vertical else (c - 4, fixed, c + 4, fixed + 3)
                items.append(pdf(f"{v:.2f}".replace(".", ","), *box, angle=90 if vertical else 0))
                pos += v * k

        chain([5.0, 10.0, 5.0], 200, 180, False)       # cotas: x 200..400 (20 m)
        chain([4.0, 6.0], 300, 180, True)              # cotas: y 300..400 (10 m)
        for x, name in zip(xs, ["SALA", "COZINHA", "QUARTO", "BANHEIRO", "LAVANDERIA", "ESCRITORIO"]):
            items.append(pdf(name, x, 350, x + 40, 358))
        return interpret(items, None)

    def test_rooms_outside_the_cotas_give_no_area(self):
        r = self._plan([220 + 110 * i for i in range(6)])          # ambientes até x = 770
        self.assertEqual(r.method, "indisponivel")
        self.assertIsNone(r.area_m2)
        self.assertFalse(r.envelope.reliable)
        self.assertTrue(any("só parte da planta" in w for w in r.warnings))

    def test_rooms_inside_the_cotas_keep_the_rectangle(self):
        r = self._plan([210 + 30 * i for i in range(6)])           # todos entre x 210 e 400
        self.assertEqual(r.method, "cotas")
        self.assertAlmostEqual(r.area_m2, 200.0, delta=10)


if __name__ == "__main__":
    unittest.main()


class PrivatePlansTest(unittest.TestCase):
    """Lote de plantas privadas (15/11): planta de venda e quadro 'ÁREAS (m2)' de casa."""

    def test_sales_caption_is_the_unit_private_area(self):
        # Book Autêntico (Goiânia), pág. 65: sem isso caía no total do empreendimento.
        items = [pdf("Apartamento Tipo - Final 01 - 251,57m²", 100, 50, 260, 56), pdf("4 suítes", 100, 58, 130, 63)]
        r = interpret(items, None)
        self.assertEqual((r.area_m2, r.area_kind), (251.57, "privativa"))
        # Ryad: travessão longo e o asterisco da nota.
        r = interpret([pdf("*(217,59m² + 3,12 m² de escaninho)", 100, 40, 220, 46),
                       pdf("FINAL 1 – 220,71 m² *", 100, 50, 190, 56)], None)
        self.assertEqual((r.area_m2, r.area_kind), (220.71, "privativa"))
        # O mesmo book em imagem: o OCR leu "FINAL1-220,71m²*", tudo colado.
        r = interpret([ocr("*(217,59m² +3,12 m² de escaninho)", 94, 1197, 414, 1219),
                       ocr("FINAL1-220,71m²*", 88, 235, 704, 314)], None)
        self.assertEqual((r.area_m2, r.area_kind), (220.71, "privativa"))

    def test_two_units_on_the_page_is_ambiguous(self):
        from services.plan_reader import sales_unit_area
        items = [pdf("FINAL 1 – 220,71 m²", 100, 50, 190, 56), pdf("FINAL 2 – 262,87 m²", 300, 50, 390, 56)]
        self.assertIsNone(sales_unit_area(items))

    def test_areas_table_with_one_decimal(self):
        # Casa de Ituverava-SP: "ÁREAS (m2) / TERRENO 223,00 / CONSTRUÇÃO 117,2 / LIVRE 105,8".
        from services.plan_reader import find_declared_area
        items = [pdf("ÁREAS", 100, 100, 130, 106), pdf("m2", 200, 100, 210, 106),
                 pdf("TERRENO", 100, 110, 135, 116), pdf("223,00", 190, 110, 215, 116),
                 pdf("CONSTRUÇÃO", 100, 120, 150, 126), pdf("117,2", 195, 120, 215, 126),
                 pdf("LIVRE", 100, 130, 125, 136), pdf("105,8", 195, 130, 215, 136)]
        declared, _ = find_declared_area(items)
        self.assertEqual(declared.value, 117.2)
