import os
import sys
import unittest
from unittest import mock

for k in ("ODOO_LOGIN", "ODOO_API_KEY", "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
    os.environ.setdefault(k, "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import comex_reales as R  # noqa: E402


# Desglose real de account.move.line del DI de LUMI_304 (move id 48936, company 6),
# agrupado por codigo de cuenta neto (debito - credito) -- verificado en vivo contra PROD.
# Sin lineas de percepcion IVA/Ganancias (114103/114307) ni impuestos internos (114601): Canon +
# Argom no tributa impuestos internos, y el certificado de exclusion de Petdur dejo esas dos
# percepciones en cero para este despacho puntual (no es un bug, es el dato real).
NETO_LUMI_304 = {
    "114101": 68513347.24, "114203": 3878831.09, "114204": 6239946.2, "114205": 141748.01,
    "114206": 56010.13, "114207": 59028.64, "114208": 20961.88, "114209": 44576.38,
    "114210": 619053.72, "114211": 0, "114212": 99793.77, "114213": 99793.77,
    "114214": 2637.39, "114215": 2637.39, "114216": 8369.51, "114217": 14071.14,
    "114218": 8369.51, "114219": 192407.15, "114220": 7652.99, "114221": 3811.25,
    "114222": 3811.25, "114223": 1905.63, "114224": 83817.01, "114225": 7652.99,
    "114226": 1462498.59, "118001": 15658017.54, "118005": 109395208.21,
    "118006": 271556440.9, "211101": -478182399.28,
}


def fake_odoo_lineas(neto, account_ids_by_code):
    """Arma un fake de odoo_execute_kw que sirve account.move.line + account.account
    a partir de un dict {codigo: neto}, para alimentar build_nac_real sin pegarle a Odoo."""
    lines = [{"account_id": [account_ids_by_code[c], c], "debit": max(v, 0), "credit": max(-v, 0)}
             for c, v in neto.items()]
    accounts = [{"id": i, "code": c} for c, i in account_ids_by_code.items()]

    def odoo(uid, model, method, args, kwargs=None):
        if model == "account.move.line" and method == "search_read":
            return lines
        if model == "account.account" and method == "read":
            ids = args[0]
            return [a for a in accounts if a["id"] in ids]
        raise AssertionError("llamada inesperada: %s.%s" % (model, method))
    return odoo


class TestResolverDiCohorte(unittest.TestCase):
    def test_via_x_lumi_cohorte(self):
        def fake(uid, model, method, args, kwargs=None):
            self.assertEqual(model, "x_lumi_cohorte")
            self.assertEqual(args[0], [["x_codigo", "=", "LUMI_304"], ["x_company_id", "=", 6]])
            return [{"x_di_move_id": [48936, "DI 26001IC04196663D"], "x_tc_aduanero": 1524.5,
                     "x_fecha_oficializacion": "2026-09-29", "x_despacho": "26001IC04196663D"}]

        with mock.patch.object(R, "odoo_execute_kw", fake):
            move_id, name, fecha, tc = R.resolver_di_cohorte(1, "LUMI_304")
        self.assertEqual((move_id, name, fecha, tc), (48936, "DI 26001IC04196663D", "2026-09-29", 1524.5))

    def test_x_lumi_cohorte_sin_tc_queda_pendiente_sin_caer_al_metodo_viejo(self):
        llamados = []

        def fake(uid, model, method, args, kwargs=None):
            llamados.append(model)
            if model == "x_lumi_cohorte":
                return [{"x_di_move_id": [48936, "DI x"], "x_tc_aduanero": False,
                          "x_fecha_oficializacion": "2026-09-29", "x_despacho": "26001IC04196663D"}]
            raise AssertionError("no deberia consultar %s si la cohorte existe en x_lumi_cohorte" % model)

        with mock.patch.object(R, "odoo_execute_kw", fake):
            self.assertEqual(R.resolver_di_cohorte(1, "LUMI_304"), (None, None, None, None))
        self.assertEqual(llamados, ["x_lumi_cohorte"])

    def test_fallback_al_metodo_viejo_si_no_hay_x_lumi_cohorte(self):
        def fake(uid, model, method, args, kwargs=None):
            if model == "x_lumi_cohorte":
                return []
            if model == "account.move":
                return [{"id": 44648, "name": "DI 26001IC04179604W", "date": "2026-09-09",
                          "x_lumi_tc_historico": 1512}]
            raise AssertionError(model)

        with mock.patch.object(R, "odoo_execute_kw", fake):
            result = R.resolver_di_cohorte(1, "LUMI_302")
        self.assertEqual(result, (44648, "DI 26001IC04179604W", "2026-09-09", 1512.0))


class TestBuildNacReal(unittest.TestCase):
    def test_lumi_304_con_tc_de_x_lumi_cohorte_cuadra_el_gate(self):
        """Regresion con datos reales: antes de este fix, LUMI_304 no aparecia (ref vacio en el DI
        + x_lumi_tc_historico en 0 pese a que el TC real esta en x_lumi_cohorte.x_tc_aduanero)."""
        account_ids = {c: i + 1 for i, c in enumerate(NETO_LUMI_304)}
        odoo_cohorte = fake_odoo_lineas(NETO_LUMI_304, account_ids)

        def fake(uid, model, method, args, kwargs=None):
            if model == "x_lumi_cohorte":
                return [{"x_di_move_id": [48936, "DI 26001IC04196663D"], "x_tc_aduanero": 1524.5,
                         "x_fecha_oficializacion": "2026-09-29", "x_despacho": "26001IC04196663D"}]
            return odoo_cohorte(uid, model, method, args, kwargs)

        with mock.patch.object(R, "odoo_execute_kw", fake):
            real = R.build_nac_real(1, "LUMI_304")
        self.assertIsNotNone(real, "el gate tiene que cuadrar con el TC real (1524.5)")
        self.assertEqual(real["di"], "26001IC04196663D")
        self.assertEqual(real["tc"], 1524.5)
        self.assertAlmostEqual(real["desembolso"], 63779, delta=1)
        self.assertAlmostEqual(real["noRecup"], 10271, delta=1)
        self.assertAlmostEqual(real["pIva"], 0)
        self.assertAlmostEqual(real["pGan"], 0)
        self.assertAlmostEqual(real["impInt"], 0)

    def test_gate_es_invariante_al_tc(self):
        """El gate (noRecup+credito vs desembolso) compara saldos ARS que, por partida doble, ya
        cuadran entre si ANTES de dividir por el TC -- asi que un TC equivocado (pero no cero/vacio)
        no lo hace fallar, solo corre todo el resultado en USD a una escala equivocada. El gate
        detecta un mapeo de cuentas roto, no un TC erroneo (ese es un riesgo real: un TC mal
        cargado en x_lumi_cohorte pasa el gate igual)."""
        account_ids = {c: i + 1 for i, c in enumerate(NETO_LUMI_304)}
        odoo_cohorte = fake_odoo_lineas(NETO_LUMI_304, account_ids)

        def fake(uid, model, method, args, kwargs=None):
            if model == "x_lumi_cohorte":
                return [{"x_di_move_id": [48936, "DI x"], "x_tc_aduanero": 1000.0,
                         "x_fecha_oficializacion": "2026-09-29", "x_despacho": "26001IC04196663D"}]
            return odoo_cohorte(uid, model, method, args, kwargs)

        with mock.patch.object(R, "odoo_execute_kw", fake):
            real = R.build_nac_real(1, "LUMI_304")
        self.assertIsNotNone(real)
        self.assertEqual(real["tc"], 1000.0)
        self.assertNotAlmostEqual(real["desembolso"], 63779, delta=1000)

    def test_gate_no_cuadra_si_falta_una_cuenta(self):
        """Mapeo de cuentas roto (ac'a: falta 118001, Cuenta Puente Mercaderias) -- ESTO si tiene
        que fallar el gate, a diferencia de un TC equivocado."""
        neto_roto = dict(NETO_LUMI_304)
        del neto_roto["118001"]
        account_ids = {c: i + 1 for i, c in enumerate(neto_roto)}
        odoo_cohorte = fake_odoo_lineas(neto_roto, account_ids)

        def fake(uid, model, method, args, kwargs=None):
            if model == "x_lumi_cohorte":
                return [{"x_di_move_id": [48936, "DI x"], "x_tc_aduanero": 1524.5,
                         "x_fecha_oficializacion": "2026-09-29", "x_despacho": "26001IC04196663D"}]
            return odoo_cohorte(uid, model, method, args, kwargs)

        with mock.patch.object(R, "odoo_execute_kw", fake):
            self.assertIsNone(R.build_nac_real(1, "LUMI_304"))

    def test_sin_di_resoluble_devuelve_none(self):
        def fake(uid, model, method, args, kwargs=None):
            if model == "x_lumi_cohorte":
                return []
            if model == "account.move":
                return []
            raise AssertionError(model)

        with mock.patch.object(R, "odoo_execute_kw", fake):
            self.assertIsNone(R.build_nac_real(1, "LUMI_999"))


if __name__ == "__main__":
    unittest.main()
