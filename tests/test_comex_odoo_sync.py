import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import comex_odoo_sync as S  # noqa: E402


def normalizar(ref):
    import re
    m = re.search(r"(LUMI|LUPE)[_ ]?0?(\d{2,3})", ref or "", re.IGNORECASE)
    if not m or m.group(1).upper() != "LUMI":
        return None
    return "LUMI_%03d" % int(m.group(2))


def tracker_base():
    return {
        "_ts": 1000,
        "params": {"gp_fwd": 930},
        "dispo": {"items": [{"fac": "A", "monto": 5}], "giros": []},
        "seg": [
            {"id": "LUMI_302", "pais": "Argentina", "prov": "Prov A", "prod": "Producto A", "origen": "China",
             "modo": "Marítimo", "inco": "FOB", "estado": "en transito", "fOrden": "2026-07-01", "etd": "2026-08-01",
             "eta": "2026-10-20", "fOfic": "", "fLib": "", "conts": "2", "docs": "Falta BL", "despa": "Juan",
             "opDesp": "Mundo Comex", "notas": "nota A", "costEst": "100000", "nacEst": "40000",
             "pagos": [{"concepto": "anticipo", "monto": "30000", "pagado": True},
                       {"concepto": "saldo", "monto": "70000", "pagado": False, "fecha": "2026-10-10"}],
             "_m": 900},
            {"id": "LUMI_304", "pais": "Argentina", "prov": "Prov B", "prod": "Producto B", "estado": "Embarcado",
             "eta": "fecha rara", "conts": "", "despa": "", "opDesp": "Petdur", "notas": "", "costEst": "", "nacEst": "",
             "pagos": [], "_m": 901},
            {"id": "LUPE_010", "pais": "Perú", "estado": "Embarcado", "notas": "peru", "_m": 902},
        ],
    }


REALES = [
    {"id": "LUMI_302", "gastosReal": {"total": 2688.0, "parcial": False}, "nacReal": {"desembolso": 44008.0},
     "nacEstSnap": None},
    {"id": "LUMI_304", "gastosReal": {"total": 1366.0, "parcial": True}, "nacReal": None, "nacEstSnap": None},
]


class FakeOdoo:
    """Odoo en memoria: x.comex.embarque + facturas de Petdur."""

    def __init__(self, instalado=True, facturas=None):
        self.instalado = instalado
        self.recs = {}
        self.next_id = 1
        self.facturas = facturas or []
        self.calls = []
        self.hook_search_read = None  # callable(n_llamada) para simular una edicion concurrente
        self.n_search_read = 0

    def __call__(self, model, method, args, kwargs=None):
        self.calls.append((model, method))
        if model == "ir.model":
            return 1 if self.instalado else 0
        if model == "account.move":
            return self.facturas
        assert model == S.MODELO
        # Odoo rechaza '' en un Selection (x_operador, x_estado): tiene que ir False.
        for v in ([args[0]] if method == "create" else [args[1]] if method == "write" else []):
            for campo in ("x_operador", "x_estado", "x_pais"):
                assert v.get(campo, False) != "", "Selection %s con '' (Odoo lanza ValueError)" % campo
        if method == "search_read":
            self.n_search_read += 1
            if self.hook_search_read:
                self.hook_search_read(self.n_search_read)
            nombres = args[0][0][2]
            return [copy.deepcopy(r) for r in self.recs.values() if r["name"] in nombres]
        if method == "write":
            ids, vals = args
            for i in ids:
                self.recs[i].update(vals)
            return True
        if method == "create":
            rec = dict(args[0], id=self.next_id)
            self.recs[self.next_id] = rec
            self.next_id += 1
            return rec["id"]
        raise AssertionError(method)

    def por_nombre(self, name):
        return next(r for r in self.recs.values() if r["name"] == name)


class FakeTracker:
    def __init__(self, data):
        self.data = data
        self.etag = 1
        self.escrituras = 0
        self.backups = 0
        self.conflictos_pendientes = 0
        self.antes_de_leer = None

    def leer(self):
        if self.antes_de_leer:
            self.antes_de_leer()
        return copy.deepcopy(self.data), self.etag

    def escribir(self, data, etag):
        if self.conflictos_pendientes:
            self.conflictos_pendientes -= 1
            self.etag += 1
            raise S.ConflictoTracker()
        if etag != self.etag:
            raise S.ConflictoTracker()
        self.data = copy.deepcopy(data)
        self.etag += 1
        self.escrituras += 1

    def backup(self, data):
        self.backups += 1


def correr(odoo, tracker, **kw):
    logs = []
    res = S.sincronizar(odoo, tracker.leer, tracker.escribir, tracker.backup, kw.pop("reales", REALES),
                        normalizar, log=logs.append, **kw)
    return res, logs


FACTURA_302 = {"ref": "LUMI_302 CIF", "amount_untaxed": 200000.0, "currency_id": [2, "USD"], "move_type": "in_invoice"}


class TestHelpers(unittest.TestCase):
    def test_estado_acentos_y_mayusculas(self):
        self.assertEqual(S.estado_odoo("en transito"), "En tránsito")
        self.assertEqual(S.estado_odoo("EN PRODUCCION"), "En producción")
        self.assertIs(S.estado_odoo("raro"), False)
        self.assertIs(S.estado_odoo(""), False)

    def test_operador_ida_y_vuelta(self):
        self.assertEqual(S.operador_a_odoo("Mundo Comex"), "MC")
        self.assertEqual(S.operador_a_odoo("Trice"), "TR")
        self.assertEqual(S.operador_a_odoo("Petdur"), "OT")
        self.assertEqual(S.operador_a_odoo(""), "")
        self.assertEqual(S.operador_a_tracker("MC"), "Mundo Comex")
        self.assertEqual(S.operador_a_tracker("TR"), "Trice")
        self.assertEqual(S.operador_a_tracker("OT"), "Otro")
        self.assertEqual(S.operador_a_tracker(False), "")

    def test_num_como_parsefloat(self):
        self.assertEqual(S.num("12345"), 12345.0)
        self.assertEqual(S.num("12.5abc"), 12.5)
        self.assertEqual(S.num(""), 0.0)
        self.assertEqual(S.num(None), 0.0)
        self.assertEqual(S.num(7), 7.0)

    def test_fecha(self):
        self.assertEqual(S.fecha_odoo("2026-10-20"), "2026-10-20")
        self.assertEqual(S.fecha_odoo("2026-10-20T10:00:00"), "2026-10-20")
        self.assertIs(S.fecha_odoo("fecha rara"), False)
        self.assertIs(S.fecha_odoo(""), False)

    def test_gastos_est(self):
        self.assertAlmostEqual(S.gastos_est_usd(200000, 2), 200000 * 0.005 + 2 * 2000 + 930 + 200)
        self.assertEqual(S.gastos_est_usd(None, 2), 0.0)

    def test_saldo_pagos(self):
        self.assertEqual(S.saldo_pagos(tracker_base()["seg"][0]), 70000.0)

    def test_valores_pipeline(self):
        seg = tracker_base()["seg"][0]
        v = S.valores_pipeline(seg, REALES[0], 200000.0)
        self.assertEqual(v["name"], "LUMI_302")
        self.assertEqual(v["x_estado"], "En tránsito")
        self.assertEqual(v["x_contenedores"], 2)
        self.assertEqual(v["x_nac_real"], 44008.0)
        self.assertEqual(v["x_gastos_real"], 2688.0)
        self.assertFalse(v["x_parcial"])
        self.assertEqual(v["x_nac_est"], 40000.0)
        self.assertEqual(v["x_saldo_pagos"], 70000.0)
        self.assertEqual(v["x_f_ofic"], False)
        self.assertEqual(v["company_id"], 6)

    def test_valores_pipeline_nac_est_prefiere_snapshot(self):
        seg = tracker_base()["seg"][0]
        v = S.valores_pipeline(seg, {"nacEstSnap": 39000}, None)
        self.assertEqual(v["x_nac_est"], 39000.0)
        self.assertEqual(v["x_gastos_est"], 0.0)


class TestResolverMano(unittest.TestCase):
    def rec(self, **kw):
        base = {"name": "LUMI_302", "x_notas": "nota A", "x_notas_sync": "nota A", "x_despachante": "Juan",
                "x_despachante_sync": "Juan", "x_operador": "MC", "x_operador_sync": "MC"}
        base.update(kw)
        return base

    def test_sin_cambios_en_odoo_manda_el_tracker(self):
        seg = dict(tracker_base()["seg"][0], notas="nota nueva del tracker")
        r = S.resolver_mano(self.rec(), seg)
        self.assertEqual(r["notas"]["gana"], "tracker")
        self.assertEqual(r["notas"]["odoo"], "nota nueva del tracker")
        self.assertFalse(any(x["cambia_tracker"] for x in r.values()))

    def test_odoo_editado_gana(self):
        seg = tracker_base()["seg"][0]
        r = S.resolver_mano(self.rec(x_notas="editada en Odoo", x_operador="TR"), seg)
        self.assertEqual((r["notas"]["gana"], r["notas"]["tracker"], r["notas"]["cambia_tracker"]),
                         ("odoo", "editada en Odoo", True))
        self.assertEqual((r["opDesp"]["gana"], r["opDesp"]["tracker"]), ("odoo", "Trice"))
        self.assertFalse(r["despa"]["cambia_tracker"])

    def test_operador_ot_no_pisa_texto_libre_del_tracker(self):
        seg = dict(tracker_base()["seg"][1])  # opDesp "Petdur"
        r = S.resolver_mano(self.rec(x_operador="OT", x_operador_sync="OT"), seg)
        self.assertEqual(r["opDesp"]["gana"], "tracker")
        r2 = S.resolver_mano(self.rec(x_operador="OT", x_operador_sync="MC"), seg)
        self.assertEqual(r2["opDesp"]["gana"], "odoo")
        self.assertFalse(r2["opDesp"]["cambia_tracker"], "Odoo OT == tracker 'Petdur' (OT): no se pisa el texto")

    def test_odoo_vaciado_gana(self):
        r = S.resolver_mano(self.rec(x_notas=False), tracker_base()["seg"][0])
        self.assertEqual((r["notas"]["gana"], r["notas"]["tracker"], r["notas"]["cambia_tracker"]), ("odoo", "", True))

    def test_registro_nuevo_manda_el_tracker(self):
        r = S.resolver_mano(None, tracker_base()["seg"][0])
        self.assertTrue(all(x["gana"] == "tracker" for x in r.values()))
        self.assertEqual(r["opDesp"]["odoo"], "MC")


class TestVerificarDiff(unittest.TestCase):
    def test_solo_lo_permitido(self):
        a = tracker_base()
        cambios = {"LUMI_302": {"notas": "x"}}
        b = S.aplicar_cambios_tracker(a, cambios, 5000)
        self.assertEqual(S.verificar_diff(a, b, cambios), [])
        self.assertEqual(b["seg"][0]["notas"], "x")
        self.assertEqual(b["seg"][0]["_m"], 5000)
        self.assertEqual(b["seg"][1]["_m"], 901, "los demas registros no se tocan")
        self.assertEqual(b["_ts"], 5000)

    def test_ts_siempre_avanza(self):
        a = tracker_base()
        b = S.aplicar_cambios_tracker(a, {"LUMI_302": {"notas": "x"}}, 10)
        self.assertEqual(b["_ts"], 1001)

    def test_detecta_cambio_ajeno(self):
        a = tracker_base()
        cambios = {"LUMI_302": {"notas": "x"}}
        b = S.aplicar_cambios_tracker(a, cambios, 5000)
        b["seg"][0]["estado"] = "Entregado"
        self.assertTrue(any("estado" in p for p in S.verificar_diff(a, b, cambios)))
        c = S.aplicar_cambios_tracker(a, cambios, 5000)
        c["seg"][1]["notas"] = "otro registro"
        self.assertTrue(S.verificar_diff(a, c, cambios))
        d = S.aplicar_cambios_tracker(a, cambios, 5000)
        d["dispo"]["items"].append({})
        self.assertTrue(any("dispo" in p for p in S.verificar_diff(a, d, cambios)))
        e = S.aplicar_cambios_tracker(a, cambios, 5000)
        e["seg"].pop()
        self.assertTrue(S.verificar_diff(a, e, cambios))


class TestSincronizar(unittest.TestCase):
    def test_modulo_sin_instalar_se_omite(self):
        odoo, trk = FakeOdoo(instalado=False), FakeTracker(tracker_base())
        res, _ = correr(odoo, trk)
        self.assertTrue(res["skipped"])
        self.assertEqual([c for c in odoo.calls if c[0] == S.MODELO], [])

    def test_primera_corrida_crea_solo_argentina(self):
        odoo, trk = FakeOdoo(facturas=[FACTURA_302]), FakeTracker(tracker_base())
        res, _ = correr(odoo, trk)
        self.assertEqual((res["creados"], res["actualizados"]), (2, 0))
        self.assertEqual(sorted(r["name"] for r in odoo.recs.values()), ["LUMI_302", "LUMI_304"])
        r = odoo.por_nombre("LUMI_302")
        self.assertEqual(r["x_gastos_real"], 2688.0)
        self.assertEqual(r["x_nac_real"], 44008.0)
        self.assertAlmostEqual(r["x_gastos_est"], 200000 * 0.005 + 2 * 2000 + 930 + 200)
        self.assertEqual((r["x_notas"], r["x_notas_sync"]), ("nota A", "nota A"))
        self.assertEqual((r["x_operador"], r["x_operador_sync"]), ("MC", "MC"))
        r4 = odoo.por_nombre("LUMI_304")
        self.assertEqual(r4["x_operador"], "OT")
        self.assertTrue(r4["x_parcial"])
        self.assertIs(r4["x_eta"], False)
        self.assertEqual(r4["x_gastos_est"], 0.0)
        self.assertEqual(res["cif_pendiente"], ["LUMI_304"])
        self.assertEqual(trk.escrituras, 0, "sin ediciones en Odoo el tracker no se toca")

    def test_idempotente(self):
        odoo, trk = FakeOdoo(facturas=[FACTURA_302]), FakeTracker(tracker_base())
        correr(odoo, trk)
        snap_odoo = {k: {f: v for f, v in r.items() if f != "x_sync_ts"} for k, r in odoo.recs.items()}
        antes = copy.deepcopy(trk.data)
        res, _ = correr(odoo, trk)
        self.assertEqual((res["creados"], res["actualizados"]), (0, 2))
        self.assertEqual(len(odoo.recs), 2, "no duplica")
        self.assertEqual(trk.data, antes)
        self.assertEqual(trk.escrituras, 0)
        self.assertEqual({k: {f: v for f, v in r.items() if f != "x_sync_ts"} for k, r in odoo.recs.items()},
                         snap_odoo)

    def test_writeback_de_una_nota_editada_en_odoo(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        original = copy.deepcopy(trk.data)
        odoo.por_nombre("LUMI_302")["x_notas"] = "llamar al despachante el lunes"
        res, logs = correr(odoo, trk)
        self.assertTrue(res["tracker_escrito"] and res["writeback_ok"])
        self.assertEqual(trk.escrituras, 1)
        self.assertEqual(trk.backups, 1)
        nuevo = trk.data
        self.assertEqual(nuevo["seg"][0]["notas"], "llamar al despachante el lunes")
        self.assertGreater(nuevo["_ts"], original["_ts"])
        self.assertEqual(nuevo["seg"][0]["_m"], nuevo["_ts"])
        # nada mas cambio
        self.assertEqual(S.verificar_diff(original, nuevo, {"LUMI_302": {"notas": "llamar al despachante el lunes"}}), [])
        # el snapshot de Odoo quedo alineado
        r = odoo.por_nombre("LUMI_302")
        self.assertEqual((r["x_notas"], r["x_notas_sync"]), ("llamar al despachante el lunes",) * 2)
        # y la corrida siguiente no vuelve a escribir
        res2, _ = correr(odoo, trk)
        self.assertEqual(trk.escrituras, 1)
        self.assertFalse(res2["tracker_escrito"])

    def test_estado_por_mail_y_nota_en_odoo_el_mismo_dia_no_se_pisan(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        odoo.por_nombre("LUMI_302")["x_notas"] = "nota de Odoo"
        trk.data["seg"][0]["estado"] = "Arribado"          # llego por mail
        trk.data["seg"][0]["eta"] = "2026-10-05"
        correr(odoo, trk)
        self.assertEqual(trk.data["seg"][0]["notas"], "nota de Odoo")
        self.assertEqual(trk.data["seg"][0]["estado"], "Arribado", "la nota no pisa el estado")
        r = odoo.por_nombre("LUMI_302")
        self.assertEqual(r["x_estado"], "Arribado")
        self.assertEqual(r["x_eta"], "2026-10-05")
        self.assertEqual(r["x_notas"], "nota de Odoo")

    def test_nota_cambiada_en_el_tracker_baja_a_odoo(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        trk.data["seg"][0]["notas"] = "nota nueva del tracker"
        res, _ = correr(odoo, trk)
        self.assertEqual(trk.escrituras, 0)
        r = odoo.por_nombre("LUMI_302")
        self.assertEqual((r["x_notas"], r["x_notas_sync"]), ("nota nueva del tracker",) * 2)

    def test_si_cambian_los_dos_lados_gana_odoo(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        odoo.por_nombre("LUMI_302")["x_despachante"] = "Pedro"
        trk.data["seg"][0]["despa"] = "Maria"
        correr(odoo, trk)
        self.assertEqual(trk.data["seg"][0]["despa"], "Pedro")
        self.assertEqual(odoo.por_nombre("LUMI_302")["x_despachante_sync"], "Pedro")

    def test_operador_editado_en_odoo_vuelve_como_texto(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        odoo.por_nombre("LUMI_302")["x_operador"] = "TR"
        correr(odoo, trk)
        self.assertEqual(trk.data["seg"][0]["opDesp"], "Trice")
        self.assertEqual(trk.data["seg"][1]["opDesp"], "Petdur", "otro registro intacto")

    def test_diff_desviado_aborta_y_no_pierde_la_edicion_de_odoo(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        odoo.por_nombre("LUMI_302")["x_notas"] = "editada"
        orig = S.aplicar_cambios_tracker

        def torcido(data, cambios, ms):
            n = orig(data, cambios, ms)
            n["seg"][1]["estado"] = "Entregado"
            return n
        with mock.patch.object(S, "aplicar_cambios_tracker", torcido):
            res, logs = correr(odoo, trk)
        self.assertFalse(res["writeback_ok"])
        self.assertEqual(trk.escrituras, 0)
        self.assertTrue(any("NO se escribe" in l for l in logs))
        r = odoo.por_nombre("LUMI_302")
        self.assertEqual((r["x_notas"], r["x_notas_sync"]), ("editada", "nota A"),
                         "la edicion queda en Odoo y el snapshot sin tocar: la proxima corrida la reintenta")
        res2, _ = correr(odoo, trk)
        self.assertTrue(res2["tracker_escrito"])
        self.assertEqual(trk.data["seg"][0]["notas"], "editada")

    def test_conflicto_de_etag_reintenta(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        odoo.por_nombre("LUMI_302")["x_notas"] = "editada"
        trk.conflictos_pendientes = 1
        res, logs = correr(odoo, trk)
        self.assertTrue(res["tracker_escrito"] and res["writeback_ok"])
        self.assertEqual(trk.data["seg"][0]["notas"], "editada")
        self.assertTrue(any("reintento" in l for l in logs))

    def test_conflicto_persistente_no_pierde_nada(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        odoo.por_nombre("LUMI_302")["x_notas"] = "editada"
        trk.conflictos_pendientes = 99
        res, _ = correr(odoo, trk)
        self.assertFalse(res["writeback_ok"])
        self.assertEqual(trk.data["seg"][0]["notas"], "nota A")
        self.assertEqual(odoo.por_nombre("LUMI_302")["x_notas_sync"], "nota A")

    def test_edicion_concurrente_en_odoo_entre_fases_no_se_pisa(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        trk.data["seg"][0]["notas"] = "cambio del tracker"

        def concurrente(n):
            if n == 2:  # la lectura de la fase B ya ve una edicion hecha entre las dos lecturas
                odoo.por_nombre("LUMI_302")["x_notas"] = "la escribio un usuario justo ahora"
        odoo.hook_search_read = concurrente
        odoo.n_search_read = 0
        correr(odoo, trk)
        r = odoo.por_nombre("LUMI_302")
        self.assertEqual(r["x_notas"], "la escribio un usuario justo ahora")
        self.assertEqual(r["x_notas_sync"], "nota A", "queda para la proxima corrida")

    def test_dry_run_no_escribe_nada(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        res, logs = correr(odoo, trk, dry_run=True)
        self.assertEqual(odoo.recs, {})
        correr(odoo, trk)
        odoo.por_nombre("LUMI_302")["x_notas"] = "editada"
        antes = copy.deepcopy(odoo.recs)
        res, logs = correr(odoo, trk, dry_run=True)
        self.assertEqual(odoo.recs, antes)
        self.assertEqual(trk.escrituras, 0)
        self.assertTrue(any("dry-run" in l for l in logs))

    def test_operador_vacio_en_el_tracker_va_como_false(self):
        data = tracker_base()
        data["seg"][1]["opDesp"] = ""
        odoo, trk = FakeOdoo(), FakeTracker(data)
        correr(odoo, trk)
        self.assertIs(odoo.por_nombre("LUMI_304")["x_operador"], False)
        self.assertEqual(odoo.por_nombre("LUMI_304")["x_operador_sync"], "")
        correr(odoo, trk)  # segunda corrida: sigue sin ValueError ni cambios espureos
        self.assertEqual(trk.escrituras, 0)

    def test_embarque_que_ya_no_esta_en_el_tracker_no_se_borra(self):
        odoo, trk = FakeOdoo(), FakeTracker(tracker_base())
        correr(odoo, trk)
        trk.data["seg"] = [s for s in trk.data["seg"] if s["id"] != "LUMI_304"]
        correr(odoo, trk)
        self.assertEqual(len(odoo.recs), 2)


if __name__ == "__main__":
    unittest.main()
