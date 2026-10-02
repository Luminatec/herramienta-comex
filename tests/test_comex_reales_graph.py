import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

for k in ("ODOO_LOGIN", "ODOO_API_KEY", "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
    os.environ.setdefault(k, "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import comex_odoo_sync  # noqa: E402
import comex_reales as R  # noqa: E402


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestGraphHelpers(unittest.TestCase):
    def test_put_con_if_match_y_412_es_conflicto(self):
        vistos = {}

        def fake(req, timeout=0):
            vistos["if-match"] = req.get_header("If-match")
            vistos["method"] = req.get_method()
            raise urllib.error.HTTPError(req.full_url, 412, "Precondition Failed", {}, io.BytesIO(b""))

        with mock.patch.object(R.urllib.request, "urlopen", fake):
            with self.assertRaises(comex_odoo_sync.ConflictoTracker):
                R.graph_put_file("tok", "site", "comex_data.json", b"{}", if_match='"etag1"')
        self.assertEqual(vistos, {"if-match": '"etag1"', "method": "PUT"})

    def test_put_otro_error_se_propaga(self):
        def fake(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b""))

        with mock.patch.object(R.urllib.request, "urlopen", fake):
            with self.assertRaises(urllib.error.HTTPError):
                R.graph_put_file("tok", "site", "x.json", b"{}")

    def test_get_json_con_etag_baja_sin_authorization(self):
        pedidos = []

        def fake_graph(token, path, **kw):
            return {"eTag": '"abc"', "@microsoft.graph.downloadUrl": "https://download.example/x"}

        def fake_urlopen(req, timeout=0):
            pedidos.append((req.full_url, req.get_header("Authorization")))
            return Resp(json.dumps({"_ts": 5, "seg": []}).encode("utf-8"))

        with mock.patch.object(R, "graph_request", fake_graph), mock.patch.object(R.urllib.request, "urlopen", fake_urlopen):
            data, etag = R.graph_get_json_con_etag("tok", "site", "comex_data.json")
        self.assertEqual((data, etag), ({"_ts": 5, "seg": []}, '"abc"'))
        self.assertEqual(pedidos, [("https://download.example/x", None)])

    def test_get_json_sin_etag_falla(self):
        with mock.patch.object(R, "graph_request", lambda *a, **k: {}):
            with self.assertRaises(RuntimeError):
                R.graph_get_json_con_etag("tok", "site", "comex_data.json")


class TestMatchEmbarqueFolder(unittest.TestCase):
    def test_nombre_exacto_o_con_sufijo_no_numerico(self):
        items = [{"folder": True, "name": "LUMI_304"}]
        self.assertEqual(R._match_embarque_folder(items, "LUMI_304")["name"], "LUMI_304")
        self.assertEqual(R._match_embarque_folder([{"folder": True, "name": "LUMI_304 - FJ"}],
                                                   "LUMI_304")["name"], "LUMI_304 - FJ")

    def test_no_matchea_un_prefijo_de_otro_numero(self):
        items = [{"folder": True, "name": "LUMI_304"}]
        self.assertIsNone(R._match_embarque_folder(items, "LUMI_30"))

    def test_ignora_archivos(self):
        items = [{"folder": False, "name": "LUMI_304"}]
        self.assertIsNone(R._match_embarque_folder(items, "LUMI_304"))


class TestResolverCarpetaSp(unittest.TestCase):
    def test_carpeta_directa_bajo_comex(self):
        def fake_graph(token, path, **kw):
            if path == "/sites/site/drive/root:/COMEX":
                return {"id": "root1"}
            if path == "/sites/site/drive/items/root1/children?$top=400":
                return {"value": [{"folder": True, "name": "LUMI_304", "id": "f1"}]}
            if path == "/sites/site/drive/items/f1?$select=webUrl":
                return {"webUrl": "https://sp.example/LUMI_304"}
            raise AssertionError("path inesperado: %s" % path)

        with mock.patch.object(R, "graph_request", fake_graph):
            self.assertEqual(R.resolver_carpeta_sp("tok", "site", "LUMI_304"), "https://sp.example/LUMI_304")

    def test_carpeta_dentro_de_subcarpeta_de_anio(self):
        def fake_graph(token, path, **kw):
            if path == "/sites/site/drive/root:/COMEX":
                return {"id": "root1"}
            if path == "/sites/site/drive/items/root1/children?$top=400":
                return {"value": [{"folder": True, "name": "2024", "id": "y2024"}]}
            if path == "/sites/site/drive/items/y2024/children?$top=400":
                return {"value": [{"folder": True, "name": "LUMI_304", "id": "f1"}]}
            if path == "/sites/site/drive/items/f1?$select=webUrl":
                return {"webUrl": "https://sp.example/LUMI_304"}
            raise AssertionError("path inesperado: %s" % path)

        with mock.patch.object(R, "graph_request", fake_graph):
            self.assertEqual(R.resolver_carpeta_sp("tok", "site", "LUMI_304"), "https://sp.example/LUMI_304")

    def test_no_encontrada_devuelve_none(self):
        def fake_graph(token, path, **kw):
            if path == "/sites/site/drive/root:/COMEX":
                return {"id": "root1"}
            if path == "/sites/site/drive/items/root1/children?$top=400":
                return {"value": [{"folder": True, "name": "LUMI_999", "id": "otro"}]}
            raise AssertionError("path inesperado: %s" % path)

        with mock.patch.object(R, "graph_request", fake_graph):
            self.assertIsNone(R.resolver_carpeta_sp("tok", "site", "LUMI_304"))

    def test_sin_carpeta_comex_devuelve_none(self):
        def fake_graph(token, path, **kw):
            if path == "/sites/site/drive/root:/COMEX":
                raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO(b""))
            raise AssertionError("path inesperado: %s" % path)

        with mock.patch.object(R, "graph_request", fake_graph):
            self.assertIsNone(R.resolver_carpeta_sp("tok", "site", "LUMI_304"))


if __name__ == "__main__":
    unittest.main()
