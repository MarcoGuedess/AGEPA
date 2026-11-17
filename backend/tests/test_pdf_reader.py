"""Testes do leitor de PDF: escolha de página e leituras simultâneas (PDFium)."""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.pdf_reader import choose_document_area, read_pdf, scan_document_areas  # noqa: E402


def make_pdf(pages):
    """PDF mínimo com texto (Helvetica), uma lista de linhas por página."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None]
    font = 3 + 2 * len(pages)
    kids = []
    for i, lines in enumerate(pages):
        content = "BT /F1 14 Tf 72 720 Td " + " ".join(
            ("0 -30 Td " if j else "") + f"({t}) Tj" for j, t in enumerate(lines)) + " ET"
        page_obj, cont_obj = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{page_obj} 0 R")
        objs += [None, None]
        objs[page_obj - 1] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cont_obj} 0 R "
                              f"/Resources << /Font << /F1 {font} 0 R >> >> >>")
        objs[cont_obj - 1] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    objs = objs[:font - 1] + [None]
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objs[font - 1] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out, offsets = "%PDF-1.4\n", []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{obj}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


PDF = make_pdf([["CORTES E FACHADAS", "SALA", "A=99,99 m2"],
                ["PLANTA BAIXA", "AREA CONSTRUIDA: 402,43 m2"]])


class PageChoiceTest(unittest.TestCase):
    def test_default_is_the_floor_plan_page(self):
        page = read_pdf(PDF)
        self.assertEqual((page.page_index, page.page_count), (1, 2))

    def test_chosen_page(self):
        self.assertEqual(read_pdf(PDF, 1).page_index, 0)

    def test_missing_page_is_a_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "não existe"):
            read_pdf(PDF, 7)


class DocumentAreaTest(unittest.TestCase):
    """Total do empreendimento escrito em outra página (TCC da UFSC: memorial, pág. 110)."""

    def test_total_found_in_memorial_page(self):
        doc = make_pdf([["PLANTA BAIXA", "SALA", "A=20,00 m2"],
                        ["MEMORIAL DESCRITIVO", "Area Total de Construcao - 9127,55 m2"],
                        ["empreendimento com area construida de 9.127,55 m2"]])
        found = scan_document_areas(doc)
        self.assertEqual(sorted(f["pagina"] for f in found), [2, 3])
        best = choose_document_area(found)
        self.assertEqual((best["valor"], best["paginas"], best["outros_valores"]), (9127.55, [2, 3], []))

    def test_land_area_is_not_the_building(self):
        doc = make_pdf([["PLANTA BAIXA"], ["Area do terreno: 1.582,81 m2"]])
        self.assertIsNone(choose_document_area(scan_document_areas(doc)))

    def test_prose_memorial(self):
        # CAR-BA: número antes das palavras; a ampliação, na frase seguinte, fica de fora.
        doc = make_pdf([["A unidade habitacional possui 51,34 m2 de area construida."],
                        ["O estudo de ampliacao da casa eleva para 61,12 m2 de area construida."]])
        best = choose_document_area(scan_document_areas(doc))
        self.assertEqual((best["valor"], best["outros_valores"]), (51.34, []))

    def test_minimum_requirement_is_not_a_project(self):
        # Edital da AGEHAB: "com área construída de no mínimo 46,00 m²".
        doc = make_pdf([["Casa com area construida de no minimo 46,00 m2"]])
        self.assertIsNone(choose_document_area(scan_document_areas(doc)))

    def test_several_options_keep_the_first_and_list_the_rest(self):
        doc = make_pdf([["Opcao A com area construida de 54,54 m2"], ["Opcao B com area construida de 52,46 m2"]])
        best = choose_document_area(scan_document_areas(doc))
        self.assertEqual((best["valor"], best["outros_valores"], best["repetido"]), (54.54, [52.46], False))

    def test_one_apartment_is_not_the_building(self):
        # Material de venda: a área de um apartamento numerado não é o total do prédio.
        doc = make_pdf([["COBERTURA APARTAMENTO 403", "Area total = 75,65 m2"]])
        self.assertIsNone(choose_document_area(scan_document_areas(doc)))


class ProseAreaTest(unittest.TestCase):
    def test_phrasings(self):
        from services.plan_reader import prose_built_areas
        cases = {
            "uma moradia com área construída total de 52 m², composta de sala": 52.0,      # Serviço Florestal
            "um total de área construída equivalente a 3.213,00m²": 3213.0,               # Policlínica (MS)
            "o empreendimento uma área construída de 9.127,55 m²": 9127.55,               # TCC da UFSC
        }
        for text, value in cases.items():
            self.assertEqual([v for v, _, _ in prose_built_areas(text)], [value], text)
        self.assertEqual(prose_built_areas("lote com área mínima de 200,00 m²"), [])
        # Área de COBERTURA (telhado) não é a construída: na folha da quadra do FNDE,
        # "ÁREA CONSTRUÍDA: 785,00 m²" e "ÁREA COBERTURA: 980,40 m²".
        self.assertEqual(prose_built_areas("apresenta uma área total de 980,40 m² de cobertura, para"), [])

    def test_more_phrasings_from_the_second_batch(self):
        from services.plan_reader import prose_built_areas
        cases = {
            # Mauá (UBS porte IV): "construída útil" é a área da obra
            "implantada num terreno de 5.650,00 m2, com uma área construída útil de 1.161,77 m² e": [1161.77],
            # Águas Lindas (CRAS)
            "conclusão da construção do CRAS, remanescente de obra com área de 456,22m².": [456.22],
            # Porto Alegre (CRAS Glória): a lista por pavimento termina no TOTAL
            "Área edificada: - Pav. Térreo: 312,45 m² - Subsolo: 148,00 m² - TOTAL: 460,45 m² Imóvel": [460.45],
            "Trata-se de uma edificação de dois pavimentos, com superfície total de 460,45 m².": [460.45],
            # a área de UM pavimento não é a da obra
            "concentra-se no pavimento térreo, que possui área total de 312,45 m², que se situa": [],
        }
        for text, values in cases.items():
            self.assertEqual([v for v, _, _ in prose_built_areas(text)], values, text)


class ProsePrazoTest(unittest.TestCase):
    def test_execution_period(self):
        from services.plan_reader import prose_prazos
        cases = {
            "O prazo de execução do contrato será de 240 (duzentos e quarenta) dias, com início": [8.0],  # Caçapava
            "o prazo de execução da obra será de 16 (dezesseis) meses": [16.0],                        # Salto
            "6.5. Data de execução: O prazo de execução do contrato é de 24 meses, com início": [24.0],  # Mauá
            "3 meses para elaboração dos projetos e 12 meses para construção da Creche": [12.0],
            # prazos de processo, não da obra
            "conceder-se-á à recorrente o prazo de 03 (três) dias úteis para apresentação": [],
            "O prazo de validade da proposta não será inferior a 60 (sessenta) dias": [],
        }
        for text, values in cases.items():
            self.assertEqual([m for m, _, _ in prose_prazos(text)], values, text)


class AreasHeaderTest(unittest.TestCase):
    def test_built_line_under_areas_header(self):
        # Luziânia (CRAS, folha estrutural): "ÁREAS:" e, abaixo, "CONSTRUÍDA 314,94m²".
        from services.plan_reader import TextItem, find_declared_area
        items = [TextItem("ÁREAS:", 2281, 1588, 2310, 1595, 0, "pdf"),
                 TextItem("TERRENO 1.100,00m²", 2283, 1601, 2355, 1607, 0, "pdf"),
                 TextItem("CONSTRUÍDA 314,94m²", 2283, 1611, 2361, 1618, 0, "pdf"),
                 TextItem("PERMEÁVEL 598,94m²", 2283, 1622, 2358, 1630, 0, "pdf")]
        declared, _ = find_declared_area(items)
        self.assertEqual(declared.value, 314.94)


class TitleBlockOneLineTest(unittest.TestCase):
    def test_built_area_next_to_useful_area(self):
        # CAR-BA: o carimbo inteiro numa linha só; a área útil ao lado não anula a construída.
        from services.plan_reader import TextItem, find_declared_area
        it = TextItem("ÁREA CONSTRUÇÃO - 64,82 m2 ÁREA ÚTIL - 53,41 m2 PASSEIO EXTERNO - 15,55 m2",
                      0, 0, 500, 12, 0, "pdf")
        declared, _ = find_declared_area([it])
        self.assertEqual(declared.value, 64.82)
        self.assertIsNone(find_declared_area([TextItem("ÁREA CONSTRUÍDA EXISTENTE: 120,00 m2", 0, 0, 300, 12, 0,
                                                       "pdf")])[0])


class ConcurrencyTest(unittest.TestCase):
    def test_simultaneous_reads_do_not_crash(self):
        # O servidor atende PDFs com texto sem fila; o PDFium não aceita duas threads ao mesmo
        # tempo (caiu o processo inteiro num teste real). A trava em read_pdf serializa.
        results, errors = [], []

        def work(n):
            try:
                results.append(read_pdf(PDF, 1 + n % 2).page_index)
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [0] * 4 + [1] * 4)


if __name__ == "__main__":
    unittest.main()


class GlyphFontTest(unittest.TestCase):
    """Fonte sem mapa de caracteres (casa da UNIFAP): o PDFium devolve o número do glifo."""

    def test_decode_glyph_order(self):
        from services.pdf_reader import _decode_glyph, _fix_superscript
        dec = lambda s: "".join(_decode_glyph(ord(c)) for c in s)
        self.assertEqual(dec("'RUPLWyULR"), "Dormitório")
        self.assertEqual(dec("&LUFXODomR"), "Circulação")
        self.assertEqual(dec("'LPHQV}HV"), "Dimensões")
        # algarismos e vírgula viram caracteres de controle (0x13–0x1C, 0x0F)
        self.assertEqual("".join(_decode_glyph(c) for c in (0x18, 0x14, 0x0F, 0x14, 0x1B)), "51,18")
        self.assertEqual(_fix_superscript("A=51,18 m" + chr(240)), "A=51,18 m²")
        self.assertEqual(_fix_superscript(chr(200) + "rea de Serviço"), "Área de Serviço")

    def test_unifap_sheet_reads_room_labels(self):
        path = Path(__file__).resolve().parents[2] / "avaliacao/plantas/privadas/casa_unifap/Prancha.pdf"
        if not path.exists():
            self.skipTest("PDF de teste não baixado")
        from services.pdf_reader import read_pdf
        texts = [it.text for it in read_pdf(path.read_bytes(), 1).items]
        self.assertIn("A=51,18 m²", texts)                  # varanda
        self.assertTrue(any("Dormitório" in t for t in texts))
        self.assertFalse(any("'RUPLWyULR" in t for t in texts))

    def test_normal_pdf_untouched(self):
        from services.pdf_reader import _plan_word_hits
        self.assertEqual(_plan_word_hits("Planta Baixa - Sala de Estar"), 3)
