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


if __name__ == "__main__":
    unittest.main()
