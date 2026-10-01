#!/usr/bin/env python3
"""
Job desatendido: lee facturas de despacho/forwarder desde Odoo (SOLO LECTURA)
y escribe comex_odoo_real.json en SharePoint, al lado de comex_data.json,
para que la herramienta COMEX lo lea como overlay.

FASE 1 (gastosReal) -- implementada.
FASE 2 (nacReal) -- NO implementada: los codigos de cuenta del handoff
(118001, 114101, 114103, 114307, 114601, 1142xx, 211101, 118005, 118006)
no existen en el plan de cuentas real de LUMINATEC (que usa codigos
jerarquicos con puntos, ej. "1.1.4.02.010"). Hace falta el mapeo real de
Nacho antes de poder escribirla. Mientras tanto, nacReal y nacEstSnap
quedan en None para todos los embarques.

Nunca escribe en Odoo. El unico archivo que escribe es comex_odoo_real.json.
Idempotente: reescribe el archivo completo en cada corrida.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ODOO_BASE = os.environ.get("ODOO_BASE", "https://gpowerbyte-luminatec.odoo.com")
ODOO_DB = os.environ.get("ODOO_DB", "gpowerbyte-luminatec-master-22753148")
ODOO_LOGIN = os.environ["ODOO_LOGIN"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

GRAPH_TENANT_ID = os.environ["GRAPH_TENANT_ID"]
GRAPH_CLIENT_ID = os.environ["GRAPH_CLIENT_ID"]
GRAPH_CLIENT_SECRET = os.environ["GRAPH_CLIENT_SECRET"]

SP_HOST = "luminatec.sharepoint.com"
SP_SITE = "/sites/Luminatec-Operacines"
OUTPUT_FILE = "comex_odoo_real.json"

MUNDO_COMEX_VAT = "30717845419"
# ilike 'trice%' (no '%trice%'): una busqueda por substring sin ancla de
# inicio matchea falsos positivos reales, ej. un partner persona humana
# "Martin GabrielPetricevich" (contiene "tricev" como substring).
TRICE_NAME_PATTERN = "trice%"

COHORTE_RE = re.compile(r"(LUMI|LUPE)[_ ]?0?(\d{2,3})", re.IGNORECASE)


def odoo_jsonrpc(method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": "call", "params": params}
    ).encode("utf-8")
    req = urllib.request.Request(
        ODOO_BASE + "/jsonrpc",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError("Odoo JSON-RPC error en %s: %s" % (method, body["error"]))
    return body["result"]


def odoo_authenticate():
    uid = odoo_jsonrpc(
        "authenticate",
        {
            "service": "common",
            "method": "authenticate",
            "args": [ODOO_DB, ODOO_LOGIN, ODOO_API_KEY, {}],
        },
    )
    if not uid:
        raise RuntimeError("Odoo: authenticate devolvio uid vacio (credenciales?)")
    return uid


def odoo_execute_kw(uid, model, method, args, kwargs=None):
    return odoo_jsonrpc(
        "execute_kw",
        {
            "service": "object",
            "method": "execute_kw",
            "args": [ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs or {}],
        },
    )


def normalizar_cohorte(ref):
    """Funcion pura. Devuelve 'LUMI_###' (3 digitos, cero-padded) o None.
    LUPE_ (Peru) se ignora a proposito -- solo aplica Argentina (LUMI_)."""
    if not ref:
        return None
    if "apertura" in ref.lower():
        return None
    m = COHORTE_RE.search(ref)
    if not m or m.group(1).upper() != "LUMI":
        return None
    return "LUMI_%03d" % int(m.group(2))


def tc_oficial_fecha(fecha_iso):
    """Fallback 3: TC oficial ARS publico para una fecha dada.
    Fuente: bluelytics (serie historica diaria de TC oficial). Devuelve
    None si no hay dato para esa fecha o la consulta falla -- nunca
    estima con un valor inventado."""
    try:
        req = urllib.request.Request(
            "https://api.bluelytics.com.ar/v2/evolution.json",
            headers={"User-Agent": "luminatec-comex-reales/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            serie = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print("WARN: no se pudo consultar TC oficial publico: %s" % e, file=sys.stderr)
        return None
    for row in serie:
        if row.get("date") == fecha_iso and row.get("source") == "Oficial":
            venta = row.get("value_sell")
            if venta:
                return float(venta)
    return None


def resolver_tc_cohorte(uid, cohorte, facturas_cohorte, fecha_referencia):
    """Orden de fallback del handoff: (1) factura USD del mismo embarque,
    (2) x_lumi_tc_historico del DI de esa cohorte, (3) TC oficial publico."""
    for f in facturas_cohorte:
        if f["currency_code"] == "USD" and f["amount_untaxed"]:
            return (
                abs(f["amount_untaxed_signed"]) / abs(f["amount_untaxed"]),
                "factura_usd_%s" % cohorte,
            )
    dis = odoo_execute_kw(
        uid,
        "account.move",
        "search_read",
        [[["ref", "ilike", cohorte], ["name", "ilike", "DI "]]],
        {"fields": ["name", "x_lumi_tc_historico"], "limit": 5},
    )
    for di in dis:
        tc = di.get("x_lumi_tc_historico")
        if tc:
            return (float(tc), "x_lumi_tc_historico DI %s" % di["name"])
    tc_pub = tc_oficial_fecha(fecha_referencia)
    if tc_pub:
        return (tc_pub, "TC oficial publico %s" % fecha_referencia)
    return (None, None)


def build_gastos_reales(uid):
    partners = odoo_execute_kw(
        uid, "res.partner", "search_read", [[["vat", "=", MUNDO_COMEX_VAT]]], {"fields": ["id", "name"]}
    )
    mundo_comex_ids = {p["id"] for p in partners}
    trice_partners = odoo_execute_kw(
        uid,
        "res.partner",
        "search_read",
        [[["name", "=ilike", TRICE_NAME_PATTERN]]],
        {"fields": ["id", "name"]},
    )
    trice_ids = {p["id"] for p in trice_partners}
    partner_ids = list(mundo_comex_ids | trice_ids)
    if not partner_ids:
        raise RuntimeError("No encontre ni Mundo Comex ni Trice como partners -- abortando")

    facturas = odoo_execute_kw(
        uid,
        "account.move",
        "search_read",
        [
            [
                ["partner_id", "in", partner_ids],
                ["move_type", "=", "in_invoice"],
                ["state", "=", "posted"],
            ]
        ],
        {
            "fields": [
                "ref",
                "partner_id",
                "amount_untaxed",
                "amount_untaxed_signed",
                "currency_id",
                "invoice_date",
            ],
            "context": {"allowed_company_ids": [1, 2, 3, 4, 5, 6]},
            "limit": 1000,
        },
    )

    por_cohorte = {}
    for f in facturas:
        cohorte = normalizar_cohorte(f.get("ref"))
        if not cohorte:
            continue
        f["currency_code"] = (f["currency_id"] or [None, ""])[1]
        f["_es_mc"] = f["partner_id"][0] in mundo_comex_ids
        por_cohorte.setdefault(cohorte, []).append(f)

    embarques = []
    for cohorte, items in sorted(por_cohorte.items()):
        items.sort(key=lambda f: f.get("invoice_date") or "")
        fecha_ref = items[-1].get("invoice_date") or datetime.now().date().isoformat()
        tc, tc_src = resolver_tc_cohorte(uid, cohorte, items, fecha_ref)

        total_usd = mc_usd = tr_usd = 0.0
        sin_tc = False
        for f in items:
            if f["currency_code"] == "USD":
                usd = f["amount_untaxed"]
            elif f["currency_code"] == "ARS":
                if not tc:
                    sin_tc = True
                    continue
                usd = abs(f["amount_untaxed_signed"]) / tc
            else:
                continue
            total_usd += usd
            if f["_es_mc"]:
                mc_usd += usd
            else:
                tr_usd += usd

        if sin_tc and total_usd == 0:
            print(
                "WARN: %s tiene facturas en ARS sin TC resoluble, se omite" % cohorte,
                file=sys.stderr,
            )
            continue

        mc_presente = any(f["_es_mc"] for f in items)
        tr_presente = any(not f["_es_mc"] for f in items)
        parcial = mc_presente != tr_presente or sin_tc

        embarques.append(
            {
                "id": cohorte,
                "gastosReal": {
                    "total": round(total_usd, 2),
                    "mc": round(mc_usd, 2),
                    "tr": round(tr_usd, 2),
                    "moneda": "USD",
                    "tcUsado": round(tc, 4) if tc else None,
                    "tcFuente": tc_src,
                    "parcial": parcial,
                    "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
                },
                "nacReal": None,
                "nacEstSnap": None,
            }
        )
    return embarques


def graph_token():
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": GRAPH_CLIENT_ID,
            "client_secret": GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % GRAPH_TENANT_ID,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode("utf-8"))
    if "access_token" not in body:
        raise RuntimeError("Graph: no pude obtener token (%s)" % body)
    return body["access_token"]


def graph_request(token, path, method="GET", body=None, extra_headers=None):
    headers = {"Authorization": "Bearer " + token}
    headers.update(extra_headers or {})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0" + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def write_to_sharepoint(payload):
    token = graph_token()
    site = graph_request(token, "/sites/%s:%s" % (SP_HOST, SP_SITE))
    site_id = site.get("id")
    if not site_id:
        raise RuntimeError("Graph: no encuentro el sitio de SharePoint %s" % SP_SITE)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0/sites/%s/drive/root:/%s:/content" % (site_id, OUTPUT_FILE),
        data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status not in (200, 201):
            raise RuntimeError("Graph: PUT de %s devolvio status %s" % (OUTPUT_FILE, r.status))


def main():
    uid = odoo_authenticate()
    embarques = build_gastos_reales(uid)
    payload = {
        "generado": datetime.now(timezone.utc).astimezone().isoformat(),
        "fuente": "Odoo (GitHub Action)",
        "embarques": embarques,
    }
    parciales = sum(1 for e in embarques if e["gastosReal"]["parcial"])
    sin_tc = sum(1 for e in embarques if e["gastosReal"]["tcUsado"] is None)
    print(
        "Resumen: %d embarques, %d parciales, %d sin TC resoluble"
        % (len(embarques), parciales, sin_tc)
    )
    write_to_sharepoint(payload)
    print("OK: %s escrito en SharePoint (%s)" % (OUTPUT_FILE, SP_SITE))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
