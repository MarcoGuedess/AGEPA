"""Testes da avaliação contra o gabarito (avaliar_plantas.py), sem servidor."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import avaliar_plantas as av  # noqa: E402


def linha(real=1324.27, tipo="construida", tol=0.5, na_folha=True):
    return av.Linha("x.pdf", real, tipo, na_folha, tol, True, "", "")


class ClassificarTest(unittest.TestCase):
    def test_acerto_dentro_da_tolerancia(self):
        cat, erro, aluc = av.classificar(linha(), 1324.27, "construida", "alta")
        self.assertEqual((cat, aluc), ("acerto", False))
        self.assertAlmostEqual(erro, 0.0)

    def test_erro_com_confianca_alta_e_alucinacao(self):
        cat, erro, aluc = av.classificar(linha(), 1545.99, "construida", "alta")
        self.assertEqual(cat, "erro")
        self.assertTrue(aluc)
        self.assertAlmostEqual(erro, 16.74, places=2)

    def test_erro_com_confianca_media_nao_e_alucinacao(self):
        self.assertEqual(av.classificar(linha(), 1545.99, "construida", "media")[2], False)

    def test_sem_resposta_nao_e_erro(self):
        self.assertEqual(av.classificar(linha(), None, None, "baixa"), ("sem resposta", None, False))

    def test_outro_tipo_de_area_plausivel_nao_conta_como_erro(self):
        cat, erro, aluc = av.classificar(linha(tol=10, na_folha=False), 1100.0, "util", "alta")
        self.assertEqual((cat, aluc), ("tipo diferente", False))
        self.assertLess(erro, 0)

    def test_outro_tipo_implausivel_e_erro(self):
        # Caso real (Mozarlândia): "área útil" de 10,46 m² com confiança alta numa creche de 1.324 m².
        self.assertEqual(av.classificar(linha(tol=10, na_folha=False), 10.46, "util", "alta")[::2], ("erro", True))
        # Envoltória 6x maior que a obra, com confiança baixa: erro, mas não alucinação.
        self.assertEqual(av.classificar(linha(tol=10, na_folha=False), 8462.01, "envoltoria", "baixa")[::2],
                         ("erro", False))

    def test_sem_gabarito_so_registra(self):
        self.assertEqual(av.classificar(linha(real=None), 900.0, "util", "alta"), ("sem gabarito", None, False))


class GabaritoTest(unittest.TestCase):
    def test_le_planilha_brasileira(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "g.csv"
            p.write_text(
                "arquivo;area_real_m2;tipo_area;total_na_folha;tolerancia_pct;usar;fonte_gabarito;observacoes\r\n"
                "a.pdf;1.324,27;construida;sim;0,5;sim;quadro;\r\n"
                "b.pdf;;construida;não;10;não;;memorial\r\n",
                encoding="utf-8-sig",
            )
            a, b = av.carregar_gabarito(p)
            self.assertEqual((a.area_real, a.total_na_folha, a.tolerancia_pct, a.usar), (1324.27, True, 0.5, True))
            self.assertEqual((b.area_real, b.usar), (None, False))

    def test_coluna_obrigatoria(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "g.csv"
            p.write_text("arquivo;area_real_m2\r\na.pdf;10\r\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                av.carregar_gabarito(p)


if __name__ == "__main__":
    unittest.main()
